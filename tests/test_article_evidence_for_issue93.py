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
    source_dir = tmp_path / "sources"
    completed = subprocess.run([
        sys.executable, "-m", "scripts.run_climate_monitor", "--manifest-fixture", str(FIXTURE),
        "--article-evidence-loopback", "tests.fixtures.article_content.providers:loopback_success_provider",
        "--date", "2026-09-07", "--source-dir", str(source_dir),
        "--wiki-dir", str(tmp_path / "wiki"), "--state-dir", str(tmp_path / "state"),
        "--no-sync", "--no-update-seen-state"], capture_output=True, text=True, check=True)
    artifact_path = source_dir / "article-evidence.v1_2026-09-07.json"
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
        assert record["article_id"] == item.url
        assert record["extra"]["source_item_id"] == item.source_item_id
        assert record["extra"]["source_name"] == item.source_name
        assert record["extra"]["source_id"] == "climate-92"
        assert record["extra"]["run_id"] == "run-92-6"
        assert record["extra"]["item_status"] == ("updated" if item.source_item_id.endswith("increment") else "new")
        assert record["status"] == "ok"
        assert record["summary_basis"] == "page"
        assert hashlib.sha256(record["content"].encode()).hexdigest() == record["content_hash"]
    assert not expected_items
    assert (source_dir / "climate-monitor-2026-09-07.md").exists()
    assert not (tmp_path / "state").exists()
    # The existing orchestrator creates an empty wiki directory even with --no-sync.
    assert not list((tmp_path / "wiki").rglob("*"))
