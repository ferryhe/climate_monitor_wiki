from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


JOB_ROOT = (
    Path(__file__).resolve().parents[2]
    / "monitoring"
    / "jobs"
    / "weekly-climate-monitor-08h"
)
DEFAULT_WEEKLY_MONITOR_PROMPT_META_PATH = JOB_ROOT / "prompts" / "weekly-monitor-v1.meta.json"


@dataclass(frozen=True)
class LoadedPrompt:
    prompt_id: str
    version: str
    path: Path
    raw_bytes: bytes
    sha256: str


def load_weekly_monitor_prompt(
    path: str | Path | None = None,
    *,
    meta_path: str | Path = DEFAULT_WEEKLY_MONITOR_PROMPT_META_PATH,
) -> LoadedPrompt:
    meta = _load_prompt_meta(Path(meta_path))
    prompt_path = Path(path) if path is not None else JOB_ROOT / str(meta["prompt_path"])
    raw = prompt_path.read_bytes()
    if not raw:
        raise ValueError("weekly monitor prompt is empty")
    sha256 = hashlib.sha256(raw).hexdigest()
    expected_sha256 = str(meta["sha256"])
    if path is None and sha256 != expected_sha256:
        raise ValueError("weekly monitor prompt SHA-256 does not match metadata")
    return LoadedPrompt(
        prompt_id=str(meta["prompt_id"]),
        version=str(meta["version"]),
        path=prompt_path,
        raw_bytes=raw,
        sha256=sha256,
    )


def _load_prompt_meta(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("weekly monitor prompt metadata must be a JSON object")
    required = {
        "schema_version",
        "prompt_id",
        "version",
        "prompt_path",
        "sha256",
        "authoring_request_schema",
        "authoring_response_schema",
        "contract_version",
    }
    if set(payload) != required:
        raise ValueError("weekly monitor prompt metadata has unexpected fields")
    if payload["schema_version"] != "weekly-monitor-prompt-meta.v1":
        raise ValueError("unsupported weekly monitor prompt metadata version")
    return payload
