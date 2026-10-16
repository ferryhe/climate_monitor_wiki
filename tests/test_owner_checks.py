"""Real isolated owner captures, new candidate review, and public projections."""
import base64
import hashlib
import json
import sqlite3
from copy import deepcopy

import pytest

from climate_registry.acquisition_review import digest
from climate_registry.errors import RegistryInputError
from climate_registry.owner_checks import plan_owner_check, ingest_owner_check, approve_owner_check, owner_review_template
from climate_registry.publication import stage_entities, public_connection, export_public_snapshot, public_wiki_pages
from climate_registry.read_api import RegistryReader, RegistryNotFoundError
from test_verified_publication import _database, _reader, _pdf_pair, DOC


def _manifest(database, *, kind="article", identity="good", occurrence="core:good", fields=None, body=None):
    with sqlite3.connect(database) as db:
        stage_entities(db)
    plan = plan_owner_check(database, kind, identity)
    target = next(row for row in plan["plan"]["targets"] if row["occurrence_id"] == occurrence)
    fields = fields or {"title": "Climate risk findings", "summary": "Flood exposure increased in the study region."}
    body = body or "Climate risk findings\nFlood exposure increased in the study region.\nPRIVATE TOOL FOOTER"
    proof = {}
    for key, value in fields.items():
        excerpt = "10–12 June 2027" if key in {"start_date", "end_date", "date_precision"} else value
        proof[key] = {"value": value, "excerpt": excerpt, "record_context": fields.get("name") or fields["title"]}
        if key == "summary":
            proof[key]["section"] = "article_body"
            proof[key]["support"] = "complete"
    excluded = sorted(key for key, value in target["fields"].items() if value is not None and value != "" and key != "source_document_sha256" and fields.get(key) != value)
    plan.update(actor={"id": "source-checker", "task_id": "check-task"}, authorization_reference="owner-message-actual-scope",
        audit_reference="audit/capture-record", checked_at="2026-10-15T23:00:00Z", observations=[{
            "target": target, "access_status": "accessible", "capture": {"requested_url": target["source_url"],
                "final_url": target["source_url"], "title": fields.get("title") or fields["name"],
                "tool": "browser-rendered-text", "captured_at": "2026-10-15T22:59:00Z", "text": body,
                "bytes_base64": base64.b64encode(body.encode()).decode(), "sha256": hashlib.sha256(body.encode()).hexdigest()},
            "original_comparisons": {key: {"status": "conflict" if key == "summary" else "missing"} for key, value in target["fields"].items() if value is not None and value != "" and key != "source_document_sha256"},
            "public_fields": fields, "field_evidence": proof, "excluded_fields": excluded}])
    return plan


def _ingest(database, tmp_path, manifest):
    return ingest_owner_check(database, manifest, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)


def _review(database, tmp_path, sha):
    review = owner_review_template(database, sha)
    review.update(actor={"id": "independent-reviewer", "task_id": "separate-review-task"},
        authorization_reference="owner-message-actual-scope", audit_reference="audit/independent-report",
        reviewed_at="2026-10-15T23:01:00Z", decision="pass", reason="Inspected exact capture and each corrected public field")
    return review


