from __future__ import annotations

import json
import os
import sqlite3
from io import BytesIO

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

from agentic_wiki import AgenticWikiResponder
from agentic_wiki.wiki_agent import _evidence_excerpt
from climate_monitor.pdf_intake import import_pdf_reports
from climate_registry.pdf_pipeline import (
    PdfIntakePipeline,
    enqueue_pdf_batch,
    load_active_projection,
    read_pdf_batch,
    retry_pdf_batch,
)
from climate_registry.range_reports import freeze_range_report
from climate_registry.read_api import RegistryReader
from climate_registry.schema import apply_migrations


def _pdf_bytes(summary: str = "New PDF evidence says transition planning needs regional stress tests.") -> bytes:
    output = BytesIO()
    canvas = Canvas(output, pagesize=letter)
    canvas.drawString(50, 760, "Climate Risk Intelligence Report")
    canvas.showPage()
    for y, line in (
        (760, "UPDATES"),
        (740, "Climate transition study"),
        (720, "IN WINDOW 14 SEP 2026 REPORT"),
        (700, summary),
        (680, "Source: Example"),
    ):
        canvas.drawString(50, y, line)
    canvas.linkURL("https://example.org/climate-study", (48, 738, 280, 754), relative=0)
    canvas.showPage()
    canvas.save()
    return output.getvalue()


def _registry(path) -> None:
    with sqlite3.connect(path) as connection:
        apply_migrations(connection)
        connection.execute(
            "INSERT INTO sources VALUES ('source-example', 'example.org', 'Example', "
            "'2026-09-01', '2026-09-01')"
        )
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


def _bundle(tmp_path, *, filename="IAA_CSC_Climate_Report_20260928.pdf", summary=None):
    source = tmp_path / filename
    source.write_bytes(_pdf_bytes(summary) if summary else _pdf_bytes())
    bundle = import_pdf_reports([source])
    article = next(
        item
        for item in bundle["articles"]
        if item["canonical_url"] == "https://example.org/climate-study"
    )
    article["type_safe_classification"] = {"provider": "typesafe", "label": "article"}
    bundle["documents"][0].update(period_start="2026-09-14", period_end="2026-09-14")
    return bundle


def _setup(tmp_path, *, occurrence_summary: str | None = None):
    database = tmp_path / "registry" / "article-registry.sqlite3"
    database.parent.mkdir()
    _registry(database)
    queue, runtime, base_wiki, sources = (
        tmp_path / name for name in ("queue", "runtime-wiki", "base-wiki", "sources")
    )
    for directory in (queue, runtime, base_wiki, sources):
        directory.mkdir()
    (base_wiki / "manual.md").write_text(
        "# Manual page\n\nExisting weekly content.\n", encoding="utf-8"
    )
    bundle = _bundle(tmp_path)
    if occurrence_summary is not None:
        article = next(
            item
            for item in bundle["articles"]
            if item["canonical_url"] == "https://example.org/climate-study"
        )
        article["occurrences"][0]["summary"] = occurrence_summary
    status = enqueue_pdf_batch(queue, bundle, repository_root=tmp_path / "repository")
    return database, queue, runtime, base_wiki, sources, status


def test_pdf_citation_survives_excerpt_truncation():
    citation = (
        "PDF: IAA_CSC_Climate_Report_20260928.pdf; SHA-256: "
        + "a" * 64
        + "; page 30"
    )
    excerpt = _evidence_excerpt("Evidence " + "detail " * 200, 80, citation_text=citation)
    assert "IAA_CSC_Climate_Report_20260928.pdf" in excerpt
    assert "page 30" in excerpt


def test_projection_keeps_citation_after_summary_markdown_subheading(tmp_path):
    database, queue, runtime, _, _, status = _setup(
        tmp_path,
        occurrence_summary=(
            "Transition planning evidence.\n\n"
            "## Embedded report subheading\n\n"
            "Regional stress tests remain necessary."
        ),
    )
    result = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        lambda _generation_id: None,
        repository_root=tmp_path / "repository",
    ).process(status["batch_id"])

    assert result["stage"] == "chat_ready", result["error"]
    projection, _ = load_active_projection(runtime)
    page = (projection / "registry-pdf-intake-observations.md").read_text(
        encoding="utf-8"
    )
    assert "## Embedded report subheading" in page
    assert "IAA_CSC_Climate_Report_20260928.pdf" in page
    assert "page 2" in page


