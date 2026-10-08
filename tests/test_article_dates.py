import sqlite3
import json
import subprocess
import sys
from pathlib import Path

import pytest

from climate_registry.article_dates import ArticleDateImportError, import_observations
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
        for sha in stage_entities(connection, annotations={}):
            _approve(connection, sha, {"basis":"explicit historical date fixture"}, status="accepted_legacy")


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
    expected_basis = {None:"report_date","collection":"collection_time","page_information":"information_date"}[more_precise]
    assert detail["date_basis"] == expected_basis
    assert detail["report_date"] == "2026-09-18"
    assert detail["collected_at"] == ("2026-09-10T12:00:00Z" if more_precise == "collection" else None)
    assert detail["information_date"] == ("2026-09-20" if more_precise == "page_information" else None)
    assert detail["publication_date"] is None
    assert next(o for o in detail["date_observations"] if o["observed_at"]=="2026-09-18")["evidence"] == item["evidence"]
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    with TestClient(api_server.app) as client:
        response = client.get("/api/registry/articles/article-a")
        assert response.status_code == 200
        assert response.json()["date_basis"] == expected_basis and response.json()["collected_at"] == detail["collected_at"]
    wiki = tmp_path / "wiki";wiki.mkdir()
    exported = export_public_snapshot(database,wiki / "public-registry.json")
    assert exported["articles"] == 1
    artifact = json.loads((wiki / "public-registry.json").read_text())
    assert artifact["articles"][0] == _public_dto(detail)
    with reader.public_snapshot():
        markdown = public_wiki_pages(database,reader=reader)["article-article-a.md"]
    label = {None:"Report date: 2026-09-18", "collection":"Collected at: 2026-09-10T12:00:00Z", "page_information":"Information date: 2026-09-20"}[more_precise]
    assert label in markdown
    assert "Collected at: 2026-09-18" not in markdown
    assert "Report observation date: 2026-09-18" in markdown
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
    date_label = {None:"Report date (publication unconfirmed)","collection":"Collected at","page_information":"Information date"}[more_precise]
    assert any(m["label"] == date_label and m["value"] == ("2026-09-18" if more_precise is None else value) for m in metrics)
    if more_precise != "collection":assert not any(m["label"] == "Collected at" for m in metrics)
    assert (database.read_bytes(),database.stat().st_ino) == before


def test_collection_choice_compares_actual_instants_with_offsets_and_fractional_seconds():
    from climate_registry.article_dates import select_article_dates
    report = _report_day("2026-09-19")
    observation = {"kind":"collection","observed_at":report["observed_at"],"evidence":report["evidence"]}
    fields = select_article_dates([observation], collection_times=[
        "2026-09-18T11:00:00+02:00", "2026-09-18T09:00:00.1Z"])
    assert fields["collected_at"] == "2026-09-18T09:00:00.100000Z"
    assert fields["date_basis"] == "collection_time" and fields["report_date"] == "2026-09-19"
