from __future__ import annotations

import json
from datetime import date

from climate_monitor.ai_filter import classify_candidate
from climate_monitor.models import CandidateItem, MonitorSource, RunConfig, SiteScope
from climate_monitor.research_search import (
    DEFAULT_SEARCH_MODEL,
    filter_recent_items,
    parse_openai_research_payload,
    read_research_fixture,
    search_recent_research,
)
from climate_monitor.web_listening_adapter import collect_source_items, collect_website_items, read_manifest_items


def _config() -> RunConfig:
    return RunConfig(
        report_title="Daily Climate & Actuarial Monitor",
        climate_keywords=("climate", "flood", "wildfire", "adaptation"),
        actuarial_keywords=("insurance", "capital", "supervision"),
        research_queries=("climate insurance report",),
        research_lookback_days=30,
        max_items_per_report=12,
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


def test_collect_source_items_uses_scoped_seeds_and_filters_candidates(tmp_path, monkeypatch):
    class Page:
        def __init__(self, url: str, links: list[str], text: str):
            self.final_url = url
            self.fit_markdown = text
            self.markdown = ""
            self.content_text = ""
            self.metadata_json = {"links": links}
            self.raw_html = ""

    class FakeCrawler:
        fetched: list[str] = []
        fetch_configs: list[dict] = []
        fetch_counts: dict[str, int] = {}

        def __init__(self, *, fetch_mode: str):
            self.fetch_mode = fetch_mode

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def fetch_page(self, url: str, *, fetch_mode: str, fetch_config_json: dict | None = None):
            self.fetched.append(url)
            self.fetch_configs.append(fetch_config_json or {})
            self.fetch_counts[url] = self.fetch_counts.get(url, 0) + 1
            links = ["https://www.iais.org/events/agenda.pdf"]
            if self.fetch_counts[url] > 1:
                links.append("https://www.iais.org/climate/report.pdf")
            return Page(
                url,
                links,
                f"Climate page for {url} version {self.fetch_counts[url]}",
            )

    diff = {
        "compute_hash": lambda text: f"hash:{text}",
        "extract_links": lambda html, base_url: [],
        "find_document_links": lambda links: [link for link in links if link.endswith(".pdf")],
        "find_new_links": lambda previous, current: [link for link in current if link not in previous],
        "select_compare_text": lambda **kwargs: kwargs["fit_markdown"],
    }
    source = MonitorSource(
        key="iais",
        abbreviation="IAIS",
        full_name="International Association of Insurance Supervisors",
        url="https://www.iais.org/",
    )
    scope = SiteScope(
        source_key="iais",
        seed_urls=("https://www.iais.org/news/",),
        include_patterns=("/news/", "/climate/"),
        exclude_patterns=("/events/",),
    )

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr("climate_monitor.web_listening_adapter._load_web_listening", lambda: (FakeCrawler, diff))

    baseline_items, baseline_warnings = collect_source_items(source=source, state_dir=tmp_path / "state", scope=scope)
    items, warnings = collect_source_items(source=source, state_dir=tmp_path / "state", scope=scope)

    assert baseline_items == []
    assert baseline_warnings == []
    assert warnings == []
    assert FakeCrawler.fetched == [
        "https://www.iais.org/",
        "https://www.iais.org/news/",
        "https://www.iais.org/",
        "https://www.iais.org/news/",
    ]
    assert all(config == {"user_agent_profile": "browser"} for config in FakeCrawler.fetch_configs)
    assert len(list((tmp_path / "state").glob("*.json"))) == 2
    saved_states = [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "state").glob("*.json")]
    assert all("https://www.iais.org/events/agenda.pdf" not in state["links"] for state in saved_states)
    item_urls = [item.url for item in items]
    assert "https://www.iais.org/news/" not in item_urls
    assert item_urls.count("https://www.iais.org/climate/report.pdf") == 2