def test_reload_failure_retries_one_imported_pdf_without_duplicate_rows_or_pages(tmp_path):
    database, queue, runtime, base_wiki, _, status = _setup(tmp_path)
    original_base = (base_wiki / "manual.md").read_bytes()

    def fail_reload(_generation_id: str) -> None:
        raise RuntimeError("reload unavailable")

    pipeline = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        fail_reload,
        repository_root=tmp_path / "repository",
    )
    failed = pipeline.process(status["batch_id"])
    assert failed["stage"] == "failed"
    assert failed["imported"] is failed["indexed"] is True
    assert failed["chat_ready"] is False and "reload unavailable" in failed["error"]
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            "SELECT (SELECT COUNT(*) FROM pdf_intake_documents), "
            "(SELECT COUNT(*) FROM pdf_intake_article_occurrences)"
        ).fetchone()
    projection = runtime / "generations" / failed["generation_id"]
    pages_before = sorted(path.name for path in projection.glob("*.md"))
    generations_before = sorted(path.name for path in (runtime / "generations").iterdir())
    observation = projection / "registry-pdf-intake-observations.md"
    citation_count = observation.read_text(encoding="utf-8").count(
        "IAA_CSC_Climate_Report_20260928.pdf"
    )
    assert citation_count == 1

    assert retry_pdf_batch(queue, status["batch_id"])["stage"] == "queued"
    ready = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        lambda _generation_id: None,
        repository_root=tmp_path / "repository",
    ).process(status["batch_id"])
    assert ready["stage"] == "chat_ready"
    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT (SELECT COUNT(*) FROM pdf_intake_documents), "
            "(SELECT COUNT(*) FROM pdf_intake_article_occurrences)"
        ).fetchone()
    projection, _ = load_active_projection(runtime)
    assert after == before
    assert ready["generation_id"] == failed["generation_id"]
    assert sorted(path.name for path in (runtime / "generations").iterdir()) == generations_before
    assert sorted(path.name for path in projection.glob("*.md")) == pages_before
    assert observation.read_text(encoding="utf-8").count(
        "IAA_CSC_Climate_Report_20260928.pdf"
    ) == citation_count
    assert (base_wiki / "manual.md").read_bytes() == original_base


@pytest.mark.skipif(os.name == "nt", reason="api_server management locking requires POSIX fcntl")
def test_failed_first_reload_is_not_selected_on_startup_then_retry_activates(
    monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient

    import api_server

    database, queue, runtime, base_wiki, sources, status = _setup(tmp_path)

    failed = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        lambda _generation_id: (_ for _ in ()).throw(RuntimeError("reload unavailable")),
        repository_root=tmp_path / "repository",
    ).process(status["batch_id"])
    assert failed["stage"] == "failed" and failed["chat_ready"] is False

    with sqlite3.connect(database) as connection:
        rows_before = connection.execute(
            "SELECT (SELECT COUNT(*) FROM pdf_intake_documents), "
            "(SELECT COUNT(*) FROM pdf_intake_article_occurrences)"
        ).fetchone()
    generations_before = sorted(path.name for path in (runtime / "generations").iterdir())

    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "PDF_RUNTIME_WIKI_DIR", runtime)
    monkeypatch.setattr(api_server, "WIKI_DIR", base_wiki)
    monkeypatch.setattr(api_server, "SOURCE_DIR", sources)
    monkeypatch.setattr(api_server, "RELOAD_TOKEN", "reload-token")
    startup_projection, metadata = api_server._selected_pdf_projection()
    assert startup_projection is None and metadata is None
    monkeypatch.setattr(
        api_server, "responder", AgenticWikiResponder(base_wiki, sources, startup_projection)
    )
    monkeypatch.setattr(api_server._wiki_static_files, "all_directories", [str(base_wiki)])
    client = TestClient(api_server.app)

    assert client.get("/wiki/registry-pdf-intake-observations.md").status_code == 404
    hidden = client.post(
        "/api/chat",
        json={
            "message": "What does the PDF say about regional stress tests?",
            "answerMode": "brief",
        },
    ).json()
    assert all(
        item["path"] != "wiki/registry-pdf-intake-observations.md"
        for item in hidden["sources"]
    )
    assert "IAA_CSC_Climate_Report_20260928.pdf" not in hidden["text"]

    assert retry_pdf_batch(queue, status["batch_id"])["stage"] == "queued"

    def reload_chat(generation_id: str) -> None:
        response = client.post("/api/reload", headers={"x-reload-token": "reload-token"})
        assert response.status_code == 200, response.text
        assert response.json()["pdf_projection"]["generation_id"] == generation_id

    ready = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        reload_chat,
        repository_root=tmp_path / "repository",
    ).process(status["batch_id"])
    assert ready["stage"] == "chat_ready" and ready["chat_ready"] is True

    with sqlite3.connect(database) as connection:
        rows_after = connection.execute(
            "SELECT (SELECT COUNT(*) FROM pdf_intake_documents), "
            "(SELECT COUNT(*) FROM pdf_intake_article_occurrences)"
        ).fetchone()
    assert rows_after == rows_before
    assert sorted(path.name for path in (runtime / "generations").iterdir()) == generations_before
    projection, active = load_active_projection(runtime)
    assert active["generation_id"] == failed["generation_id"] == ready["generation_id"]
    assert sorted(path.name for path in projection.glob("*.md")) == [
        "registry-pdf-intake-observations.md"
    ]
    page = client.get("/wiki/registry-pdf-intake-observations.md")
    assert page.status_code == 200
    assert "IAA_CSC_Climate_Report_20260928.pdf" in page.text
    answer = client.post(
        "/api/chat",
        json={
            "message": "What does the PDF say about regional stress tests?",
            "answerMode": "brief",
        },
    ).json()
    citation = next(
        item
        for item in answer["sources"]
        if item["path"] == "wiki/registry-pdf-intake-observations.md"
    )
    assert "IAA_CSC_Climate_Report_20260928.pdf" in citation["snippet"]
    assert "page 2" in citation["snippet"]


