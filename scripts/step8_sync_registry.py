#!/usr/bin/env python3
"""Step 8: Sync Registry using the existing weekly_sync pipeline."""
import argparse
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

REPORTS = Path("/home/ubuntu/climate_monitor_wiki/data/reports")
DB = Path("/home/ubuntu/climate_monitor_data/registry/article-registry.sqlite3")
ARTIFACTS = Path("/home/ubuntu/climate_delivery_artifacts")


def last_monday():
    today = date.today()
    if today.weekday() == 0:
        return today
    return today - timedelta(days=today.weekday())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=last_monday().isoformat())
    args = parser.parse_args()

    md_path = REPORTS / f"climate-monitor-{args.date}.md"
    if not md_path.exists():
        print(f"ERROR: markdown not found: {md_path}")
        return

    lock_file = DB.with_name(f"{DB.name}.lock")
    cmd = [
        sys.executable, "-m", "climate_registry", "weekly-sync",
        "--date", args.date,
        "--source-dir", str(REPORTS),
        "--database", str(DB),
        "--artifact-root", str(ARTIFACTS),
        "--backup-dir", str(DB.parent / "backups"),
        "--lock-file", str(lock_file),
        "--publisher-ledger-dir", str(DB.parent / "publisher-ledger"),
    ]

    print(f"Running weekly_sync for {args.date}...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    print(result.stdout)
    if result.returncode != 0:
        print(f"ERROR: weekly_sync failed (exit {result.returncode})")
        print(f"stderr: {result.stderr[:500]}")
        return
    print(f"OK: weekly_sync completed for {args.date}")


if __name__ == "__main__":
    main()
