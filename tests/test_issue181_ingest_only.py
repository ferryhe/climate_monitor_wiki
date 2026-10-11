from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

from agentic_wiki import AgenticWikiResponder
from climate_delivery.errors import LockStateError
from climate_delivery.io import exclusive_lock
from climate_monitor.pdf_intake import import_pdf_reports
from climate_registry.pdf_intake import persist_pdf_intake
from climate_registry.pdf_pipeline import (
    PdfIntakePipeline,
    enqueue_pdf_batch,
    load_active_projection,
    load_projection_manifest,
    retry_pdf_batch,
)
from climate_registry.range_reports import (
    ensure_range_report_pdf,
    freeze_range_report,
    load_active_range_overlay,
    render_range_report_html,
)
from climate_registry.read_api import RegistryReader
from climate_registry.schema import apply_migrations as _apply_migrations
from climate_registry.web_ingest_pipeline import (
    _active_pdf_snapshot,
    enqueue_web_activation,
    read_web_activation_request,
    wait_web_activation,
)
from climate_registry.wiki import render_runtime_registry


# These regressions retain the supported pre-publication Runtime/Public manifest
# contract. The current single-Registry native review path has separate v21 tests.
def apply_migrations(connection, **kwargs):
    return _apply_migrations(connection, target_version=20, **kwargs)


@pytest.fixture(autouse=True)
def legacy_pdf_writer_contract(monkeypatch):
    import climate_registry.pdf_intake as storage
    import climate_registry.publication as publication
    from test_information_checks import legacy_schema_writer,legacy_pdf_binding
    import climate_registry.pdf_pipeline as pipeline
    monkeypatch.setattr(pipeline,"_validate_pdf_binding",legacy_pdf_binding)
    monkeypatch.setattr(publication,"require_publication_migration",legacy_schema_writer)
    monkeypatch.setattr(storage, "apply_migrations", apply_migrations)
    monkeypatch.setattr(storage, "LATEST_SCHEMA_VERSION", 20)


NOW = "2026-10-02T12:00:00Z"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _pdf(
    path,
    *,
    url: str = "https://pdf-only.example/study",
    summary: str = "PDF-only evidence requires transition scenario testing.",
    publication_date_text: str | None = None,
    extra_url: str | None = None,
) -> None:
    output = BytesIO()
    canvas = Canvas(output, pagesize=letter)
    canvas.drawString(50, 760, "Climate Risk Outlook")
    canvas.showPage()
    canvas.drawString(50, 760, "UPDATES")
    canvas.drawString(50, 740, "PDF-only transition source")
    canvas.drawString(50, 720, "REPORT COVERAGE")
    canvas.drawString(50, 700, summary)
    if publication_date_text:
        canvas.drawString(50, 680, f"IN WINDOW {publication_date_text} REPORT")
    canvas.linkURL(url, (48, 738, 320, 754), relative=0)
    if extra_url:
        canvas.drawString(50, 660, "Unapproved Web identity with approved PDF evidence")
        canvas.linkURL(extra_url, (48, 658, 360, 674), relative=0)
    canvas.showPage()
    canvas.save()
    path.write_bytes(output.getvalue())


