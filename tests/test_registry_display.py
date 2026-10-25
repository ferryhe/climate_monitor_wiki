"""Publication prepares display data; web reads only select final approved DTOs."""
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from climate_registry.display import DisplayReader, display_snapshot, refresh_display
from climate_registry.publication import _approve, _pdf_dto, _public_dto, _merge_pdf_article_payloads, stage_entities, set_visibility
from climate_registry.read_api import RegistryReader
from test_registry_publication import _database, _pdf, _reader


def _publish(connection):
    for candidate in stage_entities(connection):
        _approve(connection, candidate, {"basis": "explicit approved display fixture"}, status="accepted_legacy")
    refresh_display(connection)


def test_display_searches_pdf_occurrence_text_while_returning_article_summary(tmp_path):
    from climate_registry.publication import snapshot_entity, stage_snapshot
    from test_registry_publication import _reviewed_pdf_summary_receipt

    database = _database(tmp_path, version=24)
    url = "https://pdf.example/wildfire"
    with sqlite3.connect(database) as db:
        _pdf(db, identity="pdf-search", url=url,
            occurrence={"summary": "Wildfire mention from the PDF page", "summary_basis": "verbatim_pdf_article_row"})
        _publish(db)
        snapshot = snapshot_entity(db, "pdf_article", "pdf-search")
        snapshot["derived_display"] = {"summary": "Approved article summary", "summary_provenance": "owner_reviewed_source_evidence"}
        sha = stage_snapshot(db, snapshot)
        _approve(db, sha, _reviewed_pdf_summary_receipt(snapshot, sha))
        refresh_display(db)
        result = DisplayReader(db).pdf_articles(page=1, page_size=10, query="wildfire")
        detail = DisplayReader(db).article("pdf-search", pdf=True)

    assert result["pagination"]["total"] == 1
    assert result["items"][0]["summary"] == "Approved article summary"
    assert detail["summary"] == "Approved article summary"
    assert detail["occurrences"][0]["summary"] == "Wildfire mention from the PDF page"


def test_display_web_routes_do_no_source_work_and_sql_page(tmp_path, monkeypatch):
    import api_server
    import climate_registry.publication as publication
    import climate_registry.display as display
    from test_issue221_review_repairs import _report_database, _approve_all
    database, source, _ = _report_database(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db, identity="pdf-example", url="https://pdf.example/article")
        _approve_all(db)
        document = db.execute("SELECT document_sha256 FROM pdf_intake_documents").fetchone()[0]
        core_id = db.execute("SELECT article_id FROM articles LIMIT 1").fetchone()[0]
    reader = RegistryReader(database, repository_root=tmp_path / "application", source_dir=source)
    expected_core = _public_dto(reader.article(core_id))
    expected_pdf = _pdf_dto(_merge_pdf_article_payloads(reader.pdf_article("pdf-example"), None))
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    def unexpected(*args, **kwargs):
        raise AssertionError("a web display read reconstructed authoritative source data")
    monkeypatch.setattr(publication, "published_snapshots", unexpected)
    monkeypatch.setattr(RegistryReader, "_validate_contract", unexpected)
    monkeypatch.setattr(RegistryReader, "_direct_meeting_collection", unexpected)
    monkeypatch.setattr(api_server, "RegistryListProjection", unexpected)
    monkeypatch.setattr(api_server, "load_article_taxonomy", unexpected)
    monkeypatch.setattr(api_server, "load_report_artifact", unexpected)
    monkeypatch.setattr(display, "refresh_display", unexpected)
    client = TestClient(api_server.app)
    for url in ("status?include_pdf=true", "reports?include_pdf=true", "articles?include_pdf=true", "publishers?include_pdf=true", "article-categories", "meetings?base_date=2026-10-16", "pdf-intake/articles", "pdf-intake/calendar"):
        response = client.get("/api/registry/" + url)
        assert response.status_code == 200, (url, response.text)
    assert client.get(f"/api/registry/articles/{core_id}").json() == expected_core
    assert client.get("/api/registry/pdf-intake/articles/pdf-example").json() == expected_pdf
    assert client.get("/api/registry/reports/2026-09-07").status_code == 200
    for page in (1, 2, 100):
        assert client.get(f"/api/registry/pdf-intake/reports/{document}?page={page}&page_size=1").status_code == 200
    assert client.get("/api/registry/pdf-intake/reports/malformed").status_code == 400
    statements = []
    with sqlite3.connect(database) as db:
        db.set_trace_callback(statements.append)
        payload = DisplayReader(db).report(document, pdf=True, page=2, page_size=1)
    assert payload["articles"] == [] and payload["article_offset"] == 1
    assert any("display_modules" in sql and "LIMIT 1 OFFSET 1" in sql for sql in statements)
    assert not any("snapshot_json" in sql for sql in statements)


