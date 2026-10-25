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
from climate_delivery.artifacts import ARTIFACT_ONLY_DELIVERY_STATUS
from climate_delivery.io import atomic_write_json
from climate_delivery.pdf import render_pdf
from climate_delivery.pipeline import _manifest
from climate_delivery.report import parse_weekly_report
from climate_delivery.summary import build_summary, write_summary
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


def test_meeting_default_uses_utc_date_and_preserves_explicit_base(tmp_path, monkeypatch):
    from datetime import date, datetime, timezone
    import climate_monitor.meetings as meetings
    import climate_registry.read_api as read_api

    class UtcClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return datetime(2026, 10, 4, 0, 30, tzinfo=timezone.utc)

    class NewYorkDate(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 3)

    calls = []

    def query(_database, **kwargs):
        calls.append(kwargs)
        return {"records": [], "coverage": {"status": "not_processed"}}

    monkeypatch.setattr(read_api, "datetime", UtcClock)
    monkeypatch.setattr(read_api, "date", NewYorkDate)
    monkeypatch.setattr(meetings, "query_events", query)
    reader = RegistryReader(_registry(tmp_path), repository_root=ROOT)
    assert reader.meetings()["base_date"] == "2026-10-04"
    assert reader.meetings(base_date="2026-10-03")["base_date"] == "2026-10-03"
    assert all(call["timezone_name"] == "UTC" for call in calls)


def _write_retained_artifacts(output: Path) -> dict[str, tuple[dict, bytes]]:
    expected: dict[str, tuple[dict, bytes]] = {}
    for report_date in CURRENT_HISTORICAL_DATES:
        report = parse_weekly_report(
            ROOT / "sources" / f"climate-monitor-{report_date}.md"
        )
        summary = build_summary(report)
        destination = output / report.report_date / report.sha256
        summary_path = destination / "summary.json"
        pdf_name = f"climate-monitor-{report.report_date}.pdf"
        pdf_path = destination / pdf_name
        write_summary(summary, summary_path)
        render_pdf(summary, pdf_path)
        pdf_bytes = pdf_path.read_bytes()
        atomic_write_json(
            destination / "manifest.json",
            _manifest(
                summary,
                {"status": ARTIFACT_ONLY_DELIVERY_STATUS, "recipients": []},
                summary_sha256=hashlib.sha256(summary_path.read_bytes()).hexdigest(),
                pdf_name=pdf_name,
                pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            ),
        )
        expected[report_date] = (summary, pdf_bytes)
    return expected


def _approve_fixture(database: Path, metadata_dir: Path | None = None, source_dir: Path | None = None) -> None:
    """This fixture represents an explicitly accepted public snapshot."""
    from climate_registry.publication import enabled, stage_entities, _approve
    with sqlite3.connect(database) as connection:
        if enabled(connection):
            from verified_fixtures import seed_verified_articles, seed_verified_pdf_meetings
            seed_verified_articles(connection)
            seed_verified_pdf_meetings(connection)
            annotations = load_article_annotations(metadata_dir) if metadata_dir else {}
            for sha in stage_entities(connection, annotations=annotations,source_dir=source_dir):
                _approve(connection, sha, {"basis": "accepted_test_fixture"}, status="accepted_legacy")
            from climate_registry.display import refresh_display
            refresh_display(connection)


def _registry(tmp_path: Path, *, target_version: int | None = None) -> Path:
    database = tmp_path / "article-registry.sqlite3"
    connection = sqlite3.connect(database)
    if target_version is None:
        apply_migrations(connection)
    else:
        apply_migrations(connection, target_version=target_version)
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
    _approve_fixture(database)
    return database


@pytest.fixture()
def registry_client(tmp_path, monkeypatch, request):
    database = _registry(tmp_path,target_version=getattr(request,"param",None))
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    return TestClient(app), database


def test_article_category_endpoint_returns_more_than_three_pages_in_one_snapshot(monkeypatch, tmp_path):
    from contextlib import nullcontext
    from climate_registry.list_projection import RegistryListProjection

    class Reader:
        def _public_candidates(self, *kinds): return {}
        def approved_article_cards_all(self):
            return [{"article_id": f"article-{index}", "canonical_url": f"https://example.org/{index}",
                "title": str(index), "source": "example.org", "publisher": "Example"} for index in range(350)]
        def pdf_articles_all(self, **kwargs): return []
        def approved_reports_all(self): return []
        def pdf_reports_all(self): return []
        def status(self): return {"available": True}
        def approved_merged_article_count(self): return 350
        def publishers(self, **kwargs): return {"items": [], "total": 0, "truncated": False}

    projection = RegistryListProjection(Reader())
    try:
        monkeypatch.setattr(api_server, "_registry_list_projection", lambda: nullcontext(projection))
        response = TestClient(app).get("/api/registry/article-categories")
        assert response.status_code == 200
        assert len(response.json()["items"]) == 350
        assert projection.articles(page=4, page_size=100)["pagination"]["total"] == 350
        assert len(projection.articles(page=4, page_size=100)["items"]) == 50
    finally:
        projection.close()


