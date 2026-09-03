#!/usr/bin/env python3
"""Step 8: Sync the weekly report and its articles into the Registry.

This script performs no direct database writes. It delegates to the
registry's own append-only pipeline (`climate_registry plan-update` and
`climate_registry update`), which enforces:

* append-only semantics: existing reports are never rewritten, new reports
  are rejected if they are out of order or their identity conflicts;
* report SHA conflict detection (a changed source report blocks the update);
* schema migrations, an exclusive lock file, and a pre-update exact backup.

Semantic enrichment (titles/summaries/categories/keywords) stays on the
separate review-gated `semantic-import` path; this step only installs
reports and article identities.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
SOURCES = Path(os.environ.get("CLIMATE_WIKI_SOURCES", str(HOME / "sources")))
REPORTS = Path(os.environ.get("CLIMATE_REPORTS_DIR", str(HOME / "data" / "reports")))
PYTHON = Path(os.environ.get("CLIMATE_WIKI_PYTHON", str(HOME / ".venv" / "bin" / "python")))
if not PYTHON.exists():
    # Fall back to the interpreter running this script (e.g. Render, where the
    # /home/ubuntu venv path does not exist).
    PYTHON = Path(sys.executable)
DB = Path(
    os.environ.get(
        "CLIMATE_REGISTRY_DB",
        "/home/ubuntu/climate_monitor_data/registry/article-registry.sqlite3",
    )
)
BACKUP_DIR = Path(
    os.environ.get(
        "CLIMATE_REGISTRY_BACKUP_DIR",
        "/home/ubuntu/climate_monitor_data/registry/backups",
    )
)


def run_cli(args: list[str]) -> dict:
    try:
        result = subprocess.run(
            [str(PYTHON), "-m", "climate_registry", *args],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(HOME),
            env={**os.environ, "PYTHONPATH": str(HOME)},
        )
    except OSError as exc:
        raise RuntimeError(f"could not launch climate_registry CLI ({PYTHON}): {exc}") from exc
    payload = {}
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        pass
    if result.returncode != 0:
        message = (payload or {}).get("message") or result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"climate_registry {' '.join(args)} failed (exit {result.returncode}): {message}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync weekly report + articles into the Registry")
    parser.add_argument("--date", required=True)
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

    # The registry ingests from SOURCES (the append-mostly source of truth).
    # If this week's report has not been copied there yet, promote it from the
    # generated reports directory so step 8 does not depend on step 9 running
    # first. If both copies exist but differ, refuse to proceed: that is a
    # content identity conflict the operator must resolve.
    report_path = SOURCES / f"climate-monitor-{args.date}.md"
    generated_path = REPORTS / f"climate-monitor-{args.date}.md"
    if not report_path.exists() and generated_path.exists():
        SOURCES.mkdir(parents=True, exist_ok=True)
        # pid-unique staging name: a fixed ".tmp" suffix lets concurrent or
        # retried runs clobber each other's staging file before os.replace.
        tmp_report = report_path.with_suffix(report_path.suffix + f".tmp.{os.getpid()}")
        try:
            shutil.copy2(generated_path, tmp_report)
            os.replace(tmp_report, report_path)
        except OSError:
            tmp_report.unlink(missing_ok=True)
            raise
        print(f"Promoted generated report to sources/: {report_path}")
    elif report_path.exists() and generated_path.exists():
        if report_path.read_bytes() != generated_path.read_bytes():
            print(
                f"ERROR: sources/ and generated reports disagree for {args.date}; "
                "resolve the content conflict before syncing",
                file=sys.stderr,
            )
            return 1
    if not report_path.exists():
        print(f"ERROR: report not found in {SOURCES} or {REPORTS}", file=sys.stderr)
        return 1

    # First-run bootstrap: plan-update/update refuse a missing database
    # (fail-closed), so initialize the schema before the first sync.
    if not DB.exists():
        init = run_cli(["init", "--database", str(DB)])
        print(f"Initialized registry database: {DB} (schema v{init.get('schema_version')})")

    try:
        plan_args = ["plan-update", "--source-dir", str(SOURCES), "--database", str(DB)]
        if args.allow_offcycle:
            plan_args.append("--allow-offcycle")
        plan = run_cli(plan_args)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    conflicts = plan.get("conflicts", [])
    if conflicts:
        print(f"ERROR: registry update plan contains {len(conflicts)} report identity conflict(s):", file=sys.stderr)
        for conflict in conflicts:
            print(f"  - {conflict}", file=sys.stderr)
        return 1

    if not plan.get("mutation_required"):
        print(f"OK: registry already up to date (unchanged reports: {plan.get('unchanged_report_count', 0)})")
        return 0

    try:
        update_args = [
            "update",
            "--source-dir", str(SOURCES),
            "--database", str(DB),
            "--backup-dir", str(BACKUP_DIR),
        ]
        if args.allow_offcycle:
            update_args.append("--allow-offcycle")
        updated = run_cli(update_args)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    imported = updated.get("imported_reports", [])
    print(f"OK: registry updated — imported reports: {imported or 'none'}; backup: {updated.get('backup')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
