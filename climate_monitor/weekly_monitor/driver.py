from __future__ import annotations

import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from ..orchestrator import run_monitor
from .authoring_contract import AUTHORING_CONTRACT_VERSION, load_authoring_response
from .prompt_loader import load_weekly_monitor_prompt


DRIVER_VERSION = "weekly-monitor-driver.v1"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SECRET_WORDS = ("api_key", "credential", "password", "secret", "token")


def run_weekly_monitor(
    *,
    source_config_path: str | Path = "monitoring/supranational_sources.yaml",
    run_config_path: str | Path = "monitoring/run_config.yaml",
    report_date: date | None = None,
    manifest_fixture_path: str | Path | None = None,
    research_fixture_path: str | Path | None = None,
    site_scopes_path: str | Path | None = "monitoring/site_scopes.yaml",
    state_dir: str | Path = "monitoring/state",
    source_dir: str | Path | None = None,
    wiki_dir: str | Path | None = None,
    sync: bool = True,
    update_seen_state: bool = True,
    authoring_response_path: str | Path | None = None,
    prompt_path: str | Path | None = None,
    repository_commit_sha: str | None = None,
    model_provider: str = "",
    model: str = "",
    temperature: float | None = None,
    max_output_tokens: int | None = None,
):
    if authoring_response_path is None:
        raise ValueError("production weekly driver requires an authoring response file")
    prompt = load_weekly_monitor_prompt(prompt_path) if prompt_path else load_weekly_monitor_prompt()
    commit_sha = repository_commit_sha or _repository_commit_sha(Path.cwd())
    if not _GIT_SHA.fullmatch(commit_sha):
        raise ValueError("repository commit SHA must be a 40-character lowercase hex digest")
    return run_monitor(
        source_config_path=source_config_path,
        run_config_path=run_config_path,
        report_date=report_date,
        manifest_fixture_path=manifest_fixture_path,
        research_fixture_path=research_fixture_path,
        site_scopes_path=site_scopes_path,
        state_dir=state_dir,
        source_dir=source_dir,
        wiki_dir=wiki_dir,
        sync=sync,
        update_seen_state=update_seen_state,
        authoring_response=load_authoring_response(authoring_response_path),
        prompt_provenance={
            "id": prompt.prompt_id,
            "version": prompt.version,
            "sha256": prompt.sha256,
        },
        driver_version=DRIVER_VERSION,
        contract_version=AUTHORING_CONTRACT_VERSION,
        repository_commit_sha=commit_sha,
        model_metadata=_model_metadata(
            provider=model_provider,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        ),
    )


def _repository_commit_sha(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _model_metadata(
    *,
    provider: str,
    model: str,
    temperature: float | None,
    max_output_tokens: int | None,
) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    if temperature is not None:
        if not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        settings["temperature"] = float(temperature)
    if max_output_tokens is not None:
        if isinstance(max_output_tokens, bool) or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        settings["max_output_tokens"] = int(max_output_tokens)
    return {
        "provider": _safe_public_label(provider, field="model provider"),
        "model": _safe_public_label(model, field="model"),
        "settings": {name: settings[name] for name in sorted(settings)},
    }


def _safe_public_label(value: str, *, field: str) -> str:
    cleaned = " ".join(str(value or "").split())
    lowered = cleaned.casefold()
    if len(cleaned) > 120 or any(word in lowered for word in _SECRET_WORDS) or "sk-" in lowered:
        raise ValueError(f"{field} is not safe public metadata")
    if any(marker in cleaned for marker in ("\\", "/", ":", "\n", "\r", "\t")):
        raise ValueError(f"{field} must not contain paths or control characters")
    return cleaned
