from textwrap import dedent

import pytest
import yaml

from climate_monitor.config import load_run_config, load_site_scopes, load_sources


def test_load_sources_normalizes_urls_and_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        dedent(
            """
            sources:
              - key: iais
                abbreviation: IAIS
                full_name: International Association of Insurance Supervisors
                url: www.iais.org
                high_priority: true
                tags: [insurance, climate]
              - key: iais
                abbreviation: Duplicate
                full_name: Duplicate
                url: https://example.com
            """
        ).strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate source key"):
        load_sources(path)


def test_load_sources_returns_valid_sources(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        dedent(
            """
            sources:
              - key: iais
                abbreviation: IAIS
                full_name: International Association of Insurance Supervisors
                url: www.iais.org
                high_priority: true
                tags: [insurance, climate]
            """
        ).strip(),
        encoding="utf-8",
    )

    sources = load_sources(path)

    assert len(sources) == 1
    assert sources[0].url == "https://www.iais.org"
    assert sources[0].high_priority is True
    assert sources[0].tags == ("insurance", "climate")


def test_load_site_scopes_returns_valid_scopes(tmp_path):
    path = tmp_path / "site_scopes.yaml"
    path.write_text(
        dedent(
            """
            site_scopes:
              - source_key: iais
                seed_urls:
                  - https://www.iais.org/news/
                include_patterns:
                  - /news/
                  - /climate/
                exclude_patterns:
                  - /events/
                notes: Reviewed IAIS news and climate-related paths.
            """
        ).strip(),
        encoding="utf-8",
    )

    scopes = load_site_scopes(path)

    assert len(scopes) == 1
    assert scopes[0].source_key == "iais"
    assert scopes[0].seed_urls == ("https://www.iais.org/news/",)
    assert scopes[0].include_patterns == ("/news/", "/climate/")
    assert scopes[0].exclude_patterns == ("/events/",)
    assert scopes[0].notes == "Reviewed IAIS news and climate-related paths."


def test_load_sources_registry_contains_excel_url_rows_and_missing_url_notes():
    registry_path = "monitoring/supranational_sources.yaml"

    payload = yaml.safe_load(open(registry_path, encoding="utf-8"))
    sources = load_sources(registry_path)

    assert len(sources) == 34
    assert len(payload["missing_url_notes"]) == 3
    assert {note["abbreviation"] for note in payload["missing_url_notes"]} == {
        "A2ii",
        "FAO",
        "UN Water",
    }
    assert all(source.url.startswith(("https://", "http://")) for source in sources)
    assert {source.key for source in sources} >= {"iais", "iea", "ipcc", "wto"}


def test_site_scopes_registry_covers_every_url_bearing_source():
    source_keys = {source.key for source in load_sources("monitoring/supranational_sources.yaml")}
    scopes = load_site_scopes("monitoring/site_scopes.yaml")

    assert len(scopes) == 34
    assert {scope.source_key for scope in scopes} == source_keys


def test_site_scopes_high_priority_examples_have_reviewed_climate_paths():
    scopes = {
        scope.source_key: scope
        for scope in load_site_scopes("monitoring/site_scopes.yaml")
    }
    useful_terms = (
        "climate",
        "research",
        "publication",
        "publications",
        "news",
        "sustainability",
    )

    for source_key in ("iais", "ipcc", "issb", "oecd", "wri"):
        scope = scopes[source_key]
        reviewed_text = " ".join(scope.seed_urls + scope.include_patterns).lower()
        assert any(term in reviewed_text for term in useful_terms), source_key


def test_load_run_config_reads_keywords_and_output_paths(tmp_path):
    path = tmp_path / "run_config.yaml"
    path.write_text(
        dedent(
            """
            report_title: Daily Climate & Actuarial Monitor
            max_items_per_report: 7
            climate_keywords: [Climate, Flood]
            actuarial_keywords: [Insurance, Reserving]
            research_lane:
              lookback_days: 30
              queries: [climate insurance report]
            output:
              source_dir: sources
              wiki_dir: wiki
              write_empty_report: false
            """
        ).strip(),
        encoding="utf-8",
    )

    config = load_run_config(path)

    assert config.report_title == "Daily Climate & Actuarial Monitor"
    assert config.max_items_per_report == 7
    assert config.climate_keywords == ("climate", "flood")
    assert config.actuarial_keywords == ("insurance", "reserving")
    assert config.research_queries == ("climate insurance report",)
    assert config.source_dir == "sources"
    assert config.write_empty_report is False
