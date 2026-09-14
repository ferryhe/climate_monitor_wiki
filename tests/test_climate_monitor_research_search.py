from __future__ import annotations

import json
from datetime import date

import pytest

from climate_monitor.ai_filter import classify_candidate
from climate_monitor import web_listening_adapter
from climate_monitor.models import CandidateItem, MonitorSource, RunConfig, SiteScope
from climate_monitor.research_search import (
    DEFAULT_SEARCH_MODEL,
    filter_recent_items,
    parse_openai_research_payload,
    read_research_fixture,
    search_recent_research,
)
from climate_monitor.web_listening_adapter import (
    collect_source_items,
    collect_website_items,
    commit_staged_source_checkpoints,
    read_manifest_items,
)


def _config() -> RunConfig:
    return RunConfig(
        report_title="Daily Climate & Actuarial Monitor",
        climate_keywords=("climate", "flood", "wildfire", "adaptation"),
        actuarial_keywords=("insurance", "capital", "supervision"),
        research_queries=("climate insurance report",),
        research_lookback_days=30,
        source_dir="sources",
        wiki_dir="wiki",
        write_empty_report=False,
    )


def test_parse_openai_research_payload_returns_candidate_items():
    payload = {
        "items": [
            {
                "title": "Climate risk capital report",
                "url": "https://example.org/capital",
                "summary": "Report on climate risk and insurance capital.",
                "source_name": "Example",
                "published": "2026-05-01",
            }
        ]
    }

    items = parse_openai_research_payload(payload)

    assert len(items) == 1
    assert items[0].lane == "research"
    assert items[0].title == "Climate risk capital report"
    assert items[0].source_name == "Example"
    assert items[0].published == "2026-05-01"


def test_read_research_fixture_supports_list_and_structured_payloads(tmp_path):
    list_fixture = tmp_path / "research-list.json"
    list_fixture.write_text(
        json.dumps(
            [
                {
                    "title": "Climate insurance outlook",
                    "url": "https://example.org/outlook",
                    "summary": "Insurance report on climate risk.",
                }
            ]
        ),
        encoding="utf-8",
    )
    structured_fixture = tmp_path / "research-structured.json"
    structured_fixture.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "title": "Flood capital study",
                        "url": "https://example.org/flood",
                        "summary": "Flood and capital analysis.",
                        "source_name": "Example Research",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    list_items = read_research_fixture(list_fixture)
    structured_items = read_research_fixture(structured_fixture)

    assert list_items[0].source_name == "Research search"
    assert structured_items[0].title == "Flood capital study"
    assert structured_items[0].lane == "research"


def test_search_recent_research_is_guarded_without_fixture_or_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CLIMATE_MONITOR_ENABLE_LIVE_RESEARCH", raising=False)

    assert search_recent_research(_config()) == []


def test_search_recent_research_uses_injected_openai_client_when_key_is_set(monkeypatch):
    class Parsed:
        def model_dump(self):
            return {
                "items": [
                        {
                            "title": "Wildfire insurance paper",
                            "url": "https://example.org/wildfire",
                            "summary": "Wildfire insurance research.",
                            "published": "2026-05-01",
                        }
                ]
            }

    class Responses:
        def __init__(self):
            self.kwargs = None

        def parse(self, **kwargs):
            self.kwargs = kwargs
            return type("Response", (), {"output_parsed": Parsed()})()

    class Client:
        def __init__(self):
            self.responses = Responses()

    client = Client()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_RESEARCH", "1")
    monkeypatch.delenv("CLIMATE_MONITOR_SEARCH_MODEL", raising=False)

    items = search_recent_research(
        _config(), openai_client=client, today=date(2026, 5, 14)
    )

    assert items[0].title == "Wildfire insurance paper"
    assert client.responses.kwargs["model"] == DEFAULT_SEARCH_MODEL
    assert client.responses.kwargs["tools"] == [{"type": "web_search"}]
    assert client.responses.kwargs["text_format"].__name__ == "ResearchSearchPayload"


