from __future__ import annotations

import json

from climate_monitor.ai_filter import classify_candidate
from climate_monitor.models import CandidateItem, MonitorSource, RunConfig, SiteScope
from climate_monitor.research_search import (
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

    items = search_recent_research(_config(), openai_client=client)

    assert items[0].title == "Wildfire insurance paper"
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
        fetch_counts: dict[str, int] = {}

        def __init__(self, *, fetch_mode: str):
            self.fetch_mode = fetch_mode

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def fetch_page(self, url: str, *, fetch_mode: str):
            self.fetched.append(url)
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
    assert len(list((tmp_path / "state").glob("*.json"))) == 2
    saved_states = [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "state").glob("*.json")]
    assert all("https://www.iais.org/events/agenda.pdf" not in state["links"] for state in saved_states)
    item_urls = [item.url for item in items]
    assert "https://www.iais.org/news/" in item_urls
    assert item_urls.count("https://www.iais.org/climate/report.pdf") == 2


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

        def fetch_page(self, url: str, *, fetch_mode: str):
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

    recent = filter_recent_items(items, today=__import__("datetime").date(2026, 5, 14), lookback_days=30)

    assert [item.title for item in recent] == ["Recent climate report"]