def test_dirty_generation_fails_closed_pending_staging_keeps_old_publication_and_rollback(tmp_path, monkeypatch):
    import api_server
    database = _database(tmp_path, version=24)
    with sqlite3.connect(database) as db:
        _publish(db)
        generation = db.execute("SELECT generation FROM display_state").fetchone()[0]
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    client = TestClient(api_server.app)
    before = client.get("/api/registry/articles/a").json()
    with sqlite3.connect(database) as db:
        db.execute("UPDATE articles SET publication_eligible=0 WHERE article_id='a'")
        stage_entities(db)
        assert not refresh_display(db)
        assert db.execute("SELECT generation FROM display_state").fetchone()[0] == generation
    assert client.get("/api/registry/articles/a").json() == before
    with sqlite3.connect(database) as db:
        db.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_id='a'")
    assert client.get("/api/registry/articles/a").status_code == 503
    with sqlite3.connect(database) as db:
        refresh_display(db)
    assert client.get("/api/registry/articles/a").status_code == 404
    import climate_registry.display as display
    def failed(*args, **kwargs):
        raise RuntimeError("generation failed")
    monkeypatch.setattr(display, "refresh_display", failed)
    with pytest.raises(RuntimeError, match="generation failed"):
        set_visibility(database, "article", "a", True)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT is_visible FROM registry_publication WHERE entity_id='a'").fetchone()[0] == 0
        assert db.execute("SELECT dirty FROM display_state").fetchone()[0] == 0


def test_publication_batch_refreshes_once_after_all_mutations(tmp_path, monkeypatch):
    from climate_registry.information_checks import _writer
    import climate_registry.display as display
    database = _database(tmp_path, version=24)
    with sqlite3.connect(database) as db:
        _publish(db)
    builds = []
    original = display.RegistryListProjection
    def counted(reader):
        builds.append(tuple(reader._snapshot_connection.execute(
            "SELECT is_visible FROM registry_publication ORDER BY entity_id")))
        return original(reader)
    monkeypatch.setattr(display, "RegistryListProjection", counted)
    with _writer(database) as db:
        db.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_id='a'")
        db.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_id='b'")
        assert builds == []
    assert len(builds) == 1 and [row[0] for row in builds[0]] == [0, 0]
    with display_snapshot(_reader(database, tmp_path)) as stored:
        assert stored.articles(page=1, page_size=20)["items"] == []


def test_display_snapshot_is_atomic_and_backfill_is_idempotent(tmp_path):
    from climate_registry.publication import migrate_publication
    from climate_registry.contract import SCHEMA_VERSION
    database = _database(tmp_path, version=SCHEMA_VERSION)
    with sqlite3.connect(database) as db:
        _publish(db)
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    reader = _reader(database, tmp_path)
    writer = sqlite3.connect(database)
    try:
        with display_snapshot(reader) as old:
            old_version = old.version
            assert old.articles(page=1, page_size=20)["pagination"]["total"] == 2
            writer.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_id='a'")
            refresh_display(writer)
            writer.commit()
            assert old.version == old_version
            assert old.articles(page=1, page_size=20)["pagination"]["total"] == 2
        with display_snapshot(reader) as new:
            assert new.version != old_version
            assert new.articles(page=1, page_size=20)["pagination"]["total"] == 1
    finally:
        writer.close()
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")
    before = database.read_bytes()
    migrate_publication(database, tmp_path / "backup", apply=True)
    assert database.read_bytes() == before


