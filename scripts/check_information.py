#!/usr/bin/env python3
"""Check meeting/article observations independently; usable by a future scheduler."""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_registry.information_checks import run_checks
from climate_delivery.io import exclusive_lock, transaction_lock, atomic_write_json


def run_daily_checks(argv: list[str], *, result_dir: Path) -> int:
    """Admit one daily occurrence, then finish both independent check kinds."""
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
            print(json.dumps({"kind": kind, "status": "failed", "error": type(exc).__name__}), flush=True)
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
    parser.add_argument("--limit", type=int, help="Process at most this many entries, then resume later")
    parser.add_argument("--data-root", type=Path, default=os.getenv("WEB_LISTENING_DATA_ROOT"))
    parser.add_argument("--result", type=Path, help="Save machine-readable run summary")
    parser.add_argument("--scheduled", action="store_true", help="Guard the 05:00 New York occurrence and resume its saved work")
    parser.add_argument("--occurrence", help=argparse.SUPPRESS)
    parser.add_argument("--refresh-chat", action="store_true", help="Publish the updated active PDF snapshot and reload Chat")
    parser.add_argument("--queue-dir", type=Path, default=os.getenv("CLIMATE_PDF_INTAKE_QUEUE_DIR"))
    parser.add_argument("--runtime-wiki-dir", type=Path, default=os.getenv("CLIMATE_PDF_RUNTIME_WIKI_DIR"))
    args = parser.parse_args(argv)
    if args.resume and args.occurrence_id:
        parser.error("--resume uses its frozen observations; omit --occurrence-id")
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
            if previous.get("occurrence") == occurrence:
                if previous.get("status") == "complete" and (not args.refresh_chat or previous.get("projection", {}).get("status") == "chat_ready"):
                    print(json.dumps({"status": "already_complete", "kind": args.kind, "occurrence": occurrence}))
                    return 0
                continuation = ["--resume", previous["run_id"]] if previous.get("status") in {"pending", "complete"} else ["--retry", previous["run_id"]]
            else:
                continuation = []
            forwarded = [value for value in (argv if argv is not None else sys.argv[1:]) if value != "--scheduled"] + continuation + ["--occurrence", occurrence]
            code = main(forwarded)
            result = json.loads(args.result.read_text())
            result.update(occurrence=occurrence, observed_at=current.isoformat())
            atomic_write_json(args.result, result)
            return code
    def progress(item):
        print(json.dumps(item, ensure_ascii=False), flush=True)
        if args.result and item.get("status") == "pending":
            atomic_write_json(args.result, {**item, "kind": args.kind, "occurrence": args.occurrence})
    with exclusive_lock(args.queue_dir, "intake-writer") if args.queue_dir else nullcontext():
        result = run_checks(args.database.resolve(), kind=args.kind, backup_dir=args.backup_dir.resolve(),
            occurrence_ids=set(args.occurrence_id) if args.occurrence_id else None,
            resume_run_id=args.resume, retry_run_id=args.retry, limit=args.limit, data_root=args.data_root,
            progress=progress)
    if args.occurrence:
        result["occurrence"] = args.occurrence
    if args.result:
        atomic_write_json(args.result, result)
    if args.refresh_chat:
        from climate_registry.pdf_pipeline import PdfIntakePipeline
        from scripts.run_pdf_intake_writer import _reload_chat
        pipeline = PdfIntakePipeline(args.queue_dir, args.database, args.backup_dir,
            args.runtime_wiki_dir, _reload_chat)
        try:
            result["projection"] = pipeline.refresh_checks(result["run_id"])
        except Exception as exc:
            result["projection"] = {"status": "failed", "error": type(exc).__name__}
            if args.result:
                atomic_write_json(args.result, result)
            raise
    if args.result:
        atomic_write_json(args.result, result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    # Partial/failed evidence is actionable to a scheduler; a limited run is resumable.
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
