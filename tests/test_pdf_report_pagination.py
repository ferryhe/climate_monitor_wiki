import json
import sqlite3
import shutil
import subprocess
from pathlib import Path

import pytest

from climate_registry.read_api import RegistryNotFoundError, RegistryQueryError, RegistryReader
from test_issue221_review_repairs import _approve_all
from test_registry_publication import _database, _reader


def test_public_pdf_pages_construct_only_selected_articles(tmp_path, monkeypatch):
    database = _database(tmp_path)
    document = "f" * 64
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,"
            "extracted_text_sha256,document_json,imported_at) VALUES(?,?,?,'application/pdf',1,?,?,?)",
            (document, "/evidence/report.pdf", "report.pdf", "e" * 64,
             json.dumps({"title": "Report", "executive_summary": "Complete summary",
                         "pages": [{"page": 1, "text": "Raw text"}]}), "2026-09-01"))
        for index in range(7):
            connection.execute(
                "INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES(?,?,?,?)",
                (f"pdf-{index}", f"https://example.org/pdf-{index}", f"Article {index}", "2026-09-01"))
            connection.execute(
                "INSERT INTO pdf_intake_article_occurrences(occurrence_id,article_id,source_document_sha256,"
                "page,raw_url,report_date,content_sha256,page_sha256,occurrence_json) VALUES(?,?,?,1,?,'2026-09-01',?,?,?)",
                (f"occ-{index}", f"pdf-{index}", document, f"https://example.org/pdf-{index}", "d" * 64,
                 "c" * 64, json.dumps({"summary": f"Summary {index}", "summary_basis": "verbatim_pdf_article_row"})))
        from verified_fixtures import seed_verified_articles
        seed_verified_articles(connection)
        _approve_all(connection)

    calls = []
    original = RegistryReader._pdf_article_from_snapshot

    def counted(self, snapshot, **kwargs):
        calls.append(snapshot["entity_id"])
        return original(self, snapshot, **kwargs)

    monkeypatch.setattr(RegistryReader, "_pdf_article_from_snapshot", counted)
    reader = _reader(database, tmp_path)
    first = reader.pdf_report(document, page=1, page_size=5)
    assert [item["article_id"] for item in first["articles"]] == [f"pdf-{i}" for i in range(5)]
    assert set(calls) == {f"pdf-{i}" for i in range(5)}
    assert first["executive_summary"] == []
    assert first["article_count"] == 7
    assert "pages" not in first
    assert first["pagination"] == {"page": 1, "page_size": 5, "total": 7, "pages": 2}
    calls.clear()
    second = reader.pdf_report(document, page=2, page_size=5)
    assert [item["article_id"] for item in second["articles"]] == ["pdf-5", "pdf-6"]
    assert set(calls) == {"pdf-5", "pdf-6"}
    assert second["article_offset"] == 5
    assert reader.pdf_report(document, page=3, page_size=5)["articles"] == []
    with pytest.raises(RegistryQueryError):
        reader.pdf_report(document, page=0)

    import api_server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    client = TestClient(api_server.app)
    response = client.get(f"/api/registry/pdf-intake/reports/{document}?page=2&page_size=5")
    assert response.status_code == 200
    assert [row["article_id"] for row in response.json()["articles"]] == ["pdf-5", "pdf-6"]
    assert "pages" not in response.json()
    assert client.get(f"/api/registry/pdf-intake/reports/{document}?page=0").status_code == 400
    assert client.get(f"/api/registry/pdf-intake/reports/{document}?page=1&page_size=101").status_code == 400
    api_server._clear_registry_list_cache()
    assert client.get("/api/registry/reports?include_pdf=true").status_code == 200
    import climate_registry.publication as publication
    original_decode = publication._load_snapshot_json
    def no_redecode(*args):
        pytest.fail("unchanged PDF page decoded an already validated candidate")
    monkeypatch.setattr(publication, "_load_snapshot_json", no_redecode)
    assert client.get(f"/api/registry/pdf-intake/reports/{document}?page=2").status_code == 200
    monkeypatch.setattr(publication, "_load_snapshot_json", original_decode)
    api_server._clear_registry_list_cache()

    # Legacy runtime reads use the catalog before constructing occurrence/check payloads.
    runtime = RegistryReader(database, repository_root=tmp_path / "application", public=False)
    catalog = runtime.pdf_report_module_catalog(document)
    assert catalog == [("article", f"pdf_article:pdf-{i}") for i in range(7)]
    selected = runtime.pdf_report(document, module_keys=set(catalog[5:]))
    assert [row["article_id"] for row in selected["articles"]] == ["pdf-5", "pdf-6"]
    assert runtime.pdf_report(document, module_keys=set())["articles"] == []
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) "
            "VALUES('calendar-alias','https://example.org/meeting','Calendar alias','2026-09-01')")
        connection.execute(
            "INSERT INTO pdf_intake_article_occurrences(occurrence_id,article_id,source_document_sha256,"
            "page,raw_url,report_date,content_sha256,page_sha256,occurrence_json) "
            "VALUES('calendar-occ','calendar-alias',?,1,'https://example.org/meeting','2026-09-01',?,?,?)",
            (document, "d" * 64, "c" * 64, json.dumps({"summary_basis": "verbatim_pdf_calendar_row"})))
    assert runtime.pdf_report_module_catalog(document) == catalog
    with sqlite3.connect(database) as connection:
        from verified_fixtures import seed_verified_articles
        seed_verified_articles(connection)
        _approve_all(connection)
    published = reader.pdf_report(document, page=1, page_size=5)
    assert len(published["articles"]) == 5
    assert published["pagination"]["total"] == 7
    assert published["article_count"] == 7
    assert [row["article_id"] for row in reader.pdf_report(document, page=2)["articles"]] == ["pdf-5", "pdf-6"]
    # A new visibility generation must not reuse the previously visible PDF candidate.
    assert client.get("/api/registry/reports?include_pdf=true").status_code == 200
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_kind='pdf_article' AND entity_id='pdf-0'")
    hidden_page = client.get(f"/api/registry/pdf-intake/reports/{document}?page=1")
    assert hidden_page.status_code == 200
    assert [row["article_id"] for row in hidden_page.json()["articles"]] == [f"pdf-{i}" for i in range(1, 6)]
    assert api_server._REGISTRY_LIST_CACHE is None
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE registry_publication SET is_visible=1 WHERE entity_kind='pdf_article' AND entity_id='pdf-0'")
    assert client.get("/api/registry/reports?include_pdf=true").status_code == 200
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER registry_candidates_immutable_update")
        connection.execute("UPDATE registry_candidates SET snapshot_json=json_set(snapshot_json,'$.tampered',1) "
            "WHERE candidate_sha256=(SELECT published_candidate_sha256 FROM registry_publication "
            "WHERE entity_kind='pdf_article' AND entity_id='pdf-0')")
    assert client.get(f"/api/registry/pdf-intake/reports/{document}?page=1").status_code == 503
    assert api_server._REGISTRY_LIST_CACHE is None


