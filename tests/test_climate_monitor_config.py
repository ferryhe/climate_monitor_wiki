from textwrap import dedent

import pytest
import yaml

from climate_monitor.config import load_run_config, load_sources


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
