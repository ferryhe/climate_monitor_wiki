import json
import sqlite3

import pytest

from climate_registry.list_projection import ARTICLE_FIELDS, RegistryListProjection
from climate_registry.publication import _approve, stage_entities
from climate_registry.read_api import RegistryReader
from test_registry_publication import _database


def test_sql_projection_filters_core_and_pdf_independently_and_returns_only_cards():
    class Reader:
        def _public_candidates(self, *kinds):
            assert set(kinds) == {"article", "pdf_article", "meeting", "pdf_meeting"}
        def approved_article_cards_all(self):
            return [{"article_id": "core", "canonical_url": "https://example.org/a", "title": "Straße",
                "summary": "core summary", "categories": ["Flood"], "keywords": ["tail risk"],
                "source": "example.org", "publisher": "Publisher", "source_id": "source-1",
                "last_seen": "2026-09-02", "appearances": [
                    {"report_date": "2026-09-01", "pillar": "A"},
                    {"report_date": "2026-09-02", "pillar": "B"}]}]
        def pdf_articles_all(self, **kwargs):
            return [{"article_id": "pdf", "canonical_url": "https://example.org/a", "title": "PDF alias",
                "summary": "PDF only hit", "source": "example.org", "publisher": "example.org",
                "last_seen": "2026-09-03", "source_kind": "pdf", "occurrences": [{"report_date": "2026-09-03"}]}]
        def approved_reports_all(self): return []
        def pdf_reports_all(self): return []
        def status(self): return {"available": True}
        def approved_merged_article_count(self): return 1
        def publishers(self, **kwargs): return {"items": [], "total": 0, "truncated": False}

    projection = RegistryListProjection(Reader())
    statements = []
    projection.connection.set_trace_callback(statements.append)
    try:
        def page(**filters):
            return projection.articles(page=1, page_size=20, include_pdf=True, **filters)
        for query in ("STRASSE", "tail risk", "Flood", "PDF only hit"):
            result = page(query=query)
            assert result["pagination"]["total"] == 1
            assert result["items"][0]["article_id"] == "core"
            assert set(result["items"][0]) <= ARTICLE_FIELDS
        for source in ("source-1", "Publisher", "example.org", "WWW.example.org"):
            assert page(source=source)["pagination"]["total"] == 1
        assert page(query="PDF only hit", pillar="A")["items"] == []
        assert page(report_date="2026-09-03")["items"][0]["article_id"] == "core"
        assert page(report_date="2026-09-01", pillar="B")["items"] == []
        assert page(report_date="2026-09-02", pillar="B")["pagination"]["total"] == 1
        assert any("COUNT(*)" in sql for sql in statements)
        assert any("LIMIT 20 OFFSET 0" in sql for sql in statements)
        keywords = json.loads(projection.keyword_body)["items"][0]
        assert keywords["keywords"] == ["tail risk"] and keywords["summary"] == "core summary"
    finally:
        projection.close()


def test_shared_lists_do_not_revalidate_or_decode_stable_source(tmp_path, monkeypatch):
    import api_server
    import climate_registry.publication as publication
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):
            _approve(db, sha, {"basis": "list projection fixture"}, status="accepted_legacy")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    api_server._clear_article_keyword_cache()
    api_server._warm_article_keyword_cache()
    assert api_server._ARTICLE_KEYWORDS_CACHE is not None
    def unexpected(*args, **kwargs): raise AssertionError("stable list reloaded source snapshots or full contract")
    monkeypatch.setattr(publication, "published_snapshots", unexpected)
    monkeypatch.setattr(RegistryReader, "_validate_contract", unexpected)
    assert len(api_server.registry_articles(page="1", page_size="1", include_pdf=True)["items"]) == 1
    assert api_server.registry_reports(include_pdf=True)["pagination"]["total"] == 0
    assert api_server.registry_status(include_pdf=True)["articles"] == 2
    assert api_server.registry_publishers(include_pdf=True)["items"]
    assert len(json.loads(api_server.registry_article_keywords().body)["items"]) == 2
    api_server._clear_article_keyword_cache()


