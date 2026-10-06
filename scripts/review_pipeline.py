#!/usr/bin/env python3
"""Business queue commands for native Hermes agent cron and script-only sending."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from climate_registry import acquisition_review as acquisition
from climate_delivery import report_review as reports


def pending(kind: str, root: Path):
    pattern = "*/acquisition-review/state.json" if kind == "acquisition" else "reports/*/state.json"
    for path in sorted(root.glob(pattern)):
        state = json.loads(path.read_text())
        recovery_pending = kind == "acquisition" and state["status"] == "reviewed_partial" and any(
            value.get("status") == "recovering" for value in state.get("sources", {}).values())
        if state["status"] not in {"pending_review", "reviewing"} and not recovery_pending:
            continue
        if kind == "acquisition":
            result_path = Path(state["packet"]["attempt_result_path"])
            if not result_path.is_file():
                continue
        claim_path = path.parent / "claim.json" if kind == "acquisition" else path.parent / "revisions" / f"{state['revision']:04d}" / "claim.json"
        if claim_path.exists():
            claim = json.loads(claim_path.read_text())
            if not claim.get("released_at") and not acquisition.owner_finished(claim):
                continue  # Native owner completion, not elapsed time, permits reconciliation.
        return path.parent, state
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("peek", "claim", "submit", "correct", "recover", "activate", "regenerate", "send"))
    parser.add_argument("--kind", choices=("acquisition", "report"), default="report")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--target", help="run id for acquisition, occurrence date for reports")
    # Native local terminal injects the current ContextVar identity per command.
    # Never discover a session by selecting the latest row in state.db.
    session = os.environ.get("HERMES_SESSION_ID", "")
    parser.add_argument("--session-id", default=session or None)
    parser.add_argument("--execution-id", default=session or None)
    parser.add_argument("--reviewer", default="hermes-native:" + session.split("_")[1] if session.startswith("cron_") else None)
    parser.add_argument("--hermes-database", type=Path, default=Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))) / "state.db")
    parser.add_argument("--timeout-seconds", type=int, default=4500)
    parser.add_argument("--token")
    parser.add_argument("--result", type=Path)
    parser.add_argument("--reason")
    parser.add_argument("--queue-dir", type=Path, default=os.getenv("CLIMATE_INTAKE_QUEUE_DIR") or os.getenv("CLIMATE_PDF_INTAKE_QUEUE_DIR"))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--send", action="store_true", help="Enable actual delivery after the separate production cutover")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_absolute() or root.is_relative_to(ROOT):
        parser.error("business state must be external to the checkout")
    if args.command == "peek":
        selected = pending(args.kind, root)
        resolved = {}
        if selected:
            target_root, state = selected
            packet = state["packet"]
            if args.kind == "report":
                revision_root = target_root / "revisions" / f"{state['revision']:04d}"
                resolved = {"pdf": str((revision_root / packet["pdf_path"]).resolve()),
                    "source": str((revision_root / "report-source.json").resolve()),
                    "snapshot": str((target_root / "snapshot.json").resolve()),
                    "previous_source": str((target_root / "revisions" / f"{state['revision'] - 1:04d}" / "report-source.json").resolve()) if state["revision"] > 1 else None,
                    "text": (packet.get("readable_text") or {}).get("path") or str((revision_root / packet["text_path"]).resolve()),
                    "pages": [str((revision_root / page["path"]).resolve()) for page in packet["pages"]]}
            else:
                resolved = {"candidate_bodies": [(c.get("readable_text") or {}).get("path") or str((target_root / c["body_path"]).resolve()) for c in packet["candidates"]]}
        print(json.dumps({"wakeAgent": bool(selected), "kind": args.kind,
            **({"target": selected[1].get("occurrence") or selected[1]["packet"]["run_id"],
                "packet": selected[1]["packet"], "resolved_paths": resolved} if selected else {})}, sort_keys=True))
        return 0
    if args.command == "send" and not args.target:
        from datetime import datetime, timedelta, timezone
        results = []
        for state in reports.report_states(root):
            if state["status"] != "approved" or datetime.now(timezone.utc) < acquisition.timestamp(state["approval"]["approved_at"]) + timedelta(minutes=60):
                continue
            try:
                results.append(reports.send_approved(root, state["occurrence"], no_send=not args.send, config_path=args.config))
            except Exception as exc:
                results.append({"occurrence": state["occurrence"], "status": "blocked", "error": type(exc).__name__})
        print(json.dumps(results, sort_keys=True))
        return int(any(value.get("status") == "blocked" for value in results))
    if not args.target:
        parser.error("a precise --target is required")
    acquisition_root = root / args.target / "acquisition-review"
    if args.command == "regenerate":
        if args.kind != "report" or not args.reason:
            parser.error("report regeneration requires an explicit repair reason")
        result = reports.regenerate_report(root, args.target, reason=args.reason)
    elif args.command == "claim":
        if not all((args.session_id, args.execution_id, args.reviewer, args.hermes_database)):
            parser.error("native session/execution/reviewer/database identity is required")
        context = dict(session_id=args.session_id, execution_id=args.execution_id, reviewer=args.reviewer,
            hermes_database=args.hermes_database, timeout_seconds=args.timeout_seconds)
        result = (acquisition.claim_acquisition(acquisition_root, **context) if args.kind == "acquisition"
            else reports.claim_report(root, args.target, **context))
    elif args.command == "submit":
        if not args.result or not args.token:
            parser.error("--result and --token are required")
        value = json.loads(args.result.read_text())
        result = (acquisition.review_acquisition(acquisition_root, args.token, value)
            if args.kind == "acquisition" else reports.submit_report_review(root, args.target, args.token, **value))
    elif args.command == "correct":
        if args.kind != "acquisition" or not args.result:
            parser.error("candidate corrections require acquisition --result")
        value = json.loads(args.result.read_text())
        result = acquisition.correct_candidate(acquisition_root, value["item_id"], value["changes"], reason=value["reason"])
    elif args.command == "recover":
        if args.kind != "acquisition" or not args.token:
            parser.error("recovery requires an acquisition review token")
        from climate_monitor.management import ManagementService
        result = acquisition.recover_acquisition(acquisition_root, args.token, ManagementService.from_environment())
    elif args.command == "activate":
        if args.kind != "acquisition" or not args.queue_dir:
            parser.error("activation requires the acquisition writer queue")
        database = args.database or Path(json.loads((acquisition_root / "state.json").read_text())["packet"]["registry_database"])
        result = acquisition.activate_approved(acquisition_root, queue_dir=args.queue_dir,
            database=database, repository_root=ROOT)
    else:
        result = reports.send_approved(root, args.target, no_send=not args.send, config_path=args.config)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