def test_read_manifest_items_converts_discovered_items_to_website_candidates(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "web-listening-manifest.v1",
                "source": {"source_id": "iais", "site_name": "IAIS"},
                "discovered_items": [
                    {
                        "item_id": "1",
                        "item_type": "page",
                        "url": "https://www.iais.org/climate-supervision",
                        "title": "Climate supervision update",
                        "status": "new",
                        "observed_at": "2026-05-14T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    items = read_manifest_items(manifest)

    assert len(items) == 1
    assert items[0].lane == "website"
    assert items[0].source_name == "IAIS"
    assert items[0].detected_at == "2026-05-14T00:00:00Z"


def test_read_manifest_items_ignores_manifest_entries_without_actionable_status(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "source": {"site_name": "IAIS"},
                "discovered_items": [
                    {
                        "item_id": "old",
                        "item_type": "page",
                        "url": "https://www.iais.org/old-climate",
                        "title": "Old climate page",
                        "status": "unchanged",
                    },
                    {
                        "item_id": "skipped",
                        "item_type": "page",
                        "url": "https://www.iais.org/skipped-climate",
                        "title": "Skipped climate page",
                        "status": "skipped",
                    },
                    {
                        "item_id": "new",
                        "item_type": "page",
                        "url": "https://www.iais.org/new-climate",
                        "title": "New climate page",
                        "status": "new",
                    },
                    {
                        "item_id": "changed",
                        "item_type": "page",
                        "url": "https://www.iais.org/changed-climate",
                        "title": "Changed climate page",
                        "status": "changed",
                    },
                    {
                        "item_id": "legacy",
                        "item_type": "page",
                        "url": "https://www.iais.org/legacy-climate",
                        "title": "Legacy climate page",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    items = read_manifest_items(manifest)

    assert [item.title for item in items] == [
        "New climate page",
        "Changed climate page",
        "Legacy climate page",
    ]


def test_read_manifest_items_maps_file_links_to_document_candidates_with_asset_metadata(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "web-listening-manifest.v1",
                "source": {"source_id": "iais", "site_name": "IAIS"},
                "discovered_items": [
                    {
                        "item_id": "page-1",
                        "item_type": "page",
                        "url": "https://www.iais.org/climate-supervision",
                        "title": "Climate supervision update",
                        "summary": "Website update summary.",
                        "status": "new",
                        "observed_at": "2026-05-14T00:00:00Z",
                    },
                    {
                        "item_id": "file-1",
                        "item_type": "file_link",
                        "url": "https://www.iais.org/uploads/climate-report.pdf",
                        "title": "Climate report PDF",
                        "summary": "Insurance supervisors discuss climate reporting.",
                        "status": "new",
                        "observed_at": "2026-05-14T00:01:00Z",
                        "content_type": "application/pdf",
                    },
                ],
                "downloaded_assets": [
                    {
                        "asset_id": "sha256-abc123",
                        "source_item_id": "file-1",
                        "url": "https://www.iais.org/uploads/climate-report.pdf",
                        "local_path": "data/downloads/_tracked/iais/climate-report.pdf",
                        "canonical_blob_path": "data/downloads/_blobs/ab/abc123.pdf",
                        "tracked_path": "data/downloads/_tracked/iais/climate-report.pdf",
                        "filename": "climate-report.pdf",
                        "media_type": "application/pdf",
                        "bytes": 123456,
                        "checksum": {"algorithm": "sha256", "value": "abc123"},
                        "status": "downloaded",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    items = read_manifest_items(manifest)

    assert [item.lane for item in items] == ["website", "document"]
    document = items[1]
    assert document.source_item_id == "file-1"
    assert document.asset_id == "sha256-abc123"
    assert document.asset_local_path == "data/downloads/_tracked/iais/climate-report.pdf"
    assert document.asset_canonical_blob_path == "data/downloads/_blobs/ab/abc123.pdf"
    assert document.asset_tracked_path == "data/downloads/_tracked/iais/climate-report.pdf"
    assert document.asset_filename == "climate-report.pdf"
    assert document.asset_media_type == "application/pdf"
    assert document.asset_bytes == 123456
    assert document.asset_checksum_algorithm == "sha256"
    assert document.asset_checksum_value == "abc123"
    assert document.asset_metadata == {
        "asset_id": "sha256-abc123",
        "source_item_id": "file-1",
        "url": "https://www.iais.org/uploads/climate-report.pdf",
        "local_path": "data/downloads/_tracked/iais/climate-report.pdf",
        "canonical_blob_path": "data/downloads/_blobs/ab/abc123.pdf",
        "tracked_path": "data/downloads/_tracked/iais/climate-report.pdf",
        "filename": "climate-report.pdf",
        "media_type": "application/pdf",
        "bytes": 123456,
        "checksum": {"algorithm": "sha256", "value": "abc123"},
        "status": "downloaded",
    }


def test_read_manifest_items_only_attaches_assets_to_document_candidates(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "source": {"site_name": "IAIS"},
                "discovered_items": [
                    {
                        "item_id": "page-1",
                        "item_type": "page",
                        "url": "https://www.iais.org/climate-supervision",
                        "title": "Climate supervision update",
                    }
                ],
                "downloaded_assets": [
                    {
                        "asset_id": "sha256-secret",
                        "source_item_id": "page-1",
                        "local_path": "C:\\Users\\ferry\\Downloads\\secret.pdf",
                        "filename": "secret.pdf",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    items = read_manifest_items(manifest)

    assert items[0].lane == "website"
    assert items[0].asset_id == ""
    assert items[0].asset_local_path == ""


def test_collect_website_items_uses_fixture_without_live_web_listening(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "source": {"site_name": "IAIS"},
                "discovered_items": [
                    {
                        "url": "https://www.iais.org/climate",
                        "title": "Climate update",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    sources = [
        MonitorSource(
            key="iais",
            abbreviation="IAIS",
            full_name="International Association of Insurance Supervisors",
            url="https://www.iais.org/",
        )
    ]

    items, warnings = collect_website_items(
        sources,
        state_dir=tmp_path / "state",
        manifest_fixture_path=manifest,
    )

    assert warnings == []
    assert items[0].title == "Climate update"


@pytest.mark.usefixtures("governed_adapter_runtime")
def test_collect_website_items_preserves_duplicate_discovery_origins_for_url_merge(
    tmp_path, monkeypatch
):
    sources = [
        MonitorSource(key="one", abbreviation="ONE", full_name="One", url="https://one.example/"),
        MonitorSource(key="two", abbreviation="TWO", full_name="Two", url="https://two.example/"),
    ]
    checkpoint_calls: list[tuple[str, bool, bool]] = []

    def fake_collect_source_items(
        *,
        source,
        state_dir,
        fetch_mode="http",
        scope=None,
        stage_checkpoint=False,
        update_checkpoint=True,
        _runtime=None,
        seed_outcomes=None,
    ):
        checkpoint_calls.append(
            (source.key, stage_checkpoint, update_checkpoint)
        )
        return [
            CandidateItem(
                title=f"{source.full_name} discovery",
                url="https://example.org/shared",
                summary="Climate insurance evidence.",
                source_name=source.full_name,
                lane="website",
            )
        ], []

    monkeypatch.setattr(
        "climate_monitor.web_listening_adapter.collect_source_items",
        fake_collect_source_items,
    )

    items, warnings = collect_website_items(
        sources,
        state_dir=tmp_path / "state",
        stage_checkpoints=True,
    )

    assert warnings == []
    assert [item.source_name for item in items] == ["One", "Two"]
    assert checkpoint_calls == [("one", True, True), ("two", True, True)]


def test_public_site_result_preserves_scope_lanes_artifact_identity_and_attempts(tmp_path):
    class Budget:
        limits = {"fetch_attempts": 8}

        def usage(self):
            return {"fetch_attempts": 0}

        def remaining_seconds(self):
            return 60

        def claim(self, *args, **kwargs):
            return None

        def complete_tool(self, *args, **kwargs):
            return None

    payload = {
        "status": "completed", "stop_reason": "source_exhausted",
        "usage": {"requests": 2}, "errors": [],
        "attempts": [{"tool_id": "acquisition.web_http", "outcome": "succeeded"}],
        "site_skill_candidate": {"site_key": "example.test"},
        "site_state": {
            "generated_at": "2026-09-13T00:00:00Z",
            "seed_page_text": "unrelated page-wide climate wording",
            "pages": [
                {"canonical_url": "https://example.test/", "artifact_id": "seed",
                 "content_digest": "sha256:" + "0" * 64},
                {"canonical_url": "https://example.test/news/update", "artifact_id": "html",
                 "content_digest": "sha256:" + "1" * 64},
                {"canonical_url": "https://example.test/reports/update.pdf", "artifact_id": "pdf",
                 "content_digest": "sha256:" + "2" * 64},
                {"canonical_url": "https://example.test/events/ignored", "artifact_id": "excluded",
                 "content_digest": "sha256:" + "3" * 64},
            ],
        },
    }

    class Result:
        def to_dict(self):
            return payload

    class Runtime:
        request = None

        def explore_site(self, request):
            self.request = request
            return Result()

    source = MonitorSource("example", "EX", "Example", "https://example.test/")
    scope = SiteScope(
        "example", (), ("/news/", "/reports/"), ("/events/",),
    )
    runtime = Runtime()
    receipt = web_listening_adapter._run_site_seed(
        runtime, source, scope, source.url, tmp_path,
        web_listening_adapter.gateway_configuration([source], {"example": scope}),
        Budget(),
    )

    assert runtime.request.scope.seeds == (source.url,)
    assert runtime.request.scope.include_paths == ("/", "/news/**", "/reports/**")
    assert receipt["attempts"] == payload["attempts"]
    assert [(item["url"], item["lane"]) for item in receipt["candidates"]] == [
        ("https://example.test/news/update", "website"),
        ("https://example.test/reports/update.pdf", "document"),
    ]
    assert [item["source_item_id"] for item in receipt["candidates"]] == ["html", "pdf"]
    assert [item["content_hash"] for item in receipt["candidates"]] == ["1" * 64, "2" * 64]
    assert all("page-wide climate wording" not in item["evidence_text"]
               for item in receipt["candidates"])


def test_public_site_policy_rejection_preserves_upstream_reason_and_attempts(tmp_path):
    class Budget:
        limits = {"fetch_attempts": 4}
        def usage(self): return {"fetch_attempts": 0}
        def remaining_seconds(self): return 60
        def claim(self, *args, **kwargs): return None
        def complete_tool(self, *args, **kwargs): return None

    attempt = {"tool_id": "acquisition.playwright", "outcome": "failed"}
    payload = {
        "status": "rejected", "stop_reason": "rejected",
        "usage": {"requests": 1}, "errors": [{"code": "robots.forbidden"}],
        "attempts": [attempt], "site_skill_candidate": None, "site_state": None,
    }
    class Runtime:
        def explore_site(self, _request):
            return type("Result", (), {"to_dict": lambda self: payload})()

    source = MonitorSource("example", "EX", "Example", "https://example.test/")
    receipt = web_listening_adapter._run_site_seed(
        Runtime(), source, None, source.url, tmp_path,
        web_listening_adapter.gateway_configuration([source], {}), Budget(),
    )
    assert receipt["status"] == "rejected"
    assert receipt["event_kind"] == "policy"
    assert receipt["error"] == "robots.forbidden"
    assert receipt["attempts"] == [attempt]
    assert receipt["candidates"] == []


def test_staged_source_checkpoints_commit_only_fully_covered_seed_candidates(
    tmp_path, monkeypatch,
):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    web_listening_adapter._save_checkpoint(
        first, {"seed": "first"}, candidate_urls=["https://example.test/a"],
        staged=True, update=True,
    )
    web_listening_adapter._save_checkpoint(
        second, {"seed": "second"}, candidate_urls=["https://example.test/b"],
        staged=True, update=True,
    )
    monkeypatch.setattr(
        web_listening_adapter, "_valid_refresh_checkpoint_mapping", lambda value: True,
    )

    assert commit_staged_source_checkpoints(
        tmp_path, committed_urls={"https://example.test/a"},
    ) == 1
    assert json.loads(first.read_text()) == {"seed": "first"}
    assert not second.exists()
    assert not list(tmp_path.glob("*.pending-run.json"))


def test_classify_candidate_sets_climate_and_actuarial_flags():
    item = CandidateItem(
        title="Climate capital supervision update",
        url="https://example.org/update",
        summary="Insurance supervisors discuss climate risk capital.",
        source_name="Example",
        lane="website",
    )

    classified = classify_candidate(item, _config())

    assert classified.climate_related is True
    assert classified.actuarial_related is True
    assert classified.topics == ("capital", "climate", "insurance", "supervision")
    assert classified.categories == ("Climate Risk", "Insurance Risk")
    assert classified.keywords == ("capital", "climate", "insurance", "supervision")
    assert classified.climate_signal == "general_climate"
    assert classified.actuarial_signal == "insurance_risk"
    assert classified.confidence > 0.0


def test_classify_candidate_uses_evidence_text_for_climate_relevance():
    item = CandidateItem(
        title="New report",
        url="https://example.org/report",
        summary="A new publication was observed.",
        source_name="Example",
        lane="website",
        evidence_text="The body discusses adaptation and insurance supervision.",
    )

    classified = classify_candidate(item, _config())

    assert classified.climate_related is True
    assert classified.actuarial_related is True
    assert "adaptation" in classified.topics
    assert classified.climate_signal == "adaptation_resilience"
    assert classified.evidence_snippet.startswith("The body discusses adaptation")


def test_filter_recent_items_enforces_lookback_window():
    items = [
        CandidateItem(
            title="Recent climate report",
            url="https://example.org/recent",
            summary="Climate and insurance.",
            source_name="Example",
            lane="research",
            published="2026-05-01",
        ),
        CandidateItem(
            title="Old climate report",
            url="https://example.org/old",
            summary="Climate and insurance.",
            source_name="Example",
            lane="research",
            published="2026-03-01",
        ),
        CandidateItem(
            title="Undated climate report",
            url="https://example.org/undated",
            summary="Climate and insurance.",
            source_name="Example",
            lane="research",
            published="",
        ),
        CandidateItem(
            title="Invalid date climate report",
            url="https://example.org/invalid",
            summary="Climate and insurance.",
            source_name="Example",
            lane="research",
            published="not-a-date",
        ),
    ]

    recent = filter_recent_items(items, today=date(2026, 5, 14), lookback_days=30)

    assert [item.title for item in recent] == ["Recent climate report"]
