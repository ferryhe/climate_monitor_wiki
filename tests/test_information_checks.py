"""Checks belong to a source observation, never merely to its URL."""
import hashlib
import json
import sqlite3
import sys
from types import SimpleNamespace
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from climate_monitor.meetings import EXTRACTION_FIELDS
from climate_monitor.meeting_fields import merge_meeting_observations
from climate_registry.information_checks import check_targets, evaluate, latest_checks, run_checks
from climate_registry.read_api import RegistryReader
from climate_registry.schema import apply_migrations


def _database(tmp_path):
    database = tmp_path / "registry.sqlite3"
    with sqlite3.connect(database) as connection:
        apply_migrations(connection, target_version=16)
        connection.execute("INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,extracted_text_sha256,document_json,imported_at) VALUES(?,?,'report.pdf','application/pdf',0,?,'{}','2026-10-04')",
            ("a" * 64, str(tmp_path / "report.pdf"), "b" * 64))
        for index, name in enumerate(("Climate risk conference", "Adaptation forum"), 1):
            item = {"event_id": f"pdf-event-{index}", "occurrence_id": f"meeting-{index}", "name": name,
                "publisher": "Example Institute", "relevance": "Climate risk and insurance.", "kind": "event", "raw_date": "5–8 October 2026",
                "date_precision": "day", "start_date": "2026-10-05", "end_date": "2026-10-08",
                "date_evidence": "5–8 October 2026", "summary": name, "raw_text": name,
                "page": 1, "source_urls": ["https://example.org/events"], "source_document_sha256": "a" * 64}
            connection.execute("INSERT INTO pdf_intake_calendar_items VALUES(?,?,?,1,?,'event',?,'day',?,?,?, ?,NULL,?)",
                (item["occurrence_id"], item["event_id"], "a" * 64, name, item["raw_date"], item["start_date"],
                    item["end_date"], name, "c" * 64, json.dumps(item)))
        connection.execute("INSERT INTO pdf_intake_articles(article_id,canonical_url,title,imported_at) VALUES('pdf-article','https://example.org/events','Climate publication','2026-10-04')")
        for index, summary in enumerate(("Emissions decreased.", "Emissions increased."), 1):
            item = {"occurrence_id": f"article-{index}", "anchor_text": "Climate publication", "summary": summary,
                "summary_basis": "verbatim_pdf_paragraph", "raw_url": "https://example.org/events", "page": index,
                "source_document_sha256": "a" * 64}
            connection.execute("INSERT INTO pdf_intake_article_occurrences(occurrence_id,article_id,source_document_sha256,page,raw_url,content_sha256,page_sha256,occurrence_json) VALUES(?,?,?,?,?,?,?,?)",
                (item["occurrence_id"], "pdf-article", "a" * 64, index, item["raw_url"], "d" * 64, "e" * 64, json.dumps(item)))
    return database


def _record(occurrence_id, url, body=None):
    body = body or "# Climate risk conference\nExample Institute hosts Climate risk conference on 5–8 October 2026.\n09:00–10:30 GMT+8 at Singapore."
    return {"article_id": occurrence_id, "requested_url": url, "final_url": url, "status": "ok",
        "content": body, "content_ref": None, "content_hash": hashlib.sha256(body.encode()).hexdigest()}


def _judgment(kind, fields, body):
    from climate_registry.information_checks import source_fields
    comparisons = {key: {"status": "supported"} for key, value in source_fields(kind, fields).items() if value is not None}
    if kind == "articles":
        comparisons["title"] = {"status": "supported"}
        comparisons["summary"] = {"status": "conflict" if "increased" in fields["summary"] else "supported"}
        return {"comparisons": comparisons}
    candidate = {key: None for key in EXTRACTION_FIELDS}
    candidate.update(name="Climate risk conference", organizer="Example Institute", event_type="conference", status="scheduled",
        date_precision="day", start_date="2026-10-05", end_date="2026-10-08", raw_time_text="09:00–10:30",
        timezone="GMT+8", location="Singapore", date_evidence="Example Institute hosts Climate risk conference on 5–8 October 2026.")
    if fields["name"] != candidate["name"]:
        comparisons["name"] = {"status": "missing"}
    return {"comparisons": comparisons, "website_candidate": candidate}