def _insert_pdf_intake_records(database: Path, *, unique_url="https://example.com/wildfire-report") -> None:
    document_sha256 = "f" * 64
    occurrence = {
        "occurrence_id": "pdf-occurrence-unique",
        "source_document_sha256": document_sha256,
        "page": 3,
        "raw_url": unique_url + "?utm_source=pdf",
        "report_date": "2026-09-03",
        "publication_date": "2026-09-01",
        "title": "Wildfire risk report",
        "summary": "Wildfire risk findings preserved from the PDF.",
        "content_sha256": "a" * 64,
    }
    duplicate_occurrence = {
        **occurrence,
        "occurrence_id": "pdf-occurrence-core",
        "raw_url": "https://example.com/full",
        "title": "Climate: 100% transition",
    }
    calendar_item = {
        "event_id": "pdf-event-publication",
        "occurrence_id": "pdf-calendar-occurrence",
        "source_document_sha256": document_sha256,
        "page": 4,
        "name": "Climate research launch",
        "kind": "publication",
        "raw_date": "4 September 2026",
        "start_date": "2026-09-04",
        "end_date": "2026-09-04",
        "date_precision": "day",
        "summary": "Launch date listed in the source PDF.",
        "source_urls": ["https://example.com/report"],
        "location": "Paris, France",
        "online_url": "https://example.com/meeting-room",
    }
    connection = sqlite3.connect(database)
    connection.execute(
        """INSERT INTO pdf_intake_documents (
            document_sha256, source_path, filename, media_type, size_bytes, date_of_run,
            period_start, period_end, extracted_text_sha256, document_json, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (document_sha256, "C:/input/report.pdf", "report.pdf", "application/pdf", 100,
         "2026-09-03", "2026-09-01", "2026-09-02", "b" * 64, "{}",
         "2026-09-03T00:00:00Z"),
    )
    connection.executemany(
        """INSERT INTO pdf_intake_articles (
            article_id, canonical_url, title, type_safe_classification_json, imported_at
        ) VALUES (?, ?, ?, ?, ?)""",
        (
            ("pdf-article-unique", unique_url, "Wildfire risk report",
             '{"provider":"typesafe","label":"article"}', "2026-09-03T00:00:00Z"),
            ("pdf-article-core", "https://example.com/full", "Core article duplicate", None,
             "2026-09-03T00:00:00Z"),
        ),
    )
    if connection.execute("PRAGMA user_version").fetchone()[0] >= 16:
        connection.execute(
            """UPDATE pdf_intake_articles
               SET core_article_id='article-full', confirmation_basis='exact_url_eligible_detail'
               WHERE article_id='pdf-article-core'"""
        )
    connection.executemany(
        """INSERT INTO pdf_intake_article_occurrences (
            occurrence_id, article_id, source_document_sha256, page, raw_url, report_date,
            publication_date, content_sha256, page_sha256, occurrence_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ("pdf-occurrence-unique", "pdf-article-unique", document_sha256, 3,
             occurrence["raw_url"], occurrence["report_date"], occurrence["publication_date"],
             occurrence["content_sha256"], "c" * 64, json.dumps(occurrence)),
            ("pdf-occurrence-core", "pdf-article-core", document_sha256, 3,
             duplicate_occurrence["raw_url"], duplicate_occurrence["report_date"],
             duplicate_occurrence["publication_date"], duplicate_occurrence["content_sha256"],
             "c" * 64, json.dumps(duplicate_occurrence)),
        ),
    )
    connection.execute(
        """INSERT INTO pdf_intake_calendar_items (
            occurrence_id, event_id, source_document_sha256, page, name, kind, raw_date,
            date_precision, start_date, end_date, summary, content_sha256,
            type_safe_classification_json, item_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (calendar_item["occurrence_id"], calendar_item["event_id"], document_sha256,
         calendar_item["page"], calendar_item["name"], calendar_item["kind"],
         calendar_item["raw_date"], calendar_item["date_precision"], calendar_item["start_date"],
         calendar_item["end_date"], calendar_item["summary"], "d" * 64, None,
         json.dumps(calendar_item)),
    )
    connection.commit()
    connection.close()
    _approve_fixture(database)


def test_nested_public_connections_restore_outer_archive_connection(registry_client):
    _, database = registry_client
    _insert_pdf_intake_records(database)
    reader = RegistryReader(database, repository_root=ROOT)
    before = database.read_bytes()
    with reader.connect() as outer:
        original = reader._snapshot_source_connection
        assert original is not outer
        with reader.connect() as inner:
            assert inner is not outer
            assert reader._snapshot_source_connection is not original
        assert reader._snapshot_source_connection is original
        with pytest.raises(RuntimeError, match="controlled inner failure"):
            with reader.connect():
                raise RuntimeError("controlled inner failure")
        assert reader._snapshot_source_connection is original
        assert original.execute("SELECT COUNT(*) FROM pdf_intake_article_occurrences").fetchone()[0] == 2
        assert reader.pdf_report("f" * 64)["filename"] == "report.pdf"
        assert reader._snapshot_source_connection is original
        assert outer.execute("SELECT COUNT(*) FROM pdf_intake_article_occurrences").fetchone()[0] == 2
    assert not hasattr(reader, "_snapshot_source_connection")
    assert database.read_bytes() == before


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
    status_payload = status.json()
    assert len(status_payload.pop("cache_version")) == 32
    assert status_payload == {
        "available": True,
        "schema_version": 26,
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
    artifact_root = tmp_path / "artifacts"
    expected_artifacts = _write_retained_artifacts(artifact_root)
    monkeypatch.setenv("CLIMATE_DELIVERY_OUTPUT_DIR", str(artifact_root))
    from climate_registry.display import refresh_artifact_display
    for report_date in CURRENT_HISTORICAL_DATES:
        refresh_artifact_display(report_date, output_dir=artifact_root)
    client = TestClient(app)

    listing = client.get("/api/registry/reports?page=1&page_size=100")
    assert listing.status_code == 200
    listed_dates = {item["report_date"] for item in listing.json()["items"]}
    assert set(CURRENT_HISTORICAL_DATES) <= listed_dates
    for report_date in CURRENT_HISTORICAL_DATES:
        detail = client.get(f"/api/registry/reports/{report_date}")
        assert detail.status_code == 200
        payload = detail.json()
        summary, pdf_bytes = expected_artifacts[report_date]
        assert payload["report_date"] == summary["report"]["date"] == report_date
        assert payload["report_title"] == summary["report"]["title"]
        assert payload["report_briefing"] is None
        assert payload["report_pdf"] == {
            "filename": f"climate-monitor-{report_date}.pdf",
            "download_url": f"/api/registry/reports/{report_date}/pdf",
        }
        download = client.get(payload["report_pdf"]["download_url"])
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/pdf"
        assert hashlib.sha256(download.content).hexdigest() == hashlib.sha256(
            pdf_bytes
        ).hexdigest()


@pytest.mark.parametrize("registry_client",[20],indirect=True)
def test_publishers_are_bounded_deterministic_read_only_choices(registry_client):
    client, _ = registry_client
    response = client.get("/api/registry/publishers")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"hostname": "example.com", "label": "example.com"},
            {"hostname": "insurer.test", "label": "insurer.test"},
            {"hostname": "soa.org", "label": "Society of Actuaries (SOA)"},
        ],
        "total": 3,
        "truncated": False,
    }


@pytest.mark.parametrize("registry_client",[20],indirect=True)
def test_publisher_label_uses_canonical_state_agency_name(registry_client):
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

    assert {item["hostname"]: item["label"] for item in items}["insurance.ca.gov"] == \
        "California Department of Insurance (CDI)"


@pytest.mark.parametrize("registry_client",[20],indirect=True)
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
            """
            UPDATE articles SET current_content_version_id = NULL
            WHERE article_id IN ('article-meta', 'article-excerpt')
            """
        )
    connection.close()
    _approve_fixture(database,source_dir=source_dir)
    monkeypatch.setattr(api_server, "SOURCE_DIR", source_dir)

    report = client.get("/api/registry/reports/2026-08-10").json()
    assert report["executive_summary"] == []  # Archive text has no original-summary adoption.
    assert report["articles"][0]["summary"] == "Report-derived full summary."
    assert report["articles"][0]["summary_provenance"] == "verified_source_evidence"
    assert report["articles"][0]["categories"] == ["Insurance", "Climate risk"]
    assert report["articles"][0]["keywords"] == ["risk", "insurance"]
    assert report["articles"][0]["metadata_provenance"] == {
        "categories": "content_enrichment",
        "keywords": "content_enrichment",
    }
    assert report["articles"][1]["categories"] == []
    assert report["articles"][1]["keywords"] == []
    assert report["articles"][1]["summary"] == "Report-derived excerpt summary."
    assert report["articles"][1]["report_summary"] == "Report-derived excerpt summary."
    assert report["articles"][1]["metadata_provenance"] == {
        "categories": None,
        "keywords": None,
    }
    incomplete_detail = client.get("/api/registry/articles/article-excerpt").json()
    assert incomplete_detail["report_summary"] == "Report-derived excerpt summary."
    semantic_fields = (
        "summary", "categories", "keywords", "summary_provenance",
        "metadata_provenance",
    )
    assert {
        name: incomplete_detail[name] for name in semantic_fields
    } == {
        name: report["articles"][1][name] for name in semantic_fields
    }

    article = client.get("/api/registry/articles/article-meta").json()
    assert article["enrichment"]["categories"] == []
    assert article["enrichment"]["keywords"] == []
    assert article["report_metadata"] == {
        "categories": ["Capital & Solvency", "Climate Risk"], "keywords": ["capital", "climate"],
    }
    assert article["categories"] == ["Capital & Solvency", "Climate Risk"]
    assert article["keywords"] == ["capital", "climate"]
    assert article["metadata_provenance"] == {"categories": "source_report", "keywords": "source_report"}
    # The same frozen report classifications remain metadata in private and public reads.
    article = RegistryReader(database, repository_root=ROOT, source_dir=source_dir, public=False).article("article-meta")
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
    _approve_fixture(database,metadata_dir,source_dir)
    monkeypatch.setattr(api_server, "SOURCE_DIR", source_dir)
    monkeypatch.setattr(api_server, "ARTICLE_METADATA_DIR", metadata_dir)

    public_report = client.get("/api/registry/reports/2026-08-10").json()
    assert public_report["articles"][2]["summary"] == "Report-derived metadata summary."
    assert public_report["articles"][2]["categories"] == ["Capital & Solvency", "Climate Risk"]
    def private_reader():
        return RegistryReader(database, repository_root=ROOT, source_dir=source_dir, metadata_dir=metadata_dir, public=False)
    report = private_reader().report("2026-08-10")
    assert report["articles"][0]["summary"] == "Enriched full summary."
    assert report["articles"][0]["summary_provenance"] == "content_enrichment"
    assert report["articles"][0]["categories"] == ["Insurance", "Climate risk"]
    assert report["articles"][0]["keywords"] == ["risk", "insurance"]
    assert report["articles"][2]["summary"] == "The item links capital and climate considerations."
    assert report["articles"][2]["categories"] == ["Capital & Solvency", "Climate Risk"]
    assert report["articles"][2]["metadata_provenance"]["categories"] == "original_content_annotation"

    article = private_reader().article("article-meta")
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
    report_article = report["articles"][2]
    assert {
        name: report_article[name]
        for name in ("summary", "categories", "keywords", "metadata_provenance")
    } == {
        name: article[name]
        for name in ("summary", "categories", "keywords", "metadata_provenance")
    }

    payload["articles"][2]["source_basis"] = "official_replacement"
    payload["articles"][2]["source_url"] = "https://insurer.test/corrected-meta"
    (metadata_dir / "articles-001-003.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    _approve_fixture(database,metadata_dir,source_dir)
    article = private_reader().article("article-meta")
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
    _approve_fixture(database, metadata_dir)
    monkeypatch.setattr(api_server, "ARTICLE_METADATA_DIR", metadata_dir)

    with caplog.at_level(logging.DEBUG, logger="climate_registry.read_api"):
        report = client.get("/api/registry/reports/2026-08-10").json()
        detail = client.get("/api/registry/articles/article-full").json()
        # Archive bundle precedence remains inspectable without publishing its generated facts.
        private = RegistryReader(database, repository_root=ROOT, metadata_dir=metadata_dir, public=False).article("article-full")
        assert private["summary"] == "Current DB bundle summary."

    report_article = report["articles"][0]
    assert [item["article_id"] for item in report["articles"]] == [
        "article-full",
        "article-excerpt",
        "article-meta",
    ]
    assert report_article["pillar"] == "A"
    assert report_article["summary"] == "Report-derived full summary."
    assert report_article["categories"] == []
    assert report_article["keywords"] == []
    assert report_article["summary_provenance"] == "verified_source_evidence"
    assert report_article["metadata_provenance"] == {
        "categories": "content_enrichment",
        "keywords": "content_enrichment",
    }
    assert report_article["title"] == "Climate: 100% transition"
    assert report_article["source_annotation"] == {'source_basis':'original_content','source_url':'https://example.com/full','generated_on':'2026-08-17'}

    assert detail["original_url"] == "https://example.com/full"
    assert detail["summary"] == "Report-derived full summary."
    assert detail["categories"] == []
    assert detail["keywords"] == []
    assert detail["summary_provenance"] == "verified_source_evidence"
    assert detail["metadata_provenance"] == {
        "categories": "content_enrichment",
        "keywords": "content_enrichment",
    }
    assert detail["title"] == "Climate: 100% transition"
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
    assert any("overlap_fields=summary,categories,keywords" in message for message in messages)
    assert any("conflict_fields=summary,categories,keywords" in message for message in messages)
    rendered_logs = "\n".join(messages)
    for secret in (
        "https://example.com/full",
        "Current DB bundle summary.",
        "JSON annotation summary",
        str(metadata_dir),
    ):
        assert secret not in rendered_logs


def test_manual_enrichment_can_attach_to_an_article_version_without_body(registry_client):
    client, database = registry_client
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE articles SET current_content_version_id = NULL WHERE article_id = 'article-meta'"
        )
        connection.execute(
            """INSERT INTO article_enrichments(
                   enrichment_id, article_id, article_version_id, status, summary,
                   categories_json, keywords_json, language, generator_kind,
                   generator_name, generator_version, generated_at
               ) VALUES ('manual-meta', 'article-meta', 'version-meta', 'complete',
                         'Human-reviewed summary.', '[\"policy-regulation\"]',
                         '[\"disclosure\"]', 'en', 'manual', 'human-review',
                         'manual-v1', '2026-10-07T12:00:00Z')"""
        )

    _approve_fixture(database)
    detail = client.get("/api/registry/articles/article-meta").json()
    report = client.get("/api/registry/reports/2026-08-10").json()
    report_article = next(item for item in report["articles"] if item["article_id"] == "article-meta")

    assert detail["summary"] == "Report-derived metadata summary."
    assert detail["summary_provenance"] == "verified_source_evidence"
    assert detail["enrichment"]["article_version_id"] == "version-meta"
    assert detail["enrichment"]["content_version_id"] is None
    assert detail["enrichment"]["generator"]["kind"] == "manual"
    assert detail["categories"] == ["policy-regulation"]
    assert report_article["summary"] == "Report-derived metadata summary."
    assert report_article["summary_provenance"] == "verified_source_evidence"


def test_pdf_snapshot_keeps_approved_verified_article_summary(monkeypatch):
    reader = object.__new__(RegistryReader)
    reader._pdf_occurrences_from_snapshot = lambda _snapshot: [{
        "occurrence_id": "page-14", "page": 14, "summary": "PDF page summary"}]
    monkeypatch.setattr("climate_registry.publication.approved_display", lambda _snapshot: {
        "title": "Approved article title", "summary": "Approved article summary",
        "summary_provenance": "verified_source_evidence", "categories": ["Climate risk"],
        "keywords": ["insurance"], "metadata_provenance": {"categories": "verified", "keywords": "verified"}})
    snapshot = {"entity_kind": "pdf_article", "entity_id": "pdf-a", "_approved_candidate_sha256": "a" * 64,
        "tables": {"pdf_intake_articles": [{"article_id": "pdf-a", "canonical_url": "https://example.org/a",
            "title": "PDF title"}], "articles": []}, "supplements": []}
    detail = reader._pdf_article_from_snapshot(snapshot)
    assert detail["summary"] == "Approved article summary"
    assert detail["summary_provenance"] == "verified_source_evidence"
    assert detail["categories"] == ["Climate risk"] and detail["keywords"] == ["insurance"]


def test_bundled_161_annotations_preserve_private_archive_payloads(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "historical-registry.sqlite3"
    build_audit_registry(root / "sources", database, tmp_path / "audit")
    annotations = load_article_annotations(root / "article_metadata")
    _approve_fixture(database,root/"article_metadata",root/"sources")
    reader = RegistryReader(
        database,
        repository_root=root,
        source_dir=root / "sources",
        metadata_dir=root / "article_metadata", public=False,
    )

    observed = {}
    reports = sorted(
        parse_report_directory(root / "sources", allow_offcycle=True),
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
    assert [item["article_id"] for item in first["items"]] == ["article-excerpt", "article-full"]
    assert [item["article_id"] for item in second["items"]] == ["article-meta"]
    cards = [item for payload in (first, second) for item in payload["items"]]
    assert all(item["publication_date"] is None for item in cards)
    assert all(item["collected_at"] == "2026-08-13T12:00:00Z" for item in cards)


def test_pdf_intake_articles_and_calendar_are_queryable_without_fabricating_core_lineage(registry_client):
    client, database = registry_client
    _insert_pdf_intake_records(database)

    listing = client.get(
        "/api/registry/pdf-intake/articles",
        params={"query": "wildfire", "source": "www.example.com", "report_date": "2026-09-03"},
    )
    assert listing.status_code == 200
    assert [item["article_id"] for item in listing.json()["items"]] == ["pdf-article-unique"]
    assert listing.json()["items"][0]["source_kind"] == "pdf"
    assert listing.json()["items"][0]["occurrence_count"] == 1
    assert "pdf-article-core" not in {
        item["article_id"] for item in client.get("/api/registry/pdf-intake/articles").json()["items"]
    }
    core_listing = client.get("/api/registry/articles").json()["items"]
    assert next(item for item in core_listing if item["article_id"] == "article-full")["pdf_occurrence_count"] == 1

    detail = client.get("/api/registry/pdf-intake/articles/pdf-article-unique")
    assert detail.status_code == 200
    assert detail.json()["type_safe_classification"] == {"provider": "typesafe", "label": "article"}
    assert detail.json()["occurrences"][0]["raw_url"].endswith("utm_source=pdf")
    assert "source_observations" not in detail.json()["occurrences"][0]
    assert "C:/input/report.pdf" not in detail.text
    assert client.get("/api/registry/articles/article-full").json()["pdf_occurrences"][0]["occurrence_id"] == "pdf-occurrence-core"

    calendar = client.get(
        "/api/registry/pdf-intake/calendar", params={"query": "research launch", "kind": "publication"}
    )
    assert calendar.status_code == 200
    assert calendar.json()["items"][0]["kind"] == "publication"
    assert calendar.json()["items"][0]["source_kind"] == "pdf"
    assert calendar.json()["items"][0]["source_filename"] == "report.pdf"
    assert calendar.json()["items"][0]["location"] == "Paris, France"
    assert calendar.json()["items"][0]["online_url"] == "https://example.com/meeting-room"
    assert calendar.json()["items"][0]["page"] == 4
    assert calendar.json()["items"][0]["source_document_sha256"] == "f" * 64
    assert "source_observations" not in calendar.json()["items"][0]
    assert "C:/input/report.pdf" not in calendar.text
    raw = RegistryReader(database, repository_root=ROOT, public=False)
    assert raw.pdf_article("pdf-article-unique")["occurrences"][0]["source_observations"][0]["path"] == "C:/input/report.pdf"
    assert raw.pdf_calendar_items()["items"][0]["source_observations"][0]["path"] == "C:/input/report.pdf"


def test_direct_public_pdf_readers_preserve_frozen_source_and_meeting_contracts(registry_client, monkeypatch):
    import climate_registry.publication as publication

    _, database = registry_client
    _insert_pdf_intake_records(database)
    reader = RegistryReader(database, repository_root=ROOT)

    def fail_projection(_source):
        raise AssertionError("direct public reads must not open a projected database")

    monkeypatch.setattr(publication, "public_connection", fail_projection)
    source_observation = {
        "path": "C:/input/report.pdf",
        "filename": "report.pdf",
        "observed_at": "2026-09-03T00:00:00Z",
    }

    core = reader.article("article-full")
    assert core["pdf_occurrences"][0]["source_observations"] == [source_observation]
    pdf_article = reader.pdf_article("pdf-article-unique")
    assert pdf_article["occurrences"][0]["source_observations"] == [source_observation]
    assert reader.pdf_articles()["items"][0]["latest_occurrence"]["source_observations"] == [source_observation]
    calendar = reader.pdf_calendar_items()["items"][0]
    assert calendar["source_observations"] == [source_observation]

    meetings = reader.meetings(base_date="1900-01-01")
    assert meetings["items"][0]["source_observations"] == [source_observation]
    assert meetings["items"][0].get("canonical_event_id") is None
    assert meetings["coverage"]["returned_record_count"] == 0
    assert "timezone" not in meetings


def test_combined_article_filters_keep_core_dto_for_a_matching_linked_pdf(registry_client, monkeypatch):
    client, database = registry_client
    _insert_pdf_intake_records(database)

    def fail_projection(_source):
        raise AssertionError("HTTP collection reads must not open a projected database")

    def fail_detail(*_args, **_kwargs):
        raise AssertionError("combined collection reads must not load article details")

    import climate_registry.publication as publication
    monkeypatch.setattr(publication, "public_connection", fail_projection)
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    monkeypatch.setattr(RegistryReader, "article", fail_detail)
    monkeypatch.setattr(RegistryReader, "articles", fail_detail)

    # The approved core summary does not contain “wildfire”; the linked PDF
    # occurrence does, and should make the core article searchable without
    # replacing the core DTO's displayed fields.
    for params in (
        {"query": "wildfire", "include_pdf": "true"},
        {"report_date": "2026-09-03", "include_pdf": "true"},
        {"pillar": "A", "include_pdf": "true"},
    ):
        response = client.get("/api/registry/articles", params=params)
        assert response.status_code == 200
        items = response.json()["items"]
        assert any(item["canonical_url"] == "https://example.com/full" for item in items), (params,items)
        linked = next(item for item in items if item["canonical_url"] == "https://example.com/full")
        assert linked["article_id"] == "article-full"
        assert linked["source_kind"] == "registry"
        assert linked["title"] == "Climate: 100% transition"
        assert linked["source_label"] == "Registry · PDF import"
        assert linked["pdf_occurrence_count"] == 1


@pytest.mark.parametrize("count", [1, 100, 229])
def test_approved_article_collection_batches_candidate_reads_without_details(tmp_path, monkeypatch, count):
    import climate_registry.publication as publication

    database = tmp_path / "external" / "registry.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        apply_migrations(db)
        db.execute("INSERT INTO sources VALUES('scale-source','scale.example','Scale Publisher','2026-09-01','2026-09-01')")
        for index in range(count):
            identity = f"article-{index:03d}"
            version = f"version-{index:03d}"
            url = f"https://scale.example/{index}"
            db.execute("INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen) VALUES(?,?,'scale-source','2026-09-01','2026-09-01')",
                (identity, url))
            db.execute("INSERT INTO article_versions VALUES(?,?,?,?,?,?,'report-title-summary','2026-09-01','2026-09-01')",
                (version, identity, f"Article {index}", f"Article {index}", "Approved collection fixture", f"fingerprint-{index}"))
            db.execute("UPDATE articles SET current_version_id=? WHERE article_id=?", (version, identity))
        from climate_registry.publication import _approve, stage_entities
        from verified_fixtures import seed_verified_articles
        seed_verified_articles(db)
        for sha in stage_entities(db):
            _approve(db, sha, {"basis": "accepted collection scale fixture"}, status="accepted_legacy")

    publication_calls = []
    original_selection = publication.published_snapshots

    def counted_selection(*args, **kwargs):
        publication_calls.append((args[1], kwargs.get("entity_ids", args[2] if len(args) > 2 else None)))
        return original_selection(*args, **kwargs)

    monkeypatch.setattr(publication, "published_snapshots", counted_selection)
    monkeypatch.setattr(publication, "public_connection", lambda _source: pytest.fail("article list created a projection"))
    monkeypatch.setattr(RegistryReader, "article", lambda *_args, **_kwargs: pytest.fail("article collection loaded a detail"))
    reader = RegistryReader(database, repository_root=tmp_path / "application")

    result = reader.articles(page=1, page_size=20, query="Article")
    assert result["pagination"]["total"] == count
    assert len(result["items"]) == min(count, 20)
    assert len(publication_calls) == 1
    assert all(item["current_version_id"].startswith("version-") for item in result["items"])
    assert all(item["record_reference"]["candidate_sha256"] == item["published_candidate_sha256"]
        for item in result["items"])


def test_pdf_http_helpers_share_direct_reads_and_preserve_report_sources(registry_client, monkeypatch):
    client, database = registry_client
    _insert_pdf_intake_records(database)

    def fail_projection(_source):
        raise AssertionError("public HTTP helpers must not create a projected database")

    import climate_registry.publication as publication
    monkeypatch.setattr(publication, "public_connection", fail_projection)
    assert client.get("/api/registry/meetings").status_code == 200
    assert client.get("/api/registry/pdf-intake/articles").status_code == 200
    assert client.get("/api/registry/pdf-intake/articles/pdf-article-unique").status_code == 200
    assert client.get("/api/registry/pdf-intake/calendar").status_code == 200
    monkeypatch.setattr(
        api_server, "_pdf_registry_view",
        lambda: pytest.fail("current Registry detail must not resolve the legacy overlay"),
    )
    report = client.get("/api/registry/pdf-intake/reports/" + "f" * 64)
    assert report.status_code == 200
    assert report.json()["source_filenames"] == ["report.pdf"]


def test_pdf_document_cache_checks_and_original_bytes_are_reused_per_request(registry_client, monkeypatch):
    import types
    import climate_registry.information_checks as information_checks
    import climate_registry.publication as publication
    import climate_registry.read_api as read_api

    client, database = registry_client
    _insert_pdf_intake_records(database)
    original_pdf = b"%PDF-1.7\nissue221 approved source bytes\n%%EOF\n"
    document_sha = hashlib.sha256(original_pdf).hexdigest()
    document_json = json.dumps({"title": "Issue 221 PDF", "page_count": 2,
        "pages": [{"page": 1, "text": "Approved first page"}, {"page": 2, "text": "Approved second page"}]})
    with sqlite3.connect(database) as db:
        db.execute("""INSERT INTO pdf_intake_documents(
            document_sha256,source_path,filename,media_type,size_bytes,extracted_text_sha256,
            document_json,imported_at,period_start,period_end,date_of_run,original_pdf)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (document_sha, "C:/input/issue221.pdf", "issue221.pdf", "application/pdf", len(original_pdf),
             "e" * 64, document_json, "2026-09-03T00:00:00Z", "2026-09-01", "2026-09-02",
             "2026-09-03", original_pdf))
        db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES(?,?,?,?)",
            ("pdf-byte-article", "https://example.net/issue221", "Issue 221 Article", "2026-09-03T00:00:00Z"))
        for ordinal in (1, 2):
            occurrence_id = f"pdf-byte-occurrence-{ordinal}"
            occurrence = {"occurrence_id": occurrence_id, "source_document_sha256": document_sha,
                "page": ordinal, "raw_url": "https://example.net/issue221", "report_date": "2026-09-03",
                "title": "Issue 221 Article", "summary": "Frozen PDF article observation",
                "summary_basis": "verbatim_pdf_article_row", "content_sha256": str(ordinal) * 64}
            db.execute("""INSERT INTO pdf_intake_article_occurrences(
                occurrence_id,article_id,source_document_sha256,page,raw_url,report_date,
                publication_date,content_sha256,page_sha256,occurrence_json)
                VALUES(?,?,?,?,?,?,NULL,?,?,?)""",
                (occurrence_id, "pdf-byte-article", document_sha, ordinal, occurrence["raw_url"],
                 occurrence["report_date"], occurrence["content_sha256"], str(ordinal + 2) * 64,
                 json.dumps(occurrence)))
        from climate_registry.publication import _approve, stage_entities
        from verified_fixtures import seed_verified_articles
        seed_verified_articles(db)
        for sha in stage_entities(db):
            _approve(db, sha, {"basis": "accepted Issue 221 PDF fixture"}, status="accepted_legacy")

    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(publication, "public_connection", lambda _source: pytest.fail("PDF read created a projection"))

    document_decodes = 0
    original_loads = read_api.json.loads

    def counted_loads(value, *args, **kwargs):
        nonlocal document_decodes
        if value == document_json:
            document_decodes += 1
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(read_api.json, "loads", counted_loads)
    check_calls = 0
    original_checks = information_checks.latest_checks_from_attempts

    def counted_checks(*args, **kwargs):
        nonlocal check_calls
        check_calls += 1
        return original_checks(*args, **kwargs)

    monkeypatch.setattr(information_checks, "latest_checks_from_attempts", counted_checks)
    detail_reader = RegistryReader(database, repository_root=ROOT)
    detail = detail_reader.pdf_article("pdf-byte-article")
    assert len(detail["occurrences"]) == 2
    assert document_decodes == 1
    assert check_calls == 2

    document_decodes = 0
    statements = []
    original_connect = read_api.sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        if str(args[0]).startswith(database.as_uri()):
            connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(read_api.sqlite3, "connect", traced_connect)
    helper_hashes = 0
    route_hashes = 0
    original_sha256 = hashlib.sha256

    def helper_sha256(value=b"", *args, **kwargs):
        nonlocal helper_hashes
        if value == original_pdf:
            helper_hashes += 1
        return original_sha256(value, *args, **kwargs)

    def route_sha256(value=b"", *args, **kwargs):
        nonlocal route_hashes
        if value == original_pdf:
            route_hashes += 1
        return original_sha256(value, *args, **kwargs)

    monkeypatch.setattr(read_api, "hashlib", types.SimpleNamespace(sha256=helper_sha256))
    monkeypatch.setattr(api_server, "hashlib", types.SimpleNamespace(sha256=route_sha256))
    response = client.get(f"/api/registry/pdf-intake/reports/{document_sha}/pdf")
    assert response.status_code == 200
    assert response.content == original_pdf
    assert document_decodes == 1
    assert helper_hashes == 1
    assert route_hashes == 0
    assert sum("SELECT original_pdf FROM pdf_intake_documents" in statement for statement in statements) == 1