@pytest.mark.parametrize("outcome", ["commit", "rollback", "error"])
@pytest.mark.parametrize("spill", [0, 16])
@pytest.mark.parametrize("active_publication", [False, True])
def test_large_rollback_journal_refresh_keeps_old_display_readable_and_restores_spill(tmp_path, monkeypatch, outcome, spill, active_publication):
    import climate_registry.display as display
    from verified_fixtures import seed_verified_articles
    database = _database(tmp_path, version=24)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE article_versions SET observed_summary=?", ("Approved climate risk. " + "Flood exposure increased. " * 10000,))
        seed_verified_articles(db)
        _publish(db)
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert db.execute("SELECT sum(length(dto)) FROM display_details").fetchone()[0] > 128 * 1024
        old_version = DisplayReader(db).version
    in_build, resume_build = threading.Event(), threading.Event()
    before_finish, finish = threading.Event(), threading.Event()
    observed = {}
    original_boundaries = display._boundaries
    def paused(reader):
        observed["during_spill"] = reader._snapshot_connection.execute("PRAGMA cache_spill").fetchone()[0]
        in_build.set()
        if not resume_build.wait(10):
            raise RuntimeError("concurrent reader did not resume generation")
        if outcome == "error":
            raise RuntimeError("injected display generation failure")
        return original_boundaries(reader)
    monkeypatch.setattr(display, "_boundaries", paused)
    def writer():
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA cache_size=-32")
            connection.execute("PRAGMA cache_spill=16")
            connection.execute(f"PRAGMA cache_spill={spill}")
            observed["original_spill"] = connection.execute("PRAGMA cache_spill").fetchone()[0]
            if active_publication:
                connection.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_id='a'")
            old_display()
            observed["readable_before_refresh"] = True
            try:
                refresh_display(connection, force=True)
            except RuntimeError as exc:
                if outcome != "error" or str(exc) != "injected display generation failure":
                    raise
                observed["generation_error"] = True
            observed["restored_spill"] = connection.execute("PRAGMA cache_spill").fetchone()[0]
            before_finish.set()
            if not finish.wait(10):
                raise RuntimeError("concurrent reader did not release publication")
            if outcome == "commit":
                connection.commit()
            else:
                connection.rollback()
            observed["finished_spill"] = connection.execute("PRAGMA cache_spill").fetchone()[0]
            if not spill:
                connection.execute("PRAGMA cache_spill=ON")
                observed["retained_threshold"] = connection.execute("PRAGMA cache_spill").fetchone()[0]
        except BaseException as exc:
            observed["writer_error"] = repr(exc)
            before_finish.set()
        finally:
            connection.rollback()
            connection.close()
    def old_display():
        connection = sqlite3.connect(database, timeout=0.25)
        try:
            connection.execute("BEGIN")
            stored = DisplayReader(connection)
            assert stored.version == old_version
            assert stored.articles(page=1, page_size=20)["pagination"]["total"] == 2
            assert stored.article("a")["summary"].startswith("Approved climate risk.")
        finally:
            connection.close()
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert in_build.wait(10), observed
        old_display()
        resume_build.set()
        assert before_finish.wait(10), observed
        assert "writer_error" not in observed, observed
        # Restoring the caller's spill setting must not flush the generation early.
        old_display()
    finally:
        resume_build.set()
        finish.set()
        thread.join(10)
    assert not thread.is_alive()
    assert "writer_error" not in observed, observed
    assert observed["readable_before_refresh"]
    assert observed["during_spill"] == 0
    assert observed["original_spill"] == observed["restored_spill"] == observed["finished_spill"]
    if not spill:
        assert observed["retained_threshold"] == 16
    assert observed.get("generation_error", False) == (outcome == "error")
    with sqlite3.connect(database) as db:
        stored = DisplayReader(db)
        assert (stored.version != old_version) == (outcome == "commit")
        changed = outcome == "commit" and active_publication
        assert stored.articles(page=1, page_size=20)["pagination"]["total"] == (1 if changed else 2)
        assert db.execute("SELECT is_visible FROM registry_publication WHERE entity_id='a'").fetchone()[0] == (0 if changed else 1)


