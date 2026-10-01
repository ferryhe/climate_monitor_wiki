from __future__ import annotations

import sqlite3
from io import BytesIO

from fastapi.testclient import TestClient
from fastapi_users.password import PasswordHelper
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

import api_server
from climate_registry.schema import apply_migrations


def _pdf_bytes() -> bytes:
    output = BytesIO()
    canvas = Canvas(output, pagesize=letter)
    canvas.drawString(50, 760, "Climate Risk Intelligence Report")
    canvas.showPage()
    for y, line in ((760, "UPDATES"), (740, "Climate risk study"),
                    (720, "IN WINDOW 14 SEP 2026 REPORT"),
                    (700, "Article summary from the PDF."), (680, "Source: Example")):
        canvas.drawString(50, y, line)
    canvas.linkURL("https://example.org/climate-study", (48, 738, 250, 754), relative=0)
    canvas.showPage()
    for y, line in ((760, "Key Dates"), (740, "DATE(S) EVENT HOST RELEVANCE"),
                    (720, "4–5 Sep 2026"), (700, "EVENT"), (680, "Climate conference"),
                    (660, "A conference relevant to insurers.")):
        canvas.drawString(50, y, line)
    canvas.linkURL("https://example.org/climate-conference", (48, 676, 260, 694), relative=0)
    canvas.save()
    return output.getvalue()


def _client(monkeypatch, tmp_path):
    database = tmp_path / "registry.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection)
    connection.execute("INSERT INTO sources VALUES ('source-example', 'example.org', 'Example', '2026-09-01', '2026-09-01')")
    connection.execute(
        """INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen,
           current_version_id, document_kind, publication_eligible, exclusion_reason)
           VALUES ('core-climate-study', 'https://example.org/climate-study', 'source-example',
           '2026-09-01', '2026-09-01', 'version-climate-study', 'article', 1, NULL)"""
    )
    connection.execute(
        """INSERT INTO article_versions VALUES ('version-climate-study', 'core-climate-study',
           'Core climate study', 'core climate study', 'Core report summary.', ?,
           'report-title-summary', '2026-09-01', '2026-09-01')""",
        ("c" * 64,),
    )
    connection.commit()
    connection.close()
    backup = tmp_path / "backups"
    backup.mkdir()
    queue = tmp_path / "queue"
    queue.mkdir()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setenv("CLIMATE_REGISTRY_BACKUP_DIR", str(backup))
    monkeypatch.setenv("CLIMATE_PDF_INTAKE_QUEUE_DIR", str(queue))
    monkeypatch.setenv("CLIMATE_CONSOLE_USERNAME", "operator")
    monkeypatch.setenv("CLIMATE_CONSOLE_PASSWORD_HASH", PasswordHelper().hash("correct horse"))
    monkeypatch.setenv("CLIMATE_CONSOLE_SESSION_SECRET", "test-secret-with-at-least-32-bytes")
    api_server._LOGIN_LIMITER.reset()
    client = TestClient(api_server.app, base_url="https://testserver")
    assert client.post("/api/manage/auth/login", data={"username": "operator", "password": "correct horse"}).status_code == 204
    return client, database


def _confirmed_preview(preview: dict) -> dict:
    return {
        "confirmed": "true",
        "preview_digest": preview["preview_digest"],
        "preview_sha": [document["sha256"] for document in preview["documents"]],
    }


