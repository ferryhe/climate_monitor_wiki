import sqlite3
import json
import subprocess
import sys
from pathlib import Path

import pytest

from climate_registry.article_dates import ArticleDateImportError, import_observations, validate_observations
from climate_registry.schema import apply_migrations


def _database(path):
    connection = sqlite3.connect(path)
    apply_migrations(connection)
    connection.execute(
        "INSERT INTO sources VALUES ('s', 'example.org', 'Example', '2026-01-01', '2026-01-01')"
    )
    connection.execute(
        """INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen)
           VALUES ('article-a', 'https://example.org/a', 's', '2026-01-01', '2026-01-01')"""
    )
    connection.commit()
    connection.close()
    return path


def _pdf_article(connection, *, article_id="pdf-a", url="https://pdf.example/a", core_article_id=None):
    connection.execute("""INSERT INTO pdf_intake_documents(
        document_sha256,source_path,filename,media_type,size_bytes,extracted_text_sha256,document_json,imported_at)
        VALUES(?, '/evidence/source.pdf','source.pdf','application/pdf',1,?,?,?)""",
        ("f" * 64, "e" * 64, "{}", "2026-10-01T00:00:00Z"))
    connection.execute("""INSERT INTO pdf_intake_articles(
        article_id,canonical_url,title,imported_at,core_article_id)
        VALUES(?,?, 'PDF article','2026-10-01T00:00:00Z',?)""", (article_id, url, core_article_id))
    connection.execute("""INSERT INTO pdf_intake_article_occurrences VALUES(
        ?,?,'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',2,?,'2026-10-01',NULL,?,?,?)""",
        ("pdf-occurrence", article_id, url, "d" * 64, "c" * 64,
         json.dumps({"summary": "Source summary", "summary_basis": "verbatim_pdf_article_row"})))


def _observation(kind, observed_at, *, record_id="17"):
    return {
        "article_id": "article-a",
        "canonical_url": "https://example.org/a",
        "observation_kind": kind,
        "observed_at": observed_at,
        "evidence": {
            "source_system": "web_listening" if kind == "collection" else "publisher_page",
            "database": "web_listening.db" if kind == "collection" else "original-page",
            "table": "site_snapshots" if kind == "collection" else "article_metadata",
            "record_id": record_id,
            "source_url": "https://example.org/a",
            "match_basis": "snapshot_final_url" if kind == "collection" else "publisher_date_field",
        },
    }


def _payload(*observations):
    return {"schema_version": "article-date-observations.v1", "observations": list(observations)}


def _reviewed_typed_observation(date_kind, observed_at, *, record_id, precision, url="https://example.org/a"):
    item = _observation("page_information", observed_at, record_id=record_id)
    item["evidence"].update(date_kind=date_kind, date_precision=precision, review_status="pass",
        target_url=url, field="Official page date field", text=f"Source confirms {observed_at}")
    return item


def test_importer_normalizes_collection_evidence_and_is_idempotent(tmp_path):
    database = _database(tmp_path / "registry.sqlite3")
    item = _observation("collection", "2026-01-31T23:30:00-08:00")

    assert import_observations(database, _payload(item)) == {"inserted": 1, "already_present": 0}
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM article_date_observations").fetchone() == (0,)

    assert import_observations(
        database, _payload(item), write=True, recorded_at="2026-02-04T12:00:00Z"
    ) == {"inserted": 1, "already_present": 0}
    assert import_observations(database, _payload(item), write=True) == {
        "inserted": 0, "already_present": 1
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT observed_at, recorded_at FROM article_date_observations"
        ).fetchone() == ("2026-02-01T07:30:00Z", "2026-02-04T12:00:00Z")


def test_page_information_keeps_day_precision_and_only_fills_missing_collection(tmp_path):
    database = _database(tmp_path / "registry.sqlite3")
    page_date = _observation("page_information", "2026-01-28")
    assert import_observations(database, _payload(page_date), write=True) == {
        "inserted": 1, "already_present": 0
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT observed_at FROM article_date_observations"
        ).fetchone() == ("2026-01-28",)

    collection = _observation("collection", "2026-01-29T00:00:00Z")
    with pytest.raises(ArticleDateImportError, match="only when no collection evidence"):
        import_observations(database, _payload(collection, page_date))