def test_owner_correction_requires_distinct_new_candidate_review_and_projects_every_reader(tmp_path):
    database = _database(tmp_path)
    manifest = _manifest(database)
    with sqlite3.connect(database) as db:
        original = db.execute("SELECT * FROM article_versions").fetchall()
    reader = _reader(database, tmp_path)
    assert reader.articles()["items"] == []
    result = _ingest(database, tmp_path, manifest)
    assert result["status"] == "staged"
    assert reader.articles()["items"] == []
    assert _ingest(database, tmp_path, manifest)["status"] == "no-op"
    review = _review(database, tmp_path, result["candidate_sha256"])
    bad = deepcopy(review); bad["actor"] = manifest["actor"]
    with pytest.raises(RegistryInputError, match="independent"):
        approve_owner_check(database, bad, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    approved = approve_owner_check(database, review, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    assert approved["status"] == "approved"
    def check(current):
        article = current.article("good")
        assert article["title"] == "Climate risk findings"
        assert article["summary"] == "Flood exposure increased in the study region."
        assert article["summary_provenance"] == "owner_agent_verified_source"
        assert "PRIVATE TOOL" not in json.dumps(article)
        assert "Summary good" not in json.dumps(article)
        with pytest.raises(RegistryNotFoundError):
            current.article("unchecked")
    check(reader)
    with reader.public_snapshot():
        check(reader)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM article_versions").fetchall() == original
        assert db.execute("PRAGMA user_version").fetchone()[0] == 23
        assert db.execute("SELECT count(*) FROM registry_reviews WHERE status='pass'").fetchone()[0] == 1
        projected = public_connection(db)
        try:
            assert projected.execute("SELECT observed_summary FROM article_versions WHERE article_id='good'").fetchone()[0] == manifest["observations"][0]["public_fields"]["summary"]
        finally:
            projected.close()
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    check(RegistryReader.from_public_export(root))
    pages = public_wiki_pages(database)
    text = json.dumps(pages)
    assert "Flood exposure increased" in text and "PRIVATE TOOL" not in text and "Summary good" not in text
    from climate_registry.verification_report import write_verification_report
    report = write_verification_report(database, tmp_path/"reports")
    rows = json.loads(__import__('pathlib').Path(report["json_path"]).read_text())["rows"]
    published = next(row for row in rows if row["entity_id"] == "good" and row["version"] == "published")
    assert published["owner_agent_verification"]["original_verification_status"] == "conflict"
    assert published["owner_agent_verification"]["excluded_fields"] == ["summary", "title"]


@pytest.mark.parametrize("change", ["bytes", "page", "field", "actor", "authorization", "state", "source", "evidence"])
def test_owner_preflight_aborts_before_any_registry_write(tmp_path, change):
    database = _database(tmp_path)
    manifest = _manifest(database)
    entry = manifest["observations"][0]
    if change == "bytes": entry["capture"]["bytes_base64"] = base64.b64encode(b"other").decode()
    if change == "page": entry["target"]["source_binding"]["page"] = 999
    if change == "field": entry["public_fields"]["summary"] = "Invented unsupported claim"
    if change == "actor": manifest["actor"]["task_id"] = ""
    if change == "authorization": manifest["authorization_reference"] = ""
    if change == "state": manifest["plan"]["expected_state"]["is_visible"] = 0
    if change == "source": entry["capture"]["requested_url"] = "https://elsewhere.example/"
    if change == "evidence": entry["field_evidence"]["summary"]["record_context"] = "Different record"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(RegistryInputError):
        _ingest(database, tmp_path, manifest)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not list((tmp_path/"backups").glob("*"))


def test_owner_meeting_does_not_publish_original_extra_facts_and_keeps_report_metadata(tmp_path):
    database = _database(tmp_path)
    _pdf_pair(database, target_extra={"location": "PRIVATE unsupported venue", "relevance": "PRIVATE unsupported recommendation"})
    fields = {"name": "IAIS Annual Conference 2027", "raw_date": "10–12 June 2027", "start_date": "2027-06-10", "end_date": "2027-06-12", "date_precision": "day"}
    manifest = _manifest(database, kind="pdf_meeting", identity="target", occurrence="target-occ", fields=fields,
        body="IAIS Annual Conference 2027\n10–12 June 2027\nPRIVATE TOOL FOOTER")
    result = _ingest(database, tmp_path, manifest)
    review = _review(database, tmp_path, result["candidate_sha256"])
    approve_owner_check(database, review, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    reader = _reader(database, tmp_path)
    def check(current):
        meetings = current.meetings_all(base_date="2026-10-15")
        assert len(meetings) == 1
        assert meetings[0]["start_date"] == "2027-06-10" and meetings[0]["verification_status"] == "verified"
        assert not meetings[0].get("location") and not meetings[0].get("relevance_reason")
        assert "PRIVATE" not in json.dumps(meetings) and "Verified primary observation" not in json.dumps(meetings)
        report = current.pdf_report(DOC)
        assert report["calendar_count"] == 1 and report["calendar_items"][0]["page"] == 9
        assert not current.pdf_report('a'*64)["calendar_items"]
    check(reader)
    with reader.public_snapshot(): check(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    check(RegistryReader.from_public_export(root))


def test_owner_publication_date_paraphrase_and_range_roundtrip(tmp_path):
    from climate_registry.range_reports import _range_source
    database = _database(tmp_path)
    fields = {"title": "Climate risk findings", "summary": "The study reports growing flood exposure.", "publication_date": "2026-10-02"}
    body = "Climate risk findings\nFlood exposure increased in the study region.\n2 October 2026\nPRIVATE TOOL FOOTER"
    manifest = _manifest(database, fields=fields, body=body)
    manifest["observations"][0]["field_evidence"]["summary"]["excerpt"] = "Flood exposure increased in the study region."
    manifest["observations"][0]["field_evidence"]["publication_date"]["excerpt"] = "2 October 2026"
    result = _ingest(database, tmp_path, manifest)
    approve_owner_check(database, _review(database, tmp_path, result["candidate_sha256"]), queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    reader = _reader(database, tmp_path)
    assert reader.article("good")["information_date"] == "2026-10-02"
    live = _range_source(reader, "2026-10-02", "2026-10-02")
    assert [row["article_id"] for row in live["articles"]] == ["good"]
    assert "The study reports growing flood exposure." in json.dumps(live)
    assert "Summary good" not in json.dumps(live) and "PRIVATE TOOL" not in json.dumps(live)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    static = RegistryReader.from_public_export(root)
    assert _range_source(static, "2026-10-02", "2026-10-02")["articles"] == live["articles"]
    from climate_registry.owner_checks import _dates
    assert _dates("20-Aug-2026") == {"start_date": "2026-08-20", "end_date": None, "date_precision": "day"}


def test_owner_review_rejects_live_change_without_staging_and_preserves_prior_public(tmp_path):
    database = _database(tmp_path)
    manifest = _manifest(database)
    result = _ingest(database, tmp_path, manifest)
    review = _review(database, tmp_path, result["candidate_sha256"])
    approve_owner_check(database, review, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    assert approve_owner_check(database, review, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)["status"] == "no-op"
    old = _reader(database, tmp_path).article("good")["published_candidate_sha256"]
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_versions VALUES('new-version','good','New title','New title','New unverified fact','new-fingerprint','report-title-summary','2026-10-16','2026-10-16')")
        db.execute("UPDATE articles SET current_version_id='new-version' WHERE article_id='good'")
    with pytest.raises(RegistryInputError, match="live source"):
        approve_owner_check(database, review, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    with pytest.raises(RegistryInputError, match="changed live"):
        _ingest(database, tmp_path, manifest)
    assert _reader(database, tmp_path).article("good")["published_candidate_sha256"] == old


def test_owner_explicit_to_range_and_no_year_or_end_inference(tmp_path):
    database = _database(tmp_path); _pdf_pair(database)
    fields = {"name": "COP31", "raw_date": "9 to 20 November 2026", "start_date": "2026-11-09", "end_date": "2026-11-20", "date_precision": "day"}
    manifest = _manifest(database, kind="pdf_meeting", identity="target", occurrence="target-occ", fields=fields,
        body="COP31\n9 to 20 November 2026")
    for key in ("start_date", "end_date", "date_precision"):
        manifest["observations"][0]["field_evidence"][key]["excerpt"] = "9 to 20 November 2026"
    assert ingest_owner_check(database, manifest, queue_dir=tmp_path/"queue")["status"] == "dry_run"
    invalid = deepcopy(manifest)
    entry = invalid["observations"][0]
    entry["public_fields"]["end_date"] = "2026-11-21"
    entry["field_evidence"]["end_date"]["value"] = "2026-11-21"
    with pytest.raises(RegistryInputError, match="precision"):
        _ingest(database, tmp_path, invalid)
    invalid = deepcopy(manifest); entry = invalid["observations"][0]
    text = "COP31\n9 to 20 November"
    entry["capture"].update(text=text, bytes_base64=base64.b64encode(text.encode()).decode(), sha256=hashlib.sha256(text.encode()).hexdigest())
    for key in ("start_date", "end_date", "date_precision"):
        entry["field_evidence"][key]["excerpt"] = "9 to 20 November"
    entry["public_fields"].pop("raw_date"); entry["field_evidence"].pop("raw_date")
    with pytest.raises(RegistryInputError, match="precision"):
        _ingest(database, tmp_path, invalid)


def test_cli_owner_plan_and_review_templates_are_read_only(tmp_path, capsys):
    from climate_registry.cli import main
    database = _database(tmp_path)
    manifest = _manifest(database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    assert main(["plan-owner-check", "--database", str(database), "--kind", "article", "--id", "good"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["plan"] == manifest["plan"] and output["observations"] == []
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_batch_owner_preflight_is_atomic_and_uses_one_backup_per_phase(tmp_path):
    database = _database(tmp_path)
    manifests = [_manifest(database), _manifest(database, identity="unchecked", occurrence="core:unchecked")]
    invalid = deepcopy(manifests); invalid[1]["plan"]["expected_state"]["is_visible"] = 0
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(RegistryInputError, match="stale"):
        _ingest(database, tmp_path, invalid)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not list((tmp_path/"backups").glob("*"))
    result = _ingest(database, tmp_path, manifests)
    assert len(result["results"]) == 2 and len(list((tmp_path/"backups").glob("*"))) == 1
    assert __import__('pathlib').Path(result["backup_path"]).is_file()
    receipts = [_review(database, tmp_path, item["candidate_sha256"]) for item in result["results"]]
    bad = deepcopy(receipts); bad[1]["actor"] = manifests[1]["actor"]
    with pytest.raises(RegistryInputError, match="independent"):
        approve_owner_check(database, bad, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    assert _reader(database, tmp_path).articles()["items"] == []
    assert len(list((tmp_path/"backups").glob("*"))) == 1
    result = approve_owner_check(database, receipts, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    assert len(result["results"]) == 2 and len(list((tmp_path/"backups").glob("*"))) == 2
    assert len(_reader(database, tmp_path).articles()["items"]) == 2
    assert approve_owner_check(database, receipts, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)["backup_path"] is None


def test_owner_linked_core_and_three_reports_keep_unchecked_copies_metadata_only(tmp_path):
    from test_registry_publication import _pdf
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db, identity="pdf-good", url="https://example.org/good", verified=False)
        db.execute("INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,extracted_text_sha256,document_json,imported_at,date_of_run) VALUES(?, '/evidence/third.pdf','third.pdf','application/pdf',1,?,'{}','2026-09-28','2026-09-28')", ('a'*64, 'b'*64))
        for index, document in enumerate((DOC, 'a'*64), 1):
            raw = {"title": "PRIVATE unsupported alias title", "summary": "PRIVATE unsupported alias prose", "summary_basis": "verbatim_pdf_article_row", "raw_url": "https://example.org/good"}
            db.execute("INSERT INTO pdf_intake_article_occurrences VALUES(?,?,?,?,?,'2026-09-14',NULL,?,?,?)",
                ("copy-"+str(index), "pdf-good", document, index+3, "https://example.org/good", 'd'*64, 'c'*64, json.dumps(raw)))
        originals = db.execute("SELECT occurrence_json FROM pdf_intake_article_occurrences ORDER BY occurrence_id").fetchall()
    manifest = _manifest(database, kind="pdf_article", identity="pdf-good", occurrence="occurrence")
    assert manifest["plan"]["entity_kind"] == "article" and len(manifest["plan"]["targets"]) == 4
    result = _ingest(database, tmp_path, manifest)
    approve_owner_check(database, _review(database, tmp_path, result["candidate_sha256"]), queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    def check(current):
        core = current.article("good")
        assert core["summary_provenance"] == "owner_agent_verified_source"
        assert core["summary"] == "Flood exposure increased in the study region."
        assert core["title"] == "Climate risk findings"
        article = current.pdf_article("pdf-good")
        assert article["title"] == "Climate risk findings"
        assert len(article["occurrences"]) == 3
        observations = {row["occurrence_id"]: row for row in article["occurrences"]}
        assert observations["occurrence"]["verification_status"] == "verified"
        assert all(observations[key]["metadata_only"] and not observations[key].get("summary") for key in ("copy-1", "copy-2"))
        for document in ('f'*64, DOC, 'a'*64):
            report = current.pdf_report(document)
            assert report["article_count"] == 1
            assert report["articles"][0]["title"] == "Climate risk findings"
        assert len(current.pdf_articles_all(include_linked=True)) == 1
        assert "PRIVATE" not in json.dumps(core) + json.dumps(article)
    reader = _reader(database, tmp_path)
    check(reader)
    with reader.public_snapshot(): check(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    check(RegistryReader.from_public_export(root))
    assert "PRIVATE" not in (root/"wiki/public-registry.json").read_text()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT occurrence_json FROM pdf_intake_article_occurrences ORDER BY occurrence_id").fetchall() == originals


@pytest.mark.skipif(__import__('os').name == 'nt', reason='API imports existing POSIX fcntl management')
def test_owner_actual_api_chat_and_frozen_range_consume_only_reviewed_scope(tmp_path, monkeypatch):
    import api_server
    from fastapi.testclient import TestClient
    from climate_registry.range_reports import _range_source
    from agentic_wiki.wiki_agent import AgenticWikiResponder
    database = _database(tmp_path); _pdf_pair(database, target_extra={"kind": "publication", "location": "PRIVATE venue"})
    fields = {"name": "IAIS Annual Conference 2027", "raw_date": "10–12 June 2027", "start_date": "2027-06-10", "end_date": "2027-06-12", "date_precision": "day", "event_type": "conference"}
    manifest = _manifest(database, kind="pdf_meeting", identity="target", occurrence="target-occ", fields=fields,
        body="IAIS Annual Conference 2027\n10–12 June 2027\nThis conference brings together supervisors.\nPRIVATE TOOL FOOTER")
    manifest["observations"][0]["field_evidence"]["event_type"]["excerpt"] = "This conference brings together supervisors."
    result = _ingest(database, tmp_path, manifest)
    approve_owner_check(database, _review(database, tmp_path, result["candidate_sha256"]), queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    reader = _reader(database, tmp_path)
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    client = TestClient(api_server.app)
    response = client.get('/api/registry/pdf-intake/reports/'+DOC+'?page=1&page_size=5')
    assert response.status_code == 200
    item = response.json()["calendar_items"][0]
    assert item["name"] == fields["name"] and item["start_date"] == fields["start_date"]
    assert item.get("kind") != "publication" and "PRIVATE" not in response.text
    wiki = tmp_path/"chat-wiki"; wiki.mkdir()
    for name, content in public_wiki_pages(database).items():
        (wiki/name).write_text(content, encoding="utf-8")
    responder = AgenticWikiResponder(wiki, tmp_path/"sources"); responder.client = None
    responder.chat_evidence.reader_factory = lambda: _reader(database, tmp_path)
    chat = responder.chat_evidence.answer("What are the confirmed dates for the IAIS Annual Conference 2027? Cite sources.")
    assert not chat["needs_clarification"] and chat["sources"]
    assert "2027-06-10" in chat["text"] and "PRIVATE" not in json.dumps(chat)
    range_source = _range_source(reader, "2027-06-01", "2027-06-20")
    assert "PRIVATE" not in json.dumps(range_source)


def test_failed_owner_scope_cannot_replace_prior_approved_version(tmp_path):
    database = _database(tmp_path)
    first = _manifest(database)
    staged = _ingest(database, tmp_path, first)
    approve_owner_check(database, _review(database, tmp_path, staged["candidate_sha256"]), queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    prior = _reader(database, tmp_path).article("good")["published_candidate_sha256"]
    failed = _manifest(database)
    failed["checked_at"] = "2026-10-15T23:02:00Z"
    entry = failed["observations"][0]
    entry.update(public_fields={}, field_evidence={}, access_status="unavailable",
        excluded_fields=sorted(key for key, value in entry["target"]["fields"].items() if value is not None and value != "" and key != "source_document_sha256"))
    staged = _ingest(database, tmp_path, failed)
    review = _review(database, tmp_path, staged["candidate_sha256"])
    review["reviewed_at"] = "2026-10-15T23:03:00Z"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(RegistryInputError, match="no eligible"):
        approve_owner_check(database, review, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert _reader(database, tmp_path).article("good")["published_candidate_sha256"] == prior


def test_owner_scope_does_not_normalize_excluded_time_or_fill_from_another_copy():
    from climate_monitor.meeting_fields import pdf_meeting_fields, merge_meeting_observations
    item = {"event_id": "one", "occurrence_id": "one", "source_document_sha256": DOC, "name": "Conference",
        "raw_date": "10 June 2027 at 10:00 UTC", "owner_agent_verification": {"public_fields": {"name": "Conference"}},
        "verification_status": "verified", "pdf_observations": []}
    normalized = pdf_meeting_fields(item)
    assert normalized["raw_time_text"] is None and normalized["event_timezone"] is None
    unrelated = {**item, "owner_agent_verification": None, "location": "PRIVATE other location"}
    for records in ([normalized, unrelated], [unrelated, normalized]):
        merged = merge_meeting_observations(records)
        assert len(merged) == 1 and not merged[0].get("location")


@pytest.mark.skipif(__import__('os').name == 'nt', reason='Existing native meeting fixture imports POSIX API management')
def test_native_owner_meeting_keeps_supported_facts_and_truthful_provenance(tmp_path):
    from test_issue136_meetings import _database as native_database, _candidate
    from climate_monitor.meetings import process_batch
    from climate_registry.publication import migrate_publication
    body = "World Climate Summit 2027. June 10–12, 2027. Climate risk agenda."
    database = native_database(tmp_path, [body])
    migrate_publication(database, tmp_path/"backups", apply=True)
    candidate = _candidate(location=None, online_url=None, organizer=None, raw_time_text=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None)
    result = process_batch(database, "batch", prompt_text="fixture", prompt_version="v1", provider="fixture", model="fixture", extractor=lambda request: {"events": [candidate]})
    assert result["status"] == "succeeded", result
    with sqlite3.connect(database) as db:
        event, source = db.execute("SELECT event_id,event_source_id FROM climate_event_sources").fetchone()
    fields = {"name": "World Climate Summit 2027", "event_type": "conference", "status": "scheduled", "raw_date": "10–12 June 2027", "start_date": "2027-06-10", "end_date": "2027-06-12", "date_precision": "day"}
    manifest = _manifest(database, kind="meeting", identity=event, occurrence=source, fields=fields,
        body="World Climate Summit 2027\n10–12 June 2027\nThe conference is scheduled.\nPRIVATE TOOL FOOTER")
    for key in ("event_type", "status"):
        manifest["observations"][0]["field_evidence"][key]["excerpt"] = "The conference is scheduled."
    result = _ingest(database, tmp_path, manifest)
    approve_owner_check(database, _review(database, tmp_path, result["candidate_sha256"]), queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    reader = _reader(database, tmp_path)
    def check(current):
        meetings = current.meetings_all(base_date="2026-10-15")
        assert len(meetings) == 1 and meetings[0]["start_date"] == fields["start_date"]
        assert meetings[0]["owner_agent_verification"]["verification_basis"] == "owner-agent-information-check.v1"
        assert not meetings[0].get("relevance_reason") and "PRIVATE" not in json.dumps(meetings)
    check(reader)
    with reader.public_snapshot(): check(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    check(RegistryReader.from_public_export(root))


def _owner_article_with_private_metadata(tmp_path, source):
    from test_registry_publication import _pdf
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_enrichments(enrichment_id,article_id,article_version_id,status,summary,categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at) VALUES('e-good','good','v-good','complete','PRIVATE original enrichment summary','[\"PRIVATE category\"]','[\"PRIVATE keyword\"]','en','manual','PRIVATE generator','v1','2026-09-01T00:00:00Z')")
        if source != "native":
            _pdf(db, identity="pdf-good", url="https://example.org/"+("good" if source == "linked" else "pdf-only"), verified=False)
            db.execute("UPDATE pdf_intake_articles SET type_safe_classification_json=?", (json.dumps({"label": "article", "reason": "PRIVATE classifier reason"}),))
        originals = {table: db.execute("SELECT * FROM "+table).fetchall() for table in ("article_enrichments", "pdf_intake_articles", "pdf_intake_article_occurrences")}
    fields = {"title": "Climate risk findings", "summary": "Flood exposure increased in the study region.", "publication_date": "2026-10-02"}
    manifest = _manifest(database, kind="article" if source == "native" else "pdf_article",
        identity="good" if source == "native" else "pdf-good", occurrence="core:good" if source == "native" else "occurrence",
        fields=fields, body="Climate risk findings\nFlood exposure increased in the study region.\n2 October 2026")
    manifest["observations"][0]["field_evidence"]["publication_date"]["excerpt"] = "2 October 2026"
    staged = _ingest(database, tmp_path, manifest)
    approve_owner_check(database, _review(database, tmp_path, staged["candidate_sha256"]), queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    return database, originals


@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
def test_owner_projection_clears_unreviewed_metadata_and_preserves_dates_across_consumers(tmp_path, source):
    from climate_registry.range_reports import _range_source
    database, originals = _owner_article_with_private_metadata(tmp_path, source)
    def check(current):
        item = current.pdf_article("pdf-good") if source == "pdf" else current.article("good")
        assert item["summary"] == "Flood exposure increased in the study region."
        assert not item.get("categories") and not item.get("keywords") and not item.get("type_safe_classification")
        assert not (item.get("enrichment") or {}).get("generator")
        assert item["publication_date"] == "2026-10-02"
        if source != "pdf":
            assert item["information_date"] == "2026-10-02"
            proof = item["date_observations"][0]["evidence"]
            assert proof["text"] == "2 October 2026" and proof["table"] == "registry_reviews"
            assert proof["record_id"] == item["owner_agent_verification"]["review_id"]
        assert "PRIVATE" not in json.dumps(item)
        if source != "native":
            pdf = current.pdf_article("pdf-good")
            assert not pdf.get("type_safe_classification")
            assert pdf["occurrences"][0]["publication_date"] == "2026-10-02"
            assert "PRIVATE" not in json.dumps(current.pdf_report('f'*64))
    reader = _reader(database, tmp_path)
    check(reader)
    with reader.public_snapshot(): check(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    static = RegistryReader.from_public_export(root)
    check(static)
    assert "PRIVATE" not in (root/"wiki/public-registry.json").read_text()
    assert "PRIVATE" not in json.dumps(public_wiki_pages(database))
    live_range = _range_source(reader, "2026-10-02", "2026-10-02")
    assert "PRIVATE" not in json.dumps(live_range)
    if source != "pdf":
        assert [item["article_id"] for item in live_range["articles"]] == ["good"]
    assert _range_source(static, "2026-10-02", "2026-10-02")["articles"] == live_range["articles"]
    with sqlite3.connect(database) as db:
        for table, rows in originals.items():
            assert db.execute("SELECT * FROM "+table).fetchall() == rows


@pytest.mark.skipif(__import__('os').name == 'nt', reason='Actual API imports POSIX management')
@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
def test_owner_actual_api_hides_metadata_and_keeps_reviewed_linked_date(tmp_path, monkeypatch, source):
    import api_server
    from fastapi.testclient import TestClient
    database, _ = _owner_article_with_private_metadata(tmp_path, source)
    monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(database, tmp_path))
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    client = TestClient(api_server.app)
    path = "/api/registry/pdf-intake/articles/pdf-good" if source == "pdf" else "/api/registry/articles/good"
    response = client.get(path)
    assert response.status_code == 200 and "PRIVATE" not in response.text
    assert response.json()["publication_date"] == "2026-10-02"
    listed = client.get("/api/registry/articles?include_pdf=true")
    assert listed.status_code == 200 and "PRIVATE" not in listed.text
    if source != "native":
        report = client.get('/api/registry/pdf-intake/reports/'+'f'*64+'?page=1&page_size=5')
        assert report.status_code == 200 and "PRIVATE" not in report.text


@pytest.mark.parametrize("date_scope,quote,expected", [
    ({"start_date": "2027-06-10", "end_date": "2027-06-12", "date_precision": "day"}, "10–12 June 2027", "2027-06-10 to 2027-06-12 (precision: day)"),
    ({"start_date": "2027-06", "date_precision": "month"}, "June 2027", "2027-06 (precision: month)"),
    ({"deadline_date": "2027-05-01", "date_precision": "day"}, "1 May 2027", "Deadline: 2027-05-01"),
])
def test_owner_wiki_uses_supported_structured_dates_without_excluded_raw_text(tmp_path, monkeypatch, date_scope, quote, expected):
    from climate_registry.wiki import render_runtime_registry
    from climate_registry.range_reports import _range_source
    database = _database(tmp_path); _pdf_pair(database)
    fields = {"name": "IAIS Annual Conference 2027", **date_scope}
    manifest = _manifest(database, kind="pdf_meeting", identity="target", occurrence="target-occ", fields=fields,
        body="IAIS Annual Conference 2027\n"+quote)
    for key in date_scope:
        manifest["observations"][0]["field_evidence"][key]["excerpt"] = quote
    staged = _ingest(database, tmp_path, manifest)
    approve_owner_check(database, _review(database, tmp_path, staged["candidate_sha256"]), queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    reader = _reader(database, tmp_path)
    def check(current):
        item = current.pdf_calendar_items_all()[0]
        assert not item.get("raw_date")
        assert all(item.get(key) == value for key, value in date_scope.items())
        report = current.pdf_report(DOC)
        assert all(report["calendar_items"][0].get(key) == value for key, value in date_scope.items())
    check(reader)
    with reader.public_snapshot(): check(reader)
    pages = public_wiki_pages(database)
    assert expected in pages["registry-meetings.md"]
    assert "Date not provided" not in pages["registry-meetings.md"] and "Verified primary observation" not in json.dumps(pages)
    runtime = render_runtime_registry(tmp_path/"runtime", web_database=None, pdf_database=database,
        manifest={"web_items": [], "pdf_occurrence_ids": [], "pdf_calendar_occurrence_ids": ["target-occ"]})
    assert expected in runtime["registry-meetings.md"]
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    static = RegistryReader.from_public_export(root)
    check(static)
    assert expected in static.static_snapshot["wiki_pages"]["registry-meetings.md"]
    assert "Verified primary observation" not in json.dumps(_range_source(reader, "2027-05-01", "2027-05-14"))
    if __import__('os').name != 'nt':
        import api_server
        from fastapi.testclient import TestClient
        monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(database, tmp_path))
        monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
        response = TestClient(api_server.app).get('/api/registry/pdf-intake/reports/'+DOC+'?page=1&page_size=5')
        assert response.status_code == 200
        item = response.json()["calendar_items"][0]
        assert not item.get("raw_date") and all(item.get(key) == value for key, value in date_scope.items())
