from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

from agentic_wiki import AgenticWikiResponder
from agentic_wiki.wiki_agent import _evidence_excerpt
from climate_delivery.errors import LockStateError
from climate_delivery.io import exclusive_lock
from climate_monitor.pdf_intake import import_pdf_reports
from climate_registry.pdf_intake import persist_pdf_intake
from climate_registry.pdf_pipeline import (
    PdfIntakePipeline,
    enqueue_pdf_batch,
    list_pdf_batches,
    load_active_projection,
    load_projection_manifest,
    read_pdf_batch,
    retry_pdf_batch,
)
from climate_registry.range_reports import (
    ensure_range_report_pdf,
    freeze_range_report,
    load_active_range_overlay,
    render_range_report_html,
)
from climate_registry.read_api import RegistryReader
from climate_registry.schema import apply_migrations
from climate_registry.wiki import sync_registry_wiki


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


def _ack_activation(queue):
    def reload_chat(generation_id: str) -> None:
        pending = json.loads((queue / "pending.json").read_text(encoding="utf-8"))
        assert pending["generation_id"] == generation_id
        os.replace(queue / "pending.json", queue / "active.json")

    return reload_chat


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
    database, queue, runtime, base_wiki, sources, status = _setup(
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
        _ack_activation(queue),
        repository_root=tmp_path / "repository",
    ).process(status["batch_id"])

    assert result["stage"] == "chat_ready", result["error"]
    projection, _ = load_active_projection(runtime, queue / "active.json")
    page = (projection / "article-core-climate-study.md").read_text(
        encoding="utf-8"
    )
    assert "## Embedded report subheading" in page
    assert "IAA_CSC_Climate_Report_20260928.pdf" in page
    assert "page 2" in page
    hit = next(
        item
        for item in AgenticWikiResponder(base_wiki, sources, projection).kb.search(
            "Transition planning evidence", top_k=10
        )
        if "Transition planning evidence" in item.chunk.text
    )
    assert "IAA_CSC_Climate_Report_20260928.pdf" in hit.chunk.markdown
    assert "page 2" in hit.chunk.markdown


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
    observation = projection / "article-core-climate-study.md"
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
        _ack_activation(queue),
        repository_root=tmp_path / "repository",
    ).process(status["batch_id"])
    assert ready["stage"] == "chat_ready"
    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT (SELECT COUNT(*) FROM pdf_intake_documents), "
            "(SELECT COUNT(*) FROM pdf_intake_article_occurrences)"
        ).fetchone()
    projection, _ = load_active_projection(runtime, queue / "active.json")
    assert after == before
    assert ready["generation_id"] == failed["generation_id"]
    assert sorted(path.name for path in (runtime / "generations").iterdir()) == generations_before
    assert sorted(path.name for path in projection.glob("*.md")) == pages_before
    assert observation.read_text(encoding="utf-8").count(
        "IAA_CSC_Climate_Report_20260928.pdf"
    ) == citation_count
    assert (base_wiki / "manual.md").read_bytes() == original_base


