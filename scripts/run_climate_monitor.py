from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_monitor.orchestrator import run_monitor
from climate_monitor.weekly_monitor.driver import run_weekly_monitor


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the climate web listening monitor.")
    parser.add_argument("--source-config", default="monitoring/supranational_sources.yaml")
    parser.add_argument("--run-config", default="monitoring/run_config.yaml")
    parser.add_argument("--date", default="")
    parser.add_argument("--manifest-fixture", default="")
    parser.add_argument("--research-fixture", default="")
    parser.add_argument(
        "--article-changes-artifact",
        default="",
        help="Current Step 1a article_changes JSON; requires --pillar-b-artifact.",
    )
    parser.add_argument(
        "--pillar-b-artifact",
        default="",
        help="Current Step 1b pillar_b JSON; requires --article-changes-artifact.",
    )
    parser.add_argument("--site-scopes", default="monitoring/site_scopes.yaml")
    parser.add_argument("--state-dir", default="monitoring/state")
    parser.add_argument("--source-dir", default="")
    parser.add_argument("--wiki-dir", default="")
    parser.add_argument("--no-sync", action="store_true")
    parser.add_argument(
        "--no-update-seen-state",
        action="store_true",
        help=(
            "Do not prepare or commit URL history or live source checkpoints. By "
            "default they commit only after Markdown, semantic sidecar, combined "
            "candidate evidence, full candidate-item snapshot, and canonical URL "
            "state succeed."
        ),
    )
    parser.add_argument(
        "--production-weekly",
        action="store_true",
        help="Use the repo-owned strict weekly driver and versioned prompt.",
    )
    parser.add_argument("--authoring-response", default="")
    parser.add_argument("--model-provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="Print structured JSON for ai_interface.")
    args = parser.parse_args()

    if args.production_weekly and not args.authoring_response:
        parser.error("--production-weekly requires --authoring-response")
    if bool(args.article_changes_artifact) != bool(args.pillar_b_artifact):
        parser.error(
            "--article-changes-artifact and --pillar-b-artifact must be supplied together"
        )
    if args.article_changes_artifact and (args.manifest_fixture or args.research_fixture):
        parser.error("current Pillar artifacts cannot be combined with manifest/research fixtures")

    report_date = date.fromisoformat(args.date) if args.date else None
    common = {
        "source_config_path": Path(args.source_config),
        "run_config_path": Path(args.run_config),
        "report_date": report_date,
        "manifest_fixture_path": Path(args.manifest_fixture) if args.manifest_fixture else None,
        "research_fixture_path": Path(args.research_fixture) if args.research_fixture else None,
        "article_changes_artifact_path": (
            Path(args.article_changes_artifact) if args.article_changes_artifact else None
        ),
        "pillar_b_artifact_path": Path(args.pillar_b_artifact) if args.pillar_b_artifact else None,
        "site_scopes_path": Path(args.site_scopes) if args.site_scopes else None,
        "state_dir": Path(args.state_dir),
        "source_dir": Path(args.source_dir) if args.source_dir else None,
        "wiki_dir": Path(args.wiki_dir) if args.wiki_dir else None,
        "sync": not args.no_sync,
        "update_seen_state": not args.no_update_seen_state,
    }
    if args.production_weekly:
        result = run_weekly_monitor(
            **common,
            authoring_response_path=Path(args.authoring_response) if args.authoring_response else None,
            model_provider=args.model_provider,
            model=args.model,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
        )
    else:
        if args.authoring_response:
            parser.error("--authoring-response requires --production-weekly")
        result = run_monitor(**common)

    if args.json:
        print(result.to_json(), end="")
        return

    if result.report_path:
        print(f"Report written: {result.report_path}")
        print(f"Items included: {len(result.items)}")
        print(f"Wiki synced: {'yes' if result.synced else 'no'}")
    else:
        print("No monitor-matching updates found; no report written.")
    for note in result.dedup_notes:
        print(f"Dedup: {note}")
    for warning in result.warnings:
        print(f"Warning: {warning}")


if __name__ == "__main__":
    main()
