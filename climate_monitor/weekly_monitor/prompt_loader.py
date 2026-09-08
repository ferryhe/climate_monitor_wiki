from __future__ import annotations

import hashlib
import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from string import Template


JOB_ROOT = (
    Path(__file__).resolve().parents[2]
    / "monitoring"
    / "jobs"
    / "weekly-climate-monitor-08h"
)
DEFAULT_WEEKLY_MONITOR_PROMPT_META_PATH = JOB_ROOT / "prompts" / "weekly-monitor-v1.meta.json"
DEFAULT_ARTICLE_RELEVANCE_PATH = JOB_ROOT / "prompts" / "article-relevance-v1.prompt.md"
DEFAULT_PILLAR_B_SEARCH_PATH = JOB_ROOT / "prompts" / "pillar-b-search-v1.prompt.md"


def load_article_relevance_rules(path: str | Path | None = None) -> str:
    """Rules are embedded in the existing URL request and its checkpoint hash."""
    rules = Path(path or DEFAULT_ARTICLE_RELEVANCE_PATH).read_text(encoding="utf-8").strip()
    if not rules:
        raise ValueError("article relevance rules are empty")
    return rules


@dataclass(frozen=True)
class LoadedPrompt:
    prompt_id: str
    version: str
    path: Path
    raw_bytes: bytes
    sha256: str


def load_pillar_b_search_prompt(
    report_date: date, output_path: str | Path, *, path: str | Path | None = None,
) -> LoadedPrompt:
    """Render the editable search task; no search, inference, or file writes.

    The window is three calendar months anchored to the report date, with
    month-end clamping. The production consumer validates the dated envelope.
    """
    destination = Path(output_path)
    if not destination.is_absolute():
        raise ValueError("Pillar B output path must be absolute")
    start = pillar_b_window_start(report_date)
    prompt_path = Path(path) if path is not None else DEFAULT_PILLAR_B_SEARCH_PATH
    template = prompt_path.read_text(encoding="utf-8")
    values = {"report_date": report_date.isoformat(), "window_start": start.isoformat(),
              "search_start": (start - timedelta(days=1)).isoformat(),
              "search_end": (report_date + timedelta(days=1)).isoformat(),
              "output_path_json": json.dumps(str(destination), ensure_ascii=False)}
    # A missing placeholder would silently turn the live search into a stale
    # copied task. Keep the date window and destination bound by the renderer.
    if any("${" + key + "}" not in template for key in values):
        raise ValueError("Pillar B template must retain its date/window/output placeholders")
    try:
        raw = Template(template).substitute(values).encode("utf-8")
    except (ValueError, KeyError) as exc:
        raise ValueError("Pillar B template has invalid or unknown placeholders") from exc
    return LoadedPrompt(prompt_id="pillar_b_search", version="v1", path=prompt_path,
                        raw_bytes=raw, sha256=hashlib.sha256(raw).hexdigest())


def pillar_b_window_start(report_date: date) -> date:
    month_index = report_date.year * 12 + report_date.month - 1 - 3
    year, month = divmod(month_index, 12)
    month += 1
    return date(year, month, min(report_date.day, monthrange(year, month)[1]))


def pillar_b_search_queries(report_date: date) -> list[str]:
    """Use the editable prompt as the only query-set definition."""
    text = load_pillar_b_search_prompt(report_date, DEFAULT_PILLAR_B_SEARCH_PATH.resolve()).raw_bytes.decode("utf-8")
    lines = text.splitlines()
    headings = [i for i, line in enumerate(lines) if line.strip() == "## Search queries"]
    if len(headings) != 1:
        raise ValueError("Pillar B prompt must contain exactly one ## Search queries section")
    section = lines[headings[0] + 1:]
    end = next((i for i, line in enumerate(section) if line.startswith("## ")), len(section))
    queries = [line[2:].strip() for line in section[:end] if line.startswith("- ")]
    if not queries or any(not query for query in queries) or len(queries) != len(set(queries)):
        raise ValueError("Pillar B search queries must be non-empty and unique")
    return queries


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