def test_pdf_intake_digest_omits_original_pdf_before_serialization(monkeypatch):
    source_bytes = "x" * (2 * 1024 * 1024)
    bundle = {
        "schema_version": "climate-pdf-intake.v1",
        "generated_at": "first",
        "typesafe": {"status": "complete", "classified": 1, "failed_batches": 0},
        "documents": [{"source": {
            "sha256": "a" * 64,
            "filename": "report.pdf",
            "source_observations": [{"path": "manage-upload://a/report.pdf", "filename": "report.pdf"}],
            "original_pdf_base64": source_bytes,
        }}],
        "articles": [],
        "calendar_items": [],
    }
    real_dumps = api_server.json.dumps
    serialized = []

    def record_dumps(value, *args, **kwargs):
        serialized.append(value)
        return real_dumps(value, *args, **kwargs)

    monkeypatch.setattr(api_server.json, "dumps", record_dumps)
    first = api_server._pdf_intake_bundle_digest(bundle)
    assert len(serialized) == 1
    assert "original_pdf_base64" not in serialized[0]["documents"][0]["source"]
    assert bundle["documents"][0]["source"]["original_pdf_base64"] is source_bytes

    bundle["generated_at"] = "second"
    bundle["documents"][0]["source"]["original_pdf_base64"] = "y" * len(source_bytes)
    assert api_server._pdf_intake_bundle_digest(bundle) == first


def test_pdf_import_requires_session_and_preview_never_writes(monkeypatch, tmp_path):
    client, database = _client(monkeypatch, tmp_path)
    payload = {"files": ("report.pdf", _pdf_bytes(), "application/pdf")}
    assert TestClient(api_server.app).get("/manage/pdf-import", follow_redirects=False).status_code == 303
    assert TestClient(api_server.app).post("/api/manage/pdf-intake/preview", files=payload).status_code == 401

    preview = client.post("/api/manage/pdf-intake/preview", files=payload)
    assert preview.status_code == 200
    assert preview.json()["writable"] is True
    article = next(item for item in preview.json()["articles"] if item["url"] == "https://example.org/climate-study")
    assert article["title"] == "Climate risk study"
    assert article["page"] == 2 and "Article summary" in article["report_summary"]
    assert article["source_document"] == "report.pdf"
    calendar = preview.json()["calendar"][0]
    assert calendar["name"] == "Climate conference"
    assert calendar["page"] == 3 and calendar["date"] == "2026-09-04"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_documents").fetchone()[0] == 0


def test_pdf_import_queues_idempotently_without_writing_registry(monkeypatch, tmp_path):
    client, database = _client(monkeypatch, tmp_path)
    original_database = database.read_bytes()
    valid = _pdf_bytes()
    real_import = api_server.import_pdf_reports

    def import_with_article_label(inputs):
        bundle = real_import(inputs)
        for article in bundle["articles"]:
            if article.get("canonical_url") == "https://example.org/climate-study":
                article["type_safe_classification"] = {
                    "provider": "typesafe", "label": "article"
                }
        return bundle

    monkeypatch.setattr(api_server, "import_pdf_reports", import_with_article_label)
    invalid_batch = client.post(
        "/api/manage/pdf-intake/preview",
        files=[("files", ("valid.pdf", valid, "application/pdf")),
               ("files", ("bad.pdf", b"not a PDF", "application/pdf"))],
    )
    assert invalid_batch.status_code == 422
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_documents").fetchone()[0] == 0

    first_preview = client.post(
        "/api/manage/pdf-intake/preview", files={"files": ("first.pdf", valid, "application/pdf")},
    ).json()
    mismatch = client.post(
        "/api/manage/pdf-intake/import",
        params={"confirmed": "true", "preview_digest": first_preview["preview_digest"], "preview_sha": "wrong"},
        files={"files": ("first.pdf", valid, "application/pdf")},
    )
    assert mismatch.status_code == 422
    queued = client.post(
        "/api/manage/pdf-intake/import", params=_confirmed_preview(first_preview),
        files={"files": ("first.pdf", valid, "application/pdf")},
    )
    assert queued.status_code == 200, queued.text
    assert queued.json()["stage"] == "queued"
    queue = tmp_path / "queue"
    assert [path.name for path in queue.iterdir()] == [queued.json()["batch_id"]]
    repeated = client.post(
        "/api/manage/pdf-intake/import", params=_confirmed_preview(first_preview),
        files={"files": ("first.pdf", valid, "application/pdf")},
    )
    assert repeated.status_code == 200
    assert repeated.json()["batch_id"] == queued.json()["batch_id"]
    assert len(list(queue.iterdir())) == 1
    assert database.read_bytes() == original_database

    renamed_preview = client.post(
        "/api/manage/pdf-intake/preview", files={"files": ("renamed.pdf", valid, "application/pdf")},
    ).json()
    assert renamed_preview["preview_digest"] != first_preview["preview_digest"]
    renamed_mismatch = client.post(
        "/api/manage/pdf-intake/import", params=_confirmed_preview(first_preview),
        files={"files": ("renamed.pdf", valid, "application/pdf")},
    )
    assert renamed_mismatch.status_code == 422
    renamed = client.post(
        "/api/manage/pdf-intake/import", params=_confirmed_preview(renamed_preview),
        files={"files": ("renamed.pdf", valid, "application/pdf")},
    )
    assert renamed.status_code == 200
    assert renamed.json()["batch_id"] != queued.json()["batch_id"]
    assert len(list(queue.iterdir())) == 2
    assert database.read_bytes() == original_database


