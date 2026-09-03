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
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("CLIMATE_WIKI_HOME", "/home/ubuntu/climate_monitor_wiki"))
SOURCES = Path(os.environ.get("CLIMATE_WIKI_SOURCES", str(HOME / "sources")))
PYTHON = Path(os.environ.get("CLIMATE_WIKI_PYTHON", str(HOME / ".venv" / "bin" / "python")))
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
    args = parser.parse_args()

    report_path = SOURCES / f"climate-monitor-{args.date}.md"
    if not report_path.exists():
        print(f"ERROR: source report not found: {report_path}", file=sys.stderr)
        return 1

    try:
        plan = run_cli(["plan-update", "--source-dir", str(SOURCES), "--database", str(DB)])
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
        updated = run_cli(
            [
                "update",
                "--source-dir", str(SOURCES),
                "--database", str(DB),
                "--backup-dir", str(BACKUP_DIR),
            ]
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    imported = updated.get("imported_reports", [])
    print(f"OK: registry updated — imported reports: {imported or 'none'}; backup: {updated.get('backup')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
