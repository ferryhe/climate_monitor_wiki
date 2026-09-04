#!/usr/bin/env python3
"""Step 9: Publish a generated report through the isolated rolling PR.

The controlled server and Render both consume reviewed Git history. This step
therefore delegates to ``weekly_wiki_refresh.sh`` instead of changing the
production checkout or reloading a running application directly. Merge and
deployment remain separate, human-controlled operations.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))
PYTHON = Path(os.environ.get("CLIMATE_WIKI_PYTHON", str(HOME / ".venv" / "bin" / "python")))
if not PYTHON.exists():
    PYTHON = Path(sys.executable)
PUBLISHER = HOME / "scripts" / "weekly_wiki_refresh.sh"


def last_monday() -> date:
    """Get the most recent Monday."""
    today = date.today()
    if today.weekday() == 0:
        return today
    return today - timedelta(days=today.weekday())


def run(
    cmd: list[str],
    *,
    timeout: int = 900,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(HOME),
        env=env,
    )
    if result.returncode != 0:
        print(f"ERROR: {' '.join(cmd)} failed (exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish the weekly report through the rolling PR")
    parser.add_argument("--date", default=last_monday().isoformat())
    parser.add_argument("--allow-offcycle", action="store_true", help="Allow non-Monday report dates")
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
    if parsed_date > date.today():
        print(f"ERROR: report date {args.date} is in the future")
        return 1
    if parsed_date.weekday() != 0 and not args.allow_offcycle:
        print(f"ERROR: report date {args.date} is not a Monday; pass --allow-offcycle to override")
        return 1

    # The report stays in the external candidate directory. The publisher
    # validates it in a temporary clone and changes only the rolling PR branch.
    md_path = REPORTS / f"climate-monitor-{args.date}.md"
    if not md_path.exists():
        print(f"ERROR: markdown not found: {md_path}", file=sys.stderr)
        return 1
    if not PUBLISHER.is_file():
        print(f"ERROR: publisher wrapper not found: {PUBLISHER}", file=sys.stderr)
        return 1

    publisher_env = dict(os.environ)
    publisher_env.pop("RELOAD_TOKEN", None)
    publisher_env.update(
        {
            "REPO": str(HOME),
            "PYTHON": str(PYTHON),
            "REPORT_DIR": str(REPORTS),
        }
    )
    if args.allow_offcycle:
        publisher_env["CLIMATE_PUBLISH_ALLOW_OFFCYCLE"] = "1"
    else:
        publisher_env.pop("CLIMATE_PUBLISH_ALLOW_OFFCYCLE", None)

    result = run(["bash", str(PUBLISHER)], env=publisher_env)
    if result.returncode != 0:
        return 1
    if result.stdout.strip():
        print(result.stdout.strip())
    print("OK: rolling PR publisher completed; merge and deployment are still required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
