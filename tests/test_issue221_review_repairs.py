"""Regression tests for the frozen-public-read fixes from the Issue #221 review."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from climate_registry.publication import (
    _approve, export_public_snapshot, install_git_snapshot, public_wiki_pages,
    set_visibility, stage_entities, stage_snapshot, snapshot_entity,
)
from climate_registry.read_api import RegistryReader
from climate_registry.schema import apply_migrations

from test_registry_publication import _database, _pdf, _reader


def _review4_legacy_candidates():
    path = Path(__file__).parent / "fixtures" / "issue221_review4_legacy_candidates.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _insert_fixture_rows(connection, table, rows):
    for row in rows:
        columns = list(row)
        connection.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(row[column] for column in columns))


def _approve_all(connection, **kwargs):
    for candidate in stage_entities(connection, **kwargs):
        _approve(connection, candidate, {"basis": "Issue #221 approved fixture"}, status="accepted_legacy")


def _report_database(tmp_path):
    from climate_registry.persistent import initialize_registry, update_registry

    source = tmp_path / "sources"
    source.mkdir()
    (source / "climate-monitor-2026-09-07.md").write_text(
        "# Weekly Climate Monitor\n**Report Date:** 2026-09-07\n"
        "## Executive Summary\n- Sites checked: **1**, succeeded: **1**, failed: **0**\n"
        "## Pillar A — Changes\n- **Real climate article**\n  - Real report observation.\n"
        "  🔗 https://example.org/article\n- **Reference record**\n"
        "  - Reference observed summary.\n  🔗 https://example.org/reference\n"
        "## Pillar B — Intelligence\n## Original Links\n"
        "- https://example.org/article\n- https://example.org/reference\n",
        encoding="utf-8",
    )
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    update_registry(source, database, tmp_path / "backups")
    with sqlite3.connect(database) as connection:
        _approve_all(connection, source_dir=source)
        reference_id = connection.execute(
            "SELECT article_id FROM articles WHERE canonical_url='https://example.org/reference'"
        ).fetchone()[0]
    return database, source, reference_id


def test_pdf_report_reads_only_target_document_and_approved_associations(tmp_path, monkeypatch):
    database = _database(tmp_path)
    shas = [f"{value:064x}" for value in (1, 2, 3)]
    with sqlite3.connect(database) as connection:
        for index, sha in enumerate(shas):
            article_id = f"pdf-{index}"
            occurrence_id = f"occ-{index}"
            connection.execute(
                "INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,"
                "extracted_text_sha256,document_json,imported_at) VALUES(?,?,?,'application/pdf',1,?,?,?)",
                (sha, f"/evidence/{index}.pdf", f"{index}.pdf", "e" * 64,
                 json.dumps({"title": f"Document {index}", "pages": [{"page": 1, "text": f"Text {index}"}]}),
                 "2026-09-01"),
            )
            connection.execute(
                "INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES(?,?,?,?)",
                (article_id, f"https://pdf.example/{index}", f"Article {index}", "2026-09-01"),
            )
            observation = {"summary": f"Approved {index}", "summary_basis": "verbatim_pdf_article_row"}
            connection.execute(
                "INSERT INTO pdf_intake_article_occurrences(occurrence_id,article_id,source_document_sha256,"
                "page,raw_url,report_date,content_sha256,page_sha256,occurrence_json) "
                "VALUES(?,?,?,1,?,'2026-09-01',?,?,?)",
                (occurrence_id, article_id, sha, f"https://pdf.example/{index}", "d" * 64, "c" * 64,
                 json.dumps(observation)),
            )
        _approve_all(connection)

    def fail_full_collection(*_args, **_kwargs):
        pytest.fail("single-document PDF detail used a full-document collection")

    monkeypatch.setattr(RegistryReader, "pdf_reports_all", fail_full_collection)
    monkeypatch.setattr(RegistryReader, "pdf_articles_all", fail_full_collection)
    monkeypatch.setattr(RegistryReader, "pdf_calendar_items_all", fail_full_collection)
    result = _reader(database, tmp_path).pdf_report(shas[0])
    assert result["document_sha256"] == shas[0]
    assert [item["article_id"] for item in result["articles"]] == ["pdf-0"]
    assert result["source_filenames"] == ["0.pdf"]


def test_candidate_hash_is_reused_across_one_core_collection_request(tmp_path, monkeypatch):
    import climate_registry.publication as publication

    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        _approve_all(connection)
    counts = {}
    original = publication.digest

    def counted(value):
        if isinstance(value, dict) and value.get("entity_kind"):
            key = (value["entity_kind"], value["entity_id"])
            counts[key] = counts.get(key, 0) + 1
        return original(value)

    monkeypatch.setattr(publication, "digest", counted)
    result = _reader(database, tmp_path).articles()
    assert {item["article_id"] for item in result["items"]} == {"a", "b"}
    assert counts == {("article", "a"): 1, ("article", "b"): 1}
    assert all(item["record_reference"]["candidate_sha256"] == item["published_candidate_sha256"]
        for item in result["items"])


def test_meeting_content_stays_approved_while_latest_run_coverage_updates(tmp_path, monkeypatch):
    import climate_monitor.meetings as meetings
    from test_issue136_meetings import _candidate, _database as meeting_database

    body = "World Climate Summit 2027 meets June 10–12, 2027 in New York. Registration deadline May 1, 2027. Register https://example.com/register. Climate risk agenda."
    database = meeting_database(tmp_path, [body])
    meetings.process_batch(database, "batch", prompt_text="first", prompt_version="v1", provider="test",
        model="test", extractor=lambda _request: {"events": [_candidate()]})
    with sqlite3.connect(database) as connection:
        _approve_all(connection)
    meetings.process_batch(database, "batch", prompt_text="second", prompt_version="v2", provider="test",
        model="test", extractor=lambda _request: (_ for _ in ()).throw(RuntimeError("provider unavailable")))

    reader = _reader(database, tmp_path)
    seen_connections = []
    original = meetings._meeting_status_selection

    def same_transaction(connection, **kwargs):
        assert connection is reader._snapshot_connection
        seen_connections.append(connection)
        return original(connection, **kwargs)

    monkeypatch.setattr(meetings, "_meeting_status_selection", same_transaction)
    direct = reader.meetings(base_date="2027-01-01")
    assert len(seen_connections) == 1
    monkeypatch.setattr(meetings, "_meeting_status_selection", original)
    with reader.public_snapshot():
        projected = reader.meetings(base_date="2027-01-01")
    assert direct["items"] == projected["items"]
    assert direct["coverage"]["status"] == projected["coverage"]["status"] == "partial"


def test_absorbed_pdf_detail_uses_the_approved_core_candidate(tmp_path):
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        _pdf(connection, identity="pdf-first", url="https://example.org/later")
        _approve_all(connection)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE articles SET canonical_url='https://example.org/later' WHERE article_id='a'")
        connection.execute("UPDATE pdf_intake_articles SET title='New approved PDF title' WHERE article_id='pdf-first'")
        stage_entities(connection)
        candidate = connection.execute(
            "SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='a'"
        ).fetchone()[0]
        _approve(connection, candidate, {"basis": "approved core absorption"}, status="accepted_legacy")

    reader = _reader(database, tmp_path)
    direct = reader.pdf_article("pdf-first")
    with reader.public_snapshot():
        projected = reader.pdf_article("pdf-first")
    collection = next(item for item in reader.pdf_articles_all(include_linked=True)
        if item["canonical_url"] == "https://example.org/later")
    assert direct["title"] == projected["title"] == collection["title"] == "New approved PDF title"


def test_report_directory_keeps_history_but_counts_only_visible_approved_appearances(tmp_path):
    database, source, reference_id = _report_database(tmp_path)
    set_visibility(database, "article", reference_id, False)
    reader = RegistryReader(database, repository_root=tmp_path / "application", source_dir=source)
    report = reader.reports()["items"][0]
    detail = reader.report("2026-09-07")
    assert report["report_date"] == "2026-09-07"
    assert report["article_count"] == len(detail["articles"]) == 1


def test_pdf_wiki_index_selects_page_names_without_rendering_bodies(tmp_path, monkeypatch):
    import climate_registry.wiki as wiki

    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        _pdf(connection, identity="pdf-source", url="https://pdf.example/article")
        _approve_all(connection)

    def fail_body_renderer(_items):
        pytest.fail("Wiki index rendered PDF observation Markdown bodies")

    monkeypatch.setattr(wiki, "_registry_source_observation_pages", fail_body_renderer)
    index = public_wiki_pages(database, page="index.md")["index.md"]
    assert "registry-source-observation-occurrence.md" in index
    assert "registry-source-observations.md" in index


def test_installed_static_payload_content_is_listed_in_wiki_index(tmp_path):
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        content_hash = "a" * 64
        connection.execute(
            "INSERT INTO article_content_versions(content_version_id,article_id,content_sha256,markdown_content,"
            "markdown_sha256,content_type,source_bytes,extraction_method,extraction_version,first_fetched_at) "
            "VALUES('body-a','a',?,'# Article body',?,'text/markdown',14,'fixture','v1','2026-09-01')",
            (content_hash, content_hash),
        )
        connection.execute("UPDATE articles SET current_content_version_id='body-a' WHERE article_id='a'")
        _approve_all(connection)
    application = tmp_path / "application"
    wiki = application / "wiki"
    wiki.mkdir(parents=True)
    export_public_snapshot(database, wiki / "public-registry.json")
    installed = tmp_path / "installed.sqlite3"
    with sqlite3.connect(installed) as connection:
        apply_migrations(connection)
    install_git_snapshot(installed, wiki)

    reader = RegistryReader(installed, repository_root=application)
    with reader.public_snapshot(direct=True):
        from climate_registry.wiki import render_registry_page
        target = render_registry_page(installed, "article-a.md", reader=reader)
        index = render_registry_page(installed, "index.md", reader=reader)
    assert target and "Article body" in target
    assert "article-a.md" in index


def test_dated_wiki_uses_the_same_publication_eligibility_as_bulk(tmp_path):
    import climate_registry.wiki as wiki

    database, source, reference_id = _report_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE articles SET publication_eligible=0,document_kind='landing_page' WHERE article_id=?",
            (reference_id,))
        _approve_all(connection, source_dir=source)
    reader = RegistryReader(database, repository_root=tmp_path / "application", source_dir=source)
    with reader.public_snapshot(direct=True):
        direct = wiki.render_registry_page(database, "climate-monitor-2026-09-07.md", reader=reader)
    bulk = public_wiki_pages(database)["climate-monitor-2026-09-07.md"]
    assert "Reference record" not in direct and "Reference record" not in bulk
    assert "Real climate article" in direct and "Real climate article" in bulk


def test_core_publishers_keep_distinct_historical_labels_without_pdf(tmp_path):
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        candidate = stage_snapshot(connection, snapshot_entity(connection, "article", "a"))
        _approve(connection, candidate, {"basis": "first publisher version"}, status="accepted_legacy")
        connection.execute("UPDATE sources SET display_name='New publisher' WHERE source_id='s'")
        candidate = stage_snapshot(connection, snapshot_entity(connection, "article", "b"))
        _approve(connection, candidate, {"basis": "second publisher version"}, status="accepted_legacy")

    result = _reader(database, tmp_path).publishers()
    assert result == {"items": [
        {"hostname": "example.org", "label": "New publisher"},
        {"hostname": "example.org", "label": "Publisher"},
    ], "total": 2, "truncated": False}


def test_incomplete_source_report_bundle_keeps_report_summary_but_not_search_semantics(tmp_path, monkeypatch):
    import api_server
    from fastapi.testclient import TestClient

    database, source, article_id = _report_database(tmp_path)
    reader = RegistryReader(database, repository_root=tmp_path / "application", source_dir=source)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    with reader.public_snapshot(direct=True):
        cards = reader.articles()["items"]
        card = next(item for item in cards if item["article_id"] == article_id)
        detail = reader.article(article_id)
        filtered = reader.articles(query="Reference observed summary")
    assert card["summary"] is detail["summary"] is None
    assert card["report_summary"] == detail["report_summary"] == "Reference observed summary."
    assert card.get("metadata_provenance") == {"categories": None, "keywords": None}
    response = TestClient(api_server.app).get("/api/registry/articles", params={"query": "Reference observed summary"})
    assert response.status_code == 200
    assert response.json()["pagination"]["total"] == filtered["pagination"]["total"] == 0


def test_pdf_approved_occurrences_keep_legacy_latest_order_in_http_search(tmp_path, monkeypatch):
    import api_server
    from fastapi.testclient import TestClient

    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        _pdf(connection, identity="pdf-one", url="https://pdf.example/one",
            occurrence={"summary": "Earlier approved observation", "summary_basis": "verbatim_pdf_article_row"})
        connection.execute("""INSERT INTO pdf_intake_article_occurrences(occurrence_id,article_id,source_document_sha256,page,
            raw_url,report_date,content_sha256,page_sha256,occurrence_json)
            VALUES('newer-occ','pdf-one',? ,3,'https://pdf.example/one','2026-09-01',?,?,?)""",
            ("f" * 64, "e" * 64, "c" * 64,
             json.dumps({"summary": "Newest approved observation", "summary_basis": "verbatim_pdf_article_row"})))
        _approve_all(connection)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(database, tmp_path))
    response = TestClient(api_server.app).get("/api/registry/articles", params={
        "include_pdf": "true", "query": "Newest approved observation"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["pagination"]["total"] == 1
    assert payload["items"][0]["article_id"] == "pdf-one"
    assert "summary" not in payload["items"][0] and "latest_occurrence" not in payload["items"][0]
    client = TestClient(api_server.app)
    categories = client.get("/api/registry/article-categories").json()["items"]
    item = next(item for item in categories if item["article_id"] == "pdf-one")
    assert "summary" not in item and "latest_occurrence" not in item
    detail = client.get("/api/registry/pdf-intake/articles/pdf-one").json()
    assert detail["occurrences"][0]["occurrence_id"] == "newer-occ"
    assert detail["published_candidate_sha256"] == item["published_candidate_sha256"]


def test_include_pdf_publishers_preserve_sorted_core_label_overwrite(tmp_path, monkeypatch):
    import api_server
    from fastapi.testclient import TestClient

    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE sources SET display_name='Alpha Historical' WHERE source_id='s'")
        candidate = stage_snapshot(connection, snapshot_entity(connection, "article", "a"))
        _approve(connection, candidate, {"basis": "historical publisher"}, status="accepted_legacy")
        connection.execute("UPDATE sources SET display_name='Zulu Current' WHERE source_id='s'")
        candidate = stage_snapshot(connection, snapshot_entity(connection, "article", "b"))
        _approve(connection, candidate, {"basis": "later publisher"}, status="accepted_legacy")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(database, tmp_path))
    response = TestClient(api_server.app).get("/api/registry/publishers", params={"include_pdf": "true"})
    assert response.status_code == 200
    assert response.json() == {"items": [{"hostname": "example.org", "label": "Zulu Current"}],
        "total": 1, "truncated": False}


def test_duplicate_pdf_observation_target_keeps_first_identity_for_get_and_head(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api_server import WikiStaticFiles

    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        _pdf(connection, identity="pdf-one", url="https://pdf.example/one",
            occurrence={"summary": "Repeated passage", "summary_basis": "verbatim_pdf_article_row"})
        connection.execute("""INSERT INTO pdf_intake_article_occurrences SELECT 'z-duplicate',article_id,source_document_sha256,
            page,raw_url,report_date,publication_date,content_sha256,page_sha256,occurrence_json
            FROM pdf_intake_article_occurrences WHERE occurrence_id='occurrence'""")
        _approve_all(connection)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT", raising=False)
    pages = public_wiki_pages(database)
    assert "registry-source-observation-occurrence.md" in pages
    assert "registry-source-observation-z-duplicate.md" not in pages
    disk = tmp_path / "wiki"
    disk.mkdir()
    app = FastAPI()
    app.mount("/wiki", WikiStaticFiles(directory=disk), name="wiki")
    with TestClient(app) as client:
        for method in (client.get, client.head):
            assert method("/wiki/registry-source-observation-occurrence.md").status_code == 200
            assert method("/wiki/registry-source-observation-z-duplicate.md").status_code == 404


def test_git_wiki_only_body_is_listed_without_rendering_it_for_index(tmp_path):
    import climate_registry.wiki as wiki

    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE article_versions SET observed_title='',observed_summary=''")
    application = tmp_path / "application"
    pages = application / "wiki"
    pages.mkdir(parents=True)
    body = "# Approved legacy body\n\nOnly installed Git Wiki content is available.\n"
    (pages / "article-a.md").write_text(body, encoding="utf-8")
    install_git_snapshot(database, pages)
    reader = RegistryReader(database, repository_root=application)
    with reader.public_snapshot(direct=True):
        index = wiki.render_registry_page(database, "index.md", reader=reader)
        target = wiki.render_registry_page(database, "article-a.md", reader=reader)
    assert "article-a.md" in index
    assert "Only installed Git Wiki content is available." not in index
    assert target and "Only installed Git Wiki content is available." in target


def test_evidence_turn_meeting_pages_reuse_one_merged_collection_and_keep_scope(tmp_path, monkeypatch):
    import climate_monitor.meetings as meeting_module
    from agentic_wiki.chat_evidence import EvidenceTurn
    from climate_registry.read_api import RegistryReader

    from test_issue136_meetings import _database as meeting_database
    database = meeting_database(tmp_path, [])
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    records = [{"event_id": f"native-{index:03d}", "name": f"Native climate meeting {index:03d}",
        "start_date": "2027-06-10", "end_date": "2027-06-10", "deadline_date": None,
        "status": "scheduled", "relevance_reason": "Climate risk", "verification_status": "verified"}
        for index in range(229)]
    selection_calls = []
    def approved(self, *, base_date, timezone_name, include_unknown):
        selection_calls.append((base_date, timezone_name, include_unknown))
        return records, {"status": "processed", "records_scope": "approved_versions", "returned_record_count": 229}
    monkeypatch.setattr(RegistryReader, "_approved_meeting_records", approved)
    monkeypatch.setattr(reader, "pdf_calendar_items_all", lambda **_kwargs: [])
    monkeypatch.setattr(reader, "resolve_pdf_meeting_identities", lambda items: items)
    status_calls = []
    monkeypatch.setattr(meeting_module, "_meeting_status_selection",
        lambda connection, **_kwargs: (status_calls.append(connection) or ("complete", [], [])))

    from test_chat_evidence import owner as chat_owner
    chat = chat_owner(tmp_path)
    chat.reader_factory = lambda: reader
    turn = EvidenceTurn(chat, "Show upcoming climate meetings", None, [])
    meetings, _chronology = turn.meetings("2027-01-01")
    assert len(meetings) == len({item["event_id"] for item in meetings}) == 229
    assert selection_calls == [("2027-01-01", "America/New_York", True)]
    assert len(status_calls) == 1
    assert any('"returned_record_count": 229' in note for note in turn.notes)

    injected = []
    chat.meeting_view = lambda _reader, *, page, base_date: (injected.append((page, base_date)) or {
        "items": [{"event_id": f"injected-{page}", "name": "Injected", "start_date": "2027-06-10"}],
        "pagination": {"pages": 2}, "coverage": {"status": "partial"}})
    next_turn = EvidenceTurn(chat, "Show upcoming climate meetings", None, [])
    injected_items, _ = next_turn.meetings("2027-01-01")
    assert injected == [(1, "2027-01-01"), (2, "2027-01-01")]
    assert [item["event_id"] for item in injected_items] == ["injected-1", "injected-2"]


def test_recent_pdf_evidence_verifies_one_packet_per_direct_request(tmp_path, monkeypatch):
    import climate_registry.information_checks as checks
    from datetime import datetime, timezone
    from agentic_wiki.chat_evidence import EvidenceTurn

    database = _database(tmp_path)
    occurrence = {"title": "Climate evidence article", "summary": "Recent climate evidence for insurance risk.",
        "summary_basis": "verbatim_pdf_article_row", "raw_url": "https://pdf.example/recent"}
    with sqlite3.connect(database) as connection:
        _pdf(connection, identity="pdf-recent", url="https://pdf.example/recent", occurrence=occurrence)
        connection.execute("INSERT INTO knowledge_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("knowledge-pdf", "article", "pdf-recent", "pdf", "pdf:recent", "b" * 64, "{}", "{}",
             "2026-09-30T12:00:00Z", "2026-09-30T12:00:00Z", "2026-09-30T12:00:00Z", "approved_pdf"))
        raw = connection.execute("SELECT * FROM pdf_intake_article_occurrences WHERE occurrence_id='occurrence'").fetchone()
        row = dict(zip([column[0] for column in connection.execute("SELECT * FROM pdf_intake_article_occurrences WHERE 0").description], raw))
        item = {**json.loads(row["occurrence_json"]), "occurrence_id": row["occurrence_id"],
            "source_document_sha256": row["source_document_sha256"]}
        for field in ("page", "raw_url", "publication_date", "content_sha256", "report_date"):
            item[field] = row.get(field)
        revision = checks.source_revision("articles", item)
        packet = {"issue221_packet_marker": "recent-pdf-check", "checked_at": "2026-09-30T12:00:00Z",
            "access_status": "accessible", "verification_status": "verified",
            "website_candidate": {"title": occurrence["title"], "summary": occurrence["summary"]},
            "reader": {"content_hash": "c" * 64, "final_url": "https://pdf.example/recent"},
            "verified_information": {"summary": occurrence["summary"]}}
        packet_json = json.dumps(packet, sort_keys=True, separators=(",", ":"))
        packet_sha = checks._sha(packet)
        connection.execute("INSERT INTO article_check_runs VALUES('run-one','{}',?,'2026-09-30T12:00:00Z',?,'complete',1,1,NULL)",
            (__import__("hashlib").sha256(b"{}").hexdigest(), "2026-09-30T12:00:00Z"))
        connection.execute("""INSERT INTO article_check_attempts(attempt_id,run_id,occurrence_id,source_url,
            source_revision_sha256,checked_at,access_status,verification_status,packet_json,packet_sha256)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("attempt-one", "run-one", "occurrence", "https://pdf.example/recent", revision,
             "2026-09-30T12:00:00Z", "accessible", "verified", packet_json, packet_sha))
        _approve_all(connection)

    decode_count = hash_count = 0
    original_loads, original_sha = checks.json.loads, checks._sha
    def counted_loads(value, *args, **kwargs):
        nonlocal decode_count
        if value == packet_json:
            decode_count += 1
        return original_loads(value, *args, **kwargs)
    def counted_sha(value):
        nonlocal hash_count
        if isinstance(value, dict) and value.get("issue221_packet_marker") == "recent-pdf-check":
            hash_count += 1
        return original_sha(value)
    monkeypatch.setattr(checks.json, "loads", counted_loads)
    monkeypatch.setattr(checks, "_sha", counted_sha)
    reader = _reader(database, tmp_path)
    from test_chat_evidence import owner as chat_owner
    chat = chat_owner(tmp_path)
    chat.reader_factory = lambda: reader
    turn = EvidenceTurn(chat, "Summarize climate evidence articles added in the last 7 days.", None, [])
    turn.now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    result = turn.search_knowledge("Summarize climate evidence articles added last 7 days.", target="recent_articles")
    assert len(result) == 1 and result[0]["source"]["evidence_id"] == "pdf-recent"
    assert (decode_count, hash_count) == (1, 1)
    with reader.public_snapshot(direct=True):
        reader.pdf_articles_all()
        reader.pdf_article("pdf-recent")
    assert (decode_count, hash_count) == (2, 2), "packet verification is cached only within one public read request"