def test_hidden_direct_wiki_page_cannot_fall_through_to_active_pdf_layer(
    registry_client, monkeypatch, tmp_path
):
    client, database = registry_client
    from climate_registry.publication import set_visibility

    base = tmp_path / "weekly-wiki"
    runtime = tmp_path / "active-pdf-generation"
    base.mkdir()
    runtime.mkdir()
    page = "article-article-full.md"
    (base / page).write_text("# Previous approved core page\n", encoding="utf-8")
    (runtime / page).write_text("# Activated PDF overlay\n\nprivate PDF text\n", encoding="utf-8")
    monkeypatch.setattr(api_server, "WIKI_DIR", base)
    monkeypatch.setattr(
        api_server._wiki_static_files, "all_directories", [str(runtime), str(base)]
    )
    set_visibility(database, "article", "article-full", False)

    for method in (client.get, client.head):
        response = method("/wiki/" + page)
        assert response.status_code == 404
        assert "private PDF text" not in response.text


def test_pdf_publishers_keep_existing_core_host_label(registry_client):
    client, database = registry_client
    _insert_pdf_intake_records(database)

    response = client.get("/api/registry/publishers", params={"include_pdf": "true"})
    assert response.status_code == 200
    publishers = response.json()["items"]
    example = [item for item in publishers if item["hostname"] == "example.com"]
    assert example == [{"hostname": "example.com", "label": "example.com"}]


