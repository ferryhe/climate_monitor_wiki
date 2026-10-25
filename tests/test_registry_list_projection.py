import json
import os
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
                "publication_date": "2026-09-02", "updated_date": "2026-10-01", "revision_date": "2026-09",
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
            assert result["items"][0]["updated_date"] == "2026-10-01"
            assert result["items"][0]["revision_date"] == "2026-09"
        for source in ("source-1", "Publisher", "example.org", "WWW.example.org"):
            assert page(source=source)["pagination"]["total"] == 1
        assert page(query="PDF only hit", pillar="A")["items"] == []
        assert page(report_date="2026-09-03")["items"][0]["article_id"] == "core"
        assert page(report_date="2026-09-01", pillar="B")["items"] == []
        assert page(report_date="2026-09-02", pillar="B")["pagination"]["total"] == 1
        assert any("COUNT(*)" in sql for sql in statements)
        assert any("LIMIT 20 OFFSET 0" in sql for sql in statements)
        keywords = json.loads(projection.category_body)["items"][0]
        assert keywords["categories"] == ["Flood"]
        assert "keywords" not in keywords and "summary" not in keywords
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
    api_server._clear_registry_list_cache()
    api_server._warm_registry_list_cache()
    assert api_server._REGISTRY_LIST_CACHE is not None
    def unexpected(*args, **kwargs): raise AssertionError("stable list reloaded source snapshots or full contract")
    monkeypatch.setattr(publication, "published_snapshots", unexpected)
    monkeypatch.setattr(RegistryReader, "_validate_contract", unexpected)
    assert len(api_server.registry_articles(page="1", page_size="1", include_pdf=True)["items"]) == 1
    assert api_server.registry_reports(include_pdf=True)["pagination"]["total"] == 0
    assert api_server.registry_status(include_pdf=True)["articles"] == 2
    assert api_server.registry_publishers(include_pdf=True)["items"]
    assert len(json.loads(api_server.registry_article_categories().body)["items"]) == 2
    api_server._clear_registry_list_cache()


def test_optimized_projection_uses_article_paths_and_keeps_host_filtering():
    from climate_registry.read_api import _publisher_label

    class Reader:
        def _public_candidates(self, *kinds):
            return {}
        def approved_article_cards_all(self):
            return [
                {"article_id": "bcbs", "canonical_url": "https://bis.org/committees/bcbs/overview",
                    "title": "BCBS", "source": "bis.org", "publisher": "The Basel Committee on Banking Supervision",
                    "source_id": "source-bis", "last_seen": "2026-09-02", "appearances": []},
                {"article_id": "bis", "canonical_url": "https://bis.org/about/index.htm",
                    "title": "BIS", "source": "bis.org", "publisher": "The Basel Committee on Banking Supervision",
                    "source_id": "source-bis", "last_seen": "2026-09-01", "appearances": []},
            ]
        def pdf_articles_all(self, **kwargs):
            return [
                {"article_id": "pdf-oca", "canonical_url": "https://osfi-bsif.gc.ca/en/oca/about-office-chief-actuary",
                    "title": "OCA", "summary": "OCA summary", "source": "osfi-bsif.gc.ca", "publisher": "osfi-bsif.gc.ca",
                    "last_seen": "2026-09-03", "source_kind": "pdf", "occurrences": [{"report_date": "2026-09-03"}]},
                {"article_id": "pdf-bcbs", "canonical_url": "https://bis.org/committees/bcbs/pdf-only",
                    "title": "BCBS PDF", "summary": "BCBS PDF summary", "source": "bis.org", "publisher": "bis.org",
                    "last_seen": "2026-09-04", "source_kind": "pdf", "occurrences": [{"report_date": "2026-09-04"}]},
            ]
        def approved_reports_all(self): return []
        def pdf_reports_all(self): return []
        def status(self): return {"available": True}
        def approved_merged_article_count(self): return 2
        def publishers(self, *, include_pdf=False):
            items = [{"hostname": "bis.org", "label": _publisher_label(
                "bis.org", "The Basel Committee on Banking Supervision")}]
            if include_pdf:
                items.append({"hostname": "osfi-bsif.gc.ca", "label": _publisher_label(
                    "osfi-bsif.gc.ca", "osfi-bsif.gc.ca")})
            return {"items": items, "total": len(items), "truncated": False}

    projection = RegistryListProjection(Reader())
    try:
        items = projection.articles(page=1, page_size=10, include_pdf=True)["items"]
        assert {item["article_id"]: (item["publisher"], item["source"]) for item in items} == {
            "pdf-oca": ("Office of the Chief Actuary (OCA)", "osfi-bsif.gc.ca"),
            "pdf-bcbs": ("The Basel Committee on Banking Supervision (BCBS)", "bis.org"),
            "bcbs": ("The Basel Committee on Banking Supervision (BCBS)", "bis.org"),
            "bis": ("Bank for International Settlements (BIS)", "bis.org"),
        }
        assert projection.articles(page=1, page_size=10, source="source-bis")["pagination"]["total"] == 2
        assert projection.publishers(include_pdf=True)["items"][0] == {
            "hostname": "bis.org", "label": "Bank for International Settlements (BIS)"}
        categories = {item["article_id"]: item["publisher"]
            for item in json.loads(projection.category_body)["items"]}
        assert categories["bcbs"] == "The Basel Committee on Banking Supervision (BCBS)"
        assert categories["bis"] == "Bank for International Settlements (BIS)"
    finally:
        projection.close()