def test_dated_wiki_target_matches_bulk_source_modes_without_article_n_plus_one(tmp_path, monkeypatch):
    import climate_registry.wiki as wiki

    database, source, _article_id = _report_database(tmp_path)
    path = "climate-monitor-2026-09-07.md"
    no_source_bulk = public_wiki_pages(database)[path]
    source_bulk = public_wiki_pages(database, source_dir=source)[path]
    reader = RegistryReader(database, repository_root=tmp_path / "application", source_dir=source)
    original = RegistryReader.article
    def fail_detail_method(*_args, **_kwargs):
        pytest.fail("target report page must use the one batched approved snapshot selection")
    monkeypatch.setattr(RegistryReader, "article", fail_detail_method)
    with reader.public_snapshot(direct=True):
        direct_no_source = wiki.render_registry_page(database, path, reader=reader)
        direct_with_source = wiki.render_registry_page(database, path, reader=reader, source_dir=source)
    monkeypatch.setattr(RegistryReader, "article", original)
    assert direct_no_source == no_source_bulk
    assert direct_with_source == source_bulk
    assert "Approved Registry version: " in direct_with_source


def test_pdf_source_cache_stays_bound_to_each_approved_candidate(tmp_path):
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        _pdf(connection, identity="pdf-a", url="https://pdf.example/a")
        connection.execute("INSERT INTO pdf_intake_document_sources VALUES(?,?,?,?)",
            ("f" * 64, "/evidence/z-first.pdf", "Z first.pdf", "2026-08-01T00:00:00Z"))
        candidate = stage_snapshot(connection, snapshot_entity(connection, "pdf_article", "pdf-a"))
        _approve(connection, candidate, {"basis": "first frozen dependency"}, status="accepted_legacy")
        connection.execute("INSERT INTO pdf_intake_document_sources VALUES(?,?,?,?)",
            ("f" * 64, "/evidence/a-later.pdf", "A later.pdf", "2026-09-01T00:00:00Z"))
        connection.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) "
            "VALUES('pdf-b','https://pdf.example/b','PDF B','2026-09-01')")
        occurrence = {"summary": "Second observation", "summary_basis": "verbatim_pdf_article_row"}
        connection.execute("INSERT INTO pdf_intake_article_occurrences VALUES('occ-b','pdf-b',?,3,?,'2026-09-01',NULL,?,?,?)",
            ("f" * 64, "https://pdf.example/b", "d" * 64, "c" * 64, json.dumps(occurrence)))
        candidate = stage_snapshot(connection, snapshot_entity(connection, "pdf_article", "pdf-b"))
        _approve(connection, candidate, {"basis": "second frozen dependency"}, status="accepted_legacy")

    reader = _reader(database, tmp_path)
    listed = next(item for item in reader.pdf_articles_all() if item["article_id"] == "pdf-b")
    detail = reader.pdf_article("pdf-b")
    expected = ["A later.pdf", "Z first.pdf"]
    assert [item["filename"] for item in listed["occurrences"][0]["source_observations"]] == expected
    assert listed["occurrences"][0]["source_observations"] == detail["occurrences"][0]["source_observations"]
    from climate_registry.wiki import _citation
    assert _citation(listed["occurrences"][0]) == _citation(detail["occurrences"][0])