def _seed_web(
    database,
    *,
    item_title: str = "Web transition evidence",
    publication_date: str = "2026-01-01",
    fetched_at: str = NOW,
    discovered_at: str = NOW,
) -> None:
    with sqlite3.connect(database) as connection:
        apply_migrations(connection)
        connection.execute("INSERT INTO sources VALUES ('web', 'web.example', 'Web Source', ?, ?)", (NOW, NOW))
        connection.execute(
            """INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen,
               document_kind, publication_eligible, display_policy)
               VALUES ('web-article', 'https://web.example/article', 'web', ?, ?,
               'article', 1, 'full_markdown')""",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO article_versions VALUES ('title-v1', 'web-article', 'Web transition evidence', "
            "'web transition evidence', 'Observed summary', ?, 'report-title-summary', ?, ?)",
            (_sha("title"), NOW, NOW),
        )
        for version, body in (
            ("content-pinned", "Pinned web evidence says coastal resilience funding is accelerating."),
            ("content-new", "A later unactivated body must not replace the pinned evidence."),
        ):
            digest = _sha(body)
            connection.execute(
                "INSERT INTO article_content_versions VALUES (?, 'web-article', ?, ?, ?, "
                "'text/markdown', ?, 'reader', 'reader-v1', ?)",
                (version, digest, body, digest, len(body.encode()), NOW),
            )
        connection.execute(
            "UPDATE articles SET current_version_id='title-v1', current_content_version_id='content-new' "
            "WHERE article_id='web-article'"
        )
        connection.execute(
            """INSERT INTO acquisition_batches VALUES
               ('web-batch', 'pre-report-acquisition-batch.v1', '2026-01-05', ?, ?,
                '{"mode":"unlimited"}', 'no_search', 'site-only', ?, ?)""",
            (NOW, NOW, "a" * 64, NOW),
        )
        connection.execute(
            """INSERT INTO article_fetches(fetch_id, article_id, requested_url, final_url,
               fetched_at, fetch_status, http_status, content_type, content_version_id)
               VALUES ('fetch-web', 'web-article', 'https://web.example/article',
               'https://web.example/article', ?, 'success', 200, 'text/markdown', 'content-pinned')""",
            (fetched_at,),
        )
        evidence = json.dumps({
            "kind": "publisher", "text": f"Published {publication_date}",
        })
        connection.execute(
            """INSERT INTO acquisition_items(acquisition_item_id, batch_id, ordinal, article_id,
               raw_url, source_name, title, summary, discovered_at, discovery_kind, discovery_ref,
               origins_json, publication_date, publication_date_evidence_json, date_status,
               selection_status, selection_reason, update_status, material_status, fetch_id,
               content_version_id, attempts_json, processing_status)
               VALUES ('web-item', 'web-batch', 1, 'web-article', 'https://web.example/article',
               'Web Source', ?, 'Web acquisition summary', ?, 'site',
               'https://web.example/article', '[]', ?, ?, 'eligible', 'selected',
               'relevant', 'baseline', 'full_content', 'fetch-web', 'content-pinned', '[]', 'complete')""",
            (item_title, discovered_at, publication_date, evidence),
        )


def _add_web_observation(
    database,
    *,
    batch_id: str,
    item_id: str,
    article_id: str,
    content_version_id: str,
    body: str,
    discovered_at: str,
    publication_date: str,
    title: str,
) -> None:
    title_version_id = f"title-{item_id}"
    fetch_id = f"fetch-{item_id}"
    canonical_url = f"https://web.example/{article_id}"
    digest = _sha(body)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT OR IGNORE INTO articles(article_id, canonical_url, source_id, first_seen,
               last_seen, document_kind, publication_eligible, display_policy)
               VALUES (?, ?, 'web', ?, ?, 'article', 1, 'full_markdown')""",
            (article_id, canonical_url, discovered_at, discovered_at),
        )
        connection.execute(
            "INSERT INTO article_versions VALUES (?, ?, ?, ?, ?, ?, 'report-title-summary', ?, ?)",
            (title_version_id, article_id, title, title.casefold(), f"Summary for {title}",
             _sha(title), discovered_at, discovered_at),
        )
        connection.execute(
            "INSERT INTO article_content_versions VALUES (?, ?, ?, ?, ?, 'text/markdown', ?, "
            "'reader', 'reader-v1', ?)",
            (content_version_id, article_id, digest, body, digest, len(body.encode()), discovered_at),
        )
        connection.execute(
            "UPDATE articles SET current_version_id=?, current_content_version_id=?, last_seen=? "
            "WHERE article_id=?",
            (title_version_id, content_version_id, discovered_at, article_id),
        )
        connection.execute(
            """INSERT INTO acquisition_batches VALUES
               (?, 'pre-report-acquisition-batch.v1', ?, ?, ?, '{"mode":"unlimited"}',
                'no_search', 'site-only', ?, ?)""",
            (batch_id, publication_date, discovered_at, discovered_at, _sha(batch_id), discovered_at),
        )
        connection.execute(
            """INSERT INTO article_fetches(fetch_id, article_id, requested_url, final_url,
               fetched_at, fetch_status, http_status, content_type, content_version_id)
               VALUES (?, ?, ?, ?, ?, 'success', 200, 'text/markdown', ?)""",
            (fetch_id, article_id, canonical_url, canonical_url, discovered_at, content_version_id),
        )
        evidence = json.dumps({"kind": "publisher", "text": f"Published {publication_date}"})
        connection.execute(
            """INSERT INTO acquisition_items(acquisition_item_id, batch_id, ordinal, article_id,
               raw_url, source_name, title, summary, discovered_at, discovery_kind, discovery_ref,
               origins_json, publication_date, publication_date_evidence_json, date_status,
               selection_status, selection_reason, update_status, material_status, fetch_id,
               content_version_id, attempts_json, processing_status)
               VALUES (?, ?, 1, ?, ?, 'Web Source', ?, ?, ?, 'site', ?, '[]', ?, ?,
               'eligible', 'selected', 'relevant', 'content_changed', 'full_content', ?, ?, '[]', 'complete')""",
            (item_id, batch_id, article_id, canonical_url, title, f"Summary for {title}",
             discovered_at, canonical_url, publication_date, evidence, fetch_id, content_version_id),
        )


def _ack(queue, *, raise_after_commit=False):
    def reload_chat(generation_id: str) -> None:
        pending = json.loads((queue / "pending.json").read_text(encoding="utf-8"))
        assert pending["generation_id"] == generation_id
        os.replace(queue / "pending.json", queue / "active.json")
        if raise_after_commit:
            raise RuntimeError("connection closed after committed reload")
    return reload_chat


def test_pdf_and_web_activation_share_one_pinned_read_only_snapshot(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    sentinel = repository / "sentinel.txt"
    sentinel.write_text("clean", encoding="utf-8")
    runtime_db = tmp_path / "runtime" / "registry.sqlite3"
    runtime_db.parent.mkdir()
    _seed_web(runtime_db)
    public_db = tmp_path / "public" / "registry.sqlite3"
    public_db.parent.mkdir()
    connection = sqlite3.connect(public_db)
    try:
        apply_migrations(connection)
    finally:
        connection.close()

    queue, runtime, wiki, sources = (tmp_path / name for name in ("queue", "runtime-wiki", "wiki", "sources"))
    for path in (queue, runtime, wiki, sources):
        path.mkdir()
    pdf_path = tmp_path / "outlook.pdf"
    _pdf(pdf_path)
    bundle = import_pdf_reports([pdf_path])
    bundle["documents"][0].update(period_start="2025-12-31", period_end="2025-12-31")
    bundle["calendar_items"].append({
        "event_id": "future-key-date", "occurrence_id": "future-key-date-occurrence",
        "source_document_sha256": bundle["documents"][0]["source"]["sha256"],
        "page": 2, "name": "Future climate filing", "kind": "deadline",
        "raw_date": "2027-03-01", "date_precision": "day", "start_date": "2027-03-01",
        "end_date": "2027-03-01", "summary": "Future Key Date filing deadline",
        "content_sha256": _sha("future filing"), "source_urls": [],
    })
    queued = enqueue_pdf_batch(queue, bundle, repository_root=repository)
    pdf_ready = PdfIntakePipeline(
        queue, runtime_db, tmp_path / "backups", runtime, _ack(queue), repository_root=repository,
    ).process(queued["batch_id"])
    assert pdf_ready["chat_ready"] is True, pdf_ready["error"]
    legacy_generation, legacy_active = load_active_projection(runtime, queue / "active.json")
    (legacy_generation / "intake-manifest.json").unlink()
    for key in (
        "manifest_sha256", "projection_kind", "registry_snapshot", "pdf_registry_snapshot",
        "pdf_registry_sha256", "web_registry_snapshot", "web_registry_sha256",
    ):
        legacy_active.pop(key, None)
    (queue / "active.json").write_text(json.dumps(legacy_active), encoding="utf-8")

    writer = PdfIntakePipeline(
        queue, runtime_db, tmp_path / "backups", runtime,
        _ack(queue, raise_after_commit=True), repository_root=repository,
    )
    queued_web = enqueue_web_activation(
        queue, runtime_db, "web-batch",
        frozen_payload_sha256="b" * 64, repository_root=repository,
    )
    assert queued_web["stage"] == "queued"
    request = read_web_activation_request(queue, "web-batch")
    assert request["registry_snapshot_path"].startswith(str(queue.resolve()))
    request_bytes = (queue / "web" / _sha("web-batch") / "request.json").read_bytes()
    snapshot_bytes = (queue / "web" / _sha("web-batch") / "registry.sqlite3").read_bytes()
    assert enqueue_web_activation(
        queue, runtime_db, "web-batch",
        frozen_payload_sha256="b" * 64, repository_root=repository,
    )["stage"] == "queued"
    assert (queue / "web" / _sha("web-batch") / "request.json").read_bytes() == request_bytes
    assert (queue / "web" / _sha("web-batch") / "registry.sqlite3").read_bytes() == snapshot_bytes
    with exclusive_lock(queue, "intake-writer"):
        with pytest.raises(LockStateError):
            writer.process_next()

    def fail_reload(_generation_id):
        raise RuntimeError("reload unavailable")

    failed_writer = PdfIntakePipeline(
        queue, runtime_db, tmp_path / "backups", runtime,
        fail_reload, repository_root=repository,
    )
    assert failed_writer.process_next()["stage"] == "failed"
    assert enqueue_web_activation(
        queue, runtime_db, "web-batch",
        frozen_payload_sha256="b" * 64, repository_root=repository,
    )["stage"] == "failed"
    web_ready = writer.process_next()
    assert wait_web_activation(queue, "web-batch", timeout_seconds=0) == web_ready
    assert web_ready["stage"] == "chat_ready"
    assert web_ready["acquisition_complete"] is True
    assert web_ready["indexed"] is True
    assert web_ready["chat_ready"] is True
    generation, metadata = load_active_projection(runtime, queue / "active.json")
    manifest = load_projection_manifest(generation, metadata)
    assert manifest["web_items"] == [{
        "acquisition_item_id": "web-item", "batch_id": "web-batch",
        "article_id": "web-article", "content_version_id": "content-pinned",
        "collected_at": NOW,
        "publication_date": "2026-01-01",
        "publication_date_evidence": {"kind": "publisher", "text": "Published 2026-01-01"},
    }]
    assert manifest["pdf_occurrence_ids"]
    assert metadata["pdf_registry_snapshot"].endswith(
        f"{legacy_active['generation_id']}.sqlite3"
    )

    responder = AgenticWikiResponder(wiki, sources, generation)
    corpus = "\n".join(document.markdown for document in responder.kb.documents)
    assert "Pinned web evidence says coastal resilience funding" in corpus
    assert "later unactivated body" not in corpus
    assert "PDF-only evidence requires transition scenario testing" in corpus
    assert "Publication date: 2026-01-01" in corpus
    assert f"Collected at: {NOW}" in corpus
    assert 'Publication-date evidence: {"kind": "publisher", "text": "Published 2026-01-01"}' in corpus
    assert f"discovered: {NOW}" in corpus
    responder.client = None
    web_answer = responder.answer("coastal resilience funding", answer_mode="brief")
    pdf_answer = responder.answer("transition scenario testing", answer_mode="brief")
    assert any(source["path"].endswith("article-web-article.md") for source in web_answer["sources"])
    assert any(
        source["path"].startswith("wiki/registry-source-observation-")
        for source in pdf_answer["sources"]
    )
    legacy_overlay_report = freeze_range_report(
        RegistryReader(public_db, repository_root=repository),
        tmp_path / "legacy-reports",
        start_date="2025-12-31", end_date="2026-12-30",
        generated_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        overlay_reader=RegistryReader(metadata["web_registry_snapshot"], repository_root=repository),
        pdf_overlay_reader=RegistryReader(metadata["pdf_registry_snapshot"], repository_root=repository),
        overlay_manifest=manifest,
    )
    assert legacy_overlay_report["pdf_source_updates"]
    assert legacy_overlay_report["articles"][0]["content_version_id"] == "content-pinned"

    later_bundle = deepcopy(bundle)
    later_bundle["articles"][0]["occurrences"][0]["summary"] += " Retained after web activation."
    later = enqueue_pdf_batch(queue, later_bundle, repository_root=repository)
    assert later["batch_id"] != queued["batch_id"]
    assert PdfIntakePipeline(
        queue, runtime_db, tmp_path / "backups", runtime, _ack(queue), repository_root=repository,
    ).process(later["batch_id"])["chat_ready"] is True
    generation, metadata = load_active_projection(runtime, queue / "active.json")
    manifest = load_projection_manifest(generation, metadata)
    assert manifest["web_items"][0]["acquisition_item_id"] == "web-item"
    assert (generation / "article-web-article.md").is_file()

    snapshot = freeze_range_report(
        RegistryReader(public_db, repository_root=repository),
        tmp_path / "reports",
        start_date="2025-12-31", end_date="2026-12-30",
        generated_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        overlay_reader=RegistryReader(metadata["web_registry_snapshot"], repository_root=repository),
        pdf_overlay_reader=RegistryReader(metadata["pdf_registry_snapshot"], repository_root=repository),
        overlay_manifest=manifest,
    )
    assert snapshot["articles"][0]["content_version_id"] == "content-pinned"
    assert snapshot["articles"][0]["publication_date"] == "2026-01-01"
    assert snapshot["articles"][0]["provenance"]["publication_date"]["selected"]["evidence"]
    assert any("PDF-only transition source" in item["title"] for item in snapshot["pdf_source_updates"])
    assert any(item["event_id"] == "future-key-date" for item in snapshot["pdf_calendar"]["records"])
    assert snapshot["executive_summary"]
    assert all(point["citations"] for point in snapshot["executive_summary"])
    html = render_range_report_html(snapshot)
    pdf = ensure_range_report_pdf(snapshot, tmp_path / "reports")
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf).pages)
    for text in ("Web transition evidence", "PDF-only transition source", "Future climate filing"):
        assert text in html
        assert text in pdf_text
    assert sentinel.read_text(encoding="utf-8") == "clean"


def test_web_activation_reads_legacy_six_field_request_and_projection(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    queue, runtime = tmp_path / "queue", tmp_path / "runtime-wiki"
    queue.mkdir()
    runtime.mkdir()
    runtime_db = tmp_path / "runtime" / "registry.sqlite3"
    runtime_db.parent.mkdir()
    _seed_web(runtime_db)
    public_db = tmp_path / "public" / "registry.sqlite3"
    public_db.parent.mkdir()
    with sqlite3.connect(public_db) as connection:
        apply_migrations(connection)

    enqueue_web_activation(
        queue, runtime_db, "web-batch",
        frozen_payload_sha256="c" * 64, repository_root=repository,
    )
    request_path = queue / "web" / _sha("web-batch") / "request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    for item in request["web_items"]:
        item.pop("collected_at")
    request_path.write_text(json.dumps(request), encoding="utf-8")
    legacy_request_bytes = request_path.read_bytes()

    assert read_web_activation_request(queue, "web-batch")["web_items"] == request["web_items"]
    assert enqueue_web_activation(
        queue, runtime_db, "web-batch",
        frozen_payload_sha256="c" * 64, repository_root=repository,
    )["stage"] == "queued"
    assert request_path.read_bytes() == legacy_request_bytes

    writer = PdfIntakePipeline(
        queue, runtime_db, tmp_path / "backups", runtime, _ack(queue),
        repository_root=repository,
    )
    assert writer.process_next()["chat_ready"] is True
    generation, active = load_active_projection(runtime, queue / "active.json")
    current_manifest = load_projection_manifest(generation, active)
    assert current_manifest["web_items"][0]["collected_at"] == NOW

    legacy_manifest = deepcopy(current_manifest)
    for item in legacy_manifest["web_items"]:
        item.pop("collected_at")
    payload = (json.dumps(legacy_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    (generation / "intake-manifest.json").write_bytes(payload)
    active_path = queue / "active.json"
    active["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    active_path.write_text(json.dumps(active), encoding="utf-8")

    legacy_generation, legacy_active = load_active_projection(runtime, active_path)
    loaded_legacy = load_projection_manifest(legacy_generation, legacy_active)
    assert "collected_at" not in loaded_legacy["web_items"][0]
    legacy_wiki = tmp_path / "legacy-wiki"
    render_runtime_registry(
        legacy_wiki,
        web_database=Path(legacy_active["web_registry_snapshot"]),
        pdf_database=None,
        manifest=loaded_legacy,
    )
    assert f"Collected at: {NOW}" in (
        legacy_wiki / "article-web-article.md"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("date_source", ["runtime_pdf", "public_pdf", "public_web"])
def test_authorized_date_selection_retains_activated_web_main_facts(tmp_path, date_source):
    repository = tmp_path / "repository"
    repository.mkdir()
    writer_db = tmp_path / "writer" / "climate_registry.sqlite3"
    writer_db.parent.mkdir()
    _seed_web(
        writer_db,
        publication_date="2026-09-20",
        fetched_at="2026-09-25T12:00:00Z",
    )
    _add_web_observation(
        writer_db,
        batch_id="unapproved-web-batch",
        item_id="unapproved-web-item",
        article_id="unapproved-article",
        content_version_id="unapproved-content",
        body="UNAPPROVED WRITER BODY",
        discovered_at="2026-09-25T13:00:00Z",
        publication_date="2026-09-25",
        title="Unapproved writer title",
    )
    with sqlite3.connect(writer_db) as connection:
        connection.execute(
            """INSERT INTO article_enrichments(
                   enrichment_id, content_version_id, status, summary, categories_json,
                   keywords_json, language, generator_kind, generator_name,
                   generator_version, generated_at, error_code, error_message
               ) VALUES
               ('approved-web-enrichment', 'content-pinned', 'complete', ?, ?, ?,
                'en', 'deterministic', 'fixture', 'semantic-v3', ?, NULL, NULL)""",
            (
                "Approved Web enrichment summary",
                json.dumps(["Physical risk"]),
                json.dumps(["resilience"]),
                NOW,
            ),
        )
    public_db = tmp_path / "public.sqlite3"
    with sqlite3.connect(public_db) as connection:
        apply_migrations(connection)
        connection.execute(
            "INSERT INTO sources VALUES ('public', 'public.example', 'Public', ?, ?)",
            (NOW, NOW),
        )
        connection.execute(
            """INSERT INTO articles(article_id, canonical_url, source_id, first_seen,
               last_seen, document_kind, publication_eligible, display_policy)
               VALUES ('web-article', 'https://web.example/article', 'public', ?, ?,
               'article', 1, 'full_markdown')""",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO article_versions VALUES "
            "('public-title', 'web-article', 'Public title', 'public title', "
            "'Public summary', ?, 'report-title-summary', ?, ?)",
            (_sha("public title"), NOW, NOW),
        )
        public_body = "Authorized Public body without a local date observation."
        connection.execute(
            "INSERT INTO article_content_versions VALUES "
            "('public-content', 'web-article', ?, ?, ?, 'text/markdown', ?, "
            "'reader', 'reader-v1', ?)",
            (
                _sha(public_body), public_body, _sha(public_body),
                len(public_body), NOW,
            ),
        )
        connection.execute(
            "UPDATE articles SET current_version_id='public-title', "
            "current_content_version_id='public-content' WHERE article_id='web-article'"
        )
    connection.close()
    if date_source == "public_web":
        _add_web_observation(
            public_db,
            batch_id="public-date-batch",
            item_id="public-date-item",
            article_id="web-article",
            content_version_id="public-date-content",
            body="Authorized Public Web date source body.",
            discovered_at="2026-09-25T11:00:00Z",
            publication_date="2026-09-25",
            title="Public date observation title",
        )
    queue, runtime = tmp_path / "queue", tmp_path / "runtime"
    queue.mkdir()
    runtime.mkdir()
    enqueue_web_activation(
        queue, writer_db, "web-batch",
        frozen_payload_sha256="9" * 64, repository_root=repository,
    )
    writer = PdfIntakePipeline(
        queue, writer_db, tmp_path / "backups", runtime, _ack(queue),
        repository_root=repository,
    )
    assert writer.process_next()["chat_ready"] is True

    occurrence = None
    if date_source.endswith("_pdf"):
        pdf_path = tmp_path / "approved-date-update.pdf"
        _pdf(
            pdf_path,
            url="https://web.example/article",
            summary="Approved later PDF observation for the same article.",
            publication_date_text="25 SEP 2026",
            extra_url="https://web.example/unapproved-article",
        )
        bundle = import_pdf_reports([pdf_path])
        bundle["documents"][0].update(
            period_start="2026-09-25", period_end="2026-09-25"
        )
        pdf_article = next(
            item for item in bundle["articles"]
            if item["canonical_url"] == "https://web.example/article"
        )
        for item in bundle["articles"]:
            item["type_safe_classification"] = {
                "provider": "typesafe", "label": "article",
            }
        occurrence = pdf_article["occurrences"][0]
        if date_source == "runtime_pdf":
            queued = enqueue_pdf_batch(queue, bundle, repository_root=repository)
            assert writer.process(queued["batch_id"])["chat_ready"] is True
        else:
            persist_pdf_intake(public_db, tmp_path / "public-backups", bundle)

    generation, _ = load_active_projection(runtime, queue / "active.json")
    assert "Pinned web evidence says coastal resilience funding" in (
        generation / "article-web-article.md"
    ).read_text(encoding="utf-8")
    web_reader, pdf_reader, manifest = load_active_range_overlay(
        runtime, queue, repository_root=repository
    )
    snapshot = freeze_range_report(
        RegistryReader(public_db, repository_root=repository),
        tmp_path / "range-reports",
        start_date="2026-09-25", end_date="2026-09-25",
        generated_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        overlay_reader=web_reader,
        pdf_overlay_reader=pdf_reader,
        overlay_manifest=manifest,
    )
    expected_ids = {"web-article"}
    assert {item["article_id"] for item in snapshot["articles"]} == expected_ids
    article = next(
        item for item in snapshot["articles"] if item["article_id"] == "web-article"
    )
    assert article["date_basis"] == "collection_time"
    assert article["collected_at"] == "2026-09-25T12:00:00Z"
    assert article["range_date"] == "2026-09-25"
    assert article["publication_date"] == "2026-09-20"
    assert article["content_version_id"] == "content-pinned"
    assert "Pinned web evidence says coastal resilience funding" in article["content"]
    assert "later unactivated body" not in article["content"]
    assert article["summary"] == "Approved Web enrichment summary"
    assert article["categories"] == ["Physical risk"]
    assert article["keywords"] == ["resilience"]
    citation_kinds = {item["kind"] for item in article["citations"]}
    assert citation_kinds == (
        {"url", "pdf_page"} if date_source.endswith("_pdf") else {"url"}
    )
    selected = article["provenance"]["publication_date"]["selected"]
    assert selected == {
        "date": "2026-09-20",
        "observation_id": "web-item",
        "evidence": {"kind": "publisher", "text": "Published 2026-09-20"},
    }
    assert "UNAPPROVED WRITER BODY" not in json.dumps(snapshot)


def test_invalid_active_manifest_is_a_controlled_handoff_error(tmp_path):
    generation = tmp_path / "runtime" / "generations" / "gen"
    generation.mkdir(parents=True)
    payload = b'{"schema_version":"climate-intake-projection.v1","generation_id":"gen","web_items":[{}],"pdf_occurrence_ids":[]}\n'
    (generation / "intake-manifest.json").write_bytes(payload)
    with pytest.raises(RuntimeError, match="manifest is invalid"):
        load_projection_manifest(generation, {
            "generation_id": "gen", "manifest_sha256": hashlib.sha256(payload).hexdigest(),
        })


def test_failed_pdf_then_web_activation_then_pdf_retry_merges_both(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    queue, runtime = tmp_path / "queue", tmp_path / "runtime-wiki"
    queue.mkdir()
    runtime.mkdir()
    runtime_db = tmp_path / "runtime" / "registry.sqlite3"
    runtime_db.parent.mkdir()
    _seed_web(runtime_db)
    writer_db = runtime_db  # Both continuations use the same bound business Registry.

    pdf_path = tmp_path / "retry.pdf"
    _pdf(pdf_path)
    bundle = import_pdf_reports([pdf_path])
    bundle["documents"][0].update(period_start="2025-12-31", period_end="2025-12-31")
    bundle["calendar_items"].append({
        "event_id": "failed-pdf-date", "occurrence_id": "failed-pdf-date-occurrence",
        "source_document_sha256": bundle["documents"][0]["source"]["sha256"],
        "page": 2, "name": "Unactivated PDF filing", "kind": "deadline",
        "raw_date": "2027-03-01", "date_precision": "day",
        "start_date": "2027-03-01", "end_date": "2027-03-01",
        "summary": "Must remain hidden until PDF activation",
        "content_sha256": _sha("unactivated filing"), "source_urls": [],
    })
    pdf = enqueue_pdf_batch(queue, bundle, repository_root=repository)

    def fail_reload(_generation_id):
        raise RuntimeError("reload unavailable")

    failed = PdfIntakePipeline(
        queue, writer_db, tmp_path / "backups", runtime, fail_reload,
        repository_root=repository,
    ).process(pdf["batch_id"])
    assert failed["stage"] == "failed", failed
    assert failed["indexed"] is True, failed["error"]

    writer = PdfIntakePipeline(
        queue, writer_db, tmp_path / "backups", runtime, _ack(queue),
        repository_root=repository,
    )
    enqueue_web_activation(
        queue, runtime_db, "web-batch",
        frozen_payload_sha256="c" * 64, repository_root=repository,
    )
    assert writer.process_next()["chat_ready"] is True
    web_generation, web_active = load_active_projection(runtime, queue / "active.json")
    assert web_active["projection_kind"] == "web"
    web_manifest = load_projection_manifest(web_generation, web_active)
    assert web_manifest["pdf_occurrence_ids"] == []
    public_db = tmp_path / "public" / "registry.sqlite3"
    public_db.parent.mkdir()
    with sqlite3.connect(public_db) as connection:
        apply_migrations(connection)
    web_only_report = freeze_range_report(
        RegistryReader(public_db, repository_root=repository),
        tmp_path / "web-only-reports",
        start_date="2025-12-31", end_date="2026-12-30",
        generated_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        overlay_reader=RegistryReader(
            web_active["web_registry_snapshot"], repository_root=repository,
        ),
        overlay_manifest=web_manifest,
    )
    assert web_only_report["pdf_source_updates"] == []
    assert web_only_report["pdf_calendar"]["records"] == []

    retry_pdf_batch(queue, pdf["batch_id"])
    retried = writer.process_next()
    assert retried["stage"] == "chat_ready"
    generation, active = load_active_projection(runtime, queue / "active.json")
    manifest = load_projection_manifest(generation, active)
    assert active["projection_kind"] == "pdf"
    assert manifest["web_items"][0]["acquisition_item_id"] == "web-item"
    assert manifest["pdf_occurrence_ids"]
    assert (generation / "article-web-article.md").is_file()


def test_invalid_web_request_does_not_starve_later_writer_jobs(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    queue, runtime = tmp_path / "queue", tmp_path / "runtime-wiki"
    queue.mkdir()
    runtime.mkdir()
    runtime_db = tmp_path / "runtime.sqlite3"
    _seed_web(runtime_db)
    enqueue_web_activation(
        queue, runtime_db, "web-batch",
        frozen_payload_sha256="3" * 64, repository_root=repository,
    )
    request_path = queue / "web" / _sha("web-batch") / "request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["registry_sha256"] = "0" * 64
    request_path.write_text(json.dumps(request), encoding="utf-8")

    _add_web_observation(
        runtime_db, batch_id="valid-later", item_id="valid-later-item",
        article_id="valid-later-article", content_version_id="valid-later-content",
        body="Later valid writer evidence.", discovered_at="2026-10-03T12:00:00Z",
        publication_date="2026-02-01", title="Later valid evidence",
    )
    enqueue_web_activation(
        queue, runtime_db, "valid-later",
        frozen_payload_sha256="4" * 64, repository_root=repository,
    )
    public_db = tmp_path / "public.sqlite3"
    with sqlite3.connect(public_db) as connection:
        apply_migrations(connection)
    writer = PdfIntakePipeline(
        queue, runtime_db, tmp_path / "backups", runtime, _ack(queue),
        repository_root=repository,
    )

    invalid = writer.process_next()
    assert invalid["batch_id"] == "web-batch"
    assert invalid["stage"] == "failed" and invalid["chat_ready"] is False
    assert "web activation request is invalid" in invalid["error"]
    ready = writer.process_next()
    assert ready["batch_id"] == "valid-later" and ready["chat_ready"] is True
    assert writer.process_next() is None


def test_delayed_web_retry_uses_one_snapshot_for_active_union(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    queue, runtime = tmp_path / "queue", tmp_path / "runtime-wiki"
    queue.mkdir()
    runtime.mkdir()
    runtime_db = tmp_path / "runtime" / "registry.sqlite3"
    runtime_db.parent.mkdir()
    _seed_web(
        runtime_db,
        fetched_at="2026-01-31T10:00:00Z",
        discovered_at="2026-01-31T10:00:00Z",
    )
    public_db = tmp_path / "public" / "registry.sqlite3"
    public_db.parent.mkdir()
    connection = sqlite3.connect(public_db)
    try:
        apply_migrations(connection)
    finally:
        connection.close()

    enqueue_web_activation(
        queue, runtime_db, "web-batch",
        frozen_payload_sha256="d" * 64, repository_root=repository,
    )

    def fail_reload(_generation_id):
        raise RuntimeError("reload unavailable")

    failed_writer = PdfIntakePipeline(
        queue, runtime_db, tmp_path / "backups", runtime, fail_reload,
        repository_root=repository,
    )
    assert failed_writer.process_next()["stage"] == "failed"

    _add_web_observation(
        runtime_db, batch_id="batch-b", item_id="item-b", article_id="article-b",
        content_version_id="content-b", body="Batch B immutable evidence.",
        discovered_at="2026-02-01T12:00:00Z", publication_date="2026-02-01",
        title="Batch B evidence",
    )
    enqueue_web_activation(
        queue, runtime_db, "batch-b",
        frozen_payload_sha256="e" * 64, repository_root=repository,
    )
    writer = PdfIntakePipeline(
        queue, runtime_db, tmp_path / "backups", runtime, _ack(queue),
        repository_root=repository,
    )
    assert writer.process_next()["chat_ready"] is True

    enqueue_web_activation(
        queue, runtime_db, "web-batch",
        frozen_payload_sha256="d" * 64, repository_root=repository,
    )
    retried = writer.process_next()
    assert retried["chat_ready"] is True
    generation, active = load_active_projection(runtime, queue / "active.json")
    manifest = load_projection_manifest(generation, active)
    assert {item["acquisition_item_id"] for item in manifest["web_items"]} == {
        "web-item", "item-b",
    }
    assert sorted(path.name for path in generation.glob("article-*.md")) == [
        "article-article-b.md", "article-web-article.md",
    ]
    snapshot = freeze_range_report(
        RegistryReader(public_db, repository_root=repository),
        tmp_path / "union-reports", start_date="2026-01-01", end_date="2026-03-01",
        overlay_reader=RegistryReader(active["web_registry_snapshot"], repository_root=repository),
        overlay_manifest=manifest,
    )
    assert {
        (article["article_id"], article["content_version_id"])
        for article in snapshot["articles"]
    } == {("web-article", "content-pinned"), ("article-b", "content-b")}


def test_web_page_keeps_latest_observation_across_unrelated_activation(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    queue, runtime = tmp_path / "queue", tmp_path / "runtime-wiki"
    queue.mkdir()
    runtime.mkdir()
    runtime_db = tmp_path / "runtime" / "registry.sqlite3"
    runtime_db.parent.mkdir()
    _seed_web(
        runtime_db,
        fetched_at="2026-01-31T10:00:00Z",
        discovered_at="2026-01-31T10:00:00Z",
    )
    public_db = tmp_path / "public" / "registry.sqlite3"
    public_db.parent.mkdir()
    connection = sqlite3.connect(public_db)
    try:
        apply_migrations(connection)
    finally:
        connection.close()
    writer = PdfIntakePipeline(
        queue, runtime_db, tmp_path / "backups", runtime, _ack(queue),
        repository_root=repository,
    )

    enqueue_web_activation(
        queue, runtime_db, "web-batch",
        frozen_payload_sha256="f" * 64, repository_root=repository,
    )
    assert writer.process_next()["chat_ready"] is True
    _add_web_observation(
        runtime_db, batch_id="newer-batch", item_id="a-new", article_id="web-article",
        content_version_id="content-latest", body="Latest validated transition body.",
        discovered_at="2026-02-01T12:00:00Z", publication_date="2026-02-01",
        title="Latest transition evidence",
    )
    enqueue_web_activation(
        queue, runtime_db, "newer-batch",
        frozen_payload_sha256="1" * 64, repository_root=repository,
    )
    assert writer.process_next()["chat_ready"] is True
    _add_web_observation(
        runtime_db, batch_id="unrelated-batch", item_id="m-unrelated",
        article_id="unrelated-article", content_version_id="content-unrelated",
        body="Unrelated validated evidence.", discovered_at="2026-02-02T12:00:00Z",
        publication_date="2026-03-01", title="Unrelated evidence",
    )
    enqueue_web_activation(
        queue, runtime_db, "unrelated-batch",
        frozen_payload_sha256="2" * 64, repository_root=repository,
    )
    assert writer.process_next()["chat_ready"] is True

    generation, active = load_active_projection(runtime, queue / "active.json")
    manifest = load_projection_manifest(generation, active)
    assert {item["acquisition_item_id"] for item in manifest["web_items"]} == {
        "web-item", "a-new", "m-unrelated",
    }
    page = (generation / "article-web-article.md").read_text(encoding="utf-8")
    assert "Latest validated transition body" in page
    assert "Pinned web evidence says coastal resilience" not in page
    wiki, sources = tmp_path / "wiki", tmp_path / "sources"
    wiki.mkdir()
    sources.mkdir()
    responder = AgenticWikiResponder(wiki, sources, generation)
    responder.client = None
    answer = responder.answer("latest validated transition body", answer_mode="brief")
    assert any(source["path"].endswith("article-web-article.md") for source in answer["sources"])
    snapshot = freeze_range_report(
        RegistryReader(public_db, repository_root=repository),
        tmp_path / "latest-reports", start_date="2026-01-01", end_date="2026-03-01",
        overlay_reader=RegistryReader(active["web_registry_snapshot"], repository_root=repository),
        overlay_manifest=manifest,
    )
    current = next(article for article in snapshot["articles"] if article["article_id"] == "web-article")
    assert current["content_version_id"] == "content-latest"
    assert "Latest validated transition body" in current["content"]


def test_empty_activated_title_falls_back_to_canonical_url_not_unactivated_title(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    queue, runtime = tmp_path / "queue", tmp_path / "runtime-wiki"
    queue.mkdir()
    runtime.mkdir()
    runtime_db = tmp_path / "runtime.sqlite3"
    _seed_web(runtime_db, item_title="", fetched_at="2026-01-05T12:00:00Z")
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "INSERT INTO article_versions VALUES "
            "('title-unactivated', 'web-article', 'UNACTIVATED TITLE', "
            "'unactivated title', 'Unactivated summary', ?, "
            "'report-title-summary', ?, ?)",
            (_sha("unactivated title"), NOW, NOW),
        )
        connection.execute(
            "UPDATE articles SET current_version_id='title-unactivated' "
            "WHERE article_id='web-article'"
        )
    public_db = tmp_path / "public.sqlite3"
    with sqlite3.connect(public_db) as connection:
        apply_migrations(connection)
    enqueue_web_activation(
        queue, runtime_db, "web-batch",
        frozen_payload_sha256="e" * 64, repository_root=repository,
    )
    ready = PdfIntakePipeline(
        queue, runtime_db, tmp_path / "backups", runtime, _ack(queue),
        repository_root=repository,
    ).process_next()
    assert ready["chat_ready"] is True

    generation, active = load_active_projection(runtime, queue / "active.json")
    page = (generation / "article-web-article.md").read_text(encoding="utf-8")
    assert page.startswith("# https://web.example/article\n")
    assert "UNACTIVATED TITLE" not in page
    assert "later unactivated body" not in page
    assert "Pinned web evidence says coastal resilience funding" in page
    assert "Web acquisition summary" in page
    assert "Publication date: 2026-01-01" in page
    assert "source: Web Source" in page

    wiki, sources = tmp_path / "wiki", tmp_path / "sources"
    wiki.mkdir()
    sources.mkdir()
    kb = AgenticWikiResponder(wiki, sources, generation).kb
    document = next(doc for doc in kb.documents if doc.file == "article-web-article.md")
    assert document.markdown.startswith("# https://web.example/article\n")
    assert "UNACTIVATED TITLE" not in document.markdown

    manifest = load_projection_manifest(generation, active)
    snapshot = freeze_range_report(
        RegistryReader(public_db, repository_root=repository),
        tmp_path / "range-reports",
        start_date="2026-01-01", end_date="2026-01-31",
        overlay_reader=RegistryReader(
            active["web_registry_snapshot"], repository_root=repository
        ),
        overlay_manifest=manifest,
    )
    article = snapshot["articles"][0]
    assert article["title"] == "https://web.example/article"
    assert article["content_version_id"] == "content-pinned"
    assert article["provenance"]["title"] == {
        "basis": "canonical_url", "observation_id": None,
    }
    html = render_range_report_html(snapshot)
    assert "UNACTIVATED TITLE" not in html
    assert "Pinned web evidence says coastal resilience funding" in html


@pytest.mark.skipif(os.name == "nt", reason="api_server management locking requires POSIX fcntl")
def test_active_web_page_merges_same_named_public_history_for_chat_and_wiki(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import api_server

    repository = tmp_path / "repository"
    repository.mkdir()
    queue, runtime, wiki, sources = (
        tmp_path / name for name in ("queue", "runtime-wiki", "wiki", "sources")
    )
    for path in (queue, runtime, wiki, sources):
        path.mkdir()
    (wiki / "article-web-article.md").write_text(
        "# Published Public article\n\nPublished Public body.\n\n"
        "https://public.example/approved-history\n",
        encoding="utf-8",
    )
    runtime_db = tmp_path / "runtime.sqlite3"
    _seed_web(runtime_db)
    public_db = tmp_path / "public.sqlite3"
    with sqlite3.connect(public_db) as connection:
        apply_migrations(connection)

    monkeypatch.setenv("CLIMATE_PDF_INTAKE_QUEUE_DIR", str(queue))
    monkeypatch.setattr(api_server, "PDF_RUNTIME_WIKI_DIR", runtime)
    monkeypatch.setattr(api_server, "WIKI_DIR", wiki)
    monkeypatch.setattr(api_server, "SOURCE_DIR", sources)
    monkeypatch.setattr(api_server, "RELOAD_TOKEN", "reload-token")
    initial = AgenticWikiResponder(wiki, sources)
    initial.client = None
    monkeypatch.setattr(api_server, "responder", initial)
    monkeypatch.setattr(api_server._wiki_static_files, "all_directories", [str(wiki)])
    client = TestClient(api_server.app)

    def reload_chat(generation_id):
        response = client.post(
            "/api/reload", params={"generation_id": generation_id},
            headers={"x-reload-token": "reload-token"},
        )
        assert response.status_code == 200, response.text
        api_server.responder.client = None

    writer = PdfIntakePipeline(
        queue, runtime_db, tmp_path / "backups", runtime, reload_chat,
        repository_root=repository,
    )
    enqueue_web_activation(
        queue, runtime_db, "web-batch",
        frozen_payload_sha256="5" * 64, repository_root=repository,
    )
    assert writer.process_next()["chat_ready"] is True
    first_page = client.get("/wiki/article-web-article.md")
    assert "Pinned web evidence says coastal resilience" in first_page.text
    assert "Published Public body" in first_page.text
    assert "https://public.example/approved-history" in first_page.text
    merged_document = next(
        doc for doc in api_server.responder.kb.documents
        if doc.path == "wiki/article-web-article.md"
    )
    assert "Published Public body" in merged_document.markdown
    assert "Pinned web evidence says coastal resilience" in merged_document.markdown
    first_answer = client.post(
        "/api/chat",
        json={"message": "coastal resilience funding", "answerMode": "brief"},
    ).json()
    assert any(
        item["path"] == "wiki/article-web-article.md"
        and "Pinned web evidence says coastal resilience" in item["snippet"]
        for item in first_answer["sources"]
    )

    _add_web_observation(
        runtime_db, batch_id="web-update", item_id="web-update-item",
        article_id="web-article", content_version_id="web-update-content",
        body="Updated active overlay body replaces both older copies.",
        discovered_at="2026-10-03T12:00:00Z", publication_date="2026-02-01",
        title="Updated active Web evidence",
    )
    enqueue_web_activation(
        queue, runtime_db, "web-update",
        frozen_payload_sha256="6" * 64, repository_root=repository,
    )
    assert writer.process_next()["chat_ready"] is True
    updated_page = client.get("/wiki/article-web-article.md")
    assert "Updated active overlay body" in updated_page.text
    assert "Pinned web evidence says coastal resilience" not in updated_page.text
    assert "Published Public body" in updated_page.text
    updated_answer = client.post(
        "/api/chat",
        json={"message": "updated active overlay body", "answerMode": "brief"},
    ).json()
    assert any(
        item["path"] == "wiki/article-web-article.md"
        and "Updated active overlay body" in item["snippet"]
        for item in updated_answer["sources"]
    )


def test_legacy_pdf_projection_fails_closed_without_its_validated_snapshot(tmp_path):
    with pytest.raises(RuntimeError, match="legacy active PDF Registry snapshot is invalid"):
        _active_pdf_snapshot(
            tmp_path / "runtime",
            {"generation_id": "legacy", "registry_sha256": "a" * 64},
            {"pdf-occurrence"},
        )


def test_metadata_only_web_projection_does_not_expose_body(tmp_path):
    database = tmp_path / "registry.sqlite3"
    _seed_web(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE articles SET display_policy='metadata_only' "
            "WHERE article_id='web-article'"
        )
    wiki = tmp_path / "wiki"
    render_runtime_registry(
        wiki,
        web_database=database,
        pdf_database=None,
        manifest={
            "web_items": [{
                "acquisition_item_id": "web-item", "batch_id": "web-batch",
                "article_id": "web-article", "content_version_id": "content-pinned",
                "publication_date": "2026-01-01",
                "publication_date_evidence": {
                    "kind": "publisher", "text": "Published 2026-01-01",
                },
            }],
            "pdf_occurrence_ids": [],
        },
    )
    page = (wiki / "article-web-article.md").read_text(encoding="utf-8")
    assert "Web acquisition summary" in page
    assert "Pinned web evidence says coastal resilience funding" not in page