def test_article_lists_use_approved_publication_date_with_unknowns_last():
    from climate_registry.read_api import filter_article_cards, filter_pdf_articles

    class Reader:
        def _public_candidates(self, *kinds): return {}
        def approved_article_cards_all(self):
            return [
                {"article_id": "new", "canonical_url": "https://example.org/new", "title": "New",
                    "source": "example.org", "publisher": "Example", "last_seen": "2026-10-17",
                    "publication_date": "2026-10-10", "collected_at": "2026-10-25T00:00:00Z", "appearances": []},
                {"article_id": "old", "canonical_url": "https://example.org/old", "title": "Old",
                    "source": "example.org", "publisher": "Example", "last_seen": "2026-10-19",
                    "publication_date": "2026-09-01", "collected_at": "2026-10-24T00:00:00Z", "appearances": []},
                {"article_id": "unknown", "canonical_url": "https://example.org/unknown", "title": "Unknown",
                    "source": "example.org", "publisher": "Example", "last_seen": "2026-10-18",
                    "publication_date": "", "appearances": []},
                {"article_id": "collected", "canonical_url": "https://example.org/collected", "title": "Collected",
                    "source": "example.org", "publisher": "Example", "last_seen": "2026-10-01",
                    "publication_date": None, "collected_at": "2026-10-15T00:00:00Z", "appearances": []},
            ]
        def pdf_articles_all(self, **kwargs):
            return [{"article_id": "pdf-mid", "canonical_url": "https://pdf.example/mid", "title": "PDF",
                "source": "pdf.example", "publisher": "PDF Example", "source_kind": "pdf",
                "last_seen": "2026-10-20", "publication_date": "2026-10-05",
                "collected_at": "2026-10-30T00:00:00Z",
                "occurrences": [{"report_date": "2026-10-20"}]}]
        def approved_reports_all(self): return []
        def pdf_reports_all(self): return []
        def status(self): return {"available": True}
        def approved_merged_article_count(self): return 3
        def publishers(self, **kwargs): return {"items": [], "total": 0, "truncated": False}

    projection = RegistryListProjection(Reader())
    try:
        first = projection.articles(page=1, page_size=2, include_pdf=True)["items"]
        second = projection.articles(page=2, page_size=2, include_pdf=True)["items"]
        third = projection.articles(page=3, page_size=2, include_pdf=True)["items"]
        assert [item["article_id"] for item in first + second + third] == ["collected", "new", "pdf-mid", "old", "unknown"]
        assert first[0]["collected_at"] == "2026-10-15T00:00:00Z"
        assert first[1]["publication_date"] == "2026-10-10"
        assert first[1]["collected_at"] == "2026-10-25T00:00:00Z"
        assert second[0]["publication_date"] == "2026-10-05"
        assert second[0]["collected_at"] == "2026-10-30T00:00:00Z"
        assert second[1]["publication_date"] == "2026-09-01"
        assert third[0]["publication_date"] is None
        assert third[0]["collected_at"] is None
        category_ids = [item["article_id"] for item in json.loads(projection.category_body)["items"]]
        assert category_ids == ["pdf-mid", "old", "unknown", "new", "collected"]
        core = projection.articles(page=1, page_size=10)["items"]
        assert [item["article_id"] for item in core] == ["collected", "new", "old", "unknown"]
        assert core[0]["collected_at"][:10] > core[1]["publication_date"]
        direct = filter_article_cards([
            {"article_id": "new", "publication_date": "2026-10-10", "last_seen": "2026-10-17"},
            {"article_id": "tie-z", "publication_date": "2026-10-10", "last_seen": "2026-10-01"},
            {"article_id": "tie-a", "publication_date": "2026-10-10", "last_seen": "2026-10-20"},
            {"article_id": "old", "publication_date": "2026-09-01", "last_seen": "2026-10-19"},
            {"article_id": "collected", "publication_date": None, "collected_at": "2026-10-05T00:00:00Z", "last_seen": "2026-10-18"},
            {"article_id": "unknown", "last_seen": "2026-10-18"},
        ])
        assert [item["article_id"] for item in direct] == ["new", "tie-a", "tie-z", "collected", "old", "unknown"]
        direct_pdf = filter_pdf_articles([
            {"article_id": "pdf-old", "publication_date": "2026-09-01", "last_seen": "2026-10-19", "occurrences": []},
            {"article_id": "pdf-new", "publication_date": "2026-10-10", "last_seen": "2026-10-17", "occurrences": []},
            {"article_id": "pdf-collected", "collected_at": "2026-10-05T00:00:00Z", "last_seen": "2026-10-18", "occurrences": []},
            {"article_id": "pdf-unknown", "last_seen": "2026-10-18", "occurrences": []},
        ])
        assert [item["article_id"] for item in direct_pdf] == ["pdf-new", "pdf-collected", "pdf-old", "pdf-unknown"]
    finally:
        projection.close()