def _renamed_bundle(queue, batch_id: str, filename: str) -> dict:
    bundle = json.loads((queue / batch_id / "bundle.json").read_text(encoding="utf-8"))
    source = bundle["documents"][0]["source"]
    source["filename"] = filename
    source["path"] = f"manage-upload://renamed/{filename}"
    for observation in source["source_observations"]:
        observation.update(filename=filename, path=f"manage-upload://renamed/{filename}")
    for article in bundle["articles"]:
        for occurrence in article["occurrences"]:
            occurrence["source_document"] = filename
    return bundle


def test_new_single_pdf_batch_with_same_bytes_records_renamed_source_once(tmp_path):
    database, queue, runtime, _, _, first = _setup(tmp_path)
    pipeline = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        lambda _generation_id: None,
        repository_root=tmp_path / "repository",
    )
    assert pipeline.process(first["batch_id"])["chat_ready"] is True

    second = enqueue_pdf_batch(
        queue,
        _renamed_bundle(queue, first["batch_id"], "renamed.pdf"),
        repository_root=tmp_path / "repository",
    )
    assert second["batch_id"] != first["batch_id"]
    assert pipeline.process(second["batch_id"])["chat_ready"] is True

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_documents").fetchone()[0] == 1
        occurrences = connection.execute(
            "SELECT COUNT(*) FROM pdf_intake_article_occurrences"
        ).fetchone()[0]
        filenames = {
            row[0]
            for row in connection.execute("SELECT filename FROM pdf_intake_document_sources")
        }
    assert filenames == {"IAA_CSC_Climate_Report_20260928.pdf", "renamed.pdf"}
    generations = sorted(path.name for path in (runtime / "generations").iterdir())

    assert pipeline.process(first["batch_id"])["chat_ready"] is True
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM pdf_intake_article_occurrences"
        ).fetchone()[0] == occurrences
    assert sorted(path.name for path in (runtime / "generations").iterdir()) == generations


