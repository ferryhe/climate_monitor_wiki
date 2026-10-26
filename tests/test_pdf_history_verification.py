import hashlib
import json
import sqlite3
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

import api_server
from climate_monitor.pdf_intake import import_pdf_reports
from climate_registry.information_checks import evaluate, check_targets, deduplicate_pdf_occurrences, run_checks
from climate_registry.pdf_intake import persist_pdf_intake
from climate_registry.persistent import initialize_registry
from climate_registry.read_api import RegistryReader
from test_pdf_intake import _report_pdf, _report_pdf_with_two_articles
from test_issue170_pdf_import import _client
from test_information_checks import _database, _record, _judgment


# The old runtime-overlay history contract is retained for schema20 archives.
@pytest.fixture(autouse=True)
def legacy_pdf_history_contract(monkeypatch):
    from climate_registry.schema import apply_migrations as real_migrations
    import climate_registry.persistent as persistent
    import climate_registry.pdf_intake as storage
    import climate_registry.information_checks as checks
    import climate_monitor.meetings as meetings
    import climate_registry.publication as publication
    from test_information_checks import legacy_schema_writer,legacy_pdf_binding
    import climate_registry.pdf_pipeline as pipeline
    monkeypatch.setattr(pipeline,"_validate_pdf_binding",legacy_pdf_binding)
    monkeypatch.setattr(publication,"require_publication_migration",legacy_schema_writer)
    import test_issue170_pdf_import as api_fixture
    def migrate(connection, *, target_version=None):
        return real_migrations(connection,target_version=20 if target_version is None else target_version)
    monkeypatch.setattr(api_fixture,"apply_migrations",migrate)
    monkeypatch.setattr(persistent,"apply_migrations",migrate)
    monkeypatch.setattr(storage,"apply_migrations",migrate)
    monkeypatch.setattr(storage,"LATEST_SCHEMA_VERSION",20)
    monkeypatch.setattr(checks,"apply_migrations",migrate)
    monkeypatch.setattr(meetings,"SCHEMA_VERSION",20)
    from climate_monitor.pdf_intake import import_pdf_reports as current_import
    def legacy_import(inputs):
        bundle = current_import(inputs)
        bundle.pop("source_coverage")
        return bundle
    monkeypatch.setattr(sys.modules[__name__], "import_pdf_reports", legacy_import)
    monkeypatch.setattr(api_server, "import_pdf_reports", legacy_import)