def test_legacy_pdf_publisher_fallback_uses_host_default_for_shared_site(
    registry_client, monkeypatch,
):
    from contextlib import nullcontext

    client, database = registry_client
    _insert_pdf_intake_records(database, unique_url="https://bis.org/publ/bcbs239.pdf")
    # Exercise the slower include-PDF fallback instead of the optimized projection.
    monkeypatch.setattr(api_server, "_registry_list_projection", lambda *args, **kwargs: nullcontext(None))

    articles = client.get("/api/registry/articles", params={"include_pdf": "true"})
    publishers = client.get("/api/registry/publishers", params={"include_pdf": "true"})

    assert articles.status_code == 200
    pdf_article = next(item for item in articles.json()["items"] if item["article_id"] == "pdf-article-unique")
    assert pdf_article["publisher"] == "The Basel Committee on Banking Supervision (BCBS)"
    assert publishers.status_code == 200
    bis = [item for item in publishers.json()["items"] if item["hostname"] == "bis.org"]
    assert bis == [{"hostname": "bis.org", "label": "Bank for International Settlements (BIS)"}]


def test_pdf_read_paths_remain_available_for_supported_v12_registry(tmp_path, monkeypatch):
    database = _registry(tmp_path, target_version=12)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    client = TestClient(app)

    articles = client.get("/api/registry/pdf-intake/articles")
    calendar = client.get("/api/registry/pdf-intake/calendar")
    detail = client.get("/api/registry/articles/article-full")
    missing_pdf_article = client.get("/api/registry/pdf-intake/articles/pdf-article-unique")

    assert articles.status_code == 200
    assert articles.json()["items"] == []
    assert calendar.status_code == 200
    assert calendar.json()["items"] == []
    assert detail.status_code == 200
    assert "pdf_occurrences" not in detail.json()
    assert missing_pdf_article.status_code == 404