def test_typed_update_and_revision_dates_keep_source_precision_with_collection(tmp_path):
    from climate_registry.article_dates import select_article_dates

    database = _database(tmp_path / "registry.sqlite3")
    updated = _observation("page_information", "2026-10-15", record_id="updated")
    updated["evidence"].update(date_kind="updated", date_precision="day")
    revised = _observation("page_information", "2018-01", record_id="revision")
    revised["evidence"].update(date_kind="revised", date_precision="month")
    collected = _observation("collection", "2026-10-16T12:00:00Z", record_id="collected")

    assert import_observations(database, _payload(updated, revised, collected), write=True) == {
        "inserted": 3, "already_present": 0
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT observed_at FROM article_date_observations WHERE source_record_id='revision'"
        ).fetchone() == ("2018-01",)

    selected = select_article_dates([
        {"kind": "page_information", "observed_at": updated["observed_at"], "evidence": updated["evidence"]},
        {"kind": "page_information", "observed_at": revised["observed_at"], "evidence": revised["evidence"]},
    ], collection_times=[collected["observed_at"]])
    assert selected["updated_date"] == "2026-10-15"
    assert selected["revision_date"] == "2018-01"
    assert selected["information_date"] is None
    assert selected["collected_at"] == "2026-10-16T12:00:00Z"


@pytest.mark.parametrize(("value", "precision"), [("2026-09-02", "day"), ("2026-09", "month"), ("2018", "year")])
def test_typed_publication_dates_preserve_precision_and_win_over_collection(value, precision):
    from climate_registry.article_dates import select_article_dates

    item = _observation("page_information", value, record_id="published")
    item["evidence"].update(date_kind="published", date_precision=precision)
    checked = validate_observations(_payload(item))[0]
    selected = select_article_dates([{
        "kind": checked["observation_kind"], "observed_at": checked["observed_at"],
        "evidence": checked["evidence"],
    }], collection_times=["2026-10-16T12:00:00Z"])

    assert selected["publication_date"] == value
    assert selected["information_date"] is None
    assert selected["collected_at"] == "2026-10-16T12:00:00Z"


def test_reviewed_activity_date_is_separate_and_appears_in_dates(tmp_path):
    from climate_registry.read_api import RegistryReader
    from climate_registry.publication import set_visibility, export_public_snapshot

    database = _database(tmp_path / "registry.sqlite3")
    _approve_fixture(database)
    activity = _observation("page_information", "2026-10-21", record_id="event-page")
    activity["evidence"].update(
        date_kind="event", date_precision="day", event_name="Climate risk conference",
        event_type="conference", event_status="scheduled", event_start_date="2026-10-21",
        event_end_date="2026-10-24", date_text="21–24 October 2026", review_status="pass",
        target_url="https://example.org/a", field="Event schedule", text="21–24 October 2026",
        source_url="https://www.iais.org/events/iais-annual-conference-2027/",
        event_url="https://www.iais.org/events/iais-annual-conference-2027/",
    )
    collected = _observation("collection", "2026-10-16T12:00:00Z", record_id="collection")
    assert import_observations(database, _payload(activity, collected), write=True) == {
        "inserted": 2, "already_present": 0
    }

    reader = RegistryReader(database, repository_root=tmp_path / "application")
    with reader.public_snapshot(direct=True):
        assert reader._reviewed_article_activity_events() == []
        assert reader.meetings(base_date="2026-10-18", page_size=20)["items"] == []
    _approve_fixture(database)
    with reader.public_snapshot(direct=True):
        events = reader._reviewed_article_activity_events()
        payload = reader.meetings(base_date="2026-10-18", page_size=20)
        collected = {"existing-event": {"name": "Existing IAIS record", "start_date": "2026-10-21",
            "end_date": "2026-10-24", "source_urls": ["https://www.iais.org/events/iais-annual-conference-2027"]}}
        reader._merge_reviewed_article_activity_items(collected, "2026-10-18")
    assert len(events) == 1
    assert events[0]["start_date"] == "2026-10-21"
    assert events[0]["end_date"] == "2026-10-24"
    assert events[0]["verification_status"] == "verified"
    assert [item["name"] for item in payload["items"]] == ["Climate risk conference"]
    assert payload["items"][0]["raw_date"] == "21–24 October 2026"
    assert list(collected) == ["existing-event"]
    assert len(collected["existing-event"]["source_urls"]) == 2
    with reader.public_snapshot():
        assert reader._reviewed_article_activity_events()[0]["name"] == "Climate risk conference"
        projected = reader.meetings(base_date="2026-10-18", page_size=20)
        assert [item["name"] for item in projected["items"]] == ["Climate risk conference"]
    export_root = tmp_path / "approved-export"
    (export_root / "wiki").mkdir(parents=True)
    export_public_snapshot(database, export_root / "wiki" / "public-registry.json")
    exported = json.loads((export_root / "wiki" / "public-registry.json").read_text(encoding="utf-8"))
    assert [item["name"] for item in exported["meeting_records"] if item.get("name") == "Climate risk conference"] == ["Climate risk conference"]
    static_reader = RegistryReader.from_public_export(export_root)
    assert [item["name"] for item in static_reader.meetings(base_date="2026-10-18")["items"]] == ["Climate risk conference"]
    from climate_registry.display import display_snapshot
    with display_snapshot(reader) as stored:
        assert stored is not None
        assert stored.meetings(page=1, page_size=20, base_date="2026-10-18")["pagination"]["total"] == 1
        assert stored.meetings(page=1, page_size=20, base_date="2026-10-25")["pagination"]["total"] == 0
    with sqlite3.connect(database) as connection:
        boundaries = {row[0] for row in connection.execute("SELECT start_date FROM display_meeting_intervals")}
    assert "2026-10-25" in boundaries
    set_visibility(database, "article", "article-a", False)
    with reader.public_snapshot(direct=True):
        assert reader._reviewed_article_activity_events() == []
        assert reader.meetings(base_date="2026-10-18", page_size=20)["items"] == []


