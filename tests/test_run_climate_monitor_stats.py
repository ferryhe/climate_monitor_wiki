"""Regression tests for the three surgical fixes to
``scripts/run_climate_monitor.py`` flagged by remote Copilot review on
PR #104 (Issue #87).

Fix A: ``_derive_stats`` must not double-count ``unresolved`` when
       computing ``expected_total`` (rejects valid outcomes such as
       ``updated=1, unchanged=0, blocked=0, failed=1, unresolved=1,
       total=3``).
Fix B: ``_run_prepare`` must write ``candidate_item_snapshot`` into
       the staging dir, never into ``source_dir``.
Fix C: ``_run_finalize`` must read the report date from the bundle,
       not from the operator's CLI ``--report-date``.

Counts are validated against upstream source dispositions. Discovered
articles do not have to occur one-for-one with those dispositions.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

# Import the helpers from the existing AC-1..AC-12 suite. They are
# defined at module scope in test_issue87_live_chain.py and we re-use
# the same fixture builder so the regression tests stay consistent
# with the broader production-chain contract.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_issue87_live_chain import (  # noqa: E402
    CLI,
    FIXTURE_DIR,
    REPORT_DATE,
    ROOT,
    _build_57_record_outcome,
    _build_response_from_request,
    _prepare_env,
    _call,
)

from scripts.run_climate_monitor import _derive_stats  # noqa: E402


# ---------------------------------------------------------------------------
# Fix A — _derive_stats does not double-count unresolved
# ---------------------------------------------------------------------------


def _records_for_outcome(outcome_counts: dict, *, dispositions: list[str]) -> list[dict]:
    """Build a manifest-shaped record list whose ``disposition`` matches
    ``dispositions``. The records are paired 1:1 with the disposition
    labels so ``observed`` in ``_derive_stats`` agrees with ``derived``."""
    records = []
    for i, disp in enumerate(dispositions):
        url = f"https://www.example.test/{i:03d}"
        records.append({
            "final_url": url,
            "requested_url": url,
            "title": f"record {i:03d}",
            "summary": "",
            "summary_basis": "page",
            "title_basis": "upstream_artifact",
            "display_pillar": "A",
            "origins": [{"pillar": "A", "source": "test", "url": url}],
            "disposition": disp,
            "content_hash": hashlib.sha256(url.encode()).hexdigest(),
        })
    return records


def test_derive_stats_does_not_double_count_unresolved():
    """With ``failed=1, unresolved=1, total=3`` the v2 derivation must
    return a stats dict whose components sum to 3, not 4. Before the
    fix, the ``+ derived["unresolved"]`` addend inside ``expected_total``
    double-counted unresolved and the function raised
    ``SystemExit("outcome counts inconsistent: requested=3 vs
    components sum=4")``.
    """
    counts = {
        "requested": 3,
        "updated": 1,
        "unchanged": 0,
        "blocked": 0,
        "failed": 1,
        "unresolved": 1,
    }
    outcome = {"counts": counts, "dispositions": [
        {"disposition": name} for name in ("updated", "failed", "unresolved")]}
    records = _records_for_outcome(
        counts, dispositions=["updated", "failed", "failed"],
    )
    # Must NOT raise.
    stats = _derive_stats(records, outcome)
    assert stats == {
        "total": 3,
        "updated": 1,
        "unchanged": 0,
        "blocked": 0,
        "failed": 2,   # failed=1 + unresolved=1
        "unresolved": 1,
    }
    # The canonical total must equal the sum of the 4 mutually exclusive
    # buckets + failed (no extra unresolved addend).
    assert stats["updated"] + stats["unchanged"] + stats["blocked"] + stats["failed"] == stats["total"]


def test_derive_stats_full_dry_run_fixture_passes():
    """The 57-record fixture (33/9/14/1/0) must continue to derive
    without raising. This protects the existing AC-7 fixture from the
    minimal-correction regression introduced by Fix A.
    """
    workspace = Path("/tmp/_derive_stats_fixture_workspace")
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        fixture_dir = _build_57_record_outcome(workspace)
        outcome = json.loads(
            (fixture_dir / "acquisition-batch-result.v2.json").read_text()
        )
        manifest = json.loads(
            (fixture_dir / "web-listening-manifest.v1.json").read_text()
        )
        from scripts.run_climate_monitor import _collect_same_run_records, _attach_outcome_disposition
        records = _attach_outcome_disposition(_collect_same_run_records(outcome, manifest), outcome)
        # Must not raise.
        stats = _derive_stats(records, outcome)
        assert stats["total"] == 57
        assert stats["updated"] == 33
        assert stats["unchanged"] == 9
        assert stats["blocked"] == 14
        assert stats["failed"] == 1   # failed=1, unresolved=0
        assert stats["unresolved"] == 0
    finally:
        # Clean up so the test is hermetic.
        for child in workspace.iterdir():
            if child.is_dir():
                for sub in child.rglob("*"):
                    if sub.is_file():
                        sub.unlink()
                child.rmdir()
            else:
                child.unlink()
        workspace.rmdir()


# ---------------------------------------------------------------------------
# Fix B — prepare writes snapshot to staging_dir only
# ---------------------------------------------------------------------------


def test_ac7_full_dry_run_chain_does_not_write_to_sources_before_finalize(tmp_path):
    """After ``prepare`` (and before ``finalize``), ``source_dir`` must
    not contain a ``candidate_item_snapshot*`` file. The snapshot
    lives under ``staging_dir`` until finalize materialises source
    artifacts through the orchestrator's #91 transaction.
    """
    fixture = _build_57_record_outcome(tmp_path)
    env = _prepare_env(tmp_path, dry_run=True, fixture_dir=fixture)
    staging_dir = _run_prepare_only(tmp_path, env, fixture)
    source_snapshots = list(
        (tmp_path / "source_dir").glob("candidate_item_snapshot*")
    )
    staging_snapshots = list(
        staging_dir.glob("candidate_item_snapshot*")
    )
    assert source_snapshots == [], (
        f"prepare leaked candidate_item_snapshot into source_dir: "
        f"{source_snapshots}"
    )
    assert staging_snapshots, (
        "prepare did not write candidate_item_snapshot into staging_dir"
    )


# ---------------------------------------------------------------------------
# Fix C — finalize uses bundle's pinned report_date, not --report-date
# ---------------------------------------------------------------------------


def test_finalize_uses_bundle_report_date_over_cli_arg(tmp_path):
    """When the operator passes a stale ``--report-date`` to finalize
    that disagrees with the bundle's pinned date, the orchestrator
    must observe the bundle's pinned date (the prepared report file
    must land at the bundle date, not the CLI date).
    """
    fixture = _build_57_record_outcome(tmp_path)
    env = _prepare_env(tmp_path, dry_run=True, fixture_dir=fixture)
    staging_dir = _run_prepare_only(tmp_path, env, fixture)
    response_path = _build_response_from_request(staging_dir, fixture)
    stale_cli_date = "2099-01-01"
    finalize = _call(CLI + [
        "--production-weekly", "--authoring-mode", "finalize",
        "--report-date", stale_cli_date,
        "--staging-dir", str(staging_dir),
        "--authoring-response", str(response_path),
        "--article-evidence-loopback",
        "scripts.hermes_job:dry_run_unavailable_provider",
        "--no-update-seen-state", "--no-sync", "--json",
        "--state-dir", env["CLIMATE_STATE_DIR"],
        "--source-dir", env["CLIMATE_SOURCE_DIR"],
        "--wiki-dir", env["CLIMATE_WIKI_DIR"],
        "--source-config", env["CLIMATE_SOURCE_CONFIG"],
        "--run-config", env["CLIMATE_RUN_CONFIG"],
        "--site-scopes", env["CLIMATE_SITE_SCOPES"],
    ], env=env)
    assert finalize.returncode == 0, finalize.stdout + finalize.stderr
    # The pinned bundle date must win, not the stale CLI date.
    pinned_report = list((tmp_path / "source_dir").glob(
        f"climate-monitor-{REPORT_DATE}.md"
    ))
    stale_report = list((tmp_path / "source_dir").glob(
        f"climate-monitor-{stale_cli_date}.md"
    ))
    assert pinned_report, (
        f"expected report under bundle date {REPORT_DATE}; got "
        f"{list((tmp_path / 'source_dir').glob('climate-monitor-*.md'))}"
    )
    assert not stale_report, (
        f"finalize ignored bundle date and wrote under CLI date "
        f"{stale_cli_date}: {stale_report}"
    )


# ---------------------------------------------------------------------------
# helpers — kept local so this regression file is self-contained
# ---------------------------------------------------------------------------


def _run_prepare_only(workspace: Path, env: dict, fixture: Path) -> Path:
    """Run the production-weekly prepare step and return the staging dir.

    Mirrors ``_run_prepare`` from test_issue87_live_chain.py but is kept
    here so this regression module does not depend on a private name
    that future refactors may rename.
    """
    staging_dir = workspace / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    env["CLIMATE_OUTCOME_ARTIFACT"] = str(
        fixture / "acquisition-batch-result.v2.json"
    )
    env["CLIMATE_MANIFEST_ARTIFACT"] = str(
        fixture / "web-listening-manifest.v1.json"
    )
    env["CLIMATE_PILLAR_B_ARTIFACT"] = str(fixture / "pillar-b.json")
    result = _call(CLI + [
        "--production-weekly", "--authoring-mode", "prepare",
        "--article-evidence-loopback", "scripts.hermes_job:dry_run_unavailable_provider",
        "--report-date", REPORT_DATE,
        "--acquisition-batch", env["CLIMATE_OUTCOME_ARTIFACT"],
        "--web-listening-manifest", env["CLIMATE_MANIFEST_ARTIFACT"],
        "--pillar-b-artifact", env["CLIMATE_PILLAR_B_ARTIFACT"],
        "--state-dir", env["CLIMATE_STATE_DIR"],
        "--source-dir", env["CLIMATE_SOURCE_DIR"],
        "--wiki-dir", env["CLIMATE_WIKI_DIR"],
        "--source-config", env["CLIMATE_SOURCE_CONFIG"],
        "--run-config", env["CLIMATE_RUN_CONFIG"],
        "--site-scopes", env["CLIMATE_SITE_SCOPES"],
        "--staging-dir", str(staging_dir),
        "--no-sync",
        "--json",
    ], env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (staging_dir / "bundle.json").is_file()
    return staging_dir
