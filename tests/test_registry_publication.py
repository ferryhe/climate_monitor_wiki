"""Public consumers use the exact approved version during later improvement."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from climate_registry.acquisition_review import digest
from climate_registry.contract import validate_registry_contract
from climate_registry.publication import (
    _approve, migrate_publication, resolve_database, set_visibility,
    snapshot_entity, stage_entities, stage_snapshot,
)
from climate_registry.read_api import RegistryNotFoundError, RegistryReader
from climate_registry.schema import apply_migrations


def _database(tmp_path, version=22):
    database = tmp_path / "external" / "registry.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        apply_migrations(db, target_version=version)
        db.execute("INSERT INTO sources VALUES('s','example.org','Publisher','2026-09-01','2026-09-01')")
        for identity in ("a", "b"):
            db.execute("INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen) VALUES(?,?, 's','2026-09-01','2026-09-01')", (identity, "https://example.org/" + identity))
            db.execute("INSERT INTO article_versions VALUES(?,?,?,?,?,?,?,'2026-09-01','2026-09-01')", ("v-"+identity, identity, "Title "+identity, "Title "+identity, "Observed summary", "f-"+identity, "report-title-summary"))
            db.execute("UPDATE articles SET current_version_id=? WHERE article_id=?", ("v-"+identity, identity))
    return database


def _reader(database, tmp_path):
    return RegistryReader(database, repository_root=tmp_path / "application")


def test_first_pending_partial_publication_and_visibility(tmp_path):
    database = _database(tmp_path)
    reader = _reader(database, tmp_path)
    with sqlite3.connect(database) as db:
        assert validate_registry_contract(db) == 22
        stage_entities(db)
        sha = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_id='a'").fetchone()[0]
    assert reader.articles()["items"] == []
    assert reader.publishers()["items"] == []
    with pytest.raises(RegistryNotFoundError):
        reader.article("a")
    with sqlite3.connect(database) as db:
        _approve(db, sha, {"basis": "test verified receipt"}, status="accepted_legacy")
    assert [item["article_id"] for item in reader.articles()["items"]] == ["a"]
    assert reader.article("a")["title"] == "Title a"
    assert reader.articles(query=" Title a ",source=" s ")["pagination"]["total"]==1
    set_visibility(database, "article", "a", False)
    assert reader.articles()["items"] == []
    assert reader.publishers()["items"] == []
    with pytest.raises(RegistryNotFoundError):
        reader.article("a")
    set_visibility(database, "article", "a", True)
    assert reader.article("a")["published_candidate_sha256"] == sha


def test_exact_metadata_change_keeps_old_public_version_and_revokes_pass(tmp_path):
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        first = stage_snapshot(db, snapshot_entity(db, "article", "a"))
        _approve(db, first, {"basis": "test verified receipt"}, status="accepted_legacy")
        db.execute("UPDATE sources SET display_name='New publisher' WHERE source_id='s'")
        second = stage_snapshot(db, snapshot_entity(db, "article", "a"))
        assert first != second
        with pytest.raises(ValueError, match="another final review"):
            _approve(db, first, {"basis": "stale receipt"}, status="accepted_legacy")
    reader = _reader(database, tmp_path)
    assert reader.article("a")["publisher"] == "Publisher"
    assert reader.article("a")["published_candidate_sha256"] == first
    with sqlite3.connect(database) as db:
        _approve(db, second, {"basis": "new exact receipt"}, status="accepted_legacy")
    assert reader.article("a")["publisher"] == "New publisher"


def test_exact_raw_body_changes_even_when_display_text_matches(tmp_path):
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        for version, body in (("c1", "same body"), ("c2", "same  body")):
            sha = hashlib.sha256(body.encode()).hexdigest()
            db.execute("INSERT INTO article_content_versions VALUES(?,?,?,?,?,'text/markdown',?,'test','v1','2026-09-01T00:00:00Z')", (version, "a", sha, body, sha, len(body)))
            db.execute("UPDATE articles SET current_content_version_id=? WHERE article_id='a'", (version,))
            candidate = stage_snapshot(db, snapshot_entity(db, "article", "a"))
            if version == "c1":
                first = candidate
                _approve(db, first, {"basis": "test exact receipt"}, status="accepted_legacy")
        assert candidate != first
    reader = _reader(database, tmp_path)
    assert reader.article("a")["content"]["content_version_id"] == "c1"
    assert reader.article("a")["available_content"]["content_version_id"] == "c1"


def test_legacy_migration_backup_and_idempotency(tmp_path):
    database = _database(tmp_path, version=19)
    original = database.read_bytes()
    dry = migrate_publication(database, tmp_path / "backups")
    assert dry["status"] == "dry_run" and database.read_bytes() == original
    applied = migrate_publication(database, tmp_path / "backups", apply=True)
    assert applied["accepted_legacy"] == 2
    assert Path(applied["backup"]).read_bytes() == original
    with sqlite3.connect(database) as db:
        before = db.execute("SELECT * FROM registry_publication ORDER BY entity_id").fetchall()
        candidates = db.execute("SELECT count(*) FROM registry_candidates").fetchone()[0]
    repeated = migrate_publication(database, tmp_path / "backups", apply=True)
    assert repeated["accepted_legacy"] == 0 and repeated["manual_materialized"] == 0
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM registry_publication ORDER BY entity_id").fetchall() == before
        assert db.execute("SELECT count(*) FROM registry_candidates").fetchone()[0] == candidates
    assert _reader(database, tmp_path).articles()["pagination"]["total"] == 2


@pytest.mark.parametrize("version",range(3,21))
@pytest.mark.parametrize("populated",[False,True])
def test_every_legacy_publication_preview_preserves_file_then_explicitly_migrates(tmp_path,version,populated):
    database=_database(tmp_path,version=version)
    if not populated:
        with sqlite3.connect(database) as db:
            db.execute("DELETE FROM article_versions");db.execute("DELETE FROM articles");db.execute("DELETE FROM sources")
    before=database.read_bytes();inode=database.stat().st_ino
    result=migrate_publication(database,tmp_path/"backups")
    assert result=={"status":"dry_run","schema_version":version,"target_version":22,"core_articles":2 if populated else 0,"pdf_articles":0}
    assert database.read_bytes()==before and database.stat().st_ino==inode and not (tmp_path/"backups").exists()
    migrated=migrate_publication(database,tmp_path/"backups",apply=True)
    assert migrated["accepted_legacy"]==(2 if populated else 0)
    assert Path(migrated["backup"]).read_bytes()==before
    assert _reader(database,tmp_path).articles()["pagination"]["total"]==(2 if populated else 0)
    with sqlite3.connect(database) as db:
        assert validate_registry_contract(db)==22 and db.execute("PRAGMA foreign_key_check").fetchall()==[]
        publications=db.execute("SELECT * FROM registry_publication ORDER BY entity_id").fetchall()
    repeated=migrate_publication(database,tmp_path/"backups",apply=True)
    assert repeated["accepted_legacy"]==0 and repeated["manual_materialized"]==0
    with sqlite3.connect(database) as db:assert db.execute("SELECT * FROM registry_publication ORDER BY entity_id").fetchall()==publications


@pytest.mark.parametrize("writer",["information_checks","pdf_intake","update","semantic_import"])
@pytest.mark.parametrize("version",[19,21])
def test_existing_legacy_writers_require_explicit_publication_migration(tmp_path,writer,version):
    from climate_registry.errors import RegistryInputError
    from climate_registry.information_checks import run_checks
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.persistent import update_registry
    from climate_registry.semantic_import import _apply
    database=_database(tmp_path,version=version)
    if version==21:
        with sqlite3.connect(database) as db:
            for sha in stage_entities(db):_approve(db,sha,{"basis":"original21 accepted public fixture"},status="accepted_legacy")
    reader=_reader(database,tmp_path)
    assert reader.articles()["pagination"]["total"]==2
    before=database.read_bytes()
    backups=tmp_path/"writer-backups"
    sources=tmp_path/"sources";sources.mkdir()
    calls=[]
    def unexpected(*args,**kwargs):
        calls.append((args,kwargs))
        pytest.fail("legacy writer reached fetch/model before explicit migration")
    operations={
        "information_checks":lambda:run_checks(database,kind="articles",backup_dir=backups,fetcher=unexpected,verifier=unexpected),
        "pdf_intake":lambda:persist_pdf_intake(database,backups,{"schema_version":"climate-pdf-intake.v1","documents":[],"articles":[],"calendar_items":[]}),
        "update":lambda:update_registry(sources,database,backups),
        "semantic_import":lambda:_apply("unused",[],database,backups,target={},report_date="2026-09-01",report_filename="unused.md",sidecar_count=0),
    }
    with pytest.raises(RegistryInputError,match="migrate-publication --apply"):
        operations[writer]()
    assert database.read_bytes()==before and reader.articles()["pagination"]["total"]==2
    assert not backups.exists() and not calls
    assert not list(database.parent.glob("*.candidate")) and not list(database.parent.glob("*.semantic-candidate"))
    applied=migrate_publication(database,tmp_path/"migration-backups",apply=True)
    assert applied["accepted_legacy"]==(2 if version==19 else 0) and reader.articles()["pagination"]["total"]==2
    assert migrate_publication(database,tmp_path/"migration-backups",apply=True)["accepted_legacy"]==0
    result=run_checks(database,kind="articles",backup_dir=backups,occurrence_ids=set(),fetcher=unexpected,verifier=unexpected)
    assert result["item_count"]==0 and result["status"]=="complete" and not calls
    assert reader.articles()["pagination"]["total"]==2


def test_resume_checks_configured_database_but_explicit_rehearsal_is_supported(tmp_path, monkeypatch):
    database = _database(tmp_path)
    second = database.with_name("rehearsal.sqlite3")
    second.write_bytes(database.read_bytes())
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    assert resolve_database(second) == second
    with pytest.raises(ValueError, match="frozen task binding"):
        resolve_database(frozen=second)


@pytest.mark.parametrize("continuation",["resume","retry"])
def test_information_check_continuation_is_bound_to_original_database(tmp_path,monkeypatch,continuation):
    from test_information_checks import _database as check_database,_record,_judgment
    from climate_registry.information_checks import run_checks
    database=check_database(tmp_path)
    migrate_publication(database,tmp_path/"backups",apply=True)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    first=run_checks(database,kind="articles",backup_dir=tmp_path/"backups",limit=1,fetcher=_record,verifier=_judgment)
    assert first["status"]=="pending"
    with sqlite3.connect(database) as db:
        frozen_json,frozen_sha=db.execute("SELECT input_json,input_sha256 FROM article_check_runs WHERE run_id=?",(first["run_id"],)).fetchone()
    db.close()
    assert hashlib.sha256(frozen_json.encode()).hexdigest()==frozen_sha
    assert json.loads(frozen_json)["registry_database"]==str(database.resolve())
    other=tmp_path/"copy.sqlite3";other.write_bytes(database.read_bytes())
    before_a,before_b=database.read_bytes(),other.read_bytes()
    calls=[]
    def unexpected(*args,**kwargs):
        calls.append((args,kwargs));pytest.fail("switched continuation reached fetch/model")
    kwargs={continuation+"_run_id":first["run_id"]}
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(other))
    for selected in (other,database):
        with pytest.raises(ValueError,match="frozen task binding"):
            run_checks(selected,kind="articles",backup_dir=tmp_path/"backups",fetcher=unexpected,verifier=unexpected,**kwargs)
        assert database.read_bytes()==before_a and other.read_bytes()==before_b and not calls
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    continued=run_checks(database,kind="articles",backup_dir=tmp_path/"backups",fetcher=_record,verifier=_judgment,**kwargs)
    assert continued["status"]!="pending"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT input_json,input_sha256 FROM article_check_runs WHERE run_id=?",(first["run_id"],)).fetchone()==(frozen_json,frozen_sha)


@pytest.mark.parametrize("continuation",["resume","retry"])
@pytest.mark.parametrize("legacy",[False,True])
def test_information_check_continuation_rejects_unbound_or_changed_frozen_input(tmp_path,monkeypatch,continuation,legacy):
    from climate_registry.information_checks import run_checks
    database=_database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    frozen={"schema_version":"information-check.v1","kind":"articles","targets":[],"retry_of":None}
    if not legacy:frozen["registry_database"]=str(database.resolve())
    encoded=json.dumps(frozen,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    sha=hashlib.sha256(encoded.encode()).hexdigest() if legacy else "0"*64
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_check_runs(run_id,input_json,input_sha256,created_at,status,item_count) VALUES('historical',?,?, '2026-09-01','pending',0)",(encoded,sha))
    db.close();before=database.read_bytes()
    expected="no frozen Registry binding" if legacy else "input hash differs"
    with pytest.raises(ValueError,match=expected):
        run_checks(database,kind="articles",backup_dir=tmp_path/"backups",**{continuation+"_run_id":"historical"})
    assert database.read_bytes()==before
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT input_json,input_sha256,status FROM article_check_runs WHERE run_id='historical'").fetchone()==(encoded,sha,"pending")
    result=run_checks(database,kind="articles",backup_dir=tmp_path/"backups",occurrence_ids=set())
    assert result["status"]=="complete" and result["run_id"]!="historical"


@pytest.mark.skipif(os.name=="nt",reason="API management imports POSIX fcntl")
@pytest.mark.parametrize("endpoint",["/api/registry/articles/a","/api/registry/articles?include_pdf=true"])
def test_article_api_uses_one_approved_core_pdf_snapshot(tmp_path,monkeypatch,endpoint):
    from fastapi.testclient import TestClient
    import api_server
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db,occurrence={"summary":"Approved PDF A","summary_basis":"verbatim_pdf_article_row"})
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit approved A fixture"}, status="accepted_legacy")
    db.close()
    other=tmp_path/"other.sqlite3";other.write_bytes(database.read_bytes())
    with sqlite3.connect(other) as db:
        db.execute("UPDATE article_versions SET observed_summary='Approved core B' WHERE article_id='a'")
        db.execute("""INSERT INTO pdf_intake_article_occurrences
            SELECT 'occurrence-b',article_id,source_document_sha256,3,raw_url,'2026-09-02',publication_date,
                ?,page_sha256,? FROM pdf_intake_article_occurrences WHERE occurrence_id='occurrence'""",
            ("b"*64,json.dumps({"summary":"Approved PDF B","summary_basis":"verbatim_pdf_article_row"})))
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit approved B fixture"}, status="accepted_legacy")
    db.close()
    first=_reader(database,tmp_path);second=_reader(other,tmp_path)
    expected=first.article("a")
    calls=[]
    def select():
        calls.append(1)
        return first if len(calls)==1 else second
    monkeypatch.setattr(api_server,"_registry_reader",select)
    response=TestClient(api_server.app).get(endpoint)
    assert response.status_code==200 and len(calls)==1,response.text
    if endpoint.endswith("/a"):
        from climate_registry.publication import _public_dto
        assert response.json()==json.loads(json.dumps(_public_dto(expected)))
        assert response.json()["pdf_occurrences"][0]["summary"]=="Approved PDF A"
    else:
        item=next(item for item in response.json()["items"] if item["article_id"]=="a")
        assert item["summary"]==expected["summary"] and item["pdf_occurrence_count"]==1


def test_native_review_requires_actual_full_snapshot_inspection(tmp_path, monkeypatch):
    from climate_registry.publication import prepare_review, review_candidates
    from climate_registry.acquisition_review import claim_review
    from climate_delivery.io import atomic_write_json
    database = _database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    root = tmp_path / "review"
    _complete_fixture_information(database)
    packet = prepare_review(database, root)
    hermes = tmp_path / "hermes.sqlite3"
    session = "cron_registry_final_1"
    with sqlite3.connect(hermes) as db:
        db.execute("CREATE TABLE sessions(id TEXT,source TEXT,end_reason TEXT)")
        db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,tool_calls TEXT,tool_call_id TEXT,content TEXT)")
        db.execute("INSERT INTO sessions VALUES(?, 'cron', NULL)", (session,))
    claim = claim_review(root, packet, session_id=session, execution_id=session, hermes_database=hermes, reviewer="native")
    candidate = packet["candidates"][0]
    sha = candidate["candidate_sha256"]
    conclusion = {sha: {"candidate_sha256": sha, "status": "pass", "reason": "faithful observed summary"}}
    with pytest.raises(ValueError, match="full text read"):
        review_candidates(root, claim["token"], conclusion)
    with sqlite3.connect(hermes) as db:
        call = {"id": "read-1", "function": {"name": "read_file", "arguments": json.dumps({"path": candidate["snapshot_path"]})}}
        db.execute("INSERT INTO messages(session_id,role,tool_calls) VALUES(?, 'assistant', ?)", (session, json.dumps([call])))
        db.execute("INSERT INTO messages(session_id,role,tool_call_id,content) VALUES(?, 'tool','read-1',?)", (session, Path(candidate["snapshot_path"]).read_text(encoding="utf-8")))
    assert review_candidates(root, claim["token"], conclusion)["reviewed"] == 1
    assert _reader(database, tmp_path).articles()["pagination"]["total"] == 1


def _pdf(db, identity="a", url="https://example.org/a", *, occurrence=None, period=(None,None), run_date=None, document=None):
    db.execute("INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,extracted_text_sha256,document_json,imported_at,period_start,period_end,date_of_run) VALUES('ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff','/evidence/source.pdf','source.pdf','application/pdf',1,'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',?,'2026-08-01T00:00:00Z',?,?,?)",(json.dumps(document or {}),*period,run_date))
    db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES(?,?, 'Original PDF title','2026-08-01T00:00:00Z')", (identity,url))
    db.execute("INSERT INTO pdf_intake_article_occurrences VALUES('occurrence',?,'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',2,?,'2026-08-01',NULL,'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd','cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',?)", (identity,url,json.dumps(occurrence or {"summary":"Original PDF observation", "summary_basis":"verbatim_pdf_article_row"})))


def test_shared_core_pdf_identity_has_one_visibility_and_version(tmp_path):
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db)
        stage_entities(db)
        assert db.execute("SELECT count(*) FROM registry_publication WHERE entity_id='a'").fetchone()[0] == 1
        first = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_id='a'").fetchone()[0]
        _approve(db, first, {"basis":"explicit fixture approval"}, status="accepted_legacy")
        db.execute("UPDATE pdf_intake_articles SET title='Pending PDF title' WHERE article_id='a'")
        stage_entities(db)
    reader = _reader(database, tmp_path)
    assert reader.pdf_article("a")["title"] == "Original PDF title"
    assert reader.article("a")["published_candidate_sha256"] == first
    set_visibility(database,"pdf_article","a",False)
    assert reader.articles()["items"] == []
    assert reader.pdf_articles_all(include_linked=True) == []
    with pytest.raises(RegistryNotFoundError):
        reader.pdf_article("a")
    set_visibility(database,"article","a",True)
    assert reader.pdf_article("a")["title"] == "Original PDF title"


def test_shared_url_with_different_pdf_id_is_one_canonical_flag(tmp_path):
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db, identity="pdf-alias")
        for sha in stage_entities(db):
            _approve(db,sha,{"basis":"explicit fixture approval"}, status="accepted_legacy")
        assert db.execute("SELECT count(*) FROM registry_publication WHERE entity_kind='pdf_article'").fetchone()[0] == 0
    reader = _reader(database,tmp_path)
    assert len(reader.pdf_articles_all(include_linked=True)) == 1
    set_visibility(database,"pdf_article","pdf-alias",False)
    assert reader.pdf_articles_all(include_linked=True) == []
    assert [item["article_id"] for item in reader.articles()["items"]] == ["b"]


def test_approved_annotations_are_frozen_until_new_approval(tmp_path):
    from climate_registry.annotations import ArticleAnnotation
    database = _database(tmp_path)
    url="https://example.org/a"
    annotation = ArticleAnnotation(url,url,"Approved title","original_content","Approved summary",("Climate Risk",),("insurance",),"2026-09-01")
    with sqlite3.connect(database) as db:
        sha = stage_snapshot(db,snapshot_entity(db,"article","a",annotations={url:annotation}))
        _approve(db,sha,{"basis":"explicit fixture approval"}, status="accepted_legacy")
        stage_snapshot(db,snapshot_entity(db,"article","a",annotations={}))
    detail = _reader(database,tmp_path).article("a")
    assert detail["title"] == "Approved title"
    assert detail["summary"] == "Approved summary"


def test_render_keeps_committed_wiki_body_and_exports_stable_public_data(tmp_path,monkeypatch):
    from climate_registry.publication import install_git_snapshot, public_wiki_pages, export_public_snapshot
    database = _database(tmp_path)
    application=tmp_path/"application"; wiki=application/"wiki"; wiki.mkdir(parents=True)
    body="# Approved committed article\n\nRich committed glacier insurance evidence missing from reports.\n"
    (wiki/"article-a.md").write_text(body,encoding="utf-8")
    install_git_snapshot(database,wiki)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    assert public_wiki_pages(database,wiki_dir=wiki)["article-a.md"] == body
    reader=_reader(database,tmp_path)
    assert reader.article("a")["available_content"]["markdown"] == body
    from agentic_wiki.wiki_agent import WikiKnowledgeBase
    kb=WikiKnowledgeBase(wiki,tmp_path/"sources")
    assert any("glacier insurance" in chunk.text for chunk in kb.chunks)
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT")
    one=export_public_snapshot(database,tmp_path/"one.json")
    two=export_public_snapshot(database,tmp_path/"two.json")
    assert one["artifact_sha256"] == two["artifact_sha256"]
    value=json.loads((tmp_path/"one.json").read_text(encoding="utf-8"))
    assert "registry_candidates" not in value and "registry_reviews" not in value
    assert all("latest_fetch" not in item for item in value["articles"])


def _complete_fixture_information(database,*,entity_ids=()):
    """Real T1 path, with honest unavailable fixture sources; never a native production pass."""
    from climate_registry.information_checks import run_checks
    from climate_registry.publication import _information_groups,information_ready
    migrate_publication(database,database.parent/"fixture-publication-backups",apply=True)
    with sqlite3.connect(database) as db:
        stage_entities(db)
        groups = _information_groups(db)
        waiting = []
        for sha,encoded in db.execute("""SELECT c.candidate_sha256,c.snapshot_json FROM registry_publication p
            JOIN registry_candidates c ON c.candidate_sha256=p.latest_candidate_sha256
            WHERE p.published_candidate_sha256 IS NOT p.latest_candidate_sha256 OR p.entity_id IN ("""+",".join("?" for _ in entity_ids)+")",tuple(entity_ids)):
            snapshot = json.loads(encoded)
            if not information_ready(db,snapshot,information_groups=groups):
                key=(snapshot["entity_kind"],snapshot["entity_id"])
                waiting.extend(target for target in snapshot["information_targets"] if not information_ready(db,
                    {**snapshot,"information_targets":[target]},information_groups={key:[target]}))
    def unavailable(*_):
        raise RuntimeError("explicit test fixture source unavailable")
    for kind in ("articles","meetings"):
        ids = {target["occurrence_id"] for target in waiting if target["check_kind"]==kind}
        if ids:
            result = run_checks(database,kind=kind,backup_dir=database.parent/"fixture-t1-backups",occurrence_ids=ids,fetcher=unavailable)
            assert result["status"]=="partial" and result["completed_count"]==result["item_count"]


def _approve_native_packet(database,root,hermes,session,*,statuses=None):
    from climate_registry.publication import prepare_review,review_candidates
    from climate_registry.acquisition_review import claim_review
    _complete_fixture_information(database)
    packet=prepare_review(database,root)
    with sqlite3.connect(hermes) as db:
        db.execute("CREATE TABLE sessions(id TEXT,source TEXT,end_reason TEXT)")
        db.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,tool_calls TEXT,tool_call_id TEXT,content TEXT)")
        db.execute("INSERT INTO sessions VALUES(?,'cron',NULL)",(session,))
        for item in packet["candidates"]:
            path=item["snapshot_path"]
            call=item["candidate_sha256"]
            db.execute("INSERT INTO messages(session_id,role,tool_calls) VALUES(?,'assistant',?)",(session,json.dumps([{"id":call,"function":{"name":"read_file","arguments":json.dumps({"path":path})}}])))
            db.execute("INSERT INTO messages(session_id,role,tool_call_id,content) VALUES(?,'tool',?,?)",(session,call,Path(path).read_text(encoding="utf-8")))
    claim=claim_review(root,packet,session_id=session,execution_id=session,hermes_database=hermes,reviewer="native-test")
    return review_candidates(root,claim["token"],{item["candidate_sha256"]:{"candidate_sha256":item["candidate_sha256"],"status":(statuses or {}).get(item["entity_id"],"pass"),"reason":"Actual full snapshot review conclusion"} for item in packet["candidates"]})


@pytest.mark.skipif(os.name == "nt", reason="governed acquisition fixtures require POSIX fcntl")
def test_current_acquisition_writers_require_explicit_publication_migration(tmp_path,monkeypatch):
    from test_issue112_acquisition import _batch,_item
    from climate_registry.acquisition import store_acquisition_batch,freeze_acquisition_for_report,load_acquisition_batch
    from climate_registry.errors import RegistryInputError
    database=_database(tmp_path,version=19)
    payload=_batch([_item(url="https://new.example.org/article")])
    before=database.read_bytes()
    assert _reader(database,tmp_path).articles()["pagination"]["total"]==2
    with pytest.raises(RegistryInputError,match="migrate-publication"):
        store_acquisition_batch(database,payload)
    assert database.read_bytes()==before
    # Seed only historical SQL facts, not a present-day low-schema writer.
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO acquisition_batches VALUES(?,?,?,?,?,?,?,?,?,?)",("legacy","pre-report-acquisition-batch.v1","2026-09-10","2026-09-10T08:00:00Z","2026-09-10T08:00:00Z",'{"mode":"unlimited"}',"no_search","historical fixture","a"*64,None))
    db.close();before=database.read_bytes()
    assert load_acquisition_batch(database,"legacy")["items"]==[]
    with pytest.raises(RegistryInputError,match="migrate-publication"):
        freeze_acquisition_for_report(database,"legacy",report_date="2026-09-10")
    assert database.read_bytes()==before and _reader(database,tmp_path).articles()["pagination"]["total"]==2
    migrate_publication(database,tmp_path/"backups",apply=True)
    store_acquisition_batch(database,payload)
    freeze_acquisition_for_report(database,"batch-112",report_date="2026-09-10")
    assert _reader(database,tmp_path).articles()["pagination"]["total"]==2  # New intake is pending.


@pytest.mark.skipif(os.name == "nt", reason="management locking requires POSIX fcntl")
def test_new_task_rejects_old_business_database_before_run_state_or_launcher(tmp_path,monkeypatch):
    from test_issue94_management_console import _definition,_store
    from climate_monitor.management import ManagementService
    definition=_definition(tmp_path)
    database=_database(tmp_path,version=19)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    store=_store(tmp_path);store.save(definition,actor="operator")
    calls=[];service=ManagementService(store=store,runtime_root=tmp_path/"runs",launcher=lambda binding:calls.append(binding) or 123)
    before=database.read_bytes();state=store.active_path.read_bytes()
    for trigger in ("manual","scheduled"):
        with pytest.raises(ValueError,match="schema 22.*migrate-publication"):
            service.start(trigger=trigger)
        assert not calls and store.active_path.read_bytes()==state and database.read_bytes()==before
        assert not list((tmp_path/"runs").rglob("*.json"))
    migrate_publication(database,tmp_path/"backups",apply=True)
    assert service.start(trigger="manual")["accepted"] and len(calls)==1


@pytest.mark.parametrize("statuses,hidden,expected,approved,published",[
    ({},False,"published",2,2),({},True,"approved",2,1),
    ({"b":"needs_correction"},False,"partially_published",1,1),
    ({"a":"needs_correction","b":"needs_correction"},False,"needs_correction",0,0),
    ({"a":"rejected","b":"rejected"},False,"no_approved_candidates",0,0),
])
def test_registry_activate_reports_this_native_packet_truthfully(tmp_path,monkeypatch,capsys,statuses,hidden,expected,approved,published):
    from scripts.review_pipeline import main
    database=_database(tmp_path);runs=tmp_path/"runs";root=runs/"registry-review"
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    with sqlite3.connect(database) as db:stage_entities(db)
    if hidden:set_visibility(database,"article","a",False)
    # Real fixture full-file reads and claim/review receipts, with no fabricated activation pass.
    _approve_native_packet(database,root,tmp_path/"hermes.sqlite3","cron_activation",statuses=statuses)
    packet_before=(root/"packet.json").read_bytes();before=database.read_bytes()
    queue=tmp_path/"queue";queue.mkdir()
    code=main(["activate","--kind","acquisition","--root",str(runs),"--target","registry-review","--queue-dir",str(queue)])
    result=json.loads(capsys.readouterr().out)
    assert result["status"]==expected and result["approved_count"]==approved and result["published_count"]==published
    assert result["reviewed_count"]==2 and result["queued_count"]==0 and not list(queue.rglob("*"))
    assert code==int(approved==0) and database.read_bytes()==before and (root/"packet.json").read_bytes()==packet_before


def test_registry_activate_does_not_reuse_a_prior_round_approval(tmp_path,monkeypatch,capsys):
    from scripts.review_pipeline import main
    from climate_registry.publication import prepare_review
    database=_database(tmp_path);runs=tmp_path/"runs";root=runs/"registry-review"
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    _approve_native_packet(database,root,tmp_path/"hermes.sqlite3","cron_prior_activation")
    with sqlite3.connect(database) as db:
        db.execute("UPDATE article_versions SET observed_summary='New pending revision' WHERE article_id='a'")
    _complete_fixture_information(database)
    packet=prepare_review(database,root)
    assert len(packet["candidates"])==1
    queue=tmp_path/"queue";queue.mkdir()
    assert main(["activate","--kind","acquisition","--root",str(runs),"--target","registry-review","--queue-dir",str(queue)])==1
    result=json.loads(capsys.readouterr().out)
    assert result["status"]=="pending_review" and result["approved_count"]==result["published_count"]==0


@pytest.mark.skipif(os.name == "nt", reason="governed acquisition fixtures require POSIX fcntl")
@pytest.mark.parametrize("source_kind",["site","search"])
def test_new_web_and_search_writer_wait_for_native_approval(tmp_path,monkeypatch,source_kind):
    from test_issue112_acquisition import _batch,_item
    from climate_registry.acquisition import store_acquisition_batch,freeze_acquisition_for_report
    from climate_registry.web_ingest_pipeline import enqueue_web_activation
    from climate_registry.pdf_pipeline import PdfIntakePipeline
    from climate_delivery.io import atomic_write_json
    database=tmp_path/"registry.sqlite3"
    with sqlite3.connect(database) as db: apply_migrations(db)
    application=tmp_path/"application"; application.mkdir()
    runs=tmp_path/"runs"; queue=tmp_path/"queue"; runtime=tmp_path/"runtime"
    for directory in (runs,queue,runtime): directory.mkdir()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    monkeypatch.setenv("CLIMATE_ACQUISITION_RUN_DIR",str(runs))
    item=_item(discovery_kind=source_kind,discovery_ref="result-1" if source_kind=="search" else "site-1",discovery_search_ref="search-1" if source_kind=="search" else None,published_date=None)
    payload=_batch([item])
    store_acquisition_batch(database,payload)
    frozen=freeze_acquisition_for_report(database,payload["batch_id"],report_date=payload["report_date"])
    enqueue_web_activation(queue,database,payload["batch_id"],frozen_payload_sha256=digest(frozen),repository_root=application)
    def reload(generation):
        pending=json.loads((queue/"pending.json").read_text(encoding="utf-8"))
        atomic_write_json(queue/"active.json",pending)
    writer=PdfIntakePipeline(queue,database,tmp_path/"backups",runtime,reload,repository_root=application)
    pending=writer.process_next()
    assert pending["stage"] == "pending_review" and not pending["chat_ready"]
    assert _reader(database,tmp_path).articles()["items"] == []
    _approve_native_packet(database,runs/"registry-review",tmp_path/"hermes.sqlite3","cron_"+source_kind)
    ready=writer.process_next()
    assert ready["chat_ready"] is True
    assert _reader(database,tmp_path).articles()["pagination"]["total"] == 1
    assert any("Evidence" in page.read_text(encoding="utf-8") for page in (runtime/"generations"/ready["generation_id"]).glob("article-*.md"))


@pytest.mark.skipif(os.name == "nt", reason="governed acquisition fixtures require POSIX fcntl")
@pytest.mark.parametrize("stage",["queued","pending_review","failed"])
def test_web_activation_binding_precedes_all_task_mutations(tmp_path,monkeypatch,stage):
    from test_issue112_acquisition import _batch,_item
    from climate_registry.acquisition import store_acquisition_batch,freeze_acquisition_for_report
    from climate_registry.publication import digest
    from climate_registry.web_ingest_pipeline import enqueue_web_activation,WebIngestPipeline
    from climate_registry.pdf_pipeline import PdfIntakePipeline
    from climate_delivery.io import atomic_write_json
    database=tmp_path/"registry.sqlite3"
    with sqlite3.connect(database) as db:apply_migrations(db)
    db.close()
    application=tmp_path/"application";application.mkdir()
    queue=tmp_path/"queue";runtime=tmp_path/"runtime"
    queue.mkdir();runtime.mkdir()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    monkeypatch.delenv("CLIMATE_ACQUISITION_RUN_DIR",raising=False)
    payload=_batch([_item(published_date=None)])
    store_acquisition_batch(database,payload)
    frozen=freeze_acquisition_for_report(database,payload["batch_id"],report_date=payload["report_date"])
    sha=digest(frozen)
    enqueue=lambda target:enqueue_web_activation(queue,target,payload["batch_id"],frozen_payload_sha256=sha,repository_root=application)
    queued=enqueue(database)
    writer=PdfIntakePipeline(queue,database,tmp_path/"backups",runtime,lambda generation:None,repository_root=application)
    if stage=="pending_review":
        assert writer.process_next()["stage"]=="pending_review"
    if stage=="failed":
        # Exercise the normal failed activation -> enqueue retry path.
        with sqlite3.connect(database) as db:
            for candidate in stage_entities(db):_approve(db,candidate,{"basis":"explicit approved web fixture"}, status="accepted_legacy")
        def fail_reload(generation):raise OSError("reload unavailable")
        writer.reload_chat=fail_reload
        failed=writer.process_next()
        assert failed["stage"]=="failed"
        assert enqueue(database)["stage"]=="failed"
        assert list((queue/"web").glob("*/retry.json"))
    other=tmp_path/"copy.sqlite3";other.write_bytes(database.read_bytes())
    files=lambda:{str(p):p.read_bytes() for root in (queue,runtime) for p in root.rglob("*") if p.is_file()}
    before=files();before_a,before_b=database.read_bytes(),other.read_bytes()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(other))
    wrong=PdfIntakePipeline(queue,other,tmp_path/"backups",runtime,lambda generation:None,repository_root=application)
    for operation in (wrong.process_next,writer.process_next,
            lambda:WebIngestPipeline(queue,other,runtime,lambda generation:None,repository_root=application).process(queued["batch_id"]),
            lambda:WebIngestPipeline(queue,database,runtime,lambda generation:None,repository_root=application).process(queued["batch_id"]),
            lambda:enqueue(other),lambda:enqueue(database)):
        with pytest.raises(ValueError,match="frozen task binding"):operation()
        assert files()==before and database.read_bytes()==before_a and other.read_bytes()==before_b
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    if stage=="queued":
        assert writer.process_next()["stage"]=="pending_review"
    elif stage=="pending_review":
        assert writer.process_next() is None  # Same database legitimately awaits approval.
    else:
        def reload(generation):atomic_write_json(queue/"active.json",json.loads((queue/"pending.json").read_text(encoding="utf-8")))
        writer.reload_chat=reload
        assert writer.process_next()["chat_ready"]


@pytest.mark.skipif(os.name == "nt", reason="governed acquisition fixtures require POSIX fcntl")
@pytest.mark.parametrize("version",[19,21])
@pytest.mark.parametrize("has_binding",[False,True])
def test_historical_web_queue_binding_is_checked_independently_of_schema(tmp_path,monkeypatch,version,has_binding):
    from test_issue112_acquisition import _batch,_item
    from climate_registry.acquisition import store_acquisition_batch,freeze_acquisition_for_report
    from climate_registry.web_ingest_pipeline import enqueue_web_activation,read_web_activation_request,WebIngestPipeline
    from climate_registry.pdf_pipeline import PdfIntakePipeline
    from climate_delivery.io import atomic_write_json
    if version==19:
        database=_historical_selected_acquisition(tmp_path)
    else:
        database=tmp_path/"old.sqlite3"
        with sqlite3.connect(database) as db:apply_migrations(db)
        db.close()
    application=tmp_path/"application";application.mkdir()
    queue=tmp_path/"queue";runtime=tmp_path/"runtime";queue.mkdir();runtime.mkdir()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    monkeypatch.delenv("CLIMATE_ACQUISITION_RUN_DIR",raising=False)
    payload=_batch([_item()])
    if version==19:
        from climate_registry.acquisition import load_acquisition_batch
        payload["batch_id"]="selected"
        frozen=load_acquisition_batch(database,"selected")
    else:
        store_acquisition_batch(database,payload)
        frozen=freeze_acquisition_for_report(database,payload["batch_id"],report_date=payload["report_date"])
    enqueue=lambda db:enqueue_web_activation(queue,db,payload["batch_id"],frozen_payload_sha256=digest(frozen),repository_root=application)
    status=enqueue(database)
    request_path=next((queue/"web").glob("*/request.json"))
    if not has_binding:
        request=json.loads(request_path.read_text(encoding="utf-8"));request.pop("source_registry_database")
        atomic_write_json(request_path,request)
    assert read_web_activation_request(queue,payload["batch_id"])["batch_id"]==status["batch_id"]
    other=tmp_path/"other.sqlite3"
    with sqlite3.connect(other) as db:apply_migrations(db)
    db.close()
    files=lambda:{str(p):p.read_bytes() for root in (queue,runtime) for p in root.rglob("*") if p.is_file()}
    before=files();before_a,before_b=database.read_bytes(),other.read_bytes();reloads=[]
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(other))
    writer=PdfIntakePipeline(queue,other,tmp_path/"backups",runtime,reloads.append,repository_root=application)
    for operation in (writer.process_next,
            lambda:WebIngestPipeline(queue,database,runtime,reloads.append,repository_root=application).process(status["batch_id"]),
            lambda:enqueue(database)):
        with pytest.raises(ValueError,match="frozen task binding" if has_binding else "no frozen Registry binding"):operation()
        assert files()==before and not reloads
        assert database.read_bytes()==before_a and other.read_bytes()==before_b
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    same=PdfIntakePipeline(queue,database,tmp_path/"backups",runtime,reloads.append,repository_root=application)
    if has_binding:
        if version==19:
            def reload(generation):atomic_write_json(queue/"active.json",json.loads((queue/"pending.json").read_text(encoding="utf-8")))
            same.reload_chat=reload
            assert same.process_next()["chat_ready"]
        else:
            assert same.process_next()["stage"]=="pending_review"
    else:
        with pytest.raises(ValueError,match="no frozen Registry binding"):same.process_next()
        assert files()==before and database.read_bytes()==before_a


def _historical_selected_acquisition(tmp_path):
    """Construct historical SQL facts without calling a current writer on schema19."""
    from test_issue136_meetings import _database as historical_database
    database=historical_database(tmp_path,["Historical full climate article evidence."],target_version=19)
    with sqlite3.connect(database) as db:
        db.row_factory=sqlite3.Row
        db.execute("INSERT INTO acquisition_batches VALUES(?,?,?,?,?,?,?,?,?,?)",("selected","pre-report-acquisition-batch.v1","2026-01-05","2026-01-05T00:00:00Z","2026-01-05T00:10:00Z",'{"mode":"unlimited"}',"no_search","historical fixture","b"*64,None))
        fetch=dict(db.execute("SELECT * FROM article_fetches LIMIT 1").fetchone());fetch["fetch_id"]="selected-fetch"
        db.execute(f"INSERT INTO article_fetches({','.join(fetch)}) VALUES({','.join('?' for _ in fetch)})",tuple(fetch.values()))
        item=dict(db.execute("SELECT * FROM acquisition_items LIMIT 1").fetchone())
        item.update(acquisition_item_id="selected-item",batch_id="selected",fetch_id="selected-fetch",publication_date="2026-01-01",
            publication_date_evidence_json=json.dumps({"kind":"publisher","text":"Published 2026-01-01"}),date_status="eligible",selection_status="selected",selection_reason="historically selected")
        db.execute(f"INSERT INTO acquisition_items({','.join(item)}) VALUES({','.join('?' for _ in item)})",tuple(item.values()))
        db.execute("UPDATE acquisition_batches SET frozen_at='2026-01-05T00:10:00Z' WHERE batch_id='selected'")
    db.close()
    return database


@pytest.mark.skipif(os.name == "nt", reason="governed acquisition fixtures require POSIX fcntl")
@pytest.mark.parametrize("visibility",["hidden","pending"])
def test_bound_legacy_web_queue_after_migration_uses_current_public_projection(tmp_path,monkeypatch,visibility):
    from test_issue112_acquisition import _batch,_item
    from climate_registry.acquisition import store_acquisition_batch,freeze_acquisition_for_report,load_acquisition_batch
    from climate_registry.web_ingest_pipeline import enqueue_web_activation,read_web_activation_request
    from climate_registry.pdf_pipeline import PdfIntakePipeline
    from climate_delivery.io import atomic_write_json
    database=_historical_selected_acquisition(tmp_path)
    application=tmp_path/"application";application.mkdir()
    queue=tmp_path/"queue";runtime=tmp_path/"runtime";queue.mkdir();runtime.mkdir()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    monkeypatch.delenv("CLIMATE_ACQUISITION_RUN_DIR",raising=False)
    frozen=load_acquisition_batch(database,"selected")
    queued=enqueue_web_activation(queue,database,"selected",frozen_payload_sha256=digest(frozen),repository_root=application)
    request=read_web_activation_request(queue,queued["batch_id"])
    with sqlite3.connect(request["registry_snapshot_path"]) as db:assert db.execute("PRAGMA user_version").fetchone()[0]==19
    migrate_publication(database,tmp_path/"migration-backups",apply=True)
    identity=load_acquisition_batch(database,"selected")["items"][0]["article_id"]
    if visibility=="hidden":
        set_visibility(database,"article",identity,False)
    else:
        with sqlite3.connect(database) as db:
            candidate=snapshot_entity(db,"article",identity)
            candidate["derived_display"]={"summary":"Unapproved pending correction"}
            stage_snapshot(db,candidate)
    reloads=[]
    def reload(generation):
        reloads.append(generation);atomic_write_json(queue/"active.json",json.loads((queue/"pending.json").read_text(encoding="utf-8")))
    writer=PdfIntakePipeline(queue,database,tmp_path/"backups",runtime,reload,repository_root=application)
    result=writer.process_next()
    if visibility=="hidden":
        assert result["chat_ready"] and reloads
        assert not list((runtime/"generations").rglob("article-*.md"))
        assert _reader(database,tmp_path).articles()["items"]==[]
    else:
        assert result["stage"]=="pending_review" and not result["chat_ready"] and not reloads
        assert not list(runtime.rglob("*.md"))
        assert _reader(database,tmp_path).article(identity)["summary"]!="Unapproved pending correction"


def test_already_frozen_legacy_acquisition_verification_does_not_open_a_writer(tmp_path):
    from climate_registry.acquisition import freeze_acquisition_for_report
    database=_historical_selected_acquisition(tmp_path)
    before=database.read_bytes()
    frozen=freeze_acquisition_for_report(database,"selected",report_date="2026-01-05")
    assert frozen["record_count"]==1 and frozen["records"][0]["content"]=="Historical full climate article evidence."
    assert database.read_bytes()==before
    with sqlite3.connect(database) as db:assert db.execute("PRAGMA user_version").fetchone()[0]==19


def test_rag_pages_and_revision_share_one_visibility_snapshot(tmp_path,monkeypatch):
    from agentic_wiki import AgenticWikiResponder
    from climate_registry import publication
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA journal_mode=WAL")
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit approved RAG fixture"}, status="accepted_legacy")
    db.close()
    wiki=tmp_path/"wiki";sources=tmp_path/"sources";wiki.mkdir();sources.mkdir()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT",raising=False)
    original=publication.public_wiki_pages;hidden=[]
    def pages_then_hide(*args,**kwargs):
        pages=original(*args,**kwargs)
        if not hidden:
            set_visibility(database,"article","a",False);hidden.append(True)
        return pages
    monkeypatch.setattr(publication,"public_wiki_pages",pages_then_hide)
    responder=AgenticWikiResponder(wiki_dir=wiki,source_dir=sources);responder.client=None
    assert "article-a.md" in {doc.file for doc in responder.kb.documents}
    assert responder.kb.public_registry_revision!=publication.public_revision(database)
    monkeypatch.setattr(publication,"public_wiki_pages",original)
    for _ in range(2):
        answer=responder.answer("Title a",language="en",answer_mode="brief")
        assert "article-a.md" not in {doc.file for doc in responder.kb.documents}
        assert all(item.get("path")!="wiki/article-a.md" for item in answer["sources"])
        assert responder.kb.public_registry_revision==publication.public_revision(database)


def test_new_pdf_writer_waits_for_native_review_and_then_resumes(tmp_path,monkeypatch):
    from reportlab.pdfgen.canvas import Canvas
    from climate_monitor.pdf_intake import import_pdf_reports
    from climate_registry.pdf_pipeline import enqueue_pdf_batch,PdfIntakePipeline
    from climate_delivery.io import atomic_write_json
    source=tmp_path/"source.pdf"
    canvas=Canvas(str(source)); canvas.drawString(50,760,"Climate Risk Outlook");canvas.showPage()
    canvas.drawString(50,760,"UPDATES");canvas.drawString(50,740,"PDF transition insurance evidence")
    canvas.drawString(50,720,"REPORT COVERAGE");canvas.drawString(50,700,"Transition insurance scenarios preserve this PDF observation.")
    canvas.linkURL("https://pdf.example/study",(48,738,360,754),relative=0);canvas.showPage();canvas.save()
    bundle=import_pdf_reports([source])
    assert bundle["articles"]
    database=tmp_path/"registry.sqlite3"
    with sqlite3.connect(database) as db:apply_migrations(db)
    db.close()
    application=tmp_path/"application";application.mkdir()
    queue=tmp_path/"queue";runtime=tmp_path/"runtime";runs=tmp_path/"runs"
    for directory in(queue,runtime,runs):directory.mkdir()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database));monkeypatch.setenv("CLIMATE_ACQUISITION_RUN_DIR",str(runs))
    queued=enqueue_pdf_batch(queue,bundle,repository_root=application)
    def reload(generation):atomic_write_json(queue/"active.json",json.loads((queue/"pending.json").read_text(encoding="utf-8")))
    writer=PdfIntakePipeline(queue,database,tmp_path/"backups",runtime,reload,repository_root=application)
    result=writer.process_next()
    assert result["stage"] == "pending_review" and not result["chat_ready"], result
    assert _reader(database,tmp_path).pdf_articles_all() == []
    from climate_registry.publication import prepare_review, _approve, stage_entities
    from climate_registry.acquisition_review import claim_review
    packet = prepare_review(database,runs/"registry-review")
    assert packet["candidates"]==[]
    from scripts.review_pipeline import pending
    assert pending("acquisition",runs) is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM article_check_runs").fetchone()[0]==0
        sha = connection.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind='pdf_article'").fetchone()[0]
        with pytest.raises(ValueError,match="completed source-bound T1"):
            _approve(connection,sha,{"test_fixture_native":"must not bypass T1"})
    files = {str(path):path.read_bytes() for path in runs.rglob("*") if path.is_file()}
    with pytest.raises(ValueError,match="no current T1-completed"):
        claim_review(runs/"registry-review",packet,session_id="cron_missing",execution_id="cron_missing",
            reviewer="fixture",hermes_database=tmp_path/"absent-hermes.sqlite3")
    assert files=={str(path):path.read_bytes() for path in runs.rglob("*") if path.is_file()}
    from climate_registry.information_checks import run_checks,source_fields
    body="PDF transition insurance evidence. Transition insurance scenarios preserve this PDF observation."
    checked=run_checks(database,kind="articles",backup_dir=tmp_path/"check-backups",
        fetcher=lambda key,url:{"article_id":key,"requested_url":url,"final_url":url,"status":"ok",
            "content":body,"content_ref":None,"content_hash":hashlib.sha256(body.encode()).hexdigest()},
        verifier=lambda kind,fields,text:{"comparisons":{**{key:{"status":"supported"} for key,value in source_fields(kind,fields).items() if value is not None},"title":{"status":"supported"},"summary":{"status":"supported"}}})
    assert checked["completed_count"]==1 and checked["status"]=="partial"  # Unknown publication date stays explicit.
    assert _reader(database,tmp_path).pdf_articles_all()==[]  # T1 improvement is still pending final review.
    _approve_native_packet(database,runs/"registry-review",tmp_path/"hermes.sqlite3","cron_pdf")
    result=writer.process_next()
    assert result["chat_ready"] is True
    assert _reader(database,tmp_path).pdf_articles_all()


@pytest.mark.parametrize("retry",[False,True])
def test_imported_pdf_continuation_rejects_another_database_without_mutation(tmp_path,monkeypatch,retry):
    import gc
    from test_issue176_chat_ready_pipeline import _setup,_ack_activation
    from climate_registry.pdf_pipeline import PdfIntakePipeline,retry_pdf_batch
    database,queue,runtime,wiki,sources,queued=_setup(tmp_path)
    gc.collect()  # Close the historical fixture's SQLite handles before Windows replacement.
    migrate_publication(database,tmp_path/"migration-backups",apply=True)
    gc.collect()
    application=tmp_path/"repository"
    runs=tmp_path/"runs";runs.mkdir()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    monkeypatch.setenv("CLIMATE_ACQUISITION_RUN_DIR",str(runs))
    writer=PdfIntakePipeline(queue,database,tmp_path/"backups",runtime,_ack_activation(queue),repository_root=application)
    pending=writer.process_next()
    assert pending["stage"]=="pending_review" and pending["imported"]
    assert pending["registry_database"]==str(database.resolve())
    _approve_native_packet(database,runs/"registry-review",tmp_path/"hermes.sqlite3","cron_pdf_bound")
    gc.collect()
    if retry:
        def failed_reload(generation):raise OSError("temporary reload unavailable")
        writer.reload_chat=failed_reload
        failed=writer.process_next()
        assert failed["stage"]=="failed" and failed["imported"] and failed["indexed"]
    other=tmp_path/"copy.sqlite3";other.write_bytes(database.read_bytes())
    before_a,before_b=database.read_bytes(),other.read_bytes()
    def files():
        return {str(path):path.read_bytes() for root in (queue,runtime,runs) for path in root.rglob("*") if path.is_file()}
    before=files()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(other))
    wrong=PdfIntakePipeline(queue,other,tmp_path/"backups",runtime,_ack_activation(queue),repository_root=application)
    operations=[lambda:wrong.process(queued["batch_id"]),lambda:writer.process(queued["batch_id"])]
    if not retry:operations.append(wrong.process_next)
    else:operations.append(lambda:retry_pdf_batch(queue,queued["batch_id"]))
    for operation in operations:
        with pytest.raises(ValueError,match="frozen task binding"):operation()
        assert database.read_bytes()==before_a and other.read_bytes()==before_b
        assert files()==before
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    if retry:assert retry_pdf_batch(queue,queued["batch_id"])["stage"]=="queued"
    same=PdfIntakePipeline(queue,database,tmp_path/"backups",runtime,_ack_activation(queue),repository_root=application)
    assert same.process_next()["chat_ready"]


@pytest.mark.parametrize("historical_missing_binding",[False,"processing","failed","queued"])
def test_pdf_first_write_binding_survives_failed_imported_status_save(tmp_path,monkeypatch,historical_missing_binding):
    import gc
    from test_issue176_chat_ready_pipeline import _setup,_ack_activation
    from climate_registry.pdf_pipeline import PdfIntakePipeline,retry_pdf_batch
    database,queue,runtime,wiki,sources,queued=_setup(tmp_path)
    gc.collect();migrate_publication(database,tmp_path/"migration-backups",apply=True);gc.collect()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    monkeypatch.delenv("CLIMATE_ACQUISITION_RUN_DIR",raising=False)
    writer=PdfIntakePipeline(queue,database,tmp_path/"backups",runtime,_ack_activation(queue),repository_root=tmp_path/"repository")
    original=writer._save
    def failed_save(batch_id,status,**changes):
        if changes.get("stage")=="imported":raise OSError("interrupted after database commit")
        return original(batch_id,status,**changes)
    monkeypatch.setattr(writer,"_save",failed_save)
    failed=writer.process_next()
    assert failed["stage"]=="failed" and not failed["imported"]
    assert failed["registry_database"]==str(database.resolve())
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM pdf_intake_documents").fetchone()[0]==1
    db.close()
    other=tmp_path/"copy.sqlite3"
    if historical_missing_binding:
        # An older writer committed the same real PDF without freezing its path.
        from climate_delivery.io import atomic_write_json
        path=queue/queued["batch_id"]/"status.json"
        old=json.loads(path.read_text(encoding="utf-8"));old.pop("registry_database")
        old["stage"]=historical_missing_binding
        atomic_write_json(path,old)
        with sqlite3.connect(other) as db:apply_migrations(db)
        db.close()
    else:
        other.write_bytes(database.read_bytes())
    before_a,before_b=database.read_bytes(),other.read_bytes()
    path=queue/queued["batch_id"]/"status.json";before=path.read_bytes()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(other))
    wrong=PdfIntakePipeline(queue,other,tmp_path/"backups",runtime,_ack_activation(queue),repository_root=tmp_path/"repository")
    all_files=lambda:{str(p):p.read_bytes() for root in (queue,runtime) for p in root.rglob("*") if p.is_file()}
    before_files=all_files()
    operations=[lambda:wrong.process(queued["batch_id"])]
    operations.append(wrong.process_next if historical_missing_binding in {"processing","queued"} else lambda:retry_pdf_batch(queue,queued["batch_id"]))
    for operation in operations:
        with pytest.raises(ValueError,match="no frozen Registry binding" if historical_missing_binding else "frozen task binding"):operation()
        assert path.read_bytes()==before and database.read_bytes()==before_a and other.read_bytes()==before_b
        assert all_files()==before_files
    if historical_missing_binding in {"processing","queued"}:
        assert retry_pdf_batch(queue,queued["batch_id"])["stage"]==historical_missing_binding  # Read-only, not a retryable failure.
    else:
        assert wrong.process_next() is None  # Failed batches require an explicit retry.
    assert all_files()==before_files and database.read_bytes()==before_a and other.read_bytes()==before_b
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    if historical_missing_binding:
        with pytest.raises(ValueError,match="no frozen Registry binding"):writer.process(queued["batch_id"])
        return
    monkeypatch.setattr(writer,"_save",original)
    retry_pdf_batch(queue,queued["batch_id"])
    assert writer.process_next()["imported"]


def test_unstarted_pdf_queue_can_choose_current_database_and_old_unbound_audit_stays_read_only(tmp_path,monkeypatch):
    import gc
    from test_issue176_chat_ready_pipeline import _setup,_ack_activation
    from climate_registry.pdf_pipeline import PdfIntakePipeline,retry_pdf_batch,read_pdf_batch,list_pdf_batches
    from climate_delivery.io import atomic_write_json
    database,queue,runtime,wiki,sources,queued=_setup(tmp_path)
    gc.collect();migrate_publication(database,tmp_path/"migration-backups",apply=True);gc.collect()
    other=tmp_path/"chosen.sqlite3";other.write_bytes(database.read_bytes())
    before_a=database.read_bytes()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(other))
    monkeypatch.delenv("CLIMATE_ACQUISITION_RUN_DIR",raising=False)
    writer=PdfIntakePipeline(queue,other,tmp_path/"backups",runtime,_ack_activation(queue),repository_root=tmp_path/"repository")
    imported=writer.process_next()
    assert imported["imported"] and imported["registry_database"]==str(other.resolve())
    assert database.read_bytes()==before_a
    old={key:value for key,value in imported.items() if key!="registry_database"}
    old.update(stage="failed",error="historical interrupted task")
    path=queue/queued["batch_id"]/"status.json";atomic_write_json(path,old)
    before=path.read_bytes();before_b=other.read_bytes()
    assert read_pdf_batch(queue,queued["batch_id"])==old and list_pdf_batches(queue)["total_batches"]==1
    for operation in (lambda:writer.process(queued["batch_id"]),lambda:retry_pdf_batch(queue,queued["batch_id"])):
        with pytest.raises(ValueError,match="no frozen Registry binding"):operation()
        assert path.read_bytes()==before and other.read_bytes()==before_b


def test_manual_migration_cannot_approve_a_later_automated_change(tmp_path):
    from climate_registry.acquisition_review import record_knowledge
    database=_database(tmp_path,version=19)
    with sqlite3.connect(database) as db:
        record_knowledge(db,kind="article",entity_id="a",source_kind="information_check",source_ref="registry-supplement:a",
            fields={"summary":"Owner-approved observed summary"}, evidence={"pending_enrichment":{"summary":"Owner-approved observed summary"},"source_row_sha256":"f"*64},recorded_at="2026-09-01T00:00:00Z")
    first=migrate_publication(database,tmp_path/"backups",apply=True)
    assert first["manual_published"] == 1
    assert _reader(database,tmp_path).article("a")["summary"] == "Owner-approved observed summary"
    with sqlite3.connect(database) as db:
        db.execute("UPDATE sources SET display_name='New unreviewed publisher' WHERE source_id='s'")
        stage_entities(db)
    again=migrate_publication(database,tmp_path/"backups",apply=True)
    assert again["manual_published"] == 0
    detail=_reader(database,tmp_path).article("a")
    assert detail["publisher"] == "Publisher"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT published_candidate_sha256 IS NOT latest_candidate_sha256 FROM registry_publication WHERE entity_id='a'").fetchone()[0] == 1


@pytest.mark.parametrize("materialized",[False,True])
@pytest.mark.parametrize("change",["model","manual","explicit","body","version"])
def test_once_only_supplement_preserves_baseline_and_later_approved_primary(tmp_path,materialized,change):
    from climate_registry.acquisition_review import record_knowledge
    from climate_registry.publication import export_public_snapshot
    from climate_registry.range_reports import _range_source
    database=_database(tmp_path,version=19)
    body="# Exact original body\n\nRetained insurance evidence."
    body_sha=hashlib.sha256(body.encode()).hexdigest()
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_content_versions VALUES('old-body','a',?,?,?,'text/markdown',?,'reader','v1','2026-09-01T00:00:00Z')",(body_sha,body,body_sha,len(body)))
        db.execute("INSERT INTO article_date_observations VALUES('original-date','a','https://example.org/a','page_information','2026-09-01','fixture','fixture','articles','a','{\"text\":\"Updated 1 September 2026\"}','2026-09-01T00:00:00Z')")
        db.execute("UPDATE articles SET current_content_version_id='old-body' WHERE article_id='a'")
        db.execute("INSERT INTO article_enrichments(enrichment_id,content_version_id,status,summary,categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at) VALUES('original-enrichment','old-body','complete','Original baseline summary','[]','[]','en','deterministic','fixture','v1','2026-09-01T00:00:00Z')")
        fields={"summary":"Original baseline summary","content":body}
        record_knowledge(db,kind="article",entity_id="a",source_kind="site",source_ref="legacy-core:a",fields=fields,
            evidence={"enrichment_id":"original-enrichment","content_sha256":body_sha},recorded_at="2026-09-01T00:00:00Z",time_basis="legacy_time_unknown")
        evidence={"pending_enrichment":{"title":"Owner curated title","summary":"Owner-approved initial summary","categories":["Old category"],"keywords":["old"],"language":"en"},
            "article_version_id":"v-a","content_version_id":"old-body","content_sha256":body_sha,"source_row_sha256":"f"*64,
            "materialize_manual_enrichment":materialized,"field_sources":{"summary":"observed_summary"}}
        record_knowledge(db,kind="article",entity_id="a",source_kind="site",source_ref="registry-supplement:a",fields=fields,
            evidence=evidence,recorded_at="2026-09-02T00:00:00Z",time_basis="legacy_time_unknown")
        originals={table:db.execute("SELECT * FROM "+table).fetchall() for table in ("article_content_versions","knowledge_versions")}
    applied=migrate_publication(database,tmp_path/"backups",apply=True)
    assert applied["manual_materialized"]==int(materialized) and applied["manual_published"]==1
    reader=_reader(database,tmp_path)
    old=reader.article("a")
    assert (old["title"],old["summary"],old["categories"],old["keywords"])==("Owner curated title","Owner-approved initial summary",["Old category"],["old"])
    assert old["summary_provenance"]=="source_report" and old["supplement_generator"]["kind"]=="manual"
    if materialized:assert old["enrichment"]["generator"]["kind"]=="manual"
    with sqlite3.connect(database) as db:
        content_id="old-body"
        if change=="body":
            newer="# New approved body\n\nNew source evidence.";sha=hashlib.sha256(newer.encode()).hexdigest()
            db.execute("INSERT INTO article_content_versions VALUES('new-body','a',?,?,?,'text/markdown',?,'reader','v2','2026-09-03T00:00:00Z')",(sha,newer,sha,len(newer)))
            db.execute("UPDATE articles SET current_content_version_id='new-body' WHERE article_id='a'");content_id="new-body"
        if change=="version":
            db.execute("INSERT INTO article_versions VALUES('new-version','a','New observed title','New observed title','New observed summary','new-fingerprint','report-title-summary','2026-09-03','2026-09-03')")
            db.execute("UPDATE articles SET current_version_id='new-version' WHERE article_id='a'")
        if change!="explicit":
            db.execute("INSERT INTO article_enrichments(enrichment_id,content_version_id,status,summary,categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at) VALUES('new-enrichment',?,'complete','Approved new primary','[\"New category\"]','[\"new\"]','en',?,'fixture','v2','2026-09-04T00:00:00Z')",(content_id,"manual" if change=="manual" else "model"))
        candidate=snapshot_entity(db,"article","a")
        if change=="explicit":candidate["derived_display"]={"summary":"Approved new primary","categories":["New category"],"keywords":["new"]}
        current=stage_snapshot(db,candidate)
    assert reader.article("a")==old
    with sqlite3.connect(database) as db:_approve(db,current,{"basis":"explicit complete changed-candidate fixture approval"}, status="accepted_legacy")
    detail=reader.article("a")
    assert (detail["summary"],detail["categories"],detail["keywords"])==("Approved new primary",["New category"],["new"])
    assert detail["title"]==("New observed title" if change=="version" else "Owner curated title")
    assert detail["date_observations"]==old["date_observations"] and detail["canonical_url"]==old["canonical_url"] and detail["publisher"]==old["publisher"]
    path=tmp_path/"public.json";export_public_snapshot(database,path)
    artifact=json.loads(path.read_text(encoding="utf-8"))
    assert next(item for item in artifact["articles"] if item["article_id"]=="a")["summary"]==detail["summary"]
    assert "Approved new primary" in artifact["wiki_pages"]["article-a.md"] and "Owner-approved initial summary" not in artifact["wiki_pages"]["article-a.md"]
    ranged=_range_source(reader,"2026-09-01","2026-09-04")["articles"]
    assert next(item for item in ranged if item["article_id"]=="a")["summary"]==detail["summary"]
    with sqlite3.connect(database) as db:
        previous=json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",(old["published_candidate_sha256"],)).fetchone()[0])
        from climate_registry.publication import approved_display
        assert approved_display(previous)["summary"]=="Owner-approved initial summary"
        for table,rows in originals.items():assert all(row in db.execute("SELECT * FROM "+table).fetchall() for row in rows)


def test_export_install_round_trip_keeps_public_date_observation_shape(tmp_path,monkeypatch):
    from climate_registry.publication import export_public_snapshot,install_git_snapshot
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_date_observations VALUES('date-a','a','https://example.org/a','page_information','2026-08-31','publisher_page','private/raw.json','dates','row-a',?,'2026-09-01')", (json.dumps({"date_kind":"updated","match_basis":"publisher_date_field","debug":"private"}),))
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit fixture approval"}, status="accepted_legacy")
    wiki=tmp_path/"application"/"wiki";wiki.mkdir(parents=True)
    export_public_snapshot(database,wiki/"public-registry.json")
    exported=json.loads((wiki/"public-registry.json").read_text(encoding="utf-8"))
    observation=next(item for item in exported["articles"] if item["article_id"]=="a")["date_observations"][0]
    assert observation["kind"] == "page_information" and observation["evidence"]["date_kind"] == "updated"
    assert "debug" not in observation["evidence"]
    target=tmp_path/"render.sqlite3"
    with sqlite3.connect(target) as db:apply_migrations(db)
    db.close()
    install_git_snapshot(target,wiki)
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    reader=_reader(target,tmp_path)
    assert reader.article("a")["date_observations"] == [observation]


def test_publisher_source_rebuild_exports_only_the_exact_public_version(tmp_path):
    from scripts.sync_source_wiki import sync_source_wiki
    from scripts.publish_weekly_reports import validate_allowlist
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        first=stage_snapshot(db,snapshot_entity(db,"article","a"))
        _approve(db,first,{"basis":"explicit fixture approval"}, status="accepted_legacy")
        db.execute("UPDATE article_versions SET observed_title='Pending title',observed_summary='Pending private summary' WHERE article_id='a'")
        stage_entities(db)
    sources=tmp_path/"sources";wiki=tmp_path/"wiki";sources.mkdir()
    (sources/"climate-monitor-2026-09-07.md").write_text("""# Weekly Climate Monitor
**Report Date:** 2026-09-07
## Executive Summary
- Pending private executive claim.
## Pillar A — Changes
- **Pending title**
  - Pending private summary.
  🔗 https://example.org/a
- **Hidden candidate**
  - Hidden private summary.
  🔗 https://example.org/b
## Original Links
- https://example.org/a
- https://example.org/b
""",encoding="utf-8")
    sync_source_wiki(source_dir=sources,wiki_dir=wiki,cadence="weekly",registry_database=database)
    artifact=json.loads((wiki/"public-registry.json").read_text(encoding="utf-8"))
    assert [item["article_id"] for item in artifact["articles"]] == ["a"]
    assert artifact["articles"][0]["title"] == "Title a"
    public_text=json.dumps(artifact)
    assert "Pending private" not in public_text and "Hidden private" not in public_text
    assert "Title a" in (wiki/"climate-monitor-2026-09-07.md").read_text(encoding="utf-8")
    original=(wiki/"public-registry.json").read_bytes()
    sync_source_wiki(source_dir=sources,wiki_dir=wiki,cadence="weekly",registry_database=database)
    assert (wiki/"public-registry.json").read_bytes()==original
    validate_allowlist([("A","wiki/public-registry.json"),("M","wiki/registry-meetings.md")],imported_dates=set())
    set_visibility(database,"article","a",False)
    sync_source_wiki(source_dir=sources,wiki_dir=wiki,cadence="weekly",registry_database=database)
    hidden=json.loads((wiki/"public-registry.json").read_text(encoding="utf-8"))
    assert hidden["articles"]==[] and "Title a" not in json.dumps(hidden)
    assert not (wiki/"climate-monitor-2026-09-07.md").exists()
    assert (sources/"climate-monitor-2026-09-07.md").is_file()


@pytest.mark.skipif(os.name == "nt", reason="management locking requires POSIX fcntl")
def test_legacy_definition_load_save_uses_environment_and_resume_checks_old_binding(tmp_path,monkeypatch):
    from test_issue94_management_console import _definition,_store
    from climate_monitor.management import _sha,ManagementService
    from climate_registry.persistent import initialize_registry
    definition=_definition(tmp_path)
    legacy_database=tmp_path/"old-runtime.sqlite3";initialize_registry(legacy_database)
    definition["runtime"]["registry_database"]=str(legacy_database)
    store=_store(tmp_path)
    raw={"schema_version":"climate-acquisition-task-state.v1","version":1,"saved_at":"2026-09-01T00:00:00Z","saved_by":"legacy","definition_sha256":_sha(definition),"definition":definition}
    store.active_path.write_text(json.dumps(raw),encoding="utf-8")
    loaded=store.load()
    assert "registry_database" not in loaded["definition"]["runtime"]
    saved=store.save(loaded["definition"],expected_version=1,actor="operator")
    assert saved["version"]==2
    assert json.loads((store.version_root/"00000001.json").read_text(encoding="utf-8"))==raw
    service=ManagementService(store=store,runtime_root=tmp_path/"runs",launcher=lambda binding:12345)
    started=service.start(trigger="manual")
    binding=service.binding(started["run_id"])
    assert binding["registry_database"]==str(tmp_path/"registry.sqlite3")
    # Model a pre-routing frozen binding without editing its historical identity.
    binding.pop("registry_routing",None);binding["registry_database"]=str(legacy_database)
    path=tmp_path/"runs"/started["run_id"]/"binding.json"
    path.write_text(json.dumps(binding),encoding="utf-8")
    (path.parent/"runtime.json").write_text(json.dumps({"state":"failed","attempt":1,"pid":None}),encoding="utf-8")
    before=path.read_bytes()
    with pytest.raises(ValueError,match="frozen task binding"):
        service.resume(started["run_id"])
    assert path.read_bytes()==before


@pytest.mark.skipif(os.name == "nt", reason="management locking requires POSIX fcntl")
@pytest.mark.parametrize("custom_launcher",[False,True])
@pytest.mark.parametrize("retry",[False,True])
def test_parent_meeting_launch_checks_frozen_database_before_any_mutation(tmp_path,monkeypatch,custom_launcher,retry):
    from test_issue94_management_console import _definition,_store
    from test_issue112_acquisition import _batch,_item
    from climate_monitor.management import ManagementService
    from climate_monitor.meetings import process_batch
    from climate_registry.acquisition import store_acquisition_batch
    import scripts.run_agent_acquisition as runner
    definition=_definition(tmp_path)
    definition["meeting"]["enabled"]=True
    store=_store(tmp_path);store.save(definition,actor="operator")
    launched=[]
    class Child:
        pid=54321
        def wait(self):return 0
    def popen(*args,**kwargs):launched.append("Popen");return Child()
    service=ManagementService(store=store,runtime_root=tmp_path/"runs",launcher=lambda binding:123,
        meeting_launcher=(lambda binding:launched.append(binding) or 234) if custom_launcher else None)
    run_id=service.start(trigger="manual")["run_id"]
    binding=service.binding(run_id);database=Path(binding["registry_database"])
    store_acquisition_batch(database,_batch([_item()],batch_id=binding["acquisition_batch_id"],
        report_date=binding["report_date"],policy=binding["date_policy"]))
    def failed(request):raise RuntimeError("temporary model failure")
    result=process_batch(database,binding["acquisition_batch_id"],prompt_text=binding["meeting"]["prompt_text"],
        prompt_version=binding["meeting"]["prompt_version"],provider="test",model="test",extractor=failed)
    assert result["status"]=="failed"
    other=tmp_path/"other.sqlite3";other.write_bytes(database.read_bytes())
    before_a,before_b=database.read_bytes(),other.read_bytes()
    files=lambda:{str(p):p.read_bytes() for p in (tmp_path/"runs").rglob("*") if p.is_file()}
    before=files()
    monkeypatch.setattr(runner.subprocess,"Popen",popen)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(other))
    assert service.binding(run_id)==binding
    assert service.meeting_progress(run_id)["status"]=="partial"  # Historical audit remains readable.
    for operation in (lambda:service.start_meetings(run_id,retry_failed=retry),
            lambda:runner._launch_meeting_worker(tmp_path/"runs"/run_id/"attempt-1.json",binding)):
        with pytest.raises(ValueError,match="frozen task binding"):operation()
        assert not launched and files()==before
        assert database.read_bytes()==before_a and other.read_bytes()==before_b
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    assert service.start_meetings(run_id,retry_failed=retry)["accepted"]
    assert len(launched)==1
    assert runner._launch_meeting_worker(tmp_path/"runs"/run_id/"attempt-1.json",binding)["status"]=="launched"
    assert len(launched)==2


def test_meeting_model_work_can_replace_pdf_database_without_losing_meeting_write(tmp_path):
    from test_issue136_meetings import _database as meeting_database,_candidate
    from climate_monitor.meetings import process_batch,freeze_snapshot
    from climate_monitor.pdf_intake import import_pdf_reports
    from climate_registry.pdf_intake import persist_pdf_intake
    from reportlab.pdfgen.canvas import Canvas
    body="World Climate Summit 2027 meets June 10–12, 2027 in New York. Registration deadline May 1, 2027. Register https://example.com/register. Climate risk agenda."
    database=meeting_database(tmp_path,[body])
    source=tmp_path/"parallel.pdf";canvas=Canvas(str(source));canvas.drawString(50,760,"Climate Risk Outlook");canvas.showPage()
    canvas.drawString(50,760,"UPDATES");canvas.drawString(50,740,"PDF transition evidence");canvas.drawString(50,720,"REPORT COVERAGE");canvas.drawString(50,700,"Preserved PDF insurance observation.")
    canvas.linkURL("https://pdf.example/evidence",(48,738,360,754),relative=0);canvas.showPage();canvas.save()
    bundle=import_pdf_reports([source]);assert bundle["articles"]
    def extractor(request):
        # A replacement writer succeeds while the network/model boundary is open.
        persist_pdf_intake(database,tmp_path/"backups",bundle)
        return {"events":[_candidate()]}
    result=process_batch(database,"batch",prompt_text="real source evidence",prompt_version="v1",provider="test",model="test",extractor=extractor)
    assert result["status"]=="succeeded",result
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM climate_events").fetchone()[0]==1
        assert db.execute("SELECT count(*) FROM pdf_intake_documents").fetchone()[0]==1
        assert db.execute("PRAGMA foreign_key_check").fetchall()==[]
    snapshot=freeze_snapshot(database,base_date="2027-01-01",timezone_name="UTC")
    assert snapshot["records"]
    from climate_registry.publication import export_public_snapshot,install_git_snapshot
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit fixture approval"}, status="accepted_legacy")
    wiki=tmp_path/"application"/"wiki";wiki.mkdir(parents=True)
    exported=wiki/"public-registry.json"
    export_public_snapshot(database,exported)
    artifact=json.loads(exported.read_text(encoding="utf-8"))
    assert any(item["origin"]=="web_collection" for item in artifact["meeting_records"])
    rendered=tmp_path/"render.sqlite3"
    with sqlite3.connect(rendered) as db:apply_migrations(db)
    db.close();install_git_snapshot(rendered,wiki)
    expected_live=_reader(database,tmp_path).meetings(base_date="2027-01-01")["items"]
    import os
    previous=os.environ.get("CLIMATE_REGISTRY_STATIC_SNAPSHOT")
    os.environ["CLIMATE_REGISTRY_STATIC_SNAPSHOT"]="1"
    try:
        assert _reader(rendered,tmp_path).meetings(base_date="2027-01-01")["items"]==expected_live
    finally:
        if previous is None:os.environ.pop("CLIMATE_REGISTRY_STATIC_SNAPSHOT",None)
        else:os.environ["CLIMATE_REGISTRY_STATIC_SNAPSHOT"]=previous


def test_writer_entrypoint_requires_an_existing_canonical_database(tmp_path,monkeypatch):
    from scripts.run_pdf_intake_writer import _pipeline
    missing=tmp_path/"missing.sqlite3"
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(missing))
    with pytest.raises(ValueError,match="existing database"):_pipeline()
    assert not missing.exists()


def test_writer_pending_review_obeys_poll_interval(monkeypatch):
    from scripts import run_pdf_intake_writer as entry
    from types import SimpleNamespace
    import sys
    monkeypatch.setattr(sys,"argv",["writer","--poll-seconds","0.2"])
    monkeypatch.setattr(entry,"_pipeline",lambda:SimpleNamespace(process_next=lambda:{"batch_id":"pending","stage":"pending_review","chat_ready":False}))
    pauses=[]
    def pause(seconds):
        pauses.append(seconds)
        raise StopIteration("end controlled poll")
    monkeypatch.setattr(entry.time,"sleep",pause)
    with pytest.raises(StopIteration,match="controlled poll"):entry.main()
    assert pauses==[0.2]


def test_publisher_choices_use_each_approved_source_version(tmp_path):
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit initial fixture approval"}, status="accepted_legacy")
        db.execute("UPDATE sources SET hostname='new.example.org',display_name='New publisher' WHERE source_id='s'")
        stage_entities(db)
        new_b=db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_id='b'").fetchone()[0]
        _approve(db,new_b,{"basis":"explicit new b fixture approval"}, status="accepted_legacy")
    reader=_reader(database,tmp_path)
    assert reader.article("a")["source"]=="example.org"
    assert reader.article("b")["source"]=="new.example.org"
    assert {item["hostname"] for item in reader.publishers()["items"]}=={"example.org","new.example.org"}
    set_visibility(database,"article","b",False)
    assert {item["hostname"] for item in reader.publishers()["items"]}=={"example.org"}


@pytest.mark.skipif(os.name == "nt", reason="Registry Wiki generation imports POSIX meeting readers")
@pytest.mark.parametrize("failure", ["missing", "corrupt", "schema", "unreadable", "location"])
def test_registry_outage_clears_same_responder_and_recovery_reloads_approved(tmp_path, monkeypatch, failure):
    from agentic_wiki import AgenticWikiResponder
    application = tmp_path / "application"
    wiki, sources = application / "wiki", application / "sources"
    wiki.mkdir(parents=True); sources.mkdir()
    (wiki / "article-stale.md").write_text("# Stale hidden disk sentinel\n\nDisk must not be cited.", encoding="utf-8")
    (sources / "climate-monitor-2026-09-01.md").write_text("# Raw source sentinel\n\nRaw archive must not be cited.", encoding="utf-8")
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        sha = stage_snapshot(db, snapshot_entity(db, "article", "a"))
        _approve(db, sha, {"basis": "explicit approved fixture"}, status="accepted_legacy")
    saved = database.read_bytes()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    responder = AgenticWikiResponder(wiki, sources); responder.client = None
    assert any(doc.file == "article-a.md" for doc in responder.kb.documents)
    assert responder.kb.source_documents == []
    if failure == "missing": database.unlink()
    elif failure == "corrupt": database.write_bytes(b"unavailable Registry")
    elif failure == "schema":
        with sqlite3.connect(database) as db: db.execute("PRAGMA user_version=999")
    elif failure == "unreadable":
        if os.name == "nt": pytest.skip("POSIX file permissions required")
        database.chmod(0o000)
    else: monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(Path(__file__).resolve()))
    try:
        # Both fresh startup and the existing responder must fail closed without crashing.
        fresh = AgenticWikiResponder(wiki, sources); fresh.client = None
        assert fresh.kb.documents == [] and fresh.kb.source_documents == [] and fresh.kb.chunks == []
        result = responder.answer("Observed summary", language="en", answer_mode="brief")
        assert result["sources"] == [] and responder.kb.documents == [] and responder.kb.source_documents == []
        assert responder.kb.public_registry_unavailable
    finally:
        if database.exists(): database.chmod(0o600)
        database.write_bytes(saved)
        monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    responder.answer("Observed summary", language="en", answer_mode="brief")
    assert any(doc.file == "article-a.md" for doc in responder.kb.documents)
    assert not responder.kb.public_registry_unavailable
    set_visibility(database, "article", "a", False)
    responder.answer("Observed summary", language="en", answer_mode="brief")
    assert all(doc.file not in {"article-a.md", "article-stale.md"} for doc in responder.kb.documents)
    assert responder.kb.source_documents == []


@pytest.mark.skipif(os.name == "nt", reason="fresh API startup imports require POSIX fcntl")
@pytest.mark.parametrize("failure", ["missing", "corrupt", "schema", "unreadable", "location"])
@pytest.mark.parametrize("retired", ["unconfigured", "runtime_only", "corrupt"])
def test_api_startup_degrades_configured_registry_but_keeps_health_home_offline_chat(tmp_path, failure, retired):
    import subprocess, sys
    database = tmp_path / "registry.sqlite3"
    if failure not in {"missing", "location"}:
        if failure == "corrupt": database.write_bytes(b"unavailable Registry")
        else:
            with sqlite3.connect(database) as db: apply_migrations(db)
            db.close()
            if failure == "schema":
                with sqlite3.connect(database) as db: db.execute("PRAGMA user_version=999")
            else: database.chmod(0o000)
    env = dict(os.environ, CLIMATE_REGISTRY_DB=str(Path(__file__).resolve() if failure == "location" else database), OPENAI_API_KEY="")
    env.pop("CLIMATE_REGISTRY_STATIC_SNAPSHOT", None)
    for name in ("CLIMATE_RUNTIME_WIKI_DIR", "CLIMATE_PDF_RUNTIME_WIKI_DIR", "CLIMATE_INTAKE_QUEUE_DIR", "CLIMATE_PDF_INTAKE_QUEUE_DIR"):
        env.pop(name, None)
    if retired != "unconfigured":
        env["CLIMATE_RUNTIME_WIKI_DIR"] = str(tmp_path / "retired-runtime")
    if retired == "corrupt":
        queue = tmp_path / "retired-queue"; queue.mkdir()
        (queue / "active.json").write_text("{interrupted historical pointer", encoding="utf-8")
        env["CLIMATE_INTAKE_QUEUE_DIR"] = str(queue)
    script = '''from fastapi.testclient import TestClient
import api_server
api_server.responder.client = None
client = TestClient(api_server.app)
assert client.get("/api/health").status_code == 200
assert client.get("/").status_code == 200
response = client.get("/api/registry/status")
assert response.status_code == 503
assert response.json()["reason"] in {"invalid_location","invalid_schema","database_unavailable"}
chat = client.post("/api/chat", json={"message":"Observed summary", "answer_mode":"brief"})
assert chat.status_code == 200, chat.text
assert chat.json()["sources"] == []
assert api_server.responder.kb.documents == [] and api_server.responder.kb.source_documents == []
print("healthy application; unavailable Registry; empty configured corpus")
'''
    try:
        completed = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=30)
        assert completed.returncode == 0, completed.stderr + completed.stdout
    finally:
        if database.exists(): database.chmod(0o600)


def _startup_overlay_fixture(tmp_path, mode, runtime_key):
    """Build the retired generation with the existing snapshot/renderer contracts."""
    from climate_registry.pdf_pipeline import _write_projection_manifest
    from climate_registry.wiki import snapshot_registry, sync_registry_wiki
    from climate_delivery.io import atomic_write_json
    runtime, queue = tmp_path / "retired-runtime", tmp_path / "retired-queue"
    queue.mkdir()
    env = {}
    if mode != "queue_only": env[runtime_key] = str(runtime)
    queue_key = "CLIMATE_INTAKE_QUEUE_DIR" if runtime_key == "CLIMATE_RUNTIME_WIKI_DIR" else "CLIMATE_PDF_INTAKE_QUEUE_DIR"
    if mode != "runtime_only": env[queue_key] = str(queue)
    if mode == "corrupt": (queue / "active.json").write_text("{interrupted historical pointer", encoding="utf-8")
    if mode == "valid":
        legacy_root = tmp_path / "legacy"; legacy_root.mkdir()
        legacy = _database(legacy_root, version=20)
        generation = runtime / "generations" / "retired"; generation.mkdir(parents=True)
        snapshot = runtime / "registry-snapshots" / "retired.sqlite3"
        snapshot_sha = snapshot_registry(legacy, snapshot)
        sync_registry_wiki(snapshot, generation)
        manifest_sha = _write_projection_manifest(generation, "retired", web_items=[], pdf_occurrence_ids=set())
        atomic_write_json(queue / "active.json", {"generation_id":"retired", "path":str(generation),
            "registry_sha256":snapshot_sha, "registry_snapshot":str(snapshot), "manifest_sha256":manifest_sha})
    return env


@pytest.mark.skipif(os.name == "nt", reason="fresh API startup imports require POSIX fcntl")
@pytest.mark.parametrize("runtime_key", ["CLIMATE_RUNTIME_WIKI_DIR", "CLIMATE_PDF_RUNTIME_WIKI_DIR"])
@pytest.mark.parametrize("retired", ["runtime_only", "queue_only", "missing", "corrupt", "valid"])
@pytest.mark.parametrize("approved", [False, True])
def test_current_api_startup_and_reload_ignore_retired_projection(tmp_path, runtime_key, retired, approved):
    import subprocess, sys
    if approved:
        database = _database(tmp_path)
        with sqlite3.connect(database) as db:
            candidate = snapshot_entity(db, "article", "a")
            candidate["derived_display"] = {"title":"Approved startup title", "summary":"Approved startup summary"}
            _approve(db, stage_snapshot(db, candidate), {"basis":"explicit exact startup fixture approval"}, status="accepted_legacy")
    else:
        database = tmp_path / "registry.sqlite3"
        with sqlite3.connect(database) as db: apply_migrations(db)
    env = dict(os.environ, CLIMATE_REGISTRY_DB=str(database), OPENAI_API_KEY="", RELOAD_TOKEN="startup-fixture-token")
    for name in ("CLIMATE_REGISTRY_STATIC_SNAPSHOT", "CLIMATE_RUNTIME_WIKI_DIR", "CLIMATE_PDF_RUNTIME_WIKI_DIR", "CLIMATE_INTAKE_QUEUE_DIR", "CLIMATE_PDF_INTAKE_QUEUE_DIR"):
        env.pop(name, None)
    env.update(_startup_overlay_fixture(tmp_path, retired, runtime_key))
    before = {str(path):path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    inode = database.stat().st_ino
    script = '''from fastapi.testclient import TestClient
import api_server
api_server.responder.client = None
client = TestClient(api_server.app)
assert client.get('/api/health').status_code == 200
assert client.get('/').status_code == 200
assert client.get('/api/registry/status').status_code == 200
assert api_server._selected_pdf_projection() == (None, None)
assert client.get('/api/registry/articles').json()['pagination']['total'] == EXPECTED
assert client.get('/api/registry/articles/b').status_code == 404
if EXPECTED:
    assert client.get('/api/registry/articles/a').json()['title'] == 'Approved startup title'
assert api_server.responder.kb.source_documents == []
assert all('Title b' not in doc.markdown for doc in api_server.responder.kb.documents)
assert client.post('/api/reload', headers={'X-Reload-Token':'startup-fixture-token'}).status_code == 200
assert api_server._selected_pdf_projection() == (None, None)
assert client.get('/api/registry/articles').json()['pagination']['total'] == EXPECTED
chat = client.post('/api/chat', json={'message':'Approved startup summary', 'answer_mode':'brief'})
assert chat.status_code == 200 and api_server.responder.kb.source_documents == []
print('startup/reload/offline approved Registry; retired projection ignored')
'''.replace("EXPECTED", str(int(approved)))
    completed = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert database.stat().st_ino == inode
    assert {str(path):path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.skipif(os.name == "nt", reason="fresh API startup imports require POSIX fcntl")
@pytest.mark.parametrize("runtime_key", ["CLIMATE_RUNTIME_WIKI_DIR", "CLIMATE_PDF_RUNTIME_WIKI_DIR"])
@pytest.mark.parametrize("retired", ["runtime_only", "corrupt", "valid"])
def test_legacy_api_startup_retains_strict_projection_selection(tmp_path, runtime_key, retired):
    import subprocess, sys
    database = _database(tmp_path, version=20)
    env = dict(os.environ, CLIMATE_REGISTRY_DB=str(database), OPENAI_API_KEY="")
    for name in ("CLIMATE_REGISTRY_STATIC_SNAPSHOT", "CLIMATE_RUNTIME_WIKI_DIR", "CLIMATE_PDF_RUNTIME_WIKI_DIR", "CLIMATE_INTAKE_QUEUE_DIR", "CLIMATE_PDF_INTAKE_QUEUE_DIR"):
        env.pop(name, None)
    env.update(_startup_overlay_fixture(tmp_path, retired, runtime_key))
    before = {str(path):path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    completed = subprocess.run([sys.executable, "-c", "import api_server; assert api_server._selected_pdf_projection()[1]['generation_id'] == 'retired'"],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=30)
    if retired == "valid": assert completed.returncode == 0, completed.stderr + completed.stdout
    else:
        assert completed.returncode != 0
        expected = "requires the durable batch queue" if retired == "runtime_only" else "active PDF Wiki projection is invalid"
        assert expected in completed.stderr
    assert {str(path):path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.skipif(os.name == "nt", reason="fresh API startup imports require POSIX fcntl")
@pytest.mark.parametrize("retired,runtime_key", [("runtime_only","CLIMATE_RUNTIME_WIKI_DIR"), ("corrupt","CLIMATE_PDF_RUNTIME_WIKI_DIR")])
def test_actual_render_startup_ignores_retired_projection(tmp_path, retired, runtime_key):
    import subprocess, sys
    env = dict(os.environ, OPENAI_API_KEY="")
    for name in ("CLIMATE_REGISTRY_DB", "CLIMATE_REGISTRY_STATIC_SNAPSHOT", "CLIMATE_RUNTIME_WIKI_DIR", "CLIMATE_PDF_RUNTIME_WIKI_DIR", "CLIMATE_INTAKE_QUEUE_DIR", "CLIMATE_PDF_INTAKE_QUEUE_DIR"):
        env.pop(name, None)
    env.update(_startup_overlay_fixture(tmp_path, retired, runtime_key))
    before = {str(path):path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    script = '''import json
from fastapi.testclient import TestClient
from scripts import run_render_web
def serve():
    import api_server
    artifact = json.loads((run_render_web.ROOT / 'wiki/public-registry.json').read_text())
    assert api_server._selected_pdf_projection() == (None, None)
    client = TestClient(api_server.app)
    assert client.get('/api/health').status_code == 200
    assert client.get('/').status_code == 200
    assert client.get('/api/registry/status').status_code == 200
    assert client.get('/api/registry/articles').json()['pagination']['total'] == len(artifact['articles'])
    first = artifact['articles'][0]
    assert client.get('/api/registry/articles/' + first['article_id']).json() == first
    assert api_server.responder.kb.source_documents == []
    assert client.post('/api/chat', json={'message':'climate', 'answer_mode':'brief'}).status_code == 200
run_render_web._run_app = serve
run_render_web.main()
print('real Render bootstrap and API startup ignore retired projection')
'''
    completed = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, timeout=40)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert {str(path):path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.skipif(os.name == "nt", reason="API management imports require POSIX fcntl")
@pytest.mark.parametrize("endpoint", ["status", "reports"])
def test_combined_reports_and_status_share_pdf_visibility_snapshot(tmp_path, monkeypatch, endpoint):
    import api_server
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        _pdf(db, identity="standalone", url="https://pdf.example/c", run_date="2026-09-03")
        for sha in stage_entities(db): _approve(db, sha, {"basis": "explicit approved fixture"}, status="accepted_legacy")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    original = getattr(RegistryReader, endpoint)
    changed = False
    def read(reader, **kwargs):
        nonlocal changed
        result = original(reader, **kwargs)
        if not changed:
            changed = True
            set_visibility(database, "pdf_article", "standalone", False)
        return result
    monkeypatch.setattr(RegistryReader, endpoint, read)
    if endpoint == "status":
        first = api_server.registry_status(include_pdf=True)
        assert (first["articles"], first["reports"], first["latest_report_date"]) == (3, 1, "2026-09-03")
        later = api_server.registry_status(include_pdf=True)
        assert (later["articles"], later["reports"], later["latest_report_date"]) == (2, 0, None)
    else:
        first = api_server.registry_reports(page="1", page_size="1", include_pdf=True)
        assert first["pagination"]["total"] == 1 and first["items"][0]["report_date"] == "2026-09-03"
        assert first["items"][0]["article_count"] == 1
        later = api_server.registry_reports(page="1", page_size="1", include_pdf=True)
        assert later["pagination"]["total"] == 0 and later["items"] == []


@pytest.mark.skipif(os.name == "nt", reason="API management imports require POSIX fcntl")
def test_public_api_matches_git_dto_and_keeps_raw_evidence_immutable(tmp_path, monkeypatch):
    import api_server
    from fastapi.testclient import TestClient
    from climate_registry.publication import _public_dto, export_public_snapshot
    database = _database(tmp_path)
    evidence = {"source_url":"https://example.org/a", "date_basis":"observed collection time", "text":"Collected 1 September at 12:00 UTC", "database":"/private/upstream.sqlite3", "debug":{"candidate":"private trace"}}
    with sqlite3.connect(database) as db:
        _pdf(db, occurrence={"summary":"Approved PDF summary", "summary_basis":"verbatim_pdf_article_row",
            "publication_date_evidence":{"kind":"publisher", "url":"https://example.org/a", "text":"Published 1 September", "database":"/private/upstream.sqlite3"}})
        db.execute("INSERT INTO article_date_observations VALUES('date','a','https://example.org/a','collection','2026-09-01T12:00:00Z','fixture','fixture','articles','a',?,'2026-09-01T12:00:00Z')", (json.dumps(evidence),))
        for sha in stage_entities(db): _approve(db, sha, {"basis": "explicit full public fixture approval"}, status="accepted_legacy")
    before = database.read_bytes()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(database, tmp_path))
    client = TestClient(api_server.app)
    raw = _reader(database, tmp_path).article("a")
    assert raw["date_observations"][0]["evidence"]["database"] == "/private/upstream.sqlite3"
    assert raw["pdf_occurrences"][0]["source_observations"][0]["path"] == "/evidence/source.pdf"
    public = client.get("/api/registry/articles/a")
    assert public.status_code == 200 and public.json() == _public_dto(raw)
    assert public.json()["date_observations"][0]["evidence"]["date_basis"] == evidence["date_basis"]
    assert public.json()["pdf_occurrences"][0]["summary"] == "Approved PDF summary"
    for url in ("/api/registry/articles/a", "/api/registry/articles?include_pdf=true", "/api/registry/pdf-intake/articles?include_linked=true", "/api/registry/pdf-intake/articles/a"):
        response = client.get(url)
        assert response.status_code == 200
        assert "/private/upstream.sqlite3" not in response.text and "/evidence/source.pdf" not in response.text
        assert '"checks"' not in response.text and '"source_observations"' not in response.text
    path = tmp_path / "public.json"; export_public_snapshot(database, path)
    exported = next(item for item in json.loads(path.read_text(encoding="utf-8"))["articles"] if item["article_id"] == "a")
    assert public.json() == exported and _public_dto(exported) == exported
    assert database.read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="API management imports require POSIX fcntl")
@pytest.mark.parametrize("include_pdf", [False, True])
def test_combined_publisher_response_uses_one_public_snapshot(tmp_path, monkeypatch, include_pdf):
    import api_server
    from climate_registry.information_checks import _writer
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        # WAL permits the ordinary writer to commit while this response holds a read snapshot.
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        db.execute("UPDATE sources SET hostname='old.example',display_name='Old publisher' WHERE source_id='s'")
        db.execute("INSERT INTO sources VALUES('new','new.example','New publisher','2026-09-01','2026-09-01')")
        db.execute("UPDATE articles SET canonical_url='https://old.example/a' WHERE article_id='a'")
        db.execute("UPDATE articles SET source_id='new',canonical_url='https://new.example/b' WHERE article_id='b'")
        _pdf(db, identity="linked", url="https://old.example/a")
        db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES('standalone','https://pdf.example/c','Standalone PDF','2026-08-01T00:00:00Z')")
        db.execute("INSERT INTO pdf_intake_article_occurrences SELECT 'standalone-occurrence','standalone',source_document_sha256,page,'https://pdf.example/c',report_date,publication_date,content_sha256,page_sha256,occurrence_json FROM pdf_intake_article_occurrences WHERE occurrence_id='occurrence'")
        stage_entities(db)
        for kind, identity in (("article", "a"), ("pdf_article", "standalone")):
            sha = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?", (kind, identity)).fetchone()[0]
            _approve(db, sha, {"basis": "explicit public fixture approval"}, status="accepted_legacy")
        new_sha = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='b'").fetchone()[0]
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(database, tmp_path))
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    original = RegistryReader.publishers
    switched = False
    def publishers(reader):
        nonlocal switched
        result = original(reader)
        if not switched:
            switched = True
            with _writer(database) as db:
                db.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_kind='article' AND entity_id='a'")
                _approve(db, new_sha, {"basis": "explicit next-version fixture approval"}, status="accepted_legacy")
        return result
    monkeypatch.setattr(RegistryReader, "publishers", publishers)
    expected_pdf = {"pdf.example"} if include_pdf else set()
    first = api_server.registry_publishers(include_pdf=include_pdf)
    assert {item["hostname"] for item in first["items"]} == {"old.example"} | expected_pdf
    assert first["total"] == len(first["items"])
    later = api_server.registry_publishers(include_pdf=include_pdf)
    assert {item["hostname"] for item in later["items"]} == {"new.example"} | expected_pdf
    combined = api_server.registry_articles(include_pdf=True, page_size="1")
    assert combined["pagination"]["total"] == 2
    assert api_server.registry_articles(include_pdf=True, source="pdf.example")["items"][0]["source_kind"] == "pdf"
    assert api_server.registry_articles(include_pdf=True, source="new.example")["items"][0]["article_id"] == "b"
    assert api_server.registry_articles(include_pdf=False)["pagination"]["total"] == 1


@pytest.mark.skipif(os.name == "nt", reason="API management imports require POSIX fcntl")
def test_combined_status_current_count_uses_its_public_snapshot(tmp_path, monkeypatch):
    import api_server
    from climate_registry.information_checks import _writer
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        for sha in stage_entities(db): _approve(db, sha, {"basis": "explicit initial fixture approval"}, status="accepted_legacy")
        db.execute("INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen) VALUES('c','https://example.org/c','s','2026-09-02','2026-09-02')")
        db.execute("INSERT INTO article_versions VALUES('v-c','c','New title','New title','New summary','f-c','report-title-summary','2026-09-02','2026-09-02')")
        db.execute("UPDATE articles SET current_version_id='v-c' WHERE article_id='c'")
        stage_entities(db)
        new_sha = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='c'").fetchone()[0]
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    original = RegistryReader.status
    switched = False
    def status(reader):
        nonlocal switched
        result = original(reader)
        if not switched:
            switched = True
            with _writer(database) as db:
                db.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_id IN ('a','b')")
                _approve(db, new_sha, {"basis": "explicit next-version fixture approval"}, status="accepted_legacy")
        return result
    monkeypatch.setattr(RegistryReader, "status", status)
    assert api_server.registry_status(include_pdf=True)["articles"] == 2
    assert api_server.registry_status(include_pdf=True)["articles"] == 1


@pytest.mark.skipif(os.name == "nt", reason="governed acquisition fixtures require POSIX fcntl")
@pytest.mark.parametrize("artifact", ["missing", "corrupt"])
@pytest.mark.parametrize("has_binding", [True, False])
def test_web_retry_binding_preflight_survives_unavailable_snapshot(tmp_path, monkeypatch, artifact, has_binding):
    from test_issue112_acquisition import _batch, _item
    from climate_registry.acquisition import store_acquisition_batch, freeze_acquisition_for_report
    from climate_registry.web_ingest_pipeline import enqueue_web_activation, WebIngestPipeline
    from climate_registry.pdf_pipeline import PdfIntakePipeline
    from climate_delivery.io import atomic_write_json
    old, new = tmp_path / "old.sqlite3", tmp_path / "new.sqlite3"
    for database in (old, new):
        with sqlite3.connect(database) as db: apply_migrations(db)
        db.close()
    application, queue, runtime = (tmp_path / name for name in ("application", "queue", "runtime"))
    for directory in (application, queue, runtime): directory.mkdir()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(old))
    monkeypatch.delenv("CLIMATE_ACQUISITION_RUN_DIR", raising=False)
    payload = _batch([_item()])
    store_acquisition_batch(old, payload)
    frozen = freeze_acquisition_for_report(old, payload["batch_id"], report_date=payload["report_date"])
    enqueue = lambda target: enqueue_web_activation(queue, target, payload["batch_id"], frozen_payload_sha256=digest(frozen), repository_root=application)
    queued = enqueue(old)
    with sqlite3.connect(old) as db:
        for sha in stage_entities(db): _approve(db, sha, {"basis": "explicit full web fixture approval"}, status="accepted_legacy")
    def fail_reload(generation): raise OSError("reload temporarily unavailable")
    writer = PdfIntakePipeline(queue, old, tmp_path / "backups", runtime, fail_reload, repository_root=application)
    assert writer.process_next()["stage"] == "failed"
    assert enqueue(old)["stage"] == "failed"  # Normal retry retains the original immutable request.
    request_path = next((queue / "web").glob("*/request.json"))
    retry = request_path.parent / "retry.json"
    assert retry.is_file()
    if not has_binding:
        request = json.loads(request_path.read_text(encoding="utf-8")); request.pop("source_registry_database")
        atomic_write_json(request_path, request)
    snapshot = request_path.parent / "registry.sqlite3"
    if artifact == "missing": snapshot.rename(tmp_path / "retained-snapshot.sqlite3")
    else: snapshot.write_bytes(b"unavailable historical snapshot")
    files = lambda: {str(p): p.read_bytes() for directory in (queue, runtime) for p in directory.rglob("*") if p.is_file()}
    before, old_bytes, new_bytes = files(), old.read_bytes(), new.read_bytes()
    reloads = []
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(new))
    for selected in (old, new):
        direct = WebIngestPipeline(queue, selected, runtime, reloads.append, repository_root=application)
        poller = PdfIntakePipeline(queue, selected, tmp_path / "backups", runtime, reloads.append, repository_root=application)
        for operation in (lambda: direct.process(queued["batch_id"]), poller.process_next, lambda: enqueue(selected)):
            with pytest.raises(ValueError, match="frozen task binding" if has_binding else "no frozen Registry binding"): operation()
            assert files() == before and old.read_bytes() == old_bytes and new.read_bytes() == new_bytes and reloads == []
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(old))
    if not has_binding:
        with pytest.raises(ValueError, match="no frozen Registry binding"): writer.process_next()
        assert files() == before
    else:
        # Same-selector snapshot failure remains a truthful failure; binding/request is never repaired or rebound.
        saved_request = request_path.read_bytes()
        result = WebIngestPipeline(queue, old, runtime, reloads.append, repository_root=application).process(queued["batch_id"])
        assert result["stage"] == "failed" and result["attempts"] == 2 and "request is invalid" in result["error"]
        assert request_path.read_bytes() == saved_request and not retry.exists() and reloads == []
        assert old.read_bytes() == old_bytes and new.read_bytes() == new_bytes


def test_public_snapshot_preserves_knowledge_append_order_at_equal_timestamps(tmp_path):
    from climate_registry.acquisition_review import record_knowledge
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        for text in ("ZZ baseline","AA improved","MM current"):
            record_knowledge(db,kind="article",entity_id="a",source_kind="information_check",source_ref="observation-a",
                fields={"summary":text},evidence={},recorded_at="2026-09-30T12:00:00Z")
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit current fixture approval"}, status="accepted_legacy")
    with _reader(database,tmp_path).connect() as projected:
        summaries=[json.loads(row[0])["summary"] for row in projected.execute("SELECT fields_json FROM knowledge_versions WHERE entity_id='a' ORDER BY rowid")]
    assert summaries==["ZZ baseline","AA improved","MM current"]


@pytest.mark.parametrize("visible",[True,False])
def test_pdf_approved_before_core_inherits_visibility_without_inheriting_pass(tmp_path,visible):
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-first",url="https://example.org/later")
        old=stage_snapshot(db,snapshot_entity(db,"pdf_article","pdf-first"))
        _approve(db,old,{"basis":"explicit PDF fixture approval"}, status="accepted_legacy")
    if not visible:set_visibility(database,"pdf_article","pdf-first",False)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen) VALUES('later','https://example.org/later','s','2026-09-01','2026-09-01')")
        db.execute("INSERT INTO article_versions VALUES('v-later','later','Pending core title','Pending core title','Pending core summary','f-later','report-title-summary','2026-09-01','2026-09-01')")
        db.execute("UPDATE articles SET current_version_id='v-later' WHERE article_id='later'")
        stage_entities(db)
        newest,published,flag=db.execute("SELECT latest_candidate_sha256,published_candidate_sha256,is_visible FROM registry_publication WHERE entity_kind='article' AND entity_id='later'").fetchone()
        assert published is None and newest!=old and bool(flag)==visible
    reader=_reader(database,tmp_path)
    with pytest.raises(RegistryNotFoundError):reader.article("later")
    if visible:
        assert reader.pdf_article("pdf-first")["title"]=="Original PDF title"
        assert "Pending core" not in json.dumps(reader.pdf_articles_all(include_linked=True))
    else:
        assert reader.pdf_articles_all(include_linked=True)==[]
    with sqlite3.connect(database) as db:_approve(db,newest,{"basis":"explicit new full core approval"}, status="accepted_legacy")
    if visible:assert reader.article("later")["title"]=="Pending core title"
    else:
        with pytest.raises(RegistryNotFoundError):reader.article("later")
        assert reader.pdf_articles_all(include_linked=True)==[]


def test_core_pdf_export_uses_curated_occurrences_and_preserves_public_fields(tmp_path):
    from climate_registry.publication import _public_dto,export_public_snapshot
    occurrence={"occurrence_id":"one","page":2,"summary":"Approved PDF summary","summary_basis":"verbatim_pdf_article_row",
        "verified_information":{"summary":"Verified PDF summary"},"checks":{"private":"run/debug"},
        "attempts":[{"private":"request/response"}],"source_observations":[{"path":"/private/pdf/source.pdf"}],"reader":{"private":"reader/debug"}}
    public=_public_dto({"pdf_occurrences":[occurrence]})["pdf_occurrences"][0]
    assert public=={key:value for key,value in occurrence.items() if key in {"occurrence_id","page","summary","summary_basis","verified_information"}}
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db)
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit public fixture"}, status="accepted_legacy")
    path=tmp_path/"public.json";export_public_snapshot(database,path)
    text=path.read_text(encoding="utf-8")
    assert "/evidence/source.pdf" not in text and '"checks"' not in text and '"source_observations"' not in text
    article=next(item for item in json.loads(text)["articles"] if item["article_id"]=="a")
    assert article["pdf_occurrences"][0]["summary"]=="Original PDF observation"


def test_pdf_report_git_projection_keeps_partial_linked_visibility_and_archive_gate(tmp_path,monkeypatch):
    from climate_registry.publication import export_public_snapshot,install_git_snapshot
    database=_database(tmp_path);document="f"*64
    with sqlite3.connect(database) as db:
        _pdf(db,run_date="2026-09-03",document={"pages":[{"page":2,"text":"Raw full report marker"}],"executive_summary":"Raw full report marker"})  # Core-linked observation is governed by the core approval.
        db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES('pdf-pending','https://example.org/pending','Pending PDF source','2026-09-03T00:00:00Z')")
        db.execute("INSERT INTO pdf_intake_article_occurrences VALUES('pending-occ','pdf-pending',?,2,'https://example.org/pending','2026-09-03',NULL,?,?,?)",(document,"d"*64,"c"*64,json.dumps({"summary":"Pending PDF source text","summary_basis":"verbatim_pdf_article_row"})))
        stage_entities(db)
        sha=db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='a'").fetchone()[0]
        _approve(db,sha,{"basis":"explicit linked PDF/core fixture approval"}, status="accepted_legacy")
    live=_reader(database,tmp_path)
    assert live.pdf_reports_all()[0]["article_count"]==1
    with pytest.raises(RegistryNotFoundError):live.pdf_report(document)
    wiki=tmp_path/"application/wiki";wiki.mkdir(parents=True)
    path=wiki/"public-registry.json"
    rendered=tmp_path/"render.sqlite3"
    with sqlite3.connect(rendered) as db:apply_migrations(db)
    db.close()
    for visible in (True,False):
        if not visible:set_visibility(database,"article","a",False)
        export_public_snapshot(database,path)
        artifact=json.loads(path.read_text())
        assert len(artifact["pdf_reports"])==(1 if visible else 0) and artifact["pdf_report_details"]=={}
        assert "Pending PDF source text" not in path.read_text() and "Raw full report marker" not in path.read_text()
        install_git_snapshot(rendered,wiki);monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
        static=_reader(rendered,tmp_path)
        assert static.pdf_reports_all()==artifact["pdf_reports"]
        with pytest.raises(RegistryNotFoundError):static.pdf_report(document)
        monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT")
    set_visibility(database,"article","a",True)
    with sqlite3.connect(database) as db:
        sha=db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_id='pdf-pending'").fetchone()[0]
        _approve(db,sha,{"basis":"explicit second PDF fixture approval"}, status="accepted_legacy")
    export_public_snapshot(database,path);artifact=json.loads(path.read_text())
    assert artifact["pdf_reports"][0]["article_count"]==2 and document in artifact["pdf_report_details"]
    assert "Raw full report marker" not in path.read_text()
    install_git_snapshot(rendered,wiki);monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    static=_reader(rendered,tmp_path)
    assert static.pdf_report(document)==artifact["pdf_report_details"][document]
    assert static.pdf_reports_all(allowed_occurrence_ids={"pending-occ"})[0]["article_count"]==1
    assert [row["article_id"] for row in static.pdf_report(document,allowed_occurrence_ids={"pending-occ"})["articles"]]==["pdf-pending"]
    with pytest.raises(RegistryNotFoundError):static.pdf_report(document,include_bytes=True)
    # Existing artifacts remain readable without inventing missing PDF source data.
    artifact.pop("pdf_reports");artifact.pop("pdf_report_details");artifact.pop("artifact_sha256")
    artifact["artifact_sha256"]=digest(artifact);path.write_text(json.dumps(artifact),encoding="utf-8")
    assert _reader(rendered,tmp_path).pdf_reports_all()==[]


def test_pending_core_pdf_alias_keeps_both_old_approved_snapshots(tmp_path):
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-first",url="https://example.org/later")
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit separate core/PDF fixture approval"}, status="accepted_legacy")
    reader=_reader(database,tmp_path)
    before=reader.article("a")
    pdf=reader.pdf_article("pdf-first")
    with sqlite3.connect(database) as db:
        db.execute("UPDATE articles SET canonical_url='https://example.org/later' WHERE article_id='a'")
        stage_entities(db)
        current=db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='a'").fetchone()[0]
    assert reader.article("a")==before and reader.pdf_article("pdf-first")==pdf
    assert len(reader.pdf_articles_all())==1
    with sqlite3.connect(database) as db:_approve(db,current,{"basis":"explicit absorbed core/PDF fixture approval"}, status="accepted_legacy")
    with reader.connect() as projected:
        assert projected.execute("SELECT count(*) FROM public_snapshot_metadata WHERE entity_kind='pdf_article'").fetchone()[0]==0
    assert reader.article("a")["pdf_occurrences"][0]["summary"]=="Original PDF observation"
    set_visibility(database,"pdf_article","pdf-first",False)
    with pytest.raises(RegistryNotFoundError):reader.article("a")
    assert reader.pdf_articles_all(include_linked=True)==[]
    set_visibility(database,"article","a",True)
    assert reader.article("a")["pdf_occurrences"][0]["summary"]=="Original PDF observation"


def test_hidden_pdf_meeting_stays_hidden_after_native_identity_is_approved(tmp_path):
    from test_issue136_meetings import _database as meeting_database,_candidate
    from climate_monitor.meetings import process_batch
    body="World Climate Summit 2027 meets June 10–12, 2027 in New York. Registration deadline May 1, 2027. Register https://example.com/register. Climate risk agenda."
    database=meeting_database(tmp_path,[body])
    item={"event_id":"pdf-first","occurrence_id":"pdf-meeting-first","name":"World Climate Summit 2027","kind":"event",
        "raw_date":"June 10–12, 2027","date_precision":"day","start_date":"2027-06-10","end_date":"2027-06-12",
        "summary":"Original PDF meeting","page":2,"source_urls":["https://example.com/register"],"source_document_sha256":"f"*64}
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-independent",url="https://example.org/independent")
        db.execute("INSERT INTO pdf_intake_calendar_items VALUES(?,?,?,2,?,'event',?,'day',?,?,?, ?,NULL,?)",
            (item["occurrence_id"],item["event_id"],"f"*64,item["name"],item["raw_date"],item["start_date"],item["end_date"],item["summary"],"c"*64,json.dumps(item)))
        sha=stage_snapshot(db,snapshot_entity(db,"pdf_meeting","pdf-first"))
        _approve(db,sha,{"basis":"explicit approved PDF meeting fixture"}, status="accepted_legacy")
    set_visibility(database,"pdf_meeting","pdf-first",False)
    result=process_batch(database,"batch",prompt_text="source evidence",prompt_version="v1",provider="test",model="test",extractor=lambda request:{"events":[_candidate()]})
    assert result["status"]=="succeeded"
    with sqlite3.connect(database) as db:
        event=db.execute("SELECT event_id FROM climate_events").fetchone()[0]
        packet=json.dumps({"canonical_event_id":event})
        db.execute("INSERT INTO meeting_check_runs VALUES('check','{}',?,'2026-09-01','2026-09-01','complete',1,1,NULL)",("a"*64,))
        db.execute("INSERT INTO meeting_check_attempts(attempt_id,run_id,occurrence_id,source_url,source_revision_sha256,checked_at,access_status,verification_status,packet_json,packet_sha256) VALUES('attempt','check','pdf-meeting-first','https://example.com/register',?,'2026-09-01','accessible','verified',?,?)",("b"*64,packet,hashlib.sha256(packet.encode()).hexdigest()))
        stage_entities(db)
        native=db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind='meeting' AND entity_id=?",(event,)).fetchone()[0]
        assert db.execute("SELECT is_visible FROM registry_publication WHERE entity_kind='meeting' AND entity_id=?",(event,)).fetchone()[0]==0
        _approve(db,native,{"basis":"explicit full canonical meeting fixture approval"}, status="accepted_legacy")
    reader=_reader(database,tmp_path)
    assert reader.pdf_calendar_items_all()==[] and reader.meetings(base_date="2027-01-01")["items"]==[]
    set_visibility(database,"meeting",event,True)
    assert reader.meetings(base_date="2027-01-01")["items"]


def test_meeting_wiki_renders_unified_native_pdf_and_verified_alias_once(tmp_path,monkeypatch):
    from test_issue136_meetings import _database as meeting_database,_candidate
    from climate_monitor.meetings import process_batch
    from climate_registry import information_checks
    from climate_registry.publication import export_public_snapshot
    body="\n".join(name+" meets June 10–12, 2027 in New York. Example hosts it. Registration deadline May 1, 2027. Register https://example.com/register. Climate risk agenda." for name in ("World Climate Summit 2027","Independent Climate Summit"))
    database=meeting_database(tmp_path,[body])
    processed=process_batch(database,"batch",prompt_text="source evidence",prompt_version="v1",provider="test",model="test",
        extractor=lambda request:{"events":[_candidate(deadline_type=None,deadline_date=None,deadline_evidence=None),_candidate(name="Independent Climate Summit",deadline_type=None,deadline_date=None,deadline_evidence=None)]})
    assert processed["status"]=="succeeded",processed
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-independent",url="https://example.org/independent")
        for identity,name in (("shared","World Climate Summit 2027"),("standalone","Standalone PDF meeting")):
            item={"event_id":identity,"occurrence_id":"pdf-"+identity,"name":name,"kind":"event","raw_date":"June 10–12, 2027",
                "date_precision":"day","start_date":"2027-06-10","end_date":"2027-06-12","summary":"Original PDF meeting", "page":2,
                "source_urls":["https://example.com/register"],"source_document_sha256":"f"*64}
            db.execute("INSERT INTO pdf_intake_calendar_items VALUES(?,?,?,2,?,'event',?,'day',?,?,?, ?,NULL,?)",
                (item["occurrence_id"],identity,"f"*64,name,item["raw_date"],item["start_date"],item["end_date"],item["summary"],"c"*64,json.dumps(item)))
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit separate native/PDF fixture approval"}, status="accepted_legacy")
        previous = db.execute("SELECT entity_kind,entity_id,published_candidate_sha256 FROM registry_publication WHERE entity_kind IN ('meeting','pdf_meeting') ORDER BY entity_kind,entity_id").fetchall()
    reader=_reader(database,tmp_path)
    before=reader.meetings_all(base_date="1900-01-01")
    assert len(before)==4 and len(reader.pdf_calendar_items_all())==2
    def fetcher(identity,url):
        return {"article_id":identity,"requested_url":url,"final_url":url,"status":"ok","content":body,"content_hash":hashlib.sha256(body.encode()).hexdigest(),"content_ref":None}
    information_checks.run_checks(database,kind="meetings",backup_dir=tmp_path/"backups",occurrence_ids={"pdf-shared"},fetcher=fetcher,
        verifier=lambda kind,fields,content:{"comparisons":{key:{"status":"supported"} for key in fields},"website_candidate":_candidate(deadline_type=None,deadline_date=None,deadline_evidence=None)})
    # The real check stages a merged candidate; it cannot rewrite the old public relation.
    assert reader.meetings_all(base_date="1900-01-01")==before
    assert len(reader.pdf_calendar_items_all())==2
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT entity_kind,entity_id,published_candidate_sha256 FROM registry_publication WHERE entity_kind IN ('meeting','pdf_meeting') ORDER BY entity_kind,entity_id").fetchall()==previous
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit unified meeting fixture approval"}, status="accepted_legacy")
    reader=_reader(database,tmp_path)
    meetings=reader.meetings_all(base_date="1900-01-01")
    assert len(meetings)==3
    wiki=tmp_path/"application/wiki";wiki.mkdir(parents=True)
    path=wiki/"public-registry.json";export_public_snapshot(database,path)
    artifact=json.loads(path.read_text(encoding="utf-8"))
    markdown=artifact["wiki_pages"]["registry-meetings.md"]
    assert markdown.count("\n## ")==len(meetings)
    for name in ("World Climate Summit 2027","Independent Climate Summit","Standalone PDF meeting"):
        assert markdown.count("## "+name+"\n")==1
    assert "SHA-256: "+"f"*64 in markdown and "page 2" in markdown
    def private_keys(value):
        if isinstance(value,dict):
            for key,item in value.items():
                assert key not in {"checks","reader","attempts","source_observations","item_json","original_pdf_base64"}
                private_keys(item)
        elif isinstance(value,list):
            for item in value:private_keys(item)
    private_keys(artifact)
    assert "/evidence/source.pdf" not in path.read_text(encoding="utf-8")
    merged=next(item for item in artifact["meeting_records"] if item["name"]=="World Climate Summit 2027")
    observation=merged["pdf_observations"][0]
    assert observation["source_document_sha256"]=="f"*64 and observation["page"]==2
    assert observation["raw_date"]=="June 10–12, 2027" and observation["summary"]=="Original PDF meeting"
    assert observation["source_urls"]==["https://example.com/register"]
    assert "PDF observation: Original PDF meeting" in markdown
    event=merged["event_id"]
    set_visibility(database,"meeting",event,False)
    assert len(reader.meetings_all(base_date="1900-01-01"))==2
    assert {item["occurrence_id"] for item in reader.pdf_calendar_items_all()}=={"pdf-standalone"}
    set_visibility(database,"pdf_meeting","shared",True)
    assert reader.meetings_all(base_date="1900-01-01")==meetings
    import api_server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(api_server,"_registry_reader",lambda:_reader(database,tmp_path))
    monkeypatch.setattr(api_server,"_range_report_overlay",lambda:(None,None,None))
    client=TestClient(api_server.app)
    for url in ("/api/registry/meetings?base_date=1900-01-01","/api/registry/pdf-intake/calendar"):
        response=client.get(url)
        assert response.status_code==200
        private_keys(response.json())
        assert "/evidence/source.pdf" not in response.text
    assert client.get("/api/registry/meetings?base_date=1900-01-01").json()["items"]==artifact["meeting_records"]
    from climate_registry.publication import install_git_snapshot
    rendered=tmp_path/"render.sqlite3"
    with sqlite3.connect(rendered) as db:apply_migrations(db)
    db.close();install_git_snapshot(rendered,wiki)
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    assert _reader(rendered,tmp_path).meetings_all(base_date="1900-01-01")==artifact["meeting_records"]


def test_approved_native_meeting_wiki_and_live_render_rag_keep_fields_with_pending_article(tmp_path,monkeypatch):
    from test_issue136_meetings import _database as meeting_database,_candidate
    from climate_monitor.meetings import process_batch
    from climate_registry.publication import export_public_snapshot,install_git_snapshot
    from agentic_wiki import AgenticWikiResponder
    body="World Climate Summit 2027 meets June 10–12, 2027 in New York. Example hosts it. World Climate Summit 2027 registration closes May 1, 2027. Register https://example.com/register. Climate risk agenda. PrivatePendingBodyMarker."
    database=meeting_database(tmp_path,[body])
    processed=process_batch(database,"batch",prompt_text="source evidence",prompt_version="v1",provider="test",model="test",extractor=lambda request:{"events":[_candidate(deadline_evidence="World Climate Summit 2027 registration closes May 1, 2027")]})
    assert processed["status"]=="succeeded"
    with sqlite3.connect(database) as db:
        stage_entities(db)
        sha=db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind='meeting'").fetchone()[0]
        _approve(db,sha,{"basis":"explicit exact native meeting fixture approval; article remains pending"}, status="accepted_legacy")
    reader=_reader(database,tmp_path)
    assert reader.articles()["items"]==[]
    record=reader.meetings_all(base_date="1900-01-01")[0]
    assert record["sources"][0]["source_url"]=="https://example.com/1"
    wiki=tmp_path/"wiki";sources=tmp_path/"sources";wiki.mkdir();sources.mkdir()
    path=wiki/"public-registry.json";export_public_snapshot(database,path)
    artifact=json.loads(path.read_text(encoding="utf-8"))
    markdown=artifact["wiki_pages"]["registry-meetings.md"]
    for text in ("Source: [https://example.com/1]", "Online URL: https://example.com/register", "Location: New York", "Deadline: 2027-05-01", "End date: 2027-06-12", "Host: Example"):
        assert text in markdown
    rendered=tmp_path/"render.sqlite3"
    with sqlite3.connect(rendered) as db:apply_migrations(db)
    db.close();install_git_snapshot(rendered,wiki)
    for selected,static in ((database,False),(rendered,True)):
        monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(selected))
        if static:monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
        else:monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT",raising=False)
        responder=AgenticWikiResponder(wiki_dir=wiki,source_dir=sources);responder.client=None
        document=next(doc for doc in responder.kb.documents if doc.file=="registry-meetings.md")
        assert document.markdown==markdown
        assert all("PrivatePendingBodyMarker" not in doc.markdown for doc in responder.kb.documents)
        answer=responder.answer("World Climate Summit 2027 location and registration deadline",language="en",answer_mode="brief")
        assert answer["sources"]
        assert {"https://example.com/1","https://example.com/register"} <= {url for source in answer["sources"] for url in source.get("source_urls",[])}
        assert "New York" in answer["text"] and "2027-05-01" in answer["text"]


def test_approved_meeting_query_availability_matches_render_and_preserves_coverage(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    from test_issue136_meetings import _database as meeting_database,_candidate
    from climate_monitor.meetings import process_batch
    from climate_registry.publication import export_public_snapshot,install_git_snapshot
    from climate_registry import range_reports
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT",raising=False)
    database=meeting_database(tmp_path,["World Climate Summit 2027 meets June 10–12, 2027 in New York. Registration deadline May 1, 2027. Register https://example.com/register. Climate risk agenda."])
    result=process_batch(database,"batch",prompt_text="source evidence",prompt_version="v1",provider="test",model="test",extractor=lambda request:{"events":[_candidate()]})
    assert result["status"]=="succeeded"
    reader=_reader(database,tmp_path)
    pending=range_reports._meeting_payload(reader,None,base_date="2027-01-01")
    assert pending["records"]==[] and pending["status"]=="empty"
    assert pending["coverage"]=={"status":"processed","records_scope":"approved_versions","returned_record_count":0}
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit approved meeting fixture"}, status="accepted_legacy")
        event=db.execute("SELECT event_id FROM climate_events").fetchone()[0]
    live=range_reports.freeze_range_report(reader,tmp_path/"live-reports",start_date="2026-09-01",end_date="2026-09-01",generated_at=datetime(2027,1,1,tzinfo=timezone.utc))
    meeting=live["meeting"]
    assert meeting["status"]=="included" and len(meeting["records"])==1
    assert meeting["coverage"]=={"status":"processed","records_scope":"approved_versions","returned_record_count":1}
    html=range_reports.render_range_report_html(live)
    assert "Meeting query status: included" in html and "Meeting coverage:</strong> processed" in html
    wiki=tmp_path/"application/wiki";wiki.mkdir(parents=True)
    export_public_snapshot(database,wiki/"public-registry.json")
    rendered=tmp_path/"render.sqlite3"
    with sqlite3.connect(rendered) as db:apply_migrations(db)
    db.close();install_git_snapshot(rendered,wiki)
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    restored=range_reports.freeze_range_report(_reader(rendered,tmp_path),tmp_path/"render-reports",start_date="2026-09-01",end_date="2026-09-01",generated_at=datetime(2027,1,1,tzinfo=timezone.utc))
    assert restored["meeting"]["status"]=="included"
    assert restored["meeting"]["coverage"]=={**meeting["coverage"],"source":"approved_git_snapshot"}
    assert [{key:record[key] for key in meeting["records"][0]} for record in restored["meeting"]["records"]]==meeting["records"]
    html=range_reports.render_range_report_html(restored)
    assert "Meeting query status: included" in html and "Meeting coverage:</strong> processed" in html
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT")
    set_visibility(database,"meeting",event,False)
    empty=range_reports._meeting_payload(reader,None,base_date="2027-01-01")
    assert empty["status"]=="empty" and empty["records"]==[] and empty["coverage"]["status"]=="processed"
    export_public_snapshot(database,wiki/"public-registry.json")
    install_git_snapshot(rendered,wiki)
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    empty=range_reports._meeting_payload(_reader(rendered,tmp_path),None,base_date="2027-01-01")
    assert empty["status"]=="empty" and empty["records"]==[] and empty["coverage"]=={"status":"processed","records_scope":"approved_versions","returned_record_count":0,"source":"approved_git_snapshot"}
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT")
    def unavailable(*args,**kwargs):raise OSError("meeting read failed")
    monkeypatch.setattr(range_reports,"query_events",unavailable)
    failed=range_reports._meeting_payload(reader,None,base_date="2027-01-01")
    assert failed["status"]=="unavailable" and failed["records"]==[]
    assert failed["coverage"]=={"status":"unavailable","error":"OSError"}


def test_public_meeting_coverage_exports_only_existing_status_and_counts():
    from climate_registry.publication import public_meeting_coverage
    raw={"status":"partial","records_scope":"approved_versions","returned_record_count":1,
        "runs":[{"error":"private failure /evidence/source.pdf"}],"prompt_text":"private prompt"}
    assert public_meeting_coverage(raw)=={"status":"partial","records_scope":"approved_versions","returned_record_count":1}
    assert public_meeting_coverage({})=={"status":"unavailable"}


def test_approved_meeting_queries_preserve_partial_and_failed_source_coverage(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    from test_issue136_meetings import _database as meeting_database,_candidate
    from climate_monitor.meetings import process_batch,query_events,meeting_status
    from climate_registry import range_reports
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT",raising=False)
    body="World Climate Summit 2027 meets June 10–12, 2027 in New York. Registration deadline May 1, 2027. Register https://example.com/register. Climate risk agenda."
    database=meeting_database(tmp_path,[body,body])
    def extract(request):
        if request["content_version_id"]=="content-2":raise RuntimeError("private model failure /evidence/source.pdf")
        return {"events":[_candidate()]}
    result=process_batch(database,"batch",prompt_text="private prompt",prompt_version="v1",provider="test",model="test",extractor=extract)
    assert result["status"]=="partial"
    assert meeting_status(database,batch_id="batch")["status"]=="partial"
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit approved partial meeting fixture"}, status="accepted_legacy")
    reader=_reader(database,tmp_path)
    public=reader.meetings(base_date="2027-01-01")
    assert len(public["items"])==1
    assert public["coverage"]=={"status":"partial","records_scope":"approved_versions","returned_record_count":1}
    frozen=range_reports.freeze_range_report(reader,tmp_path/"reports",start_date="2026-09-01",end_date="2026-09-01",generated_at=datetime(2027,1,1,tzinfo=timezone.utc))
    assert frozen["meeting"]["status"]=="partial" and len(frozen["meeting"]["records"])==1
    html=range_reports.render_range_report_html(frozen)
    assert "Meeting query status: partial" in html and "Meeting coverage:</strong> partial" in html
    assert "private model failure" not in json.dumps(public) and "/evidence/source.pdf" not in json.dumps(public)
    assert set(public["coverage"])=={"status","records_scope","returned_record_count"}
    from climate_registry.publication import export_public_snapshot,install_git_snapshot
    wiki=tmp_path/"application/wiki";wiki.mkdir(parents=True)
    export_public_snapshot(database,wiki/"public-registry.json")
    artifact=json.loads((wiki/"public-registry.json").read_text(encoding="utf-8"))
    assert artifact["meeting_coverage"]==public["coverage"]
    assert "private model failure" not in json.dumps(artifact) and "/evidence/source.pdf" not in json.dumps(artifact)
    rendered=tmp_path/"render.sqlite3"
    with sqlite3.connect(rendered) as db:apply_migrations(db)
    db.close();install_git_snapshot(rendered,wiki)
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    static_reader=_reader(rendered,tmp_path)
    static=static_reader.meetings(base_date="2027-01-01")
    assert static["coverage"]=={**public["coverage"],"source":"approved_git_snapshot"}
    restored=range_reports.freeze_range_report(static_reader,tmp_path/"static-reports",start_date="2026-09-01",end_date="2026-09-01",generated_at=datetime(2027,1,1,tzinfo=timezone.utc))
    assert restored["meeting"]["status"]=="partial" and len(restored["meeting"]["records"])==1
    assert "Meeting query status: partial" in range_reports.render_range_report_html(restored)
    assert static["items"]==artifact["meeting_records"]
    # Older trusted Git artifacts have no claim about source coverage.
    artifact.pop("meeting_coverage");artifact.pop("artifact_sha256")
    artifact["artifact_sha256"]=digest(artifact)
    from climate_delivery.io import atomic_write_json
    atomic_write_json(wiki/"public-registry.json",artifact);install_git_snapshot(rendered,wiki)
    old_reader=_reader(rendered,tmp_path)
    assert old_reader.meetings(base_date="2027-01-01")["coverage"]=={"status":"unavailable","source":"approved_git_snapshot"}
    assert range_reports._meeting_payload(old_reader,None,base_date="2027-01-01")["status"]=="unavailable"
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT")
    # An explicitly failed target remains unavailable; it is not processed coverage.
    failed_root=tmp_path/"failed";failed_root.mkdir()
    failed_database=meeting_database(failed_root,[body])
    def fail(request):raise RuntimeError("temporary failure")
    failed=process_batch(failed_database,"batch",prompt_text="private prompt",prompt_version="v1",provider="test",model="test",extractor=fail)
    assert failed["status"]=="failed"
    failed_reader=_reader(failed_database,failed_root)
    with failed_reader.public_snapshot(),failed_reader.connect() as connection:
        queried=query_events(failed_database,base_date="2027-01-01",meeting_enabled=True,target_batch_id="batch",
            registry_connection=connection,coverage_connection=failed_reader._snapshot_source_connection)
    assert queried["coverage"]=={"status":"failed","records_scope":"approved_versions","returned_record_count":0}
    assert range_reports._meeting_status(queried["coverage"],queried["records"])=="unavailable"


@pytest.mark.parametrize("coverage,expected",[
    ({"status":"succeeded"},"included"),({"status":"succeeded_empty"},"included"),
    ({"status":"complete"},"included"),({"status":"partial"},"partial"),
    ({"status":"failed"},"unavailable"),({"status":"disabled"},"unavailable"),
    ({"status":"enabled_unprocessed"},"unavailable"),({"status":"unavailable"},"unavailable"),
    ({"status":"processed"},"unavailable"),
])
def test_meeting_availability_preserves_legacy_and_unavailable_coverage(coverage,expected):
    from climate_registry.range_reports import _meeting_status
    original=dict(coverage)
    assert _meeting_status(coverage,[{"event_id":"existing-record"}])==expected
    assert coverage==original


def test_explicit_meeting_snapshot_requires_the_current_approved_full_record(tmp_path):
    from test_issue136_meetings import _database as meeting_database,_candidate
    from climate_monitor.meetings import process_batch,freeze_snapshot,load_snapshot
    from climate_registry.range_reports import _meeting_payload
    database=meeting_database(tmp_path,["World Climate Summit 2027 meets June 10–12, 2027 in New York. Registration deadline May 1, 2027. Register https://example.com/register. Climate risk agenda."])
    result=process_batch(database,"batch",prompt_text="source evidence",prompt_version="v1",provider="test",model="test",extractor=lambda request:{"events":[_candidate()]})
    assert result["status"]=="succeeded"
    frozen=freeze_snapshot(database,base_date="2027-01-01",timezone_name="UTC")
    reader=_reader(database,tmp_path)
    assert _meeting_payload(reader,frozen["snapshot_id"],base_date="2027-01-01")["records"]==[]
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit approved meeting fixture"}, status="accepted_legacy")
        event=db.execute("SELECT event_id FROM climate_events").fetchone()[0]
    approved=_meeting_payload(reader,frozen["snapshot_id"],base_date="2027-01-01")
    assert approved["records"]==frozen["records"] and approved["coverage"]=={**frozen["coverage"],"excluded_unapproved_count":0}
    with sqlite3.connect(database) as db:
        db.execute("UPDATE climate_events SET location='Pending new location' WHERE event_id=?",(event,))
        stage_entities(db)
    newer=freeze_snapshot(database,base_date="2027-01-01",timezone_name="UTC")
    assert _meeting_payload(reader,newer["snapshot_id"],base_date="2027-01-01")["records"]==[]
    assert _meeting_payload(reader,frozen["snapshot_id"],base_date="2027-01-01")["records"]==frozen["records"]
    set_visibility(database,"meeting",event,False)
    assert _meeting_payload(reader,frozen["snapshot_id"],base_date="2027-01-01")["records"]==[]
    assert load_snapshot(database,frozen["snapshot_id"])["records"]==frozen["records"]


def test_archived_executive_summary_keeps_exact_source_sha_when_all_articles_hidden(tmp_path):
    from climate_registry.persistent import update_registry,initialize_registry
    source=tmp_path/"sources";source.mkdir()
    report=source/"climate-monitor-2026-09-07.md"
    report.write_text("# Weekly Climate Monitor\n\n**Report Date:** 2026-09-07\n\n## Executive Summary\n- Sites checked: **1**, succeeded: **1**, failed: **0**\n- Immutable historical executive claim.\n\n## Pillar A — Changes\n- **Climate outlook**\n  - Original report observation.\n  🔗 https://example.org/historical\n\n## Pillar B — Intelligence\n\n## Original Links\n- https://example.org/historical\n",encoding="utf-8")
    database=tmp_path/"registry.sqlite3";initialize_registry(database)
    update_registry(source,database,tmp_path/"backups")
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db,source_dir=source):_approve(db,sha,{"basis":"explicit baseline fixture"}, status="accepted_legacy")
        identity=db.execute("SELECT article_id FROM articles").fetchone()[0]
    reader=RegistryReader(database,repository_root=tmp_path/"application",source_dir=source)
    baseline=reader.report("2026-09-07")["executive_summary"]
    assert baseline[-1]=="Immutable historical executive claim."
    set_visibility(database,"article",identity,False)
    assert reader.report("2026-09-07")["executive_summary"]==baseline
    assert reader.articles()["items"]==[]
    report.write_text(report.read_text(encoding="utf-8").replace("Immutable historical","Changed historical"),encoding="utf-8")
    assert reader.report("2026-09-07")["executive_summary"]==[]


@pytest.mark.skipif(os.name=="nt",reason="API management imports POSIX fcntl")
def test_all_wiki_markdown_routes_use_approved_pages_and_missing_pages_404(tmp_path,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api_server import WikiStaticFiles
    from climate_registry.publication import public_wiki_pages
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit approved fixture"}, status="accepted_legacy")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT",raising=False)
    disk=tmp_path/"wiki";disk.mkdir()
    for name in ("climate-monitor-2026-09-07.md","meetings.md","topic-climate-risk.md"):
        (disk/name).write_text("Pending disk-only private claims",encoding="utf-8")
    app=FastAPI();app.mount("/wiki",WikiStaticFiles(directory=disk),name="wiki")
    with TestClient(app) as client:
        pages=public_wiki_pages(database)
        for name,body in pages.items():
            response=client.get("/wiki/"+name)
            assert response.status_code==200 and response.text==body
        for name in ("climate-monitor-2026-09-07.md","meetings.md","topic-climate-risk.md"):
            assert client.get("/wiki/"+name).status_code==404


@pytest.mark.parametrize("legacy",[False,True])
def test_static_registry_and_raw_source_http_obey_current_boundary_without_changing_archives(tmp_path,monkeypatch,legacy):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import api_server
    from climate_registry.publication import public_wiki_pages,export_public_snapshot
    database=_database(tmp_path,version=20 if legacy else 22)
    if not legacy:
        with sqlite3.connect(database) as db:
            for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit approved static HTTP fixture"}, status="accepted_legacy")
    wiki=tmp_path/"wiki";sources=tmp_path/"sources";wiki.mkdir();sources.mkdir()
    snapshot=wiki/"public-registry.json"
    if legacy:snapshot.write_text('{"historical_snapshot":true}',encoding="utf-8")
    else:export_public_snapshot(database,snapshot)
    (wiki/"index.md").write_text("Old disk index",encoding="utf-8")
    (wiki/"article-a.md").write_text("Old disk article https://example.org/a",encoding="utf-8")
    source=sources/"climate-monitor-2026-09-07.md";source.write_text("Immutable source archive https://example.org/a",encoding="utf-8")
    original={p:p.read_bytes() for p in (snapshot,source)}
    app=FastAPI();app.mount("/wiki",api_server.WikiStaticFiles(directory=wiki));app.mount("/sources",api_server.SourceStaticFiles(directory=sources))
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database));monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT",raising=False)
    monkeypatch.setattr(api_server,"_registry_reader",lambda:_reader(os.getenv("CLIMATE_REGISTRY_DB"),tmp_path))
    client=TestClient(app,raise_server_exceptions=False);api=TestClient(api_server.app,raise_server_exceptions=False)
    assert api.get("/api/registry/articles/a").status_code==200
    if not legacy:
        assert client.get("/wiki/article-a.md").status_code==200
        set_visibility(database,"article","a",False)
        assert api.get("/api/registry/articles/a").status_code==404
        assert client.get("/wiki/article-a.md").status_code==404
        assert client.get("/wiki/index.md").text==public_wiki_pages(database)["index.md"]
    for method in (client.get,client.head):
        for url in ("/wiki/public-registry.json","/sources/"+source.name):
            assert method(url).status_code==(200 if legacy else 404)
    # Every configured outage fails closed, even if disk bytes are readable.
    corrupt=tmp_path/"corrupt.sqlite3";corrupt.write_bytes(b"invalid sqlite fixture")
    wrong=tmp_path/"wrong-schema.sqlite3"
    with sqlite3.connect(wrong) as db:db.execute("PRAGMA user_version=999")
    for invalid in (str(tmp_path/"missing.sqlite3"),"relative.sqlite3",str(corrupt),str(wrong)):
        monkeypatch.setenv("CLIMATE_REGISTRY_DB",invalid)
        assert api.get("/api/registry/status").status_code==503
        for method in (client.get,client.head):
            for url in ("/wiki/public-registry.json","/wiki/index.md","/sources/"+source.name):
                response=method(url)
                assert response.status_code==503
                if method==client.get:assert response.json()=={"detail":"Article registry is unavailable."}
                assert str(tmp_path) not in response.text and "relative.sqlite3" not in response.text
        assert api.get("/api/health").status_code==200 and api.get("/").status_code==200
    monkeypatch.delenv("CLIMATE_REGISTRY_DB")
    assert client.get("/wiki/public-registry.json").content==original[snapshot]
    assert client.get("/sources/"+source.name).content==original[source]
    assert {p:p.read_bytes() for p in original}==original


def test_new_range_uses_approved_annotations_dates_and_render_public_dto(tmp_path,monkeypatch):
    from climate_registry.annotations import ArticleAnnotation
    from climate_registry.publication import export_public_snapshot,install_git_snapshot
    from climate_registry.range_reports import _range_source,freeze_range_report
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_date_observations VALUES('date-a','a','https://example.org/a','page_information','2026-08-31','test','test.db','articles','a',?,'2026-09-01T00:00:00Z')",(json.dumps({"date_basis":"publisher_page","text":"Updated 31 August 2026"}),))
        annotation=ArticleAnnotation("https://example.org/a","https://example.org/a","Approved title","original_content","Approved summary",("Climate Risk",),("insurance",),"2026-09-01")
        first=stage_snapshot(db,snapshot_entity(db,"article","a",annotations={"https://example.org/a":annotation}))
        _approve(db,first,{"basis":"explicit approved annotation fixture"}, status="accepted_legacy")
        # A later candidate retains the old published DTO while pending.
        db.execute("UPDATE article_versions SET observed_title='Pending title',observed_summary='Pending summary' WHERE article_id='a'")
        stage_entities(db,annotations={})
    reader=_reader(database,tmp_path)
    expected=reader.article("a")
    ranged=_range_source(reader,"2026-08-31","2026-08-31")["articles"][0]
    assert {key:ranged[key] for key in ("title","summary","categories","keywords","publisher")}=={key:expected[key] for key in ("title","summary","categories","keywords","publisher")}
    assert ranged["range_date"]=="2026-08-31" and ranged["date_basis"]=="information_date"
    assert ranged["provenance"]["summary"]["candidate_sha256"]==first
    wiki=tmp_path/"application/wiki";wiki.mkdir(parents=True)
    export_public_snapshot(database,wiki/"public-registry.json")
    rendered=tmp_path/"render.sqlite3"
    with sqlite3.connect(rendered) as db:apply_migrations(db)
    db.close();install_git_snapshot(rendered,wiki)
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    render_reader=_reader(rendered,tmp_path)
    result=_range_source(render_reader,"2026-08-31","2026-08-31")["articles"]
    assert result==[ranged]
    frozen=freeze_range_report(render_reader,tmp_path/"reports",start_date="2026-08-31",end_date="2026-08-31")
    assert frozen["articles"]==[ranged]


def test_export_and_wiki_include_all_approved_meetings_beyond_first_page(tmp_path,monkeypatch):
    from test_issue136_meetings import _database as meeting_database,_candidate
    from climate_monitor.meetings import process_batch
    from climate_registry.publication import export_public_snapshot,install_git_snapshot
    names=[f"World Climate Summit {index}" for index in range(101)]
    body="\n".join(name+" meets June 10–12, 2027 in New York. Example hosts it. Registration deadline May 1, 2027. Register https://example.com/register. Climate risk agenda." for name in names)
    database=meeting_database(tmp_path,[body])
    result=process_batch(database,"batch",prompt_text="source evidence",prompt_version="v1",provider="test",model="test",
        extractor=lambda request:{"events":[_candidate(name=name,deadline_type=None,deadline_date=None,deadline_evidence=None) for name in names]})
    assert result["status"]=="succeeded"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM climate_events").fetchone()[0]==101
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit all-meeting fixture approval"}, status="accepted_legacy")
    wiki=tmp_path/"application/wiki";wiki.mkdir(parents=True)
    export_public_snapshot(database,wiki/"public-registry.json")
    artifact=json.loads((wiki/"public-registry.json").read_text(encoding="utf-8"))
    assert len(artifact["meeting_records"])==101
    markdown=artifact["wiki_pages"]["registry-meetings.md"]
    assert all("## "+name+"\n" in markdown for name in names)
    assert markdown.endswith("\n") and not markdown.endswith("\n\n")
    rendered=tmp_path/"render.sqlite3"
    with sqlite3.connect(rendered) as db:apply_migrations(db)
    db.close();install_git_snapshot(rendered,wiki)
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    restored=_reader(rendered,tmp_path).meetings_all(base_date="2027-01-01")
    assert len(restored)==101 and {item["name"] for item in restored}==set(names)
    from climate_registry.range_reports import freeze_range_report
    frozen=freeze_range_report(_reader(rendered,tmp_path),tmp_path/"range-reports",start_date="2026-09-01",end_date="2026-09-01")
    assert len(frozen["meeting"]["records"])==101
    monkeypatch.delenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT")
    from scripts.sync_source_wiki import sync_source_wiki
    sources=tmp_path/"sources";sources.mkdir()
    sync_source_wiki(source_dir=sources,wiki_dir=wiki,cadence="weekly",registry_database=database)
    assert (wiki/"registry-meetings.md").is_file()
    with sqlite3.connect(database) as db:db.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_kind='meeting'")
    hidden=sync_source_wiki(source_dir=sources,wiki_dir=wiki,cadence="weekly",registry_database=database)
    assert "registry-meetings.md" in hidden.pruned_pages and not (wiki/"registry-meetings.md").exists()
    assert "registry-meetings.md" not in json.loads((wiki/"public-registry.json").read_text(encoding="utf-8"))["wiki_pages"]


@pytest.mark.skipif(os.name=="nt",reason="acquisition review locks require POSIX fcntl")
def test_all_acquisition_review_writes_fail_closed_after_database_switch(tmp_path,monkeypatch):
    from test_issue113_range_reports import _database as acquisition_database
    from climate_registry.acquisition_review import queue_acquisition_review,correct_candidate,review_acquisition,claim_review,claim_acquisition,recover_acquisition,activate_approved
    import shutil
    database=acquisition_database(tmp_path,target_version=22)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    run=tmp_path/"run";run.mkdir()
    binding={"registry_database":str(database),"run_id":"run","acquisition_batch_id":"batch","attempt":1,"task_version":3,"source_keys":["example"]}
    state=queue_acquisition_review(run/"binding.json",binding,{"source_outcomes":[]})
    root=run/"acquisition-review"
    Path(state["packet"]["attempt_result_path"]).write_text(json.dumps({"run_id":"run","attempt":1,"finished_at":"2026-10-08T00:00:00Z"}),encoding="utf-8")
    other=tmp_path/"other.sqlite3";shutil.copyfile(database,other)
    before_a=database.read_bytes();before_b=other.read_bytes()
    before_files={str(path.relative_to(root)):path.read_bytes() for path in root.rglob("*") if path.is_file() and path.name!=".review.lock"}
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(other))
    key=state["packet"]["candidates"][0]["identity"]["acquisition_item_id"]
    for operation in (
        lambda:queue_acquisition_review(run/"binding.json",binding,{"source_outcomes":[]}),
        lambda:correct_candidate(root,key,{"summary":"Derived correction"},reason="actual source correction"),
        lambda:review_acquisition(root,"unused-token",{"items":{}}),
        lambda:claim_review(root,state["packet"],session_id="cron_independent",execution_id="run",hermes_database=tmp_path/"unused-hermes.db",reviewer="native"),
        lambda:claim_acquisition(root,session_id="cron_independent",execution_id="run",hermes_database=tmp_path/"unused-hermes.db",reviewer="native"),
        lambda:recover_acquisition(root,"unused-token",None),
        lambda:activate_approved(root,queue_dir=tmp_path/"queue",database=database,repository_root=tmp_path/"application"),
    ):
        with pytest.raises(ValueError,match="frozen task binding"):operation()
        assert database.read_bytes()==before_a and other.read_bytes()==before_b
        assert {str(path.relative_to(root)):path.read_bytes() for path in root.rglob("*") if path.is_file() and path.name!=".review.lock"}==before_files


def test_registry_review_claim_rejects_switched_database_before_claim_history(tmp_path,monkeypatch):
    from climate_registry.publication import prepare_review
    from climate_registry.acquisition_review import claim_review
    database=_database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    root=tmp_path/"review";packet=prepare_review(database,root)
    other=tmp_path/"other.sqlite3";other.write_bytes(database.read_bytes())
    before={str(path.relative_to(root)):path.read_bytes() for path in root.rglob("*") if path.is_file()}
    before_a,before_b=database.read_bytes(),other.read_bytes()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(other))
    with pytest.raises(ValueError,match="frozen task binding"):
        claim_review(root,packet,session_id="cron_independent",execution_id="run",hermes_database=tmp_path/"unused-hermes.db",reviewer="native")
    assert {str(path.relative_to(root)):path.read_bytes() for path in root.rglob("*") if path.is_file()}==before
    assert database.read_bytes()==before_a and other.read_bytes()==before_b


def test_render_new_range_preserves_approved_pdf_summary_coverage_and_citations(tmp_path,monkeypatch):
    from climate_registry.publication import export_public_snapshot,install_git_snapshot
    from climate_registry.range_reports import _range_source,freeze_range_report
    database=_database(tmp_path)
    occurrence={"summary":"Original PDF observation","summary_basis":"verbatim_pdf_article_row","raw_url":"https://pdf.example/observed",
        "anchor_text":"Original PDF title","page":2,"source_document_sha256":"f"*64,"content_sha256":"d"*64}
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-only",url=occurrence["raw_url"],occurrence=occurrence,period=("2026-09-01","2026-09-07"))
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit approved PDF fixture"}, status="accepted_legacy")
    reader=_reader(database,tmp_path)
    live=_range_source(reader,"2026-09-01","2026-09-07")["pdf_source_updates"]
    assert len(live)==1
    wiki=tmp_path/"application/wiki";wiki.mkdir(parents=True)
    export_public_snapshot(database,wiki/"public-registry.json")
    rendered=tmp_path/"render.sqlite3"
    with sqlite3.connect(rendered) as db:apply_migrations(db)
    db.close();install_git_snapshot(rendered,wiki)
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    frozen=freeze_range_report(_reader(rendered,tmp_path),tmp_path/"ranges",start_date="2026-09-01",end_date="2026-09-07")
    restored=frozen["pdf_source_updates"]
    fields=("title","summary","publication_date","publication_date_label","coverage_period","citations","observed_at")
    assert [{key:item[key] for key in fields} for item in restored]==[{key:item[key] for key in fields} for item in live]
    assert "/evidence/source.pdf" not in json.dumps(restored)


def test_public_dto_range_keeps_collection_priority_over_in_range_page_date(tmp_path,monkeypatch):
    from climate_registry.range_reports import _range_source
    from climate_registry.publication import export_public_snapshot,install_git_snapshot
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        for identity,kind,stamp in (("page","page_information","2026-08-31"),("collection","collection","2026-09-01T12:00:00Z")):
            db.execute("INSERT INTO article_date_observations VALUES(?,'a','https://example.org/a',?,?,'test','test.db','articles','a',?,'2026-09-01T00:00:00Z')",(identity,kind,stamp,json.dumps({"text":stamp})))
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit date priority fixture"}, status="accepted_legacy")
    live=_reader(database,tmp_path)
    assert _range_source(live,"2026-08-31","2026-08-31")["articles"]==[]
    selected=_range_source(live,"2026-09-01","2026-09-01")["articles"]
    assert selected[0]["date_basis"]=="collection_time"
    wiki=tmp_path/"application/wiki";wiki.mkdir(parents=True);export_public_snapshot(database,wiki/"public-registry.json")
    rendered=tmp_path/"render.sqlite3"
    with sqlite3.connect(rendered) as db:apply_migrations(db)
    db.close();install_git_snapshot(rendered,wiki);monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    static=_reader(rendered,tmp_path)
    assert _range_source(static,"2026-08-31","2026-08-31")["articles"]==[]
    assert _range_source(static,"2026-09-01","2026-09-01")["articles"]==selected


def test_new_range_live_and_render_use_the_same_approved_body(tmp_path,monkeypatch):
    from climate_registry.publication import export_public_snapshot,install_git_snapshot
    from climate_registry.range_reports import _range_source
    database=_database(tmp_path)
    body="# Approved evidence\n\nClimate risk body with insurance observations."
    sha=hashlib.sha256(body.encode()).hexdigest()
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_content_versions VALUES('body-a','a',?,?,?,'text/markdown',?,'reader','v1','2026-09-01T00:00:00Z')",(sha,body,sha,len(body)))
        db.execute("UPDATE articles SET current_content_version_id='body-a',display_policy='full_markdown' WHERE article_id='a'")
        db.execute("INSERT INTO article_fetches(fetch_id,article_id,requested_url,final_url,fetched_at,fetch_status,http_status,content_type,content_version_id) VALUES('fetch-body-a','a','https://example.org/a','https://example.org/a','2026-09-01T00:00:00Z','success',200,'text/markdown','body-a')")
        db.execute("INSERT INTO article_date_observations VALUES('date-a','a','https://example.org/a','page_information','2026-08-31','test','test.db','articles','a','{}','2026-09-01T00:00:00Z')")
        for candidate in stage_entities(db):_approve(db,candidate,{"basis":"explicit body fixture approval"}, status="accepted_legacy")
    reader=_reader(database,tmp_path)
    live=_range_source(reader,"2026-09-01","2026-09-01")["articles"]
    assert live[0]["content"]==reader.article("a")["available_content"]["markdown"]
    wiki=tmp_path/"application/wiki";wiki.mkdir(parents=True);export_public_snapshot(database,wiki/"public-registry.json")
    rendered=tmp_path/"render.sqlite3"
    with sqlite3.connect(rendered) as db:apply_migrations(db)
    db.close();install_git_snapshot(rendered,wiki);monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    static=_range_source(_reader(rendered,tmp_path),"2026-09-01","2026-09-01")["articles"]
    assert static[0]["content"]==live[0]["content"]
    assert static[0]["content_version_id"]==live[0]["content_version_id"]=="body-a"


def test_current_dated_wiki_uses_approved_dto_and_keeps_source_report_identity(tmp_path):
    from climate_registry.annotations import ArticleAnnotation
    from climate_registry.persistent import initialize_registry,update_registry
    from climate_registry.publication import public_wiki_pages,export_public_snapshot
    source=tmp_path/"sources";source.mkdir()
    report=source/"climate-monitor-2026-09-07.md"
    report.write_text("# Weekly Climate Monitor\n**Report Date:** 2026-09-07\n## Executive Summary\n- Sites checked: **1**, succeeded: **1**, failed: **0**\n## Pillar A — Changes\n- **Observed title**\n  - Original observed summary.\n  🔗 https://example.org/a\n## Pillar B — Intelligence\n## Original Links\n- https://example.org/a\n",encoding="utf-8")
    original=report.read_bytes();report_sha=hashlib.sha256(original).hexdigest()
    database=tmp_path/"registry.sqlite3";initialize_registry(database);update_registry(source,database,tmp_path/"backups")
    annotation=ArticleAnnotation("https://example.org/a","https://example.org/a","Approved title","original_content","Approved summary",("Climate Risk",),("insurance",),"2026-09-01")
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db,annotations={"https://example.org/a":annotation},source_dir=source):
            _approve(db,sha,{"basis":"explicit weekly annotation fixture"}, status="accepted_legacy")
        db.execute("UPDATE article_versions SET observed_title='Pending title',observed_summary='Pending summary'")
        stage_entities(db,annotations={},source_dir=source)
    current=public_wiki_pages(database)[report.name]
    assert "## Approved title\nApproved summary" in current
    assert "Source report: "+report.name+"; SHA-256: "+report_sha in current
    assert "Observed title" not in current and "Pending title" not in current
    assert current.endswith("\n") and not current.endswith("\n\n")
    path=tmp_path/"public.json";export_public_snapshot(database,path,source_dir=source)
    exported=json.loads(path.read_text(encoding="utf-8"))["wiki_pages"][report.name]
    assert "## Approved title\nApproved summary" in exported and report_sha in exported
    assert exported.endswith("\n") and not exported.endswith("\n\n")
    assert report.read_bytes()==original


def test_render_real_audit_seed_reuses_existing_hostname_for_new_git_article(tmp_path,monkeypatch):
    from climate_registry.audit import build_audit_registry,_stable_id
    from climate_registry.persistent import initialize_registry,update_registry
    from climate_registry.publication import export_public_snapshot,install_git_snapshot
    source=tmp_path/"sources";source.mkdir()
    (source/"climate-monitor-2026-09-07.md").write_text("# Weekly Climate Monitor\n**Report Date:** 2026-09-07\n## Executive Summary\n- Sites checked: **1**, succeeded: **1**, failed: **0**\n## Pillar A — Changes\n- **Observed title**\n  - Original observed summary.\n  🔗 https://example.org/observed\n## Pillar B — Intelligence\n## Original Links\n- https://example.org/observed\n",encoding="utf-8")
    database=tmp_path/"production-copy.sqlite3";initialize_registry(database);update_registry(source,database,tmp_path/"backups")
    url="https://example.org/new";identity=_stable_id("article",url)
    with sqlite3.connect(database) as db:
        existing=db.execute("SELECT source_id FROM sources WHERE hostname='example.org'").fetchone()[0]
        db.execute("INSERT INTO articles(article_id,canonical_url,source_id,first_seen,last_seen) VALUES(?,?,?,'2026-09-08','2026-09-08')",(identity,url,existing))
        db.execute("INSERT INTO article_versions VALUES('new-version',?,'New approved title','New approved title','New approved summary','new-fingerprint','report-title-summary','2026-09-08','2026-09-08')",(identity,))
        db.execute("UPDATE articles SET current_version_id='new-version' WHERE article_id=?",(identity,))
        for sha in stage_entities(db,source_dir=source):_approve(db,sha,{"basis":"explicit approved startup fixture"}, status="accepted_legacy")
    wiki=tmp_path/"application/wiki";wiki.mkdir(parents=True);export_public_snapshot(database,wiki/"public-registry.json",source_dir=source)
    rendered=tmp_path/"render.sqlite3";build_audit_registry(source,rendered,tmp_path/"render-audit")
    with sqlite3.connect(rendered) as db:
        existing_source=db.execute("SELECT source_id FROM sources WHERE hostname='example.org'").fetchone()[0]
    install_git_snapshot(rendered,wiki)
    with sqlite3.connect(rendered) as db:
        assert db.execute("SELECT source_id FROM articles WHERE article_id=?",(identity,)).fetchone()[0]==existing_source
        assert db.execute("SELECT count(*) FROM sources WHERE hostname='example.org'").fetchone()[0]==1
        assert db.execute("PRAGMA foreign_key_check").fetchall()==[]
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT","1")
    detail=_reader(rendered,tmp_path).article(identity)
    assert detail["title"]=="New approved title"
    assert detail["report_summary"]=="New approved summary"


@pytest.mark.parametrize("with_report",[False,True])
def test_article_information_date_never_manufactures_report_coverage(tmp_path,monkeypatch,with_report):
    from agentic_wiki import AgenticWikiResponder
    from climate_registry.persistent import update_registry
    database=_database(tmp_path)
    sources=tmp_path/"sources";sources.mkdir()
    wiki=tmp_path/"wiki";wiki.mkdir()
    if with_report:
        (sources/"climate-monitor-2026-09-14.md").write_text("# Weekly Climate Monitor\n**Report Date:** 2026-09-14\n## Executive Summary\n- Sites checked: **1**, succeeded: **1**, failed: **0**\n## Pillar A — Changes\n- **Observed title**\n  - Actual report observation.\n  🔗 https://report.example/a\n## Pillar B — Intelligence\n## Original Links\n- https://report.example/a\n",encoding="utf-8")
        update_registry(sources,database,tmp_path/"backups")
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit report/date fixture approval"}, status="accepted_legacy")
        snapshot=snapshot_entity(db,"article","a")
        snapshot["derived_display"]={"title":"Climate evidence updated 2026-09-01","summary":"Approved article evidence."}
        sha=stage_snapshot(db,snapshot);_approve(db,sha,{"basis":"explicit article/date fixture approval"}, status="accepted_legacy")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    responder=AgenticWikiResponder(wiki_dir=wiki,source_dir=sources);responder.client=None
    article=next(doc for doc in responder.kb.documents if doc.file=="article-a.md")
    assert article.date=="2026-09-01" and article.type!="daily"
    expected=["2026-09-14"] if with_report else []
    assert sorted(path.name.removeprefix("climate-monitor-").removesuffix(".md") for path in sources.glob("climate-monitor-*.md"))==expected
    assert responder.kb.report_dates==expected
    assert responder.kb.latest_date==("2026-09-14" if with_report else "")
    assert responder.kb.source_documents==[]
    if with_report:
        result=responder.answer("Summarize the past 2 weeks of reports.",language="en",answer_mode="executive")
        assert "Reports with evidence: 1" in result["text"]
        assert "- 2026-09-14:" in result["text"] and "- 2026-09-01:" not in result["text"]


def test_terminal_partial_t1_is_per_item_and_running_missing_or_stale_evidence_waits(tmp_path,monkeypatch):
    from climate_registry.information_checks import run_checks
    from climate_registry.publication import prepare_review,information_ready
    database=_database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit preexisting public fixture"},status="accepted_legacy")
        _pdf(db,identity="pdf-one",url="https://pdf.example/one")
        db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES('pdf-two','https://pdf.example/two','Second PDF','2026-08-01T00:00:00Z')")
        db.execute("""INSERT INTO pdf_intake_article_occurrences SELECT 'second-occurrence','pdf-two',source_document_sha256,page,
            'https://pdf.example/two',report_date,publication_date,content_sha256,page_sha256,occurrence_json
            FROM pdf_intake_article_occurrences WHERE occurrence_id='occurrence'""")
        stage_entities(db)
    root=tmp_path/"runs/registry-review"
    assert prepare_review(database,root)["candidates"]==[]
    calls=[]
    def unavailable(identity,url):
        calls.append((identity,url))
        raise RuntimeError("actual fixture source attempt unavailable")
    checked=run_checks(database,kind="articles",backup_dir=tmp_path/"backups",occurrence_ids={"occurrence","second-occurrence"},limit=1,fetcher=unavailable)
    assert checked["status"]=="pending" and checked["completed_count"]==1
    assert prepare_review(database,root)["candidates"]==[]
    checked=run_checks(database,kind="articles",backup_dir=tmp_path/"backups",resume_run_id=checked["run_id"],fetcher=unavailable)
    assert checked["status"]=="partial" and checked["completed_count"]==2 and len(calls)==2
    packet=prepare_review(database,root)
    assert {item["entity_id"] for item in packet["candidates"]}=={"pdf-one","pdf-two"}
    with sqlite3.connect(database) as db:
        for item in packet["candidates"]:
            snapshot=json.loads(Path(item["snapshot_path"]).read_text())
            assert information_ready(db,snapshot)
            attempts=snapshot["tables"]["article_check_attempts"]
            assert len(attempts)==1 and attempts[0]["access_status"]=="failed"
        # A newer real run that has not finished invalidates old readiness.
    newer=run_checks(database,kind="articles",backup_dir=tmp_path/"backups",occurrence_ids={"occurrence","second-occurrence"},limit=1,fetcher=unavailable)
    assert newer["status"]=="pending"
    with sqlite3.connect(database) as db:
        snapshot=json.loads(Path(packet["candidates"][0]["snapshot_path"]).read_text())
        assert not information_ready(db,snapshot)
    assert prepare_review(database,root)["candidates"]==[]
    run_checks(database,kind="articles",backup_dir=tmp_path/"backups",resume_run_id=newer["run_id"],fetcher=unavailable)
    packet=prepare_review(database,root)
    statuses={item["entity_id"]:"pass" if item["entity_id"]=="pdf-one" else "needs_correction" for item in packet["candidates"]}
    _approve_native_packet(database,root,tmp_path/"hermes.sqlite3","cron_terminal_partial",statuses=statuses)
    assert {item["article_id"] for item in _reader(database,tmp_path).pdf_articles_all()}=={"pdf-one"}
    assert len(prepare_review(database,root)["candidates"])==1


def test_storage_only_21_to_22_preserves_pdf_rows_packets_and_every_public_pointer(tmp_path):
    database=_database(tmp_path,version=21)
    with sqlite3.connect(database) as db:
        _pdf(db,identity="pdf-history",url="https://pdf.example/history")
        packet={"occurrence_id":"occurrence","source_url":"https://pdf.example/history","reader":{},"checked_at":"2026-08-02T00:00:00Z","access_status":"failed","verification_status":"unchecked"}
        frozen={"kind":"articles","targets":[],"registry_database":str(database)}
        encoded=json.dumps(frozen,sort_keys=True)
        db.execute("INSERT INTO article_check_runs VALUES(?,?,?,?,?,?,?,?,?)",("historical",encoded,hashlib.sha256(encoded.encode()).hexdigest(),"2026-08-02T00:00:00Z","2026-08-02T00:00:00Z","partial",1,1,None))
        from climate_registry.information_checks import _sha
        db.execute("INSERT INTO article_check_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",("old-attempt","historical","occurrence","https://pdf.example/history","a"*64,packet["checked_at"],"failed","unchecked",json.dumps(packet),_sha(packet)))
        for sha in stage_entities(db):_approve(db,sha,{"basis":"actual old21 public fixture baseline"},status="accepted_legacy")
        db.execute("UPDATE articles SET publication_eligible=0 WHERE article_id='b'")
        stage_entities(db)
        db.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_id='pdf-history'")
        immutable={table:db.execute('SELECT * FROM '+table).fetchall() for table in ("article_check_runs","registry_candidates","registry_reviews","registry_publication")}
        old_attempt=db.execute("SELECT rowid,* FROM article_check_attempts").fetchall()
    db.close()
    before=database.read_bytes()
    public=_reader(database,tmp_path).articles()
    result=migrate_publication(database,tmp_path/"backups",apply=True)
    assert result["schema_version"]==22 and result["accepted_legacy"]==result["manual_materialized"]==result["manual_published"]==result["staged"]==0
    assert Path(result["backup"]).read_bytes()==before
    with sqlite3.connect(database) as db:
        assert validate_registry_contract(db)==22
        assert db.execute("PRAGMA foreign_key_check").fetchall()==[]
        for table,rows in immutable.items():assert db.execute('SELECT * FROM '+table).fetchall()==rows
        migrated=db.execute("SELECT rowid,* FROM article_check_attempts").fetchall()
        assert [row[:-1] for row in migrated]==old_attempt and all(row[-1] is None for row in migrated)
    assert _reader(database,tmp_path).articles()==public
    before, inode=database.read_bytes(),database.stat().st_ino
    repeated=migrate_publication(database,tmp_path/"backups",apply=True)
    assert repeated["backup"] is None and repeated["staged"]==repeated["accepted_legacy"]==0
    assert database.read_bytes()==before and database.stat().st_ino==inode


def test_storage_22_constraints_keep_real_source_foreign_keys_and_immutable_attempts(tmp_path):
    database=_database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO article_check_runs VALUES('run','{}',?,'2026-09-01T00:00:00Z',NULL,'running',1,0,NULL)",("f"*64,))
        _pdf(db,identity="pdf-source",url="https://pdf.example/source")
        values=("attempt","run",None,"https://example.org/a","a"*64,"2026-09-01T00:00:00Z","failed","unchecked","{}","b"*64)
        for occurrence,core,run in ((None,None,"run"),("occurrence","a","run"),("missing",None,"run"),(None,"missing","run"),(None,"a","missing")):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("INSERT INTO article_check_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",(values[0],run,occurrence,*values[3:],core))
        db.execute("INSERT INTO article_check_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",(*values,"a"))
        with pytest.raises(sqlite3.IntegrityError):db.execute("INSERT INTO article_check_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",("duplicate",*values[1:],"a"))
        with pytest.raises(sqlite3.IntegrityError):db.execute("UPDATE article_check_attempts SET core_article_id='b'")
        with pytest.raises(sqlite3.IntegrityError):db.execute("DELETE FROM article_check_attempts")


def test_21_storage_upgrade_failure_rolls_back_without_changing_public_data(tmp_path,monkeypatch):
    import climate_registry.schema as schema
    database=_database(tmp_path,version=21)
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):_approve(db,sha,{"basis":"prior published fixture"},status="accepted_legacy")
    before=database.read_bytes()
    old=schema.MIGRATIONS
    monkeypatch.setattr(schema,"MIGRATIONS",old[:-1]+((22,old[-1][1],old[-1][2]+"SELECT * FROM missing_migration_table;"),))
    with pytest.raises(sqlite3.OperationalError):migrate_publication(database,tmp_path/"backups",apply=True)
    assert database.read_bytes()==before
    with sqlite3.connect(database) as db:assert validate_registry_contract(db)==21


def test_default_core_t1_freezes_real_sources_and_body_change_requires_new_improvement(tmp_path,monkeypatch):
    from climate_registry.information_checks import run_checks,attempt_identity
    from climate_registry.publication import prepare_review,information_ready
    database=_database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    calls=[]
    def unavailable(identity,url):
        calls.append((identity,url));raise RuntimeError("actual terminal fixture source unavailable")
    result=run_checks(database,kind="articles",backup_dir=tmp_path/"backups",fetcher=unavailable)
    assert result["status"]=="partial" and result["item_count"]==result["completed_count"]==2
    assert set(calls)=={("core:a","https://example.org/a"),("core:b","https://example.org/b")}
    root=tmp_path/"runs/registry-review";packet=prepare_review(database,root)
    assert {item["entity_id"] for item in packet["candidates"]}=={"a","b"}
    with sqlite3.connect(database) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute("SELECT * FROM article_check_attempts").fetchall()
        assert {row["core_article_id"] for row in rows}=={"a","b"} and all(row["occurrence_id"] is None for row in rows)
        assert {attempt_identity(row,"article") for row in rows}=={"core:a","core:b"}
        for row in rows:
            actual=json.loads(row["packet_json"])
            assert actual["source_binding"]["article_id"]==actual["entity_id"]==row["core_article_id"]
    _approve_native_packet(database,root,tmp_path/"hermes.sqlite3","cron_core_improved")
    old=_reader(database,tmp_path).article("a")
    with sqlite3.connect(database) as db:
        body="New actual source body";sha=hashlib.sha256(body.encode()).hexdigest()
        db.execute("INSERT INTO article_content_versions VALUES('new-body','a',?,?,?,'text/markdown',?,'test','v1','2026-09-03T00:00:00Z')",(sha,body,sha,len(body)))
        db.execute("UPDATE articles SET current_content_version_id='new-body' WHERE article_id='a'")
        stage_entities(db)
    assert prepare_review(database,root)["candidates"]==[]
    assert _reader(database,tmp_path).article("a")==old
    run_checks(database,kind="articles",backup_dir=tmp_path/"backups",occurrence_ids={"core:a"},fetcher=unavailable)
    assert {item["entity_id"] for item in prepare_review(database,root)["candidates"]}=={"a"}


@pytest.mark.parametrize("partial",[False,True])
def test_normal_native_meeting_waits_for_matching_actual_t1_source_attempt(tmp_path,monkeypatch,partial):
    from test_issue197_workflow import _native_meeting_material_fixture
    from climate_registry.information_checks import run_checks
    from climate_registry.publication import prepare_review
    database,clock,process,approve=_native_meeting_material_fixture(tmp_path,monkeypatch,partial=partial)
    result=process("first")
    assert result["status"]==("partial" if partial else "succeeded")
    root=tmp_path/"runs/registry-review"
    assert prepare_review(database,root)["candidates"]==[]
    calls=[]
    def unavailable(identity,url):calls.append((identity,url));raise RuntimeError("terminal native source fixture unavailable")
    checked=run_checks(database,kind="meetings",backup_dir=tmp_path/"backups",fetcher=unavailable)
    assert checked["status"]=="partial" and checked["item_count"]==checked["completed_count"]==1
    packet=prepare_review(database,root)
    meeting=next(item for item in packet["candidates"] if item["entity_kind"]=="meeting")
    snapshot=json.loads(Path(meeting["snapshot_path"]).read_text())
    attempts=snapshot["tables"]["meeting_check_attempts"]
    assert len(attempts)==1 and attempts[0]["occurrence_id"] is None
    assert attempts[0]["event_source_id"]==snapshot["information_targets"][0]["occurrence_id"]==calls[0][0]
    assert json.loads(attempts[0]["packet_json"])["entity_id"]==meeting["entity_id"]
    approved=approve("native_t1_source","2026-10-10T12:00:00Z")
    record=_reader(database,tmp_path).meetings_all(base_date="1900-01-01")[0]
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind='meeting'").fetchone()[0]==approved
    assert record["sources"][0]["source_url"]==calls[0][1]


@pytest.mark.skipif(os.name=="nt",reason="real governed acquisition persistence requires POSIX fcntl")
def test_actual_source_queue_waits_for_t1_and_refreezes_only_the_same_original_binding(tmp_path,monkeypatch):
    from test_issue112_acquisition import _database as empty_database,_batch,_item
    from climate_registry.acquisition import store_acquisition_batch
    from climate_registry.acquisition_review import queue_acquisition_review,claim_acquisition
    from climate_registry.information_checks import run_checks
    from climate_registry.publication import prepare_review
    from climate_delivery.io import atomic_write_json
    from scripts.review_pipeline import pending
    database=empty_database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    payload=_batch([_item()]);store_acquisition_batch(database,payload)
    runs=tmp_path/"runs";run=runs/"source-run"
    binding={"registry_database":str(database),"run_id":"source-run","acquisition_batch_id":payload["batch_id"],"attempt":1,"task_version":3,"source_keys":["example"]}
    atomic_write_json(run/"binding.json",binding)
    atomic_write_json(run/"attempt-1-acquisition.json",payload)
    atomic_write_json(run/"attempt-1-result.json",{"run_id":"source-run","attempt":1,"finished_at":"2026-09-30T12:05:00Z"})
    state=queue_acquisition_review(run/"binding.json",binding,payload);review=run/"acquisition-review"
    old=state["packet"];old_sha=state["packet_sha256"]
    before={str(path):path.read_bytes() for path in run.rglob("*") if path.is_file()};raw=database.read_bytes()
    assert pending("acquisition",runs) is None
    with pytest.raises(ValueError,match="completed source-bound T1"):
        claim_acquisition(review,session_id="cron_not_yet",execution_id="early",reviewer="fixture",hermes_database=tmp_path/"no-native.db")
    assert database.read_bytes()==raw and before=={str(path):path.read_bytes() for path in run.rglob("*") if path.is_file()}
    checked=run_checks(database,kind="articles",backup_dir=tmp_path/"backups",fetcher=lambda *_:(_ for _ in ()).throw(RuntimeError("real terminal unavailable fixture")))
    assert checked["status"]=="partial" and checked["item_count"]==checked["completed_count"]==1
    prepare_review(database,runs/"registry-review")
    current=json.loads((review/"state.json").read_text())
    assert current["packet_sha256"]!=old_sha
    assert current["packet"]["binding_sha256"]==old["binding_sha256"]==digest(binding)
    assert current["packet"]["candidates"][0]["identity"]==old["candidates"][0]["identity"]
    assert current["packet"]["candidates"][0]["registry_candidate_sha256"]!=old["candidates"][0]["registry_candidate_sha256"]
    assert (review/"packets"/(old_sha+".json")).read_bytes()==before[str(review/"packets"/(old_sha+".json"))]
    assert (run/"binding.json").read_bytes()==before[str(run/"binding.json")]
    assert current["history"]==state["history"] and not (review/"claim.json").exists()
    assert pending("acquisition",runs)[1]["packet"]==current["packet"]
    with pytest.raises(ValueError,match="completed source-bound T1"):
        from climate_registry.acquisition_review import claim_review
        claim_review(review,old,session_id="cron_stale",execution_id="stale",reviewer="fixture",hermes_database=tmp_path/"no-native.db")


def test_final_prepare_keeps_the_native_owner_packet_and_stale_receipt_cannot_publish(tmp_path,monkeypatch):
    from test_issue197_workflow import _native,_native_read
    from climate_registry.acquisition_review import claim_review
    from climate_registry.publication import prepare_review,review_candidates
    from climate_registry.information_checks import run_checks
    database=_database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    _complete_fixture_information(database)
    root=tmp_path/"runs/registry-review";packet=prepare_review(database,root)
    hermes=tmp_path/"native.db";session="cron_exact_owner"
    _native(hermes,session)
    for index,item in enumerate(packet["candidates"]):
        path=Path(item["snapshot_path"])
        _native_read(hermes,session,path,path.read_text(),"snapshot-"+str(index))
    claim=claim_review(root,packet,session_id=session,execution_id=session,hermes_database=hermes,reviewer="actual fixture owner")
    original={str(path):path.read_bytes() for path in root.rglob("*") if path.is_file()}
    run_checks(database,kind="articles",backup_dir=tmp_path/"backups",fetcher=lambda *_:(_ for _ in ()).throw(RuntimeError("new actual terminal source check")))
    before=database.read_bytes()
    assert prepare_review(database,root)==packet
    assert database.read_bytes()==before and original=={str(path):path.read_bytes() for path in root.rglob("*") if path.is_file()}
    result=review_candidates(root,claim["token"],{item["candidate_sha256"]:{"candidate_sha256":item["candidate_sha256"],"status":"pass","reason":"Full older snapshot actually read; current source check changed"} for item in packet["candidates"]})
    assert result["reviewed"]==2 and _reader(database,tmp_path).articles()["items"]==[]
    with sqlite3.connect(database) as db:
        assert {row[0] for row in db.execute("SELECT status FROM registry_reviews")}=={"needs_correction"}
    latest=prepare_review(database,root)
    assert {item["candidate_sha256"] for item in latest["candidates"]}!={item["candidate_sha256"] for item in packet["candidates"]}


def test_historical_empty_t1_with_path_remains_audit_only_after_storage_upgrade(tmp_path,monkeypatch):
    from climate_registry.information_checks import run_checks
    database=_database(tmp_path)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB",str(database))
    frozen={"schema_version":"information-check.v1","kind":"articles","targets":[],"retry_of":None,"registry_database":str(database)}
    encoded=json.dumps(frozen,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_check_runs VALUES('old-empty',?,?, '2026-09-01T00:00:00Z','2026-09-01T00:00:00Z','complete',0,0,NULL)",(encoded,hashlib.sha256(encoded.encode()).hexdigest()))
    before=database.read_bytes()
    for option in ("resume_run_id","retry_run_id"):
        with pytest.raises(ValueError,match="source/version binding"):
            run_checks(database,kind="articles",backup_dir=tmp_path/"backups",**{option:"old-empty"},fetcher=lambda *_:pytest.fail("old audit reached fetch"))
        assert database.read_bytes()==before