@pytest.mark.parametrize("damage", ["deleted", "schema", "approval", "binding"])
def test_cached_lists_fail_closed_when_source_is_invalid(tmp_path, monkeypatch, damage):
    import api_server
    from fastapi import HTTPException
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):
            _approve(db, sha, {"basis": "list invalidation fixture"}, status="accepted_legacy")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    api_server._clear_article_keyword_cache()
    assert api_server.registry_articles(include_pdf=True)["pagination"]["total"] == 2
    if damage == "deleted":
        database.unlink()
    else:
        with sqlite3.connect(database) as db:
            if damage == "schema": db.execute("PRAGMA user_version=999")
            elif damage == "approval":
                db.execute("DROP TRIGGER registry_reviews_immutable_delete")
                db.execute("DELETE FROM registry_reviews")
            else:
                db.execute("DROP TRIGGER registry_publication_requires_approval_update")
                db.execute("UPDATE registry_publication SET published_candidate_sha256='broken'")
    with pytest.raises(HTTPException) as error:
        api_server.registry_articles(include_pdf=True)
    assert error.value.status_code == 503
    assert error.value.detail == "Article registry is unavailable."
    assert api_server._ARTICLE_KEYWORDS_CACHE is None


def test_list_generation_never_publishes_uncommitted_wal_visibility(tmp_path, monkeypatch):
    import api_server
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):
            _approve(db, sha, {"basis": "list WAL fixture"}, status="accepted_legacy")
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    api_server._clear_article_keyword_cache()
    assert api_server.registry_articles(include_pdf=True)["pagination"]["total"] == 2
    writer = sqlite3.connect(database)
    try:
        writer.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_id='a'")
        assert api_server.registry_articles(include_pdf=True)["pagination"]["total"] == 2
        writer.rollback()
        assert api_server.registry_articles(include_pdf=True)["pagination"]["total"] == 2
        writer.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_id='a'")
        writer.commit()
        committed = api_server.registry_articles(include_pdf=True)
        assert committed["pagination"]["total"] == 1
        status = api_server.registry_status(include_pdf=True)
        assert status["articles"] == 1 and status["cache_version"] == committed["cache_version"]
    finally:
        writer.close()
        api_server._clear_article_keyword_cache()


@pytest.mark.parametrize("change", ["delete", "replace_invalid"])
def test_source_removed_or_replaced_during_build_cannot_return_old_cards(tmp_path, monkeypatch, change):
    import os
    import api_server
    from fastapi import HTTPException
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):
            _approve(db, sha, {"basis": "list replacement race fixture"}, status="accepted_legacy")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    api_server._clear_article_keyword_cache()
    original = api_server.RegistryListProjection
    def replace_after_build(reader):
        result = original(reader)
        if change == "delete": database.unlink()
        else:
            invalid = tmp_path / "invalid.sqlite3"
            invalid.write_bytes(b"not a Registry")
            os.replace(invalid, database)
        return result
    monkeypatch.setattr(api_server, "RegistryListProjection", replace_after_build)
    with pytest.raises(HTTPException) as error:
        api_server.registry_articles(include_pdf=True)
    assert error.value.status_code == 503
    assert api_server._ARTICLE_KEYWORDS_CACHE is None


def test_continuously_changing_source_cannot_publish_or_cache_a_generation(tmp_path, monkeypatch):
    import api_server
    from fastapi import HTTPException
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):
            _approve(db, sha, {"basis": "bounded generation fixture"}, status="accepted_legacy")
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA journal_mode=WAL")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    api_server._clear_article_keyword_cache()
    original = api_server.RegistryListProjection
    builds = []
    def change_after_every_build(reader):
        result = original(reader)
        builds.append(result)
        with sqlite3.connect(database) as db:
            db.execute("UPDATE registry_publication SET is_visible=1-is_visible WHERE entity_id='a'")
        return result
    monkeypatch.setattr(api_server, "RegistryListProjection", change_after_every_build)
    with pytest.raises(HTTPException) as error:
        api_server.registry_articles(include_pdf=True)
    assert error.value.status_code == 503 and len(builds) == 3
    assert api_server._ARTICLE_KEYWORDS_CACHE is None
    for projection in builds:
        with pytest.raises(sqlite3.ProgrammingError):
            projection.connection.execute("SELECT 1")