def test_multiple_activity_dates_from_one_page_remain_separate(tmp_path):
    from climate_registry.read_api import RegistryReader

    database = _database(tmp_path / "registry.sqlite3")
    observations = []
    for position, (start, end) in enumerate((("2026-10-20", "2026-10-20"), ("2026-10-27", "2026-10-27")), 1):
        item = _observation("page_information", start, record_id=f"session-{position}")
        item["evidence"].update(date_kind="event", date_precision="day", event_name="NASA SAR training",
            event_type="meeting", event_status="scheduled", event_start_date=start, event_end_date=end,
            date_text=start, review_status="pass")
        item["evidence"].update(target_url="https://example.org/a", field="Training schedule", text=start)
        observations.append(item)
    assert import_observations(database, _payload(*observations), write=True)["inserted"] == 2
    _approve_fixture(database)

    reader = RegistryReader(database, repository_root=tmp_path / "application")
    with reader.public_snapshot(direct=True):
        collected = {}
        reader._merge_reviewed_article_activity_items(collected, "2026-10-18")
    assert len(collected) == 2
    assert {item["start_date"] for item in collected.values()} == {"2026-10-20", "2026-10-27"}


def test_partial_typed_date_requires_explicit_precision():
    item = _observation("page_information", "2026-09", record_id="unqualified-month")
    item["evidence"]["date_kind"] = "updated"
    with pytest.raises(ArticleDateImportError, match="state its precision"):
        from climate_registry.article_dates import validate_observations
        validate_observations(_payload(item))


def test_public_date_evidence_retains_precision():
    from climate_registry.publication import _date_evidence_dto

    assert _date_evidence_dto({"date_kind": "revised", "date_precision": "month", "source_url": "https://example.org/source", "target_url": "https://example.org/a"}) == {
        "date_kind": "revised", "date_precision": "month", "source_url": "https://example.org/source", "target_url": "https://example.org/a"
    }


