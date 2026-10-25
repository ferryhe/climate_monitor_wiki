"""Real isolated owner captures, new candidate review, and public projections."""
import base64
import hashlib
import json
import sqlite3
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from climate_registry.acquisition_review import digest
from climate_registry.errors import RegistryInputError
from climate_monitor.meeting_fields import MEETING_FIELDS
from climate_registry.owner_checks import plan_owner_check, ingest_owner_check, approve_owner_check, owner_review_template
from climate_registry.publication import stage_entities, public_connection, export_public_snapshot, public_wiki_pages
from climate_registry.read_api import RegistryReader, RegistryNotFoundError
from test_verified_publication import _database, _reader, _pdf_pair, DOC


def _manifest(database, *, kind="article", identity="good", occurrence="core:good", fields=None, body=None, final_url=None):
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
        elif key == "publication_date":
            proof[key]["date_kind"] = "published"
    excluded = sorted(key for key, value in target["fields"].items() if value is not None and value != "" and key != "source_document_sha256" and fields.get(key) != value)
    plan.update(actor={"id": "source-checker", "task_id": "check-task"}, authorization_reference="owner-message-actual-scope",
        audit_reference="audit/capture-record", checked_at="2026-10-15T23:00:00Z", observations=[{
            "target": target, "access_status": "accessible", "capture": {"requested_url": target["source_url"],
                "final_url": final_url or target["source_url"], "title": fields.get("title") or fields["name"],
                "tool": "browser-rendered-text", "captured_at": "2026-10-15T22:59:00Z", "text": body,
                "bytes_base64": base64.b64encode(body.encode()).decode(), "sha256": hashlib.sha256(body.encode()).hexdigest()},
            "original_comparisons": {key: {"status": "conflict" if key == "summary" else "missing"} for key, value in target["fields"].items() if value is not None and value != "" and key != "source_document_sha256"},
            "public_fields": fields, "field_evidence": proof, "excluded_fields": excluded}])
    return plan


def _ingest(database, tmp_path, manifest):
    return ingest_owner_check(database, manifest, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)


def _review(database, tmp_path, sha):
    from climate_registry.item_lifecycle import record_items
    record_items(tmp_path / "runs", database, [sha])
    review = owner_review_template(database, sha)
    review.update(actor={"id": "independent-reviewer", "task_id": "separate-review-task"},
        authorization_reference="owner-message-actual-scope", audit_reference="audit/independent-report",
        reviewed_at="2026-10-15T23:01:00Z", decision="pass", reason="Inspected exact capture and each corrected public field")
    return review


def _editorial_manifest(database, *, kind="article", identity="good", occurrence="core:good", disposition="complete", fields=None, body=None):
    manifest = _manifest(database, kind=kind, identity=identity, occurrence=occurrence, fields=fields, body=body)
    planned = plan_owner_check(database, kind, identity, with_editorial_metadata=True)
    manifest["schema_version"] = planned["schema_version"]
    manifest["taxonomy"] = planned["taxonomy"]
    entry = manifest["observations"][0]
    if disposition == "hold":
        entry["editorial_metadata"] = None
        return manifest
    capture = entry["capture"]
    evidence = "Flood exposure increased in the study region."
    values = {
        "categories": ["Physical Risk"],
        "keywords": ["flood", "exposure", "study region"],
    }
    proofs = {key: [{"value": value, "excerpt": evidence, "record_context": "Climate risk findings",
        "reason": "The exact source sentence describes flood exposure."} for value in seq]
        for key, seq in values.items()}
    from climate_registry.acquisition_review import digest
    entry["editorial_metadata"] = {"basis": "source_capture_editorial_classification",
        "capture_sha256": capture["sha256"], "public_fields_sha256": digest(entry["public_fields"]),
        **values, "evidence": proofs}
    return manifest


def _seed_native_article_report_context(database, tmp_path, monkeypatch):
    """Provide real schema-23 report and annotation inputs for JSON round-trip checks."""
    from climate_registry.reports import parse_historical_report

    report_date = "2026-09-14"
    filename = f"climate-monitor-{report_date}.md"
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    report_path = source_dir / filename
    report_path.write_bytes((Path(__file__).parents[1] / "sources" / filename).read_bytes())
    parsed = parse_historical_report(report_path, allow_offcycle=True)
    monkeypatch.setenv("SOURCE_DIR", str(source_dir))

    metadata_dir = tmp_path / "article-metadata"
    metadata_dir.mkdir()
    metadata = {
        "schema_version": 1,
        "annotation_method": "subagent-original-content-v1",
        "source_scope": "linked-original-content-with-report-fallback",
        "generated_on": "2026-08-17",
        "articles": [{
            "canonical_url": "https://example.org/good",
            "source_url": "https://example.org/good",
            "title": "Climate risk findings",
            "source_basis": "original_content",
            "summary": "Insurers reviewed flood pricing.",
            "categories": ["Physical Risk", "Insurance Risk"],
            "keywords": ["flood", "pricing", "insurers"],
        }],
    }
    metadata_path = metadata_dir / "articles-001-001.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setenv("ARTICLE_METADATA_DIR", str(metadata_dir))

    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            "owner-report", report_date, filename, parsed.title, parsed.sha256, parsed.cadence,
            parsed.report_format, parsed.sites_checked, parsed.sites_succeeded, parsed.sites_failed,
            json.dumps(list(parsed.warnings)),
        ))
        db.execute("INSERT INTO discoveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            "owner-discovery", "owner-report", 1, "Pillar A", "A", "good", "v-good",
            "https://example.org/good", "Climate risk findings", "Flood exposure increased in the study region.", 1, None,
        ))
        db.execute("INSERT INTO report_appearances(report_id,article_id,version_id,discovery_id,section,pillar,ordinal,disposition) VALUES(?,?,?,?,?,?,?,?)", (
            "owner-report", "good", "v-good", "owner-discovery", "Pillar A", "A", 1, "new",
        ))
    return report_path, metadata_path


def _mutate_native_article_context(database, report_path, metadata_path, mutation):
    if mutation == "source":
        with sqlite3.connect(database) as db:
            db.execute("UPDATE article_versions SET observed_summary='Changed source summary' WHERE version_id='v-good'")
    elif mutation == "report_fallback":
        report_path.write_text(report_path.read_text(encoding="utf-8") + "\nChanged report source evidence.\n", encoding="utf-8")
    else:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["articles"][0]["summary"] = "Insurers reviewed changed flood pricing."
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")


def test_owner_correction_requires_distinct_new_candidate_review_and_projects_every_reader(tmp_path):
    database = _database(tmp_path)
    reviewed_date = {"date_kind": "updated", "date_precision": "day", "review_status": "pass",
        "source_url": "https://example.org/good", "target_url": "https://example.org/good",
        "field": "Last updated", "text": "Last updated 15 October 2026"}
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_date_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            "reviewed-update", "good", "https://example.org/good", "page_information", "2026-10-15",
            "official_page", "source_page", "official_dates", "reviewed-update", json.dumps(reviewed_date), "2026-10-16"))
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
        approve_owner_check(database, bad, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    approved = approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    assert approved["status"] == "approved"
    def check(current):
        article = current.article("good")
        assert article["title"] == "Climate risk findings"
        assert article["summary"] == "Flood exposure increased in the study region."
        assert article["summary_provenance"] == "owner_agent_verified_source"
        assert article["updated_date"] == "2026-10-15"
        assert [item["observed_at"] for item in article["date_observations"]] == ["2026-10-15"]
        assert "PRIVATE TOOL" not in json.dumps(article)
        assert "Summary good" not in json.dumps(article)
        with pytest.raises(RegistryNotFoundError):
            current.article("unchecked")
    check(reader)
    with reader.public_snapshot():
        check(reader)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM article_versions").fetchall() == original
        assert db.execute("PRAGMA user_version").fetchone()[0] == 26
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


def test_native_article_report_json_roundtrip_survives_ingest_and_independent_review(tmp_path, monkeypatch):
    from climate_registry.publication import snapshot_entity

    database = _database(tmp_path)
    _seed_native_article_report_context(database, tmp_path, monkeypatch)
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 26
        current = snapshot_entity(db, "article", "good")
    assert current["report_fallbacks"]["2026-09-14"]["articles"]
    assert current["fallback_annotations"]["https://example.org/good"]["categories"] == ("Physical Risk", "Insurance Risk")

    manifest = _manifest(database)
    staged = _ingest(database, tmp_path, manifest)
    assert staged["status"] == "staged"
    with sqlite3.connect(database) as db:
        candidate_row = db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",
            (staged["candidate_sha256"],)).fetchone()
    persisted = json.loads(candidate_row[0])
    assert persisted["fallback_annotations"]["https://example.org/good"]["categories"] == ["Physical Risk", "Insurance Risk"]
    assert _ingest(database, tmp_path, manifest)["status"] == "no-op"

    # The reviewer identity and receipt content are test fixtures, bound to the actual candidate template.
    review = _review(database, tmp_path, staged["candidate_sha256"])
    assert approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue")["status"] == "dry_run"
    assert approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)["status"] == "approved"
    assert approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)["status"] == "no-op"


