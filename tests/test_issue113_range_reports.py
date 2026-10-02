from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

import api_server
import climate_registry.range_reports as range_reports
from climate_registry.range_reports import (
    RENDERER_VERSION,
    RangeReportError,
    ensure_range_report_pdf,
    freeze_range_report,
    load_range_report,
    pdf_path,
    render_range_report_html,
    render_range_report_pdf,
    resolve_report_route,
)
from climate_registry.read_api import RegistryReader
from climate_registry.schema import apply_migrations


NOW = "2026-09-30T12:00:00Z"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _database(tmp_path: Path, *, b_published: str = "2026-09-25") -> Path:
    database = tmp_path / "registry.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection)
    connection.execute(
        "INSERT INTO sources VALUES ('source', 'example.org', 'Example Institute', ?, ?)",
        (NOW, NOW),
    )
    connection.execute(
        """INSERT INTO acquisition_batches VALUES
           ('batch', 'pre-report-acquisition-batch.v1', '2026-09-30', ?, ?,
            '{"mode":"unlimited"}', 'no_search', 'site-only', ?, ?)""",
        (NOW, NOW, "a" * 64, NOW),
    )

    def article(article_id: str, url: str, title: str, body: str, *, policy: str = "full_markdown"):
        connection.execute(
            """INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen,
                                      document_kind, publication_eligible, display_policy)
               VALUES (?, ?, 'source', ?, ?, 'article', 1, ?)""",
            (article_id, url, NOW, NOW, policy),
        )
        version_id = "version-" + article_id
        connection.execute(
            """INSERT INTO article_versions VALUES (?, ?, ?, ?, ?, ?,
                       'report-title-summary', ?, ?)""",
            (version_id, article_id, title, title.casefold(), "Observed summary",
             _sha(title + "Observed summary"), NOW, NOW),
        )
        content_id = "content-" + article_id
        digest = _sha(body)
        connection.execute(
            """INSERT INTO article_content_versions VALUES
               (?, ?, ?, ?, ?, 'text/markdown', ?, 'reader', 'reader-v1', ?)""",
            (content_id, article_id, digest, body, digest, len(body.encode()), NOW),
        )
        connection.execute(
            "UPDATE articles SET current_version_id=?, current_content_version_id=? WHERE article_id=?",
            (version_id, content_id, article_id),
        )
        connection.execute(
            """INSERT INTO article_enrichments VALUES
               (?, ?, 'complete', ?, ?, ?, 'en', 'deterministic', 'fixture', 'semantic-v3',
                ?, NULL, NULL)""",
            ("enrichment-" + article_id, content_id, f"Summary for {title} — café μ",
             json.dumps(["Physical risk"]), json.dumps(["pricing", "resilience"]), NOW),
        )
        return content_id

    content_a = article(
        "article-a", "https://example.org/a", "Registry-only climate article — café μ",
        ("Long persisted climate evidence with Unicode café μ and page-safe text. " * 35 + "\n\n") * 4,
    )
    content_b = article("article-b", "https://example.org/b", "Second article", "Short body")
    content_unknown = article(
        "article-unknown", "https://example.org/unknown", "Unknown date article", "Unknown body"
    )
    article(
        "article-month", "https://example.org/month", "Month precision article", "Month body"
    )

    def observation(
        item_id: str, ordinal: int, article_id: str, url: str, content_id: str,
        published: str | None, *, second_url: str | None = None,
    ):
        fetch_id = "fetch-" + item_id
        connection.execute(
            """INSERT INTO article_fetches(
                   fetch_id, article_id, requested_url, final_url, fetched_at, fetch_status,
                   http_status, content_type, content_version_id)
               VALUES (?, ?, ?, ?, ?, 'success', 200, 'text/markdown', ?)""",
            (fetch_id, article_id, url, url, NOW, content_id),
        )
        origins = [{"url": url, "source": "site"}]
        if second_url:
            origins.append({"url": second_url, "source": "search"})
        evidence = (
            json.dumps({"kind": "publisher", "url": url, "text": f"Published {published}"})
            if published else None
        )
        connection.execute(
            """INSERT INTO acquisition_items(
                   acquisition_item_id, batch_id, ordinal, article_id, raw_url, source_name,
                   title, summary, discovered_at, discovery_kind, discovery_ref, origins_json,
                   publication_date, publication_date_evidence_json, date_status,
                   selection_status, selection_reason, update_status, material_status,
                   fetch_id, content_version_id, attempts_json, processing_status)
               VALUES (?, 'batch', ?, ?, ?, 'Example Institute', ?, ?, ?, 'site', ?, ?, ?, ?, ?,
                       'selected', 'relevant', 'baseline', 'full_content', ?, ?, '[]', 'complete')""",
            (item_id, ordinal, article_id, url, f"Observed {article_id}",
             f"Observation summary {article_id}", NOW, "site-ref-" + item_id,
             json.dumps(origins), published, evidence,
             "eligible" if published else "unknown_pending_review", fetch_id, content_id),
        )

    observation(
        "a-1", 1, "article-a", "https://example.org/a?source=site", content_a,
        "2026-09-20", second_url="https://mirror.example/a",
    )
    observation("a-2", 2, "article-a", "https://example.org/a?source=search", content_a, "2026-09-20")
    observation("b-1", 3, "article-b", "https://example.org/b", content_b, b_published)
    observation(
        "unknown-1", 4, "article-unknown", "https://example.org/unknown", content_unknown, None
    )

    pdf_bytes = b"%PDF-1.4 fixture"
    document_sha = hashlib.sha256(pdf_bytes).hexdigest()
    connection.execute(
        """INSERT INTO pdf_intake_documents(
               document_sha256, source_path, filename, media_type, size_bytes, original_pdf,
               extracted_text_sha256, document_json, imported_at)
           VALUES (?, 'C:/input/report.pdf', 'report.pdf', 'application/pdf', ?, ?, ?, '{}', ?)""",
        (document_sha, len(pdf_bytes), pdf_bytes, "b" * 64, NOW),
    )
    connection.execute(
        "INSERT INTO pdf_intake_document_sources VALUES (?, 'C:/input/report.pdf', 'report.pdf', ?)",
        (document_sha, NOW),
    )
    connection.execute(
        """INSERT INTO pdf_intake_articles(
               article_id, canonical_url, title, type_safe_classification_json, imported_at,
               core_article_id, confirmation_basis)
           VALUES ('pdf-a', 'https://example.org/a', 'PDF A',
                   '{"provider":"typesafe","label":"article"}', ?, 'article-a',
                   'exact_url_eligible_detail')""",
        (NOW,),
    )
    connection.execute(
        """INSERT INTO pdf_intake_articles(
               article_id, canonical_url, title, type_safe_classification_json, imported_at,
               core_article_id, confirmation_basis)
           VALUES ('pdf-unconfirmed', 'https://example.org/home', 'Homepage PDF',
                   '{"provider":"typesafe","label":"landing_page"}', ?, NULL, NULL)""",
        (NOW,),
    )
    connection.execute(
        """INSERT INTO pdf_intake_articles(
               article_id, canonical_url, title, type_safe_classification_json, imported_at,
               core_article_id, confirmation_basis)
           VALUES ('pdf-month', 'https://example.org/month', 'PDF month',
                   '{"provider":"typesafe","label":"article"}', ?, 'article-month',
                   'exact_url_eligible_detail')""",
        (NOW,),
    )
    occurrence = {
        "occurrence_id": "pdf-occ-a", "anchor_text": "PDF A",
        "summary": "PDF observation", "publication_date_evidence": "20 SEP 2026",
    }
    connection.execute(
        """INSERT INTO pdf_intake_article_occurrences VALUES
           ('pdf-occ-a', 'pdf-a', ?, 7, 'https://example.org/a.pdf', NULL,
            '2026-09-20', ?, ?, ?)""",
        (document_sha, "c" * 64, "d" * 64, json.dumps(occurrence)),
    )
    month_occurrence = {
        "occurrence_id": "pdf-occ-month", "anchor_text": "PDF month",
        "summary": "Month observation", "publication_date_evidence": "September 2026",
    }
    connection.execute(
        """INSERT INTO pdf_intake_article_occurrences VALUES
           ('pdf-occ-month', 'pdf-month', ?, 9, 'https://example.org/month.pdf', NULL,
            '2026-09', ?, ?, ?)""",
        (document_sha, "e" * 64, "f" * 64, json.dumps(month_occurrence)),
    )
    unconfirmed_occurrence = {
        "occurrence_id": "pdf-occ-unconfirmed", "anchor_text": "Homepage PDF",
        "summary": "Unconfirmed landing-page observation",
        "publication_date_evidence": "22 SEP 2026",
    }
    connection.execute(
        """INSERT INTO pdf_intake_article_occurrences VALUES
           ('pdf-occ-unconfirmed', 'pdf-unconfirmed', ?, 11, 'https://example.org/home', NULL,
            '2026-09-22', ?, ?, ?)""",
        (document_sha, "1" * 64, "2" * 64, json.dumps(unconfirmed_occurrence)),
    )
    connection.commit()
    connection.close()
    return database