def test_imported_pdf_history_retains_structure_original_download_and_one_copy(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "report.pdf"
    _report_pdf(path, include_article=True)
    bundle = import_pdf_reports([path])
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    persist_pdf_intake(database, tmp_path / "backups", bundle)
    renamed = tmp_path / "renamed.pdf"
    renamed.write_bytes(path.read_bytes())
    persist_pdf_intake(database, tmp_path / "backups", import_pdf_reports([renamed]))
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        article = dict(connection.execute("SELECT * FROM pdf_intake_articles LIMIT 1").fetchone())
        article.update(article_id="legacy-calendar-article", canonical_url="https://example.org/calendar-only", core_article_id=None)
        connection.execute("INSERT INTO pdf_intake_articles VALUES(" + ",".join("?" for _ in article) + ")", tuple(article.values()))
        occurrence = dict(connection.execute("SELECT * FROM pdf_intake_article_occurrences LIMIT 1").fetchone())
        occurrence.update(occurrence_id="legacy-calendar-occurrence", article_id=article["article_id"],
            occurrence_json=json.dumps({**json.loads(occurrence["occurrence_json"]), "summary_basis": "verbatim_pdf_calendar_row"}))
        connection.execute("INSERT INTO pdf_intake_article_occurrences VALUES(" + ",".join("?" for _ in occurrence) + ")", tuple(occurrence.values()))
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    client = TestClient(api_server.app)
    history = client.get("/api/registry/reports?include_pdf=true").json()
    assert history["pagination"]["total"] == 1
    item = history["items"][0]
    assert item["report_date"] == "2026-09-03" and item["source_label"] == "PDF import"
    sha = bundle["documents"][0]["source"]["sha256"]
    detail = client.get(f"/api/registry/pdf-intake/reports/{sha}").json()
    assert detail["edition"] == 9 and detail["reporting_period"]
    assert detail["executive_summary"] == [] and detail["pages"] == [] and detail["pdf_metadata"]
    assert detail["articles"] and detail["calendar_items"]
    assert item["article_count"] == len(detail["articles"]) == 1
    assert detail["source_filenames"] == ["renamed.pdf", "report.pdf"]
    assert "original_pdf_base64" not in json.dumps(detail)
    assert str(tmp_path) not in json.dumps(detail)
    downloaded = client.get(detail["report_pdf"]["download_url"])
    assert downloaded.status_code == 200 and downloaded.content == path.read_bytes()
    assert hashlib.sha256(downloaded.content).hexdigest() == sha
    status = client.get("/api/registry/status?include_pdf=true").json()
    assert status["reports"] == 1 and status["articles"] == 1 and status["latest_report_date"] == "2026-09-03"
    assert client.get("/api/registry/publishers?include_pdf=true").json()["items"] == [
        {"hostname": "example.org", "label": "example.org"}]


def test_published_pdf_report_deduplicates_exact_passages_only():
    document_sha = "a" * 64
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE pdf_intake_article_occurrences (
            occurrence_id TEXT, article_id TEXT, source_document_sha256 TEXT
        );
        CREATE TABLE pdf_intake_calendar_items (
            occurrence_id TEXT, event_id TEXT, source_document_sha256 TEXT
        );
        CREATE TABLE pdf_intake_articles (article_id TEXT);
        CREATE TABLE pdf_intake_documents (
            document_sha256 TEXT, filename TEXT, date_of_run TEXT, document_json TEXT,
            period_start TEXT, period_end TEXT, pdf_created_at TEXT, pdf_modified_at TEXT,
            imported_at TEXT, original_pdf BLOB
        );
    """)
    connection.execute(
        "INSERT INTO pdf_intake_documents VALUES(?,?,?,?,?,?,?,?,?,?)",
        (document_sha, "report.pdf", "2026-09-28", json.dumps({"title": "Report"}),
         None, None, None, None, "2026-09-28T00:00:00Z", None),
    )
    rows = [
        {"occurrence_id": "first-copy", "article_id": "article-a", "source_document_sha256": document_sha,
         "report_date": "2026-09-28", "page": 2, "raw_url": "https://example.org/report?a=1#top",
         "occurrence_json": json.dumps({"summary": "Same passage"})},
        {"occurrence_id": "second-copy", "article_id": "article-a", "source_document_sha256": document_sha,
         "report_date": "2026-09-28", "page": 2, "raw_url": "https://example.org/report?a=1#copy",
         "occurrence_json": json.dumps({"summary": "Same passage"})},
        {"occurrence_id": "distinct-page", "article_id": "article-a", "source_document_sha256": document_sha,
         "report_date": "2026-09-28", "page": 3, "raw_url": "https://example.org/report?a=1",
         "occurrence_json": json.dumps({"summary": "Same passage"})},
    ]
    connection.executemany(
        "INSERT INTO pdf_intake_article_occurrences VALUES (:occurrence_id,:article_id,:source_document_sha256)", rows
    )
    connection.execute("INSERT INTO pdf_intake_articles VALUES ('article-a')")
    snapshot = {
        "entity_kind": "pdf_article", "entity_id": "article-a", "_approved_candidate_sha256": "b" * 64,
        "dependencies": {"pdf_intake_documents": [{
            "document_sha256": document_sha, "filename": "report.pdf", "document_json": json.dumps({"title": "Report"}),
        }]},
        "tables": {
            "pdf_intake_articles": [{"article_id": "article-a", "canonical_url": "https://example.org/report"}],
            "pdf_intake_article_occurrences": rows,
        },
    }
    reader = object.__new__(RegistryReader)
    reader.public = True
    reader._snapshot_direct = True
    reader._pdf_document_cache = {}
    reader._pdf_source_cache = {}
    reader._pdf_check_packet_cache = {}
    reader._pdf_document_candidates = lambda _connection, _sha: {("pdf_article", "article-a"): snapshot}

    report = reader._pdf_report_from_published_candidates(
        connection, document_sha, allowed_occurrence_ids=None, allowed_calendar_ids=None, include_bytes=False
    )
    occurrences = report["articles"][0]["occurrences"]
    assert [item["occurrence_id"] for item in occurrences] == ["distinct-page", "second-copy"]
    assert report["article_count"] == 1


def test_unified_articles_share_pagination_and_keep_linked_pdf_details(tmp_path, monkeypatch):
    client, database = _client(monkeypatch, tmp_path)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    path = tmp_path / "articles.pdf"
    _report_pdf_with_two_articles(path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE articles SET canonical_url='https://example.org/first-study'")
    bundle = import_pdf_reports([path])
    persist_pdf_intake(database, tmp_path / "backups", bundle)
    result = client.get("/api/registry/articles?include_pdf=true&page_size=1").json()
    assert result["pagination"]["total"] == 3 and result["pagination"]["pages"] == 3
    items = result["items"] + [row for page in (2,3) for row in client.get(f"/api/registry/articles?include_pdf=true&page_size=1&page={page}").json()["items"]]
    assert len({item["article_id"] for item in items}) == 3
    assert len({item["canonical_url"] for item in items}) == 2
    assert {item["source_kind"] for item in items} == {"registry", "pdf"}
    core = next(item for item in items if item["source_kind"] == "registry")
    assert core.get("source_label") != "Registry · PDF import" and core.get("pdf_occurrence_count",0) == 0
    core_search = client.get("/api/registry/articles?include_pdf=true&query=Core").json()["items"]
    assert len(core_search) == 1 and core_search[0].get("source_label") == core.get("source_label")
    assert core_search[0].get("pdf_occurrence_count",0) == 0
    searched = client.get("/api/registry/articles?include_pdf=true&query=First").json()["items"]
    assert len(searched) == 1 and searched[0]["article_id"] != core["article_id"] and searched[0]["source_kind"] == "pdf"
    assert client.get(f"/api/registry/pdf-intake/articles/{searched[0]['article_id']}").json()["occurrences"]
    detail=client.get(f"/api/registry/articles/{core['article_id']}").json()
    assert detail["report_summary"] == "Core report summary." and not detail.get("pdf_occurrences")
    assert client.get("/api/registry/articles?include_pdf=true&query=Second").json()["pagination"]["total"] == 1
    assert client.get("/api/registry/articles?include_pdf=true&source=missing.org").json()["pagination"]["total"] == 0
    assert client.get("/api/registry/status?include_pdf=true").json()["articles"] == 3


def test_pdf_history_lists_archives_and_downloads_both_while_filtering_extracted_content(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    public, runtime = tmp_path / "public.sqlite3", tmp_path / "runtime.sqlite3"
    initialize_registry(public)
    initialize_registry(runtime)
    bundles = []
    for name, day in (("active", "4–5 Sep 2026"), ("pending", "6–7 Sep 2026")):
        path = tmp_path / f"{name}.pdf"
        _report_pdf(path, calendar_date=day, include_article=True)
        bundle = import_pdf_reports([path])
        persist_pdf_intake(runtime, tmp_path / "backups", bundle)
        bundles.append(bundle)
    active = bundles[0]
    manifest = {"pdf_occurrence_ids": [item["occurrence_id"] for article in active["articles"] for item in article["occurrences"]],
        "pdf_calendar_occurrence_ids": [item["occurrence_id"] for item in active["calendar_items"]], "web_items": []}
    reader = RegistryReader(runtime, repository_root=tmp_path / "application")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(public))
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, reader, manifest))
    client = TestClient(api_server.app)
    history = client.get("/api/registry/reports?include_pdf=true").json()
    assert history["pagination"]["total"] == 2
    pending_sha = bundles[1]["documents"][0]["source"]["sha256"]
    pending_detail = client.get(f"/api/registry/pdf-intake/reports/{pending_sha}")
    assert pending_detail.status_code == 200
    assert pending_detail.json()["articles"] == [] and pending_detail.json()["calendar_items"] == []
    assert pending_detail.json()["executive_summary"] == [] and pending_detail.json()["pages"] == []
    pending_download = client.get(f"/api/registry/pdf-intake/reports/{pending_sha}/pdf")
    assert pending_download.status_code == 200
    assert hashlib.sha256(pending_download.content).hexdigest() == pending_sha
    active_sha = active["documents"][0]["source"]["sha256"]
    active_download = client.get(f"/api/registry/pdf-intake/reports/{active_sha}/pdf")
    assert active_download.status_code == 200
    assert hashlib.sha256(active_download.content).hexdigest() == active_sha
    with sqlite3.connect(public) as connection:
        # Migrated Public documents can predate retained original PDF bytes.
        connection.execute("ATTACH DATABASE ? AS imported", (str(runtime),))
        for table in ("pdf_intake_documents", "pdf_intake_articles", "pdf_intake_article_occurrences", "pdf_intake_calendar_items"):
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            values = ",".join("NULL" if name == "original_pdf" else name for name in columns)
            column = "document_sha256" if table == "pdf_intake_documents" else "source_document_sha256"
            condition = "" if table == "pdf_intake_articles" else f" WHERE {column}=?"
            connection.execute(f"INSERT INTO {table} SELECT {values} FROM imported.{table}{condition}", () if not condition else (active_sha,))
    overlapping = client.get(f"/api/registry/pdf-intake/reports/{active_sha}").json()
    downloaded = client.get(overlapping["report_pdf"]["download_url"])
    assert downloaded.status_code == 200 and hashlib.sha256(downloaded.content).hexdigest() == active_sha
    assert client.get("/api/registry/reports?include_pdf=true").json()["pagination"]["total"] == 2


def test_active_web_article_excludes_pending_pdf_until_separately_activated(tmp_path, monkeypatch):
    client, _ = _client(monkeypatch, tmp_path)
    from test_issue113_range_reports import _database as active_database
    from climate_registry.web_ingest_pipeline import _batch_items, _manifest_item
    active_root = tmp_path / "active-web"
    active_root.mkdir()
    runtime = active_database(active_root)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with sqlite3.connect(runtime) as connection:
        connection.execute("UPDATE articles SET canonical_url='https://example.org/first-study' WHERE article_id='article-a'")
        existing_links=connection.execute("SELECT article_id,core_article_id,confirmation_basis FROM pdf_intake_articles ORDER BY article_id").fetchall()
    path = tmp_path / "pending.pdf"
    _report_pdf_with_two_articles(path)
    bundle = import_pdf_reports([path])
    persist_pdf_intake(runtime, tmp_path / "backups", bundle)
    public = tmp_path / "public.sqlite3"
    initialize_registry(public)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(public))
    reader = RegistryReader(runtime, repository_root=tmp_path / "application")
    identity = next(_manifest_item(item) for item in _batch_items(runtime, "batch") if item["acquisition_item_id"] == "a-1")
    manifest = {"web_items": [identity], "pdf_occurrence_ids": []}
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (reader, None, manifest))
    listed = client.get("/api/registry/articles?include_pdf=true").json()["items"]
    assert len(listed) == 1 and listed[0]["pdf_occurrence_count"] == 0
    detail = client.get("/api/registry/articles/article-a").json()
    assert not detail.get("pdf_occurrences")
    assert detail["content"]["content_version_id"] == identity["content_version_id"]
    assert detail["summary"].startswith("Summary for Registry-only climate article")
    assert client.get("/api/registry/pdf-intake/articles").json()["pagination"]["total"] == 0
    manifest["pdf_occurrence_ids"] = [item["occurrence_id"] for article in bundle["articles"] for item in article["occurrences"]]
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (reader, reader, manifest))
    # Activating source observations is not approval that they are this web Article.
    detail=client.get("/api/registry/articles/article-a").json()
    assert not detail.get("pdf_occurrences")
    assert detail["content"]["content_version_id"] == identity["content_version_id"]
    assert detail["summary"].startswith("Summary for Registry-only climate article")
    listed=client.get("/api/registry/articles?include_pdf=true").json()
    assert listed["pagination"]["total"] == 3
    assert {row["article_id"] for row in listed["items"]} == {"article-a"}|{row["article_id"] for row in bundle["articles"]}
    assert next(row for row in listed["items"] if row["article_id"] == "article-a").get("pdf_occurrence_count",0) == 0
    assert client.get("/api/registry/pdf-intake/articles").json()["pagination"]["total"] == 2
    with sqlite3.connect(runtime) as connection:
        # The range fixture already has older explicit link rows. This import
        # neither rewrites them nor grants identity to these newly activated sources.
        imported_ids={row["article_id"] for row in bundle["articles"]}
        rows=connection.execute("SELECT article_id,core_article_id,confirmation_basis FROM pdf_intake_articles ORDER BY article_id").fetchall()
        assert all(row in rows for row in existing_links)
        assert all(row[1] is None and row[2] is None for row in rows if row[0] in imported_ids)


def test_verified_article_uses_existing_body_enrichment_and_conflict_has_none(tmp_path):
    database = _database(tmp_path)
    target = check_targets(database, "articles", {"article-1"})[0]
    body = ("# Climate publication\n\nEmissions decreased. Climate insurance companies assess physical risk, "
        "capital exposure, biodiversity impacts and transition scenarios using scientific research and financial data.")
    body = "[![](https://www.e.org/cover.png)](https://download.example.org/guide)\n\n" + body
    packet = evaluate(target, "articles", _record(target["occurrence_id"], target["source_url"], body), _judgment)
    assert packet["verification_status"] == "verified"
    information = packet["verified_information"]
    assert information["summary"].startswith("Climate publication Emissions decreased.")
    assert information["categories"] and len(information["keywords"]) >= 8
    assert information["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    conflicting = check_targets(database, "articles", {"article-2"})[0]
    assert "verified_information" not in evaluate(conflicting, "articles", _record("article-2", conflicting["source_url"], body), _judgment)


def test_enrichment_failure_remains_visible_and_retryable(tmp_path):
    database = _database(tmp_path)
    options = dict(database=database, kind="articles", backup_dir=tmp_path / "backups",
        occurrence_ids={"article-1"}, verifier=_judgment)
    short = run_checks(**options, fetcher=lambda identifier, url: _record(identifier, url, "Emissions decreased."))
    assert short["status"] == "partial" and short["unverified_count"] == 0
    assert short["enrichment_failed_count"] == 1
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    checked = next(item for item in reader.pdf_article("pdf-article")["occurrences"] if item["occurrence_id"] == "article-1")
    assert checked["verification_status"] == "verified" and checked["verified_information"] is None
    assert checked["checks"][0]["enrichment_error"] == "insufficient_content"
    retry = run_checks(**options, retry_run_id=short["run_id"],
        fetcher=lambda *_: pytest.fail("verified observations are skipped even when enrichment failed"))
    assert retry["status"] == "complete" and retry["item_count"] == 0
    body = "Climate insurance companies assess physical risk, capital exposure, biodiversity impacts and transition scenarios using scientific research and financial data."
    repair = run_checks(**options, reverify_all=True, fetcher=lambda identifier, url: _record(identifier, url, body))
    assert repair["status"] == "complete" and repair["item_count"] == 1 and repair["enrichment_failed_count"] == 0
    from climate_registry.wiki import render_runtime_registry
    page = render_runtime_registry(tmp_path / "wiki", web_database=None, pdf_database=database,
        manifest={"web_items": [], "pdf_occurrence_ids": ["article-1"]})["registry-source-observation-article-1.md"]
    assert "Verified information" in page and "Categories:" in page and "Keywords:" in page


def test_duplicate_observations_remain_checkable_and_runtime_can_refresh(tmp_path, monkeypatch):
    from climate_registry.wiki import render_runtime_registry
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        row = list(connection.execute("SELECT * FROM pdf_intake_article_occurrences WHERE occurrence_id='article-1'").fetchone())
        row[0] = "article-copy"
        connection.execute("INSERT INTO pdf_intake_article_occurrences VALUES(" + ",".join("?" for _ in row) + ")", row)
    assert len(check_targets(database, "articles")) == 3
    named = check_targets(database, "articles", {"article-1", "article-copy"})
    assert {item["occurrence_id"] for item in named} == {"article-1", "article-copy"}
    pages = render_runtime_registry(tmp_path / "wiki", web_database=None, pdf_database=database,
        manifest={"web_items": [], "pdf_occurrence_ids": ["article-1", "article-copy"]})
    assert sum(page.count("Emissions decreased.") for page in pages.values()) == 1
    assert "Emissions decreased." not in pages["registry-source-observations.md"]
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    client = TestClient(api_server.app)
    listed = client.get("/api/registry/articles?include_pdf=true").json()["items"]
    assert len(listed) == 1 and listed[0]["occurrence_count"] == 3
    retained=client.get("/api/registry/pdf-intake/articles/pdf-article").json()["occurrences"]
    assert {item["occurrence_id"] for item in retained} == {"article-1","article-2","article-copy"}


def test_duplicate_pdf_passages_merge_checks_but_keep_different_reports():
    first = {"occurrence_id": "old", "source_document_sha256": "a" * 64, "page": 26,
        "raw_url": "https://example.org/study", "summary": "Same report passage."}
    checked = {**first, "occurrence_id": "new", "summary": "Same  report\npassage.", "checks": [{
        "source_url": first["raw_url"], "access_status": "accessible", "verification_status": "verified",
        "checked_at": "2026-10-05T09:00:00Z", "website_candidate": None,
        "verified_information": {"categories": ["Physical risk"], "keywords": ["insurance"]}}]}
    values = deduplicate_pdf_occurrences([first, checked, {**first, "source_document_sha256": "b" * 64}])
    assert len(values) == 2 and values[0]["verification_status"] == "verified"
    assert values[0]["verified_information"]["categories"] == ["Physical risk"]


@pytest.mark.parametrize("filename,public_filename", [
    ("registry-source-observations.md", "registry-source-observations.md"),
    ("article-example.md", "article-example.md"),
    ("article-example.md", "registry-source-observations.md")])
def test_public_and_runtime_pdf_passage_merge_retains_verified_information(tmp_path, filename, public_filename):
    from agentic_wiki.wiki_agent import AgenticWikiResponder, merge_registry_runtime_markdown
    from climate_registry.wiki import _render_registry_article, _registry_source_observation_pages
    original = {"occurrence_id": "public-copy", "source_document_sha256": "a" * 64, "page": 26,
        "raw_url": "https://example.org/study", "summary": "Original climate PDF passage.",
        "source_observations": [{"filename": "climate.pdf"}]}
    checked = {**original, "occurrence_id": "runtime-copy", "verified_information": {
        "summary": "Website climate evidence.", "categories": ["Physical risk"], "keywords": ["insurance"],
        "source_url": original["raw_url"], "generated_at": "2026-10-05"}}
    other = {**original, "occurrence_id": "other-passage", "summary": "Another distinct PDF passage."}
    def render(name, occurrences):
        if name.startswith("article-"):
            return _render_registry_article({"article_id": "example", "title": "Climate study",
                "canonical_url": original["raw_url"], "pdf_occurrences": occurrences})
        return "\n".join(_registry_source_observation_pages([{"occurrences": occurrences}]).values())
    public, runtime = render(public_filename, [original, other]), render(filename, [checked])
    merged = merge_registry_runtime_markdown(filename, public, runtime)
    assert merged.count(original["summary"]) == merged.count(other["summary"]) == 1
    assert merged.count("Verified information") == 1 and "Physical risk" in merged and "insurance" in merged
    reverse = merge_registry_runtime_markdown(filename, runtime, public)
    assert reverse.count(original["summary"]) == 1 and "Verified information" in reverse
    wiki, overlay, sources = [tmp_path / name for name in ("wiki", "overlay", "sources")]
    for path in (wiki, overlay, sources):
        path.mkdir()
    (wiki / public_filename).write_text(public)
    (overlay / filename).write_text(runtime)
    responder = AgenticWikiResponder(wiki, sources, overlay)
    corpus = "\n".join(doc.markdown for doc in responder.kb.documents)
    assert corpus.count(original["summary"]) == 1 and "Verified information" in corpus
    assert sum(chunk.markdown.count(original["summary"]) for chunk in responder.kb.chunks) == 1
    files = api_server.WikiStaticFiles(directory=str(wiki))
    files.all_directories.insert(0, str(overlay))
    assert files._merged_markdown(filename).count(original["summary"]) == 1
    if filename != public_filename:
        assert original["summary"] not in files._merged_markdown(public_filename)
        assert other["summary"] in files._merged_markdown(public_filename)


def test_pdf_source_handoff_retains_published_baseline_and_late_retry_keeps_human_date(tmp_path, monkeypatch):
    # This test exercises the current schema, independently of the archive fixture.
    monkeypatch.undo()
    from climate_monitor.pdf_intake import import_pdf_reports
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.read_api import RegistryReader
    from test_owner_publication_dates import _seed
    from test_article_content_dates import _manual
    from test_owner_checks import _ingest
    from reportlab.pdfgen.canvas import Canvas
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    database = _seed(tmp_path)
    _ingest(database, tmp_path, _manual(database))
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    from test_verified_publication import _check,_approve_all
    from test_identity_relations import _merge_identity
    known=tmp_path/"known-same-object.pdf"
    canvas=Canvas(str(known),invariant=1)
    canvas.drawString(50,760,"UPDATES")
    canvas.drawString(50,740,"Original PDF title")
    canvas.drawString(50,720,"Original PDF observation")
    canvas.linkURL("https://example.org/good",(48,738,280,755),relative=0)
    canvas.save()
    known_bundle=import_pdf_reports([known])
    known_article=known_bundle["articles"][0]
    persist_pdf_intake(database,tmp_path/"pdf-backups",known_bundle)
    management=RegistryReader(database,repository_root=tmp_path/"application",public=False)
    assert management.pdf_source_coverage(known_bundle["documents"][0]["source"]["sha256"])["history"][0]["published_baselines"][0]["status"] == "pending_g4"
    _check(database,tmp_path,ids={"core:good",*[row["occurrence_id"] for row in known_article["occurrences"]]},
        body="Title good. Summary good. Original PDF title. Original PDF observation. Same climate report landing and download.")
    _approve_all(database)
    _merge_identity(database,tmp_path,[("pdf_article",known_article["article_id"])])
    before = reader.article("good")
    source = tmp_path / "old-original-upload.pdf"
    canvas = Canvas(str(source), invariant=1)
    for y, text in ((760, "UPDATES"), (740, "Old original PDF title"),
        (720, "IN WINDOW 14 SEP 2025 REPORT"), (700, "Old verbatim PDF observation, not the current article body.")):
        canvas.drawString(50, y, text)
    canvas.linkURL("https://example.org/good", (48, 738, 280, 755), relative=0)
    canvas.save()
    bundle = import_pdf_reports([source])
    bundle["articles"][0]["type_safe_classification"] = {"provider": "typesafe", "label": "article"}
    persist_pdf_intake(database, tmp_path / "pdf-backups", bundle)
    management = RegistryReader(database, repository_root=tmp_path / "application", public=False)
    history = management.pdf_source_coverage(bundle["documents"][0]["source"]["sha256"])["history"]
    baseline = history[0]["published_baselines"][0]
    assert baseline["status"] == "published" and baseline["read_at"] and baseline["requires_downstream_recheck"]
    assert baseline["public_view"]["derived_display"]["manual_date_overrides"]["publication_date"] == before["manual_date_overrides"]["publication_date"]
    assert reader.article("good")["summary"] == before["summary"]
    _ingest(database, tmp_path, _manual(database, fields={"publication_date": "2026-10-03"}))
    current = reader.article("good")
    with sqlite3.connect(database) as connection:
        body = connection.execute("SELECT current_version_id,current_content_version_id FROM articles WHERE article_id='good'").fetchone()
        publication = connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()
    connection.close()
    persist_pdf_intake(database, tmp_path / "pdf-backups", bundle)
    after = reader.article("good")
    assert after["publication_date"] == "2026-10-03"
    assert after["manual_date_overrides"] == current["manual_date_overrides"]
    assert after["summary"] == before["summary"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT current_version_id,current_content_version_id FROM articles WHERE article_id='good'").fetchone() == body
        assert connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone() == publication
    connection.close()
    # The original observed baseline remains historical; downstream must re-read it.
    assert management.pdf_source_coverage(bundle["documents"][0]["source"]["sha256"])["history"] == history


@pytest.mark.parametrize("instant,runs", [("2026-10-06T09:00:00Z", True), ("2026-10-06T10:00:00Z", True),
    ("2026-12-06T09:00:00Z", True), ("2026-12-06T10:00:00Z", True), ("2026-10-06T17:30:00Z", True)])
def test_incremental_checker_dispatches_in_both_seasons_and_during_daytime(tmp_path, instant, runs, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/hermes_job_information_check.sh"
    date = tmp_path / "date"
    date.write_text('#!/bin/sh\nexec /bin/date --date="$CHECK_TEST_TIME" +%H\n')
    date.chmod(0o700)
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/sh\n[ "$6" = "config" ] && [ "$7" = "--format" ] && [ "$8" = "json" ] || exit 1\nprintf \'%s\\n\' \'{"name":"seasonal-fixture","services":{"pdf-intake-writer":{}}}\'\n')
    docker.chmod(0o700)
    sudo = tmp_path / "sudo"
    sudo.write_text('#!/bin/sh\nif [ "$3" = "ps" ]; then printf "seasonal-writer\\n"; exit 0; fi\nwhile [ "$#" -gt 0 ]; do\n  if [ "$1" = "-c" ]; then shift; printf "%s" "$1" > "$CHECK_TEST_PACKET"; exit 0; fi\n  shift\ndone\nexit 1\n')
    sudo.chmod(0o700)
    packet = tmp_path / "packet.py"
    configuration = tmp_path / "configuration.env"
    configuration.write_text("TYPESAFE_API_KEY=test-only-placeholder\n")
    result = subprocess.run(["bash", str(script)], env={**os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"], "CHECK_TEST_TIME": instant,
        "PYTHON": sys.executable,
        "CHECK_TEST_PACKET": str(packet), "CLIMATE_WIKI_ENV_FILE": str(configuration)}, capture_output=True, text=True)
    assert result.returncode == 0 and packet.exists() is runs
    if runs:
        for name in ("CLIMATE_REGISTRY_DB", "CLIMATE_REGISTRY_BACKUP_DIR", "CLIMATE_PDF_INTAKE_QUEUE_DIR",
            "CLIMATE_PDF_RUNTIME_WIKI_DIR", "CLIMATE_PDF_RELOAD_URL", "CLIMATE_ACQUISITION_RUN_DIR", "RELOAD_TOKEN"):
            monkeypatch.setenv(name, "configured")
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.setattr(sys, "stdin", StringIO(configuration.read_text()))
        monkeypatch.setattr(sys, "argv", ["-c", "--preflight"])
        monkeypatch.setattr("climate_monitor.article_content_adapter.check_dependencies", lambda: "available")
        with pytest.raises(SystemExit) as ready:
            exec(compile(packet.read_text(), "daily-check", "exec"), {})
        assert ready.value.code == 0 and os.environ["TYPESAFE_API_KEY"] == "test-only-placeholder"
        monkeypatch.setattr(sys, "stdin", StringIO(""))
        with pytest.raises(SystemExit) as missing:
            exec(compile(packet.read_text(), "daily-check", "exec"), {})
        assert missing.value.code == 2 and os.environ["TYPESAFE_API_KEY"] == ""


def test_reviewed_pdf_alias_keeps_duplicate_source_ids_and_single_body_render(tmp_path,monkeypatch):
    monkeypatch.undo()
    from test_identity_relations import _seed,_merge_identity
    from test_registry_publication import _pdf
    from test_verified_publication import _check,_approve_all
    from climate_registry.publication import public_wiki_pages
    database=_seed(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-copy",url="https://example.org/good",occurrence_id="article-1",verified=False)
        row=list(db.execute("SELECT * FROM pdf_intake_article_occurrences WHERE occurrence_id='article-1'").fetchone())
        row[0]="article-copy"
        db.execute("INSERT INTO pdf_intake_article_occurrences VALUES(?,?,?,?,?,?,?,?,?,?)",row)
    _check(database,tmp_path,ids={"core:good","article-1","article-copy"},
        body="Title good. Summary good. Original PDF title. Original PDF observation. Same climate report landing and download.")
    _approve_all(database)
    _merge_identity(database,tmp_path,[("pdf_article","pdf-copy")])
    reader=RegistryReader(database,repository_root=tmp_path/"application")
    for direct in (True,False):
        with reader.public_snapshot(direct=direct):
            assert {row["occurrence_id"] for row in reader.article("good")["pdf_occurrences"]} == {"article-1","article-copy"}
            assert next(item for item in reader.articles()["items"] if item["article_id"] == "good")["pdf_occurrence_count"] == 2
    pages=public_wiki_pages(database)
    assert pages["article-good.md"].count("Original PDF observation") == 1