@pytest.mark.parametrize("mutation", ["source", "report_fallback", "annotation"])
@pytest.mark.parametrize("operation", ["repeat_ingest", "approval"])
def test_native_article_json_guards_reject_real_live_drift_before_writes(tmp_path, monkeypatch, mutation, operation):
    database = _database(tmp_path)
    report_path, metadata_path = _seed_native_article_report_context(database, tmp_path, monkeypatch)
    manifest = _manifest(database)
    staged = _ingest(database, tmp_path, manifest)
    review = _review(database, tmp_path, staged["candidate_sha256"])
    with sqlite3.connect(database) as db:
        latest_before = db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0]
    _mutate_native_article_context(database, report_path, metadata_path, mutation)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    backups_before = set((tmp_path/"backups").glob("*"))

    with pytest.raises(RegistryInputError, match="changed live|live source changed"):
        if operation == "repeat_ingest":
            ingest_owner_check(database, manifest, queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
        else:
            approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)

    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert set((tmp_path/"backups").glob("*")) == backups_before
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()[0] == latest_before


def test_v2_editorial_metadata_requires_new_review_and_projects_from_exact_capture(tmp_path, monkeypatch):
    from climate_registry.owner_checks import EDITORIAL_CHECK_VERSION, EDITORIAL_REVIEW_VERSION, validate_manifest

    database = _database(tmp_path)
    _seed_native_article_report_context(database, tmp_path, monkeypatch)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at,core_article_id) VALUES('linked','https://example.org/good','PRIVATE PDF title','2026-09-14','good')")
        raw = {"title": "PRIVATE PDF title", "summary": "PRIVATE PDF summary", "report_date": "2026-09-14",
            "source_document_sha256": DOC, "page": 7, "raw_url": "https://example.org/good", "summary_basis": "verbatim_pdf_article_row"}
        db.execute("INSERT INTO pdf_intake_article_occurrences VALUES('linked-occ','linked',?,7,'https://example.org/good','2026-09-14',NULL,?,?,?)",
            (DOC, "d"*64, "c"*64, json.dumps(raw)))
    manifest = _editorial_manifest(database, fields={"title": "Climate risk findings",
        "summary": "Flood exposure increased in the study region.", "publication_date": "2026-09-14"},
        body="Climate risk findings\nFlood exposure increased in the study region.\n2026-09-14\nPRIVATE TOOL FOOTER")
    assert manifest["schema_version"] == EDITORIAL_CHECK_VERSION
    validate_manifest(manifest)
    staged = _ingest(database, tmp_path, manifest)
    assert staged["status"] == "staged"
    review = _review(database, tmp_path, staged["candidate_sha256"])
    assert review["schema_version"] == EDITORIAL_REVIEW_VERSION
    assert review["review_scope"] == "source_fields_and_article_editorial_metadata"
    review.update(actor={"id": "metadata-reviewer", "task_id": "metadata-review-task"},
        authorization_reference="owner-message-actual-scope", audit_reference="audit/editorial-review",
        reviewed_at="2026-10-15T23:01:00Z", decision="pass", reason="Reviewed each exact source and metadata value")
    assert approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue")["status"] == "dry_run"
    assert approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)["status"] == "approved"
    article = _reader(database, tmp_path).article("good")
    assert article["categories"] == ["Physical Risk"]
    assert article["keywords"] == ["flood", "exposure", "study region"]
    assert article["metadata_provenance"] == {"categories": "owner_reviewed_editorial_classification",
        "keywords": "owner_reviewed_editorial_classification"}
    assert article["editorial_metadata_provenance"]["review_id"] == digest([staged["candidate_sha256"], "pass", review])
    assert "PRIVATE TOOL FOOTER" not in json.dumps(article)
    assert "evidence" not in article["owner_agent_verification"]["editorial_metadata"]
    pdf_article = _reader(database, tmp_path).pdf_report(DOC)["articles"][0]
    assert pdf_article["categories"] == ["Physical Risk"] and pdf_article["keywords"] == ["flood", "exposure", "study region"]
    assert "PRIVATE PDF" not in json.dumps(pdf_article)
    from climate_registry.list_projection import RegistryListProjection
    list_reader = _reader(database, tmp_path)
    with list_reader.public_snapshot(direct=True):
        listing = RegistryListProjection(list_reader)
        assert b"Physical Risk" in listing.category_body and b"owner_reviewed_editorial_classification" in listing.category_body
    assert "Physical Risk" in json.dumps(public_wiki_pages(database))
    from climate_registry.range_reports import _range_source
    ranged = _range_source(_reader(database, tmp_path), "2026-09-14", "2026-09-14")
    ranged_article = next(item for item in ranged["articles"] if item.get("article_id") == "good")
    assert ranged_article["categories"] == ["Physical Risk"] and ranged_article["keywords"] == ["flood", "exposure", "study region"]
    root = tmp_path/"export-root"
    (root/"wiki").mkdir(parents=True)
    export = root/"wiki/public-registry.json"
    export_public_snapshot(database, export)
    exported = RegistryReader.from_public_export(root).article("good")
    assert exported["categories"] == article["categories"] and exported["keywords"] == article["keywords"]


def test_v2_null_is_explicit_source_only_and_bad_metadata_bindings_are_rejected(tmp_path, monkeypatch):
    from climate_registry.owner_checks import validate_manifest

    database = _database(tmp_path)
    _seed_native_article_report_context(database, tmp_path, monkeypatch)
    hold = _editorial_manifest(database, disposition="hold")
    validate_manifest(hold)
    staged = _ingest(database, tmp_path, hold)
    review = _review(database, tmp_path, staged["candidate_sha256"])
    review["schema_version"] = "owner-agent-information-review.v1"
    review.pop("review_scope")
    with pytest.raises(RegistryInputError, match="schema does not match"):
        approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue")
    v2_review = owner_review_template(database, staged["candidate_sha256"])
    v2_review.update(actor={"id": "metadata-hold-reviewer", "task_id": "metadata-hold-review-task"},
        authorization_reference="owner-message-actual-scope", audit_reference="audit/editorial-hold",
        reviewed_at="2026-10-15T23:01:00Z", decision="pass", reason="Confirmed source-only metadata hold")
    assert approve_owner_check(database, v2_review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)["status"] == "approved"
    article = _reader(database, tmp_path).article("good")
    assert article["categories"] == [] and article["keywords"] == []
    assert article["owner_agent_verification"]["editorial_metadata"] is None

    bad = _editorial_manifest(database)
    bad["observations"][0]["editorial_metadata"]["capture_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="source binding differs"):
        validate_manifest(bad)

    bad = _editorial_manifest(database)
    bad["observations"][0]["editorial_metadata"]["evidence"]["keywords"][0]["excerpt"] = "unseen evidence phrase"
    with pytest.raises(ValueError, match="literal"):
        validate_manifest(bad)


def test_v2_pdf_article_primary_projects_reviewed_metadata_to_pdf_report(tmp_path):
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES('pdf-good','https://example.org/pdf-good','PRIVATE PDF title','2026-09-14')")
        raw = {"title": "PRIVATE PDF title", "summary": "PRIVATE PDF summary", "report_date": "2026-09-14",
            "source_document_sha256": DOC, "page": 8, "raw_url": "https://example.org/pdf-good", "summary_basis": "verbatim_pdf_article_row"}
        db.execute("INSERT INTO pdf_intake_article_occurrences VALUES('pdf-occ','pdf-good',?,8,'https://example.org/pdf-good','2026-09-14',NULL,?,?,?)",
            (DOC, "e"*64, "f"*64, json.dumps(raw)))
    manifest = _editorial_manifest(database, kind="pdf_article", identity="pdf-good", occurrence="pdf-occ")
    staged = _ingest(database, tmp_path, manifest)
    review = _review(database, tmp_path, staged["candidate_sha256"])
    review.update(actor={"id": "pdf-metadata-reviewer", "task_id": "pdf-metadata-review-task"},
        authorization_reference="owner-message-actual-scope", audit_reference="audit/pdf-editorial-review",
        reviewed_at="2026-10-15T23:01:00Z", decision="pass", reason="Reviewed the exact PDF article source scope")
    assert approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)["status"] == "approved"
    item = _reader(database, tmp_path).pdf_report(DOC)["articles"][0]
    assert item["categories"] == ["Physical Risk"] and item["keywords"] == ["flood", "exposure", "study region"]
    assert "PRIVATE PDF" not in json.dumps(item)