def test_report_summary_provenance_comes_from_approved_summary_field(tmp_path, monkeypatch):
    import api_server
    from climate_registry.acquisition_review import record_knowledge
    from fastapi.testclient import TestClient

    database, source, _reference_id = _report_database(tmp_path)
    with sqlite3.connect(database) as connection:
        identity, version = connection.execute(
            "SELECT article_id,current_version_id FROM articles WHERE canonical_url='https://example.org/article'"
        ).fetchone()
        evidence = {"pending_enrichment": {"title": "Approved title", "summary": "Approved summary",
            "categories": ["Climate Risk"], "keywords": ["insurance"], "language": "en"},
            "article_version_id": version, "source_row_sha256": "f" * 64,
            "field_sources": {"summary": "observed_summary"}}
        record_knowledge(connection, kind="article", entity_id=identity, source_kind="site",
            source_ref="registry-supplement:" + identity, fields={"summary": "Approved summary"},
            evidence=evidence, recorded_at="2026-09-08T00:00:00Z", time_basis="legacy_time_unknown")
        candidate = stage_snapshot(connection, snapshot_entity(connection, "article", identity, source_dir=source))
        _approve(connection, candidate, {"basis": "approved report supplement"}, status="accepted_legacy")

    reader = RegistryReader(database, repository_root=tmp_path / "application", source_dir=source)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    report_article = next(item for item in reader.report("2026-09-07")["articles"] if item["article_id"] == identity)
    detail = reader.article(identity)
    assert report_article["summary_provenance"] == detail["summary_provenance"] == "source_report"
    assert report_article["metadata_provenance"]["categories"] == "manual_supplement"
    response = TestClient(api_server.app).get("/api/registry/reports/2026-09-07")
    assert response.status_code == 200
    http_article = next(item for item in response.json()["articles"] if item["article_id"] == identity)
    assert http_article["summary_provenance"] == "source_report"