def _reader(database: Path, tmp_path: Path) -> RegistryReader:
    return RegistryReader(database, repository_root=tmp_path / "application")


def test_chat_date_routing_is_bounded_and_server_validated(monkeypatch):
    assert resolve_report_route(
        "Generate the report for the last 14 days", today=date(2026, 9, 30),
        typesafe_router=lambda _: {"action": "generate_registry_report", "date_fields": "last_14_days"},
    ) == range_reports.ReportRoute("generate", "2026-09-17", "2026-09-30")
    assert resolve_report_route(
        "Create a report from 2026-09-01 to 2026-09-14", today=date(2026, 9, 30),
        typesafe_router=lambda _: {"action": "generate_registry_report", "date_fields": "start_and_end"},
    ).start_date == "2026-09-01"
    recent = resolve_report_route(
        "A climate report for the recent 30 days", today=date(2026, 9, 30),
        typesafe_router=lambda _: {"action": "generate_registry_report", "date_fields": "recent_days"},
    )
    assert (recent.start_date, recent.end_date) == ("2026-09-01", "2026-09-30")
    assert resolve_report_route(
        "Create a report for 2026-09-01", today=date(2026, 9, 30), typesafe_router=lambda _: None,
    ).clarification.startswith("Please provide both")
    assert "valid dates" in resolve_report_route(
        "Create a report 2026-02-30 to 2026-03-01",
        today=date(2026, 9, 30), typesafe_router=lambda _: None,
    ).clarification
    assert resolve_report_route(
        "What is parametric insurance?", today=date(2026, 9, 30), typesafe_router=lambda _: None,
    ).action == "normal_chat"
    assert resolve_report_route(
        "What does the last report say about rainfall?", today=date(2026, 9, 30),
        typesafe_router=lambda _: {"action": "normal_chat", "date_fields": "none"},
    ).action == "normal_chat"
    assert resolve_report_route(
        "What does the last report say about rainfall?", today=date(2026, 9, 30),
        typesafe_router=lambda _: None,
    ).action == "normal_chat"
    needs_dates = resolve_report_route(
        "I need a climate report", today=date(2026, 9, 30), typesafe_router=lambda _: None,
    )
    assert needs_dates.action == "clarify"
    assert "last 14 days" in needs_dates.clarification
    assert resolve_report_route(
        "Generate a climate report", today=date(2026, 9, 30),
        typesafe_router=lambda _: {"action": "generate_registry_report", "date_fields": "incomplete"},
    ).action == "clarify"

    class FakeChoice:
        def __init__(self, *, instructions, criteria):
            assert "URL" in instructions or "Application code" in instructions
            self.criteria = criteria

    class FakeClient:
        def __init__(self, *, api_key, timeout):
            assert api_key == "fixture-key" and timeout == 20.0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def system_one(self, state, questions):
            assert state == {"message": "Generate a report for the last 14 days"}
            assert set(questions["action"].criteria) == range_reports._ROUTE_CHOICES
            assert set(questions["date_fields"].criteria) == {
                "none", "last_14_days", "recent_days", "start_and_end", "incomplete"
            }
            return SimpleNamespace(choices={
                "action": SimpleNamespace(choice="generate_registry_report"),
                "date_fields": SimpleNamespace(choice="last_14_days"),
            })

    monkeypatch.setitem(sys.modules, "typesafe_sdk", SimpleNamespace(Choice=FakeChoice, TypeSafeClient=FakeClient))
    monkeypatch.setenv("TYPESAFE_API_KEY", "fixture-key")
    assert range_reports._typesafe_route("Generate a report for the last 14 days") == {
        "action": "generate_registry_report", "date_fields": "last_14_days"
    }


