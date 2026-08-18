from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api_server
from api_server import app
from climate_monitor.dedupe import canonical_url
from climate_registry.annotations import load_article_annotations
from climate_registry.audit import build_audit_registry
from climate_registry.read_api import RegistryContractError, RegistryReader
from climate_registry.reports import parse_report_directory
from climate_registry.schema import apply_migrations


ROOT = Path(__file__).resolve().parents[1]
CURRENT_HISTORICAL_DATES = (
    "2026-07-27",
    "2026-08-03",
    "2026-08-10",
    "2026-08-17",
)


def _registry(tmp_path: Path) -> Path:
    database = tmp_path / "article-registry.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection)
    with connection:
        connection.executemany(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?)",
            (
                ("source-a", "example.com", "Example Institute", "2026-08-03", "2026-08-10"),
                ("source-b", "insurer.test", "Insurer Research", "2026-08-03", "2026-08-10"),
                ("source-soa", "soa.org", "soa.org", "2026-08-03", "2026-08-10"),
            ),
        )
        connection.executemany(
            "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    "report-new", "2026-08-10", "climate-monitor-2026-08-10.md",
                    "Weekly Climate Monitor — 10 August", "sha-new", "weekly",
                    "weekly-pillars-v1", 57, 56, 1, '["one warning"]',
                ),
                (
                    "report-old", "2026-08-03", "climate-monitor-2026-08-03.md",
                    "Weekly Climate Monitor — 3 August", "sha-old", "weekly",
                    "weekly-pillars-v1", 57, 57, 0, "[]",
                ),
            ),
        )
        connection.executemany(
            """
            INSERT INTO articles(
                article_id, canonical_url, source_id, first_seen, last_seen,
                current_version_id, document_kind, publication_eligible,
                current_content_version_id, display_policy
            ) VALUES (?, ?, ?, ?, ?, ?, 'article', 1, ?, ?)
            """,
            (
                (
                    "article-full", "https://example.com/full", "source-a", "2026-08-03",
                    "2026-08-10", "version-full", None, "full_markdown",
                ),
                (
                    "article-excerpt", "https://example.com/excerpt", "source-a", "2026-08-10",
                    "2026-08-10", "version-excerpt", None, "summary_excerpt",
                ),
                (
                    "article-meta", "https://insurer.test/meta", "source-b", "2026-08-10",
                    "2026-08-10", "version-meta", None, "metadata_only",
                ),
            ),
        )
        connection.executemany(
            "INSERT INTO article_versions VALUES (?, ?, ?, ?, ?, ?, 'report-title-summary', ?, ?)",
            (
                (
                    "version-full", "article-full", "Climate: 100% transition", "climate transition",
                    "Report-derived full summary.", "fp-full", "2026-08-03", "2026-08-10",
                ),
                (
                    "version-excerpt", "article-excerpt", "Flood pricing [2026]", "flood pricing",
                    "Report-derived excerpt summary.", "fp-excerpt", "2026-08-10", "2026-08-10",
                ),
                (
                    "version-meta", "article-meta", "Capital & climate", "capital climate",
                    "Report-derived metadata summary.", "fp-meta", "2026-08-10", "2026-08-10",
                ),
            ),
        )
        discoveries = (
            (
                "discovery-old", "report-old", 1, "Pillar A", "A", "article-full",
                "version-full", "https://example.com/full?old=1", "Climate: 100% transition",
                "Report-derived full summary.", 1, None,
            ),
            (
                "discovery-new-1", "report-new", 1, "Pillar A", "A", "article-full",
                "version-full", "https://example.com/full", "Climate: 100% transition",
                "Report-derived full summary.", 1, None,
            ),
            (
                "discovery-new-2", "report-new", 2, "Pillar B", "B", "article-excerpt",
                "version-excerpt", "https://example.com/excerpt", "Flood pricing [2026]",
                "Report-derived excerpt summary.", 1, None,
            ),
            (
                "discovery-new-3", "report-new", 3, "Pillar B", "B", "article-meta",
                "version-meta", "https://insurer.test/meta", "Capital & climate",
                "Report-derived metadata summary.", 1, None,
            ),
        )
        connection.executemany("INSERT INTO discoveries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", discoveries)
        connection.executemany(
            """
            INSERT INTO report_appearances(
                report_id, article_id, version_id, discovery_id, section, pillar, ordinal,
                disposition, observation_status, external_content_change
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown')
            """,
            (
                ("report-old", "article-full", "version-full", "discovery-old", "Pillar A", "A", 1, "new", "new_article"),
                ("report-new", "article-full", "version-full", "discovery-new-1", "Pillar A", "A", 1, "previously-seen", "previously_seen"),
                ("report-new", "article-excerpt", "version-excerpt", "discovery-new-2", "Pillar B", "B", 2, "new", "new_article"),
                ("report-new", "article-meta", "version-meta", "discovery-new-3", "Pillar B", "B", 3, "new", "new_article"),
            ),
        )
        for suffix in ("full", "excerpt", "meta"):
            markdown = f"# {suffix.title()}\n\n" + (f"Evidence for {suffix}. " * 80)
            connection.execute(
                """
                INSERT INTO article_content_versions(
                    content_version_id, article_id, content_sha256, markdown_content,
                    markdown_sha256, content_type, source_bytes, extraction_method,
                    extraction_version, first_fetched_at
                ) VALUES (?, ?, ?, ?, ?, 'text/html', 2048, 'html-to-markdown', '1', ?)
                """,
                (
                    f"content-{suffix}", f"article-{suffix}", hashlib.sha256(markdown.encode()).hexdigest(),
                    markdown, hashlib.sha256((markdown + "md").encode()).hexdigest(),
                    "2026-08-13T12:00:00Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO article_fetches(
                    fetch_id, article_id, requested_url, final_url, fetched_at, fetch_status,
                    http_status, content_type, content_version_id
                ) VALUES (?, ?, ?, ?, '2026-08-13T12:00:00Z', 'success', 200, 'text/html', ?)
                """,
                (
                    f"fetch-{suffix}", f"article-{suffix}", f"https://example.com/{suffix}",
                    f"https://example.com/{suffix}", f"content-{suffix}",
                ),
            )
            categories = '["Insurance","Climate risk"]' if suffix == "full" else "not-json"
            connection.execute(
                """
                INSERT INTO article_enrichments(
                    enrichment_id, content_version_id, status, summary, categories_json,
                    keywords_json, language, generator_kind, generator_name,
                    generator_version, generated_at
                ) VALUES (?, ?, 'complete', ?, ?, ?, 'en', 'deterministic', 'registry-rules', '1', ?)
                """,
                (
                    f"enrichment-{suffix}", f"content-{suffix}", f"Enriched {suffix} summary.",
                    categories, '["risk","insurance"]', "2026-08-13T12:01:00Z",
                ),
            )
            connection.execute(
                "UPDATE articles SET current_content_version_id = ? WHERE article_id = ?",
                (f"content-{suffix}", f"article-{suffix}"),
            )
    connection.close()
    return database


@pytest.fixture()
def registry_client(tmp_path, monkeypatch):
    database = _registry(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    return TestClient(app), database


def test_status_is_safe_when_registry_is_missing_invalid_or_inside_repo(tmp_path, monkeypatch):
    client = TestClient(app)
    monkeypatch.delenv("CLIMATE_REGISTRY_DB", raising=False)
    assert client.get("/api/health").json() == {"status": "ok"}
    status = client.get("/api/registry/status")
    assert status.status_code == 503
    assert status.json() == {
        "available": False,
        "reason": "not_configured",
    }

    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(tmp_path / "missing.sqlite3"))
    response = client.get("/api/registry/status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "database_unavailable"}
    assert str(tmp_path) not in response.text

    inside_repo = Path(__file__).resolve().parents[1] / "never-create.sqlite3"
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(inside_repo))
    inside = client.get("/api/registry/status")
    assert inside.status_code == 503
    assert inside.json() == {
        "available": False,
        "reason": "invalid_location",
    }


def test_status_and_report_endpoints_are_newest_first(registry_client):
    client, _ = registry_client
    status = client.get("/api/registry/status")
    assert status.status_code == 200
    assert status.json() == {
        "available": True,
        "schema_version": 3,
        "reports": 2,
        "articles": 3,
        "discoveries": 4,
        "latest_report_date": "2026-08-10",
    }

    response = client.get("/api/registry/reports?page=1&page_size=1")
    assert response.status_code == 200
    payload = response.json()
    assert payload["pagination"] == {"page": 1, "page_size": 1, "total": 2, "pages": 2}
    assert [item["report_date"] for item in payload["items"]] == ["2026-08-10"]
    assert payload["items"][0]["monitoring_status"] == "partial"
    assert payload["items"][0]["article_count"] == 3
    assert "filename" not in payload["items"][0]

    detail = client.get("/api/registry/reports/2026-08-10")
    assert detail.status_code == 200
    assert [item["ordinal"] for item in detail.json()["articles"]] == [1, 2, 3]
    assert detail.json()["monitoring"]["sites_failed"] == 1
    assert detail.json()["monitoring"]["warning_count"] == 1
    assert "one warning" not in detail.text


def test_current_four_historical_report_details_are_api_readable(
    tmp_path, monkeypatch
):
    database = tmp_path / "article-registry.sqlite3"
    build_audit_registry(ROOT / "sources", database, tmp_path / "audit")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "SOURCE_DIR", ROOT / "sources")
    client = TestClient(app)

    listing = client.get("/api/registry/reports?page=1&page_size=100")
    assert listing.status_code == 200
    listed_dates = {item["report_date"] for item in listing.json()["items"]}
    assert set(CURRENT_HISTORICAL_DATES) <= listed_dates
    for report_date in CURRENT_HISTORICAL_DATES:
        detail = client.get(f"/api/registry/reports/{report_date}")
        assert detail.status_code == 200
        assert detail.json()["report_date"] == report_date


def test_publishers_are_bounded_deterministic_read_only_choices(registry_client):
    client, _ = registry_client
    response = client.get("/api/registry/publishers")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"hostname": "example.com", "label": "Example Institute"},
            {"hostname": "insurer.test", "label": "Insurer Research"},
            {"hostname": "soa.org", "label": "soa"},
        ],
        "total": 3,
        "truncated": False,
    }


def test_publisher_label_keeps_state_government_context(registry_client):
    client, database = registry_client
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?)",
            (
                "source-california-insurance",
                "insurance.ca.gov",
                "insurance.ca.gov",
                "2026-08-10",
                "2026-08-10",
            ),
        )
    connection.close()

    items = client.get("/api/registry/publishers").json()["items"]

    assert {item["hostname"]: item["label"] for item in items}["insurance.ca.gov"] == "CA insurance"


def test_publisher_choices_are_capped(registry_client):
    client, database = registry_client
    connection = sqlite3.connect(database)
    with connection:
        connection.executemany(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?)",
            (
                (
                    f"source-extra-{index}",
                    f"publisher-{index:03d}.example",
                    f"publisher-{index:03d}.example",
                    "2026-08-10",
                    "2026-08-10",
                )
                for index in range(501)
            ),
        )
    connection.close()

    payload = client.get("/api/registry/publishers").json()
    assert len(payload["items"]) == 500
    assert payload["total"] == 504
    assert payload["truncated"] is True
    assert [item["hostname"] for item in payload["items"]] == sorted(
        item["hostname"] for item in payload["items"]
    )


def test_report_and_article_metadata_are_read_from_sha_matched_source(
    registry_client, tmp_path, monkeypatch
):
    client, database = registry_client
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    report_path = source_dir / "climate-monitor-2026-08-10.md"
    report_path.write_text(
        """# Weekly Climate Monitor

**Report Date:** 2026-08-10

## Executive Summary

- Sites checked: **3**, succeeded: **2**, failed: **1**
- Exact source observation in report order.

## Pillar A — Site Changes

- **Climate: 100% transition**
  - Report-derived full summary.
  - **Categories:** Transition Risk
  - **Keywords:** transition, scenarios
  🔗 https://example.com/full

## Pillar B — Intelligence

- **Flood pricing [2026]**
  - Report-derived excerpt summary.
  🔗 https://example.com/excerpt

- **Capital & climate**
  - Report-derived metadata summary.
  - **Categories:** Capital & Solvency, Climate Risk
  - **Keywords:** capital; climate
  🔗 https://insurer.test/meta

## Original Links

- https://example.com/full
- https://example.com/excerpt
- https://insurer.test/meta
""",
        encoding="utf-8",
    )
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            "UPDATE reports SET report_sha256 = ? WHERE report_date = '2026-08-10'",
            (digest,),
        )
        connection.execute(
            "UPDATE articles SET current_content_version_id = NULL WHERE article_id = 'article-meta'"
        )
    connection.close()
    monkeypatch.setattr(api_server, "SOURCE_DIR", source_dir)

    report = client.get("/api/registry/reports/2026-08-10").json()
    assert report["executive_summary"] == [
        "Sites checked: 3, succeeded: 2, failed: 1",
        "Exact source observation in report order.",
    ]
    assert report["articles"][0]["summary"] == "Enriched full summary."
    assert report["articles"][0]["summary_provenance"] == "content_enrichment"
    assert report["articles"][0]["categories"] == ["Insurance", "Climate risk"]
    assert report["articles"][0]["keywords"] == ["risk", "insurance"]
    assert report["articles"][0]["metadata_provenance"] == {
        "categories": "content_enrichment",
        "keywords": "content_enrichment",
    }
    assert report["articles"][1]["categories"] == []

    article = client.get("/api/registry/articles/article-meta").json()
    assert article["enrichment"]["categories"] == []
    assert article["enrichment"]["keywords"] == []
    assert article["report_metadata"] == {
        "categories": ["Capital & Solvency", "Climate Risk"],
        "keywords": ["capital", "climate"],
    }
    assert article["categories"] == ["Capital & Solvency", "Climate Risk"]
    assert article["keywords"] == ["capital", "climate"]
    assert article["metadata_provenance"] == {
        "categories": "source_report",
        "keywords": "source_report",
    }


def test_source_metadata_fails_closed_when_report_sha_does_not_match(
    registry_client, tmp_path, monkeypatch
):
    client, _ = registry_client
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "climate-monitor-2026-08-10.md").write_text(
        "# Weekly Climate Monitor\n\n**Report Date:** 2026-08-10\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(api_server, "SOURCE_DIR", source_dir)

    report = client.get("/api/registry/reports/2026-08-10").json()
    assert report["executive_summary"] == []
    articles = {item["article_id"]: item for item in report["articles"]}
    assert articles["article-full"]["categories"] == ["Insurance", "Climate risk"]
    assert articles["article-full"]["keywords"] == ["risk", "insurance"]
    assert articles["article-full"]["metadata_provenance"] == {
        "categories": "content_enrichment",
        "keywords": "content_enrichment",
    }
    assert articles["article-excerpt"]["categories"] == []
    assert articles["article-meta"]["categories"] == []
    assert all(
        "Transition Risk" not in item["categories"]
        and "transition" not in item["keywords"]
        for item in report["articles"]
    )


def test_original_content_annotations_supply_unique_article_detail_without_rewriting_sources(
    registry_client, tmp_path, monkeypatch
):
    client, database = registry_client
    source_dir = tmp_path / "sources"
    metadata_dir = tmp_path / "article_metadata"
    source_dir.mkdir()
    metadata_dir.mkdir()
    report_path = source_dir / "climate-monitor-2026-08-10.md"
    report_path.write_text(
        """# Weekly Climate Monitor
**Report Date:** 2026-08-10
## Executive Summary
- Sites checked: **3**, succeeded: **3**, failed: **0**
## Pillar A — Changes
- **Climate: 100% transition**
  - Report-derived full summary.
  🔗 https://example.com/full
## Pillar B — Intelligence
- **Flood pricing [2026]**
  - Report-derived excerpt summary.
  🔗 https://example.com/excerpt
- **Capital & climate**
  - Report-derived metadata summary.
  🔗 https://insurer.test/meta
## Original Links
- https://example.com/full
- https://example.com/excerpt
- https://insurer.test/meta
""",
        encoding="utf-8",
    )
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    articles = [
        ("https://example.com/full", "Climate: 100% transition", "Transition scenarios were reviewed.", ["Transition Risk"], ["climate", "transition", "scenarios"]),
        ("https://example.com/excerpt", "Flood pricing [2026]", "The item covers flood pricing in 2026.", ["Physical Risk", "Insurance Risk"], ["flood", "pricing", "2026"]),
        ("https://insurer.test/meta", "Capital & climate", "The item links capital and climate considerations.", ["Capital & Solvency", "Climate Risk"], ["capital", "climate", "solvency"]),
    ]
    payload = {
        "schema_version": 1,
        "annotation_method": "subagent-original-content-v1",
        "source_scope": "linked-original-content-with-report-fallback",
        "generated_on": "2026-08-17",
        "articles": [
            {
                "canonical_url": url,
                "source_url": url,
                "title": title,
                "source_basis": "original_content",
                "summary": summary,
                "categories": categories,
                "keywords": keywords,
            }
            for url, title, summary, categories, keywords in articles
        ],
    }
    (metadata_dir / "articles-001-003.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            "UPDATE reports SET report_sha256 = ? WHERE report_date = '2026-08-10'", (digest,)
        )
        connection.execute(
            "UPDATE articles SET current_content_version_id = NULL WHERE article_id = 'article-meta'"
        )
    connection.close()
    monkeypatch.setattr(api_server, "SOURCE_DIR", source_dir)
    monkeypatch.setattr(api_server, "ARTICLE_METADATA_DIR", metadata_dir)

    report = client.get("/api/registry/reports/2026-08-10").json()
    assert report["articles"][0]["summary"] == "Enriched full summary."
    assert report["articles"][0]["summary_provenance"] == "content_enrichment"
    assert report["articles"][0]["categories"] == ["Insurance", "Climate risk"]
    assert report["articles"][0]["keywords"] == ["risk", "insurance"]
    assert report["articles"][2]["summary"] == "The item links capital and climate considerations."
    assert report["articles"][2]["categories"] == ["Capital & Solvency", "Climate Risk"]
    assert report["articles"][2]["metadata_provenance"]["categories"] == "original_content_annotation"

    article = client.get("/api/registry/articles/article-meta").json()
    assert article["summary"] == "The item links capital and climate considerations."
    assert article["summary_provenance"] == "original_content_annotation"
    assert article["metadata_provenance"] == {
        "categories": "original_content_annotation",
        "keywords": "original_content_annotation",
    }
    assert article["report_metadata"] == {"categories": [], "keywords": []}
    assert article["source_annotation"] == {
        "source_basis": "original_content",
        "source_url": "https://insurer.test/meta",
        "generated_on": "2026-08-17",
    }

    payload["articles"][2]["source_basis"] = "official_replacement"
    payload["articles"][2]["source_url"] = "https://insurer.test/corrected-meta"
    (metadata_dir / "articles-001-003.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    article = client.get("/api/registry/articles/article-meta").json()
    assert article["summary_provenance"] == "official_replacement_annotation"
    assert article["metadata_provenance"] == {
        "categories": "official_replacement_annotation",
        "keywords": "official_replacement_annotation",
    }
    assert article["source_annotation"] == {
        "source_basis": "official_replacement",
        "source_url": "https://insurer.test/corrected-meta",
        "generated_on": "2026-08-17",
    }


def test_complete_db_enrichment_is_atomic_over_conflicting_json_annotation(
    registry_client, tmp_path, monkeypatch, caplog
):
    client, database = registry_client
    metadata_dir = tmp_path / "article_metadata"
    metadata_dir.mkdir()
    (metadata_dir / "articles-001-001.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "annotation_method": "subagent-original-content-v1",
                "source_scope": "linked-original-content-with-report-fallback",
                "generated_on": "2026-08-17",
                "articles": [
                    {
                        "canonical_url": "https://example.com/full",
                        "source_url": "https://example.com/full",
                        "title": "Compatibility annotation title",
                        "source_basis": "original_content",
                        "summary": "JSON annotation summary must not replace the DB bundle.",
                        "categories": ["Transition Risk"],
                        "keywords": ["climate", "transition", "scenarios"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            """
            INSERT INTO article_enrichments(
                enrichment_id, content_version_id, status, summary, categories_json,
                keywords_json, language, generator_kind, generator_name,
                generator_version, generated_at
            ) VALUES (
                'enrichment-full-new', 'content-full', 'complete',
                'Current DB bundle summary.', 'not-json', '[]', 'en',
                'deterministic', 'registry-rules', '2', '2026-08-18T12:01:00Z'
            )
            """
        )
    connection.close()
    monkeypatch.setattr(api_server, "ARTICLE_METADATA_DIR", metadata_dir)

    with caplog.at_level(logging.DEBUG, logger="climate_registry.read_api"):
        report = client.get("/api/registry/reports/2026-08-10").json()
        detail = client.get("/api/registry/articles/article-full").json()

    report_article = report["articles"][0]
    assert [item["article_id"] for item in report["articles"]] == [
        "article-full",
        "article-excerpt",
        "article-meta",
    ]
    assert report_article["pillar"] == "A"
    assert report_article["summary"] == "Current DB bundle summary."
    assert report_article["categories"] == []
    assert report_article["keywords"] == []
    assert report_article["summary_provenance"] == "content_enrichment"
    assert report_article["metadata_provenance"] == {
        "categories": "content_enrichment",
        "keywords": "content_enrichment",
    }
    assert report_article["title"] == "Compatibility annotation title"
    assert report_article["source_annotation"] == {
        "source_basis": "original_content",
        "source_url": "https://example.com/full",
        "generated_on": "2026-08-17",
    }

    assert detail["original_url"] == "https://example.com/full"
    assert detail["summary"] == "Current DB bundle summary."
    assert detail["categories"] == []
    assert detail["keywords"] == []
    assert detail["summary_provenance"] == "content_enrichment"
    assert detail["metadata_provenance"] == {
        "categories": "content_enrichment",
        "keywords": "content_enrichment",
    }
    assert detail["title"] == "Compatibility annotation title"
    assert [item["report_date"] for item in detail["appearances"]] == [
        "2026-08-10",
        "2026-08-03",
    ]
    assert [item["pillar"] for item in detail["appearances"]] == ["A", "A"]
    assert "content_enrichment_id" not in report_article
    assert "content_enrichment_id" not in detail

    messages = [record.getMessage() for record in caplog.records]
    assert messages
    assert all(record.levelno == logging.DEBUG for record in caplog.records)
    assert all("overlap_fields=summary,categories,keywords" in message for message in messages)
    assert all("conflict_fields=summary,categories,keywords" in message for message in messages)
    rendered_logs = "\n".join(messages)
    for secret in (
        "https://example.com/full",
        "Current DB bundle summary.",
        "JSON annotation summary",
        str(metadata_dir),
    ):
        assert secret not in rendered_logs


def test_bundled_161_annotations_preserve_existing_report_api_payloads(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "historical-registry.sqlite3"
    build_audit_registry(root / "sources", database, tmp_path / "audit")
    annotations = load_article_annotations(root / "article_metadata")
    reader = RegistryReader(
        database,
        repository_root=root,
        source_dir=root / "sources",
        metadata_dir=root / "article_metadata",
    )

    observed = {}
    reports = sorted(
        parse_report_directory(root / "sources"),
        key=lambda item: item.report_date,
        reverse=True,
    )
    for report in reports:
        payload = reader.report(report.report_date)
        for article in payload["articles"]:
            url = canonical_url(article["canonical_url"])
            if url in annotations:
                observed.setdefault(url, article)
        if len(observed) == len(annotations):
            break

    assert len(annotations) == 161
    assert set(observed) == set(annotations)
    for url, annotation in annotations.items():
        article = observed[url]
        assert article["title"] == annotation.title
        assert article["summary"] == annotation.summary
        assert article["categories"] == list(annotation.categories)
        assert article["keywords"] == list(annotation.keywords)
        assert article["summary_provenance"] == annotation.provenance
        assert article["metadata_provenance"] == {
            "categories": annotation.provenance,
            "keywords": annotation.provenance,
        }
        assert article["source_annotation"] == {
            "source_basis": annotation.source_basis,
            "source_url": annotation.source_url,
            "generated_on": annotation.generated_on,
        }


@pytest.mark.parametrize("query", ["100%", "[2026]", "' OR 1=1 --", "_"])
def test_article_search_is_parameterized_and_treats_sql_wildcards_literally(registry_client, query):
    client, _ = registry_client
    response = client.get("/api/registry/articles", params={"query": query})
    assert response.status_code == 200
    expected = 1 if query in {"100%", "[2026]"} else 0
    assert response.json()["pagination"]["total"] == expected


def test_article_filters_and_deterministic_pagination(registry_client):
    client, _ = registry_client
    response = client.get(
        "/api/registry/articles",
        params={"source": "example.com", "pillar": "B", "report_date": "2026-08-10"},
    )
    assert response.status_code == 200
    assert [item["article_id"] for item in response.json()["items"]] == ["article-excerpt"]

    first = client.get("/api/registry/articles?page=1&page_size=2").json()
    second = client.get("/api/registry/articles?page=2&page_size=2").json()
    assert [item["article_id"] for item in first["items"]] == ["article-meta", "article-excerpt"]
    assert [item["article_id"] for item in second["items"]] == ["article-full"]


def test_pillar_and_report_date_must_match_the_same_appearance(registry_client):
    client, database = registry_client
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE report_appearances SET pillar = 'B' WHERE discovery_id = 'discovery-new-1'"
    )
    connection.execute("UPDATE discoveries SET pillar = 'B' WHERE discovery_id = 'discovery-new-1'")
    connection.commit()
    connection.close()

    wrong_week = client.get(
        "/api/registry/articles",
        params={"pillar": "A", "report_date": "2026-08-10"},
    ).json()
    right_week = client.get(
        "/api/registry/articles",
        params={"pillar": "B", "report_date": "2026-08-10"},
    ).json()
    assert "article-full" not in {item["article_id"] for item in wrong_week["items"]}
    assert "article-full" in {item["article_id"] for item in right_week["items"]}


@pytest.mark.parametrize(
    "path",
    (
        "/api/registry/reports?page=0",
        "/api/registry/reports?page=nope",
        "/api/registry/reports?page=999999999999999999999999",
        "/api/registry/reports?page_size=101",
        "/api/registry/articles?pillar=C",
        "/api/registry/articles?report_date=not-a-date",
    ),
)
def test_invalid_query_parameters_return_stable_400(registry_client, path):
    client, _ = registry_client
    response = client.get(path)
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid registry query parameters."}


def test_unknown_and_malformed_identifiers_are_stable(registry_client):
    client, _ = registry_client
    assert client.get("/api/registry/reports/2026-99-99").status_code == 400
    assert client.get("/api/registry/reports/2026-08-17").status_code == 404
    assert client.get("/api/registry/articles/bad/id").status_code == 404
    assert client.get("/api/registry/articles/not-found").status_code == 404


@pytest.mark.parametrize(
    ("article_id", "policy", "has_excerpt", "has_markdown"),
    (
        ("article-meta", "metadata_only", False, False),
        ("article-excerpt", "summary_excerpt", True, False),
        ("article-full", "full_markdown", False, True),
    ),
)
def test_article_detail_enforces_display_policy(
    registry_client, article_id, policy, has_excerpt, has_markdown
):
    client, _ = registry_client
    response = client.get(f"/api/registry/articles/{article_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["display_policy"] == policy
    assert bool(payload["enrichment"].get("summary")) is True
    assert bool(payload["content"].get("supporting_excerpt")) is has_excerpt
    assert bool(payload["content"].get("markdown")) is has_markdown
    if has_excerpt:
        assert len(payload["content"]["supporting_excerpt"]) <= 500
    assert "content_sha256" not in response.text
    assert "markdown_sha256" not in response.text
    assert "error_message" not in response.text
    if article_id == "article-full":
        assert payload["categories"] == ["Insurance", "Climate risk"]
        assert payload["keywords"] == ["risk", "insurance"]
        assert payload["metadata_provenance"] == {
            "categories": "content_enrichment",
            "keywords": "content_enrichment",
        }


def test_invalid_enrichment_json_fails_closed_to_empty_lists(registry_client):
    client, _ = registry_client
    payload = client.get("/api/registry/articles/article-excerpt").json()
    assert payload["enrichment"]["categories"] == []
    assert payload["enrichment"]["keywords"] == ["risk", "insurance"]
    assert payload["categories"] == []
    assert payload["keywords"] == ["risk", "insurance"]
    assert payload["metadata_provenance"] == {
        "categories": "content_enrichment",
        "keywords": "content_enrichment",
    }


def test_reader_is_immutable_read_only_and_observes_atomic_replacement(tmp_path):
    database = _registry(tmp_path)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1])

    with reader.connect() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM articles")
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not any(database.with_name(database.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))

    replacement = tmp_path / "replacement.sqlite3"
    replacement.write_bytes(database.read_bytes())
    connection = sqlite3.connect(replacement)
    with connection:
        connection.execute("DELETE FROM discoveries WHERE discovery_id = 'discovery-old'")
        connection.execute("DELETE FROM report_appearances WHERE discovery_id = 'discovery-old'")
        connection.execute("DELETE FROM reports WHERE report_id = 'report-old'")
    connection.close()
    replacement.replace(database)
    assert reader.status()["reports"] == 1


def test_invalid_schema_is_rejected_without_mutation(tmp_path):
    database = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection, target_version=2)
    connection.close()
    before = database.read_bytes()
    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1])
    with pytest.raises(RegistryContractError):
        reader.status()
    assert database.read_bytes() == before


def test_v3_number_without_required_columns_is_rejected(tmp_path):
    database = tmp_path / "incomplete.sqlite3"
    connection = sqlite3.connect(database)
    for table in (
        "sources", "reports", "articles", "article_versions", "discoveries",
        "report_appearances", "article_content_versions", "article_fetches",
        "article_enrichments",
    ):
        connection.execute(f"CREATE TABLE {table}(placeholder TEXT)")
    connection.execute("PRAGMA user_version = 3")
    connection.close()
    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1])
    with pytest.raises(RegistryContractError):
        reader.status()


def test_registry_path_must_be_absolute(tmp_path):
    with pytest.raises(RegistryContractError):
        with RegistryReader("relative.sqlite3", repository_root=tmp_path).connect():
            pass


@pytest.mark.parametrize("missing_column", ("enrichment_id", "generated_at"))
def test_contract_requires_every_enrichment_column_used_by_detail(tmp_path, missing_column):
    database = _registry(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    columns = [
        row[1]
        for row in connection.execute("PRAGMA table_info(article_enrichments)")
        if row[1] != missing_column
    ]
    select_columns = ", ".join(columns)
    connection.execute(
        f"CREATE TABLE replacement_enrichments AS SELECT {select_columns} FROM article_enrichments"
    )
    connection.execute("DROP TABLE article_enrichments")
    connection.execute("ALTER TABLE replacement_enrichments RENAME TO article_enrichments")
    connection.commit()
    connection.close()
    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1])
    with pytest.raises(RegistryContractError):
        reader.status()


@pytest.mark.parametrize("missing_column", ("ordinal", "section", "pillar"))
def test_status_classifies_missing_discovery_coherence_columns_as_invalid_schema(
    tmp_path, monkeypatch, missing_column
):
    database = _registry(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    columns = [
        row[1]
        for row in connection.execute("PRAGMA table_info(discoveries)")
        if row[1] != missing_column
    ]
    select_columns = ", ".join(columns)
    connection.execute(
        f"CREATE TABLE replacement_discoveries AS SELECT {select_columns} FROM discoveries"
    )
    connection.execute("DROP TABLE discoveries")
    connection.execute("ALTER TABLE replacement_discoveries RENAME TO discoveries")
    connection.commit()
    connection.close()

    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    response = TestClient(app).get("/api/registry/status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_schema"}


@pytest.mark.parametrize("corruption", ("dangling", "cross_owned"))
def test_status_rejects_invalid_non_null_current_content_pointer(
    tmp_path, monkeypatch, corruption
):
    database = _registry(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP TRIGGER articles_current_content_matches_article_update")
    target = "missing-content" if corruption == "dangling" else "content-meta"
    connection.execute(
        "UPDATE articles SET current_content_version_id = ? WHERE article_id = 'article-full'",
        (target,),
    )
    connection.commit()
    connection.close()

    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    response = TestClient(app).get("/api/registry/status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_schema"}


def test_null_current_content_pointer_remains_valid(tmp_path):
    database = _registry(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE articles SET current_content_version_id = NULL WHERE article_id = 'article-full'"
    )
    connection.commit()
    connection.close()
    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1])
    assert reader.status()["available"] is True


def test_contract_ownership_query_itself_rejects_dangling_current_content(tmp_path):
    """Prove the LEFT JOIN guard works independently of SQLite FK checking."""

    database = _registry(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    trigger_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        ("articles_current_content_matches_article_update",),
    ).fetchone()[0]
    connection.execute("DROP TRIGGER articles_current_content_matches_article_update")
    connection.execute(
        "UPDATE articles SET current_content_version_id = 'missing-content' WHERE article_id = 'article-full'"
    )
    connection.execute(trigger_sql)
    connection.commit()

    class _NoRows:
        @staticmethod
        def fetchone():
            return None

    class _ForeignKeyBlindConnection:
        def execute(self, statement, parameters=()):
            if statement.strip() == "PRAGMA foreign_key_check":
                return _NoRows()
            return connection.execute(statement, parameters)

    with pytest.raises(RegistryContractError, match="content ownership"):
        RegistryReader._validate_contract(_ForeignKeyBlindConnection())
    connection.close()


@pytest.mark.parametrize(
    "corruption",
    (
        "current_version", "appearance_version", "appearance_discovery",
        "appearance_ordinal", "appearance_section", "appearance_pillar",
    ),
)
def test_contract_rejects_cross_owned_article_and_appearance_links(tmp_path, corruption):
    database = _registry(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = OFF")
    if corruption == "current_version":
        connection.execute(
            "UPDATE articles SET current_version_id = 'version-meta' WHERE article_id = 'article-full'"
        )
    elif corruption == "appearance_version":
        connection.execute(
            "UPDATE report_appearances SET version_id = 'version-meta' WHERE discovery_id = 'discovery-new-1'"
        )
    elif corruption == "appearance_discovery":
        connection.execute(
            """
            INSERT INTO discoveries VALUES (
                'discovery-mismatch', 'report-new', 99, 'Pillar B', 'B',
                'article-meta', 'version-meta', 'https://insurer.test/meta?other=1',
                'Other title', 'Other summary', 1, NULL
            )
            """
        )
        connection.execute(
            "UPDATE report_appearances SET discovery_id = 'discovery-mismatch' WHERE discovery_id = 'discovery-new-1'"
        )
    elif corruption == "appearance_ordinal":
        connection.execute(
            "UPDATE report_appearances SET ordinal = 99 WHERE discovery_id = 'discovery-new-1'"
        )
    elif corruption == "appearance_section":
        connection.execute(
            "UPDATE report_appearances SET section = 'Wrong section' WHERE discovery_id = 'discovery-new-1'"
        )
    else:
        connection.execute(
            "UPDATE report_appearances SET pillar = 'B' WHERE discovery_id = 'discovery-new-1'"
        )
    connection.commit()
    connection.close()
    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1])
    with pytest.raises(RegistryContractError):
        reader.status()


def test_relative_registry_status_is_invalid_location(monkeypatch):
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", "relative.sqlite3")
    response = TestClient(app).get("/api/registry/status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_location"}


def test_unavailable_registry_routes_return_503_without_paths(tmp_path, monkeypatch):
    client = TestClient(app)
    missing = tmp_path / "secret-location" / "missing.sqlite3"
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(missing))
    for path in ("/api/registry/reports", "/api/registry/articles"):
        response = client.get(path)
        assert response.status_code == 503
        assert response.json() == {"detail": "Article registry is unavailable."}
        assert str(tmp_path) not in response.text


@pytest.mark.parametrize("endpoint", ("reports", "articles"))
@pytest.mark.parametrize("value", ("9" * 10_000, "", "+1", "-1", " 1", "1 ", "１"))
def test_pagination_rejects_unbounded_or_non_ascii_decimal_values(
    registry_client, endpoint, value
):
    client, _ = registry_client
    response = client.get(f"/api/registry/{endpoint}", params={"page": value})
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid registry query parameters."}


@pytest.mark.parametrize("endpoint", ("reports", "articles"))
def test_bounded_leading_zero_pagination_is_deliberately_accepted(registry_client, endpoint):
    client, _ = registry_client
    response = client.get(f"/api/registry/{endpoint}?page=0001&page_size=002")
    assert response.status_code == 200
    assert response.json()["pagination"]["page"] == 1
    assert response.json()["pagination"]["page_size"] == 2


def test_registry_has_no_write_routes():
    registry_routes = {
        (method, route.path)
        for route in app.routes
        if route.path.startswith("/api/registry")
        for method in (route.methods or set())
    }
    assert all(method == "GET" for method, _ in registry_routes)


def test_frontend_exposes_safe_registry_workspace_without_operations():
    root = Path(__file__).resolve().parents[1]
    index = (root / "showcase" / "index.html").read_text(encoding="utf-8")
    script = (root / "showcase" / "app.js").read_text(encoding="utf-8")
    assert 'data-view="registryView"' in index
    assert "Historical Reports" in index
    assert "Article Database" in index
    assert "/api/registry/reports" in script
    assert "/api/registry/articles" in script
    assert "registryMarkdown.textContent" in script
    assert not any(label in index for label in ("Refetch", "Reclassify", "Delete article", "Edit article"))
    assert "registryCard.innerHTML" not in script
    assert 'role="group" aria-label="Historical report sections"' in index
    assert 'data-registry-mode="reports" aria-pressed="true"' in index
    assert 'data-registry-mode="articles" aria-pressed="false"' in index
    assert 'class="registry-switch" role="tablist"' not in index
    assert 'button.setAttribute("aria-pressed", String(active))' in script
    assert 'id="registryReportDetail"' in index and 'aria-live="polite"' in index
    assert 'id="registryArticleDetail"' in index
    assert 'registryReportDetail.setAttribute("aria-busy"' in script
    assert 'registryArticleDetail.setAttribute("aria-busy"' in script
    assert "No source articles are recorded for this report." in script
    assert "No report appearances are recorded for this article." in script
