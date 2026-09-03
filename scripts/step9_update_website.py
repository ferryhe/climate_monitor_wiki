#!/usr/bin/env python3
"""Step 9: Update website (wiki pages + verified API reload).

The reload token is read from the RELOAD_TOKEN environment variable (see
.env.example) and is never stored in this file. The post-reload smoke test
verifies that the new report date is actually served by /api/config and that
the chat endpoint still answers with evidence sources.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))
SOURCES = Path(os.environ.get("CLIMATE_WIKI_SOURCES", str(HOME / "sources")))
PYTHON = Path(os.environ.get("CLIMATE_WIKI_PYTHON", str(HOME / ".venv" / "bin" / "python")))


def last_monday() -> date:
    """Get the most recent Monday."""
    today = date.today()
    if today.weekday() == 0:
        return today
    return today - timedelta(days=today.weekday())


def run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(HOME))
    if result.returncode != 0:
        print(f"ERROR: {' '.join(cmd)} failed (exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Update website (wiki pages + verified reload)")
    parser.add_argument("--date", default=last_monday().isoformat())
    parser.add_argument("--allow-offcycle", action="store_true", help="Allow non-Monday report dates")
    parser.add_argument("--allow-future", action="store_true", help="Allow future report dates")
    args = parser.parse_args()

    try:
        parsed_date = date.fromisoformat(args.date)
    except ValueError:
        print(f"ERROR: invalid --date {args.date!r} (expected YYYY-MM-DD)")
        return 1
    if parsed_date.isoformat() != args.date:
        print(f"ERROR: invalid --date {args.date!r} (expected YYYY-MM-DD)")
        return 1
    args.date = parsed_date.isoformat()
    if parsed_date > date.today() and not args.allow_future:
        print(f"ERROR: report date {args.date} is in the future; pass --allow-future to override")
        return 1
    if parsed_date.weekday() != 0 and not args.allow_offcycle:
        print(f"ERROR: report date {args.date} is not a Monday; pass --allow-offcycle to override")
        return 1

    if not os.environ.get("RELOAD_TOKEN"):
        print("ERROR: RELOAD_TOKEN environment variable is not set", file=sys.stderr)
        return 1

    # Step 9a: Promote the generated MD into sources/ (append-mostly: never
    # overwrite an existing source with different content).
    md_path = REPORTS / f"climate-monitor-{args.date}.md"
    if not md_path.exists():
        print(f"ERROR: markdown not found: {md_path}", file=sys.stderr)
        return 1
    SOURCES.mkdir(parents=True, exist_ok=True)
    dest = SOURCES / md_path.name
    if dest.exists():
        if dest.read_bytes() != md_path.read_bytes():
            print(
                f"ERROR: {dest.name} already exists with different content; "
                "sources/ is append-mostly — resolve manually before rerunning",
                file=sys.stderr,
            )
            return 1
        print(f"{dest.name} already in sources/ (identical); nothing to copy")
    else:
        tmp_dest = dest.with_suffix(f".tmp-{os.getpid()}")
        shutil.copy2(md_path, tmp_dest)
        os.replace(tmp_dest, dest)
        print(f"Copied {md_path.name} to sources/")

    # Step 9b: Regenerate wiki pages. Fail closed on any error.
    result = run([str(PYTHON), "scripts/sync_source_wiki.py", "--cadence", "weekly"])
    if result.returncode != 0:
        return 1
    print("OK: wiki pages regenerated")

    # Step 9c: Reload the API corpus and verify the new date is served.
    result = run([str(PYTHON), "scripts/reload_and_smoke_test.py", "--date", args.date])
    if result.returncode != 0:
        return 1
    print("OK: API reloaded and smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