def test_pdf_article_typed_dates_are_staged_and_exposed_safely(tmp_path):
    from climate_registry.publication import _pdf_dto, snapshot_entity
    from climate_registry.read_api import RegistryReader

    database = _database(tmp_path / "registry.sqlite3")
    with sqlite3.connect(database) as connection:
        _pdf_article(connection)
    updated = _observation("page_information", "2026-10", record_id="pdf-updated")
    updated.update(article_id="pdf-a", canonical_url="https://pdf.example/a")
    updated["evidence"].update(date_kind="updated", date_precision="month", database="local-private.json")
    revised = _observation("page_information", "2026-09-14", record_id="pdf-revised")
    revised.update(article_id="pdf-a", canonical_url="https://pdf.example/a")
    revised["evidence"].update(date_kind="revised", date_precision="day")

    assert import_observations(database, _payload(updated, revised), entity_kind="pdf_article", write=True) == {
        "inserted": 2, "already_present": 0
    }
    with sqlite3.connect(database) as connection:
        stored = connection.execute("""SELECT observation_kind,observed_at,evidence_json
            FROM pdf_article_date_observations ORDER BY observed_at""").fetchall()
    assert [row[1] for row in stored] == ["2026-09-14", "2026-10"]
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    with reader.public_snapshot(direct=True):
        with reader.connect() as connection:
            snapshot = snapshot_entity(connection, "pdf_article", "pdf-a")
        detail = reader._pdf_article_from_snapshot(snapshot)
    assert detail["updated_date"] == "2026-10"
    assert detail["revision_date"] == "2026-09-14"
    public = _pdf_dto(detail)
    revised_public = next(item for item in public["date_observations"] if item["observed_at"] == "2026-09-14")
    assert revised_public["evidence"]["date_precision"] == "day"
    assert all("database" not in item["evidence"] for item in public["date_observations"])


def test_pdf_date_observations_require_typed_page_information(tmp_path):
    database = _database(tmp_path / "registry.sqlite3")
    with sqlite3.connect(database) as connection:
        _pdf_article(connection)
    untyped = _observation("page_information", "2026-10-01", record_id="pdf-untyped")
    untyped.update(article_id="pdf-a", canonical_url="https://pdf.example/a")
    with pytest.raises(ArticleDateImportError, match="typed page_information"):
        import_observations(database, _payload(untyped), entity_kind="pdf_article")


def test_linked_pdf_dates_flow_to_the_core_article_detail_and_card(tmp_path):
    from climate_registry.publication import snapshot_entity
    from climate_registry.read_api import RegistryReader

    database = _database(tmp_path / "registry.sqlite3")
    with sqlite3.connect(database) as connection:
        _pdf_article(connection, article_id="pdf-a", url="https://example.org/a", core_article_id="article-a")
    observations = []
    for record_id, kind, value, precision in (
        ("pub", "published", "2026-09", "month"),
        ("updated", "updated", "2026-10-15", "day"),
        ("revised", "revised", "2026-08", "month"),
    ):
        item = _observation("page_information", value, record_id=record_id)
        item.update(article_id="pdf-a", canonical_url="https://example.org/a")
        item["evidence"].update(date_kind=kind, date_precision=precision)
        observations.append(item)
    assert import_observations(database, _payload(*observations), entity_kind="pdf_article", write=True) == {
        "inserted": 3, "already_present": 0
    }
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    with reader.public_snapshot(direct=True):
        with reader.connect() as connection:
            snapshot = snapshot_entity(connection, "article", "article-a")
        detail = reader._article_from_snapshot(snapshot)
        card = reader._article_card_from_snapshot(snapshot)
    for payload in (detail, card):
        assert payload["publication_date"] == "2026-09"
        assert payload["updated_date"] == "2026-10-15"
        assert payload["revision_date"] == "2026-08"


def test_pdf_date_import_stages_core_owner_when_core_article_id_is_null(tmp_path):
    from climate_registry.publication import snapshot_entity

    database = _database(tmp_path / "registry.sqlite3")
    with sqlite3.connect(database) as connection:
        _pdf_article(connection, article_id="pdf-a", url="https://example.org/a", core_article_id=None)
    item = _reviewed_typed_observation("updated", "2026-10-15", record_id="pdf-update",
        precision="day")
    item.update(article_id="pdf-a", canonical_url="https://example.org/a")
    assert import_observations(database, _payload(item), entity_kind="pdf_article", write=True) == {
        "inserted": 1, "already_present": 0
    }
    with sqlite3.connect(database) as connection:
        row = connection.execute("""SELECT c.snapshot_json FROM registry_publication p
            JOIN registry_candidates c ON c.candidate_sha256=p.latest_candidate_sha256
            WHERE p.entity_kind='article' AND p.entity_id='article-a'""").fetchone()
        assert row is not None
        snapshot = json.loads(row[0])
    assert len(snapshot["tables"]["pdf_article_date_observations"]) == 1


