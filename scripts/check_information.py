#!/usr/bin/env python3
"""Check meeting/article observations independently; usable by a future scheduler."""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_registry.information_checks import run_checks
from climate_delivery.io import exclusive_lock


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument("--refresh-chat", action="store_true", help="Publish the updated active PDF snapshot and reload Chat")
    parser.add_argument("--queue-dir", type=Path, default=os.getenv("CLIMATE_PDF_INTAKE_QUEUE_DIR"))
    parser.add_argument("--runtime-wiki-dir", type=Path, default=os.getenv("CLIMATE_PDF_RUNTIME_WIKI_DIR"))
    args = parser.parse_args(argv)
    if args.resume and args.occurrence_id:
        parser.error("--resume uses its frozen observations; omit --occurrence-id")
    if args.refresh_chat and (not args.queue_dir or not args.runtime_wiki_dir):
        parser.error("--refresh-chat needs --queue-dir and --runtime-wiki-dir")
    with exclusive_lock(args.queue_dir, "intake-writer") if args.queue_dir else nullcontext():
        result = run_checks(args.database.resolve(), kind=args.kind, backup_dir=args.backup_dir.resolve(),
            occurrence_ids=set(args.occurrence_id) if args.occurrence_id else None,
            resume_run_id=args.resume, retry_run_id=args.retry, limit=args.limit, data_root=args.data_root,
            progress=lambda item: print(json.dumps(item, ensure_ascii=False), flush=True))
    if args.refresh_chat:
        from climate_registry.pdf_pipeline import PdfIntakePipeline
        from scripts.run_pdf_intake_writer import _reload_chat
        pipeline = PdfIntakePipeline(args.queue_dir, args.database, args.backup_dir,
            args.runtime_wiki_dir, _reload_chat)
        result["projection"] = pipeline.refresh_checks(result["run_id"])
    if args.result:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    # Partial/failed evidence is actionable to a scheduler; a limited run is resumable.
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