def test_registry_snapshot_dedupes_and_freezes_evidenced_provenance(tmp_path):
    database = _database(tmp_path)
    root = tmp_path / "range-output"
    before = (hashlib.sha256(database.read_bytes()).hexdigest(), database.stat().st_mtime_ns)
    snapshot = freeze_range_report(
        _reader(database, tmp_path), root, start_date="2026-09-17", end_date="2026-09-30"
    )
    assert (hashlib.sha256(database.read_bytes()).hexdigest(), database.stat().st_mtime_ns) == before

    assert [item["article_id"] for item in snapshot["articles"]] == ["article-a", "article-b"]
    assert snapshot["unknown_publication_date_count"] == 2
    assert snapshot["unknown_publication_date_article_ids"] == ["article-month", "article-unknown"]
    assert "pdf-unconfirmed" not in {item["article_id"] for item in snapshot["articles"]}
    assert "pdf-unconfirmed" not in snapshot["unknown_publication_date_article_ids"]
    article = snapshot["articles"][0]
    assert article["publication_date"] == "2026-09-20"
    assert article["content_version_id"] == "content-article-a"
    assert article["provenance"]["summary"]["enrichment_id"] == "enrichment-article-a"
    assert article["provenance"]["summary"]["generator"]["version"] == "semantic-v3"
    assert {item["kind"] for item in article["source_observations"]} == {
        "registry_acquisition", "registry_pdf"
    }
    assert {item["url"] for item in article["citations"] if item["kind"] == "url"} == {
        "https://example.org/a?source=site",
        "https://example.org/a?source=search",
        "https://mirror.example/a",
    }
    assert any(item["kind"] == "pdf_page" and item["page"] == 7 for item in article["citations"])
    assert sqlite3.connect(database).execute("SELECT COUNT(*) FROM reports").fetchone() == (0,)

    loaded = load_range_report(root, snapshot["snapshot_id"])
    assert loaded["snapshot_sha256"] == snapshot["snapshot_sha256"]
    repeated = freeze_range_report(
        _reader(database, tmp_path), root, start_date="2026-09-17", end_date="2026-09-30"
    )
    assert repeated["created_at"] == snapshot["created_at"]
    page = render_range_report_html(loaded)
    assert "Article ID:</strong> <code>article-a</code>" in page
    assert "content-article-a" in page and "https://example.org/a.pdf" in page
    pdf_text = "\n".join(
        value.extract_text() or "" for value in PdfReader(pdf_path(root, snapshot["snapshot_id"])).pages
    )
    assert "article-a" in pdf_text and "content-article-a" in pdf_text
    assert "https://example.org/a.pdf" in pdf_text
    escaped = dict(loaded)
    escaped["articles"] = [dict(loaded["articles"][0], title="<script>alert(1)</script>")]
    escaped_page = render_range_report_html(escaped)
    assert "<script>" not in escaped_page and "&lt;script&gt;" in escaped_page
    assert RENDERER_VERSION not in json.loads(
        (root / snapshot["snapshot_id"] / "snapshot.json").read_text()
    )


def test_pdf_source_updates_use_coverage_without_claiming_publication_dates(tmp_path):
    database = _database(tmp_path, b_published="2026-09-01")
    connection = sqlite3.connect(database)

    def pdf_article(pdf_id, url, *, core_id=None, confirmed=False):
        connection.execute(
            """INSERT INTO pdf_intake_articles(
                   article_id, canonical_url, title, type_safe_classification_json, imported_at,
                   core_article_id, confirmation_basis)
               VALUES (?, ?, ?, '{"provider":"typesafe","label":"article"}', ?, ?, ?)""",
            (pdf_id, url, pdf_id, NOW, core_id,
             "exact_url_eligible_detail" if confirmed else None),
        )

    def occurrence(occurrence_id, pdf_id, sha, page, url, summary):
        connection.execute(
            """INSERT INTO pdf_intake_article_occurrences VALUES
               (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)""",
            (occurrence_id, pdf_id, sha, page, url, _sha(summary), _sha(url), json.dumps({
                "occurrence_id": occurrence_id, "anchor_text": pdf_id, "summary": summary,
            })),
        )

    def other_document(marker, filename, start, end):
        sha = marker * 64
        connection.execute(
            """INSERT INTO pdf_intake_documents(
                   document_sha256, source_path, filename, media_type, size_bytes,
                   period_start, period_end, extracted_text_sha256, document_json, imported_at)
               VALUES (?, ?, ?, 'application/pdf', 1, ?, ?, ?, '{}', ?)""",
            (sha, f"C:/input/{filename}", filename, start, end, marker * 63 + "0", NOW),
        )
        return sha

    overlap_sha = other_document(
        "5", "IAA_CSC_Climate_Report_20260928.pdf", "2026-09-14", "2026-09-27"
    )
    unknown_sha = other_document("3", "unknown-period.pdf", None, None)
    outside_sha = other_document("4", "outside-period.pdf", "2026-08-01", "2026-08-31")
    pdf_article(
        "pdf-linked-undated", "https://example.org/unknown",
        core_id="article-unknown", confirmed=True,
    )
    occurrence(
        "pdf-occ-linked-undated", "pdf-linked-undated", overlap_sha, 2,
        "https://example.org/unknown", "Linked PDF-only observation",
    )
    pdf_article(
        "pdf-outside-formal", "https://example.org/b", core_id="article-b", confirmed=True,
    )
    occurrence(
        "pdf-occ-outside-formal", "pdf-outside-formal", overlap_sha, 3,
        "https://example.org/b", "Observation linked to an evidenced formal article",
    )
    connection.execute(
        "UPDATE articles SET publication_eligible=0, document_kind='landing_page' "
        "WHERE article_id='article-b'"
    )
    pdf_article("pdf-only", "https://pdf.example/only")
    occurrence(
        "pdf-occ-only", "pdf-only", overlap_sha, 4,
        "https://pdf.example/only", "Regional stress tests require updated scenarios.",
    )
    occurrence(
        "pdf-occ-only-repeat", "pdf-only", overlap_sha, 4,
        "https://pdf.example/only", "Regional stress tests require updated scenarios.",
    )
    pdf_article("pdf-unknown-period", "https://pdf.example/unknown-period")
    occurrence(
        "pdf-occ-unknown-period", "pdf-unknown-period", unknown_sha, 1,
        "https://pdf.example/unknown-period", "Unknown coverage observation",
    )
    pdf_article("pdf-outside-period", "https://pdf.example/outside-period")
    occurrence(
        "pdf-occ-outside-period", "pdf-outside-period", outside_sha, 1,
        "https://pdf.example/outside-period", "Outside coverage observation",
    )
    connection.commit()
    connection.close()

    root = tmp_path / "range-output"
    snapshot = freeze_range_report(
        _reader(database, tmp_path), root,
        start_date="2026-09-14", end_date="2026-09-27",
    )

    assert [item["article_id"] for item in snapshot["articles"]] == ["article-a"]
    assert len(snapshot["pdf_source_updates"]) == 2
    assert snapshot["pdf_source_exclusion_counts"] == {
        "non_overlapping_coverage": 1,
        "unknown_coverage": 2,
    }
    updates = {item["pdf_article_id"]: item for item in snapshot["pdf_source_updates"]}
    assert set(updates) == {"pdf-linked-undated", "pdf-only"}
    assert updates["pdf-linked-undated"]["core_article_id"] == "article-unknown"
    assert updates["pdf-only"]["core_article_id"] is None
    assert "article-b" not in snapshot["unknown_publication_date_article_ids"]
    example = updates["pdf-only"]
    assert example["publication_date_label"] == "文章发布日期未确认"
    assert example["coverage_period"] == {"start": "2026-09-14", "end": "2026-09-27"}
    assert example["filename"] == "IAA_CSC_Climate_Report_20260928.pdf"
    assert example["page"] == 4 and example["document_sha256"] == overlap_sha
    assert example["url"] == "https://pdf.example/only"
    assert example["summary"] == "Regional stress tests require updated scenarios."
    assert "pdf-outside-formal" not in updates
    assert sum(item["pdf_article_id"] == "pdf-only" for item in snapshot["pdf_source_updates"]) == 1
    assert not any(
        item["observation_id"] == "pdf-occ-a" for item in snapshot["pdf_source_updates"]
    )
    loaded = load_range_report(root, snapshot["snapshot_id"])
    html_report = render_range_report_html(loaded)
    pdf_text = "\n".join(
        page.extract_text() or ""
        for page in PdfReader(pdf_path(root, snapshot["snapshot_id"])).pages
    )
    for rendered in (html_report, pdf_text):
        assert "PDF Source Updates" in rendered
        assert "IAA_CSC_Climate_Report_20260928.pdf" in rendered
        assert "page 4" in rendered
        assert "2026-09-14 through 2026-09-27" in rendered
        assert "Regional stress tests require updated scenarios." in rendered
        assert "https://pdf.example/only" in rendered
    assert "文章发布日期未确认" in html_report
    assert overlap_sha in html_report and overlap_sha in pdf_text
    assert "PDF Source Updates" in pdf_text
    assert "Article publication date unconfirmed" in pdf_text