def test_independently_reviewed_typed_dates_survive_public_approval(tmp_path):
    from climate_registry.read_api import RegistryReader

    database = _database(tmp_path / "registry.sqlite3")
    with sqlite3.connect(database) as connection:
        connection.execute("""INSERT INTO article_versions VALUES(
            'version-a','article-a','Verified article','Verified article','Verified summary',
            'fixture','report-title-summary','2026-01-01','2026-01-01')""")
        connection.execute("UPDATE articles SET current_version_id='version-a' WHERE article_id='article-a'")
    _approve_fixture(database)
    publication = _reviewed_typed_observation("published", "2026-09", record_id="published",
        precision="month")
    updated = _reviewed_typed_observation("updated", "2026-10-15", record_id="updated",
        precision="day")
    assert import_observations(database, _payload(publication, updated), write=True)["inserted"] == 2
    _approve_fixture(database)

    reader = RegistryReader(database, repository_root=tmp_path / "application")
    detail = reader.article("article-a")
    assert detail["publication_date"] == "2026-09"
    assert detail["updated_date"] == "2026-10-15"
    assert detail.get("revision_date") is None
    assert {item["evidence"]["date_kind"] for item in detail["date_observations"]} == {"published", "updated"}


@pytest.mark.parametrize("observed_at", ["2026-01-28T00:00:00", "2026-1-28", "yesterday"])
def test_importer_rejects_unqualified_or_fabricated_precision(tmp_path, observed_at):
    database = _database(tmp_path / "registry.sqlite3")
    item = _observation(
        "collection" if "T" in observed_at else "page_information", observed_at
    )
    with pytest.raises(ArticleDateImportError):
        import_observations(database, _payload(item))


def _report_day(value="2026-09-18", record_id="report-18"):
    item = _observation("collection", value, record_id=record_id)
    item["evidence"].update(date_basis="daily_or_weekly_report_date", report_date=value,
        report_filename=f"climate-monitor-{value}.md",
        note="Monitoring report date; precise collection time and article publication date are unknown.")
    return item


def _approve_fixture(database):
    from climate_registry.publication import stage_entities, _approve
    with sqlite3.connect(database) as connection:
        from verified_fixtures import seed_verified_articles
        seed_verified_articles(connection)
        for sha in stage_entities(connection, annotations={}):
            _approve(connection, sha, {"basis":"explicit historical date fixture"}, status="accepted_legacy")
        from climate_registry.display import refresh_display
        refresh_display(connection)


@pytest.mark.parametrize("target", [18, 19, 20])
def test_legacy_date_preview_is_read_only_and_write_requires_explicit_publication_migration(tmp_path, capsys, target):
    from climate_registry.errors import RegistryInputError
    from scripts.backfill_article_dates import main
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        apply_migrations(connection, target_version=target)
        connection.execute("INSERT INTO sources VALUES ('s','example.org','Example','2026-01-01','2026-01-01')")
        connection.execute("""INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen)
            VALUES ('article-a','https://example.org/a','s','2026-01-01','2026-01-01')""")
    original = database.read_bytes(), database.stat().st_ino
    payload = _payload(_report_day())
    assert import_observations(database, payload) == {"inserted":1,"already_present":0}
    with pytest.raises(RegistryInputError, match="migrate-publication"):
        import_observations(database, payload, write=True)
    input_path = tmp_path / "dates.json"
    input_path.write_text(json.dumps(payload))
    assert main(["--database",str(database),"--observations",str(input_path),"--write"]) == 2
    assert "migrate-publication" in capsys.readouterr().err
    assert (database.read_bytes(), database.stat().st_ino) == original
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == target
        assert connection.execute("SELECT count(*) FROM article_date_observations").fetchone()[0] == 0


def test_backfill_cli_obeys_shared_file_lock_before_any_date_or_candidate_write(tmp_path):
    from climate_registry.persistent import _exclusive_database_lock
    database = _database(tmp_path / "registry.sqlite3")
    _approve_fixture(database)
    original = database.read_bytes(), database.stat().st_ino
    payload = tmp_path / "dates.json"
    payload.write_text(json.dumps(_payload(_observation("collection","2026-09-19T12:00:00Z"))))
    with _exclusive_database_lock(database):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/backfill_article_dates.py"),
            "--database",str(database),"--observations",str(payload),"--write"],
            capture_output=True,text=True,timeout=30)
    assert result.returncode == 2
    assert "registry update is locked" in result.stderr
    assert (database.read_bytes(), database.stat().st_ino) == original
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM article_date_observations").fetchone()[0] == 0
        assert connection.execute("SELECT latest_candidate_sha256=published_candidate_sha256 FROM registry_publication").fetchone()[0] == 1


