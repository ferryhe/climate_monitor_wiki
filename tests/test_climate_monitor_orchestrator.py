from __future__ import annotations

from datetime import date
from textwrap import dedent

import json

from climate_monitor.models import CandidateItem, MonitorRunResult
from climate_monitor.orchestrator import run_monitor


def test_run_monitor_writes_source_report_and_syncs_wiki(tmp_path):
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    manifest = tmp_path / "manifest.json"
    research = tmp_path / "research.json"
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"
    source_config.write_text(
        dedent(
            """
            sources:
              - key: iais
                abbreviation: IAIS
                full_name: International Association of Insurance Supervisors
                url: https://www.iais.org/
                high_priority: true
                tags: [insurance, climate]
            """
        ).strip(),
        encoding="utf-8",
    )
    run_config.write_text(
        f"""
report_title: Daily Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate, flood, wildfire]
actuarial_keywords: [insurance, supervision, capital]
research_lane:
  lookback_days: 30
  queries: [climate insurance report]
output:
  source_dir: {source_dir.as_posix()}
  wiki_dir: {wiki_dir.as_posix()}
  write_empty_report: false
dedupe:
  url_tracking_path: {tmp_path.as_posix()}/state/seen_urls.json
  title_tracking_path: {tmp_path.as_posix()}/state/seen_titles.json
""".strip(),
        encoding="utf-8",
    )
    manifest.write_text(
        """
{
  "schema_version": "web-listening-manifest.v1",
  "source": {"source_id": "iais", "site_name": "IAIS"},
  "discovered_items": [
    {
      "item_id": "1",
      "item_type": "page",
      "url": "https://www.iais.org/climate-supervision",
      "title": "Climate supervision update",
      "summary": "Insurance supervisors discuss climate risk.",
      "status": "new",
      "observed_at": "2026-05-14T00:00:00Z"
    }
  ],
  "downloaded_assets": []
}
""".strip(),
        encoding="utf-8",
    )
    research.write_text(
        """
[
  {
    "title": "Climate risk and insurance capital report",
    "url": "https://example.org/report",
    "summary": "A report about climate risk and insurance capital.",
    "source_name": "Example Research",
    "published": "2026-05-01"
  }
]
""".strip(),
        encoding="utf-8",
    )

    result = run_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 14),
        manifest_fixture_path=manifest,
        research_fixture_path=research,
        state_dir=tmp_path / "state",
        sync=True,
    )

    assert result.report_path is not None
    report_text = (source_dir / "climate-monitor-2026-05-14.md").read_text(encoding="utf-8")
    assert "Climate supervision update" in report_text
    assert "Climate risk and insurance capital report" in report_text
    wiki_text = (wiki_dir / "climate-monitor-2026-05-14.md").read_text(encoding="utf-8")
    assert "Source: [[sources/climate-monitor-2026-05-14]]" in wiki_text
    assert result.synced is True


def test_run_monitor_skips_empty_report_when_no_relevant_items(tmp_path):
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    manifest = tmp_path / "manifest.json"
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"

    source_config.write_text(
        "sources:\n  - key: wto\n    abbreviation: WTO\n    full_name: World Trade Organization\n    url: https://www.wto.org/\n",
        encoding="utf-8",
    )
    run_config.write_text(
        f"""
report_title: Daily Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate]
actuarial_keywords: [insurance]
research_lane:
  lookback_days: 30
  queries: []
output:
  source_dir: {source_dir.as_posix()}
  wiki_dir: {wiki_dir.as_posix()}
  write_empty_report: false
""".strip(),
        encoding="utf-8",
    )
    manifest.write_text(
        '{"source":{"site_name":"WTO"},"discovered_items":[{"url":"https://www.wto.org/trade","title":"Tariff note"}]}',
        encoding="utf-8",
    )

    result = run_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        report_date=date(2026, 5, 14),
        manifest_fixture_path=manifest,
        state_dir=tmp_path / "state",
        sync=False,
    )

    assert result.report_path is None
    assert not source_dir.exists()


def test_run_monitor_fails_when_live_collection_fails_for_every_source(tmp_path, monkeypatch):
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    source_config.write_text(
        "sources:\n  - key: bad\n    abbreviation: BAD\n    full_name: Bad Source\n    url: https://bad.example/\n",
        encoding="utf-8",
    )
    run_config.write_text(
        """
report_title: Daily Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate]
actuarial_keywords: [insurance]
research_lane:
  lookback_days: 30
  queries: []
output:
  source_dir: sources
  wiki_dir: wiki
  write_empty_report: false
""".strip(),
        encoding="utf-8",
    )

    def fake_collect(sources, *, state_dir, manifest_fixture_path=None, site_scopes=None):
        return [], ["bad: network failure"]

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr("climate_monitor.orchestrator.collect_website_items", fake_collect)

    try:
        run_monitor(
            source_config_path=source_config,
            run_config_path=run_config,
            state_dir=tmp_path / "state",
            sync=False,
        )
    except RuntimeError as exc:
        assert "failed for every configured source" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError")