def test_pdf_read_api_uses_legacy_source_columns_on_v13_registry(tmp_path, monkeypatch):
    database = _registry(tmp_path, target_version=13)
    _insert_pdf_intake_records(database)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    client = TestClient(app)

    detail = client.get("/api/registry/pdf-intake/articles/pdf-article-unique")
    calendar = client.get("/api/registry/pdf-intake/calendar")
    report = client.get("/api/registry/pdf-intake/reports/" + "f" * 64)

    assert detail.status_code == 200
    assert "source_observations" not in detail.json()["occurrences"][0]
    assert "C:/input/report.pdf" not in detail.text
    assert calendar.status_code == 200
    assert "source_observations" not in calendar.json()["items"][0]
    assert report.status_code == 200
    assert report.json()["source_filenames"] == ["report.pdf"]
    assert "C:/input/report.pdf" not in calendar.text
    raw = RegistryReader(database, repository_root=ROOT, public=False)
    assert raw.pdf_article("pdf-article-unique")["occurrences"][0]["source_observations"][0]["path"] == "C:/input/report.pdf"
    assert raw.pdf_calendar_items()["items"][0]["source_observations"][0]["path"] == "C:/input/report.pdf"


def test_pillar_and_report_date_must_match_the_same_appearance(registry_client):
    client, database = registry_client
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE report_appearances SET pillar = 'B' WHERE discovery_id = 'discovery-new-1'"
    )
    connection.execute("UPDATE discoveries SET pillar = 'B' WHERE discovery_id = 'discovery-new-1'")
    connection.commit()
    connection.close()
    _approve_fixture(database)

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
    client, database = registry_client
    response = client.get(f"/api/registry/articles/{article_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["display_policy"] == policy
    assert bool(payload["enrichment"].get("summary")) is True
    assert bool(payload["content"].get("supporting_excerpt")) is has_excerpt
    assert bool(payload["content"].get("markdown")) is has_markdown
    if has_excerpt:
        assert len(payload["content"]["supporting_excerpt"]) <= 500
    assert "content_sha256" not in payload["content"]
    available = payload.get("available_content") or {}
    if available.get("content_version_id"):
        with sqlite3.connect(database) as connection:
            actual = connection.execute("SELECT content_sha256 FROM article_content_versions WHERE content_version_id=? AND article_id=?",
                (available["content_version_id"], article_id)).fetchone()[0]
        assert available["content_sha256"] == actual
    else:
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


def test_article_detail_exposes_collection_and_page_information_dates(registry_client):
    client, database = registry_client
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO articles(
                   article_id, canonical_url, source_id, first_seen, last_seen,
                   document_kind, publication_eligible, display_policy
               ) VALUES ('article-no-fetch', 'https://insurer.test/no-fetch',
                   'source-b', '2026-08-10', '2026-08-10', 'article', 1,
                   'metadata_only')"""
        )
        connection.executemany(
            """INSERT INTO article_date_observations VALUES
               (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (
                    "collection-observation", "article-full", "https://example.com/full",
                    "collection", "2026-08-12T09:00:00Z", "web_listening", "history.sqlite3",
                    "site_snapshots", "snapshot-12",
                    json.dumps({"match_basis": "snapshot_final_url"}), "2026-08-20T00:00:00Z",
                ),
                (
                    "page-information-observation", "article-no-fetch",
                    "https://insurer.test/no-fetch",
                    "page_information", "2026-08-11", "publisher_page", "manual-review.json",
                    "article_dates", "page-meta-11",
                    json.dumps({"date_kind": "updated", "match_basis": "publisher_date_field"}),
                    "2026-08-20T00:00:00Z",
                ),
            ),
        )

    _approve_fixture(database)
    collected = client.get("/api/registry/articles/article-full").json()
    assert collected["collected_at"] == "2026-08-13T12:00:00Z"
    assert collected["information_date"] is None
    assert collected["date_basis"] == "collection_time"
    assert collected["date_observations"] == []
    private=RegistryReader(database,repository_root=ROOT,public=False)
    assert [item['observed_at'] for item in private.article('article-full')['date_observations']]==['2026-08-12T09:00:00Z']

    page_dated = client.get("/api/registry/articles/article-no-fetch").json()
    assert page_dated["collected_at"] is None
    assert page_dated["information_date"] is None
    assert page_dated["date_basis"] == "unknown"
    assert page_dated["date_observations"] == []
    assert private.article('article-no-fetch')["date_observations"][0]["evidence"]["date_kind"] == "updated"


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
    # Raw historical contract: current published versions retain their frozen report dependencies.
    database = _registry(tmp_path,target_version=20)
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