def test_executive_summary_is_frozen_from_selected_summaries_with_locators(tmp_path):
    database = _database(tmp_path, b_published="2026-09-01")
    connection = sqlite3.connect(database)
    document_sha = hashlib.sha256(b"PDF-only executive summary").hexdigest()
    connection.execute(
        """INSERT INTO pdf_intake_documents(
               document_sha256, source_path, filename, media_type, size_bytes, period_start, period_end,
               extracted_text_sha256, document_json, imported_at)
           VALUES (?, 'C:/input/summary.pdf', 'summary.pdf', 'application/pdf', 1,
                   '2026-09-01', '2026-09-30', ?, '{}', ?)""",
        (document_sha, _sha("x"), NOW),
    )
    connection.execute(
        "INSERT INTO pdf_intake_document_sources VALUES (?, 'C:/input/summary.pdf', 'summary.pdf', ?)",
        (document_sha, NOW),
    )
    connection.execute(
        """INSERT INTO pdf_intake_articles(
               article_id, canonical_url, title, type_safe_classification_json, imported_at,
               core_article_id, confirmation_basis)
           VALUES ('pdf-summary', 'https://pdf.example/summary', 'PDF-only summary',
                   '{\"provider\":\"typesafe\",\"label\":\"article\"}', ?, NULL, NULL)""",
        (NOW,),
    )
    occurrence = {"summary": "Stored PDF-only summary.", "coverage_period": "September 2026"}
    connection.execute(
        """INSERT INTO pdf_intake_article_occurrences VALUES
           ('pdf-summary-occ', 'pdf-summary', ?, 5, 'https://pdf.example/summary', NULL,
            NULL, ?, ?, ?)""",
        (document_sha, _sha("summary"), _sha("summary-url"), json.dumps(occurrence)),
    )
    connection.commit()
    connection.close()

    snapshot = freeze_range_report(
        _reader(database, tmp_path), tmp_path / "range-output",
        start_date="2026-09-01", end_date="2026-09-30",
    )
    points = snapshot["executive_summary"]
    article = next(point for point in points if point["kind"] == "registry_article")
    pdf = next(point for point in points if point["kind"] == "pdf_source")
    assert article["text"] == "Summary for Registry-only climate article — café μ — café μ"
    assert article["article_id"] == "article-a"
    assert article["citations"] == snapshot["articles"][0]["citations"]
    assert pdf == {
        "kind": "pdf_source", "text": "Stored PDF-only summary.", "title": "PDF-only summary",
        "filename": "summary.pdf", "page": 5, "document_sha256": document_sha,
        "coverage_period": {"start": "2026-09-01", "end": "2026-09-30"},
        "citations": snapshot["pdf_source_updates"][-1]["citations"],
    }
    assert article["title"] == "Registry-only climate article — café μ"
    assert article["publication_date"] == "2026-09-20"
    html_report = render_range_report_html(snapshot)
    assert "Stored PDF-only summary." in html_report
    assert "Article ID: article-a" in html_report
    assert f"summary.pdf, page 5, {document_sha}" in html_report
    assert "article publication date unconfirmed; coverage period 2026-09-01 through 2026-09-30" in html_report
    assert 'href="#publisher-1">Example Institute</a>' in html_report


def test_executive_summary_states_when_selected_updates_have_no_stored_summary():
    snapshot = {
        "date_range": {"start": "2026-09-01", "end": "2026-09-30"},
        "articles": [{"article_id": "article-a", "title": "Untitled", "summary": None, "citations": []}],
        "pdf_source_updates": [{
            "title": "PDF", "summary": None, "filename": "empty.pdf", "page": 1,
            "document_sha256": "a" * 64, "citations": [],
            "coverage_period": {"start": "2026-09-01", "end": "2026-09-30"},
        }],
        "pdf_source_exclusion_counts": {"unknown_coverage": 0, "non_overlapping_coverage": 0},
        "unknown_publication_date_count": 0,
        "executive_summary": [],
    }
    assert range_reports._executive_summary(snapshot) == [
        "1 evidenced Registry article(s) were published from 2026-09-01 through 2026-09-30.",
        "No stored Registry article summaries are available for the selected range.",
        "1 PDF source update(s) overlap the range; their article publication dates are unconfirmed.",
        "No stored PDF source summaries are available for the selected range.",
        "PDF source observations excluded: 0 with unknown coverage; 0 with non-overlapping coverage.",
    ]


def test_empty_range_and_legacy_key_dates_have_accurate_saved_snapshot_status():
    snapshot = {
        "snapshot_id": "range-report-" + "a" * 24,
        "date_range": {"start": "2026-09-01", "end": "2026-09-30", "inclusive": True},
        "articles": [], "pdf_source_updates": [],
        "unknown_publication_date_count": 0,
        "unknown_publication_date_article_ids": [],
        "meeting": {"status": "not_requested", "snapshot_id": None, "snapshot_sha256": None, "records": []},
    }
    html_report = render_range_report_html(snapshot)
    assert "No selected Registry articles or PDF source updates matched this range." in html_report
    assert '<a href="#key-dates">Key Dates</a>' in html_report
    assert '<h2 id="key-dates">Key Dates</h2>' in html_report
    assert "Key dates were not captured in this snapshot." in html_report