def test_older_filename_retry_publishes_compatible_newer_projection(tmp_path):
    database, queue, runtime, _, _, older = _setup(tmp_path)
    newer = enqueue_pdf_batch(
        queue,
        _renamed_bundle(queue, older["batch_id"], "renamed.pdf"),
        repository_root=tmp_path / "repository",
    )
    newer_pipeline = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        lambda _generation_id: None,
        repository_root=tmp_path / "repository",
    )

    assert newer_pipeline.process(newer["batch_id"])["chat_ready"] is True
    _, active_before = load_active_projection(runtime)

    def fail_reload(_generation_id: str) -> None:
        raise RuntimeError("reload unavailable")

    failed = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        fail_reload,
        repository_root=tmp_path / "repository",
    ).process(older["batch_id"])
    projection, active_after_failure = load_active_projection(runtime)
    assert failed["stage"] == "failed" and failed["chat_ready"] is False
    assert "reload unavailable" in failed["error"]
    assert active_after_failure["generation_id"] == active_before["generation_id"]
    old_page = (projection / "registry-pdf-intake-observations.md").read_text(
        encoding="utf-8"
    )
    assert "renamed.pdf" in old_page
    assert "IAA_CSC_Climate_Report_20260928.pdf" not in old_page

    retry_pdf_batch(queue, older["batch_id"])
    ready = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        lambda _generation_id: None,
        repository_root=tmp_path / "repository",
    ).process(older["batch_id"])
    projection, active_after_retry = load_active_projection(runtime)
    assert ready["chat_ready"] is True
    assert active_after_retry["batch_id"] == newer["batch_id"]
    assert active_after_retry["generation_id"] == ready["generation_id"]
    assert active_after_retry["generation_id"] != active_before["generation_id"]
    page = (projection / "registry-pdf-intake-observations.md").read_text(
        encoding="utf-8"
    )
    assert "renamed.pdf" in page
    assert "IAA_CSC_Climate_Report_20260928.pdf" in page
    assert read_pdf_batch(queue, newer["batch_id"])["generation_id"] == ready[
        "generation_id"
    ]


def test_process_next_uses_created_order_instead_of_batch_hash(tmp_path):
    database, queue, runtime, _, _, first = _setup(tmp_path)
    second = enqueue_pdf_batch(
        queue,
        _renamed_bundle(queue, first["batch_id"], "renamed.pdf"),
        repository_root=tmp_path / "repository",
    )
    statuses = [read_pdf_batch(queue, item["batch_id"]) for item in (first, second)]
    earlier, later = sorted(statuses, key=lambda item: item["batch_id"], reverse=True)
    earlier["created_at"] = "2026-09-30T00:00:00Z"
    later["created_at"] = "2026-09-30T00:00:01Z"
    for status in (earlier, later):
        (queue / status["batch_id"] / "status.json").write_text(
            json.dumps(status), encoding="utf-8"
        )
    pipeline = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        lambda _generation_id: None,
        repository_root=tmp_path / "repository",
    )

    assert pipeline.process_next()["batch_id"] == earlier["batch_id"]
    assert read_pdf_batch(queue, later["batch_id"])["stage"] == "queued"


@pytest.mark.parametrize("retry_succeeds", [True, False])
def test_retry_after_two_failed_batches_activates_only_on_success(
    tmp_path, retry_succeeds
):
    database, queue, runtime, _, _, first = _setup(tmp_path)

    def fail_reload(_generation_id: str) -> None:
        raise RuntimeError("reload unavailable")

    failing = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        fail_reload,
        repository_root=tmp_path / "repository",
    )
    assert failing.process(first["batch_id"])["stage"] == "failed"

    second = enqueue_pdf_batch(
        queue,
        _renamed_bundle(queue, first["batch_id"], "newer.pdf"),
        repository_root=tmp_path / "repository",
    )
    newer = failing.process(second["batch_id"])
    assert newer["stage"] == "failed"
    assert load_active_projection(runtime) == (None, None)

    reloaded = []

    def retry_reload(generation_id: str) -> None:
        reloaded.append(generation_id)
        if not retry_succeeds:
            raise RuntimeError("reload still unavailable")

    pipeline = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        retry_reload,
        repository_root=tmp_path / "repository",
    )
    retry_pdf_batch(queue, first["batch_id"])
    retried = pipeline.process(first["batch_id"])
    assert reloaded == [retried["generation_id"]]
    if retry_succeeds:
        _, active = load_active_projection(runtime)
        assert retried["chat_ready"] is True
        assert active["generation_id"] == retried["generation_id"]
        assert read_pdf_batch(queue, second["batch_id"])["chat_ready"] is False
    else:
        assert load_active_projection(runtime) == (None, None)
        assert retried["stage"] == "failed" and retried["chat_ready"] is False
        assert read_pdf_batch(queue, second["batch_id"])["chat_ready"] is False