def test_display_pdf_pages_and_date_intervals_match_approved_reader(tmp_path):
    from test_issue221_review_repairs import _approve_all
    from climate_registry.publication import _calendar_dto
    database = _database(tmp_path, version=24)
    # The collection's date semantics include explicit native periods and deadlines.
    with sqlite3.connect(database) as db:
        _pdf(db, identity="pdf-example", url="https://pdf.example/article")
        _approve_all(db)
        document = db.execute("SELECT document_sha256 FROM pdf_intake_documents").fetchone()[0]
    reader = _reader(database, tmp_path)
    with reader.public_snapshot(direct=True):
        expected = reader.pdf_report(document)
        expected["articles"] = [_pdf_dto(item) for item in expected["articles"]]
        expected["calendar_items"] = [_calendar_dto(item) for item in expected["calendar_items"]]
        with display_snapshot(reader) as stored:
            assert stored.report(document, pdf=True) == expected
            for page in (1, 2, 100):
                legacy = reader.pdf_report(document, page=page, page_size=1)
                legacy["articles"] = [_pdf_dto(item) for item in legacy["articles"]]
                legacy["calendar_items"] = [_calendar_dto(item) for item in legacy["calendar_items"]]
                assert stored.report(document, pdf=True, page=page, page_size=1) == legacy
            for base in ("2026-01-01", "2026-09-01", "2026-09-02", "2027-01-01"):
                for unknown in (False, True):
                    original = reader.meetings(page=1, page_size=100, base_date=base, include_unknown=unknown)
                    original["items"] = [_calendar_dto(item) for item in original["items"]]
                    assert stored.meetings(page=1, page_size=100, base_date=base, include_unknown=unknown) == original


@pytest.mark.parametrize("precision,start,end,raw,deadline", [
    ("day", "2027-06-10", "2027-06-12", "June 10–12, 2027", "2027-07-01"),
    ("day", "2027-06-10", None, "June 10, 2027", None),
    ("month", "2027-06", "2027-06", "June 2027", None),
    ("month", "2027-06", None, "June 2027", None),
    ("quarter", "2027-Q2", "2027-Q2", "Q2 2027", None),
    ("year", "2027", "2027", "2027", None),
    ("unknown", None, None, "Date to be confirmed", None),
])
def test_native_date_boundaries_match_for_both_unknown_modes(tmp_path, precision, start, end, raw, deadline):
    from climate_monitor.meetings import process_batch
    from climate_registry.publication import _calendar_dto
    from test_issue136_meetings import _candidate, _database as meeting_database
    from test_issue221_review_repairs import _approve_all
    candidate = _candidate(date_precision=precision, start_date=start, end_date=end,
        raw_time_text=raw, date_evidence=raw, location=None, online_url=None,
        deadline_type="registration" if deadline else None, deadline_date=deadline,
        deadline_evidence=deadline)
    body = f"World Climate Summit 2027 is held {raw}. Climate risk agenda."
    if deadline:
        body += f" Registration deadline {deadline}."
    database = meeting_database(tmp_path, [body])
    process_batch(database, "batch", prompt_text="dates", prompt_version="v1", provider="fixture",
        model="fixture", extractor=lambda request: {"events": [candidate]})
    with sqlite3.connect(database) as db:
        _approve_all(db)
    reader = _reader(database, tmp_path)
    for base in ("2027-01-01", "2027-06-10", "2027-06-11", "2027-06-12", "2027-06-13",
            "2027-06-30", "2027-07-01", "2027-07-02", "2027-12-31", "2028-01-01"):
        for unknown in (False, True):
            expected = reader.meetings(base_date=base, include_unknown=unknown)
            expected["items"] = [_calendar_dto(item) for item in expected["items"]]
            with display_snapshot(reader) as stored:
                assert stored.meetings(page=1, page_size=20, base_date=base, include_unknown=unknown) == expected