def test_private_writer_activation_never_mutates_or_exposes_public_registry(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    public_db = tmp_path / "public" / "article-registry.sqlite3"
    public_db.parent.mkdir()
    _registry(public_db)
    public_bundle = _bundle(
        tmp_path,
        filename="public-history.pdf",
        summary="Existing approved Public PDF history.",
    )
    persist_pdf_intake(public_db, tmp_path / "public-backups", public_bundle)
    public_bytes = public_db.read_bytes()

    writer_db = tmp_path / "pipeline" / "climate_registry.sqlite3"
    writer_db.parent.mkdir()
    _registry(writer_db)
    queue, runtime = tmp_path / "queue", tmp_path / "runtime"
    queue.mkdir()
    runtime.mkdir()
    pending_bundle = _bundle(
        tmp_path,
        filename="pending-import.pdf",
        summary="PENDING IMPORT SENTINEL before activation.",
    )
    queued = enqueue_pdf_batch(queue, pending_bundle, repository_root=repository)
    failed = PdfIntakePipeline(
        queue, writer_db, tmp_path / "pipeline" / "backups", runtime,
        lambda _generation_id: (_ for _ in ()).throw(RuntimeError("reload unavailable")),
        repository_root=repository,
    ).process(queued["batch_id"])
    assert failed["imported"] is True and failed["indexed"] is True
    assert failed["chat_ready"] is False

    web_reader, pdf_reader, manifest = load_active_range_overlay(
        runtime, queue, repository_root=repository
    )
    assert (web_reader, pdf_reader, manifest) == (
        None, None, {"web_items": [], "pdf_occurrence_ids": []},
    )
    public_report = freeze_range_report(
        RegistryReader(public_db, repository_root=repository),
        tmp_path / "public-report",
        start_date="2026-09-14", end_date="2026-09-14",
        overlay_reader=web_reader,
        pdf_overlay_reader=pdf_reader,
        overlay_manifest=manifest,
    )
    assert "Existing approved Public PDF history" in json.dumps(public_report)
    assert "PENDING IMPORT SENTINEL" not in json.dumps(public_report)
    public_wiki = tmp_path / "public-wiki"
    sync_registry_wiki(public_db, public_wiki)
    public_corpus = "\n".join(
        path.read_text(encoding="utf-8") for path in public_wiki.glob("*.md")
    )
    assert "Existing approved Public PDF history" in public_corpus
    assert "PENDING IMPORT SENTINEL" not in public_corpus
    assert public_db.read_bytes() == public_bytes

    retry_pdf_batch(queue, queued["batch_id"])
    ready = PdfIntakePipeline(
        queue, writer_db, tmp_path / "pipeline" / "backups", runtime,
        _ack_activation(queue), repository_root=repository,
    ).process(queued["batch_id"])
    assert ready["chat_ready"] is True
    generation, _ = load_active_projection(runtime, queue / "active.json")
    runtime_corpus = "\n".join(
        path.read_text(encoding="utf-8") for path in generation.glob("*.md")
    )
    assert "PENDING IMPORT SENTINEL" in runtime_corpus
    assert "PENDING IMPORT SENTINEL" not in public_corpus
    assert public_db.read_bytes() == public_bytes


def test_legacy_occurrence_identity_is_reused_through_reload_failure_and_retry(tmp_path):
    bundle = _bundle(tmp_path, filename="legacy-reimport.pdf")
    article = next(
        item for item in bundle["articles"]
        if item["canonical_url"] == "https://example.org/climate-study"
    )
    occurrence = article["occurrences"][0]
    document = bundle["documents"][0]
    database = tmp_path / "legacy-registry.sqlite3"
    legacy_id = "legacy-v13-article-occurrence"
    with sqlite3.connect(database) as connection:
        apply_migrations(connection, target_version=13)
        connection.execute(
            "INSERT INTO pdf_intake_documents (document_sha256, source_path, filename, "
            "media_type, size_bytes, date_of_run, period_start, period_end, "
            "extracted_text_sha256, document_json, imported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)",
            (
                document["source"]["sha256"], document["source"]["path"],
                document["source"]["filename"], "application/pdf",
                document["source"]["size_bytes"], document.get("date_of_run"),
                document.get("period_start"), document.get("period_end"),
                document["extracted_text_sha256"], "2026-09-01T00:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO pdf_intake_articles (article_id, canonical_url, title, "
            "type_safe_classification_json, imported_at) VALUES (?, ?, ?, NULL, ?)",
            (
                article["article_id"], article["canonical_url"], article["title"],
                "2026-09-01T00:00:00Z",
            ),
        )
        stored = dict(occurrence, occurrence_id=legacy_id)
        connection.execute(
            "INSERT INTO pdf_intake_article_occurrences (occurrence_id, article_id, "
            "source_document_sha256, page, raw_url, report_date, publication_date, "
            "content_sha256, page_sha256, occurrence_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                legacy_id, article["article_id"], occurrence["source_document_sha256"],
                occurrence["page"], occurrence["raw_url"],
                occurrence.get("report_date"), occurrence.get("publication_date"),
                occurrence["content_sha256"], occurrence["page_sha256"],
                json.dumps(stored),
            ),
        )
    connection.close()

    queue, runtime = tmp_path / "queue", tmp_path / "runtime"
    queue.mkdir()
    runtime.mkdir()
    queued = enqueue_pdf_batch(
        queue, bundle, repository_root=tmp_path / "repository"
    )
    failed = PdfIntakePipeline(
        queue, database, tmp_path / "backups", runtime,
        lambda _generation_id: (_ for _ in ()).throw(RuntimeError("reload unavailable")),
        repository_root=tmp_path / "repository",
    ).process(queued["batch_id"])
    assert failed["indexed"] is True, failed["error"]
    assert failed["stage"] == "failed"
    candidate = runtime / "generations" / failed["generation_id"]
    assert load_projection_manifest(candidate, {
        "generation_id": failed["generation_id"],
        "manifest_sha256": failed["manifest_sha256"],
    })["pdf_occurrence_ids"] == [legacy_id]

    retry_pdf_batch(queue, queued["batch_id"])
    ready = PdfIntakePipeline(
        queue, database, tmp_path / "backups", runtime, _ack_activation(queue),
        repository_root=tmp_path / "repository",
    ).process(queued["batch_id"])
    assert ready["stage"] == "chat_ready"
    assert ready["generation_id"] == failed["generation_id"]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT occurrence_id FROM pdf_intake_article_occurrences"
        ).fetchall() == [(legacy_id,)]