@pytest.mark.skipif(os.name == "nt", reason="api_server management locking requires POSIX fcntl")
def test_authenticated_api_accepts_one_pdf_and_rejects_zero_or_multiple_before_parse(
    monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient
    from fastapi_users.password import PasswordHelper

    import api_server

    database = tmp_path / "registry.sqlite3"
    _registry(database)
    original_database = database.read_bytes()
    queue = tmp_path / "queue"
    queue.mkdir()
    monkeypatch.setenv("CLIMATE_PDF_INTAKE_QUEUE_DIR", str(queue))
    monkeypatch.setenv("CLIMATE_CONSOLE_USERNAME", "operator")
    monkeypatch.setenv("CLIMATE_CONSOLE_PASSWORD_HASH", PasswordHelper().hash("correct horse"))
    monkeypatch.setenv("CLIMATE_CONSOLE_SESSION_SECRET", "test-secret-with-at-least-32-bytes")
    api_server._LOGIN_LIMITER.reset()
    client = TestClient(api_server.app, base_url="https://testserver")
    assert client.post(
        "/api/manage/auth/login",
        data={"username": "operator", "password": "correct horse"},
    ).status_code == 204

    parse_calls = 0
    real_import = api_server.import_pdf_reports

    def counted_import(inputs):
        nonlocal parse_calls
        parse_calls += 1
        return real_import(inputs)

    monkeypatch.setattr(api_server, "import_pdf_reports", counted_import)
    assert client.post("/api/manage/pdf-intake/preview").status_code == 422
    two = [
        ("files", ("one.pdf", _pdf_bytes(), "application/pdf")),
        ("files", ("two.pdf", _pdf_bytes(), "application/pdf")),
    ]
    assert client.post("/api/manage/pdf-intake/preview", files=two).status_code == 422
    assert client.post(
        "/api/manage/pdf-intake/import?confirmed=true", files=two
    ).status_code == 422
    assert parse_calls == 0
    assert not any(queue.iterdir())
    assert database.read_bytes() == original_database

    one = {"files": ("IAA_CSC_Climate_Report_20260928.pdf", _pdf_bytes(), "application/pdf")}
    preview = client.post("/api/manage/pdf-intake/preview", files=one)
    assert preview.status_code == 200
    response = client.post(
        "/api/manage/pdf-intake/import",
        params={
            "confirmed": "true",
            "preview_digest": preview.json()["preview_digest"],
            "preview_sha": preview.json()["documents"][0]["sha256"],
        },
        files=one,
    )
    assert response.status_code == 200, response.text
    assert response.json()["stage"] == "queued"
    assert len(list(queue.iterdir())) == 1

    html = (api_server.MANAGE_DIR / "pdf_import.html").read_text(encoding="utf-8")
    script = (api_server.MANAGE_DIR / "pdf_import.js").read_text(encoding="utf-8")
    assert " multiple" not in html
    assert "files.length !== 1" in script


@pytest.mark.skipif(os.name == "nt", reason="api_server management locking requires POSIX fcntl")
def test_ready_pdf_survives_weekly_registry_change_and_real_reload_with_current_base(
    monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient

    import api_server

    database, queue, runtime, base_wiki, sources, queued = _setup(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "PDF_RUNTIME_WIKI_DIR", runtime)
    monkeypatch.setattr(api_server, "WIKI_DIR", base_wiki)
    monkeypatch.setattr(api_server, "SOURCE_DIR", sources)
    monkeypatch.setattr(api_server, "RELOAD_TOKEN", "reload-token")
    monkeypatch.setattr(api_server, "responder", AgenticWikiResponder(base_wiki, sources))
    monkeypatch.setattr(api_server._wiki_static_files, "all_directories", [str(base_wiki)])
    client = TestClient(api_server.app)

    def reload_chat(generation_id: str) -> None:
        response = client.post("/api/reload", headers={"x-reload-token": "reload-token"})
        assert response.status_code == 200, response.text
        assert response.json()["pdf_projection"]["generation_id"] == generation_id

    ready = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        reload_chat,
        repository_root=tmp_path / "repository",
    ).process(queued["batch_id"])
    assert ready["chat_ready"] is True
    projection, metadata = load_active_projection(runtime)
    assert sorted(path.name for path in projection.glob("*.md")) == [
        "registry-pdf-intake-observations.md"
    ]

    # Simulate the scheduled Registry promotion and deployed base projection update.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE sources SET last_seen = '2026-09-30' WHERE source_id = 'source-example'"
        )
    (base_wiki / "article-core-climate-study.md").write_text(
        "# Core climate study\n\nUpdated weekly Registry projection.\n", encoding="utf-8"
    )
    refreshed = client.post("/api/reload", headers={"x-reload-token": "reload-token"})
    assert refreshed.status_code == 200
    assert refreshed.json()["pdf_projection"]["generation_id"] == metadata["generation_id"]

    pdf_answer = client.post(
        "/api/chat",
        json={
            "message": "What does the PDF say about regional stress tests?",
            "answerMode": "brief",
        },
    ).json()
    citation = next(
        item
        for item in pdf_answer["sources"]
        if item["path"] == "wiki/registry-pdf-intake-observations.md"
    )
    assert "IAA_CSC_Climate_Report_20260928.pdf" in citation["snippet"]
    assert "page 2" in citation["snippet"]

    base_answer = client.post(
        "/api/chat",
        json={"message": "What is in the updated weekly Registry projection?", "answerMode": "brief"},
    ).json()
    assert "Updated weekly Registry projection" in base_answer["text"]
    assert "Updated weekly Registry projection" in client.get(
        "/wiki/article-core-climate-study.md"
    ).text
    pdf_page = client.get("/wiki/registry-pdf-intake-observations.md")
    assert pdf_page.status_code == 200
    assert "IAA_CSC_Climate_Report_20260928.pdf" in pdf_page.text
    assert "page 2" in pdf_page.text
    assert read_pdf_batch(queue, queued["batch_id"])["chat_ready"] is True

    snapshot = freeze_range_report(
        RegistryReader(database, repository_root=tmp_path / "repository"),
        tmp_path / "range-reports",
        start_date="2026-09-14",
        end_date="2026-09-14",
    )
    observations = [
        item for article in snapshot["articles"] for item in article["source_observations"]
    ]
    assert any(
        item["kind"] == "registry_pdf"
        and item["batch_id"] == queued["batch_id"]
        and item["period_start"] == "2026-09-14"
        and item["period_end"] == "2026-09-14"
        and item["filename"] == "IAA_CSC_Climate_Report_20260928.pdf"
        and item["page"] == 2
        for item in observations
    )


