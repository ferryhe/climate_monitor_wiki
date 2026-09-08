"""All-site inputs preserve public per-scope identity through the monitor."""
import copy
import json
from pathlib import Path

import pytest

from scripts import run_climate_monitor as monitor
from climate_monitor.article_candidate_contract import adapt_article_changes
from climate_monitor.weekly_monitor.authoring_contract import _validate_v2_stats_shape
from pillar_b_fixture import discovery_fixture


def scope_pair(number, *, disposition="unchanged"):
    baseline = Path(__file__).parent / "fixtures/issue87/wri_repro/acquisition-batch-result.v2.json"
    outcome = json.loads(baseline.read_text())
    outcome["run_id"] = f"scope-run-{number}"
    item = outcome["dispositions"][0]
    item.update(task_id=f"wri-scope-{number}", requested_url=f"https://www.wri.org/seed-{number}",
                artifact_id=f"manifest-wri-{number}", disposition=disposition,
                reason=f"scope.{disposition}")
    counts = outcome["counts"]
    counts.update(updated=0, unchanged=0, blocked=0, failed=0, unresolved=0)
    counts[disposition] = 1
    counts["succeeded"] = int(disposition in {"updated", "unchanged"})
    counts["valid_snapshots"] = counts["succeeded"]
    counts["failed_evidence"] = int(disposition in {"blocked", "failed"})
    outcome["summary"].update(succeeded=counts["succeeded"], failed=1-counts["succeeded"])
    if disposition not in {"updated", "unchanged"}:
        item.pop("artifact_id")
        outcome.update(status="failed", full_success=False)
    manifest = {"schema_version": "web-listening-manifest.v1",
                "manifest_id": f"manifest-wri-{number}",
                "source": {"source_id": "wri", "tree_seed_url": item["requested_url"]},
                "run": {"run_id": f"run-{number}", "parent_run_id": str(number)},
                "discovered_items": [{"url": f"https://www.wri.org/climate-{number}",
                                      "title": f"Climate insurance {number}",
                                      "summary": "Climate insurance disclosure affects capital supervision."}]}
    return outcome, manifest


def test_collection_binds_two_scopes_of_one_site_without_last_record_wins():
    pairs = [scope_pair(1, disposition="updated"), scope_pair(2)]
    outcomes, manifests = map(list, zip(*pairs))
    monitor._verify_collection_identity(outcomes, manifests)
    combined = {"dispositions": [r["dispositions"][0] for r in outcomes]}
    records = monitor._attach_outcome_disposition(
        monitor._collect_same_run_records(combined, manifests), combined)
    assert [(r["title"], r["disposition"]) for r in records] == [
        ("Climate insurance 1", "updated"), ("Climate insurance 2", "unchanged")]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra", "parent", "seed"])
def test_collection_rejects_incomplete_or_cross_run_exports(mutation):
    pairs = [scope_pair(1), scope_pair(2)]
    outcomes, manifests = map(list, zip(*pairs))
    if mutation == "missing":
        manifests.pop()
    elif mutation == "duplicate":
        manifests.append(copy.deepcopy(manifests[0]))
    elif mutation == "extra":
        manifests.append(scope_pair(3)[1])
    elif mutation == "parent":
        manifests[1]["run"].update(parent_run_id="1", run_id="run-1")
    else:
        manifests[1]["source"]["tree_seed_url"] = manifests[0]["source"]["tree_seed_url"]
    with pytest.raises(SystemExit, match="identity"):
        monitor._verify_collection_identity(outcomes, manifests)


def test_terminal_batch_needs_no_placeholder_article():
    outcome, _ = scope_pair(1, disposition="blocked")
    monitor._verify_collection_identity([outcome], [])
    article_changes = monitor._outcome_to_article_changes(outcome, [], [], "2026-09-07")
    assert article_changes["articles"] == []
    assert adapt_article_changes(article_changes, artifact_id="outcomes.json",
                                 artifact_sha256="a" * 64) == []
    assert _validate_v2_stats_shape(monitor._derive_stats([], outcome))["blocked"] == 1


@pytest.mark.parametrize("empty", [False, True])
def test_public_collection_prepare_and_finalize(tmp_path, empty):
    contract = pytest.importorskip("web_listening.contracts.acquisition_batch")
    from test_issue87_live_chain import (
        CLI, REPORT_DATE, _prepare_env, _call, _build_response_from_request)
    from test_run_climate_monitor_stats import _run_prepare_only

    pairs = [scope_pair(1, disposition="updated"), scope_pair(2)] if not empty else []
    outcomes = [item[0] for item in pairs]
    # The public builder supplies failed/unresolved status and count semantics.
    for number, disposition in [(3, "blocked"), (4, "unresolved")]:
        raw, _ = scope_pair(number, disposition=disposition)
        outcomes.append(contract.build_acquisition_batch_result_v2(
            raw["dispositions"], run_id=raw["run_id"],
            authoritative_status="partial" if disposition == "unresolved" else "completed",
            failed_evidence=int(disposition == "blocked")))
    fixture = tmp_path / "inputs"
    fixture.mkdir()
    for filename, payload in [
        ("acquisition-batch-result.v2.json", outcomes),
        ("web-listening-manifest.v1.json", [item[1] for item in pairs]),
        ("pillar-b.json", discovery_fixture()),
    ]:
        (fixture / filename).write_text(json.dumps(payload))
    env = _prepare_env(tmp_path, dry_run=True, fixture_dir=fixture)
    # Request an explicit coverage-only report when the offline reader has no
    # article evidence; the library otherwise correctly returns no report.
    import yaml
    config = yaml.safe_load(Path(env["CLIMATE_RUN_CONFIG"]).read_text())
    config["output"]["write_empty_report"] = True
    config_path = tmp_path / "run-config.yaml"
    config_path.write_text(yaml.safe_dump(config))
    env["CLIMATE_RUN_CONFIG"] = str(config_path)
    staging = _run_prepare_only(tmp_path, env, fixture)
    stats = json.loads((staging / "stats.json").read_text())
    assert stats == dict(total=2 if empty else 4, updated=0 if empty else 1,
                        unchanged=0 if empty else 1, blocked=1, failed=0, unresolved=1)
    response = _build_response_from_request(staging, fixture)
    result = _call(CLI + [
        "--production-weekly", "--authoring-mode", "finalize", "--report-date", REPORT_DATE,
        "--staging-dir", str(staging), "--authoring-response", str(response),
        "--article-evidence-loopback", "scripts.hermes_job:dry_run_unavailable_provider",
        "--no-sync", "--no-update-seen-state", "--json",
        "--state-dir", env["CLIMATE_STATE_DIR"], "--source-dir", env["CLIMATE_SOURCE_DIR"],
        "--wiki-dir", env["CLIMATE_WIKI_DIR"], "--source-config", env["CLIMATE_SOURCE_CONFIG"],
        "--run-config", env["CLIMATE_RUN_CONFIG"], "--site-scopes", env["CLIMATE_SITE_SCOPES"],
    ], env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "source_dir" / f"climate-monitor-{REPORT_DATE}.md").is_file()