def test_linked_core_pdf_observation_has_no_target_wiki_page(tmp_path, monkeypatch):
    import inspect
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api_server import WikiStaticFiles

    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        from test_registry_publication import _pdf as pdf_fixture
        source = inspect.getsource(pdf_fixture).replace(
            "article_id,canonical_url,title,imported_at) VALUES(?,?, 'Original PDF title','2026-08-01T00:00:00Z')",
            "article_id,canonical_url,title,imported_at,core_article_id) "
            "VALUES(?,?, 'Original PDF title','2026-08-01T00:00:00Z','a')")
        namespace = dict(pdf_fixture.__globals__)
        exec(source, namespace)
        namespace["_pdf"](connection, identity="linked-pdf")
        _approve_all(connection)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT", raising=False)
    page = "registry-source-observation-occurrence.md"
    assert page not in public_wiki_pages(database)
    directory = tmp_path / "wiki"
    directory.mkdir()
    app = FastAPI()
    app.mount("/wiki", WikiStaticFiles(directory=directory), name="wiki")
    with TestClient(app) as client:
        assert client.get("/wiki/" + page).status_code == 404
        assert client.head("/wiki/" + page).status_code == 404


def test_native_meeting_sources_follow_legacy_url_and_version_order(tmp_path):
    import inspect
    from test_issue136_meetings import _candidate, _database as meeting_database
    from climate_monitor.meetings import process_batch

    body = "World Climate Summit 2027 meets June 10–12, 2027 in New York. Registration deadline May 1, 2027. Register https://example.com/register. Climate risk agenda."
    source = inspect.getsource(meeting_database).replace(
        'url = f"https://example.com/{ordinal}"',
        'url = "https://example.com/" + ("z-source" if ordinal == 1 else "a-source")')
    namespace = dict(meeting_database.__globals__)
    exec(source, namespace)
    database = namespace["_database"](tmp_path, [body, body])
    process_batch(database, "batch", prompt_text="event sources", prompt_version="v1", provider="test",
        model="test", extractor=lambda _request: {"events": [_candidate()]})
    with sqlite3.connect(database) as connection:
        _approve_all(connection)
    reader = _reader(database, tmp_path)
    direct = reader.meetings(base_date="2027-01-01")["items"]
    with reader.public_snapshot():
        projected = reader.meetings(base_date="2027-01-01")["items"]
    expected = ["https://example.com/a-source", "https://example.com/z-source"]
    assert [source["source_url"] for source in direct[0]["sources"]] == expected
    assert direct == projected