@pytest.mark.skipif(os.name == "nt", reason="API management imports require POSIX fcntl")
def test_static_export_article_api_orders_before_core_and_merged_pagination(monkeypatch):
    import api_server
    from climate_registry.read_api import RegistryReader

    reader = RegistryReader.__new__(RegistryReader)
    reader.database = None
    reader.public = True
    reader.source_dir = reader.metadata_dir = None
    reader.static_snapshot = {"articles": [
        {"article_id": "older", "canonical_url": "https://example.org/older", "title": "Older",
            "source": "example.org", "publisher": "Example", "first_seen": "2026-01-01",
            "last_seen": "2026-10-17", "publication_date": "2026-01-01", "collected_at": "2026-10-20T00:00:00Z", "publication_eligible": True,
            "document_kind": "article", "appearances": []},
        {"article_id": "newer", "canonical_url": "https://example.org/newer", "title": "Newer",
            "source": "example.org", "publisher": "Example", "first_seen": "2026-10-10",
            "last_seen": "2026-10-10", "publication_date": "2026-10-10", "collected_at": "2026-10-25T00:00:00Z", "publication_eligible": True,
            "document_kind": "article", "appearances": []},
        {"article_id": "collected", "canonical_url": "https://example.org/collected", "title": "Collected",
            "source": "example.org", "publisher": "Example", "first_seen": "2026-10-15",
            "last_seen": "2026-10-15", "publication_date": None, "collected_at": "2026-10-05T00:00:00Z",
            "publication_eligible": True, "document_kind": "article", "appearances": []},
        {"article_id": "unknown", "canonical_url": "https://example.org/unknown", "title": "Unknown",
            "source": "example.org", "publisher": "Example", "first_seen": "2026-10-18",
            "last_seen": "2026-10-18", "publication_date": None, "publication_eligible": True,
            "document_kind": "article", "appearances": []},
    ], "pdf_articles": [
        {"article_id": "pdf-collected", "canonical_url": "https://pdf.example/collected", "title": "PDF collected",
            "source": "pdf.example", "publisher": "PDF Example", "first_seen": "2026-10-17",
            "last_seen": "2026-10-17", "publication_date": None, "publication_eligible": True,
            "document_kind": "pdf_article", "occurrence_count": 1,
            "occurrences": [{"occurrence_id": "occ-collected", "imported_at": "2026-10-20T12:00:00Z",
                "report_date": "2026-10-17", "display_summary": "PDF collection summary"}]},
        {"article_id": "pdf-published", "canonical_url": "https://pdf.example/published", "title": "PDF published",
            "source": "pdf.example", "publisher": "PDF Example", "first_seen": "2026-10-17",
            "last_seen": "2026-10-17", "publication_date": "2026-10-11", "publication_eligible": True,
            "document_kind": "pdf_article", "occurrence_count": 1,
            "occurrences": [{"occurrence_id": "occ-published", "imported_at": "2026-10-30T12:00:00Z",
                "report_date": "2026-10-17", "display_summary": "PDF published summary"}]},
    ]}
    reader._published_candidate_sha = lambda snapshot: "a" * 64
    assert next(item for item in reader.approved_article_cards_all() if item["article_id"] == "collected")["collected_at"] == "2026-10-05T00:00:00Z"
    assert [item["article_id"] for item in reader.articles(page=1, page_size=4)["items"]] == [
        "newer", "collected", "older", "unknown"]
    pdf_cards = reader.pdf_articles(page=1, page_size=10)["items"]
    assert [item["article_id"] for item in pdf_cards] == ["pdf-published", "pdf-collected"]
    assert pdf_cards[1]["collected_at"] is None
    assert reader.pdf_article("pdf-collected").get("collected_at") is None
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))

    for include_pdf, expected in (
        (False, ["newer", "collected", "older", "unknown"]),
        (True, ["pdf-published", "newer", "collected", "older", "pdf-collected", "unknown"]),
    ):
        pages = [api_server.registry_articles(page=str(page), page_size="1", include_pdf=include_pdf)
            for page in range(1, len(expected) + 1)]
        assert [item["article_id"] for payload in pages for item in payload["items"]] == expected
        assert all(payload["pagination"]["total"] == len(expected) for payload in pages)