def test_later_success_excludes_failed_batch_until_retry(tmp_path):
    database, queue, runtime, base_wiki, sources, failed_batch = _setup(tmp_path)

    def fail_reload(_generation_id: str) -> None:
        raise RuntimeError("reload unavailable")

    failed = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        fail_reload,
        repository_root=tmp_path / "repository",
    ).process(failed_batch["batch_id"])
    assert failed["stage"] == "failed" and failed["chat_ready"] is False

    later_bundle = _bundle(
        tmp_path,
        filename="later-report.pdf",
        summary="Later ready evidence says transition planning needs scenario analysis.",
    )
    later = enqueue_pdf_batch(queue, later_bundle, repository_root=tmp_path / "repository")
    ready = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        lambda _generation_id: None,
        repository_root=tmp_path / "repository",
    ).process(later["batch_id"])
    assert ready["chat_ready"] is True
    projection, _ = load_active_projection(runtime)
    page = (projection / "registry-pdf-intake-observations.md").read_text(encoding="utf-8")
    assert "later-report.pdf" in page
    assert "IAA_CSC_Climate_Report_20260928.pdf" not in page
    rag_corpus = "\n".join(
        document.markdown
        for document in AgenticWikiResponder(base_wiki, sources, projection).kb.documents
    )
    assert "later-report.pdf" in rag_corpus
    assert "IAA_CSC_Climate_Report_20260928.pdf" not in rag_corpus

    with sqlite3.connect(database) as connection:
        counts_before = connection.execute(
            "SELECT (SELECT COUNT(*) FROM pdf_intake_documents), "
            "(SELECT COUNT(*) FROM pdf_intake_article_occurrences)"
        ).fetchone()
    retry_pdf_batch(queue, failed_batch["batch_id"])
    retried = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        lambda _generation_id: None,
        repository_root=tmp_path / "repository",
    ).process(failed_batch["batch_id"])
    assert retried["chat_ready"] is True
    projection, _ = load_active_projection(runtime)
    page = (projection / "registry-pdf-intake-observations.md").read_text(encoding="utf-8")
    assert page.count("later-report.pdf") == 1
    assert page.count("IAA_CSC_Climate_Report_20260928.pdf") == 1
    with sqlite3.connect(database) as connection:
        counts_after = connection.execute(
            "SELECT (SELECT COUNT(*) FROM pdf_intake_documents), "
            "(SELECT COUNT(*) FROM pdf_intake_article_occurrences)"
        ).fetchone()
    assert counts_after == counts_before


def test_queue_rejects_repository_storage(tmp_path):
    repository = tmp_path / "repository"
    queue = repository / "queue"
    queue.mkdir(parents=True)
    with pytest.raises(ValueError, match="outside the repository"):
        enqueue_pdf_batch(queue, {"documents": [{}]}, repository_root=repository)
