#!/usr/bin/env python3
"""Inspect one configured source through the governed web_listening adapter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from climate_monitor.config import load_site_scopes, load_sources  # noqa: E402
from climate_monitor.web_listening_adapter import (  # noqa: E402
    _seed_urls,
    collect_source_items,
)


def _external_path(parser: argparse.ArgumentParser, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        parser.error("state and runtime directories must be absolute paths")
    resolved = path.resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        parser.error("state and runtime directories must be outside the repository")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-key", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--seed-url", help="One exact configured seed; omit for all seeds")
    args = parser.parse_args()

    sources = {source.key: source for source in load_sources(ROOT / "monitoring/supranational_sources.yaml")}
    scopes = {scope.source_key: scope for scope in load_site_scopes(ROOT / "monitoring/site_scopes.yaml")}
    source = sources.get(args.source_key)
    scope = scopes.get(args.source_key)
    if source is None or scope is None:
        parser.error("source key must exist in both the inventory and reviewed scopes")
    if args.seed_url:
        if args.seed_url not in _seed_urls(source, scope):
            parser.error("seed URL must exactly match a configured seed")
        scope = replace(scope, seed_urls=(args.seed_url,), include_source_url=False)

    state_dir = _external_path(parser, args.state_dir)
    runtime_dir = _external_path(parser, args.runtime_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    os.environ["CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING"] = "1"
    os.environ["CLIMATE_WEB_LISTENING_DATA_DIR"] = str(runtime_dir)

    outcomes: dict = {}
    items, warnings = collect_source_items(
        source=source, scope=scope, state_dir=state_dir, seed_outcomes=outcomes,
    )
    observed_at = datetime.now(timezone.utc)
    run_id = observed_at.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    summary = {
        "run_id": run_id,
        "source_key": source.key,
        "observed_at": observed_at.isoformat(),
        "application_revision": os.environ.get("CLIMATE_REPOSITORY_COMMIT_SHA", ""),
        "candidate_count": len(items),
        "candidates": [
            {"url": item.url, "title": item.title, "source_item_id": item.source_item_id,
             "content_hash": item.content_hash}
            for item in items
        ],
        "warnings": warnings,
        "seed_outcomes": [
            {
                "seed_url": seed,
                "status": result.get("status"),
                "phase": result.get("phase"),
                "error": result.get("error"),
                "candidate_count": len(result.get("candidate_urls", [])),
                "executed_tools": sorted({
                    attempt.get("tool_id") for attempt in result.get("attempts", [])
                    if attempt.get("tool_id") and attempt.get("outcome") != "skipped"
                }),
                "attempts": [
                    {
                        "tool": attempt.get("tool_id"),
                        "outcome": attempt.get("outcome"),
                        "requested_url": attempt.get("requested_url"),
                        "final_url": attempt.get("final_url"),
                        "http_status": attempt.get("http_status"),
                        "error_code": (
                            attempt.get("error", {}).get("code")
                            if isinstance(attempt.get("error"), dict) else None
                        ),
                    }
                    for attempt in result.get("attempts", [])
                ],
                "checkpoint_digest": ((result.get("checkpoint") or {}).get("site_skill") or {}).get("digest"),
            }
            for seed, result in outcomes.items()
        ],
    }
    successful_seeds = sum(row["status"] == "success" for row in summary["seed_outcomes"])
    status = (
        "complete" if outcomes and successful_seeds == len(outcomes)
        else "partial" if successful_seeds else "blocked"
    )
    if status == "blocked" and any(row["status"] == "incomplete" for row in summary["seed_outcomes"]):
        status = "incomplete"
    summary["status"] = status
    output = state_dir / f"{source.key}-{run_id}.json"
    content = json.dumps(summary, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    output.write_bytes(content)
    print(json.dumps({
        "status": status,
        "source_key": source.key,
        "seed_count": len(outcomes),
        "successful_seeds": successful_seeds,
        "candidate_count": len(items),
        "summary_path": str(output),
        "summary_sha256": hashlib.sha256(content).hexdigest(),
    }, sort_keys=True))
    return 0 if status == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