def test_same_core_article_keeps_approved_occurrences_in_each_pdf_report(tmp_path):
    database = _database(tmp_path)
    first_document, second_document = "f" * 64, "9" * 64
    with sqlite3.connect(database) as connection:
        for document, filename, report_date in (
            (first_document, "first.pdf", "2026-09-01"),
            (second_document, "second.pdf", "2026-09-02"),
        ):
            connection.execute(
                "INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,"
                "extracted_text_sha256,document_json,imported_at) VALUES(?,?,?,'application/pdf',1,?,?,?)",
                (document, f"/evidence/{filename}", filename, "e" * 64, "{}", report_date))
        connection.execute(
            "INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES(?,?,?,?)",
            ("pdf-a-shared", "https://example.org/a", "Shared PDF article", "2026-09-01"))
        for document, occurrence_id in (
            (first_document, "occ-first"),
            (second_document, "occ-second"),
        ):
            connection.execute(
                "INSERT INTO pdf_intake_article_occurrences(occurrence_id,article_id,source_document_sha256,"
                "page,raw_url,report_date,content_sha256,page_sha256,occurrence_json) VALUES(?,?,?,1,?,'2026-09-01',?,?,?)",
                (occurrence_id, "pdf-a-shared", document, "https://example.org/a", "d" * 64, "c" * 64,
                 json.dumps({"summary": f"Approved observation in {occurrence_id}",
                             "summary_basis": "verbatim_pdf_article_row"})))
        from verified_fixtures import seed_verified_articles
        seed_verified_articles(connection)
        _approve_all(connection)

    reader = _reader(database, tmp_path)
    reports = {item["document_sha256"]: item for item in reader.pdf_reports_all()}
    assert set(reports) == {first_document, second_document}
    assert all(item["article_count"] == 1 for item in reports.values())
    for document, occurrence_id in ((first_document, "occ-first"), (second_document, "occ-second")):
        detail = reader.pdf_report(document)
        assert len(detail["articles"]) == 1
        article = detail["articles"][0]
        assert article["canonical_url"] == "https://example.org/a"
        assert [row["occurrence_id"] for row in article["occurrences"]] == [occurrence_id]
        assert article["occurrences"][0]["summary"] == f"Approved observation in {occurrence_id}"