def test_include_pdf_status_counts_approved_url_union_without_detail_assembly(tmp_path, monkeypatch):
    import api_server
    from fastapi.testclient import TestClient

    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        _pdf(connection, identity="pdf-independent", url="https://pdf.example/independent")
        connection.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) "
            "VALUES('pdf-core-alias','https://example.org/a','Alias','2026-09-01')")
        connection.execute("INSERT INTO pdf_intake_article_occurrences VALUES('alias-occ','pdf-core-alias',?,3,?,'2026-09-01',NULL,?,?,?)",
            ("f" * 64, "https://example.org/a", "d" * 64, "c" * 64,
             json.dumps({"summary": "Alias", "summary_basis": "verbatim_pdf_article_row"})))
        _approve_all(connection)

    def unexpected(*_args, **_kwargs):
        pytest.fail("status count assembled the full PDF article list or detail")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    api_server._warm_registry_list_cache()
    monkeypatch.setattr(api_server, "_registry_article_page", unexpected)
    monkeypatch.setattr(RegistryReader, "pdf_articles_all", unexpected)
    monkeypatch.setattr(RegistryReader, "_pdf_article_from_snapshot", unexpected)
    response = TestClient(api_server.app).get("/api/registry/status?include_pdf=true")
    assert response.status_code == 200
    assert response.json()["articles"] == 3