@pytest.mark.parametrize("damage", ["deleted", "schema", "approval", "binding"])
def test_cached_lists_fail_closed_when_source_is_invalid(tmp_path, monkeypatch, damage):
    import api_server
    from fastapi import HTTPException
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):
            _approve(db, sha, {"basis": "list invalidation fixture"}, status="accepted_legacy")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    api_server._clear_registry_list_cache()
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
    assert api_server._REGISTRY_LIST_CACHE is None


def test_list_generation_never_publishes_uncommitted_wal_visibility(tmp_path, monkeypatch):
    import api_server
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):
            _approve(db, sha, {"basis": "list WAL fixture"}, status="accepted_legacy")
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    api_server._clear_registry_list_cache()
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
        api_server._clear_registry_list_cache()


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
    api_server._clear_registry_list_cache()
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
    assert api_server._REGISTRY_LIST_CACHE is None


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
    api_server._clear_registry_list_cache()
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
    assert api_server._REGISTRY_LIST_CACHE is None
    for projection in builds:
        with pytest.raises(sqlite3.ProgrammingError):
            projection.connection.execute("SELECT 1")


def test_activity_pages_are_removed_from_article_projection():
    import json
    import sqlite3
    from climate_registry.list_projection import RegistryListProjection

    projection = object.__new__(RegistryListProjection)
    projection.prefix = ""
    projection.connection = sqlite3.connect(":memory:")
    projection.connection.executescript("""
        CREATE TABLE articles (url TEXT, article_id TEXT, last_seen TEXT, first_seen TEXT,
            core_card TEXT, merged_card TEXT, core_search TEXT, core_source TEXT, publisher TEXT,
            source_id TEXT, pdf_search TEXT, pdf_source TEXT);
        CREATE TABLE appearances (url TEXT, kind TEXT, report_date TEXT, pillar TEXT);
    """)
    for article_id, activity in (("article-news", False), ("article-event", True)):
        card = {"article_id": article_id, "title": article_id, "canonical_url": f"https://example.org/{article_id}",
            "publication_date": "2026-10-01", "collected_at": "2026-10-02T00:00:00Z", "is_activity": activity}
        projection.connection.execute("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            card["canonical_url"], article_id, "2026-10-02", "2026-10-01", json.dumps(card), json.dumps(card),
            article_id, "registry", "Example", "source", article_id, "example.org"))
    projection.connection.commit()
    result = projection.articles(page=1, page_size=20, include_pdf=True)
    assert [item["article_id"] for item in result["items"]] == ["article-news"]
    assert result["pagination"]["total"] == 1
    projection.close()