def test_meeting_and_article_runs_are_independent_resumable_and_immutable(tmp_path):
    database = _database(tmp_path)
    options = dict(database=database, backup_dir=tmp_path / "backups", fetcher=_record, verifier=_judgment)
    first = run_checks(kind="meetings", limit=1, **options)
    assert first["status"] == "pending" and first["completed_count"] == 1
    resumed = run_checks(kind="meetings", resume_run_id=first["run_id"], **options)
    assert resumed["status"] == "partial" and resumed["completed_count"] == 2
    article_run = run_checks(kind="articles", **options)
    assert article_run["status"] == "partial" and article_run["completed_count"] == 2
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    calendar = reader.pdf_calendar_items()["items"]
    by_id = {item["occurrence_id"]: item for item in calendar}
    assert by_id["meeting-1"]["verification_status"] == "verified"
    assert by_id["meeting-1"]["origin"] == "pdf_import"
    assert by_id["meeting-2"]["access_status"] == "accessible"
    assert by_id["meeting-2"]["verification_status"] == "partial"
    collected = reader.meetings(base_date="2026-10-04")["items"]
    verified = next(item for item in collected if item["verification_status"] == "verified")
    assert (verified["raw_time_text"], verified["event_timezone"], verified["location"]) == ("09:00–10:30", "GMT+8", "Singapore")
    assert verified["pdf_event_id"] == "pdf-event-1" and verified["event_id"].startswith("event-")
    assert verified["relevance_reason"] == "Climate risk and insurance."
    article = reader.pdf_article("pdf-article")
    assert {item["verification_status"] for item in article["occurrences"]} == {"verified", "conflict"}
    retry = run_checks(kind="meetings", retry_run_id=first["run_id"], **options)
    assert retry["item_count"] == retry["completed_count"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM meeting_check_attempts").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM article_check_attempts").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM articles").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM climate_events").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM acquisition_batches").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE meeting_check_attempts SET verification_status='verified'")
    assert list((tmp_path / "backups").glob("*.bak"))


def test_accessible_homepage_and_conflicting_date_never_promote(tmp_path):
    database = _database(tmp_path)
    target = check_targets(database, "meetings", {"meeting-1"})[0]
    homepage = _record("meeting-1", target["source_url"], "Welcome to Example Institute.")
    packet = evaluate(target, "meetings", homepage, _judgment)
    assert packet["access_status"] == "accessible" and packet["verification_status"] == "partial"
    assert packet["error"] and packet["canonical_event_id"] is None
    bad = _record("meeting-1", target["source_url"])
    bad["content_hash"] = "f" * 64
    packet = evaluate(target, "meetings", bad, _judgment)
    assert packet["access_status"] == "failed" and packet["verification_status"] == "unchecked"
    def conflicting(kind, fields, body):
        value = _judgment(kind, fields, body)
        value["comparisons"]["start_date"]["status"] = "conflict"
        return value
    packet = evaluate(target, "meetings", _record("meeting-1", target["source_url"]), conflicting)
    assert packet["verification_status"] == "conflict"
    missing_target = deepcopy(target)
    missing_target["fields"].update(location="Singapore", event_timezone="GMT+8")
    def missing_details(kind, fields, body):
        value = _judgment(kind, fields, body)
        value["website_candidate"].update(location=None, timezone=None)
        value["comparisons"]["location"] = {"status": "missing"}
        value["comparisons"]["event_timezone"] = {"status": "missing"}
        return value
    packet = evaluate(missing_target, "meetings", _record("meeting-1", target["source_url"]), missing_details)
    assert packet["verification_status"] == "partial"


def test_changed_source_revision_does_not_inherit_verification(tmp_path):
    database = _database(tmp_path)
    run_checks(database, kind="meetings", backup_dir=tmp_path / "backups", occurrence_ids={"meeting-1"}, fetcher=_record, verifier=_judgment)
    item = next(item for item in RegistryReader(database, repository_root=tmp_path / "application").pdf_calendar_items()["items"]
        if item["occurrence_id"] == "meeting-1")
    assert item["verification_status"] == "verified"
    changed = dict(item, publisher="Another Institute", organizer="Another Institute")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        assert latest_checks(connection, "meetings", changed)["verification_status"] == "unchecked"
    native = {"event_id": item["canonical_event_id"], "name": "Climate risk conference", "source_urls": ["https://example.org/events"]}
    imported = dict(item, event_id=item["canonical_event_id"], pdf_observations=[deepcopy(item)])
    result = merge_meeting_observations([native, imported])
    assert len(result) == 1 and len(result[0]["pdf_observations"]) == 1
    other = dict(imported, canonical_event_id="event-another-occurrence", occurrence_id="other")
    assert len(merge_meeting_observations([native, other])) == 2