def test_calendar_alias_and_check_reuse_one_verified_packet_per_request(tmp_path, monkeypatch):
    import hashlib
    import climate_registry.information_checks as checks
    from test_issue136_meetings import _candidate

    database = _database(tmp_path)
    item = {"event_id": "pdf-calendar", "occurrence_id": "calendar-occ", "name": "World Climate Summit 2027",
        "kind": "event", "raw_date": "June 10–12, 2027", "date_precision": "day", "start_date": "2027-06-10",
        "end_date": "2027-06-12", "summary": "Original PDF meeting", "page": 2,
        "source_urls": ["https://example.com/register"], "source_document_sha256": "f" * 64}
    packet = {"review3_packet_marker": "calendar-check", "checked_at": "2026-09-30T12:00:00Z",
        "access_status": "accessible", "verification_status": "verified", "website_candidate": _candidate(),
        "reader": {"content_hash": "c" * 64, "final_url": item["source_urls"][0]},
        "verified_information": {"summary": item["summary"]}}
    packet_json = json.dumps(packet, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(database) as connection:
        _pdf(connection, identity="pdf-independent", url="https://example.org/independent")
        connection.execute("INSERT INTO pdf_intake_calendar_items VALUES(?,?,?,2,?,'event',?,'day',?,?,?, ?,NULL,?)",
            (item["occurrence_id"], item["event_id"], "f" * 64, item["name"], item["raw_date"],
             item["start_date"], item["end_date"], item["summary"], "c" * 64, json.dumps(item)))
        connection.execute("INSERT INTO meeting_check_runs VALUES('check','{}',?,'2026-09-30','2026-09-30','complete',1,1,NULL)",
            (hashlib.sha256(b"{}").hexdigest(),))
        connection.execute("INSERT INTO meeting_check_attempts(attempt_id,run_id,occurrence_id,source_url,source_revision_sha256,checked_at,access_status,verification_status,packet_json,packet_sha256) VALUES('attempt','check',?,?,?,'2026-09-30T12:00:00Z','accessible','verified',?,?)",
            (item["occurrence_id"], item["source_urls"][0], checks.source_revision("meetings", item),
             packet_json, checks._sha(packet)))
        _approve_all(connection)

    calls = {"decode": 0, "hash": 0}
    original_loads, original_sha = checks.json.loads, checks._sha
    def counted_loads(value, *args, **kwargs):
        if value == packet_json:
            calls["decode"] += 1
        return original_loads(value, *args, **kwargs)
    def counted_sha(value):
        if isinstance(value, dict) and value.get("review3_packet_marker"):
            calls["hash"] += 1
        return original_sha(value)
    monkeypatch.setattr(checks.json, "loads", counted_loads)
    monkeypatch.setattr(checks, "_sha", counted_sha)
    calendar = _reader(database, tmp_path).pdf_calendar_items_all()
    assert len(calendar) == 1 and calendar[0]["verification_status"] == "verified"
    assert calls == {"decode": 1, "hash": 1}
    report = _reader(database, tmp_path).pdf_report("f" * 64)
    assert len(report["calendar_items"]) == 1
    assert calls == {"decode": 2, "hash": 2}, "the same packet is verified once in each independent request"


def test_historical_meeting_candidate_bulk_loads_missing_frozen_batch_metadata(tmp_path, monkeypatch):
    from climate_registry.publication import _approve, stage_snapshot
    from climate_registry.schema import apply_migrations

    fixture = _review4_legacy_candidates()
    snapshot = fixture["meeting_candidate"]
    assert "acquisition_batches" not in snapshot["dependencies"]
    database = tmp_path / "legacy-meetings.sqlite3"
    with sqlite3.connect(database) as connection:
        apply_migrations(connection)
        _insert_fixture_rows(connection, "acquisition_batches", fixture["meeting_batch_rows"])
        candidate = stage_snapshot(connection, snapshot)
        assert candidate == fixture["meeting_candidate_sha256"]
        _approve(connection, candidate, {"basis": "immutable pre-dependency candidate"}, status="accepted_legacy")

    reader = RegistryReader(database, repository_root=tmp_path / "application")
    statements = []
    original_source = reader._source_connection

    @contextmanager
    def traced_source(**kwargs):
        with original_source(**kwargs) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(reader, "_source_connection", traced_source)
    with reader.public_snapshot(direct=True):
        records, _coverage = reader._approved_meeting_records(
            base_date="2027-01-01", timezone_name="America/New_York", include_unknown=True)

    sources = records[0]["sources"]
    assert [source["is_current"] for source in sources] == [0, 1]
    assert [source["collected_at"] for source in sources] == ["2026-09-07T08:00:00Z", "2026-09-14T08:00:00Z"]
    batch_reads = [statement for statement in statements
        if "FROM acquisition_batches WHERE batch_id IN" in statement]
    assert len(batch_reads) == 1, "historic run batch metadata is fetched in one bounded lookup"


def test_historical_article_candidate_restores_search_origin_in_one_lookup(tmp_path, monkeypatch):
    from climate_registry.publication import _approve, stage_snapshot
    from climate_registry.schema import apply_migrations

    fixture = _review4_legacy_candidates()
    snapshot = fixture["article_candidate"]
    assert "acquisition_searches" not in snapshot["dependencies"]
    database = tmp_path / "legacy-article.sqlite3"
    with sqlite3.connect(database) as connection:
        apply_migrations(connection)
        _insert_fixture_rows(connection, "acquisition_batches", fixture["article_batch_rows"])
        _insert_fixture_rows(connection, "acquisition_searches", fixture["article_search_rows"])
        candidate = stage_snapshot(connection, snapshot)
        assert candidate == fixture["article_candidate_sha256"]
        _approve(connection, candidate, {"basis": "immutable pre-dependency candidate"}, status="accepted_legacy")

    reader = RegistryReader(database, repository_root=tmp_path / "application")
    statements = []
    original_source = reader._source_connection

    @contextmanager
    def traced_source(**kwargs):
        with original_source(**kwargs) as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(reader, "_source_connection", traced_source)
    detail = reader.article(snapshot["entity_id"])
    origin = detail["acquisition_observations"][0]["origins"][0]
    assert origin["search"] == {key: fixture["article_search_rows"][0][key]
        for key in ("batch_id", "search_ref", "search_id", "query", "engine", "status", "attempted_at")}
    search_reads = [statement for statement in statements
        if "FROM acquisition_searches WHERE" in statement and "search_ref" in statement]
    assert len(search_reads) == 1, "historic search provenance is fetched by batch/search reference in one lookup"


def test_installed_static_payload_priority_is_shared_by_index_and_target(tmp_path, monkeypatch):
    from climate_registry.publication import export_public_snapshot, install_git_snapshot

    source_root = tmp_path / "source-app"
    source_root.mkdir()
    source_database = _database(source_root)
    source_wiki = source_root / "wiki"
    source_wiki.mkdir()
    with sqlite3.connect(source_database) as connection:
        connection.execute(
            "INSERT INTO article_content_versions(content_version_id,article_id,content_sha256,markdown_content,"
            "markdown_sha256,content_type,source_bytes,extraction_method,extraction_version,first_fetched_at) "
            "VALUES('body-a','a',?,'# Approved body',?,'text/markdown',14,'fixture','v1','2026-09-01')",
            ("a" * 64, "a" * 64))
        connection.execute("UPDATE articles SET current_content_version_id='body-a' WHERE article_id='a'")
        _approve_all(connection)
    export_public_snapshot(source_database, source_wiki / "public-registry.json")

    application = tmp_path / "installed-app"
    wiki = application / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "public-registry.json").write_bytes((source_wiki / "public-registry.json").read_bytes())
    (tmp_path / "installed").mkdir()
    installed = _database(tmp_path / "installed")
    with sqlite3.connect(installed) as connection:
        connection.execute("UPDATE articles SET document_kind='landing_page',publication_eligible=0 WHERE article_id='a'")
    install_git_snapshot(installed, wiki)
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT", raising=False)

    reader = RegistryReader(installed, repository_root=application)
    with reader.public_snapshot(direct=True):
        from climate_registry.wiki import render_registry_page
        target = render_registry_page(installed, "article-a.md", reader=reader)
        monkeypatch.setattr("climate_registry.wiki._render_registry_article",
            lambda _article: pytest.fail("index page names must not render article bodies"))
        index = render_registry_page(installed, "index.md", reader=reader)
    assert target and "Approved body" in target
    assert "article-a.md" in index