def test_confirmed_pdf_cannot_restore_a_currently_ineligible_core_article(tmp_path):
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE articles SET document_kind='landing_page', publication_eligible=0 "
        "WHERE article_id='article-a'"
    )
    connection.commit()
    connection.close()

    snapshot = freeze_range_report(
        _reader(database, tmp_path), tmp_path / "range-output",
        start_date="2026-09-17", end_date="2026-09-30",
    )
    assert [item["article_id"] for item in snapshot["articles"]] == ["article-b"]
    assert "article-a" not in snapshot["unknown_publication_date_article_ids"]


def test_snapshot_reopen_rerender_and_corruption_never_query_registry(tmp_path, monkeypatch):
    database = _database(tmp_path)
    root = tmp_path / "range-output"
    snapshot = freeze_range_report(
        _reader(database, tmp_path), root, start_date="2026-09-17", end_date="2026-09-30"
    )
    target = pdf_path(root, snapshot["snapshot_id"])
    target.unlink()
    monkeypatch.setattr(range_reports, "_range_source", lambda *_args, **_kwargs: pytest.fail("live query"))
    loaded = load_range_report(root, snapshot["snapshot_id"])
    assert ensure_range_report_pdf(loaded, root).is_file()
    assert "Registry-only climate article" in render_range_report_html(loaded)

    path = root / snapshot["snapshot_id"] / "snapshot.json"
    tampered = json.loads(path.read_text())
    tampered["articles"][0]["title"] = "changed"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(RangeReportError, match="hash mismatch"):
        load_range_report(root, snapshot["snapshot_id"])


def test_pre_change_v1_snapshot_still_loads_and_serves_html_and_pdf(tmp_path, monkeypatch):
    database = _database(tmp_path)
    root = tmp_path / "range-output"
    current = freeze_range_report(
        _reader(database, tmp_path), root,
        start_date="2026-09-17", end_date="2026-09-30",
    )
    invalid_current = json.loads(json.dumps(current))
    invalid_current["executive_summary"] = None
    frozen_invalid = {
        key: value for key, value in invalid_current.items()
        if key not in {"snapshot_id", "snapshot_sha256", "created_at"}
    }
    digest_invalid = range_reports._digest(frozen_invalid)
    invalid_current["snapshot_id"] = "range-report-" + digest_invalid[:24]
    invalid_current["snapshot_sha256"] = digest_invalid
    with pytest.raises(RangeReportError, match="invalid range report schema"):
        range_reports._validate_snapshot(invalid_current, invalid_current["snapshot_id"])
    legacy_frozen = {
        key: value for key, value in current.items()
        if key not in {
            "snapshot_id", "snapshot_sha256", "created_at",
            "pdf_source_updates", "pdf_source_exclusion_counts", "pdf_calendar", "executive_summary",
        }
    }
    legacy_digest = range_reports._digest(legacy_frozen)
    legacy_id = "range-report-" + legacy_digest[:24]
    legacy = {
        **legacy_frozen,
        "snapshot_id": legacy_id,
        "snapshot_sha256": legacy_digest,
        "created_at": NOW,
    }
    legacy_dir = root / legacy_id
    legacy_dir.mkdir()
    (legacy_dir / "snapshot.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )

    loaded = load_range_report(root, legacy_id)
    assert "pdf_source_updates" not in loaded
    assert "pdf_source_exclusion_counts" not in loaded
    monkeypatch.setattr(api_server, "RANGE_REPORT_DIR", root)
    client = TestClient(api_server.app)
    web_url = f"/api/registry/range-reports/{legacy_id}/{RENDERER_VERSION}"
    html_response = client.get(web_url)
    pdf_response = client.get(web_url + "/pdf")
    assert html_response.status_code == 200
    assert "Registry-only climate article" in html_response.text
    assert "PDF Source Updates" not in html_response.text
    assert "PDF source observations excluded" not in html_response.text
    assert pdf_response.status_code == 200 and pdf_response.content.startswith(b"%PDF-")
    pdf_text = "\n".join(
        page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf_response.content)).pages
    )
    assert "Registry-only climate article" in pdf_text
    assert "PDF Source Updates" not in pdf_text
    assert "PDF source observations excluded" not in pdf_text