def test_pdf_import_rejects_changed_typesafe_result_after_preview(monkeypatch, tmp_path):
    client, database = _client(monkeypatch, tmp_path)
    real_import = api_server.import_pdf_reports
    labels = iter(("article", "event"))

    def import_with_changing_label(inputs):
        bundle = real_import(inputs)
        bundle["typesafe"] = {"status": "complete", "classified": 1, "failed_batches": 0}
        bundle["articles"][0]["type_safe_classification"] = {
            "provider": "typesafe", "label": next(labels)
        }
        return bundle

    monkeypatch.setattr(api_server, "import_pdf_reports", import_with_changing_label)
    payload = {"files": ("report.pdf", _pdf_bytes(), "application/pdf")}
    preview = client.post("/api/manage/pdf-intake/preview", files=payload).json()
    original = database.read_bytes()
    rejected = client.post("/api/manage/pdf-intake/import", params=_confirmed_preview(preview), files=payload)
    assert rejected.status_code == 422
    assert "details do not match" in rejected.json()["detail"]
    assert database.read_bytes() == original


def test_pdf_import_disables_write_without_configured_targets(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    monkeypatch.delenv("CLIMATE_PDF_INTAKE_QUEUE_DIR")
    preview = client.post("/api/manage/pdf-intake/preview", files={"files": ("report.pdf", _pdf_bytes(), "application/pdf")})
    assert preview.status_code == 200
    assert preview.json()["writable"] is False
    assert "batch processing is not configured" in preview.json()["error"]
    blocked = client.post("/api/manage/pdf-intake/import?confirmed=true", files={"files": ("report.pdf", _pdf_bytes(), "application/pdf")})
    assert blocked.status_code == 503


def test_pdf_import_rejects_a_non_sqlite_registry_without_writing(monkeypatch, tmp_path):
    client, database = _client(monkeypatch, tmp_path)
    original = b"not a SQLite database"
    database.write_bytes(original)
    payload = {"files": ("report.pdf", _pdf_bytes(), "application/pdf")}

    preview = client.post("/api/manage/pdf-intake/preview", files=payload)
    assert preview.status_code == 200
    assert preview.json()["writable"] is True

    queued = client.post(
        "/api/manage/pdf-intake/import",
        params=_confirmed_preview(preview.json()),
        files=payload,
    )
    assert queued.status_code == 200
    assert queued.json()["stage"] == "queued"
    assert database.read_bytes() == original


def test_pdf_import_never_creates_site_backup_directory(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    backup = tmp_path / "backups"
    backup.rmdir()
    payload = {"files": ("report.pdf", _pdf_bytes(), "application/pdf")}
    preview = client.post("/api/manage/pdf-intake/preview", files=payload)
    assert preview.status_code == 200
    assert preview.json()["writable"] is True
    assert not backup.exists()

    imported = client.post(
        "/api/manage/pdf-intake/import", params=_confirmed_preview(preview.json()),
        files=payload,
    )
    assert imported.status_code == 200, imported.text
    assert not backup.exists()