def test_static_publishers_keep_historical_labels_without_pdf_and_merge_with_pdf(tmp_path, monkeypatch):
    from climate_registry.publication import export_public_snapshot, install_git_snapshot

    source_root = tmp_path / "static-source"
    source_root.mkdir()
    database = _database(source_root)
    application = source_root / "application"
    wiki = application / "wiki"
    wiki.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        for identity, label in (("a", "Alpha Historical"), ("b", "Zulu Current")):
            connection.execute("UPDATE sources SET display_name=? WHERE source_id='s'", (label,))
            candidate = stage_snapshot(connection, snapshot_entity(connection, "article", identity))
            _approve(connection, candidate, {"basis": "historical publisher name"}, status="accepted_legacy")
        _pdf(connection, identity="pdf-other", url="https://other.example/article")
        candidate = stage_snapshot(connection, snapshot_entity(connection, "pdf_article", "pdf-other"))
        _approve(connection, candidate, {"basis": "approved PDF publisher"}, status="accepted_legacy")
    export_public_snapshot(database, wiki / "public-registry.json")

    installed_root = tmp_path / "static-installed"
    installed_root.mkdir()
    installed = installed_root / "registry.sqlite3"
    with sqlite3.connect(installed) as connection:
        apply_migrations(connection)
    install_git_snapshot(installed, wiki)
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT", "1")
    reader = RegistryReader(installed, repository_root=application)
    monkeypatch.setattr(reader, "article", lambda *_args, **_kwargs: pytest.fail("publishers lookup called article detail"))

    assert reader.publishers() == {"items": [
        {"hostname": "example.org", "label": "Alpha Historical"},
        {"hostname": "example.org", "label": "Zulu Current"}], "total": 2, "truncated": False}
    merged = reader.publishers(include_pdf=True)
    assert merged["items"] == [
        {"hostname": "example.org", "label": "Zulu Current"},
        {"hostname": "other.example", "label": "other"}]
    assert merged["total"] == 2 and merged["truncated"] is False


