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
    assert detail["executive_summary"] and detail["pages"] and detail["pdf_metadata"]
    assert detail["articles"] and detail["calendar_items"]
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
    assert result["pagination"]["total"] == 2 and result["pagination"]["pages"] == 2
    items = result["items"] + client.get("/api/registry/articles?include_pdf=true&page_size=1&page=2").json()["items"]
    assert len({item["canonical_url"] for item in items}) == 2
    assert {item["source_kind"] for item in items} == {"registry", "pdf"}
    core = next(item for item in items if item["source_kind"] == "registry")
    assert core["source_label"] == "Registry · PDF import"
    searched = client.get("/api/registry/articles?include_pdf=true&query=First").json()["items"]
    assert len(searched) == 1 and searched[0]["article_id"] == core["article_id"] and searched[0]["source_kind"] == "registry"
    assert client.get(f"/api/registry/articles/{searched[0]['article_id']}").json()["report_summary"] == "Core report summary."
    assert client.get(f"/api/registry/articles/{core['article_id']}").json()["pdf_occurrences"]
    assert client.get("/api/registry/articles?include_pdf=true&query=Second").json()["pagination"]["total"] == 1
    assert client.get("/api/registry/articles?include_pdf=true&source=missing.org").json()["pagination"]["total"] == 0
    assert client.get("/api/registry/status?include_pdf=true").json()["articles"] == 2


def test_pdf_history_and_downloads_only_include_active_runtime_documents(tmp_path, monkeypatch):
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
    assert client.get("/api/registry/reports?include_pdf=true").json()["pagination"]["total"] == 1
    pending_sha = bundles[1]["documents"][0]["source"]["sha256"]
    assert client.get(f"/api/registry/pdf-intake/reports/{pending_sha}/pdf").status_code == 404
    active_sha = active["documents"][0]["source"]["sha256"]
    assert client.get(f"/api/registry/pdf-intake/reports/{active_sha}/pdf").status_code == 200
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
    assert client.get("/api/registry/reports?include_pdf=true").json()["pagination"]["total"] == 1


def test_verified_article_uses_existing_body_enrichment_and_conflict_has_none(tmp_path):
    database = _database(tmp_path)
    target = check_targets(database, "articles", {"article-1"})[0]
    body = ("# Climate publication\n\nEmissions decreased. Climate insurance companies assess physical risk, "
        "capital exposure, biodiversity impacts and transition scenarios using scientific research and financial data.")
    packet = evaluate(target, "articles", _record(target["occurrence_id"], target["source_url"], body), _judgment)
    assert packet["verification_status"] == "verified"
    information = packet["verified_information"]
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
    body = "Climate insurance companies assess physical risk, capital exposure, biodiversity impacts and transition scenarios using scientific research and financial data."
    retry = run_checks(**options, retry_run_id=short["run_id"], fetcher=lambda identifier, url: _record(identifier, url, body))
    assert retry["status"] == "complete" and retry["item_count"] == 1 and retry["enrichment_failed_count"] == 0
    from climate_registry.wiki import render_runtime_registry
    page = render_runtime_registry(tmp_path / "wiki", web_database=None, pdf_database=database,
        manifest={"web_items": [], "pdf_occurrence_ids": ["article-1"]})["registry-source-observations.md"]
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
    assert pages["registry-source-observations.md"].count("Emissions decreased.") == 1
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    client = TestClient(api_server.app)
    listed = client.get("/api/registry/articles?include_pdf=true").json()["items"]
    assert len(listed) == 1 and listed[0]["occurrence_count"] == 2
    assert len(client.get("/api/registry/pdf-intake/articles/pdf-article").json()["occurrences"]) == 2
    linked = tmp_path / "linked"
    linked.mkdir()
    client, linked_database = _client(monkeypatch, linked)
    with sqlite3.connect(linked_database) as connection:
        connection.execute("UPDATE articles SET canonical_url='https://example.org/events'")
        connection.execute("ATTACH DATABASE ? AS imported", (str(database),))
        connection.execute("INSERT INTO pdf_intake_documents SELECT * FROM imported.pdf_intake_documents")
        connection.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at,core_article_id) VALUES('pdf-article','https://example.org/events','Climate publication','2026-10-04','core-climate-study')")
        connection.execute("INSERT INTO pdf_intake_article_occurrences SELECT * FROM imported.pdf_intake_article_occurrences")
    pages = render_runtime_registry(tmp_path / "confirmed-wiki", web_database=None, pdf_database=linked_database,
        manifest={"web_items": [], "pdf_occurrence_ids": ["article-1", "article-copy"]})
    assert pages["article-core-climate-study.md"].count("Emissions decreased.") == 1
    assert len(client.get("/api/registry/articles/core-climate-study").json()["pdf_occurrences"]) == 2
    assert client.get("/api/registry/articles?include_pdf=true").json()["items"][0]["pdf_occurrence_count"] == 2


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
    from climate_registry.wiki import _render_registry_article, _render_registry_source_observations
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
        return _render_registry_source_observations([{"occurrences": occurrences}])
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


@pytest.mark.parametrize("instant,runs", [("2026-10-06T09:00:00Z", True), ("2026-10-06T10:00:00Z", False),
    ("2026-12-06T09:00:00Z", False), ("2026-12-06T10:00:00Z", True)])
def test_daily_checker_runs_at_five_et_in_both_seasons(tmp_path, instant, runs, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "scripts/hermes_job_information_check.sh"
    date = tmp_path / "date"
    date.write_text('#!/bin/sh\nexec /bin/date --date="$CHECK_TEST_TIME" +%H\n')
    date.chmod(0o700)
    sudo = tmp_path / "sudo"
    sudo.write_text('#!/bin/sh\nprintf "%s" "$8" > "$CHECK_TEST_PACKET"\n')
    sudo.chmod(0o700)
    packet = tmp_path / "packet.py"
    configuration = tmp_path / "configuration.env"
    configuration.write_text("TYPESAFE_API_KEY=test-only-placeholder\n")
    result = subprocess.run(["bash", str(script)], env={**os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"], "CHECK_TEST_TIME": instant,
        "CHECK_TEST_PACKET": str(packet), "CLIMATE_WIKI_ENV_FILE": str(configuration)}, capture_output=True, text=True)
    assert result.returncode == 0 and packet.exists() is runs
    if runs:
        for name in ("CLIMATE_REGISTRY_WRITER_DB", "CLIMATE_REGISTRY_BACKUP_DIR", "CLIMATE_PDF_INTAKE_QUEUE_DIR",
            "CLIMATE_PDF_RUNTIME_WIKI_DIR", "CLIMATE_PDF_RELOAD_URL", "RELOAD_TOKEN"):
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
