#!/usr/bin/env python3
"""Ingest weekly Climate & Actuarial Monitor reports into the wiki repo.

Source of truth: /home/ubuntu/web_listening/data/reports/climate-monitor-YYYY-MM-DD.md
produced by the weekly Hermes cron job (job f5259a8ec2d9, Mondays 08:00 UTC).

This script copies any report not yet present under sources/, then regenerates
the wiki pages with the weekly cadence and (optionally) commits + pushes.

Usage:
    python scripts/ingest_weekly_reports.py                 # ingest all new + sync
    python scripts/ingest_weekly_reports.py --date 2026-08-10
    python scripts/ingest_weekly_reports.py --commit --push
    python scripts/ingest_weekly_reports.py --dry-run
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = Path("/home/ubuntu/web_listening/data/reports")
REPORT_RE = re.compile(r"^climate-monitor-(\d{4}-\d{2}-\d{2})\.md$")

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from sync_source_wiki import sync_source_wiki  # noqa: E402


def discover(
    report_dir: Path, only_date: str | None, allow_offcycle: bool = False
) -> list[Path]:
    """Return report files to ingest.
    The weekly cron fires on Mondays, but manual re-runs and debugging passes
    leave extra same-week files (Sun/Tue/Fri) in the report dir. Those are
    duplicates of the same monitoring week, so by default only Monday-dated
    reports are treated as canonical. Pass allow_offcycle to take everything,
    or --date to pin one specific file.
    """
    if not report_dir.exists():
        raise SystemExit(f"report dir not found: {report_dir}")
    found = []
    for path in sorted(report_dir.glob("climate-monitor-*.md")):
        match = REPORT_RE.fullmatch(path.name)
        if not match:
            continue
        if only_date and match.group(1) != only_date:
            continue
        if not only_date and not allow_offcycle:
            if date.fromisoformat(match.group(1)).weekday() != 0:
                continue
        found.append(path)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--date", help="Ingest only this report date (YYYY-MM-DD).")
    parser.add_argument(
        "--allow-offcycle",
        action="store_true",
        help="Also ingest non-Monday reports (manual re-runs). Off by default.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing sources/ files.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()

    sources_dir = REPO_ROOT / "sources"
    sources_dir.mkdir(exist_ok=True)

    copied, skipped = [], []
    for src in discover(args.report_dir, args.date, args.allow_offcycle):
        dest = sources_dir / src.name
        if dest.exists() and not args.force:
            skipped.append(dest.name)
            continue
        if args.dry_run:
            copied.append(dest.name)
            continue
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        copied.append(dest.name)

    print(f"ingest: copied={len(copied)} skipped_existing={len(skipped)}")
    for name in copied:
        print(f"  + {name}")

    if args.dry_run:
        print("dry-run: skipping wiki sync")
        return 0

    result = sync_source_wiki(cadence="weekly")
    print(
        "sync: "
        f"latest={result.latest_date} pages={result.daily_pages} "
        f"sources={result.source_days} created={len(result.created_pages)} "
        f"updated={len(result.updated_pages)} missing={len(result.missing_days)}"
    )
    for warning in result.warnings:
        print(f"  warn: {warning}")

    if args.commit:
        subprocess.run(["git", "add", "sources", "wiki"], cwd=REPO_ROOT, check=True)
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT
        ).returncode
        if status == 0:
            print("commit: nothing to commit")
            return 0
        msg = f"docs: weekly climate monitor update ({result.latest_date})"
        subprocess.run(["git", "commit", "-m", msg], cwd=REPO_ROOT, check=True)
        print(f"commit: {msg}")
        if args.push:
            subprocess.run(["git", "push"], cwd=REPO_ROOT, check=True)
            print("push: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