@pytest.mark.parametrize("separate_databases", [False, True])
def test_verified_reschedule_keeps_native_identity_across_all_readers(tmp_path, separate_databases):
    from test_issue136_meetings import _database as native_database, _candidate
    from climate_monitor.meetings import process_batch, query_events
    from climate_registry.range_reports import (
        freeze_range_report, render_range_report_html, render_range_report_chat, ensure_range_report_pdf,
    )
    from climate_delivery.templates.adapters import adapt_range_report
    from pypdf import PdfReader
    url = "https://example.com/summit"
    bodies = [f"Example hosts World Climate Summit 2027 on June 10–12, 2027. Register at {url}.",
        f"Example confirms World Climate Summit 2027 is rescheduled for July 1–2, 2027. Register at {url}."]
    native_dir = tmp_path / "native"
    native_dir.mkdir()
    native = native_database(native_dir, bodies)
    def candidate(body):
        moved = "rescheduled" in body
        return _candidate(start_date="2027-07-01" if moved else "2027-06-10",
            end_date="2027-07-02" if moved else "2027-06-12",
            date_evidence="rescheduled for July 1–2, 2027" if moved else "June 10–12, 2027",
            raw_time_text=None, location=None, online_url=url, deadline_type=None,
            deadline_date=None, deadline_evidence=None)
    process_batch(native, "batch", prompt_text="events", prompt_version="v1", provider="test", model="test",
        extractor=lambda request: {"events": [candidate(request["article_body"])]})
    native_items = query_events(native, base_date="2027-01-01")["records"]
    assert len(native_items) == 1 and native_items[0]["record_version"] == 2
    identity = native_items[0]["event_id"]
    database = tmp_path / "writer.sqlite3" if separate_databases else native
    item = {"event_id": "pdf-event-moved", "occurrence_id": "pdf-moved", "name": "World Climate Summit 2027",
        "publisher": "Example", "kind": "event", "raw_date": "1–2 July 2027", "date_precision": "day",
        "start_date": "2027-07-01", "end_date": "2027-07-02", "summary": "World Climate Summit 2027",
        "relevance": "Climate risk agenda", "source_urls": [url], "source_document_sha256": "a" * 64}
    with sqlite3.connect(database) as connection:
        apply_migrations(connection)
        connection.execute("INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,size_bytes,extracted_text_sha256,document_json,imported_at) VALUES(?,?,'report.pdf','application/pdf',0,?,'{}','2027-01-01')",
            ("a" * 64, str(tmp_path / "report.pdf"), "b" * 64))
        connection.execute("INSERT INTO pdf_intake_calendar_items VALUES(?,?,?,1,?,'event',?,'day',?,?,?, ?,NULL,?)",
            (item["occurrence_id"], item["event_id"], "a" * 64, item["name"], item["raw_date"], item["start_date"],
                item["end_date"], item["summary"], "c" * 64, json.dumps(item)))
    def verify(kind, fields, body):
        return {"comparisons": {key: {"status": "supported"} for key in fields}, "website_candidate": candidate(body)}
    checked = run_checks(database, kind="meetings", backup_dir=tmp_path / "backups",
        fetcher=lambda occurrence, source: _record(occurrence, source, bodies[1]), verifier=verify)
    assert checked["status"] == "complete"
    pdf_reader = RegistryReader(database, repository_root=tmp_path / "application")
    imported = pdf_reader.pdf_calendar_items()["items"]
    assert (imported[0]["canonical_event_id"] == identity) is (not separate_databases)
    public = RegistryReader(native, repository_root=tmp_path / "application")
    source_copy = deepcopy(imported)
    with sqlite3.connect(database) as connection:
        packet_before = connection.execute("SELECT packet_json FROM meeting_check_attempts").fetchone()[0]
    listed = public.meetings(base_date="2027-01-01", additional_calendar_items=imported)
    assert listed["pagination"]["total"] == 1
    assert listed["items"][0]["event_id"] == identity
    snapshot = freeze_range_report(public, tmp_path / "reports", start_date="2027-06-20", end_date="2027-07-03",
        generated_at=datetime(2027, 1, 1, tzinfo=timezone.utc), pdf_overlay_reader=pdf_reader,
        overlay_manifest={"pdf_occurrence_ids": [], "pdf_calendar_occurrence_ids": ["pdf-moved"]})
    assert snapshot["pdf_calendar"]["records"][0]["canonical_event_id"] == identity
    model = adapt_range_report(snapshot)
    assert len(model.key_dates) == 1 and model.key_dates[0][0] == item["raw_date"]
    assert model.key_dates[0][4].count("PDF import") == 1
    html = render_range_report_html(snapshot)
    assert html.count("<strong>World Climate Summit 2027") == 1
    chat = render_range_report_chat(snapshot, web_url="/report", pdf_url="/report/pdf")
    assert sum(item["name"] in line for line in chat.splitlines() if line.startswith("| ")) == 1
    text = "".join("".join(page.extract_text().split()) for page in PdfReader(ensure_range_report_pdf(snapshot, tmp_path / "reports")).pages)
    assert text.count("".join(item["name"].split())) == 1
    assert imported == source_copy
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT packet_json FROM meeting_check_attempts").fetchone()[0] == packet_before


