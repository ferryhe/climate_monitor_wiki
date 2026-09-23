#!/usr/bin/env python3
"""Short-lived isolated worker for one saved acquisition batch."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_monitor.meetings import process_batch  # noqa: E402
from scripts.run_climate_monitor import (  # noqa: E402
    _hermes_authoring_invocation,
    _parse_hermes_quiet_response,
)


def _extractor(provider: str, model: str, acquisition_binding_path=None, *, meeting_binding=None):
    help_text = None

    def invoke(request):
        nonlocal help_text
        from climate_monitor.hermes_identity import inference_runtime
        if not acquisition_binding_path:
            raise ValueError("Hermes snapshot missing; start a fresh run")
        if meeting_binding is not None:
            path, acquisition = _linked_acquisition(meeting_binding)
        else:
            path = Path(acquisition_binding_path).resolve(strict=True)
            acquisition = json.loads(path.read_text())
        executable, environment, home = inference_runtime(
            path.parent, acquisition.get("hermes_snapshot"), purpose="meetings",
            source=f"climate-acquisition-{acquisition['run_id']}",
        )
        if hashlib.sha256(request["article_body"].encode("utf-8")).hexdigest() != request["content_sha256"]:
            raise ValueError("meeting worker body hash mismatch")
        if help_text is None:
            try:
                help_result = subprocess.run(
                    [*(executable if isinstance(executable, list) else [executable]), "chat", "--help"], capture_output=True, text=True,
                    encoding="utf-8", cwd=home, env=environment, timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError("Hermes meeting extraction is unavailable") from exc
            from climate_monitor.hermes_auth_state import verify_auth
            verify_auth(home)
            if help_result.returncode:
                raise RuntimeError("Hermes meeting extraction help probe failed")
            help_text = help_result.stdout
        instruction = (
            request["prompt"]
            + "\n\nUntrusted source metadata and exact persisted body:\n"
            + json.dumps({
                "content_version_id": request["content_version_id"],
                "content_sha256": request["content_sha256"],
                "source_url": request["source_url"],
                "article_body": request["article_body"],
            }, ensure_ascii=False, sort_keys=True)
        )
        command, stdin = _hermes_authoring_invocation(
            help_text, instruction, model=model, provider=provider,
        )
        command[:1] = executable if isinstance(executable, list) else [executable]
        from climate_monitor.hermes_identity import auth_execution
        with auth_execution(home, require_identity=True) as auth_result:
            completed = subprocess.run(
                command, input=stdin, capture_output=True, text=True, encoding="utf-8",
                cwd=home, env=environment, timeout=300,
            )
            auth_result["returncode"] = completed.returncode
        if completed.returncode or not completed.stdout.strip():
            raise ValueError("Hermes meeting response absent or failed")
        return _parse_hermes_quiet_response(completed.stdout, completed.stderr)

    return invoke


def _linked_acquisition(binding):
    # Execution needs a verifiable acquisition link; historical status readers
    # do not use this worker contract.
    try:
        path = Path(binding['acquisition_binding_path']).resolve(strict=True)
        acquisition = json.loads(path.read_text(encoding='utf-8'))
        for meeting_key, acquisition_key in (
                ('acquisition_run_id', 'run_id'), ('acquisition_batch_id', 'acquisition_batch_id'),
                ('task_version', 'task_version'), ('registry_database', 'registry_database'),
                ('hermes_snapshot', 'hermes_snapshot')):
            if binding[meeting_key] != acquisition[acquisition_key]:
                raise ValueError()
        from climate_monitor.hermes_identity import load_snapshot
        payload = load_snapshot(path.parent, binding['hermes_snapshot'])
        if payload['source'] != f"climate-acquisition-{acquisition['run_id']}":
            raise ValueError()
        return path, acquisition
    except (KeyError, TypeError, ValueError, OSError):
        raise ValueError('meeting acquisition link missing or inconsistent; start a fresh run') from None


def run(binding: dict) -> dict:
    required = {
        "schema_version", "acquisition_run_id", "acquisition_batch_id", "registry_database",
        "meeting_attempt", "retry_failed", "task_version", "prompt_version", "prompt_sha256",
        "prompt_text", "retry_meeting_run_id",
    }
    if (not required <= set(binding) <= required | {"provider", "model", "acquisition_binding_path", "hermes_snapshot"}
            or binding.get("schema_version") != "climate-meeting-worker-binding.v1"):
        raise ValueError("invalid meeting worker binding")
    identity_fields = {"provider", "model"} & binding.keys()
    if identity_fields and (identity_fields != {"provider", "model"} or not all(
        isinstance(binding[key], str) and binding[key].strip() for key in identity_fields
    )):
        raise ValueError("provider and model must both be absent or non-empty strings")
    prompt = str(binding["prompt_text"]).replace("\r\n", "\n").replace("\r", "\n")
    if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != binding["prompt_sha256"]:
        raise ValueError("meeting worker prompt hash mismatch")
    _linked_acquisition(binding)
    return process_batch(
        binding["registry_database"], binding["acquisition_batch_id"],
        prompt_text=prompt, prompt_version=binding["prompt_version"],
        provider=binding.get("provider", ""), model=binding.get("model", ""),
        extractor=_extractor(binding.get("provider", ""), binding.get("model", ""),
                             binding.get("acquisition_binding_path"), meeting_binding=binding),
        retry_failed=bool(binding["retry_failed"]),
        retry_meeting_run_id=binding["retry_meeting_run_id"],
        task_version=int(binding["task_version"]),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binding", type=Path, required=True)
    args = parser.parse_args(argv)
    binding = json.loads(args.binding.read_text(encoding="utf-8"))
    try:
        result = run(binding)
    except Exception as exc:
        result = {"status": "worker_failed", "error": f"{type(exc).__name__}: {str(exc)[:800]}"}
        code = 1
    else:
        code = 0 if result["status"] in {"succeeded", "no_content"} else 1
    result_path = args.binding.with_name(args.binding.stem + "-result.json")
    result_path.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