def test_collect_source_items_honors_scope_fetch_mode_and_config(tmp_path, monkeypatch):
    class Page:
        final_url = "https://www.oecd.org/en/topics/climate-change.html"
        fit_markdown = "Climate page"
        markdown = ""
        content_text = ""
        metadata_json = {"links": []}
        raw_html = ""
        status_code = 200

    class FakeCrawler:
        init_modes: list[str] = []
        fetch_modes: list[str] = []
        fetch_configs: list[dict] = []

        def __init__(self, *, fetch_mode: str):
            self.init_modes.append(fetch_mode)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def fetch_page(self, url: str, *, fetch_mode: str, fetch_config_json: dict | None = None):
            self.fetch_modes.append(fetch_mode)
            self.fetch_configs.append(fetch_config_json or {})
            return Page()

    diff = {
        "compute_hash": lambda text: "hash",
        "extract_links": lambda html, base_url: [],
        "find_document_links": lambda links: [],
        "find_new_links": lambda previous, current: [],
        "select_compare_text": lambda **kwargs: kwargs["fit_markdown"],
    }
    source = MonitorSource(
        key="oecd",
        abbreviation="OECD",
        full_name="Organisation for Economic Co-operation and Development",
        url="https://www.oecd.org/",
    )
    scope = SiteScope(
        source_key="oecd",
        seed_urls=("https://www.oecd.org/en/topics/climate-change.html",),
        include_patterns=("/topics/climate-change",),
        exclude_patterns=(),
        fetch_mode="browser",
        fetch_config_json={
            "user_agent_profile": "browser",
            "wait_until": "domcontentloaded",
            "extra_wait_ms": 2500,
        },
    )

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr("climate_monitor.web_listening_adapter._load_web_listening", lambda: (FakeCrawler, diff))

    collect_source_items(source=source, state_dir=tmp_path / "state", scope=scope)

    assert FakeCrawler.init_modes == ["browser", "browser"]
    assert FakeCrawler.fetch_modes == ["browser", "browser"]
    assert FakeCrawler.fetch_configs == [scope.fetch_config_json, scope.fetch_config_json]


def test_collect_source_items_warns_when_seed_has_no_usable_information(tmp_path, monkeypatch):
    class Page:
        final_url = "https://www.example.org/empty"
        fit_markdown = ""
        markdown = ""
        content_text = ""
        metadata_json = {"link_count": 0, "word_count": 0, "source_kind": "html"}
        raw_html = "<html><body></body></html>"
        status_code = 200

    class FakeCrawler:
        def __init__(self, *, fetch_mode: str):
            self.fetch_mode = fetch_mode

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def fetch_page(self, url: str, *, fetch_mode: str, fetch_config_json: dict | None = None):
            return Page()

    diff = {
        "compute_hash": lambda text: "hash",
        "extract_links": lambda html, base_url: [],
        "find_document_links": lambda links: [],
        "find_new_links": lambda previous, current: [],
        "select_compare_text": lambda **kwargs: kwargs["fit_markdown"],
    }
    source = MonitorSource(
        key="example",
        abbreviation="EXAMPLE",
        full_name="Example",
        url="https://www.example.org/empty",
    )

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr("climate_monitor.web_listening_adapter._load_web_listening", lambda: (FakeCrawler, diff))

    items, warnings = collect_source_items(source=source, state_dir=tmp_path / "state")

    assert items == []
    assert len(warnings) == 1
    assert "no usable information" in warnings[0]
    assert "words=0" in warnings[0]
    assert "links=0" in warnings[0]


def test_collect_source_items_warns_when_seed_returns_security_verification(tmp_path, monkeypatch):
    class Page:
        final_url = "https://www.example.org/protected"
        fit_markdown = "# www.example.org\n\n## Performing security verification"
        markdown = ""
        content_text = ""
        metadata_json = {"link_count": 2, "word_count": 6, "source_kind": "html"}
        raw_html = "<html><body>Performing security verification</body></html>"
        status_code = 200

    class FakeCrawler:
        def __init__(self, *, fetch_mode: str):
            self.fetch_mode = fetch_mode

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def fetch_page(self, url: str, *, fetch_mode: str, fetch_config_json: dict | None = None):
            return Page()

    diff = {
        "compute_hash": lambda text: "hash",
        "extract_links": lambda html, base_url: [],
        "find_document_links": lambda links: [],
        "find_new_links": lambda previous, current: [],
        "select_compare_text": lambda **kwargs: kwargs["fit_markdown"],
    }
    source = MonitorSource(
        key="example",
        abbreviation="EXAMPLE",
        full_name="Example",
        url="https://www.example.org/protected",
    )

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr("climate_monitor.web_listening_adapter._load_web_listening", lambda: (FakeCrawler, diff))

    items, warnings = collect_source_items(source=source, state_dir=tmp_path / "state")

    assert items == []
    assert len(warnings) == 1
    assert "blocked or rejected content marker `performing security verification`" in warnings[0]