def test_calendar_only_import_and_check_refresh_publish_without_articles(tmp_path, monkeypatch):
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle
    from climate_monitor.pdf_intake import import_pdf_reports
    from climate_registry.pdf_pipeline import PdfIntakePipeline, enqueue_pdf_batch, load_projection_manifest, load_active_projection
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "calendar.pdf"
    style = getSampleStyleSheet()["BodyText"]
    rows = [["DATE(S)", "EVENT", "HOST", "RELEVANCE"], [Paragraph(value, style) for value in (
        "5–8 October 2026<br/>EVENT", '<link href="https://example.org/events">Climate risk conference</link>',
        "Example Institute", "Climate risk and insurance.")]]
    table = Table(rows, colWidths=[110, 160, 100, 130])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .5, "black"), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    SimpleDocTemplate(str(source)).build([Paragraph("Key Dates", style), table])
    bundle = import_pdf_reports([source])
    assert len(bundle["calendar_items"]) == 1 and bundle["articles"] == []
    database = tmp_path / "registry.sqlite3"
    with sqlite3.connect(database) as connection:
        apply_migrations(connection)
    connection.close()
    queue, runtime = tmp_path / "queue", tmp_path / "runtime"
    queue.mkdir()
    status = enqueue_pdf_batch(queue, bundle, repository_root=tmp_path / "application")
    def reload_chat(generation):
        pending = json.loads((queue / "pending.json").read_text())
        assert pending["generation_id"] == generation
        (queue / "active.json").write_text(json.dumps(pending))
    pipeline = PdfIntakePipeline(queue, database, tmp_path / "backups", runtime, reload_chat,
        repository_root=tmp_path / "application")
    ready = pipeline.process(status["batch_id"])
    assert ready["chat_ready"], ready
    generation, metadata = load_active_projection(runtime, queue / "active.json")
    manifest = load_projection_manifest(generation, metadata)
    assert manifest["pdf_occurrence_ids"] == [] and len(manifest["pdf_calendar_occurrence_ids"]) == 1
    assert (generation / "registry-meetings.md").is_file()
    old_snapshot = (runtime / "registry-snapshots" / f"{generation.name}.sqlite3").read_bytes()
    checked = run_checks(database, kind="meetings", backup_dir=tmp_path / "backups", fetcher=_record, verifier=_judgment)
    refreshed = pipeline.refresh_checks(checked["run_id"])
    assert refreshed["status"] == "chat_ready" and refreshed["generation_id"] != generation.name
    assert (runtime / "registry-snapshots" / f"{generation.name}.sqlite3").read_bytes() == old_snapshot
    new_generation, metadata = load_active_projection(runtime, queue / "active.json")
    assert "Verification: verified" in (new_generation / "registry-meetings.md").read_text()
    assert "Collected timezone: GMT+8" in (new_generation / "registry-meetings.md").read_text()
    from climate_registry.range_reports import freeze_range_report
    public = tmp_path / "public.sqlite3"
    with sqlite3.connect(public) as connection:
        apply_migrations(connection)
    connection.close()
    public_reader = RegistryReader(public, repository_root=tmp_path / "application")
    overlay_reader = RegistryReader(metadata["pdf_registry_snapshot"], repository_root=tmp_path / "application")
    snapshot = freeze_range_report(public_reader, tmp_path / "reports", start_date="2026-09-21", end_date="2026-10-04",
        generated_at=datetime(2026, 10, 4, tzinfo=timezone.utc), pdf_overlay_reader=overlay_reader,
        overlay_manifest=load_projection_manifest(new_generation, metadata))
    assert len(snapshot["pdf_calendar"]["records"]) == 1
    assert snapshot["pdf_calendar"]["records"][0]["relevance_reason"] == "Climate risk and insurance."
    assert snapshot["pdf_calendar"]["records"][0]["event_timezone"] == "GMT+8"