def test_active_pdf_only_article_excludes_unactivated_core_content_from_wiki_and_range(
    tmp_path,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    writer_db = tmp_path / "writer.sqlite3"
    _registry(writer_db)
    with sqlite3.connect(writer_db) as connection:
        connection.execute(
            "INSERT INTO article_versions VALUES "
            "('unactivated-title', 'core-climate-study', 'UNACTIVATED PDF TITLE', "
            "'unactivated pdf title', 'Unactivated summary', ?, "
            "'report-title-summary', '2026-10-03', '2026-10-03')",
            ("f" * 64,),
        )
        body = "UNACTIVATED FULL WEB BODY"
        digest = hashlib.sha256(body.encode()).hexdigest()
        connection.execute(
            "INSERT INTO article_content_versions VALUES "
            "('unactivated-content', 'core-climate-study', ?, ?, ?, "
            "'text/markdown', ?, 'reader', 'reader-v1', '2026-10-03')",
            (digest, body, digest, len(body)),
        )
        connection.execute(
            "UPDATE articles SET current_version_id='unactivated-title', "
            "current_content_version_id='unactivated-content', "
            "display_policy='full_markdown' WHERE article_id='core-climate-study'"
        )
    public_db = tmp_path / "public.sqlite3"
    with sqlite3.connect(public_db) as connection:
        apply_migrations(connection)
    queue, runtime = tmp_path / "queue", tmp_path / "runtime"
    queue.mkdir()
    runtime.mkdir()
    bundle = _bundle(
        tmp_path,
        filename="approved-confirmed.pdf",
        summary=(
            "Approved PDF evidence for transition stress tests. "
            "Published 14 September 2026."
        ),
    )
    approved = next(
        item for item in bundle["articles"]
        if item["canonical_url"] == "https://example.org/climate-study"
    )
    occurrence = approved["occurrences"][0]
    queued = enqueue_pdf_batch(queue, bundle, repository_root=repository)
    ready = PdfIntakePipeline(
        queue, writer_db, tmp_path / "backups", runtime, _ack_activation(queue),
        repository_root=repository,
    ).process(queued["batch_id"])
    assert ready["chat_ready"] is True

    generation, active = load_active_projection(runtime, queue / "active.json")
    page = (generation / "article-core-climate-study.md").read_text(encoding="utf-8")
    expected_title = (
        occurrence.get("anchor_text") or occurrence.get("title")
        or occurrence["raw_url"]
    )
    assert page.startswith(f"# {expected_title}\n")
    assert "UNACTIVATED PDF TITLE" not in page
    assert "UNACTIVATED FULL WEB BODY" not in page
    assert "Approved PDF evidence for transition stress tests" in page
    wiki, sources = tmp_path / "wiki", tmp_path / "sources"
    wiki.mkdir()
    sources.mkdir()
    kb_corpus = "\n".join(
        document.markdown
        for document in AgenticWikiResponder(wiki, sources, generation).kb.documents
    )
    assert "UNACTIVATED PDF TITLE" not in kb_corpus
    assert "Approved PDF evidence for transition stress tests" in kb_corpus

    manifest = load_projection_manifest(generation, active)
    snapshot = freeze_range_report(
        RegistryReader(public_db, repository_root=repository),
        tmp_path / "range-reports",
        start_date="2026-09-14", end_date="2026-09-14",
        pdf_overlay_reader=RegistryReader(
            active["pdf_registry_snapshot"], repository_root=repository
        ),
        overlay_manifest=manifest,
    )
    article = snapshot["articles"][0]
    assert article["title"] == expected_title
    assert article["content_version_id"] is None and article["content"] is None
    html = render_range_report_html(snapshot)
    pdf_text = "\n".join(
        page.extract_text() or ""
        for page in PdfReader(
            ensure_range_report_pdf(snapshot, tmp_path / "range-reports")
        ).pages
    )
    for rendered in (html, pdf_text):
        normalized = " ".join(rendered.split())
        assert "UNACTIVATED PDF TITLE" not in normalized
        assert "UNACTIVATED FULL WEB BODY" not in normalized
        assert (
            "Approved PDF evidence for transition stress tests. "
            "Published 14 September 2026."
        ) in normalized
        assert "approved-confirmed.pdf, page 2" in normalized


def test_active_untitled_pdf_source_ignores_failed_aggregate_title(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    writer_db = tmp_path / "writer.sqlite3"
    public_db = tmp_path / "public.sqlite3"
    for database in (writer_db, public_db):
        with sqlite3.connect(database) as connection:
            apply_migrations(connection)
    queue, runtime = tmp_path / "queue", tmp_path / "runtime"
    queue.mkdir()
    runtime.mkdir()

    failed_bundle = _bundle(
        tmp_path,
        filename="failed-title.pdf",
        summary="Failed PDF evidence with an aggregate title.",
    )
    failed_bundle["articles"][0]["title"] = "UNACTIVATED AGGREGATE PDF TITLE"
    failed = enqueue_pdf_batch(queue, failed_bundle, repository_root=repository)
    failed_status = PdfIntakePipeline(
        queue, writer_db, tmp_path / "backups", runtime,
        lambda _generation_id: (_ for _ in ()).throw(RuntimeError("reload unavailable")),
        repository_root=repository,
    ).process(failed["batch_id"])
    assert failed_status["stage"] == "failed" and failed_status["imported"] is True

    approved_bundle = _bundle(
        tmp_path,
        filename="approved-untitled.pdf",
        summary="Approved PDF source evidence for regional stress tests.",
    )
    approved = approved_bundle["articles"][0]
    approved["title"] = None
    approved_occurrence = approved["occurrences"][0]
    approved_occurrence["anchor_text"] = ""
    approved_occurrence["publication_date"] = None
    approved_occurrence["publication_date_evidence"] = None
    approved_id = approved_occurrence["occurrence_id"]
    approved_url = approved_occurrence["raw_url"]
    queued = enqueue_pdf_batch(queue, approved_bundle, repository_root=repository)
    ready = PdfIntakePipeline(
        queue, writer_db, tmp_path / "backups", runtime, _ack_activation(queue),
        repository_root=repository,
    ).process(queued["batch_id"])
    assert ready["chat_ready"] is True

    generation, active = load_active_projection(runtime, queue / "active.json")
    manifest = load_projection_manifest(generation, active)
    assert manifest["pdf_occurrence_ids"] == [approved_id]
    page = (generation / "registry-source-observations.md").read_text(encoding="utf-8")
    assert "UNACTIVATED AGGREGATE PDF TITLE" not in page
    assert "Approved PDF source evidence for regional stress tests" in page
    wiki, sources = tmp_path / "wiki", tmp_path / "sources"
    wiki.mkdir()
    sources.mkdir()
    kb_corpus = "\n".join(
        document.markdown
        for document in AgenticWikiResponder(wiki, sources, generation).kb.documents
    )
    assert "UNACTIVATED AGGREGATE PDF TITLE" not in kb_corpus
    assert "Approved PDF source evidence for regional stress tests" in kb_corpus

    snapshot = freeze_range_report(
        RegistryReader(public_db, repository_root=repository),
        tmp_path / "range-reports",
        start_date="2026-09-14", end_date="2026-09-14",
        pdf_overlay_reader=RegistryReader(
            active["pdf_registry_snapshot"], repository_root=repository
        ),
        overlay_manifest=manifest,
    )
    update = snapshot["pdf_source_updates"][0]
    assert update["observation_id"] == approved_id
    assert update["title"] == approved_url
    html = render_range_report_html(snapshot)
    assert "UNACTIVATED AGGREGATE PDF TITLE" not in html
    assert "Approved PDF source evidence for regional stress tests" in html
    assert "approved-untitled.pdf" in html


def test_pdf_failures_remain_in_history_after_retry_and_success(tmp_path):
    database, queue, runtime, _, _, status = _setup(tmp_path)

    def fail_reload(_generation_id: str) -> None:
        raise RuntimeError("reload unavailable")

    failed = PdfIntakePipeline(
        queue, database, tmp_path / "backups", runtime, fail_reload,
        repository_root=tmp_path / "repository",
    ).process(status["batch_id"])
    assert failed["failure_history"] == [{
        "attempt": 1, "reason": "RuntimeError: reload unavailable", "at": failed["failure_history"][0]["at"],
    }]

    assert retry_pdf_batch(queue, status["batch_id"])["error"] is None
    ready = PdfIntakePipeline(
        queue, database, tmp_path / "backups", runtime, _ack_activation(queue),
        repository_root=tmp_path / "repository",
    ).process(status["batch_id"])
    assert ready["stage"] == "chat_ready" and ready["chat_ready"] is True
    assert ready["failure_history"] == failed["failure_history"]


def test_retry_preserves_a_legacy_failure_before_clearing_its_error(tmp_path):
    database, queue, runtime, _, _, status = _setup(tmp_path)
    legacy_failure = {
        "at": "2026-10-01T12:00:00Z",
        "attempt": 4,
        "reason": "RuntimeError: legacy reload failure",
    }
    path = queue / status["batch_id"] / "status.json"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    persisted.update(stage="failed", error=legacy_failure["reason"], attempts=4,
                     updated_at=legacy_failure["at"])
    persisted.pop("failure_history")
    path.write_text(json.dumps(persisted), encoding="utf-8")

    retried = retry_pdf_batch(queue, status["batch_id"])
    assert retried["error"] is None
    assert retried["failure_history"] == [legacy_failure]
    ready = PdfIntakePipeline(
        queue, database, tmp_path / "backups", runtime, _ack_activation(queue),
        repository_root=tmp_path / "repository",
    ).process(status["batch_id"])
    assert ready["stage"] == "chat_ready" and ready["failure_history"] == [legacy_failure]


def test_retry_does_not_overwrite_a_batch_while_the_writer_is_processing(tmp_path):
    _, queue, _, _, _, status = _setup(tmp_path)
    path = queue / status["batch_id"] / "status.json"
    failed = json.loads(path.read_text(encoding="utf-8"))
    failed.update(stage="failed", error="RuntimeError: reload unavailable", attempts=1)
    path.write_text(json.dumps(failed), encoding="utf-8")

    with exclusive_lock(queue, "intake-writer"):
        with pytest.raises(LockStateError):
            retry_pdf_batch(queue, status["batch_id"])
    assert read_pdf_batch(queue, status["batch_id"])["stage"] == "failed"


def test_pdf_batch_overview_renders_retained_failure_history():
    script = (Path(__file__).parents[1] / "management_ui" / "pdf_import.js").read_text(
        encoding="utf-8"
    )
    assert "value.failure_history || []" in script
    assert "failure.at" in script and "failure.attempt" in script and "failure.reason" in script
    assert "latest.reason !== value.error" in script


def test_pdf_batch_summary_counts_current_stages_and_milestones_separately(tmp_path):
    _, queue, _, _, _, queued = _setup(tmp_path)
    failed = enqueue_pdf_batch(
        queue, _bundle(tmp_path, filename="failed.pdf"), repository_root=tmp_path / "repository",
    )
    ready = enqueue_pdf_batch(
        queue, _bundle(tmp_path, filename="ready.pdf"), repository_root=tmp_path / "repository",
    )
    for batch, changes in (
        (failed, {"stage": "failed", "imported": True, "indexed": True, "error": "RuntimeError: unavailable"}),
        (ready, {"stage": "chat_ready", "imported": True, "indexed": True, "chat_ready": True}),
    ):
        path = queue / batch["batch_id"] / "status.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value.update(changes)
        path.write_text(json.dumps(value), encoding="utf-8")

    summary = list_pdf_batches(queue)
    assert summary["total_batches"] == 3
    assert summary["stage_counts"] == {"chat_ready": 1, "failed": 1, "queued": 1}
    assert summary["milestone_counts"] == {"imported": 2, "indexed": 2, "chat_ready": 1}
    assert next(item for item in summary["batches"] if item["stage"] == "failed")["filename"] == "failed.pdf"


def test_pdf_retry_appends_another_failure_history_entry(tmp_path):
    database, queue, runtime, _, _, status = _setup(tmp_path)

    def fail_reload(_generation_id: str) -> None:
        raise RuntimeError("reload unavailable")

    pipeline = PdfIntakePipeline(
        queue, database, tmp_path / "backups", runtime, fail_reload,
        repository_root=tmp_path / "repository",
    )
    pipeline.process(status["batch_id"])
    retry_pdf_batch(queue, status["batch_id"])
    failed = pipeline.process(status["batch_id"])
    assert [(item["attempt"], item["reason"]) for item in failed["failure_history"]] == [
        (1, "RuntimeError: reload unavailable"),
        (2, "RuntimeError: reload unavailable"),
    ]


@pytest.mark.skipif(os.name == "nt", reason="api_server management locking requires POSIX fcntl")
def test_legacy_active_pdf_overlay_excludes_later_failed_pdf(monkeypatch, tmp_path):
    import api_server

    repository = tmp_path / "repository"
    repository.mkdir()
    database = tmp_path / "registry.sqlite3"
    _registry(database)
    queue, runtime = tmp_path / "queue", tmp_path / "runtime-wiki"
    queue.mkdir()
    runtime.mkdir()

    def bundle(filename, summary, event_id, event_name):
        value = _bundle(tmp_path, filename=filename, summary=summary)
        value["calendar_items"].append({
            "event_id": event_id,
            "occurrence_id": f"{event_id}-occurrence",
            "source_document_sha256": value["documents"][0]["source"]["sha256"],
            "page": 2,
            "name": event_name,
            "kind": "deadline",
            "raw_date": "2027-03-01",
            "date_precision": "day",
            "start_date": "2027-03-01",
            "end_date": "2027-03-01",
            "summary": event_name,
            "content_sha256": "a" * 64,
            "source_urls": [],
        })
        return value

    first = enqueue_pdf_batch(
        queue,
        bundle("active-a.pdf", "Active PDF A evidence.", "active-a-date", "Active A filing"),
        repository_root=repository,
    )
    ready = PdfIntakePipeline(
        queue, database, tmp_path / "backups", runtime, _ack_activation(queue),
        repository_root=repository,
    ).process(first["batch_id"])
    assert ready["chat_ready"] is True

    generation, active = load_active_projection(runtime, queue / "active.json")
    (generation / "intake-manifest.json").unlink()
    for key in (
        "manifest_sha256", "projection_kind", "registry_snapshot",
        "pdf_registry_snapshot", "pdf_registry_sha256",
        "web_registry_snapshot", "web_registry_sha256",
    ):
        active.pop(key, None)
    (queue / "active.json").write_text(json.dumps(active), encoding="utf-8")

    second = enqueue_pdf_batch(
        queue,
        bundle("failed-b.pdf", "Failed PDF B evidence.", "failed-b-date", "Failed B filing"),
        repository_root=repository,
    )
    failed = PdfIntakePipeline(
        queue, database, tmp_path / "backups", runtime,
        lambda _generation_id: (_ for _ in ()).throw(RuntimeError("reload unavailable")),
        repository_root=repository,
    ).process(second["batch_id"])
    assert failed["stage"] == "failed" and failed["chat_ready"] is False

    monkeypatch.setenv("CLIMATE_PDF_INTAKE_QUEUE_DIR", str(queue))
    monkeypatch.setattr(api_server, "PDF_RUNTIME_WIKI_DIR", runtime)
    overlay_reader, pdf_overlay_reader, manifest = api_server._range_report_overlay()
    public_database = tmp_path / "public.sqlite3"
    with sqlite3.connect(public_database) as connection:
        apply_migrations(connection)
    report = freeze_range_report(
        RegistryReader(public_database, repository_root=repository),
        tmp_path / "range-reports",
        start_date="2026-09-14", end_date="2026-09-14",
        overlay_reader=overlay_reader,
        pdf_overlay_reader=pdf_overlay_reader,
        overlay_manifest=manifest,
    )
    observations = [
        observation
        for article in report["articles"]
        for observation in article["source_observations"]
        if observation["kind"] == "registry_pdf"
    ]
    assert {item["filename"] for item in observations} == {"active-a.pdf"}
    assert {item["event_id"] for item in report["pdf_calendar"]["records"]} == {
        "active-a-date"
    }
    assert read_pdf_batch(queue, second["batch_id"])["chat_ready"] is False


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

    public_database = tmp_path / "public.sqlite3"
    with sqlite3.connect(public_database) as connection:
        apply_migrations(connection)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(public_database))
    monkeypatch.setenv("CLIMATE_PDF_INTAKE_QUEUE_DIR", str(queue))
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

    assert client.get("/wiki/article-core-climate-study.md").status_code == 404
    hidden = client.post(
        "/api/chat",
        json={
            "message": "What does the PDF say about regional stress tests?",
            "answerMode": "brief",
        },
    ).json()
    assert all(
        item["path"] != "wiki/article-core-climate-study.md"
        for item in hidden["sources"]
    )
    assert "IAA_CSC_Climate_Report_20260928.pdf" not in hidden["text"]

    overlay_reader, pdf_overlay_reader, manifest = api_server._range_report_overlay()
    assert overlay_reader is None and pdf_overlay_reader is None
    assert manifest == {"web_items": [], "pdf_occurrence_ids": []}
    configured_report = freeze_range_report(
        RegistryReader(public_database, repository_root=tmp_path / "repository"),
        tmp_path / "configured-range-reports",
        start_date="2026-09-14", end_date="2026-09-14",
        overlay_reader=overlay_reader, pdf_overlay_reader=pdf_overlay_reader,
        overlay_manifest=manifest,
    )
    assert configured_report["pdf_source_updates"] == []
    assert all(
        observation["kind"] != "registry_pdf"
        for article in configured_report["articles"]
        for observation in article["source_observations"]
    )

    monkeypatch.setattr(api_server, "PDF_RUNTIME_WIKI_DIR", None)
    legacy_overlay = api_server._range_report_overlay()
    assert legacy_overlay == (None, None, None)
    legacy_report = freeze_range_report(
        RegistryReader(database, repository_root=tmp_path / "repository"),
        tmp_path / "legacy-range-reports",
        start_date="2026-09-14", end_date="2026-09-14",
        overlay_reader=legacy_overlay[0], pdf_overlay_reader=legacy_overlay[1],
        overlay_manifest=legacy_overlay[2],
    )
    assert any(
        observation["kind"] == "registry_pdf"
        for article in legacy_report["articles"]
        for observation in article["source_observations"]
    )
    monkeypatch.setattr(api_server, "PDF_RUNTIME_WIKI_DIR", runtime)

    assert retry_pdf_batch(queue, status["batch_id"])["stage"] == "queued"

    def reload_chat(generation_id: str) -> None:
        response = client.post("/api/reload", params={"generation_id": generation_id}, headers={"x-reload-token": "reload-token"})
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
    projection, active = load_active_projection(runtime, queue / "active.json")
    assert active["generation_id"] == failed["generation_id"] == ready["generation_id"]
    assert sorted(path.name for path in projection.glob("*.md")) == [
        "article-core-climate-study.md"
    ]
    page = client.get("/wiki/article-core-climate-study.md")
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
        if item["path"] == "wiki/article-core-climate-study.md"
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
        _ack_activation(queue),
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
        _ack_activation(queue),
        repository_root=tmp_path / "repository",
    )

    assert newer_pipeline.process(newer["batch_id"])["chat_ready"] is True
    _, active_before = load_active_projection(runtime, queue / "active.json")

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
    projection, active_after_failure = load_active_projection(runtime, queue / "active.json")
    assert failed["stage"] == "failed" and failed["chat_ready"] is False
    assert "reload unavailable" in failed["error"]
    assert active_after_failure["generation_id"] == active_before["generation_id"]
    old_page = (projection / "article-core-climate-study.md").read_text(
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
        _ack_activation(queue),
        repository_root=tmp_path / "repository",
    ).process(older["batch_id"])
    projection, active_after_retry = load_active_projection(runtime, queue / "active.json")
    assert ready["chat_ready"] is True
    assert active_after_retry["batch_id"] == newer["batch_id"]
    assert active_after_retry["generation_id"] == ready["generation_id"]
    assert active_after_retry["generation_id"] != active_before["generation_id"]
    page = (projection / "article-core-climate-study.md").read_text(
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
        _ack_activation(queue),
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
    assert load_active_projection(runtime, queue / "active.json") == (None, None)

    reloaded = []

    def retry_reload(generation_id: str) -> None:
        reloaded.append(generation_id)
        if not retry_succeeds:
            raise RuntimeError("reload still unavailable")
        os.replace(queue / "pending.json", queue / "active.json")

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
        _, active = load_active_projection(runtime, queue / "active.json")
        assert retried["chat_ready"] is True
        assert active["generation_id"] == retried["generation_id"]
        assert read_pdf_batch(queue, second["batch_id"])["chat_ready"] is False
    else:
        assert load_active_projection(runtime, queue / "active.json") == (None, None)
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
    assert "error.message; setTimeout(pollBatch, 1000)" in script


@pytest.mark.skipif(os.name == "nt", reason="api_server management locking requires POSIX fcntl")
def test_ready_pdf_overlay_merges_same_path_after_weekly_base_reload(
    monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient

    import api_server

    database, queue, runtime, base_wiki, sources, queued = _setup(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setenv("CLIMATE_PDF_INTAKE_QUEUE_DIR", str(queue))
    monkeypatch.setattr(api_server, "PDF_RUNTIME_WIKI_DIR", runtime)
    monkeypatch.setattr(api_server, "WIKI_DIR", base_wiki)
    monkeypatch.setattr(api_server, "SOURCE_DIR", sources)
    monkeypatch.setattr(api_server, "RELOAD_TOKEN", "reload-token")
    monkeypatch.setattr(api_server, "responder", AgenticWikiResponder(base_wiki, sources))
    monkeypatch.setattr(api_server._wiki_static_files, "all_directories", [str(base_wiki)])
    client = TestClient(api_server.app)

    def reload_chat(generation_id: str) -> None:
        response = client.post("/api/reload", params={"generation_id": generation_id}, headers={"x-reload-token": "reload-token"})
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
    projection, metadata = load_active_projection(runtime, queue / "active.json")
    assert sorted(path.name for path in projection.glob("*.md")) == [
        "article-core-climate-study.md"
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
        if item["path"] == "wiki/article-core-climate-study.md"
    )
    assert "IAA_CSC_Climate_Report_20260928.pdf" in citation["snippet"]
    assert "page 2" in citation["snippet"]

    base_answer = client.post(
        "/api/chat",
        json={"message": "What is in the updated weekly Registry projection?", "answerMode": "brief"},
    ).json()
    assert "Updated weekly Registry projection" in base_answer["text"]
    pdf_page = client.get("/wiki/article-core-climate-study.md")
    assert pdf_page.status_code == 200
    assert "Updated weekly Registry projection" in pdf_page.text
    assert "IAA_CSC_Climate_Report_20260928.pdf" in pdf_page.text
    assert "page 2" in pdf_page.text
    pdf_head = client.head("/wiki/article-core-climate-study.md")
    assert pdf_head.content == b""
    assert int(pdf_head.headers["content-length"]) == len(pdf_page.content)
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
        _ack_activation(queue),
        repository_root=tmp_path / "repository",
    ).process(later["batch_id"])
    assert ready["chat_ready"] is True
    projection, _ = load_active_projection(runtime, queue / "active.json")
    page = (projection / "article-core-climate-study.md").read_text(encoding="utf-8")
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
        _ack_activation(queue),
        repository_root=tmp_path / "repository",
    ).process(failed_batch["batch_id"])
    assert retried["chat_ready"] is True
    projection, _ = load_active_projection(runtime, queue / "active.json")
    page = (projection / "article-core-climate-study.md").read_text(encoding="utf-8")
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


@pytest.mark.skipif(os.name == "nt", reason="api_server management locking requires POSIX fcntl")
def test_invalid_active_projection_is_not_silently_ignored(monkeypatch, tmp_path):
    import api_server

    queue, runtime = tmp_path / "queue", tmp_path / "runtime"
    queue.mkdir()
    (runtime / "generations").mkdir(parents=True)
    (queue / "active.json").write_text(
        json.dumps(
            {
                "generation_id": "missing",
                "path": str(runtime / "generations" / "missing"),
                "registry_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLIMATE_PDF_INTAKE_QUEUE_DIR", str(queue))
    monkeypatch.setattr(api_server, "PDF_RUNTIME_WIKI_DIR", runtime)
    with pytest.raises(RuntimeError, match="active PDF Wiki projection is invalid"):
        api_server._selected_pdf_projection()


@pytest.mark.skipif(os.name == "nt", reason="api_server management locking requires POSIX fcntl")
def test_api_commits_after_load_and_response_loss_is_idempotent(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import api_server

    database, queue, runtime, base_wiki, sources, queued = _setup(tmp_path)
    monkeypatch.setenv("CLIMATE_PDF_INTAKE_QUEUE_DIR", str(queue))
    monkeypatch.setattr(api_server, "PDF_RUNTIME_WIKI_DIR", runtime)
    monkeypatch.setattr(api_server, "WIKI_DIR", base_wiki)
    monkeypatch.setattr(api_server, "SOURCE_DIR", sources)
    monkeypatch.setattr(api_server, "RELOAD_TOKEN", "reload-token")
    monkeypatch.setattr(api_server, "responder", AgenticWikiResponder(base_wiki, sources))
    monkeypatch.setattr(api_server._wiki_static_files, "all_directories", [str(base_wiki)])
    client = TestClient(api_server.app)

    def lost_response(generation_id: str) -> None:
        response = client.post(
            "/api/reload",
            params={"generation_id": generation_id},
            headers={"x-reload-token": "reload-token"},
        )
        assert response.status_code == 200
        raise TimeoutError("response was lost")

    ready = PdfIntakePipeline(
        queue,
        database,
        tmp_path / "backups",
        runtime,
        lost_response,
        repository_root=tmp_path / "repository",
    ).process(queued["batch_id"])
    projection, active = load_active_projection(runtime, queue / "active.json")
    assert ready["chat_ready"] is True
    assert active["generation_id"] == ready["generation_id"]
    assert api_server.responder.kb.wiki_overlay_dir == projection

    committed = runtime / "generations" / "committed"
    committed.mkdir()
    (committed / "committed.md").write_text("# Committed\n", encoding="utf-8")
    committed_metadata = dict(
        active, generation_id="committed", path=str(committed), activated_at="later"
    )
    (queue / "pending.json").write_text(json.dumps(committed_metadata), encoding="utf-8")
    real_atomic_write_json = api_server.atomic_write_json

    def replace_then_fail(path, value):
        real_atomic_write_json(path, value)
        raise RuntimeError("directory fsync failed after replace")

    monkeypatch.setattr(api_server, "atomic_write_json", replace_then_fail)
    failed_client = TestClient(api_server.app, raise_server_exceptions=False)
    response = failed_client.post(
        "/api/reload",
        params={"generation_id": "committed"},
        headers={"x-reload-token": "reload-token"},
    )
    assert response.status_code == 500
    projection, active = load_active_projection(runtime, queue / "active.json")
    assert active == committed_metadata
    assert api_server.responder.kb.wiki_overlay_dir == projection == committed
    monkeypatch.setattr(api_server, "atomic_write_json", real_atomic_write_json)

    previous_kb = api_server.responder.kb
    previous_directories = list(api_server._wiki_static_files.all_directories)
    pending = dict(active, generation_id="broken", path=str(runtime / "generations" / "broken"))
    broken = runtime / "generations" / "broken"
    broken.mkdir()
    (broken / "broken.md").write_text("# Broken\n", encoding="utf-8")
    (queue / "pending.json").write_text(json.dumps(pending), encoding="utf-8")
    monkeypatch.setattr(
        api_server,
        "WikiKnowledgeBase",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("load failed")),
    )
    response = failed_client.post(
        "/api/reload",
        params={"generation_id": "broken"},
        headers={"x-reload-token": "reload-token"},
    )
    assert response.status_code == 500
    assert load_active_projection(runtime, queue / "active.json")[1] == active
    assert api_server.responder.kb is previous_kb
    assert api_server._wiki_static_files.all_directories == previous_directories


@pytest.mark.skipif(os.name == "nt", reason="api_server management locking requires POSIX fcntl")
def test_reload_serializes_selection_build_and_swap(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import api_server

    queue, runtime, base_wiki, sources = (
        tmp_path / name for name in ("queue", "runtime", "wiki", "sources")
    )
    for directory in (queue, runtime / "generations", base_wiki, sources):
        directory.mkdir(parents=True)
    old = runtime / "generations" / "old"
    new = runtime / "generations" / "new"
    old.mkdir()
    new.mkdir()
    (old / "old.md").write_text("# Old\n", encoding="utf-8")
    (new / "new.md").write_text("# New\n", encoding="utf-8")
    common = {
        "batch_id": "b" * 64,
        "batch_created_at": "2026-10-01T00:00:00Z",
        "registry_sha256": "c" * 64,
        "document_sha256": "d" * 64,
        "filename": "report.pdf",
        "pages": [1],
        "activated_at": "2026-10-01T00:00:00Z",
    }
    old_metadata = dict(common, generation_id="old", path=str(old))
    new_metadata = dict(common, generation_id="new", path=str(new))
    (queue / "active.json").write_text(json.dumps(old_metadata), encoding="utf-8")
    monkeypatch.setenv("CLIMATE_PDF_INTAKE_QUEUE_DIR", str(queue))
    monkeypatch.setattr(api_server, "PDF_RUNTIME_WIKI_DIR", runtime)
    monkeypatch.setattr(api_server, "WIKI_DIR", base_wiki)
    monkeypatch.setattr(api_server, "SOURCE_DIR", sources)
    monkeypatch.setattr(api_server, "RELOAD_TOKEN", "reload-token")
    monkeypatch.setattr(api_server, "responder", AgenticWikiResponder(base_wiki, sources))
    monkeypatch.setattr(api_server._wiki_static_files, "all_directories", [str(base_wiki)])
    monkeypatch.setattr(api_server, "_public_config", lambda: {})

    old_started, release_old, newer_finished = threading.Event(), threading.Event(), threading.Event()

    def build(_wiki, _sources, projection):
        if projection == old:
            old_started.set()
            assert release_old.wait(5)
        return SimpleNamespace(overlay_wiki_dir=projection)

    monkeypatch.setattr(api_server, "WikiKnowledgeBase", build)
    client = TestClient(api_server.app)
    responses = []

    first = threading.Thread(
        target=lambda: responses.append(
            client.post("/api/reload", headers={"x-reload-token": "reload-token"})
        )
    )
    first.start()
    assert old_started.wait(5)
    (queue / "pending.json").write_text(json.dumps(new_metadata), encoding="utf-8")

    def activate_new():
        responses.append(
            client.post(
                "/api/reload",
                params={"generation_id": "new"},
                headers={"x-reload-token": "reload-token"},
            )
        )
        newer_finished.set()

    second = threading.Thread(target=activate_new)
    second.start()
    time.sleep(0.1)
    assert not newer_finished.is_set()
    release_old.set()
    first.join(5)
    second.join(5)
    assert [response.status_code for response in responses] == [200, 200]
    assert api_server.responder.kb.overlay_wiki_dir == new
    assert load_active_projection(runtime, queue / "active.json")[1]["generation_id"] == "new"


@pytest.mark.skipif(os.name == "nt", reason="api_server management locking requires POSIX fcntl")
def test_withdrawn_pending_activation_cannot_commit_after_slow_load(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import api_server

    queue, runtime, base_wiki, sources = (
        tmp_path / name for name in ("queue", "runtime", "wiki", "sources")
    )
    for directory in (queue, runtime / "generations", base_wiki, sources):
        directory.mkdir(parents=True)
    candidate = runtime / "generations" / "candidate"
    candidate.mkdir()
    (candidate / "candidate.md").write_text("# Candidate\n", encoding="utf-8")
    metadata = {
        "batch_id": "b" * 64,
        "batch_created_at": "2026-10-01T00:00:00Z",
        "generation_id": "candidate",
        "path": str(candidate),
        "registry_sha256": "c" * 64,
        "document_sha256": "d" * 64,
        "filename": "report.pdf",
        "pages": [1],
        "activated_at": "2026-10-01T00:00:00Z",
    }
    (queue / "pending.json").write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setenv("CLIMATE_PDF_INTAKE_QUEUE_DIR", str(queue))
    monkeypatch.setattr(api_server, "PDF_RUNTIME_WIKI_DIR", runtime)
    monkeypatch.setattr(api_server, "WIKI_DIR", base_wiki)
    monkeypatch.setattr(api_server, "SOURCE_DIR", sources)
    monkeypatch.setattr(api_server, "RELOAD_TOKEN", "reload-token")
    previous_kb = AgenticWikiResponder(base_wiki, sources).kb
    monkeypatch.setattr(api_server, "responder", SimpleNamespace(kb=previous_kb, config=lambda: {}))
    monkeypatch.setattr(api_server._wiki_static_files, "all_directories", [str(base_wiki)])
    started, release = threading.Event(), threading.Event()

    def slow_build(*_args):
        started.set()
        assert release.wait(5)
        return SimpleNamespace(overlay_wiki_dir=candidate)

    monkeypatch.setattr(api_server, "WikiKnowledgeBase", slow_build)
    client = TestClient(api_server.app, raise_server_exceptions=False)
    responses = []
    request_thread = threading.Thread(
        target=lambda: responses.append(
            client.post(
                "/api/reload",
                params={"generation_id": "candidate"},
                headers={"x-reload-token": "reload-token"},
            )
        )
    )
    request_thread.start()
    assert started.wait(5)
    (queue / "pending.json").unlink()
    release.set()
    request_thread.join(5)
    assert responses[0].status_code == 409
    assert not (queue / "active.json").exists()
    assert api_server.responder.kb is previous_kb
    assert api_server._wiki_static_files.all_directories == [str(base_wiki)]