@pytest.mark.parametrize("hidden", [False, True])
def test_date_write_stages_pending_and_preserves_approved_snapshot_and_visibility(tmp_path, hidden):
    from climate_registry.publication import set_visibility
    from climate_registry.read_api import RegistryReader, RegistryNotFoundError
    database = _database(tmp_path / "registry.sqlite3")
    _approve_fixture(database)
    if hidden:
        set_visibility(database,"article","article-a",False)
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    with sqlite3.connect(database) as connection:
        old = connection.execute("SELECT published_candidate_sha256,is_visible FROM registry_publication").fetchone()
        old_snapshot = connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(old[0],)).fetchone()[0]
    assert import_observations(database,_payload(_report_day()),write=True) == {"inserted":1,"already_present":0}
    with sqlite3.connect(database) as connection:
        latest, published, visible = connection.execute("SELECT latest_candidate_sha256,published_candidate_sha256,is_visible FROM registry_publication").fetchone()
        assert latest != published == old[0] and visible == old[1]
        assert connection.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(published,)).fetchone()[0] == old_snapshot
        assert connection.execute("SELECT observed_at,evidence_json FROM article_date_observations").fetchone() == ("2026-09-18", json.dumps(_report_day()["evidence"],ensure_ascii=False,sort_keys=True,separators=(",",":")))
    if hidden:
        with pytest.raises(RegistryNotFoundError): reader.article("article-a")
    else:
        assert reader.article("article-a")["date_basis"] == "unknown"
    before = database.read_bytes()
    assert import_observations(database,_payload(_report_day()),write=True) == {"inserted":0,"already_present":1}
    assert database.read_bytes() == before


def test_report_day_does_not_block_page_information_in_same_payload_or_stored_history(tmp_path):
    database = _database(tmp_path / "registry.sqlite3")
    page = _observation("page_information","2026-09-20")
    assert import_observations(database,_payload(_report_day(),page)) == {"inserted":2,"already_present":0}
    assert import_observations(database,_payload(_report_day()),write=True)["inserted"] == 1
    assert import_observations(database,_payload(page),write=True)["inserted"] == 1
    aware = _observation("collection","2026-09-10T12:00:00Z",record_id="aware-10")
    assert import_observations(database,_payload(aware),write=True)["inserted"] == 1
    with pytest.raises(ArticleDateImportError,match="only when no collection evidence"):
        import_observations(database,_payload(_observation("page_information","2026-09-21",record_id="page-21")))


@pytest.mark.parametrize("value,patch", [
    ("2026-09-18", {"report_date":"2026-09-19"}),
    ("2026-09-18", {"date_basis":"publisher_date"}),
    ("2026-02-30", {}),
    ("2026-09-18T12:00:00", {"report_date":"2026-09-18"}),
])
def test_report_day_import_keeps_strict_precision_and_exact_evidence(tmp_path,value,patch):
    database = _database(tmp_path / "registry.sqlite3")
    before = database.read_bytes()
    item = _report_day(value); item["evidence"].update(patch)
    with pytest.raises(ArticleDateImportError): import_observations(database,_payload(item),write=True)
    assert database.read_bytes() == before


def test_report_day_still_requires_full_source_identity(tmp_path):
    database = _database(tmp_path / "registry.sqlite3")
    item = _report_day();del item["evidence"]["database"]
    with pytest.raises(ArticleDateImportError,match="source identity fields"):
        import_observations(database,_payload(item),write=True)