def test_public_meeting_route_uses_only_activated_runtime_observations(tmp_path, monkeypatch):
    import api_server
    runtime = _database(tmp_path)
    public = tmp_path / "public.sqlite3"
    with sqlite3.connect(public) as connection:
        apply_migrations(connection)
    connection.close()
    public_reader = RegistryReader(public, repository_root=tmp_path / "application")
    runtime_reader = RegistryReader(runtime, repository_root=tmp_path / "application")
    monkeypatch.setattr(api_server, "_registry_reader", lambda: public_reader)
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, runtime_reader,
        {"pdf_occurrence_ids": ["article-1"], "pdf_calendar_occurrence_ids": ["meeting-1"]}))
    payload = api_server.registry_meetings(base_date="2026-10-04")
    assert payload["pagination"]["total"] == 1
    assert payload["items"][0]["occurrence_id"] == "meeting-1"
    assert api_server.registry_pdf_articles()["pagination"]["total"] == 1
    assert len(api_server.registry_pdf_article("pdf-article")["occurrences"]) == 1


@pytest.mark.parametrize("zone", ["GMT+8", "ET", "America/New_York", "Asia/Manila", "America/Argentina/Buenos_Aires"])
def test_typesafe_deadline_evidence_is_separate_and_missing_fields_remain_partial(monkeypatch, zone):
    from climate_monitor.meeting_fields import pdf_meeting_fields
    from climate_registry.information_checks import typesafe_verify, source_fields, source_revision
    event_line = "Example Institute hosts Climate risk conference on 5–8 October 2026."
    deadline_line = "Registration for Climate risk conference closes on 2 October 2026."
    body = "# Climate risk conference\n" + event_line + "\n" + deadline_line + f"\n09:00–10:30 {zone}.\nVenue: Singapore."
    class Choice:
        def __init__(self, *, instructions, criteria):
            self.criteria = criteria
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def system_one(self, *, state, questions):
            selections = {"name": "Climate risk conference", "organizer": "Example Institute",
                "date_evidence": event_line, "deadline_evidence": deadline_line, "raw_time_text": "09:00–10:30", "timezone": zone}
            answers = {}
            for key, question in questions.items():
                if key.startswith("claim_"): label = "supported"
                elif key.startswith("pick_"):
                    literal = selections.get(key[5:])
                    label = next((choice for choice, value in question.criteria.items() if value == literal), "none")
                else: label = {"event_type": "conference", "status": "scheduled", "deadline_type": "registration"}[key]
                answers[key] = SimpleNamespace(choice=label)
            return SimpleNamespace(choices=answers, model="test-verifier")
    monkeypatch.setitem(sys.modules, "typesafe_sdk", SimpleNamespace(Choice=Choice, TypeSafeClient=Client))
    monkeypatch.setenv("TYPESAFE_API_KEY", "fixture-key")
    item = pdf_meeting_fields({"name": "Climate risk conference", "publisher": "Example Institute", "date_precision": "day",
        "raw_date": f"5–8 October 2026 09:00–10:30 {zone}",
        "start_date": "2026-10-05", "end_date": "2026-10-08", "deadline_type": "registration", "deadline_date": "2026-10-02"}
    )
    assert item["event_timezone"] == zone
    assert item["raw_time_text"] == "09:00–10:30"
    target = {"occurrence_id": "source", "source_url": "https://example.org/event", "fields": source_fields("meetings", item),
        "source_revision_sha256": source_revision("meetings", item)}
    packet = evaluate(target, "meetings", _record("source", target["source_url"], body), typesafe_verify)
    assert packet["verification_status"] == "verified", packet["error"]
    assert packet["website_candidate"]["timezone"] == zone
    assert packet["website_candidate"]["deadline_evidence"] == deadline_line
    pure = pdf_meeting_fields(dict(item, kind="deadline", name="Registration deadline", raw_date="2 October 2026",
        start_date="2026-10-02", end_date="2026-10-02"))
    assert pure["start_date"] is None and pure["deadline_date"] == "2026-10-02" and pure["event_type"] == "deadline"


