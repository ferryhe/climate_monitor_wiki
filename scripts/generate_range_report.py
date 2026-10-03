#!/usr/bin/env python3
"""Create one frozen date-range report from Public and active Runtime Registry data."""

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
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
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
    if (args.runtime_dir is None) != (args.queue_dir is None):
        parser.error("--runtime-dir and --queue-dir must be configured together")

    reader = RegistryReader(args.database, repository_root=ROOT)
    overlay_reader, pdf_overlay_reader, manifest = load_active_range_overlay(
        args.runtime_dir,
        args.queue_dir,
        repository_root=ROOT,
    )
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