def test_collect_source_items_allows_pages_with_incidental_human_verification_text(tmp_path, monkeypatch):
    class Page:
        final_url = "https://www.example.org/page"
        fit_markdown = "Public climate disclosure update with a sign-in link to verify you are human."
        markdown = ""
        content_text = ""
        metadata_json = {
            "links": [
                "https://www.example.org/climate/report.pdf",
                "https://www.example.org/news/update",
                "https://www.example.org/publications",
            ]
        }
        raw_html = ""
        status_code = 200

    class FakeCrawler:
        def __init__(self, *, fetch_mode: str):
            self.fetch_mode = fetch_mode

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def fetch_page(self, url: str, *, fetch_mode: str, fetch_config_json: dict | None = None):
            return Page()

    diff = {
        "compute_hash": lambda text: "hash",
        "extract_links": lambda html, base_url: [],
        "find_document_links": lambda links: [link for link in links if link.endswith(".pdf")],
        "find_new_links": lambda previous, current: [link for link in current if link not in previous],
        "select_compare_text": lambda **kwargs: kwargs["fit_markdown"],
    }
    source = MonitorSource(
        key="example",
        abbreviation="EXAMPLE",
        full_name="Example",
        url="https://www.example.org/page",
    )

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr("climate_monitor.web_listening_adapter._load_web_listening", lambda: (FakeCrawler, diff))

    items, warnings = collect_source_items(source=source, state_dir=tmp_path / "state")

    assert items == []
    assert warnings == []


def test_collect_source_items_preserves_successful_seeds_when_later_seed_fails(tmp_path, monkeypatch):
    class Page:
        def __init__(self, links: list[str]):
            self.final_url = "https://www.iais.org/"
            self.fit_markdown = "Climate page"
            self.markdown = ""
            self.content_text = ""
            self.metadata_json = {"links": links}
            self.raw_html = ""

    class FakeCrawler:
        fetch_count = 0

        def __init__(self, *, fetch_mode: str):
            self.fetch_mode = fetch_mode

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def fetch_page(self, url: str, *, fetch_mode: str, fetch_config_json: dict | None = None):
            if url.endswith("/broken/"):
                raise RuntimeError("broken seed")
            self.__class__.fetch_count += 1
            links = []
            if self.__class__.fetch_count > 1:
                links.append("https://www.iais.org/climate/report.pdf")
            return Page(links)

    diff = {
        "compute_hash": lambda text: "hash",
        "extract_links": lambda html, base_url: [],
        "find_document_links": lambda links: [link for link in links if link.endswith(".pdf")],
        "find_new_links": lambda previous, current: [link for link in current if link not in previous],
        "select_compare_text": lambda **kwargs: kwargs["fit_markdown"],
    }
    source = MonitorSource(
        key="iais",
        abbreviation="IAIS",
        full_name="International Association of Insurance Supervisors",
        url="https://www.iais.org/",
    )
    scope = SiteScope(
        source_key="iais",
        seed_urls=("https://www.iais.org/broken/",),
        include_patterns=("/climate/",),
        exclude_patterns=(),
    )

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr("climate_monitor.web_listening_adapter._load_web_listening", lambda: (FakeCrawler, diff))

    collect_source_items(source=source, state_dir=tmp_path / "state", scope=scope)
    items, warnings = collect_source_items(source=source, state_dir=tmp_path / "state", scope=scope)

    assert [item.url for item in items] == ["https://www.iais.org/climate/report.pdf"]
    assert len(warnings) == 1
    assert "broken seed" in warnings[0]


def test_collect_source_items_emits_document_lane_for_doc_links_and_website_lane_for_other_links(tmp_path, monkeypatch):
    class Page:
        def __init__(self, links: list[str]):
            self.final_url = "https://www.iais.org/"
            self.fit_markdown = "Climate page"
            self.markdown = ""
            self.content_text = ""
            self.metadata_json = {"links": links}
            self.raw_html = ""

    class FakeCrawler:
        fetch_count = 0

        def __init__(self, *, fetch_mode: str):
            self.fetch_mode = fetch_mode

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def fetch_page(self, url: str, *, fetch_mode: str, fetch_config_json: dict | None = None):
            self.__class__.fetch_count += 1
            if self.__class__.fetch_count == 1:
                return Page([])
            return Page(
                [
                    "https://www.iais.org/climate/report.pdf",
                    "https://www.iais.org/climate/news-update",
                ]
            )

    diff = {
        "compute_hash": lambda text: "hash",
        "extract_links": lambda html, base_url: [],
        "find_document_links": lambda links: [link for link in links if link.endswith(".pdf")],
        "find_new_links": lambda previous, current: [link for link in current if link not in previous],
        "select_compare_text": lambda **kwargs: kwargs["fit_markdown"],
    }
    source = MonitorSource(
        key="iais",
        abbreviation="IAIS",
        full_name="International Association of Insurance Supervisors",
        url="https://www.iais.org/",
    )

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr("climate_monitor.web_listening_adapter._load_web_listening", lambda: (FakeCrawler, diff))

    baseline_items, baseline_warnings = collect_source_items(source=source, state_dir=tmp_path / "state")
    items, warnings = collect_source_items(source=source, state_dir=tmp_path / "state")

    assert baseline_items == []
    assert baseline_warnings == []
    assert warnings == []
    lanes_by_url = {item.url: item.lane for item in items}
    assert lanes_by_url == {
        "https://www.iais.org/climate/report.pdf": "document",
        "https://www.iais.org/climate/news-update": "website",
    }