@pytest.mark.parametrize("more_precise", [None,"collection","page_information"])
def test_approved_date_dto_api_git_render_wiki_and_frontend_share_precision(tmp_path,monkeypatch,more_precise):
    import api_server
    from fastapi.testclient import TestClient
    from climate_registry.read_api import RegistryReader
    from climate_registry.publication import export_public_snapshot,install_git_snapshot,_public_dto
    from climate_registry.publication import public_wiki_pages
    database = _database(tmp_path / "registry.sqlite3")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO article_versions VALUES('version-a','article-a','Verified article','Verified article','Verified summary','fixture','report-title-summary','2026-01-01','2026-01-01')")
        connection.execute("UPDATE articles SET current_version_id='version-a' WHERE article_id='article-a'")
    _approve_fixture(database)
    item = _report_day()
    assert import_observations(database,_payload(item),write=True)["inserted"] == 1
    if more_precise:
        value = "2026-09-10T12:00:00Z" if more_precise == "collection" else "2026-09-20"
        assert import_observations(database,_payload(_observation(more_precise,value,record_id="more-precise")),write=True)["inserted"] == 1
    _approve_fixture(database)  # Exact fixture candidate approval; no native PASS is claimed.
    before = database.read_bytes(), database.stat().st_ino
    reader = RegistryReader(database,repository_root=tmp_path / "application")
    detail = reader.article("article-a")
    private=RegistryReader(database,repository_root=tmp_path/'application',public=False).article('article-a')
    assert private['date_basis']=={None:'report_date','collection':'collection_time','page_information':'information_date'}[more_precise]
    assert next(o for o in private['date_observations'] if o['observed_at']=='2026-09-18')['evidence']==item['evidence']
    expected_basis = 'unknown'
    assert detail["date_basis"] == expected_basis
    assert detail["report_date"] is detail["collected_at"] is detail["information_date"] is None
    assert detail["publication_date"] is None
    assert detail["date_observations"] == []
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    with TestClient(api_server.app) as client:
        response = client.get("/api/registry/articles/article-a")
        assert response.status_code == 200
        assert response.json()["date_basis"] == expected_basis and response.json()["collected_at"] == detail["collected_at"]
    wiki = tmp_path / "application/wiki";wiki.mkdir(parents=True)
    exported = export_public_snapshot(database,wiki / "public-registry.json")
    assert exported["articles"] == 1
    artifact = json.loads((wiki / "public-registry.json").read_text())
    assert artifact["articles"][0] == _public_dto(detail)
    with reader.public_snapshot():
        markdown = public_wiki_pages(database,reader=reader)["article-article-a.md"]
    assert 'Verified summary' in markdown
    assert "Collected at: 2026-09-18" not in markdown
    assert "Report observation date: 2026-09-18" not in markdown
    assert "Information date: 2026-09-20" not in markdown
    restored = tmp_path / "render.sqlite3"
    with sqlite3.connect(restored) as connection:apply_migrations(connection)
    install_git_snapshot(restored,wiki)
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    static = RegistryReader(restored,repository_root=tmp_path / "application")
    assert _public_dto(static.article("article-a")) == artifact["articles"][0]
    assert public_wiki_pages(restored,wiki_dir=wiki,reader=static)["article-article-a.md"] == markdown
    # Run the actual frontend metric block, stubbing only the DOM append boundary.
    javascript = (Path(__file__).resolve().parents[1] / "showcase/app.js").read_text()
    start = javascript.index('    const metrics = [\n      registryMetric("Publisher", article.publisher)')
    end = javascript.index('    const summaryPresentation =',start)
    program = 'const article='+json.dumps(_public_dto(detail))+';const pdfSource=false;const seen=[];const els={registryArticleMeta:{append:(...rows)=>seen.push(...rows)}};const registryMetric=(label,value)=>({label,value});'+javascript[start:end]+';console.log(JSON.stringify(seen));'
    metrics = json.loads(subprocess.run(["node","-e",program],check=True,capture_output=True,text=True).stdout)
    assert not any(m['label'] in {'Report date (publication unconfirmed)','Collected at','Information date','Publication date'} for m in metrics)
    assert (database.read_bytes(),database.stat().st_ino) == before


def test_collection_choice_compares_actual_instants_with_offsets_and_fractional_seconds():
    from climate_registry.article_dates import select_article_dates
    report = _report_day("2026-09-19")
    observation = {"kind":"collection","observed_at":report["observed_at"],"evidence":report["evidence"]}
    fields = select_article_dates([observation], collection_times=[
        "2026-09-18T11:00:00+02:00", "2026-09-18T09:00:00.1Z"])
    assert fields["collected_at"] == "2026-09-18T09:00:00.100000Z"
    assert fields["date_basis"] == "collection_time" and fields["report_date"] == "2026-09-19"
