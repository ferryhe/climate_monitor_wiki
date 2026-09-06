"""Downstream file-only consumer of the canonical manifest → report → evidence run."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

from climate_monitor.article_content_adapter import ARTICLE_EVIDENCE_SCHEMA
from climate_monitor.web_listening_adapter import read_manifest_items

FIXTURE = Path(__file__).parent / "fixtures/article_content/manifests/climate_92_v2.json"


def test_formal_producer_fixture_round_trip():
    before = FIXTURE.read_bytes()
    payload = json.loads(before)
    assert payload["schema_version"] == "web-listening-manifest.v1"
    assert len(payload["discovered_items"]) == 6
    items = read_manifest_items(FIXTURE)
    assert [i.source_item_id for i in items] == [
        "climate-92-html", "climate-92-no-pdf", "climate-92-increment", "climate-92-waiting"]
    assert all(i.source_name == "Climate 92 fixture" for i in items)
    assert all(i.lane == "website" for i in items)
    raw = {i["item_id"]: i for i in payload["discovered_items"]}
    assert raw["climate-92-bootstrap"]["metadata"]["prior_snapshot_id"] is None
    assert raw["climate-92-bootstrap"]["status"] == "existing"
    assert raw["climate-92-waiting"]["status"] == "new"
    assert raw["climate-92-removed"]["status"] == "removed"
    assert FIXTURE.read_bytes() == before


def test_issue93_reads_cli_artifact(tmp_path):
    """AC-4 end-to-end: ``scripts.run_climate_monitor`` (CLI) is now a thin
    wrapper. The orchestrator owns evidence staging as part of the #91
    transaction. The CLI keeps ``--article-evidence-loopback`` as a test/CI
    seam so consumers can verify the strict record contract without
    needing the upstream web_listening package installed.

    This subprocess smoke test confirms:
    1. CLI runs end-to-end with the loopback provider seam.
    2. Orchestrator stages the ``article-evidence.v1`` artifact for #93 to
       consume, with article_id/record_count/source_item_id/status/summary_basis
       all matching the producer manifest contract.
    3. The orchestrator surfaces the artifact path on stdout.
    """

    source_dir = tmp_path / "sources"
    completed = subprocess.run([
        sys.executable, "-m", "scripts.run_climate_monitor", "--manifest-fixture", str(FIXTURE),
        "--article-evidence-loopback",
        "tests.fixtures.article_content.providers:loopback_success_provider",
        "--date", "2026-09-07", "--source-dir", str(source_dir),
        "--wiki-dir", str(tmp_path / "wiki"), "--state-dir", str(tmp_path / "state"),
        "--no-sync", "--no-update-seen-state"], capture_output=True, text=True, check=True)
    artifact_path = source_dir / "article-evidence.v1_2026-09-07.json"
    assert artifact_path.exists()
    assert f"Article evidence: {artifact_path}" in completed.stdout
    artifact = json.loads(artifact_path.read_text())
    try:
        import jsonschema
    except ImportError:
        assert set(ARTICLE_EVIDENCE_SCHEMA["required"]) <= artifact.keys()
        for record in artifact["records"]:
            assert set(ARTICLE_EVIDENCE_SCHEMA["properties"]["records"]["items"]["required"]) <= record.keys()
    else:
        jsonschema.validate(artifact, ARTICLE_EVIDENCE_SCHEMA)
    assert artifact["record_count"] == len(artifact["records"]) == 4
    expected_items = {item.url: item for item in read_manifest_items(FIXTURE)}
    for record in artifact["records"]:
        item = expected_items.pop(record["requested_url"])
        # Loopback provider returns deterministic ``article_id == url``; the
        # orchestrator may also rewrite ``article_id`` to the canonical URL
        # digest. Both are acceptable as long as the requested URL is the
        # identity that drives downstream consumption.
        assert record["requested_url"] == item.url
        # Producer manifest's ``source_item_id`` is preserved end-to-end
        # via ``CandidateOrigin.metadata`` (PR #99 surfaced it from
        # ``read_manifest_items`` and the orchestrator passes it through).
        assert record["extra"]["source_item_id"] == item.source_item_id
        assert record["extra"]["source_name"] == item.source_name
        # ``item_status`` is propagated from the manifest's
        # ``discovered_items[i].status`` field for the matching URL.
        assert record["extra"].get("item_status") in ("new", "updated")
        # With the success loopback provider, all records are ``status=ok``
        # with summary_basis=page.
        assert record["status"] == "ok"
        assert record["summary_basis"] == "page"
        assert record["content"] is not None
        assert hashlib.sha256(record["content"].encode()).hexdigest() == record["content_hash"]
    assert not expected_items
    assert (source_dir / "climate-monitor-2026-09-07.md").exists()
    assert not (tmp_path / "state").exists()
    # The existing orchestrator creates an empty wiki directory even with --no-sync.
    assert not list((tmp_path / "wiki").rglob("*"))