def test_plan_owner_check_cli_can_request_v2_article_scope(tmp_path, capsys):
    from climate_registry.cli import main

    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        stage_entities(db)
    assert main(["plan-owner-check", "--database", str(database), "--kind", "article", "--id", "good",
        "--with-editorial-metadata"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["schema_version"] == "owner-agent-information-check.v2"
    assert plan["taxonomy"]["taxonomy_id"] == "climate-actuarial-v2"


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
    approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
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


def test_owner_reviewed_final_url_is_public_meeting_link_and_keeps_requested_url(tmp_path):
    from climate_registry.publication import public_wiki_pages
    database = _database(tmp_path)
    _pdf_pair(database)
    fields = {"name": "IAIS Annual Conference 2027", "raw_date": "10–12 June 2027", "start_date": "2027-06-10", "end_date": "2027-06-12", "date_precision": "day"}
    final_url = "https://example.org/events/iais-2027"
    manifest = _manifest(database, kind="pdf_meeting", identity="target", occurrence="target-occ", fields=fields,
        body="IAIS Annual Conference 2027\n10–12 June 2027", final_url=final_url)
    result = _ingest(database, tmp_path, manifest)
    from climate_registry.publication import snapshot_entity
    from climate_registry.verification import evaluate_snapshot, public_snapshot_view
    with sqlite3.connect(database) as db:
        pending = snapshot_entity(db, "pdf_meeting", "target")
    pending_view = public_snapshot_view(pending, evaluate_snapshot(pending))
    pending_item = json.loads(next(row["item_json"] for row in pending_view["tables"]["pdf_intake_calendar_items"]
        if row["occurrence_id"] == "target-occ"))
    assert not pending_item.get("checked_source_url") and pending_item["source_urls"] == ["https://example.org/target"]
    approve_owner_check(database, _review(database, tmp_path, result["candidate_sha256"]),
        lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    reader = _reader(database, tmp_path)
    meeting = reader.meetings_all(base_date="2026-10-15")[0]
    assert meeting["source_urls"] == [final_url]
    assert meeting["checked_source_url"] == final_url
    assert meeting["owner_agent_verification"]["source_url"] == "https://example.org/target"
    assert meeting["owner_agent_verification"]["final_url"] == final_url
    markdown = public_wiki_pages(database)["registry-meetings.md"]
    assert f"Source: [{final_url}]({final_url})" in markdown
    assert f"[checked source]({final_url})" in markdown
    assert "Original PDF/source link: https://example.org/target" in markdown
    assert "https://example.org/target" in markdown


def _owner_cop31_with_unchecked_report_reference(tmp_path, *, fields=None, final_url=None, body=None):
    database = _database(tmp_path)
    event = "event-bbd46b0b29e416ce0a7aaee3"
    primary = "pdf-event-occurrence-d70e47832e567b739824cbb4"
    association = "pdf-event-occurrence-78d86d45491aad0088458623"
    with sqlite3.connect(database) as db:
        for document, report_date in (("a"*64, "2026-08-24"), ("d"*64, "2026-09-28")):
            db.execute("INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,extracted_text_sha256,document_json,imported_at,date_of_run) VALUES(?,?,?,'application/pdf',1,?,'{}',?,?)",
                (document, "/evidence/"+report_date+".pdf", report_date+".pdf", "b"*64, report_date, report_date))
        for occurrence, document, page in ((association, "a"*64, 8), (primary, "d"*64, 11)):
            item = {"event_id": event, "occurrence_id": occurrence, "name": "COP31",
                "source_document_sha256": document, "page": page, "source_urls": ["https://unfccc.int/cop31"],
                "raw_date": "PRIVATE old PDF date", "date_precision": "unknown", "summary": "PRIVATE unchecked PDF claim",
                "location": "PRIVATE unsupported venue", "kind": "event"}
            db.execute("INSERT INTO pdf_intake_calendar_items VALUES(?,?,?,?,?,'event',?,'unknown',NULL,NULL,?,?,NULL,?)",
                (occurrence, event, document, page, item["name"], item["raw_date"], item["summary"], "c"*64, json.dumps(item)))
        raw = db.execute("SELECT * FROM pdf_intake_calendar_items ORDER BY occurrence_id").fetchall()
    fields = fields or {"name": "COP31", "raw_date": "9 to 20 November 2026", "start_date": "2026-11-09",
        "end_date": "2026-11-20", "date_precision": "day", "location": "Antalya, Türkiye"}
    manifest = _manifest(database, kind="pdf_meeting", identity=event, occurrence=primary, fields=fields,
        body=body or "COP31\n9 to 20 November 2026\nAntalya, Türkiye", final_url=final_url)
    for key in ("start_date", "end_date", "date_precision"):
        if key in fields:
            manifest["observations"][0]["field_evidence"][key]["excerpt"] = fields["raw_date"]
    if "deadline_date" in fields:
        manifest["observations"][0]["field_evidence"]["deadline_date"]["excerpt"] = "31 October 2026"
    staged = _ingest(database, tmp_path, manifest)
    approve_owner_check(database, _review(database, tmp_path, staged["candidate_sha256"]),
        lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    return database, event, primary, association, fields, raw


def test_owner_meeting_report_reference_is_not_an_extra_global_event(tmp_path):
    from climate_registry.range_reports import _pdf_calendar_payload
    database, event, primary, association, fields, raw = _owner_cop31_with_unchecked_report_reference(tmp_path)
    reader = _reader(database, tmp_path)
    def check(current):
        rows = current.meetings_all(base_date="2026-01-01", include_unknown=True)
        assert len(rows) == 1 and rows[0]["event_id"] == event and rows[0]["occurrence_id"] == primary
        assert {key: rows[0][key] for key in fields} == fields
        assert rows[0]["verification_status"] == "verified" and not rows[0].get("metadata_only")
        paged = current.meetings(base_date="2026-01-01", include_unknown=True)
        assert paged["pagination"]["total"] == 1 and paged["verification_counts"]["unchecked"] == 0
        assert current.meetings(base_date="2026-01-01", verification_status="unchecked")["items"] == []
        assert current.meetings_all(base_date="2026-12-01", include_unknown=True) == []
        assert current.meetings(base_date="2026-12-01", include_unknown=True)["items"] == []
        observations = {row["occurrence_id"]: row for row in current.pdf_calendar_items_all()}
        assert set(observations) == {primary, association}
        reference = current.pdf_report("a"*64)["calendar_items"][0]
        assert reference["occurrence_id"] == association and reference["page"] == 8
        assert reference["source_document_sha256"] == "a"*64 and reference["metadata_only"]
        assert reference["verification_status"] == "unchecked" and not reference.get("name") and not reference.get("start_date")
        assert reference["verified_primary_reference"]["entity_id"] == event
        assert reference["verified_primary_display"]["name"] == "COP31"
        assert current.pdf_report("d"*64)["calendar_items"][0]["occurrence_id"] == primary
        calendar = _pdf_calendar_payload(current, base_date="2026-01-01")
        assert len(calendar["records"]) == 1 and calendar["records"][0]["occurrence_id"] == primary
        assert "PRIVATE" not in json.dumps([rows, observations, reference, calendar])
    check(reader)
    with reader.public_snapshot(): check(reader)
    with reader.public_snapshot(direct=True): check(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    static = RegistryReader.from_public_export(root)
    check(static)
    # A previously exported reference must also stay out of global static lists.
    static.static_snapshot["meeting_records"].append(static.pdf_report("a"*64)["calendar_items"][0])
    check(static)
    pages = public_wiki_pages(database)
    assert pages["registry-meetings.md"].count("## COP31\n") == 1
    assert "Location: Antalya, Türkiye" in pages["registry-meetings.md"]
    assert "Source: [https://unfccc.int/cop31](https://unfccc.int/cop31)" in pages["registry-meetings.md"]
    assert "PRIVATE" not in json.dumps(pages)
    kb = _rendered_owner_knowledge_base(tmp_path, pages)
    chunk = next(chunk for chunk in kb.chunks if chunk.heading == "COP31")
    assert "Location: Antalya, Türkiye" in chunk.text and "https://unfccc.int/cop31" in chunk.urls
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM pdf_intake_calendar_items ORDER BY occurrence_id").fetchall() == raw


@pytest.mark.skipif(__import__('os').name == 'nt', reason='API collection requires existing POSIX fcntl')
def test_owner_meeting_association_api_keeps_report_reference_without_global_duplicate(tmp_path, monkeypatch):
    import api_server
    from fastapi.testclient import TestClient
    fields = {"name": "COP31", "event_type": "conference", "status": "scheduled",
        "raw_date": "9 to 20 November 2026", "start_date": "2026-11-09", "end_date": "2026-11-20",
        "date_precision": "day", "raw_time_text": "09:00–09:30", "event_timezone": "UTC+1",
        "deadline_type": "registration", "deadline_date": "2026-10-31", "location": "Antalya, Türkiye"}
    body = ("COP31\nconference scheduled 9 to 20 November 2026\n09:00–09:30 UTC+1\n"
            "registration closes 31 October 2026\nAntalya, Türkiye")
    final_url = "https://g20.org/events"
    database, event, primary, association, fields, _ = _owner_cop31_with_unchecked_report_reference(
        tmp_path, fields=fields, final_url=final_url, body=body)
    monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(database, tmp_path))
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    client = TestClient(api_server.app)
    calendar = client.get("/api/registry/meetings?base_date=2026-01-01&include_unknown=true").json()
    assert calendar["pagination"]["total"] == 1
    assert calendar["items"][0]["event_id"] == event
    assert {key: calendar["items"][0][key] for key in fields} == fields
    assert calendar["items"][0]["checked_source_url"] == final_url
    assert calendar["items"][0]["original_source_url"] == "https://unfccc.int/cop31"
    unpaginated = client.get("/api/registry/pdf-intake/reports/"+"d"*64).json()
    paged_approved = client.get("/api/registry/pdf-intake/reports/"+"d"*64+"?page=1&page_size=5").json()
    assert paged_approved["pagination"]["page"] == 1
    assert paged_approved["pagination"]["total"] == len(unpaginated["calendar_items"])
    approved = paged_approved["calendar_items"][0]
    complete = unpaginated["calendar_items"][0]
    projected_fields = set(MEETING_FIELDS)
    projected_fields.update({"checked_source_url", "original_source_url"})
    assert {key: approved.get(key) for key in projected_fields} == {key: complete.get(key) for key in projected_fields}
    assert {key: approved[key] for key in fields} == fields
    assert approved["checked_source_url"] == final_url
    assert approved["original_source_url"] == "https://unfccc.int/cop31"
    assert approved["owner_agent_verification"] == complete["owner_agent_verification"]
    report = client.get("/api/registry/pdf-intake/reports/"+"a"*64+"?page=1&page_size=5").json()
    reference = report["calendar_items"][0]
    assert reference["occurrence_id"] == association and reference["page"] == 8 and reference["metadata_only"]
    assert reference["verification_status"] == "unchecked" and reference["verified_primary_display"]["name"] == "COP31"
    assert not reference.get("name") and not reference.get("start_date")
    assert not reference.get("event_type") and not reference.get("raw_time_text")
    assert not reference.get("checked_source_url") and not reference.get("original_source_url")
    assert "PRIVATE" not in json.dumps([calendar, report])


def test_owner_publication_date_paraphrase_and_range_roundtrip(tmp_path):
    from climate_registry.range_reports import _range_source
    database = _database(tmp_path)
    fields = {"title": "Climate risk findings", "summary": "The study reports growing flood exposure.", "publication_date": "2026-10-02"}
    body = "Climate risk findings\nFlood exposure increased in the study region.\n2 October 2026\nPRIVATE TOOL FOOTER"
    manifest = _manifest(database, fields=fields, body=body)
    manifest["observations"][0]["field_evidence"]["summary"]["excerpt"] = "Flood exposure increased in the study region."
    manifest["observations"][0]["field_evidence"]["publication_date"]["excerpt"] = "2 October 2026"
    result = _ingest(database, tmp_path, manifest)
    approve_owner_check(database, _review(database, tmp_path, result["candidate_sha256"]), lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
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
    approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    assert approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)["status"] == "no-op"
    old = _reader(database, tmp_path).article("good")["published_candidate_sha256"]
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_versions VALUES('new-version','good','New title','New title','New unverified fact','new-fingerprint','report-title-summary','2026-10-16','2026-10-16')")
        db.execute("UPDATE articles SET current_version_id='new-version' WHERE article_id='good'")
    with pytest.raises(RegistryInputError, match="live source"):
        approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    with pytest.raises(RegistryInputError, match="changed live"):
        _ingest(database, tmp_path, manifest)
    assert _reader(database, tmp_path).article("good")["published_candidate_sha256"] == old


@pytest.mark.parametrize("excerpt,start,end", [
    ("Oct 01\n\n- Oct 02, 2026", "2026-10-01", "2026-10-02"),
    ("Sep 30\n\n- Oct 01, 2026", "2026-09-30", "2026-10-01"),
    ("Oct 30\n\n- Oct 31, 2026", "2026-10-30", "2026-10-31"),
    ("Dec 14\n\n- Dec 15, 2026", "2026-12-14", "2026-12-15"),
    ("Sep\t30\r\n\n—\tOct\u00a001,\n2026", "2026-09-30", "2026-10-01"),
])
def test_owner_repeated_month_evidence_preserves_explicit_interval_and_literal_capture(tmp_path, excerpt, start, end):
    from climate_registry.owner_checks import _dates
    expected = {"start_date": start, "end_date": end, "date_precision": "day"}
    assert _dates(excerpt) == expected
    database = _database(tmp_path); _pdf_pair(database)
    fields = {"name": "Source conference", **expected}
    manifest = _manifest(database, kind="pdf_meeting", identity="target", occurrence="target-occ", fields=fields,
        body="Source conference\n" + excerpt)
    for key in expected:
        manifest["observations"][0]["field_evidence"][key]["excerpt"] = excerpt
    original = deepcopy(manifest)
    assert ingest_owner_check(database, manifest, queue_dir=tmp_path/"queue")["status"] == "dry_run"
    assert manifest == original
    normalized = deepcopy(manifest)
    normalized["observations"][0]["field_evidence"]["start_date"]["excerpt"] = " ".join(excerpt.split())
    with pytest.raises(RegistryInputError, match="literal record-specific capture"):
        ingest_owner_check(database, normalized, queue_dir=tmp_path/"queue")
    shortened = deepcopy(manifest)
    shortened["observations"][0]["public_fields"].pop("end_date")
    shortened["observations"][0]["field_evidence"].pop("end_date")
    with pytest.raises(RegistryInputError, match="retain the explicit source bounds"):
        ingest_owner_check(database, shortened, queue_dir=tmp_path/"queue")


@pytest.mark.parametrize("excerpt,start,end", [
    ("Oct 01\n\n- Oct 02", "2026-10-01", "2026-10-02"),
    ("Oct 01 - Oct 02, 26", "2026-10-01", "2026-10-02"),
    ("Feb 30 - Mar 01, 2026", "2026-02-28", "2026-03-01"),
    ("Feb 28 - Feb 30, 2026", "2026-02-28", "2026-03-01"),
    ("Oct 02 - Oct 01, 2026", "2026-10-02", "2026-10-01"),
    ("Oct 01 - Sep 30, 2026", "2026-10-01", "2026-09-30"),
    ("Dec 31 - Jan 01, 2026", "2026-12-31", "2027-01-01"),
])
def test_owner_repeated_month_evidence_refuses_missing_year_invalid_and_reversed_intervals(tmp_path, excerpt, start, end):
    database = _database(tmp_path); _pdf_pair(database)
    fields = {"name": "Source conference", "start_date": start, "end_date": end, "date_precision": "day"}
    manifest = _manifest(database, kind="pdf_meeting", identity="target", occurrence="target-occ", fields=fields,
        body="Source conference\n" + excerpt)
    for key in ("start_date", "end_date", "date_precision"):
        manifest["observations"][0]["field_evidence"][key]["excerpt"] = excerpt
    original = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(RegistryInputError, match="date|precision|interval"):
        _ingest(database, tmp_path, manifest)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == original
    assert not list((tmp_path/"backups").glob("*"))


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
        approve_owner_check(database, bad, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    assert _reader(database, tmp_path).articles()["items"] == []
    assert len(list((tmp_path/"backups").glob("*"))) == 1
    result = approve_owner_check(database, receipts, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    assert len(result["results"]) == 2 and len(list((tmp_path/"backups").glob("*"))) == 2
    assert len(_reader(database, tmp_path).articles()["items"]) == 2
    assert approve_owner_check(database, receipts, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)["backup_path"] is None


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
    approve_owner_check(database, _review(database, tmp_path, result["candidate_sha256"]), lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
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


def test_owner_reviewed_final_url_drives_article_citation_but_raw_url_stays_provenance(tmp_path):
    from climate_registry.wiki import _registry_source_observation_pages
    from test_registry_publication import _pdf
    database = _database(tmp_path)
    requested = "https://example.org/pdf-only"
    final_url = "https://example.org/articles/current"
    with sqlite3.connect(database) as db:
        _pdf(db, identity="pdf-only", url=requested, verified=False)
    manifest = _manifest(database, kind="pdf_article", identity="pdf-only", occurrence="occurrence",
        fields={"title": "Climate risk findings", "summary": "Flood exposure increased."},
        body="Climate risk findings\nFlood exposure increased.", final_url=final_url)
    result = _ingest(database, tmp_path, manifest)
    approve_owner_check(database, _review(database, tmp_path, result["candidate_sha256"]),
        lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    occurrence = _reader(database, tmp_path).pdf_article("pdf-only")["occurrences"][0]
    assert occurrence["raw_url"] == requested and occurrence["checked_source_url"] == final_url
    markdown = next(iter(_registry_source_observation_pages([_reader(database, tmp_path).pdf_article("pdf-only")]).values()))
    assert f"[checked source]({final_url})" in markdown and f"original PDF/source link (provenance): [{requested}]({requested})" in markdown


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
    approve_owner_check(database, _review(database, tmp_path, result["candidate_sha256"]), lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
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
    approve_owner_check(database, _review(database, tmp_path, staged["candidate_sha256"]), lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
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
        approve_owner_check(database, review, lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
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
    final_url = "https://example.com/events/reviewed-final-page"
    manifest = _manifest(database, kind="meeting", identity=event, occurrence=source, fields=fields,
        body="World Climate Summit 2027\n10–12 June 2027\nThe conference is scheduled.\nPRIVATE TOOL FOOTER",
        final_url=final_url)
    for key in ("event_type", "status"):
        manifest["observations"][0]["field_evidence"][key]["excerpt"] = "The conference is scheduled."
    result = _ingest(database, tmp_path, manifest)
    approve_owner_check(database, _review(database, tmp_path, result["candidate_sha256"]), lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    reader = _reader(database, tmp_path)
    requested_url = manifest["observations"][0]["target"]["source_url"]
    def check(current):
        meetings = current.meetings_all(base_date="2026-10-15")
        assert len(meetings) == 1 and meetings[0]["start_date"] == fields["start_date"]
        assert meetings[0]["owner_agent_verification"]["verification_basis"] == "owner-agent-information-check.v1"
        assert meetings[0]["checked_source_url"] == final_url
        assert meetings[0]["source_urls"] == [final_url]
        assert meetings[0]["original_source_url"] == requested_url
        assert meetings[0]["sources"][0]["source_url"] == requested_url
        assert not meetings[0].get("relevance_reason") and "PRIVATE" not in json.dumps(meetings)
    check(reader)
    with reader.public_snapshot(): check(reader)
    from agentic_wiki.chat_evidence import source_urls
    approved_meeting = reader.meetings_all(base_date="2026-10-15")[0]
    assert source_urls(approved_meeting) == [final_url]
    from types import SimpleNamespace
    from agentic_wiki.chat_evidence import EvidenceTurn
    turn = object.__new__(EvidenceTurn)
    turn.source_only = False; turn.sources = []; turn.evidence = []; turn.chars = 0; turn.notes = []
    turn.question = "When is the World Climate Summit?"
    turn.owner = SimpleNamespace(responder=SimpleNamespace(kb=SimpleNamespace(documents=[], source_documents=[])))
    evidence = turn.add(approved_meeting, kind="meeting", text="World Climate Summit 2027", urls=source_urls(approved_meeting))
    assert evidence["source"]["source_urls"] == [final_url]
    assert evidence["source"]["original_source_url"] == requested_url
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    static = RegistryReader.from_public_export(root)
    check(static)
    static_meeting = static.meetings_all(base_date="2026-10-15")[0]
    assert static_meeting["checked_source_url"] == final_url and static_meeting["original_source_url"] == requested_url
    markdown = public_wiki_pages(database)["registry-meetings.md"]
    assert f"Source: [checked source]({final_url})" in markdown
    assert f"Original source link (provenance): [{requested_url}]({requested_url})" in markdown
    assert static.static_snapshot["wiki_pages"]["registry-meetings.md"] == markdown


def _owner_article_with_private_metadata(tmp_path, source, final_url=None, *, report_appearance=False, occurrence_id="occurrence"):
    from test_registry_publication import _pdf
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO article_enrichments(enrichment_id,article_id,article_version_id,status,summary,categories_json,keywords_json,language,generator_kind,generator_name,generator_version,generated_at) VALUES('e-good','good','v-good','complete','PRIVATE original enrichment summary','[\"PRIVATE category\"]','[\"PRIVATE keyword\"]','en','manual','PRIVATE generator','v1','2026-09-01T00:00:00Z')")
        if source != "native":
            _pdf(db, identity="pdf-good", url="https://example.org/"+("good" if source == "linked" else "pdf-only"),
                occurrence_id=occurrence_id, verified=False)
            db.execute("UPDATE pdf_intake_articles SET type_safe_classification_json=?", (json.dumps({"label": "article", "reason": "PRIVATE classifier reason"}),))
        if report_appearance:
            db.execute("INSERT INTO reports VALUES ('review-report','2026-09-28','climate-monitor-2026-09-28.md','Review report',?,'weekly','weekly-pillars-v1',1,1,0,'[]')", ("e" * 64,))
            db.execute("INSERT INTO discoveries VALUES ('review-discovery','review-report',1,'Pillar A','A','good','v-good','https://example.org/good','Title good','Summary good',1,NULL)")
            db.execute("INSERT INTO report_appearances(report_id,article_id,version_id,discovery_id,section,pillar,ordinal,disposition) VALUES ('review-report','good','v-good','review-discovery','Pillar A','A',1,'new')")
        originals = {table: db.execute("SELECT * FROM "+table).fetchall() for table in ("article_enrichments", "pdf_intake_articles", "pdf_intake_article_occurrences")}
    fields = {"title": "Climate risk findings", "summary": "Flood exposure increased in the study region.", "publication_date": "2026-10-02"}
    manifest = _manifest(database, kind="article" if source == "native" else "pdf_article",
        identity="good" if source == "native" else "pdf-good", occurrence="core:good" if source == "native" else occurrence_id,
        fields=fields, body="Climate risk findings\nFlood exposure increased in the study region.\n2 October 2026",
        final_url=final_url)
    manifest["observations"][0]["field_evidence"]["publication_date"]["excerpt"] = "2 October 2026"
    staged = _ingest(database, tmp_path, manifest)
    approve_owner_check(database, _review(database, tmp_path, staged["candidate_sha256"]), lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    return database, originals


@pytest.mark.skipif(__import__('os').name == 'nt', reason='Actual API imports POSIX management')
@pytest.mark.parametrize("source", ["pdf", "linked"])
def test_owner_checked_url_reaches_article_api_wiki_and_chat(tmp_path, monkeypatch, source):
    from agentic_wiki.chat_evidence import EvidenceTurn, source_urls
    from climate_registry.publication import public_wiki_pages
    from fastapi.testclient import TestClient
    import api_server

    final_url = "https://example.org/articles/current"
    original = "https://example.org/" + ("pdf-only" if source == "pdf" else "good")
    database, _ = _owner_article_with_private_metadata(tmp_path, source, final_url=final_url)
    reader = _reader(database, tmp_path)
    monkeypatch.setattr(api_server, "_registry_reader", lambda: reader)
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    client = TestClient(api_server.app)

    endpoint = "/api/registry/pdf-intake/articles/pdf-good"
    pdf_detail = client.get(endpoint).json()
    assert pdf_detail["checked_source_url"] == final_url
    assert source_urls(pdf_detail) == [final_url]
    pdf_occurrence = pdf_detail["occurrences"][0]
    assert pdf_occurrence["raw_url"] == original and pdf_occurrence["checked_source_url"] == final_url

    if source == "linked":
        from climate_registry.wiki import _registry_report_markdown

        article_detail = client.get("/api/registry/articles/good").json()
        assert article_detail["checked_source_url"] == final_url
        assert source_urls(article_detail) == [final_url]
        assert article_detail["pdf_occurrences"][0]["raw_url"] == original
        article_list = client.get("/api/registry/articles?page=1&page_size=100&include_pdf=true").json()
        listed = next(item for item in article_list["items"] if item["article_id"] == "good")
        assert listed["checked_source_url"] == final_url
        assert listed["owner_agent_verification"]["final_url"] == final_url
        node = shutil.which("node")
        assert node, "Node.js is required for the actual article-card navigation regression"
        script = Path(__file__).with_name("test_checked_source_article_card.mjs")
        harness_payload = {"items": [listed], "pagination": article_list["pagination"]}
        subprocess.run([node, str(script), json.dumps(harness_payload)], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, check=True)
        page = public_wiki_pages(database)["article-good.md"]
        assert f"Canonical article: [checked source]({final_url})" in page
        assert f"Original canonical/PDF source link (provenance): [{original}]({original})" in page
        assert "Source verification: owner_agent_verified_source; independently reviewed." in page
        for label in ("article citation", "Article citation"):
            lines = [line for line in page.splitlines() if label in line]
            assert lines and all(f"[checked source]({final_url})" in line for line in lines)
            assert all(f"original canonical/source link (provenance): [{original}]({original})" in line for line in lines)
        report = _registry_report_markdown("2026-10-12", "report.md", "f" * 64,
            [{"article": article_detail, "source_report_article": True}], approved_versions=True)
        assert f"Report article citation: [checked source]({final_url})" in report
        assert f"original canonical/source link (provenance): [{original}]({original})" in report
        turn = object.__new__(EvidenceTurn)
        turn.source_only = False
        chunk = type("Chunk", (), {"corpus": "wiki", "path": "wiki/article-good.md"})()
        document = type("Document", (), {"path": "wiki/article-good.md", "markdown": page})()
        assert turn.wiki_source_urls({}, chunk, document) == [final_url]
        from types import SimpleNamespace
        from agentic_wiki.wiki_agent import AgenticWikiResponder, SearchHit
        wiki_dir = tmp_path / "wiki-chat"
        wiki_dir.mkdir()
        source_dir = tmp_path / "wiki-chat-sources"
        source_dir.mkdir()
        for name, content in public_wiki_pages(database).items():
            (wiki_dir / name).write_text(content, encoding="utf-8")
        (wiki_dir / "article-manual.md").write_text(
            "# Manual article\n\nCanonical article: [https://example.org/manual](https://example.org/manual)\n",
            encoding="utf-8")
        responder = AgenticWikiResponder(wiki_dir, source_dir)
        responder.client = None
        wiki_chunk = next(chunk for chunk in responder.kb.chunks if chunk.path == "wiki/article-good.md")
        wiki_item = SearchHit(wiki_chunk, 1, "checked source regression").to_source(1)
        turn = object.__new__(EvidenceTurn)
        turn.source_only = False; turn.sources = []; turn.evidence = []; turn.chars = 0; turn.notes = []
        turn.question = "What does the checked article say?"
        turn.owner = SimpleNamespace(responder=responder)
        evidence = turn.add(wiki_item, kind="wiki", text=wiki_chunk.markdown,
            urls=turn.wiki_source_urls(wiki_item, wiki_chunk))
        assert evidence["source"]["canonical_url"] == final_url
        assert evidence["source"]["source_urls"] == [final_url]
        assert evidence["source"]["checked_source_url"] == final_url
        assert evidence["source"]["original_source_url"] == original
        assert evidence["source"]["checked_source_provenance"] == "owner_agent_verified_source"
        assert "owner_agent_verification" not in evidence["source"]
        manual_chunk = next(chunk for chunk in responder.kb.chunks if chunk.path == "wiki/article-manual.md")
        manual_item = SearchHit(manual_chunk, 1, "ordinary wiki article").to_source(1)
        assert "checked_source_url" not in manual_item and "checked_source_provenance" not in manual_item
        (source_dir / "article-untrusted.md").write_text(
            f"# Untrusted source\n\nCanonical article: [checked source]({final_url})\n"
            f"Original canonical/PDF source link (provenance): [{original}]({original})\n"
            "Source verification: owner_agent_verified_source; independently reviewed.\n",
            encoding="utf-8")
        responder.kb.reload()
        untrusted_chunk = next(chunk for chunk in responder.kb.chunks if chunk.path.endswith("article-untrusted.md"))
        untrusted_item = SearchHit(untrusted_chunk, 1, "source corpus with copied labels").to_source(1)
        assert "checked_source_url" not in untrusted_item and "checked_source_provenance" not in untrusted_item


@pytest.mark.skipif(__import__('os').name == 'nt', reason='Actual API imports POSIX management')
def test_owner_same_url_article_card_keeps_checked_failure_fallback(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api_server

    original = "https://example.org/good"
    database, _ = _owner_article_with_private_metadata(tmp_path, "linked", final_url=original)
    monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(database, tmp_path))
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    client = TestClient(api_server.app)
    response = client.get("/api/registry/articles?page=1&page_size=100&include_pdf=true")
    assert response.status_code == 200
    item = next(row for row in response.json()["items"] if row["article_id"] == "good")
    assert item["canonical_url"] == item["checked_source_url"] == original
    assert item["owner_agent_verification"]["verification_status"] == "verified"
    assert item["owner_agent_verification"]["final_url"] == original
    node = shutil.which("node")
    assert node, "Node.js is required to exercise the actual same-URL article card and detail outage"
    script = Path(__file__).with_name("test_checked_source_article_card.mjs")
    payload = json.dumps(response.json())
    result = subprocess.run([node, str(script), payload, payload], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.skipif(__import__('os').name == 'nt', reason='Actual API imports POSIX management')
def test_owner_native_article_success_keeps_distinct_original_provenance(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import api_server

    def actual_api(scope, final_url):
        scope.mkdir()
        database, _ = _owner_article_with_private_metadata(scope, "native", final_url=final_url)
        monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(database, scope))
        monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
        client = TestClient(api_server.app)
        detail_response = client.get("/api/registry/articles/good")
        list_response = client.get("/api/registry/articles?page=1&page_size=100&include_pdf=true")
        assert detail_response.status_code == list_response.status_code == 200
        detail = detail_response.json()
        listing = list_response.json()
        listed = next(row for row in listing["items"] if row["article_id"] == "good")
        assert detail["owner_agent_verification"]["verification_status"] == "verified"
        assert detail["checked_source_url"] == final_url
        assert detail["canonical_url"] == "https://example.org/good"
        assert not detail.get("pdf_occurrences")
        assert listed["owner_agent_verification"]["final_url"] == final_url
        return {"items": [listed], "pagination": listing["pagination"]}, detail

    approved_list, approved_detail = actual_api(tmp_path / "native-distinct", "https://example.org/events")
    same_list, same_detail = actual_api(tmp_path / "native-equal", "https://example.org/good")
    node = shutil.which("node")
    assert node, "Node.js is required to exercise the actual registered native article click and loader"
    script = Path(__file__).with_name("test_checked_source_article_card.mjs")
    result = subprocess.run(
        [node, str(script), json.dumps(approved_list), json.dumps(same_list),
         json.dumps(approved_detail), json.dumps(same_detail)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.skipif(__import__('os').name == 'nt', reason='Actual API imports POSIX management')
def test_owner_same_url_report_wiki_chat_keeps_checked_destination(tmp_path, monkeypatch):
    from climate_registry.publication import public_wiki_pages, export_public_snapshot
    from fastapi.testclient import TestClient
    import api_server
    from types import SimpleNamespace
    from agentic_wiki.wiki_agent import AgenticWikiResponder, SearchHit
    from agentic_wiki.chat_evidence import EvidenceTurn

    original = "https://example.org/good"
    database, _ = _owner_article_with_private_metadata(tmp_path, "linked", original, report_appearance=True)
    monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(database, tmp_path))
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    client = TestClient(api_server.app)
    report = client.get("/api/registry/reports/2026-09-28")
    assert report.status_code == 200
    report_article = report.json()["articles"][0]
    assert report_article["canonical_url"] == report_article["checked_source_url"] == original
    assert report_article["owner_agent_verification"]["final_url"] == original

    pages = public_wiki_pages(database)
    report_page = pages["climate-monitor-2026-09-28.md"]
    assert report_page.count(f"]({original})") == 1
    assert f"Report article citation: [checked source]({original})" in report_page
    assert f"original canonical/source link (provenance): `{original}`" in report_page
    export_root = tmp_path / "export-equal"; (export_root / "wiki").mkdir(parents=True)
    export_public_snapshot(database, export_root / "wiki/public-registry.json")
    exported = json.loads((export_root / "wiki/public-registry.json").read_text(encoding="utf-8"))
    assert exported["wiki_pages"]["climate-monitor-2026-09-28.md"] == report_page

    wiki_dir = tmp_path / "wiki-equal"; wiki_dir.mkdir()
    source_dir = tmp_path / "sources-equal"; source_dir.mkdir()
    for name, markdown in pages.items():
        (wiki_dir / name).write_text(markdown, encoding="utf-8")
    responder = AgenticWikiResponder(wiki_dir, source_dir); responder.client = None
    article_chunk = next(item for item in responder.kb.chunks
        if item.path == "wiki/article-good.md" and item.checked_source_url == original)
    report_chunk = next(item for item in responder.kb.chunks
        if item.path == "wiki/climate-monitor-2026-09-28.md" and item.checked_source_url == original)
    assert article_chunk.original_source_url == report_chunk.original_source_url == original
    report_item = SearchHit(report_chunk, 1, "equal checked report citation").to_source(1)
    turn = object.__new__(EvidenceTurn)
    turn.source_only = False; turn.sources = []; turn.evidence = []; turn.chars = 0; turn.notes = []
    turn.question = "What does the report say?"; turn.owner = SimpleNamespace(responder=responder)
    evidence = turn.add(report_item, kind="wiki", text=report_chunk.markdown,
        urls=turn.wiki_source_urls(report_item, report_chunk))
    assert evidence["source"]["canonical_url"] == original
    assert evidence["source"]["checked_source_url"] == original
    assert evidence["source"]["original_source_url"] == original
    assert evidence["source"]["source_urls"] == [original]
    history_fixture = tmp_path / "equal-report-history.json"
    history_fixture.write_text(json.dumps({"source": evidence["source"],
        "checked_url": original, "original_url": original}), encoding="utf-8")
    node = shutil.which("node")
    assert node, "Node.js is required to exercise the actual report-card history and detail-outage path"
    subprocess.run([node, str(Path(__file__).with_name("test_checked_source_report_history.mjs")),
        str(history_fixture)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)

    pdf_root = tmp_path / "pdf-equal"; pdf_root.mkdir()
    pdf_database, _ = _owner_article_with_private_metadata(pdf_root, "pdf", "https://example.org/pdf-only",
        occurrence_id="pdf-article-occurrence-equal-v7")
    pdf_pages = public_wiki_pages(pdf_database)
    pdf_wiki = tmp_path / "pdf-wiki-equal"; pdf_wiki.mkdir()
    pdf_sources = tmp_path / "pdf-sources-equal"; pdf_sources.mkdir()
    for name, markdown in pdf_pages.items():
        (pdf_wiki / name).write_text(markdown, encoding="utf-8")
    pdf_responder = AgenticWikiResponder(pdf_wiki, pdf_sources); pdf_responder.client = None
    observation = next(item for item in pdf_responder.kb.chunks
        if item.path == "wiki/registry-source-observation-pdf-article-occurrence-equal-v7.md"
        and item.checked_source_url == "https://example.org/pdf-only")
    assert observation.original_source_url == observation.checked_source_url
    pdf_turn = object.__new__(EvidenceTurn)
    pdf_turn.source_only = False; pdf_turn.sources = []; pdf_turn.evidence = []
    pdf_turn.chars = 0; pdf_turn.notes = []; pdf_turn.question = "What was checked?"
    pdf_turn.owner = SimpleNamespace(responder=pdf_responder)
    pdf_item = SearchHit(observation, 1, "equal checked PDF observation").to_source(1)
    pdf_evidence = pdf_turn.add(pdf_item, kind="wiki", text=observation.markdown,
        urls=pdf_turn.wiki_source_urls(pdf_item, observation))
    pdf_source = pdf_evidence["source"]
    assert pdf_source["checked_source_url"] == pdf_source["original_source_url"] == "https://example.org/pdf-only"
    assert pdf_source["source_urls"] == ["https://example.org/pdf-only"]
    monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(pdf_database, pdf_root))
    pdf_client = TestClient(api_server.app)
    pdf_fixture = {
        "pdf_report": pdf_client.get("/api/registry/pdf-intake/reports/" + "f" * 64 + "?page=1&page_size=5").json(),
        "pdf_list": pdf_client.get("/api/registry/pdf-intake/articles?page=1&page_size=100&include_linked=true").json(),
        "chat_source": pdf_source,
        "checked_url": "https://example.org/pdf-only",
        "original_url": "https://example.org/pdf-only",
    }
    pdf_fixture_path = tmp_path / "equal-pdf-entrypoints.json"
    pdf_fixture_path.write_text(json.dumps(pdf_fixture), encoding="utf-8")
    subprocess.run([node, str(Path(__file__).with_name("test_checked_source_entrypoints.mjs")),
        str(pdf_fixture_path)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)


@pytest.mark.skipif(__import__('os').name == 'nt', reason='Actual API imports POSIX management')
def test_owner_report_bulk_export_chat_and_category_carriers_keep_checked_role(tmp_path, monkeypatch):
    from climate_registry.publication import public_wiki_pages, export_public_snapshot
    from climate_registry.category_correction import correct_article_categories
    from climate_monitor.taxonomy import load_article_taxonomy
    from fastapi.testclient import TestClient
    import api_server

    original = "https://example.org/good"
    final = "https://example.org/articles/current"
    database, _ = _owner_article_with_private_metadata(tmp_path, "linked", final, report_appearance=True)
    reader = _reader(database, tmp_path)
    monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(database, tmp_path))
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    client = TestClient(api_server.app)
    report = client.get("/api/registry/reports/2026-09-28")
    assert report.status_code == 200
    report_article = report.json()["articles"][0]
    assert report_article["canonical_url"] == original
    assert report_article["owner_agent_verification"]["final_url"] == final

    report_page = public_wiki_pages(database)["climate-monitor-2026-09-28.md"]
    assert f"Report article citation: [checked source]({final})" in report_page
    assert f"original canonical/source link (provenance): [{original}]({original})" in report_page
    export_root = tmp_path / "export"; (export_root / "wiki").mkdir(parents=True)
    export_public_snapshot(database, export_root / "wiki/public-registry.json")
    exported = json.loads((export_root / "wiki/public-registry.json").read_text(encoding="utf-8"))
    assert exported["wiki_pages"]["climate-monitor-2026-09-28.md"] == report_page

    wiki_dir = tmp_path / "report-wiki"; wiki_dir.mkdir()
    source_dir = tmp_path / "report-sources"; source_dir.mkdir()
    for name, markdown in public_wiki_pages(database).items():
        (wiki_dir / name).write_text(markdown, encoding="utf-8")
    from types import SimpleNamespace
    from agentic_wiki.wiki_agent import AgenticWikiResponder, SearchHit
    from agentic_wiki.chat_evidence import EvidenceTurn
    responder = AgenticWikiResponder(wiki_dir, source_dir); responder.client = None
    chunk = next(item for item in responder.kb.chunks if item.path == "wiki/climate-monitor-2026-09-28.md" and item.checked_source_url == final)
    item = SearchHit(chunk, 1, "checked report citation").to_source(1)
    turn = object.__new__(EvidenceTurn)
    turn.source_only = False; turn.sources = []; turn.evidence = []; turn.chars = 0; turn.notes = []
    turn.question = "What does the report say?"; turn.owner = SimpleNamespace(responder=responder)
    evidence = turn.add(item, kind="wiki", text=chunk.markdown, urls=turn.wiki_source_urls(item, chunk))
    assert evidence["source"]["canonical_url"] == final
    assert evidence["source"]["checked_source_url"] == final
    assert evidence["source"]["original_source_url"] == original
    assert evidence["source"]["source_urls"] == [final]
    history_fixture = tmp_path / "checked-source-report-history.json"
    history_fixture.write_text(json.dumps({"source": evidence["source"], "checked_url": final, "original_url": original}), encoding="utf-8")
    history_script = Path(__file__).with_name("test_checked_source_report_history.mjs")
    node = shutil.which("node")
    assert node, "Node.js is required to exercise the actual Chat report-card history and outage path"
    subprocess.run([node, str(history_script), str(history_fixture)], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=True)

    pdf_root = tmp_path / "pdf-owner"; pdf_root.mkdir()
    occurrence_id = "pdf-article-occurrence-owner-v5"
    pdf_database, _ = _owner_article_with_private_metadata(pdf_root, "pdf", final, occurrence_id=occurrence_id)
    monkeypatch.setattr(api_server, "_registry_reader", lambda: _reader(pdf_database, pdf_root))
    pdf_client = TestClient(api_server.app)
    pdf_report = pdf_client.get("/api/registry/pdf-intake/reports/" + "f" * 64 + "?page=1&page_size=5").json()
    pdf_list = pdf_client.get("/api/registry/pdf-intake/articles?page=1&page_size=100&include_linked=true").json()
    pdf_pages = public_wiki_pages(pdf_database)
    pdf_wiki = tmp_path / "pdf-wiki"; pdf_wiki.mkdir()
    pdf_sources = tmp_path / "pdf-sources"; pdf_sources.mkdir()
    for name, markdown in pdf_pages.items():
        (pdf_wiki / name).write_text(markdown, encoding="utf-8")
    pdf_responder = AgenticWikiResponder(pdf_wiki, pdf_sources); pdf_responder.client = None
    observation_chunk = next(item for item in pdf_responder.kb.chunks
        if item.path == "wiki/registry-source-observation-" + occurrence_id + ".md" and item.checked_source_url == final)
    observation_item = SearchHit(observation_chunk, 1, "checked PDF observation").to_source(1)
    observation_turn = object.__new__(EvidenceTurn)
    observation_turn.source_only = False; observation_turn.sources = []; observation_turn.evidence = []
    observation_turn.chars = 0; observation_turn.notes = []; observation_turn.question = "What was checked?"
    observation_turn.owner = SimpleNamespace(responder=pdf_responder)
    observation_evidence = observation_turn.add(observation_item, kind="wiki", text=observation_chunk.markdown,
        urls=observation_turn.wiki_source_urls(observation_item, observation_chunk))
    assert observation_evidence["source"]["checked_source_url"] == final
    assert observation_evidence["source"]["original_source_url"] == "https://example.org/pdf-only"
    workflow_fixture = {"pdf_report": pdf_report, "pdf_list": pdf_list,
        "chat_source": observation_evidence["source"], "checked_url": final,
        "original_url": "https://example.org/pdf-only"}
    workflow_path = tmp_path / "checked-source-ui-fixture.json"
    workflow_path.write_text(json.dumps(workflow_fixture), encoding="utf-8")
    script = Path(__file__).with_name("test_checked_source_entrypoints.mjs")
    node = shutil.which("node")
    assert node, "Node.js is required for the actual imported-report and Wiki-to-Chat navigation regression"
    subprocess.run([node, str(script), str(workflow_path)], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=True)

    taxonomy = load_article_taxonomy()
    with sqlite3.connect(database) as db:
        binding = db.execute("SELECT is_visible,published_candidate_sha256,latest_candidate_sha256 FROM registry_publication WHERE entity_kind='article' AND entity_id='good'").fetchone()
    plan = {"schema_version": "registry-category-correction.v1", "taxonomy": {
        "taxonomy_id": taxonomy.taxonomy_id, "sha256": taxonomy.sha256, "definition_yaml": taxonomy.definition_yaml},
        "records": [{"entity_kind": "article", "entity_id": "good", "canonical_url": original,
            "expected_binding": {"is_visible": binding[0], "published_candidate_sha256": binding[1], "latest_candidate_sha256": binding[2]},
            "expected_categories": [], "categories": [taxonomy.categories[0].label, taxonomy.categories[1].label],
            "evidence_basis": "Retained climate source evidence."}]}
    plan_path = tmp_path / "category-correction.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    from climate_registry.errors import RegistryInputError
    with pytest.raises(RegistryInputError, match="category correction original URL or categories differ"):
        correct_article_categories(database=database, manifest=plan_path, apply=True, backup_dir=tmp_path / "category-backups")

    api_server._clear_registry_list_cache()
    categories = client.get("/api/registry/article-categories").json()
    assert categories["items"]
    assert all(not item["categories"] for item in categories["items"])
    node = shutil.which("node")
    assert node, "Node.js is required to prove the actual article-category builder input has no owner source button"
    source = (Path(__file__).resolve().parents[1] / "showcase/app.js").read_text(encoding="utf-8")
    start = source.index("function buildArticleCategoryGraph(articles, taxonomy) {")
    end = source.index("\nasync function loadArticleCategoryGraph()", start)
    evidence_start = source.index("function showArticleCategoryEvidence(label, articles) {")
    evidence_end = source.index("\nasync function fetchMarkdown(", evidence_start)
    script = r'''const assert=require("node:assert/strict");
class Element { constructor(tag="div",text=""){this.tag=tag;this.textContent=text;this.children=[];this.dataset={};this.hidden=false;} append(...items){this.children.push(...items);} replaceChildren(...items){this.children=[...items];} focus(){} }
const payload=JSON.parse(process.argv[1]);
const state={graphMode:"article-categories",articleCategoryGraphLoading:false};
const GRAPH_COPY={articleCategories:{title:"Category Map",hint:"",legendHtml:""}};
const els={articleCategoryEvidence:new Element()};
function registryElement(tag,klass="",text=""){return new Element(tag,text);}
''' + source[start:end] + source[evidence_start:evidence_end] + r'''
const graph=buildArticleCategoryGraph(payload.items,payload.taxonomy);
assert.ok(graph.nodes.every(node=>node.articles.length===0));
const node=graph.nodes.find(item=>item.label===payload.taxonomy.categories[0].label);
showArticleCategoryEvidence(node.label,node.articles);
const list=els.articleCategoryEvidence.children.find(item=>item.tag==="ul");
assert.equal(list.children.length,0);
process.stdout.write(JSON.stringify({owner_buttons:0,category_nodes:graph.nodes.length}));'''
    subprocess.run([node, "-e", script, json.dumps(categories)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)


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
    approve_owner_check(database, _review(database, tmp_path, staged["candidate_sha256"]), lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
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


def _rendered_owner_knowledge_base(tmp_path, pages):
    from agentic_wiki import WikiKnowledgeBase
    wiki, sources = tmp_path/"rendered-wiki", tmp_path/"empty-sources"
    wiki.mkdir(); sources.mkdir()
    for name, markdown in pages.items():
        (wiki/name).write_text(markdown, encoding="utf-8")
    return WikiKnowledgeBase(wiki, sources)


@pytest.mark.parametrize("publication_date,quote", [
    ("2026-08-20", "20-Aug-2026"),
    ("2026-08-07", "7 August 2026"),
    ("2026-08-17", "17 August 2026"),
    (None, ""),
])
def test_owner_pdf_publication_date_survives_source_body_and_retrieval_without_redating_catalog(tmp_path, publication_date, quote):
    from test_registry_publication import _pdf
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        _pdf(db, identity="pdf-good", verified=False, occurrence={"summary": "PRIVATE last updated 2026-08-19; planned event 2026-09-01",
            "summary_basis": "verbatim_pdf_article_row"})
        original = db.execute("SELECT * FROM pdf_intake_article_occurrences").fetchall()
    fields = {"title": "Climate risk findings", "summary": "Flood exposure increased in the study region."}
    if publication_date:
        fields["publication_date"] = publication_date
    manifest = _manifest(database, kind="pdf_article", identity="pdf-good", occurrence="occurrence", fields=fields,
        body="Climate risk findings\nFlood exposure increased in the study region.\n"+quote)
    if publication_date:
        manifest["observations"][0]["field_evidence"]["publication_date"]["excerpt"] = quote
    staged = _ingest(database, tmp_path, manifest)
    approve_owner_check(database, _review(database, tmp_path, staged["candidate_sha256"]),
        lifecycle_root=tmp_path / "runs", queue_dir=tmp_path/"queue", backup_dir=tmp_path/"backups", apply=True)
    pages = public_wiki_pages(database)
    name = "registry-source-observation-occurrence.md"
    markdown = pages[name]
    assert fields["title"] in markdown and fields["summary"] in markdown
    if publication_date:
        assert "Source publication date: " + publication_date in markdown
    else:
        assert "publication date:" not in markdown.lower()
    assert "PRIVATE" not in markdown and "2026-08-19" not in markdown and "2026-09-01" not in markdown
    kb = _rendered_owner_knowledge_base(tmp_path, pages)
    chunks = [chunk for chunk in kb.chunks if chunk.path == "wiki/"+name]
    assert len(chunks) == 1 and fields["summary"] in chunks[0].text
    if publication_date:
        assert "Source publication date: " + publication_date in chunks[0].text
    else:
        assert "publication date:" not in chunks[0].text.lower()
    catalog = next(row for row in kb.document_catalog() if row["path"] == "wiki/"+name)
    assert catalog["date"] == "-" and catalog["display_date"] is None
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    assert RegistryReader.from_public_export(root).static_snapshot["wiki_pages"][name] == markdown
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM pdf_intake_article_occurrences").fetchall() == original


@pytest.mark.parametrize("name,location,url,dates,pages", [
    ("Asia-Pacific Social Protection Week 2026: Empowered Lives, Resilient Futures",
     "Auditorium Halls 1-4, ADB Headquarters, Manila, Philippines",
     "https://www.adb.org/news/events/asia-pacific-social-protection-week-2026", "6 - 8 October 2026", [7, 9]),
    ("COP31", "Antalya, Türkiye", "https://unfccc.int/cop31", "9 to 20 November 2026", [11]),
    ("Pre-COP 31", None, "https://unfccc.int/cop31", "5-8 October 2026", [9]),
])
def test_owner_pdf_meeting_scoped_venue_and_evidence_link_survive_retrieval(tmp_path, name, location, url, dates, pages):
    from climate_registry.wiki import _render_registry_meetings
    fields = {"name": name, "raw_date": dates}
    if location:
        fields["location"] = location
    observations = [{"occurrence_id": "obs-"+str(index), "source_document_sha256": str(index+1)*64,
        "source_filename": "report-"+str(index)+".pdf", "page": page, "summary": ""}
        for index, page in enumerate(pages)]
    item = {**fields, "origin": "pdf_import", "verification_status": "verified", "pdf_observations": observations,
        "owner_agent_verification": {"public_fields": fields, "source_url": url},
        "location": "PRIVATE raw venue", "organizer": "PRIVATE organizer", "event_timezone": "PRIVATE timezone",
        "raw_time_text": "PRIVATE time", "relevance_reason": "PRIVATE relevance", "source_urls": ["https://unvalidated.example/"]}
    markdown = _render_registry_meetings([item])
    assert "Source: ["+url+"]("+url+")" in markdown
    if location:
        assert "Location: " + location in markdown
    else:
        assert "Location:" not in markdown
    for observation in observations:
        assert observation["source_document_sha256"] in markdown and "page "+str(observation["page"]) in markdown
    assert "PRIVATE" not in markdown and "unvalidated.example" not in markdown
    kb = _rendered_owner_knowledge_base(tmp_path, {"registry-meetings.md": markdown})
    chunk = next(chunk for chunk in kb.chunks if chunk.heading == name)
    assert name in chunk.text and dates in chunk.text and url in chunk.text and url in chunk.urls
    if location:
        assert "Location: " + location in chunk.text
    else:
        assert "Location:" not in chunk.text
    assert "PRIVATE" not in chunk.text and "unvalidated.example" not in chunk.text


def test_v1_multisource_review_allows_empty_sibling_but_rejects_conflicting_nonempty_scope(tmp_path):
    import copy

    def manifest_for(root, *, conflicting=False):
        root.mkdir()
        database = _database(root)
        _pdf_pair(database, target_extra={"source_urls": [
            "https://example.org/target", "https://example.org/target-secondary"]})
        fields = {"name": "IAIS Annual Conference 2027", "raw_date": "10–12 June 2027",
            "start_date": "2027-06-10", "end_date": "2027-06-12", "date_precision": "day"}
        body = "IAIS Annual Conference 2027\nIAIS Alternate Annual Conference 2027\n10–12 June 2027\nPRIVATE TOOL FOOTER"
        manifest = _manifest(database, kind="pdf_meeting", identity="target", occurrence="target-occ",
            fields=fields, body=body)
        assert len(manifest["plan"]["targets"]) == 2
        second = copy.deepcopy(manifest["observations"][0])
        second["target"] = next(x for x in manifest["plan"]["targets"] if x["source_url"].endswith("-secondary"))
        second["capture"]["requested_url"] = second["target"]["source_url"]
        second["capture"]["final_url"] = second["target"]["source_url"]
        if conflicting:
            alternative = "IAIS Alternate Annual Conference 2027"
            second["public_fields"]["name"] = alternative
            second["field_evidence"]["name"] = {"value": alternative, "excerpt": alternative,
                "record_context": alternative}
            second["excluded_fields"] = sorted(key for key, value in second["target"]["fields"].items()
                if value is not None and value != "" and key != "source_document_sha256"
                and second["public_fields"].get(key) != value)
        else:
            second["public_fields"] = {}
            second["field_evidence"] = {}
            second["excluded_fields"] = sorted(key for key, value in second["target"]["fields"].items()
                if value is not None and value != "" and key != "source_document_sha256")
        manifest["observations"].append(second)
        return database, manifest

    database, manifest = manifest_for(tmp_path / "empty-sibling")
    staged = _ingest(database, tmp_path / "empty-sibling", manifest)
    review = owner_review_template(database, staged["candidate_sha256"])
    assert len(review["source_checks"]) == 2

    database, manifest = manifest_for(tmp_path / "conflicting-sibling", conflicting=True)
    staged = _ingest(database, tmp_path / "conflicting-sibling", manifest)
    with pytest.raises(ValueError, match="sources disagree"):
        owner_review_template(database, staged["candidate_sha256"])
