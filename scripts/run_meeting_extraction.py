#!/usr/bin/env python3
"""Short-lived isolated worker for one saved acquisition batch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_monitor.meetings import process_batch  # noqa: E402
from scripts.run_climate_monitor import (  # noqa: E402
    _hermes_authoring_invocation,
    _hermes_quiet_session_id,
    _parse_hermes_quiet_response,
)
from climate_monitor.hermes_identity import observe_session_route  # noqa: E402


def _extractor(provider: str, model: str, *, hermes_home: str | None = None):
    help_text = None
    environment = {**os.environ, **({"HERMES_HOME": hermes_home} if hermes_home else {})}

    def invoke(request):
        nonlocal help_text
        if hashlib.sha256(request["article_body"].encode("utf-8")).hexdigest() != request["content_sha256"]:
            raise ValueError("meeting worker body hash mismatch")
        if help_text is None:
            try:
                help_result = subprocess.run(
                    ["hermes", "chat", "--help"], capture_output=True, text=True,
                    encoding="utf-8", cwd=ROOT, timeout=30, env=environment,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError("Hermes meeting extraction is unavailable") from exc
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
            help_text, instruction,
            model="" if hermes_home else model,
            provider="" if hermes_home else provider,
        )
        completed = subprocess.run(
            command, input=stdin, capture_output=True, text=True, encoding="utf-8",
            cwd=ROOT, timeout=300, env=environment,
        )
        if completed.returncode or not completed.stdout.strip():
            raise ValueError("Hermes meeting response absent or failed")
        response = _parse_hermes_quiet_response(completed.stdout, completed.stderr)
        if hermes_home:
            actual = observe_session_route(
                Path(hermes_home), _hermes_quiet_session_id(completed.stderr),
            )
            if actual != (provider, model):
                raise ValueError(
                    "Hermes effective identity changed; start a fresh managed run"
                )
        return response

    return invoke


def run(binding: dict) -> dict:
    required = {
        "schema_version", "acquisition_run_id", "acquisition_batch_id", "registry_database",
        "meeting_attempt", "retry_failed", "task_version", "prompt_version", "prompt_sha256",
        "prompt_text", "provider", "model", "retry_meeting_run_id",
    }
    if (set(binding) not in {frozenset(required), frozenset(required | {"hermes_home"})}
            or binding.get("schema_version") != "climate-meeting-worker-binding.v1"):
        raise ValueError("invalid meeting worker binding")
    hermes_home = binding.get("hermes_home")
    if hermes_home is not None and (
        not isinstance(hermes_home, str) or not Path(hermes_home).is_absolute()
    ):
        raise ValueError("meeting worker Hermes home must be an absolute path")
    prompt = str(binding["prompt_text"]).replace("\r\n", "\n").replace("\r", "\n")
    if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != binding["prompt_sha256"]:
        raise ValueError("meeting worker prompt hash mismatch")
    return process_batch(
        binding["registry_database"], binding["acquisition_batch_id"],
        prompt_text=prompt, prompt_version=binding["prompt_version"],
        provider=binding["provider"], model=binding["model"],
        extractor=_extractor(
            binding["provider"], binding["model"], hermes_home=hermes_home,
        ),
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
