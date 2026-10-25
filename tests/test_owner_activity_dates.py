"""Selective activity supplements preserve the current approved article."""
import base64
import hashlib
import json
import sqlite3

import pytest

from climate_registry.errors import RegistryInputError
from climate_registry.owner_checks import plan_owner_check
from climate_registry.owner_dates import ACTIVITY_SCOPE
from climate_registry.publication import export_public_snapshot
from climate_registry.read_api import RegistryReader
from test_owner_checks import _editorial_manifest, _ingest, _review
from test_owner_publication_dates import _seed, _date_review, _approve, _item, _date_fields_manifest
from test_verified_publication import _database, _reader


def _manifest(database, source="native", dates=("2026-10-21",)):
    manifest = plan_owner_check(database, "article" if source == "native" else "pdf_article",
        "good" if source == "native" else "pdf-good", activity_dates_only=True)
    target = next(item for item in manifest["plan"]["targets"]
        if item["occurrence_id"] == ("core:good" if source == "native" else "occurrence"))
    context = "Official record for " + target["source_url"]
    body = context + "\nClimate risk conference\n" + "\n".join(dates)
    raw = body.encode()
    values = [{"observed_at": day, "evidence": {"date_kind": "event", "date_precision": "day",
        "event_name": "Climate risk conference", "event_type": "conference", "event_status": "scheduled",
        "event_start_date": day, "event_end_date": day, "date_text": day,
        "review_status": "pass", "source_url": target["source_url"], "target_url": target["source_url"],
        "field": "Conference schedule", "text": day}} for day in dates]
    manifest.update(actor={"id": "activity-checker", "task_id": "activity-check-task"},
        authorization_reference="owner-approved-activity-date-completion", audit_reference="audit/source",
        checked_at="2026-10-16T12:00:00Z", observations=[{
            "target": target, "access_status": "accessible", "public_fields": {"activity_dates": values},
            "original_comparisons": {}, "excluded_fields": [],
            "field_evidence": {"activity_dates": {"value": values, "excerpt": body, "record_context": context}},
            "source_identity": {"article_url": target["source_url"], "excerpt": context,
                "reason": "Official record identifies this exact article."},
            "capture": {"requested_url": target["source_url"], "final_url": target["source_url"],
                "title": "Official activity record", "captured_at": "2026-10-16T11:59:00Z", "tool": "browser",
                "text": body, "bytes_base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest()}}])
    return manifest


def _non_event_dto(value):
    if isinstance(value, dict):
        return {key: _non_event_dto(item) for key, item in value.items()
            if key not in {"activity_dates", "is_activity", "date_observations",
                "published_candidate_sha256", "candidate_sha256"}}
    if isinstance(value, list):
        return [_non_event_dto(item) for item in value]
    return value


def _human_activity(database, source="native", *, start="2026-11-04", end=None, precision="day", event_id=None):
    manifest = plan_owner_check(database, "article" if source == "native" else "pdf_article",
        "good" if source == "native" else "pdf-good", activity_dates_only=True, human_date_override=True)
    value = {"observed_at": start, "evidence": {"date_kind": "event", "date_precision": precision,
        "event_name": "Climate risk conference", "event_type": "conference", "event_status": "scheduled",
        "event_start_date": start, "event_end_date": end or start, "date_text": start if not end or end == start else start + " to " + end}}
    if event_id:
        value["evidence"]["event_id"] = event_id
    manifest.update(actor={"id": "instruction-operator", "task_id": "explicit-date-task"},
        authorization_reference="human-message:277/activity", audit_reference="audit/activity",
        checked_at="2026-10-24T12:00:00Z", manual_authorization={"instruction": "Set the Climate risk conference event_start_date/event_end_date/date_precision to " + start + "/" + (end or start) + "/" + precision,
            "authorized_by": "owner", "scope": "record"}, observations=[{
            "target": manifest["plan"]["targets"][0], "public_fields": {"activity_dates": [value]},
            "field_evidence": {"activity_dates": {"fields": ["event_start_date", "event_end_date", "date_precision"]}}}])
    return manifest


@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
@pytest.mark.parametrize("start,precision", [("2026-11-04", "day"), ("2026-11", "month"), ("2026", "year")])
def test_explicit_activity_instruction_uses_dedicated_scope_without_source_pass(tmp_path, source, start, precision):
    from climate_registry.owner_dates import MANUAL_AUTHORIZATION
    database = _seed(tmp_path, source)
    reader = _reader(database, tmp_path); before = _item(reader, source)
    manifest = _human_activity(database, source, start=start, precision=precision)
    with sqlite3.connect(database) as db:
        source_rows = {table: db.execute("SELECT * FROM " + table).fetchall() for table in
            ("article_date_observations", "pdf_article_date_observations")}
    result = _ingest(database, tmp_path, manifest)
    assert result["status"] == "approved" and result["authorization_basis"] == MANUAL_AUTHORIZATION
    def check(current):
        after = _item(current, source)
        for field in ("summary", "title", "categories", "keywords", "publication_date", "updated_date", "revision_date", "content_date"):
            assert after.get(field) == before.get(field)
        assert after["is_activity"] and len(after["activity_dates"]) == 1
        event = after["activity_dates"][0]
        assert event["start_date"] == start and event["date_precision"] == precision
        assert event["authority"] == "human_override" and event["verification_status"] == "unchecked"
        assert event["instruction_reference"] == manifest["authorization_reference"]
        decision = after["manual_date_overrides"]["activity_dates"]
        assert decision["actor"] == manifest["actor"] and decision["old_value"] == []
        assert decision["baseline_candidate_sha256"] == manifest["plan"]["baseline_candidate_sha256"]
        assert decision["executed_at"] == manifest["checked_at"] and decision["instruction"] == manifest["manual_authorization"]["instruction"]
        meetings = current.meetings(base_date="2026-10-18", page_size=100)["items"]
        event = next(item for item in meetings if item["name"] == "Climate risk conference")
        assert event["start_date"] == start and event["authority"] == "human_override"
        assert event["verification_status"] != "verified"
    for direct in (True, False):
        with reader.public_snapshot(direct=direct): check(reader)
    root = tmp_path/"static"; (root/"wiki").mkdir(parents=True)
    export_public_snapshot(database, root/"wiki/public-registry.json")
    check(RegistryReader.from_public_export(root))
    from climate_registry.wiki import _render_registry_meetings
    event = reader.meetings(base_date="2026-10-18", page_size=100)["items"][0]
    assert "Date authority: human_override; source not verified" in _render_registry_meetings([event])
    from test_chat_evidence import owner
    chatroot = tmp_path/"chat-manual"; chatroot.mkdir()
    chat = owner(chatroot); chat.reader_factory = lambda: reader
    response = chat.answer("List climate risk conferences from 2026-01-01 to 2026-12-31")
    assert "Date authority: human_override; source not verified" in response["text"]
    assert any(item.get("authority") == "human_override" and item.get("instruction_reference") == manifest["authorization_reference"]
        for item in response["sources"])
    with sqlite3.connect(database) as db:
        for table, rows in source_rows.items(): assert db.execute("SELECT * FROM " + table).fetchall() == rows
        receipt = json.loads(db.execute("SELECT receipt_json FROM registry_reviews WHERE candidate_sha256=?", (result["candidate_sha256"],)).fetchone()[0])
        assert receipt["decision"] == "authorized" and "reviewed_at" not in receipt


@pytest.mark.parametrize("mutation", ["wrong_identity", "field", "ambiguous", "bounds", "precision", "source_pass", "capture", "empty_instruction", "mixed"])
def test_invalid_human_activity_instruction_cannot_write(tmp_path, mutation):
    database = _seed(tmp_path)
    manifest = _human_activity(database)
    entry = manifest["observations"][0]; proof = entry["public_fields"]["activity_dates"][0]["evidence"]
    if mutation == "wrong_identity": proof["event_id"] = "other-event"
    elif mutation == "field": entry["field_evidence"]["activity_dates"]["fields"] = ["event_organizer"]
    elif mutation == "ambiguous": proof["date_text"] = "11/04/2026"
    elif mutation == "bounds": proof["event_end_date"] = "2026-11-03"
    elif mutation == "precision": proof["date_precision"] = "month"
    elif mutation == "source_pass": proof["review_status"] = "pass"
    elif mutation == "capture": entry["capture"] = {"text": "fabricated"}
    elif mutation == "empty_instruction": manifest["manual_authorization"]["instruction"] = ""
    else: entry["public_fields"]["publication_date"] = "2026-11-04"
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(RegistryInputError): _ingest(database, tmp_path, manifest)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_human_activity_correction_restage_replace_and_revoke_preserve_source_history(tmp_path):
    from climate_registry.publication import stage_snapshot, snapshot_entity, _approve as approve_snapshot
    from climate_registry.owner_dates import MANUAL_AUTHORIZATION
    database = _seed(tmp_path, "linked")
    source = _manifest(database, "linked")
    source["observations"][0]["public_fields"]["activity_dates"][0]["evidence"].update(event_organizer="Climate risk conference")
    staged = _ingest(database, tmp_path, source)
    _approve(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    reader = _reader(database, tmp_path)
    original = reader.article("good")["activity_dates"][0]
    with sqlite3.connect(database) as db:
        old_candidates = dict(db.execute("SELECT candidate_sha256,snapshot_json FROM registry_candidates"))
        old_receipts = dict(db.execute("SELECT review_id,receipt_json FROM registry_reviews"))
    human = _human_activity(database, start="2026-10-21", end="2026-10-24", event_id=original["event_id"])
    human["observations"][0]["field_evidence"]["activity_dates"]["fields"] = ["event_end_date"]
    first = _ingest(database, tmp_path, human)
    after = reader.article("good")["activity_dates"]
    assert len(after) == 1 and after[0]["end_date"] == "2026-10-24"
    assert after[0]["organizer"] == original["organizer"]
    assert reader.pdf_article("pdf-good")["activity_dates"] == after
    with sqlite3.connect(database) as db:
        next_candidate = stage_snapshot(db, snapshot_entity(db, "article", "good"))
        assert next_candidate == first["candidate_sha256"]
    from test_article_content_dates import _stage_complete_bundle
    next_candidate, _ = _stage_complete_bundle(database)
    with sqlite3.connect(database) as db:
        approve_snapshot(db, next_candidate, {"basis": "new complete approved content fixture"})
    assert reader.article("good")["activity_dates"] == after
    replacement = _human_activity(database, start="2026-10-21", end="2026-10-28", event_id=after[0]["event_id"])
    replacement["observations"][0]["field_evidence"]["activity_dates"]["fields"] = ["event_end_date"]
    second = _ingest(database, tmp_path, replacement)
    assert reader.article("good")["activity_dates"][0]["end_date"] == "2026-10-28"
    automation = _ingest(database, tmp_path, _manifest(database))
    with pytest.raises(RegistryInputError):
        _approve(database, tmp_path, _date_review(database, tmp_path, automation["candidate_sha256"]))
    revoke = _human_activity(database)
    revoke["manual_authorization"].update(action="revoke", instruction="Revoke this record's Climate risk conference date instruction.")
    revoke["observations"][0].update(public_fields={"activity_dates": None},
        field_evidence={"activity_dates": {"text": "revoke", "precision": "unknown"}})
    _ingest(database, tmp_path, revoke)
    assert reader.article("good")["activity_dates"] == [original]
    assert not reader.article("good").get("manual_date_overrides")
    with sqlite3.connect(database) as db:
        for sha, encoded in old_candidates.items(): assert db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0] == encoded
        for identity, encoded in old_receipts.items(): assert db.execute("SELECT receipt_json FROM registry_reviews WHERE review_id=?", (identity,)).fetchone()[0] == encoded
        for result in (first, second):
            receipt = json.loads(db.execute("SELECT receipt_json FROM registry_reviews WHERE candidate_sha256=?", (result["candidate_sha256"],)).fetchone()[0])
            assert receipt["schema_version"] == MANUAL_AUTHORIZATION and receipt["decision"] == "authorized"


def test_version_limited_human_activity_expires_only_when_next_body_is_approved(tmp_path):
    from test_article_content_dates import _stage_complete_bundle, _stage_next_complete_bundle
    from climate_registry.publication import _approve as approve_snapshot
    database = _seed(tmp_path)
    candidate, bundle = _stage_complete_bundle(database)
    with sqlite3.connect(database) as db: approve_snapshot(db, candidate, {"basis": "body-b fixture pass"})
    manifest = _human_activity(database)
    manifest["manual_authorization"].update(scope="content_version", content_version_id="body-b")
    _ingest(database, tmp_path, manifest)
    reader = _reader(database, tmp_path)
    before = reader.article("good")["activity_dates"]
    stale = _human_activity(database, event_id=before[0]["event_id"])
    candidate, _ = _stage_next_complete_bundle(database, bundle)
    assert reader.article("good")["activity_dates"] == before
    with sqlite3.connect(database) as db: approve_snapshot(db, candidate, {"basis": "body-c fixture pass"})
    for direct in (True, False):
        with reader.public_snapshot(direct=direct):
            item = reader.article("good")
            assert item["content_version_id"] == "body-c" and not item["is_activity"] and not item["activity_dates"]
            assert not item.get("manual_date_overrides") and not item.get("activity_dates_verification")
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(RegistryInputError, match="stale"):
        _ingest(database, tmp_path, stale)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash


def test_human_activity_cannot_change_an_unselected_bound(tmp_path):
    database = _seed(tmp_path)
    staged = _ingest(database, tmp_path, _manifest(database))
    _approve(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    prior = _reader(database, tmp_path).article("good")["activity_dates"][0]
    manifest = _human_activity(database, start="2026-10-20", end="2026-10-24", event_id=prior["event_id"])
    manifest["observations"][0]["field_evidence"]["activity_dates"]["fields"] = ["event_end_date"]
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with pytest.raises(RegistryInputError, match="unselected"):
        _ingest(database, tmp_path, manifest)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_human_activity_keeps_unrelated_active_activity_without_reapproval(tmp_path):
    database = _seed(tmp_path)
    first = _ingest(database, tmp_path, _human_activity(database))
    reader = _reader(database, tmp_path)
    existing = reader.article("good")["activity_dates"][0]
    second = _human_activity(database, start="2026-11-20")
    second["observations"][0]["public_fields"]["activity_dates"][0]["evidence"]["event_name"] = "Other named activity"
    second["manual_authorization"]["instruction"] = "Set Other named activity dates to 2026-11-20, day precision."
    _ingest(database, tmp_path, second)
    after = reader.article("good")["activity_dates"]
    assert len(after) == 2 and next(event for event in after if event["name"] == existing["name"]) == existing
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM registry_reviews WHERE candidate_sha256=?", (first["candidate_sha256"],)).fetchone()[0] == 1


def test_human_activity_exact_identity_keeps_other_same_named_event(tmp_path):
    database = _seed(tmp_path)
    staged = _ingest(database, tmp_path, _manifest(database, dates=("2026-10-21", "2026-10-28")))
    _approve(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    reader = _reader(database, tmp_path)
    first, other = sorted(reader.article("good")["activity_dates"], key=lambda event: event["start_date"])
    _ingest(database, tmp_path, _human_activity(database, start="2026-10-21", event_id=first["event_id"]))
    same = next(event for event in reader.meetings(base_date="2026-10-18", page_size=100)["items"]
        if event["start_date"] == "2026-10-21")
    assert same["authority"] == "human_override" and same["verification_status"] != "verified"
    _ingest(database, tmp_path, _human_activity(database, start="2026-10-22", event_id=first["event_id"]))
    after = reader.article("good")["activity_dates"]
    assert len(after) == 2 and other in after
    changed = next(event for event in after if event.get("authority") == "human_override")
    assert changed["start_date"] == "2026-10-22" and changed["replaces_event_id"] == first["event_id"]
    _ingest(database, tmp_path, _human_activity(database, start="2026-10-23", event_id=changed["event_id"]))
    after = reader.article("good")["activity_dates"]
    assert len(after) == 2 and other in after
    assert next(event for event in after if event.get("authority") == "human_override")["start_date"] == "2026-10-23"


@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
def test_activity_only_preserves_dtos_and_moves_article_to_dates(tmp_path, source):
    database = _seed(tmp_path, source)
    reader = _reader(database, tmp_path)
    before = _item(reader, source)
    root = tmp_path / "static"
    (root / "wiki").mkdir(parents=True)
    export_public_snapshot(database, root / "wiki/public-registry.json")
    static_before = _item(RegistryReader.from_public_export(root), source)
    manifest = _manifest(database, source, dates=("2026-10-21", "2026-10-28"))
    with sqlite3.connect(database) as db:
        original = {table: db.execute("SELECT * FROM " + table).fetchall() for table in (
            "articles", "article_versions", "article_enrichments", "pdf_intake_articles",
            "pdf_intake_article_occurrences", "article_date_observations", "pdf_article_date_observations")}
        baseline = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",
            (manifest["plan"]["baseline_candidate_sha256"],)).fetchone()[0])
    staged = _ingest(database, tmp_path, manifest)
    assert _item(reader, source) == before
    assert _ingest(database, tmp_path, manifest)["status"] == "no-op"
    receipt = _date_review(database, tmp_path, staged["candidate_sha256"])
    assert receipt["review_scope"] == ACTIVITY_SCOPE
    assert receipt["schema_version"] == "owner-agent-article-activity-dates-review.v1"
    _approve(database, tmp_path, receipt)
    assert _approve(database, tmp_path, receipt)["status"] == "no-op"

    def check(current, expected=before):
        after = _item(current, source)
        assert after["is_activity"] is True
        assert {event["start_date"] for event in after["activity_dates"]} == {"2026-10-21", "2026-10-28"}
        assert _non_event_dto(after) == _non_event_dto(expected)
        events = current.meetings(base_date="2026-10-18", page_size=100)["items"]
        assert {event["start_date"] for event in events if event["name"] == "Climate risk conference"} == {"2026-10-21", "2026-10-28"}
        if source == "linked":
            assert current.pdf_article("pdf-good")["is_activity"] is True

    check(reader)
    with reader.public_snapshot(direct=True):
        check(reader)
        from climate_registry.list_projection import RegistryListProjection
        projection = RegistryListProjection(reader)
        try:
            assert not any(item["article_id"] in {"good", "pdf-good"}
                for item in projection.articles(page=1, page_size=100, include_pdf=True)["items"])
        finally:
            projection.close()
    with reader.public_snapshot():
        check(reader)
    export_public_snapshot(database, root / "wiki/public-registry.json")
    check(RegistryReader.from_public_export(root), static_before)
    with sqlite3.connect(database) as db:
        for table, rows in original.items():
            assert db.execute("SELECT * FROM " + table).fetchall() == rows
        staged_snapshot = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?",
            (staged["candidate_sha256"],)).fetchone()[0])
        assert staged_snapshot.get("derived_display") == baseline.get("derived_display")


def test_activity_only_keeps_owner_reviewed_summary_and_metadata(tmp_path):
    database = _database(tmp_path)
    staged = _ingest(database, tmp_path, _editorial_manifest(database))
    _approve(database, tmp_path, _review(database, tmp_path, staged["candidate_sha256"]))
    before = _reader(database, tmp_path).article("good")
    staged = _ingest(database, tmp_path, _manifest(database))
    _approve(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    after = _reader(database, tmp_path).article("good")
    assert _non_event_dto(after) == _non_event_dto(before)
    assert after["owner_agent_verification"] == before["owner_agent_verification"]


@pytest.mark.parametrize("mutation", ["missing_review", "source_url", "target_url", "capture_url", "name", "bounds", "title"])
def test_activity_only_rejects_missing_review_or_unbound_evidence(tmp_path, mutation):
    database = _seed(tmp_path)
    manifest = _manifest(database)
    entry = manifest["observations"][0]
    evidence = entry["public_fields"]["activity_dates"][0]["evidence"]
    if mutation == "missing_review":
        evidence.pop("review_status")
    elif mutation in {"source_url", "target_url"}:
        evidence[mutation] = "https://example.org/unrelated"
    elif mutation == "capture_url":
        entry["capture"]["requested_url"] = "https://example.org/unrelated"
    elif mutation == "name":
        evidence["event_name"] = "Invented meeting"
    elif mutation == "bounds":
        evidence["event_end_date"] = "2026-10-22"
    else:
        entry["public_fields"]["title"] = "Changed title"
    with pytest.raises(RegistryInputError):
        _ingest(database, tmp_path, manifest)


@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
@pytest.mark.parametrize("text", [None, "", "   ", "Uncaptured event evidence"])
def test_activity_only_requires_literal_text_before_projection(tmp_path, source, text):
    database = _seed(tmp_path, source)
    reader = _reader(database, tmp_path)
    before = _item(reader, source)
    meetings = reader.meetings(base_date="2026-10-18", page_size=100)
    manifest = _manifest(database, source)
    evidence = manifest["observations"][0]["public_fields"]["activity_dates"][0]["evidence"]
    if text is None:
        evidence.pop("text")
    else:
        evidence["text"] = text
    with pytest.raises(RegistryInputError, match="literal source capture evidence: text"):
        _ingest(database, tmp_path, manifest)
    assert _item(reader, source) == before
    assert reader.meetings(base_date="2026-10-18", page_size=100) == meetings


@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
@pytest.mark.parametrize(("field", "value"), [
    ("event_status", "cancelled"), ("event_status", "retrospective"), ("event_type", "retrospective"),
])
def test_activity_only_rejects_events_filtered_from_dates(tmp_path, source, field, value):
    database = _seed(tmp_path, source)
    reader = _reader(database, tmp_path)
    before = _item(reader, source)
    meetings = reader.meetings(base_date="2026-10-18", page_size=100)
    manifest = _manifest(database, source)
    manifest["observations"][0]["public_fields"]["activity_dates"][0]["evidence"][field] = value
    with pytest.raises(RegistryInputError, match="eligible for the Dates projection"):
        _ingest(database, tmp_path, manifest)
    assert _item(reader, source) == before
    assert reader.meetings(base_date="2026-10-18", page_size=100) == meetings


@pytest.mark.parametrize("mutation", ["actor", "binding", "publication", "visibility", "latest", "live"])
def test_activity_only_rejects_stale_review_without_publishing(tmp_path, mutation):
    database = _seed(tmp_path)
    manifest = _manifest(database)
    staged = _ingest(database, tmp_path, manifest)
    receipt = _date_review(database, tmp_path, staged["candidate_sha256"])
    with sqlite3.connect(database) as db:
        if mutation == "actor": receipt["actor"] = manifest["actor"]
        elif mutation == "binding": receipt["source_checks"]["baseline_candidate_sha256"] = "f" * 64
        elif mutation == "publication": db.execute("UPDATE registry_publication SET published_candidate_sha256=NULL WHERE entity_id='good'")
        elif mutation == "visibility": db.execute("UPDATE registry_publication SET is_visible=0 WHERE entity_id='good'")
        elif mutation == "latest": db.execute("UPDATE registry_publication SET latest_candidate_sha256=published_candidate_sha256 WHERE entity_id='good'")
        else: db.execute("UPDATE article_versions SET observed_summary='New unchecked body' WHERE article_id='good'")
        pointers = db.execute("SELECT * FROM registry_publication").fetchall()
        reviews = db.execute("SELECT * FROM registry_reviews").fetchall()
    with pytest.raises(RegistryInputError):
        _approve(database, tmp_path, receipt)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM registry_publication").fetchall() == pointers
        assert db.execute("SELECT * FROM registry_reviews").fetchall() == reviews


def test_activity_only_rejects_stale_plan(tmp_path):
    database = _seed(tmp_path)
    manifest = _manifest(database)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE article_versions SET observed_summary='New unchecked body' WHERE article_id='good'")
    with pytest.raises(RegistryInputError, match="stale"):
        _ingest(database, tmp_path, manifest)


@pytest.mark.parametrize("mutation", ["display", "dto"])
def test_activity_only_aborts_unintended_public_changes(tmp_path, monkeypatch, mutation):
    import climate_registry.owner_dates as dates
    database = _seed(tmp_path)
    before = _reader(database, tmp_path).article("good")
    staged = _ingest(database, tmp_path, _manifest(database))
    receipt = _date_review(database, tmp_path, staged["candidate_sha256"])
    if mutation == "display":
        append = dates._append_activity_rows
        def changed(view, *args):
            append(view, *args)
            view["derived_display"]["summary"] = "Unintended replacement"
        monkeypatch.setattr(dates, "_append_activity_rows", changed)
    else:
        render = RegistryReader._article_from_snapshot
        def changed(reader, snapshot):
            result = render(reader, snapshot)
            if result.get("is_activity"):
                result["summary"] = "Unintended reader replacement"
            return result
        monkeypatch.setattr(RegistryReader, "_article_from_snapshot", changed)
    with pytest.raises(RegistryInputError, match="non-date"):
        _approve(database, tmp_path, receipt)
    assert _reader(database, tmp_path).article("good") == before


def test_activity_only_cannot_mix_date_or_editorial_scopes(tmp_path):
    database = _seed(tmp_path)
    for extra in ({"publication_date_only": True}, {"date_fields": ["updated_date"]}, {"with_editorial_metadata": True}):
        with pytest.raises(ValueError):
            plan_owner_check(database, "article", "good", activity_dates_only=True, **extra)


@pytest.mark.parametrize(("literal", "start", "end", "precision"), [
    ("21–24 October 2026", "2026-10-21", "2026-10-24", "day"),
    ("October 2026", "2026-10", "2026-10", "month"),
    ("2026", "2026", "2026", "year"),
])
def test_activity_date_retains_literal_bounds_and_precision(tmp_path, literal, start, end, precision):
    database = _seed(tmp_path)
    manifest = _manifest(database)
    entry = manifest["observations"][0]
    value = entry["public_fields"]["activity_dates"][0]
    value["observed_at"] = start
    value["evidence"].update(event_start_date=start, event_end_date=end,
        date_precision=precision, date_text=literal, text=literal)
    body = entry["capture"]["text"].replace("2026-10-21", literal)
    raw = body.encode()
    entry["capture"].update(text=body, bytes_base64=base64.b64encode(raw).decode(),
        sha256=hashlib.sha256(raw).hexdigest())
    entry["field_evidence"]["activity_dates"]["excerpt"] = body
    staged = _ingest(database, tmp_path, manifest)
    _approve(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    event = _reader(database, tmp_path).article("good")["activity_dates"][0]
    assert (event["start_date"], event["end_date"], event["date_precision"], event["raw_date"]) == (start, end, precision, literal)


@pytest.mark.parametrize("source", ["native", "pdf", "linked"])
@pytest.mark.parametrize(("name", "valid"), [
    ("TNFD Climate Week 2026", True),
    ("TNFD Climate Week", False),
    ("TNFD Climate Week 2025 / 2026", False),
    ("TNFD Climate Week 2025", False),
])
def test_activity_date_can_use_only_the_literal_event_names_unique_year(tmp_path, source, name, valid):
    database = _seed(tmp_path, source)
    before = _item(_reader(database, tmp_path), source)
    manifest = _manifest(database, source)
    entry = manifest["observations"][0]
    value = entry["public_fields"]["activity_dates"][0]
    literal = "September 21 - September 24"
    value["observed_at"] = "2026-09-21"
    value["evidence"].update(event_name=name, event_start_date="2026-09-21", event_end_date="2026-09-24",
        date_text=literal, text=literal)
    body = entry["source_identity"]["excerpt"] + "\n" + name + "\n" + literal + "\nPublished: 2026-01-01"
    raw = body.encode()
    entry["capture"].update(text=body, bytes_base64=base64.b64encode(raw).decode(),
        sha256=hashlib.sha256(raw).hexdigest())
    entry["field_evidence"]["activity_dates"]["excerpt"] = body
    if not valid:
        with pytest.raises(RegistryInputError, match="activity date"):
            _ingest(database, tmp_path, manifest)
        assert _item(_reader(database, tmp_path), source) == before
        return
    staged = _ingest(database, tmp_path, manifest)
    _approve(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    reader = _reader(database, tmp_path)
    event = _item(reader, source)["activity_dates"][0]
    assert (event["start_date"], event["end_date"], event["date_precision"], event["raw_date"]) == (
        "2026-09-21", "2026-09-24", "day", literal)
    assert value["evidence"]["date_text"] == literal
    assert value["evidence"]["text"] == literal
    projected = next(item for item in reader.meetings(base_date="1900-01-01", page_size=100)["items"] if item["name"] == name)
    assert projected["raw_date"] == projected["date_evidence_text"] == literal


@pytest.mark.parametrize(("literal", "valid"), [
    ("28–30 February", False),
    ("February 28 - 30", False),
    ("28 to 30 February 2026", False),
    ("28 February", True),
    ("February 28, 2026", True),
    ("2026-02-28", True),
    ("28-February-2026", True),
])
def test_activity_date_does_not_collapse_an_impossible_range_to_a_single_day(tmp_path, literal, valid):
    database = _seed(tmp_path)
    reader = _reader(database, tmp_path)
    before = reader.article("good")
    manifest = _manifest(database)
    entry = manifest["observations"][0]
    value = entry["public_fields"]["activity_dates"][0]
    value["observed_at"] = "2026-02-28"
    value["evidence"].update(event_name="Climate risk conference 2026", event_start_date="2026-02-28",
        event_end_date="2026-02-28", date_text=literal, text=literal)
    body = entry["source_identity"]["excerpt"] + "\nClimate risk conference 2026\n" + literal
    raw = body.encode()
    entry["capture"].update(text=body, bytes_base64=base64.b64encode(raw).decode(),
        sha256=hashlib.sha256(raw).hexdigest())
    entry["field_evidence"]["activity_dates"]["excerpt"] = body
    if not valid:
        with pytest.raises(RegistryInputError, match="valid explicit end date"):
            _ingest(database, tmp_path, manifest)
        assert reader.article("good") == before
        assert not reader.meetings(base_date="1900-01-01", page_size=100)["items"]
        return
    staged = _ingest(database, tmp_path, manifest)
    _approve(database, tmp_path, _date_review(database, tmp_path, staged["candidate_sha256"]))
    event = reader.article("good")["activity_dates"][0]
    assert (event["start_date"], event["end_date"], event["raw_date"]) == ("2026-02-28", "2026-02-28", literal)


def test_activity_and_date_supplements_keep_the_approved_chain(tmp_path):
    database = _seed(tmp_path)
    first = _ingest(database, tmp_path, _manifest(database))
    _approve(database, tmp_path, _date_review(database, tmp_path, first["candidate_sha256"]))
    second = _ingest(database, tmp_path, _manifest(database, dates=("2026-10-28",)))
    _approve(database, tmp_path, _date_review(database, tmp_path, second["candidate_sha256"]))
    before = _reader(database, tmp_path).article("good")
    assert {event["start_date"] for event in before["activity_dates"]} == {"2026-10-21", "2026-10-28"}
    date = _ingest(database, tmp_path, _date_fields_manifest(database, fields=("updated_date",)))
    _approve(database, tmp_path, _date_review(database, tmp_path, date["candidate_sha256"]))
    after = _reader(database, tmp_path).article("good")
    assert after["activity_dates"] == before["activity_dates"]
    assert after["is_activity"] is True
    assert after["updated_date"] == "2025-11-15"
