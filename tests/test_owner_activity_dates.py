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
