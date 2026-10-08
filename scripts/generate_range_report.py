#!/usr/bin/env python3
"""Create a frozen report from the selected Registry public view."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_registry.range_reports import (  # noqa: E402
    RENDERER_VERSION,
    ensure_range_report_pdf,
    freeze_range_report,
    load_active_range_overlay,
)
from climate_registry.read_api import RegistryReader  # noqa: E402


def _configured(name: str, fallback: str = "") -> str:
    return os.getenv(name, "").strip() or (os.getenv(fallback, "").strip() if fallback else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--biweekly-date")
    parser.add_argument("--scheduled-biweekly", action="store_true")
    parser.add_argument("--meeting-snapshot-id")
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path(value) if (value := _configured(
            "CLIMATE_RUNTIME_WIKI_DIR", "CLIMATE_PDF_RUNTIME_WIKI_DIR"
        )) else None,
    )
    parser.add_argument(
        "--queue-dir",
        type=Path,
        default=Path(value) if (value := _configured(
            "CLIMATE_INTAKE_QUEUE_DIR", "CLIMATE_PDF_INTAKE_QUEUE_DIR"
        )) else None,
    )
    args = parser.parse_args(argv)
    reader = RegistryReader(args.database, repository_root=ROOT)
    with reader.connect() as connection:
        unified = connection.execute("PRAGMA user_version").fetchone()[0] >= 21
    if not unified and (args.runtime_dir is None) != (args.queue_dir is None):
        parser.error("--runtime-dir and --queue-dir must be configured together")
    if args.scheduled_biweekly:
        from climate_monitor.schedule import pipeline_due, ET
        from datetime import datetime, timezone
        current = datetime.now(timezone.utc)
        if not pipeline_due("T4", current):
            print(json.dumps({"status": "skipped", "role": "T4"}))
            return 0
        args.biweekly_date = current.astimezone(ET).date().isoformat()
    if args.biweekly_date and (args.start_date or args.end_date):
        parser.error("biweekly selection cannot be combined with a publication-date range")
    if not args.biweekly_date and not (args.start_date and args.end_date):
        parser.error("provide --biweekly-date or both --start-date and --end-date")

    overlay_reader, pdf_overlay_reader, manifest = (
        (None, None, None) if unified else load_active_range_overlay(
            args.runtime_dir,
            args.queue_dir,
            repository_root=ROOT,
        )
    )
    if args.biweekly_date:
        from climate_delivery.report_review import freeze_biweekly
        state = freeze_biweekly(reader, args.artifact_root, occurrence=args.biweekly_date,
            web_reader=overlay_reader, pdf_reader=pdf_overlay_reader, manifest=manifest)
        print(json.dumps(state, sort_keys=True))
        return 0
    snapshot = freeze_range_report(
        reader,
        args.artifact_root,
        start_date=args.start_date,
        end_date=args.end_date,
        meeting_snapshot_id=args.meeting_snapshot_id,
        overlay_reader=overlay_reader,
        pdf_overlay_reader=pdf_overlay_reader,
        overlay_manifest=manifest,
    )
    pdf = ensure_range_report_pdf(snapshot, args.artifact_root)
    print(json.dumps({
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "renderer": RENDERER_VERSION,
        "pdf_path": str(pdf.resolve()),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