def test_registry_index_uses_approved_annotation_display_and_pdf_summaries(tmp_path, monkeypatch):
    import climate_registry.wiki as wiki
    from climate_registry.annotations import ArticleAnnotation

    for scenario in ("annotation", "derived", "pdf"):
        directory = tmp_path / scenario
        directory.mkdir()
        database = _database(directory)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE article_versions SET observed_summary='' WHERE article_id='a'")
            if scenario == "pdf":
                _pdf(connection, identity="pdf-a", url="https://example.org/a")
            if scenario == "annotation":
                url = "https://example.org/a"
                annotation = ArticleAnnotation(url, url, "Approved title", "original_content",
                    "Approved annotation summary", ("Climate Risk",), ("insurance",), "2026-09-01")
                _approve_all(connection, annotations={url: annotation})
            else:
                _approve_all(connection)
            if scenario == "derived":
                snapshot = snapshot_entity(connection, "article", "a")
                snapshot["derived_display"] = {"summary": "Approved derived summary"}
                _approve(connection, stage_snapshot(connection, snapshot),
                    {"basis": "approved derived display"}, status="accepted_legacy")

        reader = RegistryReader(database, repository_root=directory / "application")
        monkeypatch.setattr(RegistryReader, "article", lambda *_args, **_kwargs:
            pytest.fail("Wiki index must not assemble article detail"))
        monkeypatch.setattr(wiki, "_render_registry_article", lambda *_args, **_kwargs:
            pytest.fail("Wiki index must not render article bodies"))
        with reader.public_snapshot(direct=True):
            index = wiki.render_registry_page(database, "index.md", reader=reader)
        assert "article-a.md" in index, f"{scenario} summary must make the approved target available"


def test_unlinked_same_url_pdf_remains_in_collection_and_wiki_sources(tmp_path, monkeypatch):
    import api_server
    from api_server import WikiStaticFiles
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        _pdf(connection, identity="pdf-a", url="https://example.org/a")
        _approve_all(connection)
    reader = _reader(database, tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT", raising=False)
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)

    collection = reader.pdf_articles_all(include_linked=False)
    assert [item["article_id"] for item in collection] == ["pdf-a"]
    pages = public_wiki_pages(database)
    assert "registry-source-observation-occurrence.md" in pages
    assert "registry-source-observations.md" in pages

    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    app = FastAPI()
    app.mount("/wiki", WikiStaticFiles(directory=str(wiki_dir)), name="wiki")
    with TestClient(app) as client:
        for path in ("/wiki/registry-source-observation-occurrence.md", "/wiki/registry-source-observations.md"):
            assert client.get(path).status_code == 200
            assert client.head(path).status_code == 200

    response = TestClient(api_server.app).get("/api/registry/pdf-intake/articles")
    assert response.status_code == 200
    assert response.json()["pagination"]["total"] == 1
    assert [item["article_id"] for item in response.json()["items"]] == ["pdf-a"]


def test_article_category_collection_carries_approved_content_version_reference(tmp_path):
    from climate_registry.publication import export_public_snapshot, install_git_snapshot

    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        content_hash = "a" * 64
        connection.execute(
            "INSERT INTO article_content_versions(content_version_id,article_id,content_sha256,markdown_content,"
            "markdown_sha256,content_type,source_bytes,extraction_method,extraction_version,first_fetched_at) "
            "VALUES('content-a','a',?,'Approved body',?,'text/markdown',13,'fixture','v1','2026-09-01')",
            (content_hash, content_hash))
        connection.execute("UPDATE articles SET current_content_version_id='content-a' WHERE article_id='a'")
        connection.execute(
            "INSERT INTO article_enrichments(enrichment_id,article_id,content_version_id,status,summary,"
            "categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at) "
            "VALUES('enrichment-a','a','content-a','complete','Approved summary','[]','[\"Insurance\"]',"
            "'en','model','fixture','v1','2026-09-01T01:00:00Z')")
        _approve_all(connection)

    direct_reader = _reader(database, tmp_path)
    with direct_reader.public_snapshot(direct=True):
        direct = next(item for item in direct_reader.articles()["items"] if item["article_id"] == "a")
    assert direct["content_version_id"] == "content-a"

    application = tmp_path / "static-application"
    wiki = application / "wiki"
    wiki.mkdir(parents=True)
    export_public_snapshot(database, wiki / "public-registry.json")
    installed_root = tmp_path / "static-installed"
    installed_root.mkdir()
    installed = installed_root / "registry.sqlite3"
    with sqlite3.connect(installed) as connection:
        apply_migrations(connection)
    install_git_snapshot(installed, wiki)
    static_reader = RegistryReader(installed, repository_root=application)
    with static_reader.public_snapshot(direct=True):
        static = next(item for item in static_reader.articles()["items"] if item["article_id"] == "a")
    assert static["content_version_id"] == direct["content_version_id"]


def test_article_category_collection_uses_frozen_content_fallback_without_enrichment(tmp_path):
    from climate_registry.annotations import ArticleAnnotation

    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        content_hash = "b" * 64
        connection.execute(
            "INSERT INTO article_content_versions(content_version_id,article_id,content_sha256,markdown_content,"
            "markdown_sha256,content_type,source_bytes,extraction_method,extraction_version,first_fetched_at) "
            "VALUES('content-a','a',?,'Approved body',?,'text/markdown',13,'fixture','v1','2026-09-01')",
            (content_hash, content_hash))
        connection.execute("UPDATE articles SET current_content_version_id='content-a' WHERE article_id='a'")
        url = "https://example.org/a"
        annotation = ArticleAnnotation(url, url, "Approved source title", "original_content",
            "Approved annotation summary", ("Climate Risk",), ("Insurance",), "2026-09-01")
        _approve_all(connection, annotations={url: annotation})

    reader = _reader(database, tmp_path)
    with reader.public_snapshot(direct=True):
        card = next(item for item in reader.articles()["items"] if item["article_id"] == "a")
        detail = reader.article("a")
        empty_card = next(item for item in reader.articles()["items"] if item["article_id"] == "b")
    assert card["content_version_id"] == detail["content"]["content_version_id"] == "content-a"
    assert card["keywords"] == ["Insurance"]
    assert card["metadata_provenance"]["keywords"] == "original_content_annotation"
    assert detail["enrichment"]["generator"] is None
    assert empty_card["content_version_id"] is None