def test_exact_v3_remains_readable_during_v4_deployment_window(tmp_path):
    database = tmp_path / "registry-v3.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection, target_version=3)
    connection.close()

    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1])
    assert reader.status() == {
        "available": True,
        "schema_version": 3,
        "reports": 0,
        "articles": 0,
        "discoveries": 0,
        "latest_report_date": None,
    }


def test_v4_missing_resolution_immutability_trigger_is_rejected(tmp_path):
    database = tmp_path / "registry-v4-corrupt.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection)
    connection.execute(
        "DROP TRIGGER article_capture_resolutions_are_append_only_update"
    )
    connection.commit()
    connection.close()

    reader = RegistryReader(database, repository_root=Path(__file__).resolve().parents[1])
    with pytest.raises(RegistryContractError):
        reader.status()


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
@pytest.mark.parametrize("value", ("9" * 10_000, "", "+1", "-1", " 1", "1 ", "١"))
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
        (method, path)
        for route in app.routes
        if (path := getattr(route, "path", "")).startswith("/api/registry")
        for method in (route.methods or set())
    }
    assert all(method == "GET" for method, _ in registry_routes)


def test_frontend_exposes_safe_registry_workspace_without_operations():
    root = Path(__file__).resolve().parents[1]
    index = (root / "showcase" / "index.html").read_text(encoding="utf-8")
    script = (root / "showcase" / "app.js").read_text(encoding="utf-8")
    assert 'data-view="registryView"' in index
    assert 'aria-selected="false">Reports</button>' in index
    assert 'data-registry-mode="articles" aria-pressed="false">\n                Articles\n' in index
    assert "/api/registry/reports" in script
    assert "/api/registry/articles" in script
    assert "registryMarkdown.textContent" in script
    assert "function registrySummaryPresentation(article)" in script
    article_list = script.split("async function loadRegistryArticles()", 1)[1].split("function appendPdfAppearances", 1)[0]
    assert "registry-card__summary" not in article_list
    assert "registrySummaryPresentation(article)" not in article_list
    assert "data-article-source=" not in index
    assert "include_pdf" in article_list
    assert script.index("if (article.summary)") < script.index("else if (article.enrichment?.summary)")
    assert script.index("else if (article.enrichment?.summary)") < script.index(
        "else if (article.report_summary)"
    )
    assert 'provenance = "source_report"' in script
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
