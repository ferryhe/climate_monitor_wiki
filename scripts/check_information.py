#!/usr/bin/env python3
"""Check meeting/article observations independently; usable by a future scheduler."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_registry.information_checks import run_checks
from climate_delivery.io import transaction_lock, atomic_write_json


def run_daily_checks(argv: list[str], *, result_dir: Path) -> int:
    """Admit one five-minute tick; give articles and meetings their own bounded batch."""
    from climate_monitor.schedule import pipeline_due, pipeline_occurrence, stamp
    current = datetime.now(timezone.utc)
    if not pipeline_due("T1", current):
        print(json.dumps({"status": "skipped", "kind": "daily"}))
        return 0
    occurrence = stamp(pipeline_occurrence("T1", current))
    result = 0
    for kind in ("articles", "meetings"):
        try:
            code = main([*argv, "--kind", kind, "--scheduled", "--result",
                str(result_dir / (kind + "-daily-check.json"))], _admitted_occurrence=occurrence)
        except Exception as exc:
            path = result_dir / (kind + "-daily-check.json")
            previous = json.loads(path.read_text()) if path.is_file() else {}
            failure = {**previous, "kind": kind, "status": "failed", "business_status": "failed", "occurrence": occurrence,
                "error": type(exc).__name__, "observed_at": datetime.now(timezone.utc).isoformat()}
            failure.pop("last_actual_outcome", None)
            failure["failure_history"] = [*previous.get("failure_history", []),
                {key: failure[key] for key in ("run_id", "completed_count", "error", "observed_at") if key in failure}]
            atomic_write_json(path, {**failure, "last_actual_outcome": failure})
            print(json.dumps(failure), flush=True)
            code = 2
        result = max(result, code)
    return result


def main(argv: list[str] | None = None, *, _admitted_occurrence: str | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("meetings", "articles"), required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--occurrence-id", action="append", help="Check these observations; repeat for multiple records")
    continuation = parser.add_mutually_exclusive_group()
    continuation.add_argument("--resume", help="Continue unchecked entries in a frozen run")
    continuation.add_argument("--retry", help="Create a new run for the unverified entries of this run")
    parser.add_argument("--reverify-all", action="store_true", help="Recheck all selected targets, including verified ones")
    parser.add_argument("--limit", type=int, help="Manual attempt limit; scheduled batch size (default 10 per kind)")
    parser.add_argument("--retry-after-minutes", type=int, default=60, help="Backoff for unchanged failed scheduled targets")
    parser.add_argument("--incremental", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-root", type=Path, default=os.getenv("WEB_LISTENING_DATA_ROOT"))
    parser.add_argument("--result", type=Path, help="Save machine-readable run summary")
    parser.add_argument("--scheduled", action="store_true", help="Run a bounded incremental batch every five minutes")
    parser.add_argument("--occurrence", help=argparse.SUPPRESS)
    parser.add_argument("--refresh-chat", action="store_true", help="Queue improved candidates for the existing T10 review")
    parser.add_argument("--queue-dir", type=Path, default=os.getenv("CLIMATE_PDF_INTAKE_QUEUE_DIR"))
    parser.add_argument("--runtime-wiki-dir", type=Path, default=os.getenv("CLIMATE_PDF_RUNTIME_WIKI_DIR"))
    args = parser.parse_args(argv)
    if args.resume and args.occurrence_id:
        parser.error("--resume uses its frozen observations; omit --occurrence-id")
    if args.reverify_all and (args.resume or args.retry):
        parser.error("--reverify-all applies only to a fresh run; omit --resume/--retry")
    if args.reverify_all and args.scheduled:
        parser.error("--reverify-all is for manual checks; omit --scheduled")
    if args.refresh_chat and (not args.queue_dir or not args.runtime_wiki_dir):
        parser.error("--refresh-chat needs --queue-dir and --runtime-wiki-dir")
    if args.scheduled:
        from climate_monitor.schedule import pipeline_due, pipeline_occurrence, stamp
        current = datetime.now(timezone.utc)
        if _admitted_occurrence is None and not pipeline_due("T1", current):
            print(json.dumps({"status": "skipped", "kind": args.kind}))
            return 0
        if not args.result:
            parser.error("scheduled information checks require --result")
        occurrence = _admitted_occurrence or stamp(pipeline_occurrence("T1", current))
        with transaction_lock(args.result.parent, "daily-check-" + args.kind):
            previous = json.loads(args.result.read_text()) if args.result.exists() else {}
            if previous.get("occurrence") == occurrence and previous.get("status") in {"complete", "partial"}:
                if not args.refresh_chat or previous.get("projection", {}).get("status") in {"chat_ready", "pending_review"}:
                    print(json.dumps({"status": "already_complete", "kind": args.kind, "occurrence": occurrence}))
                    return 0 if previous["status"] == "complete" else 2
            continuation = ["--resume", previous["run_id"]] if previous.get("run_id") and previous.get("status") in {"pending", "running", "failed"} else []
            forwarded = [value for value in (argv if argv is not None else sys.argv[1:]) if value != "--scheduled"] + continuation + ["--incremental", "--occurrence", occurrence]
            code = main(forwarded)
            result = json.loads(args.result.read_text())
            result.update(occurrence=occurrence, observed_at=current.isoformat())
            atomic_write_json(args.result, result)
            return code
    previous = json.loads(args.result.read_text()) if args.result and args.result.exists() else {}
    last_actual = previous.get("last_actual_outcome") or (previous if previous.get("completed_count", 0) else None)
    checkpoint = {}
    def progress(item):
        print(json.dumps(item, ensure_ascii=False), flush=True)
        if args.result and item.get("status") == "pending":
            checkpoint.update(item, kind=args.kind, occurrence=args.occurrence, last_actual_outcome=last_actual,
                failure_history=previous.get("failure_history", []))
            atomic_write_json(args.result, checkpoint)
        elif args.result and checkpoint and item.get("occurrence_id"):
            checkpoint["completed_count"] += 1
            atomic_write_json(args.result, checkpoint)
    # Fetch and TypeSafe run outside the intake-writer lock; DB writes keep their short lock.
    result = run_checks(args.database.resolve(), kind=args.kind, backup_dir=args.backup_dir.resolve(),
        occurrence_ids=set(args.occurrence_id) if args.occurrence_id else None,
        resume_run_id=args.resume, retry_run_id=args.retry, limit=args.limit, reverify_all=args.reverify_all,
        incremental=args.incremental, retry_after_minutes=args.retry_after_minutes,
        data_root=args.data_root, progress=progress)
    if args.occurrence:
        result["occurrence"] = args.occurrence
    if previous.get("failure_history"):
        result["failure_history"] = previous["failure_history"]
    result["business_status"] = ("skipped" if not result.get("item_count") else
        "completed" if result["status"] == "complete" else "failed" if result["status"] == "partial" else "running")
    if result.get("completed_count"):
        last_actual = {**result, "observed_at": datetime.now(timezone.utc).isoformat()}
    result["last_actual_outcome"] = last_actual
    if args.result:
        atomic_write_json(args.result, result)
    if args.refresh_chat:
        from climate_registry.publication import prepare_review, resolve_database
        review_root = Path(os.environ["CLIMATE_ACQUISITION_RUN_DIR"]) / "registry-review"
        packet = prepare_review(resolve_database(args.database), review_root)
        result["projection"] = {"status": "pending_review", "root": str(review_root), "candidate_count": len(packet["candidates"])}
    if args.result:
        atomic_write_json(args.result, result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    # Partial/failed evidence is actionable to a scheduler; a limited run is resumable.
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
