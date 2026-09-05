#!/usr/bin/env python3
"""Historical Step 2 name: commit a prepared URL delta after final report output."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_monitor.candidate_aggregation import (
    combined_candidates_path,
    serialize_combined_candidates,
    validate_combined_candidates,
)
from climate_monitor.dedupe import canonical_url
from climate_monitor.seen_state import (
    SeenStateError,
    commit_seen_url_delta,
    load_pending_seen_url_additions,
    load_pending_seen_url_transaction,
    pending_seen_url_delta_path,
)


HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))
STATE_FILE = Path(
    os.environ.get("CLIMATE_WL_STATE", "/home/ubuntu/web_listening/data/article_state.json")
)


def _report_urls(payload: object, *, report_text: str) -> set[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("categories"), dict):
        raise ValueError("final report evidence must contain categories")
    urls: set[str] = set()
    for items in payload["categories"].values():
        if not isinstance(items, list):
            raise ValueError("final report evidence categories must contain arrays")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                raise ValueError("final report evidence item is missing its URL")
            raw_url = item["url"]
            if raw_url not in report_text:
                raise ValueError("final Markdown does not contain every evidence URL")
            urls.add(canonical_url(raw_url))
    return urls


def validate_final_report_bundle(
    *,
    report: Path,
    evidence: Path,
    combined: Path,
    report_date: str,
) -> set[str]:
    """Validate the exact legacy Markdown/evidence/combined report bundle."""

    if not report.is_file():
        raise ValueError(f"final Markdown is missing: {report}")
    return validate_final_report_bundle_bytes(
        report_bytes=report.read_bytes(),
        evidence_bytes=evidence.read_bytes(),
        combined_bytes=combined.read_bytes(),
        report_date=report_date,
    )


def validate_final_report_bundle_bytes(
    *,
    report_bytes: bytes,
    evidence_bytes: bytes,
    combined_bytes: bytes,
    report_date: str,
) -> set[str]:
    """Validate one in-memory legacy Markdown/evidence/combined bundle."""

    report_text = report_bytes.decode("utf-8")
    if f"**Report Date:** {report_date}" not in report_text:
        raise ValueError("final Markdown date does not match --date")
    evidence_payload = json.loads(evidence_bytes.decode("utf-8"))
    if not isinstance(evidence_payload, dict):
        raise ValueError("final report evidence must be an object")
    if evidence_payload.get("report_date") != report_date:
        raise ValueError("final report evidence date does not match --date")
    combined_payload = validate_combined_candidates(
        json.loads(combined_bytes.decode("utf-8"))
    )
    if serialize_combined_candidates(combined_payload) != combined_bytes:
        raise ValueError("combined candidate evidence is not canonical")
    if combined_payload["report_date"] != report_date:
        raise ValueError("combined candidate evidence date does not match --date")
    candidate_urls = {item["canonical_url"] for item in combined_payload["items"]}
    report_urls = _report_urls(evidence_payload, report_text=report_text)
    total_input = evidence_payload.get("total_input")
    relevant = evidence_payload.get("relevant")
    non_relevant = evidence_payload.get("non_relevant")
    if (
        any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (
            total_input,
            relevant,
            non_relevant,
        ))
        or total_input != len(candidate_urls)
        or relevant != len(report_urls)
        or non_relevant != total_input - relevant
    ):
        raise ValueError("final report evidence counts do not match combined candidates")
    if not report_urls.issubset(candidate_urls):
        raise ValueError("final report evidence contains a URL outside combined candidates")
    return candidate_urls


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Commit the pending canonical-URL state delta only after the final Markdown, "
            "report evidence, and combined-candidates.v1 artifact exist. The historical "
            "step filename is retained for compatibility."
        )
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--state-file", type=Path, default=STATE_FILE)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--report-evidence", type=Path)
    parser.add_argument("--combined", type=Path)
    parser.add_argument(
        "--commit-pending",
        action="store_true",
        help="Apply the delta prepared by step3_aggregate after verifying final outputs.",
    )
    parser.add_argument(
        "--no-update-seen-state",
        action="store_true",
        help="Leave canonical URL state and any pending delta unchanged.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify readiness without applying the pending delta.",
    )
    args = parser.parse_args()

    try:
        parsed_date = date.fromisoformat(args.date)
    except ValueError:
        print(f"ERROR: invalid --date {args.date!r} (expected YYYY-MM-DD)")
        return 1
    if parsed_date.isoformat() != args.date:
        print(f"ERROR: invalid --date {args.date!r} (expected YYYY-MM-DD)")
        return 1

    pending = pending_seen_url_delta_path(args.state_file)
    if args.no_update_seen_state:
        print("SKIP: --no-update-seen-state leaves URL history unchanged")
        return 0
    if not args.commit_pending:
        print(
            "DEFERRED: URL history is no longer written before aggregation/report success; "
            "run again with --commit-pending after Step 5 completes"
        )
        return 0
    if not pending.exists():
        print(f"ERROR: pending seen-URL delta not found: {pending}")
        return 1

    try:
        pending_date, pending_combined_sha256, _ = load_pending_seen_url_transaction(
            args.state_file
        )
    except (OSError, SeenStateError, UnicodeError, ValueError) as exc:
        print(f"ERROR: pending seen-URL delta is invalid: {exc}")
        return 1
    if pending_date != args.date:
        print(
            f"ERROR: pending seen-URL delta belongs to {pending_date}; "
            "commit that date before another report date"
        )
        return 1

    report = args.report or (REPORTS / f"climate-monitor-{args.date}.md")
    evidence = args.report_evidence or report.with_suffix(".json")
    combined = args.combined or combined_candidates_path(REPORTS, args.date)
    try:
        candidate_urls = validate_final_report_bundle(
            report=report,
            evidence=evidence,
            combined=combined,
            report_date=args.date,
        )
        if hashlib.sha256(combined.read_bytes()).hexdigest() != pending_combined_sha256:
            raise ValueError("pending seen-URL delta identifies different combined candidates")
        if load_pending_seen_url_additions(args.state_file) != candidate_urls:
            raise ValueError("pending seen-URL delta does not match combined candidates")
    except (OSError, SeenStateError, UnicodeError, ValueError) as exc:
        print(f"ERROR: final report bundle is incomplete or invalid: {exc}")
        return 1

    if args.dry_run:
        print("DRY RUN: final report bundle is valid; URL history was not changed")
        return 0
    try:
        changed = commit_seen_url_delta(args.state_file)
    except (OSError, SeenStateError) as exc:
        print(f"ERROR: could not commit pending seen-URL delta: {exc}")
        return 1
    print("OK: pending seen-URL delta committed" if changed else "OK: seen-URL delta already applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
