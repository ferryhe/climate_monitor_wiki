from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_monitor.orchestrator import run_monitor


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the climate web listening monitor.")
    parser.add_argument("--source-config", default="monitoring/supranational_sources.yaml")
    parser.add_argument("--run-config", default="monitoring/run_config.yaml")
    parser.add_argument("--date", default="")
    parser.add_argument("--manifest-fixture", default="")
    parser.add_argument("--research-fixture", default="")
    parser.add_argument("--site-scopes", default="monitoring/site_scopes.yaml")
    parser.add_argument("--state-dir", default="monitoring/state")
    parser.add_argument("--source-dir", default="")
    parser.add_argument("--wiki-dir", default="")
    parser.add_argument("--no-sync", action="store_true")
    parser.add_argument("--no-update-seen-state", action="store_true")
    args = parser.parse_args()

    report_date = date.fromisoformat(args.date) if args.date else None
    result = run_monitor(
        source_config_path=Path(args.source_config),
        run_config_path=Path(args.run_config),
        report_date=report_date,
        manifest_fixture_path=Path(args.manifest_fixture) if args.manifest_fixture else None,
        research_fixture_path=Path(args.research_fixture) if args.research_fixture else None,
        site_scopes_path=Path(args.site_scopes) if args.site_scopes else None,
        state_dir=Path(args.state_dir),
        source_dir=Path(args.source_dir) if args.source_dir else None,
        wiki_dir=Path(args.wiki_dir) if args.wiki_dir else None,
        sync=not args.no_sync,
        update_seen_state=not args.no_update_seen_state,
    )

    if result.report_path:
        print(f"Report written: {result.report_path}")
        print(f"Items included: {len(result.items)}")
        print(f"Wiki synced: {'yes' if result.synced else 'no'}")
    else:
        print("No climate-related updates found; no report written.")
    for note in result.dedup_notes:
        print(f"Dedup: {note}")
    for warning in result.warnings:
        print(f"Warning: {warning}")


if __name__ == "__main__":
    main()