def test_live_document_link_uses_document_local_evidence_not_page_wide_text(tmp_path, monkeypatch):
    class Page:
        def __init__(self, links: list[str]):
            self.final_url = "https://www.example.org/climate/"
            self.fit_markdown = "Climate transition risk adaptation insurance supervision"
            self.markdown = ""
            self.content_text = ""
            self.metadata_json = {"links": links}
            self.raw_html = ""

    class FakeCrawler:
        fetch_count = 0

        def __init__(self, *, fetch_mode: str):
            self.fetch_mode = fetch_mode

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def fetch_page(self, url: str, *, fetch_mode: str, fetch_config_json: dict | None = None):
            self.__class__.fetch_count += 1
            if self.__class__.fetch_count == 1:
                return Page([])
            return Page(["https://www.example.org/files/board-minutes.pdf"])

    diff = {
        "compute_hash": lambda text: f"hash:{text}",
        "extract_links": lambda html, base_url: [],
        "find_document_links": lambda links: [link for link in links if link.endswith(".pdf")],
        "find_new_links": lambda previous, current: [link for link in current if link not in previous],
        "select_compare_text": lambda **kwargs: kwargs["fit_markdown"],
    }
    source = MonitorSource(
        key="example",
        abbreviation="EXAMPLE",
        full_name="Example",
        url="https://www.example.org/climate/",
    )

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr("climate_monitor.web_listening_adapter._load_web_listening", lambda: (FakeCrawler, diff))

    collect_source_items(source=source, state_dir=tmp_path / "state")
    items, warnings = collect_source_items(source=source, state_dir=tmp_path / "state")

    assert warnings == []
    document = next(item for item in items if item.lane == "document")
    assert document.url == "https://www.example.org/files/board-minutes.pdf"
    assert "Climate transition risk" not in document.evidence_text
    assert document.evidence_text == "https://www.example.org/files/board-minutes.pdf Board Minutes"


def test_live_website_link_uses_link_evidence_not_seed_page_text(tmp_path, monkeypatch):
    class Page:
        def __init__(self, links: list[str]):
            self.final_url = "https://www.example.org/climate/"
            self.fit_markdown = "Climate adaptation insurance capital supervision seed page"
            self.markdown = ""
            self.content_text = ""
            self.metadata_json = {"links": links}
            self.raw_html = ""

    class FakeCrawler:
        fetch_count = 0

        def __init__(self, *, fetch_mode: str):
            self.fetch_mode = fetch_mode

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def fetch_page(self, url: str, *, fetch_mode: str, fetch_config_json: dict | None = None):
            self.__class__.fetch_count += 1
            if self.__class__.fetch_count == 1:
                return Page([])
            return Page(["https://www.example.org/news/barbados-precautionary-sba"])

    diff = {
        "compute_hash": lambda text: f"hash:{text}",
        "extract_links": lambda html, base_url: [],
        "find_document_links": lambda links: [link for link in links if link.endswith(".pdf")],
        "find_new_links": lambda previous, current: [link for link in current if link not in previous],
        "select_compare_text": lambda **kwargs: kwargs["fit_markdown"],
    }
    source = MonitorSource(
        key="example",
        abbreviation="EXAMPLE",
        full_name="Example",
        url="https://www.example.org/climate/",
    )

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr("climate_monitor.web_listening_adapter._load_web_listening", lambda: (FakeCrawler, diff))

    collect_source_items(source=source, state_dir=tmp_path / "state")
    items, warnings = collect_source_items(source=source, state_dir=tmp_path / "state")

    assert warnings == []
    assert len(items) == 1
    assert items[0].summary == "EXAMPLE added a new website link. Link text: Barbados Precautionary Sba."
    assert items[0].evidence_text == "https://www.example.org/news/barbados-precautionary-sba Barbados Precautionary Sba"
    classified = classify_candidate(items[0], _config())
    assert classified.climate_related is False
    assert classified.actuarial_related is False


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
