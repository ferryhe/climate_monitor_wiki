import json
import sqlite3
import shutil
import subprocess
from pathlib import Path

import pytest

from climate_registry.read_api import RegistryQueryError, RegistryReader
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
        _approve_all(connection)

    calls = []
    original = RegistryReader._pdf_article_from_snapshot

    def counted(self, snapshot):
        calls.append(snapshot["entity_id"])
        return original(self, snapshot)

    monkeypatch.setattr(RegistryReader, "_pdf_article_from_snapshot", counted)
    reader = _reader(database, tmp_path)
    first = reader.pdf_report(document, page=1, page_size=5)
    assert [item["article_id"] for item in first["articles"]] == [f"pdf-{i}" for i in range(5)]
    assert set(calls) == {f"pdf-{i}" for i in range(5)}
    assert first["executive_summary"] == ["Complete summary"]
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

    # Legacy runtime reads use the catalog before constructing occurrence/check payloads.
    runtime = RegistryReader(database, repository_root=tmp_path / "application", public=False)
    catalog = runtime.pdf_report_module_catalog(document)
    assert catalog == [("article", f"https://example.org/pdf-{i}") for i in range(7)]
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
