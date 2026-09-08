"""Issue #87 AC-1..AC-12: production monitor must run the real same-run chain.

The production ``scripts/run_climate_monitor.py --production-weekly`` accepts
exactly two same-day authoring modes -- ``prepare`` and ``finalize`` -- over
the versioned public monitoring contract. ``prepare`` materialises one
staging bundle (``combined.candidates``, ``candidate_item_snapshot``,
``article-evidence.v1`` envelope, deterministic stats, the v2 authoring
request, and a single bundle digest) from the public outcome + manifest
artifacts and Pillar B; ``finalize`` consumes that bundle plus exactly one
authoring response and commits the existing #91 transaction atomically.

Production never accepts a pre-assembled authoring response, article
evidence, or stats from the operator. Dry-run (``CLIMATE_DRY_RUN=1``)
exercises the same CLI paths and the existing approved fixture set, with no
private web_listening calls.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from pillar_b_fixture import discovery_fixture

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT
PYTHON = sys.executable

REPORT_DATE = "2026-09-07"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "issue87"

CLI = [PYTHON, str(ROOT / "scripts" / "run_climate_monitor.py")]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare_env(workspace: Path, *, dry_run: bool = False,
                 fixture_dir: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLIMATE_", "REPORT_", "ARTICLE_",
                                "STATS_", "AUTHORING_"))}
    env["HERMES_INFERENCE_MODEL"] = "fixture-model"
    env["HERMES_INFERENCE_PROVIDER"] = "fixture-provider"
    env["REPORT_DATE"] = REPORT_DATE
    # Keep the ambient PYTHONPATH (which carries the installed pydantic /
    # web_listening dependencies under the active test runner) so the spawned
    # ``run_climate_monitor.py`` subprocess can import them, with ROOT first.
    ambient_pythonpath = os.environ.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(ROOT), ambient_pythonpath) if p
    )
    for name in ("state_dir", "source_dir", "wiki_dir", "job_status_dir",
                 "delivery_output_dir", "delivery_state_dir",
                 "run_ledger_dir", "reports_dir", "registry_db_dir",
                 "registry_backup_dir", "registry_lock_dir",
                 "registry_artifact_dir"):
        (workspace / name).mkdir(parents=True, exist_ok=True)
    for name, env_name in (("state_dir", "CLIMATE_STATE_DIR"),
                           ("source_dir", "CLIMATE_SOURCE_DIR"),
                           ("wiki_dir", "CLIMATE_WIKI_DIR"),
                           ("job_status_dir", "CLIMATE_JOB_STATUS_DIR"),
                           ("delivery_output_dir", "CLIMATE_DELIVERY_OUTPUT_DIR"),
                           ("delivery_state_dir", "CLIMATE_DELIVERY_STATE_DIR"),
                           ("run_ledger_dir", "CLIMATE_RUN_LEDGER_DIR"),
                           ("reports_dir", "CLIMATE_REPORTS_DIR"),
                           ("registry_db_dir", "CLIMATE_REGISTRY_DB_DIR"),
                           ("registry_backup_dir", "CLIMATE_REGISTRY_BACKUP_DIR"),
                           ("registry_lock_dir", "CLIMATE_REGISTRY_LOCK_DIR"),
                           ("registry_artifact_dir", "CLIMATE_REGISTRY_ARTIFACT_DIR")):
        env[env_name] = str(workspace / name)
    env["CLIMATE_SOURCE_CONFIG"] = str(ROOT / "monitoring" / "supranational_sources.yaml")
    env["CLIMATE_RUN_CONFIG"] = str(ROOT / "monitoring" / "run_config.yaml")
    env["CLIMATE_SITE_SCOPES"] = str(ROOT / "monitoring" / "site_scopes.yaml")
    env["CLIMATE_REPORT_PATH"] = str(workspace / "source_dir" / f"climate-monitor-{REPORT_DATE}.md")
    env["CLIMATE_JOB_STATUS_DIR"] = str(workspace / "job_status_dir")
    env["CLIMATE_REGISTRY_DB"] = str(workspace / "registry_db_dir" / "registry.sqlite3")
    env["CLIMATE_REGISTRY_BACKUP_DIR"] = str(workspace / "registry_backup_dir")
    env["CLIMATE_REGISTRY_LOCK"] = str(workspace / "registry_lock_dir" / "registry.sqlite3.lock")
    env["CLIMATE_REGISTRY_ARTIFACT_DIR"] = str(workspace / "registry_artifact_dir")
    env["CLIMATE_DRY_RUN_ROOT"] = str(workspace)
    if dry_run:
        env["CLIMATE_DRY_RUN"] = "1"
        env["CLIMATE_DRY_RUN_OUTCOME_FIXTURE"] = "1"
        if fixture_dir is not None:
            env["CLIMATE_DRY_RUN_FIXTURE_DIR"] = str(fixture_dir)
    return env


def _call(args, *, env, cwd=None):
    return subprocess.run(args, cwd=cwd or ROOT, env=env, capture_output=True,
                          text=True, timeout=300)


def _parse_json_lines(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


# ---------------------------------------------------------------------------
# AC-1: production monitor_command now goes through prepare -> finalize
# ---------------------------------------------------------------------------


def test_ac1_production_monitor_no_longer_requires_three_presets(tmp_path):
    """AC-1: production monitor must NOT require the operator to pre-assemble
    AUTHORING_RESPONSE / ARTICLE_EVIDENCE / CLIMATE_STATS_PATH. The single
    public entrypoint is the production-weekly CLI in prepare/finalize mode
    over the public outcome+manifest artifacts; an upstream-empty production
    workspace must fail closed before any seen/report, not via a hand-built
    JSON triple."""
    env = _prepare_env(tmp_path, dry_run=False)
    # Public artifacts do not yet exist. The same-run preflight must fail
    # closed before any prepared bundle, response, or stats is assembled.
    missing = tmp_path / "missing-public"
    env["CLIMATE_OUTCOME_ARTIFACT"] = str(missing / "acquisition-batch-result.v2.json")
    env["CLIMATE_MANIFEST_ARTIFACT"] = str(missing / "web-listening-manifest.v1.json")
    env["CLIMATE_PILLAR_B_ARTIFACT"] = str(missing / "pillar-b.json")
    staging = tmp_path / "staging"
    result = _call(CLI + [
        "--production-weekly",
        "--authoring-mode", "prepare",
        "--report-date", REPORT_DATE,
        "--acquisition-batch", env["CLIMATE_OUTCOME_ARTIFACT"],
        "--web-listening-manifest", env["CLIMATE_MANIFEST_ARTIFACT"],
        "--pillar-b-artifact", env["CLIMATE_PILLAR_B_ARTIFACT"],
        "--staging-dir", str(staging),
        "--state-dir", env["CLIMATE_STATE_DIR"],
        "--source-dir", env["CLIMATE_SOURCE_DIR"],
        "--wiki-dir", env["CLIMATE_WIKI_DIR"],
        "--source-config", env["CLIMATE_SOURCE_CONFIG"],
        "--run-config", env["CLIMATE_RUN_CONFIG"],
        "--site-scopes", env["CLIMATE_SITE_SCOPES"],
    ], env=env)
    assert result.returncode != 0
    sources = list((tmp_path / "source_dir").rglob("*"))
    state = list((tmp_path / "state_dir").rglob("*"))
    assert not [s for s in sources if s.is_file()]
    assert not [s for s in state if s.is_file()]
    assert not staging.exists() or not any(staging.iterdir())


# ---------------------------------------------------------------------------
# AC-2: deterministic 6-key stats mapping is enforced on the response
# ---------------------------------------------------------------------------


def test_ac2_stats_tamper_rejected_and_no_partial_commit(tmp_path, monkeypatch):
    """AC-2: finalize must reject a response whose stats do not match the
    request's deterministic 6-key shape; nothing may be written to
    sources/ or seen_urls.json when the response is tampered."""
    fixture = _build_57_record_outcome(tmp_path)
    env = _prepare_env(tmp_path, dry_run=True, fixture_dir=fixture)
    staging_dir = _run_prepare(tmp_path, env, fixture)
    response_path = _build_response_from_request(staging_dir, fixture, tampered=True)
    finalize = _call(CLI + [
        "--production-weekly",
        "--authoring-mode", "finalize",
        "--report-date", REPORT_DATE,
        "--staging-dir", str(staging_dir),
        "--authoring-response", str(response_path),
        "--article-evidence-loopback", "scripts.hermes_job:dry_run_unavailable_provider",
        "--no-update-seen-state",
        "--no-sync",
        "--json",
        "--state-dir", env["CLIMATE_STATE_DIR"],
        "--source-dir", env["CLIMATE_SOURCE_DIR"],
        "--wiki-dir", env["CLIMATE_WIKI_DIR"],
        "--source-config", env["CLIMATE_SOURCE_CONFIG"],
        "--run-config", env["CLIMATE_RUN_CONFIG"],
        "--site-scopes", env["CLIMATE_SITE_SCOPES"],
    ], env=env)
    assert finalize.returncode != 0
    assert not list((tmp_path / "source_dir").glob("climate-monitor-*.md"))
    assert not (tmp_path / "state_dir" / "seen_urls.json").exists()
    assert not (tmp_path / "state_dir" / "websites").exists() or not list((tmp_path / "state_dir" / "websites").rglob("*.pending-run.json"))


# ---------------------------------------------------------------------------
# AC-3: cross-run manifest + outcome binding is enforced
# ---------------------------------------------------------------------------


def test_ac3_cross_run_outcome_and_manifest_rejected(tmp_path):
    """AC-3: cross-run manifest + outcome binding must be enforced end-to-end.

    Mutating the manifest's run_id after prepare re-computes the bundle
    digest, but the public-artifact sha256 still binds to the original
    manifest. Finalize must fail closed because the staged bundle is no
    longer consistent with the on-disk public artifacts (a different
    upstream run has been smuggled in between prepare and finalize)."""
    fixture = _build_57_record_outcome(tmp_path)
    env = _prepare_env(tmp_path, dry_run=True, fixture_dir=fixture)
    staging_dir = _run_prepare(tmp_path, env, fixture)
    # Re-author the manifest with a different run_id while keeping its
    # filename and shape; the prepared bundle still points to the original
    # sha256, so finalize must reject the on-disk divergence.
    manifest_path = fixture / "web-listening-manifest.v1.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["run"]["run_id"] = "different-run-id"
    manifest_path.write_text(json.dumps(manifest))
    response_path = _build_response_from_request(staging_dir, fixture)
    finalize = _call(CLI + [
        "--production-weekly", "--authoring-mode", "finalize",
        "--report-date", REPORT_DATE, "--staging-dir", str(staging_dir),
        "--authoring-response", str(response_path),
        "--article-evidence-loopback", "scripts.hermes_job:dry_run_unavailable_provider",
        "--no-update-seen-state", "--no-sync", "--json",
        "--state-dir", tmp_path / "state_dir",
        "--source-dir", tmp_path / "source_dir",
        "--wiki-dir", tmp_path / "wiki_dir",
        "--source-config", str(ROOT / "monitoring" / "supranational_sources.yaml"),
        "--run-config", str(ROOT / "monitoring" / "run_config.yaml"),
        "--site-scopes", str(ROOT / "monitoring" / "site_scopes.yaml"),
    ], env=env)
    assert finalize.returncode != 0
    combined = (finalize.stdout + finalize.stderr).lower()
    assert "sha256" in combined or "digest" in combined or "identity" in combined
    assert not list((tmp_path / "source_dir").glob("climate-monitor-*.md"))


# ---------------------------------------------------------------------------
# AC-4: finalize with wrong response / request_sha mismatch fails closed
# ---------------------------------------------------------------------------


def test_ac4_finalize_rejects_response_mismatch(tmp_path):
    fixture = _build_57_record_outcome(tmp_path)
    env = _prepare_env(tmp_path, dry_run=True, fixture_dir=fixture)
    staging_dir = _run_prepare(tmp_path, env, fixture)
    # Build a response with the wrong request_sha.
    request = json.loads((staging_dir / "v2_authoring_request.json").read_text())
    stats = json.loads((staging_dir / "stats.json").read_text())
    evidence = json.loads((staging_dir / "article_evidence.json").read_text())
    response = _build_v2_response(stats, evidence["records"], request_sha="0" * 64)
    response_path = tmp_path / "wrong_response.json"
    response_path.write_text(json.dumps(response))
    finalize = _call(CLI + [
        "--production-weekly",
        "--authoring-mode", "finalize",
        "--report-date", REPORT_DATE,
        "--staging-dir", str(staging_dir),
        "--authoring-response", str(response_path),
        "--article-evidence-loopback", "scripts.hermes_job:dry_run_unavailable_provider",
        "--no-update-seen-state",
        "--no-sync",
        "--json",
        "--state-dir", env["CLIMATE_STATE_DIR"],
        "--source-dir", env["CLIMATE_SOURCE_DIR"],
        "--wiki-dir", env["CLIMATE_WIKI_DIR"],
        "--source-config", env["CLIMATE_SOURCE_CONFIG"],
        "--run-config", env["CLIMATE_RUN_CONFIG"],
        "--site-scopes", env["CLIMATE_SITE_SCOPES"],
    ], env=env)
    assert finalize.returncode != 0
    assert "request_sha" in (finalize.stdout + finalize.stderr).lower() or "mutated" in (finalize.stdout + finalize.stderr).lower()
    assert not list((tmp_path / "source_dir").glob("climate-monitor-*.md"))


# ---------------------------------------------------------------------------
# AC-5: same-date finalize retry does not double-commit
# ---------------------------------------------------------------------------


def test_ac5_same_date_retry_is_idempotent(tmp_path):
    fixture = _build_57_record_outcome(tmp_path)
    env = _prepare_env(tmp_path, dry_run=True, fixture_dir=fixture)
    staging_dir = _run_prepare(tmp_path, env, fixture)
    response_path = _build_response_from_request(staging_dir, fixture)
    finalize_args = [
        "--production-weekly", "--authoring-mode", "finalize",
        "--report-date", REPORT_DATE, "--staging-dir", str(staging_dir),
        "--authoring-response", str(response_path),
        "--article-evidence-loopback", "scripts.hermes_job:dry_run_unavailable_provider",
        "--no-sync", "--json",
        "--state-dir", env["CLIMATE_STATE_DIR"], "--source-dir", env["CLIMATE_SOURCE_DIR"],
        "--wiki-dir", env["CLIMATE_WIKI_DIR"], "--source-config", env["CLIMATE_SOURCE_CONFIG"],
        "--run-config", env["CLIMATE_RUN_CONFIG"], "--site-scopes", env["CLIMATE_SITE_SCOPES"],
    ]
    first = _call(CLI + finalize_args, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    first_report = list((tmp_path / "source_dir").glob("climate-monitor-*.md"))
    assert first_report
    first_sha = _sha256(first_report[0])
    second = _call(CLI + finalize_args, env=env)
    assert second.returncode == 0, second.stdout + second.stderr
    second_report = list((tmp_path / "source_dir").glob("climate-monitor-*.md"))
    assert second_report
    assert _sha256(second_report[0]) == first_sha


# ---------------------------------------------------------------------------
# AC-6: production refuses fixture paths
# ---------------------------------------------------------------------------


def test_ac6_production_rejects_fixture_disguised_as_response(tmp_path):
    fixture = FIXTURE_DIR
    env = _prepare_env(tmp_path, dry_run=False)
    env["AUTHORING_RESPONSE"] = str(fixture / "57_record_fixture.json")
    env["ARTICLE_EVIDENCE"] = str(fixture / "57_article_evidence.json")
    env["CLIMATE_STATS_PATH"] = str(fixture / "57_stats.json")
    result = _call(CLI + [
        "--production-weekly", "--authoring-mode", "finalize",
        "--report-date", REPORT_DATE, "--staging-dir", str(tmp_path / "staging"),
        "--authoring-response", env["AUTHORING_RESPONSE"],
    ], env=env)
    assert result.returncode != 0
    assert "fixture" in (result.stdout + result.stderr).lower() or "production" in (result.stdout + result.stderr).lower()


# ---------------------------------------------------------------------------
# AC-7: end-to-end dry-run with a real same-run chain
# ---------------------------------------------------------------------------


def test_ac7_full_dry_run_chain_writes_report_and_no_seen(tmp_path):
    fixture = _build_57_record_outcome(tmp_path)
    env = _prepare_env(tmp_path, dry_run=True, fixture_dir=fixture)
    staging_dir = _run_prepare(tmp_path, env, fixture)
    response_path = _build_response_from_request(staging_dir, fixture)
    finalize = _call(CLI + [
        "--production-weekly", "--authoring-mode", "finalize",
        "--report-date", REPORT_DATE, "--staging-dir", str(staging_dir),
        "--authoring-response", str(response_path),
        "--article-evidence-loopback", "scripts.hermes_job:dry_run_unavailable_provider",
        "--no-update-seen-state", "--no-sync", "--json",
        "--state-dir", env["CLIMATE_STATE_DIR"], "--source-dir", env["CLIMATE_SOURCE_DIR"],
        "--wiki-dir", env["CLIMATE_WIKI_DIR"], "--source-config", env["CLIMATE_SOURCE_CONFIG"],
        "--run-config", env["CLIMATE_RUN_CONFIG"], "--site-scopes", env["CLIMATE_SITE_SCOPES"],
    ], env=env)
    assert finalize.returncode == 0, finalize.stdout + finalize.stderr
    report = list((tmp_path / "source_dir").glob("climate-monitor-*.md"))
    assert report
    sidecar = report[0].with_name(report[0].stem + ".semantics.json")
    assert sidecar.is_file(), f"expected {sidecar} to exist"
    assert not (tmp_path / "state_dir" / "seen_urls.json").exists()
    parsed = json.loads(finalize.stdout)
    assert parsed["report_path"] == report[0].name
    assert parsed["report_sha256"] == hashlib.sha256(report[0].read_bytes()).hexdigest()
    assert parsed["stats"]["total"] == 57


# ---------------------------------------------------------------------------
# helper builders
# ---------------------------------------------------------------------------


def _build_57_record_outcome(workspace: Path) -> Path:
    """Materialise one public outcome (acquisition-batch-result.v2) and one
    public manifest (web-listening-manifest.v1) under the workspace. The two
    artifacts bind scope-run-2 to export run-2 / parent 2. The saved public
    builder output counts 57 sources (33/9/14/1/0); the manifest holds 42
    discovered articles from one retained source."""
    out_dir = workspace / "public"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = "run-2"
    source_id = "iais-batch"
    manifest_items = []
    pillar_b_records = []
    counts = {"updated": 0, "unchanged": 0, "blocked": 0, "failed": 0}
    for i in range(57):
        disposition = (
            "updated" if counts["updated"] < 33 else
            "unchanged" if counts["unchanged"] < 9 else
            "blocked" if counts["blocked"] < 14 else "failed"
        )
        counts[disposition] += 1
        url = f"https://www.iais.org/updates/{i:03d}"
        title = f"IAIS climate insurance supervision update {i:03d}"
        manifest_items.append({
            "item_id": f"iais-{i:03d}",
            "item_type": "page",
            "url": url,
            "final_url": url,
            "title": title,
            "summary": f"IAIS published a climate supervision update {i:03d}.",
            "status": "new" if disposition == "updated" else disposition,
            "observed_at": "2026-09-07T08:00:00Z",
            "summary_basis": "page" if disposition == "updated" else
                            "search_result" if disposition == "unchanged" else "upstream_artifact",
            "title_basis": "upstream_artifact",
            "display_pillar": "A" if i % 2 == 0 else "B",
            "origins": [{"pillar": "A" if i % 2 == 0 else "B", "source": "iais",
                         "url": url, "discovered_at": "2026-09-07T08:00:00Z"}],
            "content_hash": hashlib.sha256(url.encode()).hexdigest(),
        })
        if i % 2 == 1:
            pillar_b_records.append({"url": url, "title": title,
                "source": "web", "summary": f"IAIS published climate insurance update {i:03d}."})
    manifest = {
        "schema_version": "web-listening-manifest.v1",
        "manifest_id": "manifest-iais-batch-2",
        "source": {"source_id": source_id, "site_name": "IAIS",
                   "tree_seed_url": "https://www.iais.org/updates/000"},
        "run": {"run_id": run_id, "parent_run_id": "2", "started_at": "2026-09-07T08:00:00Z",
                "finished_at": "2026-09-07T08:05:00Z", "outcome_source": "climate-monitor"},
        "discovered_items": manifest_items,
    }
    outcome = json.loads((FIXTURE_DIR / "acquisition_batch_result.v2.57.json").read_text())
    # Forty-two discovered articles belong to this successful source; source
    # failures elsewhere in the batch do not create article records.
    manifest["discovered_items"] = manifest_items[:42]
    (out_dir / "acquisition-batch-result.v2.json").write_text(json.dumps(outcome))
    (out_dir / "web-listening-manifest.v1.json").write_text(json.dumps(manifest))
    (out_dir / "pillar-b.json").write_text(json.dumps(discovery_fixture(pillar_b_records)))
    return out_dir


def _run_prepare(workspace: Path, env: dict, fixture: Path) -> Path:
    staging_dir = workspace / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    env["CLIMATE_OUTCOME_ARTIFACT"] = str(fixture / "acquisition-batch-result.v2.json")
    env["CLIMATE_MANIFEST_ARTIFACT"] = str(fixture / "web-listening-manifest.v1.json")
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
    bundle_path = staging_dir / "bundle.json"
    assert bundle_path.is_file()
    return staging_dir


def _build_response_from_request(staging_dir: Path, fixture: Path, *, tampered: bool = False) -> Path:
    request = json.loads((staging_dir / "v2_authoring_request.json").read_text())
    stats = json.loads((staging_dir / "stats.json").read_text())
    evidence = json.loads((staging_dir / "article_evidence.json").read_text())
    if tampered:
        stats = dict(stats)
        stats["total"] += 1
    response = _build_v2_response(stats, evidence["records"], request_sha=request["request_sha256"])
    response_path = staging_dir / "authoring_response.json"
    response_path.write_text(json.dumps(response))
    return response_path


def _build_v2_response(stats: dict, records: list[dict], *, request_sha: str) -> dict:
    sys.path.insert(0, str(ROOT))
    from climate_monitor.dedupe import canonical_url
    from climate_monitor.weekly_monitor.driver import _candidate_items_from_evidence
    from climate_monitor.weekly_monitor.prompt_loader import load_weekly_monitor_prompt
    from climate_monitor.weekly_monitor.authoring_contract import build_authoring_request
    items = _candidate_items_from_evidence(None, {"records": records})
    request = build_authoring_request(
        report_date=date.fromisoformat(REPORT_DATE),
        items=items, prompt=load_weekly_monitor_prompt(),
        article_evidence={"records": records}, stats=stats,
    )
    evidence_by_canonical: dict[str, dict] = {}
    for record in records:
        canonical = canonical_url(record.get("final_url") or record.get("requested_url") or "")
        if canonical:
            evidence_by_canonical[canonical] = record
    response_articles = []
    # The response covers every candidate without an article cap. Emit one response
    # article per request article, in order, so the validator's
    # article_count == kept_ids invariant holds for the dry-run shape.
    for article in request["articles"]:
        canonical = canonical_url(article.get("url") or "")
        record = evidence_by_canonical.get(canonical)
        if record is None:
            continue
        article = json.loads(json.dumps(article))
        article.update({
            "relevant": True,
            "summary": "Honest content summary.",
            "summary_basis": record.get("summary_basis", "page"),
            "evidence_hash": record.get("content_hash"),
            "categories": ["Supervision & Disclosure"],
            "keywords": ["climate", "insurance", "capital", "supervision"],
            "title_basis": record.get("title_basis", "upstream_artifact"),
            "display_pillar": record.get("display_pillar", "A"),
            "origins": record.get("origins", []),
        })
        response_articles.append(article)
    # The orchestrator's response validation requires ``article_count ==
    # len(kept_ids)`` where kept_ids is the full set of canonical URLs in
    # the article-evidence.v1 envelope (no max_items_per_report truncation
    # at the validator boundary). The full chain owns the report-size cap;
    # the authoring response must mirror every kept article.
    return {
        "schema_version": "weekly-monitor-authoring-response.v2",
        "contract_version": "weekly-monitor-authoring.v2",
        "request_sha256": request_sha,
        "article_count": len(response_articles),
        "articles": response_articles,
        "executive_summary": (
            f"{stats['total']} total; {stats['updated']} updated; "
            f"{stats['unchanged']} unchanged; {stats['blocked']} blocked; "
            f"{stats['failed']} failed; {stats['unresolved']} unresolved."
        ),
        "stats": stats,
    }
