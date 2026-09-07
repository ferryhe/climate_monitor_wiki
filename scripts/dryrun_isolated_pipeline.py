#!/usr/bin/env python3
"""Isolated v2 contract check; use dryrun_full_pipeline.py for all consumers.

Verifies the canonical 57-record v2 contract and the deterministic
mapping (33 updated + 9 unchanged + 14 blocked + 1 failed + 0 unresolved
= 57 → 57/42/15) without touching /opt/climate_monitor_wiki, sending
email, pushing to git, or reloading the API.

Run as:

    python3 scripts/dryrun_isolated_pipeline.py [--canary]

Returns: 0 PASS, non-zero FAIL.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


REPORT_DATE = "2026-09-07"


def _python() -> str:
    py = ROOT / ".venv" / "bin" / "python"
    if py.exists():
        return str(py)
    return sys.executable


def _build_v2_response(stats: dict, articles: list[dict]) -> dict:
    from climate_monitor.weekly_monitor.authoring_contract import (
        build_authoring_request,
    )
    from climate_monitor.weekly_monitor.driver import _candidate_items_from_evidence
    from climate_monitor.weekly_monitor.prompt_loader import load_weekly_monitor_prompt
    from climate_monitor.dedupe import canonical_url

    items = _candidate_items_from_evidence(None, {"records": articles})
    request = build_authoring_request(
        report_date=date.fromisoformat(REPORT_DATE),
        items=items,
        prompt=load_weekly_monitor_prompt(),
        article_evidence={"records": articles},
        stats=stats,
    )
    evidence_by_canonical: dict[str, dict] = {}
    for article in articles:
        canonical = canonical_url(article.get("final_url") or article.get("requested_url") or "")
        if canonical:
            evidence_by_canonical[canonical] = article

    response_articles = []
    for requested_article in request["articles"]:
        response_article = json.loads(json.dumps(requested_article))
        canonical = canonical_url(requested_article.get("url") or "")
        evidence_article = evidence_by_canonical.get(canonical)
        if evidence_article is None:
            continue
        response_article.update(
            {
                "relevant": True,
                "summary": "Honest content summary.",
                "summary_basis": evidence_article["summary_basis"],
                "evidence_hash": evidence_article.get("content_hash"),
                "categories": ["Supervision & Disclosure"],
                "keywords": ["climate", "insurance", "capital"],
                "title_basis": evidence_article["title_basis"],
                "display_pillar": evidence_article["display_pillar"],
                "origins": evidence_article["origins"],
            }
        )
        response_articles.append(response_article)

    return {
        "schema_version": "weekly-monitor-authoring-response.v2",
        "contract_version": "weekly-monitor-authoring.v2",
        "request_sha256": request["request_sha256"],
        "article_count": len(response_articles),
        "articles": response_articles,
        "executive_summary": (
            f"{stats['total']} total; {stats['updated']} updated; "
            f"{stats['unchanged']} unchanged; {stats['blocked']} blocked; "
            f"{stats['failed']} failed; {stats['unresolved']} unresolved."
        ),
        "stats": stats,
    }


def _driver_validation_pass(stats: dict, articles: list[dict]) -> None:
    """Drive the production driver's v2 validation only — no orchestrator run.

    Re-runs the exact contract the dry-run cares about (AC-2 count mapping
    + AC-1 33/9/14/1/0 = 57 → 57/42/15) without touching the production
    checkout. The orchestrator's kept-set reconciliation requires a real
    manifest fixture that matches the evidence URL; the focused
    ``test_v2_authoring_response_exposes_canonical_57_42_15_split`` test
    in tests/weekly_monitor/test_weekly_driver.py already proves that the
    full chain round-trips a single kept article through the orchestrator
    with the same IAIS URL the fixture uses.
    """
    from climate_monitor.weekly_monitor.driver import run_weekly_monitor

    workspace = Path(tempfile.mkdtemp(prefix="climate-dryrun-driver-"))
    try:
        # Use a minimal manifest that matches the evidence URL exactly.
        # The IAIS URL is the only kept candidate so the v2 response
        # covers it 1:1.
        manifest_path = workspace / "manifest.json"
        manifest_path.write_text(
            (ROOT / "tests" / "fixtures" / "issue87" / "iais_minimal_manifest.json").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )

        response_path = workspace / "v2_response.json"
        response_path.write_text(
            json.dumps(_build_v2_response(stats, articles)), encoding="utf-8"
        )
        source_dir = workspace / "sources"
        wiki_dir = workspace / "wiki"
        state = workspace / "state"
        for sub in (source_dir, wiki_dir, state):
            sub.mkdir()

        result = run_weekly_monitor(
            source_config_path=ROOT / "monitoring" / "supranational_sources.yaml",
            run_config_path=ROOT / "monitoring" / "run_config.yaml",
            report_date=date.fromisoformat(REPORT_DATE),
            manifest_fixture_path=manifest_path,
            site_scopes_path=ROOT / "monitoring" / "site_scopes.yaml",
            state_dir=state,
            source_dir=source_dir,
            wiki_dir=wiki_dir,
            sync=False,
            update_seen_state=False,
            authoring_response_path=response_path,
            repository_commit_sha="f" * 40,
            article_evidence={"records": articles},
            stats=stats,
        )

        envelope = json.loads(result.to_json())
        exposed = envelope.get("stats")
        if exposed != stats:
            raise AssertionError(
                f"stats round-trip failed: got {exposed}, expected {stats}"
            )

        succeeded = stats["updated"] + stats["unchanged"]
        failed = stats["blocked"] + stats["failed"]
        if not (succeeded == 42 and failed == 15 and stats["total"] == 57):
            raise AssertionError(
                f"mapping failed: total={stats['total']} succeeded={succeeded} "
                f"failed={failed}; expected 57/42/15"
            )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _tamper_guard_pass(stats: dict, articles: list[dict]) -> None:
    """Tamper guard: the production driver must fail closed when the
    deterministic sum is broken even if all 6 keys are present.
    """
    from climate_monitor.weekly_monitor.authoring_contract import AuthoringContractError
    from climate_monitor.weekly_monitor.driver import run_weekly_monitor

    workspace = Path(tempfile.mkdtemp(prefix="climate-dryrun-tamper-"))
    try:
        manifest_path = workspace / "manifest.json"
        manifest_path.write_text(
            (ROOT / "tests" / "fixtures" / "issue87" / "iais_minimal_manifest.json").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        tampered = _build_v2_response(stats, articles)
        tampered["stats"] = {**stats, "total": stats["total"] + 1}
        tampered_path = workspace / "tampered.json"
        tampered_path.write_text(json.dumps(tampered), encoding="utf-8")

        source_dir = workspace / "sources"
        wiki_dir = workspace / "wiki"
        state = workspace / "state"
        for sub in (source_dir, wiki_dir, state):
            sub.mkdir()

        try:
            run_weekly_monitor(
                source_config_path=ROOT / "monitoring" / "supranational_sources.yaml",
                run_config_path=ROOT / "monitoring" / "run_config.yaml",
                report_date=date.fromisoformat(REPORT_DATE),
                manifest_fixture_path=manifest_path,
                site_scopes_path=ROOT / "monitoring" / "site_scopes.yaml",
                state_dir=state,
                source_dir=source_dir,
                wiki_dir=wiki_dir,
                sync=False,
                update_seen_state=False,
                authoring_response_path=tampered_path,
                repository_commit_sha="e" * 40,
                article_evidence={"records": articles},
                stats=stats,
            )
        except AuthoringContractError as exc:
            msg = str(exc)
            assert "stats.total" in msg or "deterministic" in msg, msg
        else:
            raise AssertionError("driver accepted tampered stats")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canary", action="store_true")
    args = parser.parse_args()

    fixture_dir = ROOT / "tests" / "fixtures" / "issue87"
    stats_path = fixture_dir / "57_stats.json"
    evidence_path = fixture_dir / "57_article_evidence.json"
    if not stats_path.exists() or not evidence_path.exists():
        print(
            f"[dryrun] FAIL: missing fixture under {fixture_dir}",
            file=sys.stderr,
        )
        return 3

    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    article_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    _driver_validation_pass(stats, article_evidence["records"])
    print("[dryrun] PASS contract validation (AC-2 57/42/15 mapping)")

    _tamper_guard_pass(stats, article_evidence["records"])
    print("[dryrun] PASS tamper guard (deterministic sum enforced)")

    # Informational: confirm production HEAD is untouched.
    prod_root = Path("/opt/climate_monitor_wiki")
    if prod_root.exists():
        head = subprocess.run(
            ["git", "-C", str(prod_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        print(f"[dryrun] production HEAD (read-only observation; not an unchanged-state proof): {head}")

    succeeded_count = stats["updated"] + stats["unchanged"]
    failed_count = stats["blocked"] + stats["failed"]
    print(
        f"[dryrun] AC-2 mapping verified: total={stats['total']}, "
        f"succeeded={succeeded_count} (updated+unchanged), "
        f"failed={failed_count} (blocked+failed), "
        f"unresolved={stats['unresolved']} (not double-counted)"
    )
    print("[dryrun] CONTRACT PASS (not a full-chain verification)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())