@pytest.mark.parametrize("raw, zone, clock", [
    ("5–8 October 2026 09:00–10:30 GMT+8", "GMT+8", "09:00–10:30"),
    ("5–8 October 2026 America/New_York", "America/New_York", None),
    ("5–8 October 2026\nET", "ET", None),
])
def test_pdf_clock_and_timezone_suffix_preserves_original_and_normalized_dates(raw, zone, clock):
    from climate_monitor.meeting_fields import pdf_meeting_fields
    item = {"raw_date": raw, "date_precision": "unknown", "start_date": None, "end_date": None}
    normalized = pdf_meeting_fields(item)
    assert normalized["raw_date"] == item["raw_date"]
    assert (normalized["start_date"], normalized["end_date"], normalized["date_precision"]) == ("2026-10-05", "2026-10-08", "day")
    assert normalized["event_timezone"] == zone and normalized["raw_time_text"] == clock


def test_collected_future_deadline_survives_past_pdf_event_in_reader_and_report(tmp_path, monkeypatch):
    from climate_registry.range_reports import _calendar_end_date, _pdf_calendar_payload
    from climate_delivery.templates.adapters import _key_date_rows
    database = _database(tmp_path)
    reader = RegistryReader(database, repository_root=tmp_path / "application")
    item = reader.pdf_calendar_items()["items"][0]
    item.update(raw_date="1–2 October 2026", start_date="2026-10-01", end_date="2026-10-02",
        verification_status="verified", canonical_event_id="event-late-deadline")
    candidate = {key: None for key in EXTRACTION_FIELDS}
    candidate.update(name=item["name"], event_type="conference", organizer=item["organizer"], status="scheduled",
        date_precision="day", start_date="2026-10-01", end_date="2026-10-02",
        deadline_type="expert_review", deadline_date="2026-10-05")
    item["collected_candidate"] = candidate
    monkeypatch.setattr(reader, "pdf_calendar_items", lambda **kwargs: {"items": [item], "pagination": {"pages": 1}})
    payload = _pdf_calendar_payload(reader, base_date="2026-10-04")
    assert len(payload["records"]) == 1
    projected = payload["records"][0]
    assert _calendar_end_date(projected).isoformat() == "2026-10-05"
    rows = _key_date_rows(projected, "2026-10-04")
    assert any("Deadline: expert_review" in row[1] and "2026-10-05" in row[0] for row in rows)
    listed = reader.meetings(base_date="2026-10-04")
    assert listed["pagination"]["total"] == 1 and listed["items"][0]["deadline_date"] == "2026-10-05"
    assert item.get("deadline_date") is None and item["end_date"] == "2026-10-02"
    native = dict(candidate, event_id=item["canonical_event_id"], deadline_date=None, deadline_type=None,
        relevance_reason="Website analysis.")
    combined = reader.meetings(base_date="2026-10-04", additional_events=[native])["items"][0]
    assert combined["raw_date"] == item["raw_date"]
    assert combined["deadline_date"] == "2026-10-05"
    assert combined["relevance"] == item["relevance_reason"]
    assert combined["pdf_observations"][0]["raw_date"] == item["raw_date"]