def test_run_monitor_keeps_partial_seed_warnings_from_failing_live_run(tmp_path, monkeypatch):
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    source_dir = tmp_path / "sources"
    wiki_dir = tmp_path / "wiki"
    source_config.write_text(
        "sources:\n  - key: iais\n    abbreviation: IAIS\n    full_name: IAIS\n    url: https://www.iais.org/\n",
        encoding="utf-8",
    )
    run_config.write_text(
        f"""
report_title: Daily Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate]
actuarial_keywords: [insurance]
research_lane:
  lookback_days: 30
  queries: []
output:
  source_dir: {source_dir.as_posix()}
  wiki_dir: {wiki_dir.as_posix()}
  write_empty_report: false
""".strip(),
        encoding="utf-8",
    )

    def fake_collect(sources, *, state_dir, manifest_fixture_path=None, site_scopes=None):
        return [
            CandidateItem(
                title="Climate risk update",
                url="https://www.iais.org/climate-risk/",
                summary="IAIS published a climate risk update.",
                source_name="IAIS",
                lane="website",
            )
        ], ["iais seed https://www.iais.org/broken/: timeout"]

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr("climate_monitor.orchestrator.collect_website_items", fake_collect)

    result = run_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        state_dir=tmp_path / "state",
        sync=False,
    )

    assert result.report_path is not None
    assert result.warnings == ("iais seed https://www.iais.org/broken/: timeout",)


def test_run_monitor_passes_site_scopes_to_live_collection(tmp_path, monkeypatch):
    source_config = tmp_path / "sources.yaml"
    run_config = tmp_path / "run_config.yaml"
    scopes_config = tmp_path / "site_scopes.yaml"
    source_config.write_text(
        "sources:\n  - key: iais\n    abbreviation: IAIS\n    full_name: IAIS\n    url: https://www.iais.org/\n",
        encoding="utf-8",
    )
    run_config.write_text(
        """
report_title: Daily Climate & Actuarial Monitor
max_items_per_report: 12
climate_keywords: [climate]
actuarial_keywords: [insurance]
research_lane:
  lookback_days: 30
  queries: []
output:
  source_dir: sources
  wiki_dir: wiki
  write_empty_report: false
""".strip(),
        encoding="utf-8",
    )
    scopes_config.write_text(
        """
site_scopes:
  - source_key: iais
    seed_urls:
      - https://www.iais.org/news/
    include_patterns:
      - /climate/
    exclude_patterns: []
""".strip(),
        encoding="utf-8",
    )
    seen = {}

    def fake_collect(sources, *, state_dir, manifest_fixture_path=None, site_scopes=None):
        seen["site_scopes"] = site_scopes
        return [], []

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr("climate_monitor.orchestrator.collect_website_items", fake_collect)

    run_monitor(
        source_config_path=source_config,
        run_config_path=run_config,
        site_scopes_path=scopes_config,
        state_dir=tmp_path / "state",
        sync=False,
    )

    assert seen["site_scopes"]["iais"].seed_urls == ("https://www.iais.org/news/",)


def test_monitor_run_result_serializes_stable_json_with_safe_item_fields():
    item = CandidateItem(
        title="Climate supervision update",
        url="https://www.iais.org/climate-supervision",
        summary="Insurance supervisors discuss climate risk.",
        source_name="IAIS",
        lane="website",
        published="2026-05-01",
        detected_at="2026-05-14T00:00:00Z",
        content_hash="abc123",
        evidence_text="OPENAI_API_KEY=sk-test-secret",
        climate_related=True,
        actuarial_related=True,
        relevance_reason="Climate and insurance terms matched.",
        climate_signal="physical_risk",
        actuarial_signal="insurance_risk",
        confidence=0.82,
        topics=("climate", "insurance"),
    )
    object.__setattr__(item, "document_secret", "sk-test-secret")
    object.__setattr__(item, "source_item_id", "file-1")
    object.__setattr__(item, "asset_id", "sha256-abc123")
    object.__setattr__(item, "asset_local_path", "C:\\Users\\ferry\\Downloads\\secret.pdf")
    object.__setattr__(item, "asset_tracked_path", "data/downloads/_tracked/iais/climate-report.pdf")
    object.__setattr__(item, "asset_media_type", "application/pdf")
    result = MonitorRunResult(
        report_date=date(2026, 5, 14),
        report_path="sources/climate-monitor-2026-05-14.md",
        items=(item,),
        dedup_notes=("Duplicate title - skipped",),
        warnings=("iais seed timeout",),
        synced=True,
    )

    payload = json.loads(result.to_json())

    assert list(payload) == [
        "report_date",
        "report_path",
        "synced",
        "item_count",
        "items",
        "dedup_notes",
        "warnings",
    ]
    assert payload["report_date"] == "2026-05-14"
    assert payload["report_path"] == "sources/climate-monitor-2026-05-14.md"
    assert payload["synced"] is True
    assert payload["item_count"] == 1
    assert payload["dedup_notes"] == ["Duplicate title - skipped"]
    assert payload["warnings"] == ["iais seed timeout"]
    assert payload["items"] == [
        {
            "lane": "website",
            "source": "IAIS",
            "title": "Climate supervision update",
            "url": "https://www.iais.org/climate-supervision",
            "summary": "Insurance supervisors discuss climate risk.",
            "published": "2026-05-01",
            "detected": "2026-05-14T00:00:00Z",
            "content_hash": "abc123",
            "relevance": {
                "reason": "Climate and insurance terms matched.",
                "confidence": 0.82,
            },
            "climate": {
                "related": True,
                "signal": "physical_risk",
            },
            "actuarial": {
                "related": True,
                "signal": "insurance_risk",
            },
            "topics": ["climate", "insurance"],
        }
    ]
    assert "asset_id" not in payload["items"][0]
    assert "asset_local_path" not in payload["items"][0]
    assert "C:\\Users\\ferry" not in result.to_json()
    assert "OPENAI_API_KEY" not in result.to_json()
    assert "sk-test-secret" not in result.to_json()