def test_unreviewed_pdf_archive_is_listed_and_downloadable_without_extracted_items(tmp_path, monkeypatch):
    import hashlib

    database = _database(tmp_path)
    body = b"retained original PDF bytes"
    document = hashlib.sha256(body).hexdigest()
    missing_document = "1" * 64
    corrupt_document = hashlib.sha256(b"expected bytes").hexdigest()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,"
            "extracted_text_sha256,document_json,imported_at,original_pdf) VALUES(?,?,?,'application/pdf',?,?,?, ?,?)",
            (document, "/evidence/unreviewed.pdf", "unreviewed.pdf", len(body), "e" * 64,
             json.dumps({"title": "Archived PDF", "executive_summary": "Unreviewed summary",
                         "pages": [{"page": 1, "text": "Unreviewed page text"}]}), "2026-09-01", body))
        connection.execute(
            "INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,"
            "extracted_text_sha256,document_json,imported_at) VALUES(?,?,?,'application/pdf',1,?,?,?)",
            (missing_document, "/evidence/missing.pdf", "missing.pdf", "e" * 64, "{}", "2026-09-02"))
        connection.execute(
            "INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,"
            "extracted_text_sha256,document_json,imported_at,original_pdf) VALUES(?,?,?,'application/pdf',?,?,?, ?,?)",
            (corrupt_document, "/evidence/corrupt.pdf", "corrupt.pdf", len(b"corrupt"), "e" * 64,
             "{}", "2026-09-03", b"corrupt"))

    reader = _reader(database, tmp_path)
    assert reader.pdf_reports_all()[0]["article_count"] == 0
    report = reader.pdf_report(document)
    assert report["articles"] == [] and report["calendar_items"] == []
    assert report["executive_summary"] == [] and report["pages"] == []

    import api_server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    client = TestClient(api_server.app)
    detail = client.get(f"/api/registry/pdf-intake/reports/{document}")
    assert detail.status_code == 200
    assert detail.json()["report_pdf"]["download_url"].endswith("/pdf")
    assert "Unreviewed summary" not in detail.text and "Unreviewed page text" not in detail.text
    download = client.get(f"/api/registry/pdf-intake/reports/{document}/pdf")
    assert download.status_code == 200 and download.content == body
    assert download.headers["Content-Disposition"].endswith("filename*=UTF-8''unreviewed.pdf")
    assert download.headers["X-Content-Type-Options"] == "nosniff"
    with pytest.raises(RegistryNotFoundError):
        reader.pdf_report(missing_document, include_bytes=True)
    missing_download = client.get(f"/api/registry/pdf-intake/reports/{missing_document}/pdf")
    assert missing_download.status_code == 404, missing_download.text
    assert client.get(f"/api/registry/pdf-intake/reports/{corrupt_document}/pdf").status_code == 503


def test_legacy_pagination_crosses_article_calendar_boundary():
    report = {"articles": [1, 2, 3, 4, 5, 6], "calendar_items": ["a", "b", "c", "d"],
              "executive_summary": ["Summary"], "pages": [{"text": "Raw text"}]}
    page = RegistryReader.paginate_pdf_report(report, 2, 5)
    assert page["articles"] == [6]
    assert page["calendar_items"] == ["a", "b", "c", "d"]
    assert page["article_offset"] == 5
    assert "pages" not in page
    assert "pages" in report


def test_pdf_pagination_frontend_behavior():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / "tests/test_pdf_report_pagination.mjs")],
        cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
