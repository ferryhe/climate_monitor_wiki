from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_monitor.article_content_adapter import (
    build_article_evidence_artifact,
    write_article_evidence_artifact,
)
from climate_monitor.orchestrator import run_monitor
from climate_monitor.weekly_monitor.driver import run_weekly_monitor


def _stage_article_evidence(
    *,
    items,
    report_date: date,
    source_dir: Path,
    providers=(),
    manifest_path: str | Path | None = None,
) -> Path | None:
    """Stage the post-#91 unique-candidate evidence artifact.

    This is the Issue #92 thin content-adapter layer: it consumes the
    unique-candidate list (already deduplicated by ``orchestrator``) and
    emits a versioned ``article-evidence.v1`` artifact for issue #93 to
    consume. When ``web_listening#70`` is unavailable, every record is
    URL-only with explicit ``status="unavailable"`` — no content is
    fabricated. Steps 1-5 are not modified.
    """

    if not items:
        return None
    # Candidate selection already used read_manifest_items in the orchestrator.
    # Join producer provenance here because CandidateItem carries source_item_id
    # and source_name but has no run/status fields. Never rediscover candidates.
    provenance = {}
    if manifest_path:
        payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        manifests = payload if isinstance(payload, list) else [payload]
        for manifest in manifests:
            source = manifest.get("source") or {}
            source_name = source.get("site_name") or source.get("source_id") or "Website"
            for raw in manifest.get("discovered_items", []):
                if raw.get("status") not in {"changed", "downloaded", "new", "updated"}:
                    continue
                provenance.setdefault((source_name, raw.get("item_id"), raw.get("url")), {
                    "source_id": source.get("source_id"),
                    "run_id": (raw.get("provenance") or {}).get("run_id") or (manifest.get("run") or {}).get("run_id"),
                    "item_status": raw["status"],
                })
    unique_articles = []
    for item in items:
        raw = {"article_id": "", "url": item.url, "title": item.title,
               "source_item_id": item.source_item_id, "source_name": item.source_name}
        raw.update(provenance.get((item.source_name, item.source_item_id, item.url), {}))
        # Candidate summaries/evidence snippets are not search_snippet inputs.
        if getattr(item, "search_snippet", None):
            raw["search_snippet"] = item.search_snippet
        unique_articles.append(raw)
    artifact = build_article_evidence_artifact(
        unique_articles,
        providers=providers,
        report_date=report_date.isoformat(),
    )
    return write_article_evidence_artifact(
        source_dir, report_date.isoformat(), artifact
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the climate web listening monitor.")
    parser.add_argument("--source-config", default="monitoring/supranational_sources.yaml")
    parser.add_argument("--run-config", default="monitoring/run_config.yaml")
    parser.add_argument("--date", default="")
    parser.add_argument("--manifest-fixture", default="")
    parser.add_argument("--research-fixture", default="")
    parser.add_argument(
        "--article-evidence-loopback", default="", metavar="MODULE:CALLABLE",
        help="Inject an article-content provider for test/CI evidence staging.",
    )
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

    # Issue #92 wiring: stage the post-#91 unique-candidate evidence artifact.
    # Steps 1-5 are not modified — this is a thin, additive stage. Failure to
    # stage must not break the report write.
    if result.items:
        try:
            from climate_monitor.config import load_run_config
            output_source_dir = (
                Path(args.source_dir) if args.source_dir
                else Path(load_run_config(args.run_config).source_dir)
            )
            if not output_source_dir.is_absolute():
                output_source_dir = Path.cwd() / output_source_dir
            providers = ()
            if args.article_evidence_loopback:
                module_name, separator, callable_name = args.article_evidence_loopback.partition(":")
                if not separator or not module_name or not callable_name:
                    raise ValueError("--article-evidence-loopback requires module:callable")
                provider = getattr(importlib.import_module(module_name), callable_name)
                if not callable(provider):
                    raise ValueError("--article-evidence-loopback target is not callable")
                providers = (provider,)
            artifact_path = _stage_article_evidence(
                items=result.items,
                report_date=result.report_date,
                source_dir=output_source_dir,
                providers=providers,
                manifest_path=args.manifest_fixture or None,
            )
            if artifact_path is not None and not args.json:
                print(f"Article evidence: {artifact_path}")
        except Exception as exc:  # noqa: BLE001 - honest error surfacing
            warning = f"article-evidence staging failed: {exc}"
            if args.json:
                result = replace(result, warnings=(*result.warnings, warning))
            else:
                print(f"Warning: {warning}")

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