def test_replacement_generations_and_default_utc_date(tmp_path, monkeypatch):
    import climate_registry.display as display
    from datetime import datetime, timezone
    versions = []
    for name in ("first", "second"):
        directory = tmp_path / name
        directory.mkdir()
        database = _database(directory, version=24)
        with sqlite3.connect(database) as db:
            _publish(db)
        with display_snapshot(_reader(database, tmp_path)) as stored:
            versions.append(stored.version)
    assert versions[0] != versions[1]
    class Clock(datetime):
        current = "2027-06-30"
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc
            return datetime.fromisoformat(cls.current).replace(tzinfo=tz)
    monkeypatch.setattr(display, "datetime", Clock)
    with display_snapshot(_reader(database, tmp_path)) as stored:
        assert stored.meetings(page=1, page_size=20)["base_date"] == "2027-06-30"
        Clock.current = "2027-07-01"
        assert stored.meetings(page=1, page_size=20)["base_date"] == "2027-07-01"


def test_artifact_metadata_refresh_keeps_clicks_cheap_and_download_validation(tmp_path, monkeypatch):
    import api_server
    from climate_registry.display import refresh_artifact_display
    from report_artifact_fixtures import write_canonical_artifact
    from test_report_artifact_api import _registry, REPORT_DATE, PDF_FILENAME
    database = _registry(tmp_path / "registry")
    output = tmp_path / "artifacts"
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setenv("CLIMATE_DELIVERY_OUTPUT_DIR", str(output))
    with sqlite3.connect(database) as db:
        refresh_display(db)
    client = TestClient(api_server.app)
    assert client.get(f"/api/registry/reports/{REPORT_DATE}").json()["report_pdf"] is None

    fixture = write_canonical_artifact(output)
    refresh_artifact_display(REPORT_DATE, output_dir=output)
    def unexpected(*args, **kwargs):
        raise AssertionError("detail click hashed a delivery artifact")
    monkeypatch.setattr(api_server, "load_report_artifact", unexpected)
    result = client.get(f"/api/registry/reports/{REPORT_DATE}")
    assert result.status_code == 200
    assert result.json()["report_briefing"] is None
    assert result.json()["report_pdf"]["filename"] == PDF_FILENAME
    (fixture.artifact_dir / PDF_FILENAME).write_bytes(b"%PDF-tampered")
    assert client.get(f"/api/registry/reports/{REPORT_DATE}").json()["report_pdf"] is None


@pytest.mark.parametrize("writer", ["audit", "persistent", "capture"])
def test_writer_optimization_leaves_ready_display_generation(tmp_path, monkeypatch, writer):
    from test_climate_registry_persistent import _write_report, _item, _build_current_registry
    from climate_registry.audit import build_audit_registry
    source = tmp_path / "sources"
    _write_report(source, "2026-08-03", _item("Original", "Summary", "https://example.com/item"))
    database = tmp_path / "registry.sqlite3"
    if writer == "persistent":
        from climate_registry.persistent import update_registry
        _build_current_registry(source, database, tmp_path)
        _write_report(source, "2026-08-10", _item("Updated", "New summary", "https://example.com/item"))
        invoke = lambda: update_registry(source, database, tmp_path / "backups")
    elif writer == "capture":
        from test_climate_registry_capture import _registry, _run, FakeTransport, _response
        database = _registry(tmp_path)
        invoke = lambda: _run(database, tmp_path, FakeTransport(_response()))
    else:
        invoke = lambda: build_audit_registry(source, database, tmp_path / "audit")
    if database.exists():
        with sqlite3.connect(database) as db:
            db.execute("DROP TABLE IF EXISTS sqlite_stat1")
    optimized = []
    original_connect = sqlite3.connect
    class OptimizingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA optimize":
                optimized.append(True)
                # optimize may run ANALYZE and create sqlite_stat1 for the first time.
                super().execute("ANALYZE")
            return super().execute(sql, *args, **kwargs)
    def connect(*args, **kwargs):
        kwargs.setdefault("factory", OptimizingConnection)
        return original_connect(*args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", connect)
    invoke()
    assert optimized
    with display_snapshot(_reader(database, tmp_path)) as stored:
        assert stored.articles(page=1, page_size=20)["pagination"]["total"] >= 0
