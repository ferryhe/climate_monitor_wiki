from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_monitor.orchestrator import run_monitor
from climate_monitor.weekly_monitor.driver import run_weekly_monitor


def _parse_loopback_provider(spec: str) -> tuple:
    """Parse a ``module:callable`` loopback spec into a tuple of providers.

    Test/CI seam only — used by ``--article-evidence-loopback``. Empty spec
    yields an empty tuple so the orchestrator falls back to its default
    providers (or the honest ``unavailable`` records when none are wired).
    """

    spec = (spec or "").strip()
    if not spec:
        return ()
    if ":" not in spec:
        raise SystemExit(
            "--article-evidence-loopback requires 'module:callable' form"
        )
    module_name, _, attr = spec.partition(":")
    module_name = module_name.strip()
    attr = attr.strip()
    if not module_name or not attr:
        raise SystemExit(
            "--article-evidence-loopback requires non-empty module and callable"
        )
    module = importlib.import_module(module_name)
    provider = getattr(module, attr, None)
    if not callable(provider):
        raise SystemExit(
            f"--article-evidence-loopback target {spec!r} is not callable"
        )
    return (provider,)


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
    parser.add_argument(
        "--article-evidence-loopback", default="", metavar="MODULE:CALLABLE",
        help="Inject an article-content provider for test/CI evidence staging.",
    )
    parser.add_argument("--json", action="store_true", help="Print structured JSON for ai_interface.")
    parser.add_argument(
        "--article-evidence",
        default="",
        help=(
            "Optional path to a pre-staged article-evidence.v1 JSON envelope. "
            "When provided, --production-weekly invokes the v2 request emitter "
            "and response validator before the orchestrator runs. "
            "--stats must accompany this."
        ),
    )
    parser.add_argument(
        "--stats",
        default="",
        help=(
            "Deterministic stats JSON (object with checked, succeeded, "
            "failed keys) used to build the v2 authoring request. "
            "Required when --article-evidence is set."
        ),
    )
    args = parser.parse_args()

    if args.production_weekly and not args.authoring_response:
        parser.error("--production-weekly requires --authoring-response")
    if bool(args.article_changes_artifact) != bool(args.pillar_b_artifact):
        parser.error(
            "--article-changes-artifact and --pillar-b-artifact must be supplied together"
        )
    if args.article_changes_artifact and (args.manifest_fixture or args.research_fixture):
        parser.error("current Pillar artifacts cannot be combined with manifest/research fixtures")
    if args.production_weekly:
        if bool(args.article_evidence) != bool(args.stats):
            parser.error(
                "--article-evidence and --stats must be supplied together "
                "(both required for v2 evidence path)"
            )

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
        article_evidence_payload: dict | None = None
        stats_payload: dict | None = None
        if args.article_evidence:
            try:
                article_evidence_payload = json.loads(
                    Path(args.article_evidence).read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                parser.error(
                    f"--article-evidence file is not readable JSON: {exc}"
                )
            try:
                stats_payload = json.loads(args.stats)
            except ValueError as exc:
                parser.error(f"--stats is not valid JSON: {exc}")
            if not isinstance(stats_payload, dict):
                parser.error("--stats must be a JSON object")
        result = run_weekly_monitor(
            **common,
            authoring_response_path=Path(args.authoring_response) if args.authoring_response else None,
            model_provider=args.model_provider,
            model=args.model,
            temperature=args.temperature,
            max_output_tokens=args.max_output_tokens,
            article_evidence=article_evidence_payload,
            stats=stats_payload,
        )
    else:
        if args.authoring_response:
            parser.error("--authoring-response requires --production-weekly")
        providers = _parse_loopback_provider(args.article_evidence_loopback)
        common_kwargs = dict(common)
        if providers:
            common_kwargs["providers"] = providers
        result = run_monitor(**common_kwargs)

    if args.json:
        print(result.to_json(), end="")
        return

    if result.report_path:
        print(f"Report written: {result.report_path}")
        print(f"Items included: {len(result.items)}")
        print(f"Wiki synced: {'yes' if result.synced else 'no'}")
    else:
        print("No monitor-matching updates found; no report written.")
    # Surface the article-evidence.v1 artifact path so downstream consumers
    # (Issue #93) can locate it without scanning ``source_dir``. The
    # orchestrator writes one artifact per run that survives long enough
    # to reach this print, when at least one candidate was processed;
    # the path is computed the same way the writer does.
    if result.report_path:
        artifact_path = Path(result.report_path).parent / (
            f"article-evidence.v1_{result.report_date.isoformat()}.json"
        )
        print(f"Article evidence: {artifact_path}")
    for note in result.dedup_notes:
        print(f"Dedup: {note}")
    for warning in result.warnings:
        print(f"Warning: {warning}")


if __name__ == "__main__":
    main()