def test_default_meeting_query_and_pdf_calendar_are_frozen_and_grouped(tmp_path, monkeypatch):
    source = {
        "articles": [], "pdf_source_updates": [],
        "pdf_source_exclusion_counts": {"non_overlapping_coverage": 0, "unknown_coverage": 0},
        "unknown_publication_date_count": 0, "unknown_publication_date_article_ids": [],
    }
    queried = {
        "schema_version": "climate-meeting-query.v1", "base_date": "2026-09-30", "timezone": "UTC",
        "filters": {"include_cancelled": False, "include_retrospective": False},
        "coverage": {"status": "complete"},
        "records": [{"name": "Future meeting", "start_date": "2026-10-02"}],
    }
    calls = []

    def query(database, **kwargs):
        calls.append((database, kwargs))
        return queried

    class Reader:
        database = tmp_path / "readonly-registry.sqlite3"

        def pdf_calendar_items(self, *, page, page_size):
            assert page_size == 100
            pages = {
                1: [
                    {"name": "PDF event", "kind": "event", "raw_date": "2 Oct 2026", "date_precision": "day", "end_date": "2026-10-02", "source_filename": "calendar.pdf", "page": 2, "source_document_sha256": "a" * 64},
                    {"name": "Expired PDF event", "kind": "event", "raw_date": "1 Sep 2026", "date_precision": "day", "end_date": "2026-09-01", "source_filename": "calendar.pdf", "page": 4, "source_document_sha256": "a" * 64},
                    {"name": "Unknown PDF event", "kind": "event", "raw_date": "TBA", "date_precision": "unknown", "end_date": None, "source_filename": "calendar.pdf", "page": 5, "source_document_sha256": "a" * 64},
                    {"name": "Future month", "kind": "event", "raw_date": "November 2026", "date_precision": "month", "start_date": "2026-11", "end_date": None, "source_filename": "calendar.pdf", "page": 6, "source_document_sha256": "a" * 64},
                    {"name": "Expired month", "kind": "event", "raw_date": "August 2026", "date_precision": "month", "start_date": "2026-08", "end_date": None, "source_filename": "calendar.pdf", "page": 7, "source_document_sha256": "a" * 64},
                    {"name": "Future quarter", "kind": "event", "raw_date": "Q4 2026", "date_precision": "quarter", "start_date": "2026-Q4", "end_date": None, "source_filename": "calendar.pdf", "page": 8, "source_document_sha256": "a" * 64},
                    {"name": "Expired quarter", "kind": "event", "raw_date": "Q2 2026", "date_precision": "quarter", "start_date": "2026-Q2", "end_date": None, "source_filename": "calendar.pdf", "page": 9, "source_document_sha256": "a" * 64},
                    {"name": "Future year", "kind": "event", "raw_date": "2027", "date_precision": "year", "start_date": "2027", "end_date": None, "source_filename": "calendar.pdf", "page": 10, "source_document_sha256": "a" * 64},
                    {"name": "Expired year", "kind": "event", "raw_date": "2025", "date_precision": "year", "start_date": "2025", "end_date": None, "source_filename": "calendar.pdf", "page": 11, "source_document_sha256": "a" * 64},
                ],
                2: [{"name": "Deadline", "kind": "deadline", "raw_date": "3 Oct 2026", "date_precision": "day", "end_date": "2026-10-03", "source_filename": "calendar.pdf", "page": 3, "source_document_sha256": "a" * 64}],
            }
            return {"items": pages[page], "pagination": {"pages": 2}}

    monkeypatch.setattr(range_reports, "_range_source", lambda *_: source)
    monkeypatch.setattr(range_reports, "query_events", query)
    monkeypatch.setattr(range_reports, "_pdf_calendar_available", lambda _reader: True)
    root = tmp_path / "range-output"
    snapshot = freeze_range_report(
        Reader(), root, start_date="2026-09-01", end_date="2026-09-10",
        generated_at=datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc),
    )
    assert calls == [(Reader.database, {"base_date": "2026-09-30", "timezone_name": "UTC"})]
    assert snapshot["meeting"]["status"] == "included"
    assert snapshot["meeting"]["query_id"].startswith("meeting-query-")
    assert snapshot["meeting"]["query_sha256"] and snapshot["meeting"]["base_date"] == "2026-09-30"
    query_payload = snapshot["meeting"]["query_payload"]
    assert range_reports._digest(query_payload) == snapshot["meeting"]["query_sha256"]
    assert snapshot["meeting"]["query_id"] == "meeting-query-" + snapshot["meeting"]["query_sha256"][:24]
    assert {item["name"] for item in snapshot["pdf_calendar"]["records"]} == {
        "PDF event", "Deadline", "Future month", "Future quarter", "Future year",
    }
    before_html = render_range_report_html(load_range_report(root, snapshot["snapshot_id"]))
    before_pdf = pdf_path(root, snapshot["snapshot_id"]).read_bytes()
    queried["records"][:] = [{"name": "Changed live meeting", "start_date": "2027-01-01"}]
    target = pdf_path(root, snapshot["snapshot_id"])
    target.unlink()
    rerendered = ensure_range_report_pdf(load_range_report(root, snapshot["snapshot_id"]), root).read_bytes()
    assert render_range_report_html(load_range_report(root, snapshot["snapshot_id"])) == before_html
    rerendered_text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(rerendered)).pages)
    assert "Future meeting" in rerendered_text and "Changed live meeting" not in rerendered_text
    assert before_pdf != b""
    assert "PDF Calendar Dates" in before_html and "PDF Deadlines" in before_html
    assert "calendar.pdf" in before_html and "a" * 64 in before_html
    assert "Meeting source:</strong> query" in before_html
    assert snapshot["meeting"]["query_id"] in before_html
    assert snapshot["meeting"]["query_sha256"] in before_html
    assert "Meeting query base date:</strong> 2026-09-30" in before_html
    assert "Meeting query timezone:</strong> UTC" in before_html
    assert "Meeting coverage:</strong> complete" in before_html
    empty_snapshot = {**snapshot, "meeting": {**snapshot["meeting"], "status": "empty", "records": []}}
    empty_html = render_range_report_html(empty_snapshot)
    empty_path = tmp_path / "empty-meetings.pdf"
    render_range_report_pdf(empty_snapshot, empty_path)
    empty_pdf = "\n".join(page.extract_text() or "" for page in PdfReader(empty_path).pages)
    assert "No future meetings." in empty_html and "No future meetings." in empty_pdf
    tampered = json.loads(json.dumps(snapshot))
    tampered["meeting"]["query_payload"]["records"] = []
    frozen = {key: value for key, value in tampered.items() if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
    digest = range_reports._digest(frozen)
    tampered["snapshot_id"] = "range-report-" + digest[:24]
    tampered["snapshot_sha256"] = digest
    with pytest.raises(RangeReportError, match="frozen meeting query identity"):
        range_reports._validate_snapshot(tampered, tampered["snapshot_id"])
    missing_identity = json.loads(json.dumps(snapshot))
    missing_identity["meeting"]["query_id"] = None
    missing_identity["meeting"]["query_sha256"] = None
    frozen = {key: value for key, value in missing_identity.items() if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
    digest = range_reports._digest(frozen)
    missing_identity["snapshot_id"] = "range-report-" + digest[:24]
    missing_identity["snapshot_sha256"] = digest
    with pytest.raises(RangeReportError, match="frozen meeting query identity"):
        range_reports._validate_snapshot(missing_identity, missing_identity["snapshot_id"])
    malformed_metadata = json.loads(json.dumps(snapshot))
    malformed_metadata["meeting"]["query_payload"]["timezone"] = "America/New_York"
    malformed_metadata["meeting"]["timezone"] = "America/New_York"
    query_id, query_sha256 = range_reports._query_identity(malformed_metadata["meeting"]["query_payload"])
    malformed_metadata["meeting"]["query_id"] = query_id
    malformed_metadata["meeting"]["query_sha256"] = query_sha256
    frozen = {key: value for key, value in malformed_metadata.items() if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
    digest = range_reports._digest(frozen)
    malformed_metadata["snapshot_id"] = "range-report-" + digest[:24]
    malformed_metadata["snapshot_sha256"] = digest
    with pytest.raises(RangeReportError, match="frozen meeting query metadata"):
        range_reports._validate_snapshot(malformed_metadata, malformed_metadata["snapshot_id"])


def test_calendar_and_meeting_failures_are_marked_without_losing_articles(tmp_path, monkeypatch):
    source = {
        "articles": [{
            "article_id": "article", "publication_date": "2026-09-02", "title": "Saved article",
            "content_version_id": "content", "summary": "Saved summary", "content": "Saved content",
            "categories": [], "keywords": [], "source_observations": [], "citations": [], "provenance": {},
        }], "pdf_source_updates": [],
        "pdf_source_exclusion_counts": {"non_overlapping_coverage": 0, "unknown_coverage": 0},
        "unknown_publication_date_count": 0, "unknown_publication_date_article_ids": [],
    }

    class Reader:
        database = tmp_path / "readonly-registry.sqlite3"

        def pdf_calendar_items(self, **_kwargs):
            raise OSError("unavailable")

    monkeypatch.setattr(range_reports, "_range_source", lambda *_: source)
    monkeypatch.setattr(range_reports, "query_events", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("unavailable")))
    monkeypatch.setattr(range_reports, "_pdf_calendar_available", lambda _reader: True)
    snapshot = freeze_range_report(
        Reader(), tmp_path / "range-output", start_date="2026-09-01", end_date="2026-09-10",
        generated_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    assert snapshot["meeting"]["status"] == "unavailable"
    query_payload = snapshot["meeting"]["query_payload"]
    assert query_payload["coverage"] == {"status": "unavailable", "error": "OSError"}
    assert query_payload["records"] == []
    assert range_reports._digest(query_payload) == snapshot["meeting"]["query_sha256"]
    assert snapshot["meeting"]["query_id"] == "meeting-query-" + snapshot["meeting"]["query_sha256"][:24]
    assert snapshot["pdf_calendar"]["status"] == "unavailable"
    assert snapshot["articles"][0]["title"] == "Saved article"
    assert load_range_report(tmp_path / "range-output", snapshot["snapshot_id"])["meeting"]["status"] == "unavailable"
    malformed = json.loads(json.dumps(snapshot))
    malformed["meeting"]["query_payload"]["base_date"] = "not-a-date"
    malformed["meeting"]["base_date"] = "not-a-date"
    query_id, query_sha256 = range_reports._query_identity(malformed["meeting"]["query_payload"])
    malformed["meeting"]["query_id"] = query_id
    malformed["meeting"]["query_sha256"] = query_sha256
    frozen = {key: value for key, value in malformed.items() if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
    digest = range_reports._digest(frozen)
    malformed["snapshot_id"] = "range-report-" + digest[:24]
    malformed["snapshot_sha256"] = digest
    with pytest.raises(RangeReportError, match="frozen meeting query metadata"):
        range_reports._validate_snapshot(malformed, malformed["snapshot_id"])


def test_pre_pdf_registry_marks_calendar_unavailable_without_losing_articles(tmp_path, monkeypatch):
    database = tmp_path / "registry-v12.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection, target_version=12)
    connection.close()
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    assert reader.pdf_calendar_items()["items"] == []
    source = {
        "articles": [{
            "article_id": "article", "publication_date": "2026-09-02", "title": "Saved article",
            "content_version_id": "content", "summary": "Saved summary", "content": "Saved content",
            "categories": [], "keywords": [], "source_observations": [], "citations": [], "provenance": {},
        }], "pdf_source_updates": [],
        "pdf_source_exclusion_counts": {"non_overlapping_coverage": 0, "unknown_coverage": 0},
        "unknown_publication_date_count": 0, "unknown_publication_date_article_ids": [],
    }
    monkeypatch.setattr(range_reports, "_range_source", lambda *_: source)
    snapshot = freeze_range_report(
        reader, tmp_path / "range-output", start_date="2026-09-01", end_date="2026-09-10",
        generated_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    assert snapshot["pdf_calendar"] == {
        "status": "unavailable",
        "coverage": {"status": "unavailable", "error": "pdf_calendar_unavailable"},
        "base_date": "2026-09-30", "records": [],
    }
    assert snapshot["articles"][0]["title"] == "Saved article"


def test_meeting_snapshot_coverage_states_are_distinct(tmp_path):
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    coverage_states = (
        ("1", "succeeded_empty", []),
        ("2", "failed", []),
        ("3", "partial", []),
        ("4", "disabled", []),
        ("5", "enabled_unprocessed", []),
        ("6", "not_processed", []),
        ("7", "running", []),
        ("8", "failed", [{"meeting_id": "frozen-record"}]),
        ("9", "partial", [{"meeting_id": "partial-record"}]),
        ("a", "succeeded", [{"meeting_id": "successful-record"}]),
        ("b", "succeeded", []),
    )
    for marker, coverage_status, records in coverage_states:
        suffix = marker * 24
        snapshot_id = "meeting-snapshot-" + suffix
        connection.execute(
            "INSERT INTO meeting_snapshots VALUES (?, ?, '{}', '2026-09-30', 'UTC', ?, ?, ?)",
            (snapshot_id, NOW, json.dumps(records), json.dumps({"status": coverage_status}), marker * 64),
        )
    connection.commit()
    connection.close()
    reader = _reader(database, tmp_path)

    expected = {
        "1": "empty", "2": "failed", "3": "failed", "4": "unavailable",
        "5": "unavailable", "6": "unavailable", "7": "unavailable", "8": "failed",
        "9": "failed", "a": "included", "b": "empty",
    }
    for marker, wanted in expected.items():
        snapshot = freeze_range_report(
            reader, tmp_path / marker, start_date="2026-09-17", end_date="2026-09-30",
            meeting_snapshot_id="meeting-snapshot-" + marker * 24,
        )
        assert snapshot["meeting"]["status"] == wanted
        if marker == "a":
            rendered = render_range_report_html(snapshot)
            pdf_text = "\n".join(
                page.extract_text() or "" for page in PdfReader(pdf_path(tmp_path / marker, snapshot["snapshot_id"])).pages
            )
            assert snapshot["meeting"]["snapshot_id"] in rendered and snapshot["meeting"]["snapshot_id"] in pdf_text
            assert "Meeting source:</strong> snapshot" in rendered

    missing = freeze_range_report(
        reader, tmp_path / "missing", start_date="2026-09-17", end_date="2026-09-30",
        meeting_snapshot_id="meeting-snapshot-" + "f" * 24,
    )
    assert missing["meeting"]["status"] == "unavailable"


def test_chat_returns_stable_web_and_pdf_links_without_normal_responder(tmp_path, monkeypatch):
    database = _database(tmp_path)
    output = tmp_path / "range-output"
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(api_server, "RANGE_REPORT_DIR", output)
    monkeypatch.setattr(api_server.responder, "answer", lambda *_a, **_kw: pytest.fail("normal responder"))
    client = TestClient(api_server.app)

    created = client.post(
        "/api/chat", json={"message": "Create a climate report from 2026-09-17 to 2026-09-30"}
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    report = payload["range_report"]
    assert report["web_url"] in payload["text"] and report["pdf_url"] in payload["text"]
    assert payload["sources"] == [] and payload["agent_mode"] == "offline"
    assert report["article_count"] == 2
    assert report["pdf_source_update_count"] == 0
    assert report["pdf_source_excluded_count"] == 1
    assert report["pdf_source_exclusion_counts"] == {
        "non_overlapping_coverage": 0,
        "unknown_coverage": 1,
    }
    assert load_range_report(output, report["snapshot_id"])["meeting"]["source"] == "query"
    assert report["web_url"].endswith(f"/{RENDERER_VERSION}")
    page = client.get(report["web_url"])
    pdf = client.get(report["pdf_url"])
    assert page.status_code == 200 and "Registry-only climate article" in page.text
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    assert report["snapshot_id"] in pdf.headers["content-disposition"]


def test_chat_resumes_pending_report_range_and_meeting_snapshot(tmp_path, monkeypatch):
    database = _database(tmp_path)
    output = tmp_path / "range-output"
    meeting_snapshot_id = "meeting-snapshot-" + "c" * 24
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(api_server, "RANGE_REPORT_DIR", output)
    monkeypatch.setattr(api_server.responder, "answer", lambda *_a, **_kw: pytest.fail("normal responder"))
    client = TestClient(api_server.app)

    first_question = f"I need a climate report using {meeting_snapshot_id}"
    first = client.post("/api/chat", json={"message": first_question})
    assert first.status_code == 200 and first.json()["needs_clarification"] is True

    incomplete = client.post("/api/chat", json={"messages": [
        {"role": "user", "content": first_question},
        {"role": "assistant", "content": first.json()["text"]},
        {"role": "user", "content": "2026-09-17"},
    ]})
    assert incomplete.status_code == 200
    assert incomplete.json()["needs_clarification"] is True

    completed = client.post("/api/chat", json={"messages": [
        {"role": "user", "content": first_question},
        {"role": "assistant", "content": first.json()["text"]},
        {"role": "user", "content": "2026-09-17"},
        {"role": "assistant", "content": incomplete.json()["text"]},
        {"role": "user", "content": "2026-09-17 to 2026-09-30"},
    ]})
    assert completed.status_code == 200, completed.text
    report = completed.json()["range_report"]
    assert report["date_range"] == {
        "start": "2026-09-17", "end": "2026-09-30", "inclusive": True,
    }
    snapshot = load_range_report(output, report["snapshot_id"])
    assert snapshot["meeting"]["snapshot_id"] == meeting_snapshot_id
    assert snapshot["meeting"]["status"] == "unavailable"


@pytest.mark.parametrize(("first_question", "correction", "expected", "meeting_snapshot_id"), [
    (
        "Create a climate report from 2026-02-30 to 2026-03-01 using "
        "meeting-snapshot-dddddddddddddddddddddddd",
        "2026-09-17 to 2026-09-30",
        {"start": "2026-09-17", "end": "2026-09-30", "inclusive": True},
        "meeting-snapshot-dddddddddddddddddddddddd",
    ),
    (
        "Create a climate report for 2026-09-01",
        "2026-09-10 to 2026-09-20",
        {"start": "2026-09-10", "end": "2026-09-20", "inclusive": True},
        None,
    ),
])
def test_chat_uses_latest_full_range_correction_without_stale_dates(
    tmp_path, monkeypatch, first_question, correction, expected, meeting_snapshot_id,
):
    database = _database(tmp_path)
    output = tmp_path / "range-output"
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(api_server, "RANGE_REPORT_DIR", output)
    monkeypatch.setattr(api_server.responder, "answer", lambda *_a, **_kw: pytest.fail("normal responder"))
    client = TestClient(api_server.app)

    first = client.post("/api/chat", json={"message": first_question})
    assert first.status_code == 200 and first.json()["needs_clarification"] is True
    corrected = client.post("/api/chat", json={"messages": [
        {"role": "user", "content": first_question},
        {"role": "assistant", "content": first.json()["text"]},
        {"role": "user", "content": correction},
    ]})
    assert corrected.status_code == 200, corrected.text
    report = corrected.json()["range_report"]
    assert report["date_range"] == expected
    snapshot = load_range_report(output, report["snapshot_id"])
    assert snapshot["meeting"]["snapshot_id"] == meeting_snapshot_id


def _render_fixture(path: Path, *, count: int, repeat: int) -> dict:
    articles = []
    for index in range(count):
        url = "https://example.org/" + ("very-long-segment-" * 16) + str(index)
        articles.append({
            "article_id": f"fixture-{index}", "canonical_url": url,
            "title": f"Unicode café μ article {index} " + ("long title " * 10),
            "publisher": "Fixture", "publication_date": "2026-09-20",
            "summary": "Evidence summary — café μ.", "categories": ["Physical risk"],
            "keywords": ["pricing"], "content_version_id": f"content-{index}",
            "content": ("Long persisted climate evidence with Unicode café μ. " * repeat),
            "source_observations": [],
            "citations": [{"kind": "url", "url": url}],
            "provenance": {},
        })
    frozen = {
        "schema_version": range_reports.SCHEMA_VERSION,
        "date_range": {"start": "2026-09-17", "end": "2026-09-30", "inclusive": True},
        "timezone": "UTC", "articles": articles,
        "unknown_publication_date_count": 0,
        "unknown_publication_date_article_ids": [],
        "meeting": {"status": "not_requested", "snapshot_id": None,
                    "snapshot_sha256": None, "records": []},
    }
    snapshot = {
        **frozen, "snapshot_id": "range-report-" + _sha(json.dumps(frozen))[:24],
        "snapshot_sha256": range_reports._digest(frozen), "created_at": NOW,
    }
    render_range_report_pdf(snapshot, path)
    return snapshot


def test_same_publisher_updates_are_grouped_by_frozen_topic_in_html_and_pdf(tmp_path):
    snapshot = _render_fixture(tmp_path / "initial.pdf", count=2, repeat=1)
    snapshot["articles"][1]["categories"] = ["Transition risk"]
    frozen = {
        key: value for key, value in snapshot.items()
        if key not in {"snapshot_id", "snapshot_sha256", "created_at"}
    }
    digest = range_reports._digest(frozen)
    snapshot["snapshot_id"] = "range-report-" + digest[:24]
    snapshot["snapshot_sha256"] = digest
    range_reports._validate_snapshot(snapshot, snapshot["snapshot_id"])
    html_report = render_range_report_html(snapshot)
    output = tmp_path / "topics.pdf"
    render_range_report_pdf(snapshot, output)
    reader = PdfReader(output)
    pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    outline_titles = []

    def collect_outline_titles(entries):
        for entry in entries:
            if isinstance(entry, list):
                collect_outline_titles(entry)
            else:
                title = getattr(entry, "title", None)
                outline_titles.append(title if title is not None else entry.get("/Title", ""))

    collect_outline_titles(reader.outline)

    assert '<a href="#publisher-1-topic-1">Physical risk</a>' in html_report
    assert '<a href="#publisher-1-topic-2">Transition risk</a>' in html_report
    assert '<h4 id="publisher-1-topic-1">Physical risk</h4>' in html_report
    assert '<h4 id="publisher-1-topic-2">Transition risk</h4>' in html_report
    assert "Physical risk" in pdf_text and "Transition risk" in pdf_text
    assert "Physical risk" in outline_titles and "Transition risk" in outline_titles


@pytest.mark.parametrize(("count", "repeat", "minimum_pages"), [(1, 2, 3), (8, 90, 10)])
def test_short_and_long_pdf_fixtures_have_toc_bookmarks_pages_unicode_and_citations(
    tmp_path, count, repeat, minimum_pages,
):
    output = tmp_path / f"fixture-{count}.pdf"
    snapshot = _render_fixture(output, count=count, repeat=repeat)
    reader = PdfReader(output)
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert len(reader.pages) >= minimum_pages
    assert reader.outline
    assert "Contents" in text
    assert "Page 1" in text and f"Page {len(reader.pages)}" in text
    assert "café μ" in text
    assert "Long persisted climate evidence" in text
    assert "very-long-segment" in text
    assert snapshot["snapshot_id"] in text
