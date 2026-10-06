"""Traceable synthetic fixtures: no facts copied from the visual reference."""
import copy
import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest
from pypdf import PdfReader

from climate_delivery.pdf import render_pdf
from climate_registry import range_reports


def range_fixture(*, long=False):
    url = "https://example.test/" + "long-segment-" * 30
    article = {
        "article_id": "fixture-article", "content_version_id": "fixture-version",
        "canonical_url": url, "title": "Fixture 气候风险 café μ → " + "long title " * 8,
        "publication_date": "2026-09-20", "information_date": None,
        "date_basis": "publication_date", "range_date": "2026-09-20", "collected_at": None,
        "publisher": "Alpha Institution",
        "summary": "Frozen summary 中文 — μ ≥ 2.",
        "content": ("Frozen full body 中文 café μ. " * (400 if long else 2)) + "FINAL BODY SENTINEL",
        "categories": ["Physical risk"], "keywords": ["pricing"],
        "source_observations": [], "provenance": {},
        "citations": [{"kind": "url", "url": url},
                      {"kind": "pdf", "filename": "fixture-source.pdf", "page": 7}],
    }
    frozen = {
        "schema_version": range_reports.SCHEMA_VERSION,
        "date_range": {"start": "2026-09-17", "end": "2026-09-30", "inclusive": True},
        "timezone": "UTC", "articles": [article],
        "unknown_publication_date_count": 1,
        "unknown_publication_date_article_ids": ["fixture-unknown-date"],
        "date_unknown_count": 1, "date_unknown_article_ids": ["fixture-unknown-date"],
        "meeting": {"status": "failed", "source": "snapshot", "snapshot_id": None,
                    "snapshot_sha256": None, "records": [], "coverage": {"status": "failed"}},
        "pdf_calendar": {"status": "included", "coverage": {"status": "complete"},
            "records": [{"kind": "deadline", "name": f"Deadline {i}", "raw_date": "2026-10-05",
                "end_date": "2026-10-05", "source_filename": "fixture-source.pdf", "page": 8,
                "source_document_sha256": "b" * 64, "publisher": "Alpha Institution",
                "relevance": "Frozen relevance 中文."} for i in range(70 if long else 2)]},
        "pdf_source_updates": [{"pdf_article_id": "fixture-pdf-only", "title": "PDF-only fixture",
            "summary": "PDF frozen summary.", "filename": "fixture-source.pdf", "page": 9,
            "document_sha256": "b" * 64, "content_sha256": "c" * 64,
            "url": "https://example.test/pdf-only", "publication_date_label": "文章发布日期未确认",
            "coverage_period": {"start": "2026-09-01", "end": "2026-09-30"},
            "citations": [{"kind": "pdf", "filename": "fixture-source.pdf", "page": 9}]}],
        "pdf_source_exclusion_counts": {"unknown_coverage": 2, "non_overlapping_coverage": 3},
    }
    frozen["executive_summary"] = range_reports._freeze_executive_summary(frozen)
    digest = range_reports._digest(frozen)
    return {**frozen, "snapshot_id": "range-report-" + digest[:24], "snapshot_sha256": digest,
            "created_at": "2026-10-01T08:00:00Z"}


def weekly_fixture(*, long=False):
    snapshot = range_fixture(long=long)
    article = snapshot["articles"][0]
    return {
        "schema_version": 1,
        "report": {"date": "2026-09-28", "title": "Fixture weekly climate report",
                   "sha256": "a" * 64, "sites": {"checked": 4, "succeeded": 3, "failed": 1}},
        "executive_summary": [article["summary"]],
        "monitoring_notes": ["Fixture source unavailable: reader failed."],
        "highlights": [{"pillar": "A", "title": article["title"], "summary": article["content"],
                        "url": article["canonical_url"]}],
        "article_provenance": {article["canonical_url"]: {
            "article_id": article["article_id"], "content_hash": "c" * 64,
            "source": article["publisher"], "publication_date": article["publication_date"],
            "citations": article["citations"]}},
        "original_links": [article["canonical_url"]],
        "key_dates": snapshot["pdf_calendar"]["records"],
        "coverage": [{"institution": "Alpha Institution", "status": "failed", "detail": "reader failed"}],
        "route_corrections": [{"source": "Fixture", "detail": "Frozen route correction"}],
        "glossary": [{"term": "Fixture term", "definition": "Frozen definition 中文"}],
    }


def text_of(path):
    reader = PdfReader(path)
    return reader, "\n".join(page.extract_text() or "" for page in reader.pages)


@pytest.mark.parametrize("pillars", [[], ["A", "A", "B", "B", "B"]])
def test_weekly_snapshot_preserves_exact_frozen_pillar_counts(tmp_path, pillars):
    from climate_delivery.templates.adapters import adapt_weekly_report
    summary = weekly_fixture()
    highlight = summary["highlights"][0]
    summary["highlights"] = [dict(highlight, pillar=pillar, url=f"https://example.test/update-{i}")
                             for i, pillar in enumerate(pillars)]
    original = copy.deepcopy(summary)
    counts = (("Pillar A updates", str(pillars.count("A"))),
              ("Pillar B updates", str(pillars.count("B"))))
    assert adapt_weekly_report(summary).statistics[-2:] == counts
    path = tmp_path / "weekly-pillar-counts.pdf"
    render_pdf(summary, path)
    _, text = text_of(path)
    normalized = " ".join(text.split())
    for label, count in counts:
        assert f"{label} {count}" in normalized
    assert summary == original


def _refresh_snapshot_identity(snapshot):
    frozen = {key: value for key, value in snapshot.items()
              if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
    snapshot["snapshot_sha256"] = range_reports._digest(frozen)
    snapshot["snapshot_id"] = "range-report-" + snapshot["snapshot_sha256"][:24]


@pytest.mark.parametrize("question,start,end", [
    ("查询2026-09-20到2026-10-03的项目和当前会议", "2026-09-20", "2026-10-03"),
    ("List climate updates for the last 14 days", "2026-09-21", "2026-10-04"),
    ("What changed in the last 4 weeks?", "2026-09-07", "2026-10-04"),
    ("查询过去2周的项目和当前会议", "2026-09-21", "2026-10-04"),
    ("查询最近两周的项目", "2026-09-21", "2026-10-04"),
])
def test_period_queries_use_report_route(question, start, end):
    from datetime import date
    route = range_reports.resolve_report_route(question, today=date(2026, 10, 4),
        typesafe_router=lambda _: None)
    assert (route.action, route.start_date, route.end_date) == ("generate", start, end)


@pytest.mark.parametrize("question,action", [
    ("Explain pricing implications between 2026-09-20 and 2026-10-03", "normal_chat"),
    ("List projects from 2026-10-03 to 2026-10-05", "clarify"),
    ("查询最近53周的项目", "clarify"),
    ("List projects on 2026-09-20", "clarify"),
])
def test_period_query_validation_preserves_ordinary_chat(question, action):
    from datetime import date
    assert range_reports.resolve_report_route(question, today=date(2026, 10, 4),
        typesafe_router=lambda _: None).action == action


def test_chat_report_lists_frozen_projects_and_complete_calendar():
    snapshot = range_fixture(long=True)
    snapshot["pdf_calendar"]["records"].append({"kind": "event", "name": "Broader window",
        "start_date": "2026-11", "end_date": "2026-11", "date_precision": "month",
        "source_url": "https://example.test/event_a_b?title=(Climate)|meeting"})
    _refresh_snapshot_identity(snapshot)
    original = copy.deepcopy(snapshot)
    text = range_reports.render_range_report_chat(snapshot, web_url="/reports/snapshot",
        pdf_url="/reports/snapshot.pdf")
    assert "# Climate Risk Intelligence Report" in text
    assert "**Calendar as at:** 2026-10-01 (UTC)" in text
    assert "### Alpha Institution" in text and "FINAL BODY SENTINEL" in text
    assert "## PDF Source Context - Publication Date Unconfirmed" in text
    assert "PDF-only fixture" in text and "not confirmed publications" in text
    assert "Meeting snapshot status: failed" in text
    assert "Meeting coverage: failed" in text
    assert "PDF calendar status: included. Coverage: complete" in text
    assert "| Date(s) | Event | Host | Relevance |\n| --- | --- | --- | --- |\n" in text
    assert all(f"| Deadline {i} · PDF import |" in text for i in range(70))
    assert "### Broader Windows - Day Unconfirmed" in text
    assert "2026-11 Precision: month (future)" in text
    assert "| Not provided | Not provided |" in text
    assert "event_a_b?title=%28Climate%29%7Cmeeting" in text
    assert "fixture-source.pdf" not in text
    assert "PDF import" in text and snapshot["articles"][0]["canonical_url"] in text
    assert "[Download PDF](/reports/snapshot.pdf)" in text
    assert "b" * 64 not in text
    assert snapshot == original


@pytest.mark.parametrize("route", ["range", "weekly"])
@pytest.mark.parametrize("run_date", ["2027-06-09", "2027-06-10", "2027-06-11"])
def test_unknown_meeting_end_has_no_invented_marker(tmp_path, route, run_date):
    from climate_monitor.meetings import process_batch, query_events
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report
    from test_issue136_meetings import _database, _candidate
    body = "Alpha Climate Summit 2027 starts June 10, 2027; end date TBC."
    database = _database(tmp_path, [body])
    candidate = _candidate(name="Alpha Climate Summit 2027", organizer=None,
        start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
        date_evidence="June 10, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None)
    process_batch(database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda _: {"events": [candidate]})
    record = query_events(database, base_date="2027-06-11", start_date="2027-06-11",
        end_date="2027-06-11", include_unknown=True, timezone_name="UTC")["records"][0]
    assert record["needs_confirmation"] == 1 and record["end_date"] is None
    snapshot, summary = range_fixture(), weekly_fixture()
    snapshot["created_at"] = run_date + "T08:00:00Z"
    snapshot["meeting"]["records"] = [record]
    snapshot["pdf_calendar"]["records"] = []
    _refresh_snapshot_identity(snapshot)
    summary["report"]["run_date"] = run_date
    summary["key_dates"] = [record]
    original = copy.deepcopy((snapshot, summary))
    model = adapt_range_report(snapshot) if route == "range" else adapt_weekly_report(summary)
    assert not any(f"({marker})" in model.key_dates[0][0] for marker in ("past", "current", "future"))
    path = tmp_path / "unknown-end.pdf"
    if route == "range":
        range_reports.render_range_report_pdf(snapshot, path)
    else:
        render_pdf(summary, path)
    _, text = text_of(path)
    html = range_reports.render_range_report_html(snapshot)
    assert "Enddateunconfirmed" in "".join(text.split()) and "End date unconfirmed" in html
    assert (snapshot, summary) == original


def test_supported_start_only_single_day_keeps_marker():
    from climate_delivery.templates.adapters import _key_date_rows
    record = {"name": "Verified single day", "start_date": "2027-06-10",
        "end_date": None, "date_precision": "day", "needs_confirmation": 0}
    assert "(past)" in _key_date_rows(record, "2027-06-11")[0][0]


@pytest.mark.parametrize("route", ["range", "weekly"])
def test_calendar_urls_and_verbatim_context_survive_both_outputs(tmp_path, route):
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report
    # Shape emitted by pdf_intake calendar extraction and retained by Registry.
    record = {"kind": "event", "name": "Calendar context sentinel", "start_date": "2026-10-05",
        "end_date": "2026-10-08", "date_precision": "day", "raw_date": "5–8 October 2026",
        "source_filename": "calendar.pdf", "page": 2, "source_document_sha256": "d" * 64,
        "source_urls": ["https://example.test/calendar-primary", "https://example.test/calendar-secondary"],
        "summary": "Frozen verbatim calendar context 中文 sentinel.", "summary_basis": "verbatim_pdf_row"}
    snapshot, summary = range_fixture(), weekly_fixture()
    snapshot["pdf_calendar"]["records"] = [record]
    _refresh_snapshot_identity(snapshot)
    summary["key_dates"] = [record]
    original = copy.deepcopy((snapshot, summary))
    model = adapt_range_report(snapshot) if route == "range" else adapt_weekly_report(summary)
    assert model.key_dates[0][2:4] == ("Not provided", "Not provided")
    path = tmp_path / "calendar-context.pdf"
    if route == "range":
        range_reports.render_range_report_pdf(snapshot, path)
    else:
        render_pdf(summary, path)
    _, text = text_of(path)
    html = range_reports.render_range_report_html(snapshot)
    for value in [record["source_urls"][1], record["summary"], "Verbatim context:", "PDF import"]:
        assert "".join(value.split()) in "".join(text.split()) and value in html
    annotations = [ref.get_object() for page in PdfReader(path).pages for ref in page.get("/Annots", [])]
    assert any(annotation.get("/A", {}).get("/URI") == record["source_urls"][0] for annotation in annotations)
    assert "calendar.pdf" not in text and "calendar.pdf" not in html
    assert (snapshot, summary) == original


def test_linked_pdf_update_retains_ids_in_metadata_and_displays_url(tmp_path):
    from climate_delivery.templates.adapters import adapt_range_report
    snapshot = range_fixture()
    snapshot["pdf_source_updates"][0]["core_article_id"] = "fixture-linked-core"
    _refresh_snapshot_identity(snapshot)
    original = copy.deepcopy(snapshot)
    path = tmp_path / "linked-pdf.pdf"
    range_reports.render_range_report_pdf(snapshot, path)
    _, text = text_of(path)
    html = range_reports.render_range_report_html(snapshot)
    update = adapt_range_report(snapshot).updates[-1]
    assert update.article_id == "fixture-linked-core"
    assert ("PDF article ID", "fixture-pdf-only") in update.metadata
    for value in ("fixture-linked-core", "fixture-pdf-only", "fixture-source.pdf"):
        assert value not in text and value not in html
    assert "https://example.test/pdf-only" in text and "PDF import" in text
    assert snapshot == original


def test_key_dates_preserve_real_meeting_query_fields_and_sources(tmp_path):
    from datetime import datetime, timezone
    from climate_monitor.meetings import process_batch, query_events
    from climate_delivery.templates.adapters import adapt_range_report
    from test_issue136_meetings import _database, _candidate
    body = ("Example presents World Climate Summit 2027 in New York, June 10–12, 2027. "
            "Registration closes May 1, 2027 at https://example.com/register. "
            "It includes a climate risk agenda.")
    database = _database(tmp_path, [body])
    result = process_batch(database, "batch", prompt_text="extract candidates", prompt_version="v1",
        provider="openai-api", model="gpt-test", extractor=lambda _: {"events": [_candidate()]},
        now=datetime(2026, 1, 5, tzinfo=timezone.utc))
    assert result["status"] == "succeeded"
    query = query_events(database, base_date="2027-06-11", timezone_name="UTC")
    record = query["records"][0]
    snapshot = range_fixture()
    snapshot["created_at"] = "2027-06-11T08:00:00Z"
    snapshot["meeting"]["status"] = "included"
    snapshot["meeting"]["records"] = [record]
    _refresh_snapshot_identity(snapshot)
    original = copy.deepcopy(snapshot)
    model = adapt_range_report(snapshot)
    assert model.key_dates[0][2:4] == (record["organizer"], record["relevance_reason"])
    path = tmp_path / "real-meeting.pdf"
    range_reports.render_range_report_pdf(snapshot, path)
    reader, text = text_of(path)
    compact_text = "".join(text.split())
    html = range_reports.render_range_report_html(snapshot)
    for value in [record["organizer"], record["relevance_reason"], record["start_date"],
                  record["end_date"], record["raw_time_text"]]:
        assert value in html and "".join(value.split()) in compact_text
    annotations = [ref.get_object() for page in reader.pages for ref in page.get("/Annots", [])]
    linked_urls = {annotation.get("/A", {}).get("/URI") for annotation in annotations}
    for source in record["sources"]:
        assert source["source_url"] in html
        assert source["source_url"] in linked_urls or source["source_url"] in text
    assert "(current)" in model.key_dates[0][0]
    assert snapshot == original


@pytest.mark.parametrize("route", ["range", "weekly"])
@pytest.mark.parametrize("standalone", [True, False])
@pytest.mark.parametrize("run_date, marker", [("2027-04-20", "future"),
    ("2027-05-01", "current"), ("2027-05-02", "past")])
def test_key_dates_preserve_real_independent_deadline(tmp_path, route, standalone, run_date, marker):
    from datetime import datetime, timezone
    from climate_monitor.meetings import process_batch, query_events
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report
    from test_issue136_meetings import _database, _candidate
    if standalone:
        body = "Example opens Climate Consultation Deadline; consultation responses are due May 1, 2027."
        candidate = _candidate(name="Climate Consultation Deadline", event_type="deadline",
            date_precision="unknown", start_date=None, end_date=None, raw_time_text=None,
            date_evidence=None, location=None, online_url=None, deadline_type="consultation",
            deadline_date="2027-05-01", deadline_evidence="May 1, 2027")
    else:
        body = ("Example presents World Climate Summit 2027 in New York, June 10–12, 2027. "
                "Registration closes May 1, 2027 at https://example.com/register. "
                "It includes a climate risk agenda.")
        candidate = _candidate()
    database = _database(tmp_path, [body])
    result = process_batch(database, "batch", prompt_text="extract candidates", prompt_version="v1",
        provider="openai-api", model="gpt-test", extractor=lambda _: {"events": [candidate]},
        now=datetime(2026, 1, 5, tzinfo=timezone.utc))
    assert result["status"] == "succeeded"
    query = query_events(database, include_deadlines=True, base_date="2027-05-01",
        start_date="2027-05-01", end_date="2027-05-01", timezone_name="UTC")
    record = query["records"][0]
    assert record["deadline_date"] == "2027-05-01"
    snapshot = range_fixture()
    snapshot["created_at"] = run_date + "T08:00:00Z"
    snapshot["meeting"]["status"] = "included"
    snapshot["meeting"]["records"] = [record]
    snapshot["pdf_calendar"]["records"] = []
    _refresh_snapshot_identity(snapshot)
    summary = weekly_fixture()
    summary["report"]["run_date"] = run_date
    summary["key_dates"] = [record]
    original = copy.deepcopy((snapshot, summary))
    model = adapt_range_report(snapshot) if route == "range" else adapt_weekly_report(summary)
    deadline_rows = [row for row in model.key_dates if "Deadline:" in row[1]]
    assert len(deadline_rows) == 1
    deadline = deadline_rows[0]
    assert "2027-05-01" in deadline[0] and f"({marker})" in deadline[0]
    assert f"Deadline: {record['deadline_type']}" in deadline[1]
    if standalone:
        assert len(model.key_dates) == 1
    else:
        assert len(model.key_dates) == 2
        event = model.key_dates[0]
        assert event[1] == record["name"]
        assert "2027-06-10" in event[0] and "2027-06-12" in event[0] and "(future)" in event[0]
    path = tmp_path / f"deadline-{route}.pdf"
    if route == "range":
        range_reports.render_range_report_pdf(snapshot, path)
    else:
        render_pdf(summary, path)
    _, text = text_of(path)
    html = range_reports.render_range_report_html(snapshot)
    annotations = [ref.get_object() for page in PdfReader(path).pages for ref in page.get("/Annots", [])]
    linked_urls = {annotation.get("/A", {}).get("/URI") for annotation in annotations}
    for row in model.key_dates:
        for value in row[:4]:
            assert "".join(value.split()) in "".join(text.split())
            assert value in html
        for source_url in row[4].splitlines():
            assert source_url in html
            assert source_url in linked_urls or source_url in text
    assert (snapshot, summary) == original


@pytest.mark.parametrize("route", ["range", "weekly"])
@pytest.mark.parametrize("run_date, marker", [("2026-10-04", "future"), ("2026-10-05", "current"),
    ("2026-10-06", "current"), ("2026-10-08", "current"), ("2026-10-09", "past")])
def test_key_dates_keep_full_calendar_interval_and_frozen_run_marker(tmp_path, route, run_date, marker):
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report, calendar_date_bounds
    record = {"kind": "event", "name": "Forum interval sentinel", "start_date": "2026-10-05",
        "end_date": "2026-10-08", "raw_date": "5–8 October 2026", "date_precision": "day",
        "source_filename": "calendar.pdf", "page": 2, "source_document_sha256": "d" * 64}
    snapshot = range_fixture()
    snapshot["created_at"] = run_date + "T08:00:00Z"
    snapshot["pdf_calendar"]["records"] = [record]
    _refresh_snapshot_identity(snapshot)
    summary = weekly_fixture()
    summary["report"]["run_date"] = run_date
    summary["key_dates"] = [record]
    original = copy.deepcopy((snapshot, summary))
    model = adapt_range_report(snapshot) if route == "range" else adapt_weekly_report(summary)
    path = tmp_path / f"interval-{route}.pdf"
    if route == "range":
        range_reports.render_range_report_pdf(snapshot, path)
    else:
        render_pdf(summary, path)
    _, text = text_of(path)
    compact_text = "".join(text.split())
    html = range_reports.render_range_report_html(snapshot)
    assert model.key_dates[0][0] == record["raw_date"]
    assert calendar_date_bounds(model.key_dates[0][0]) == ("2026-10-05", "2026-10-08", "day")
    assert record["raw_date"] in html and "".join(record["raw_date"].split()) in compact_text
    assert snapshot["pdf_calendar"]["records"][0]["end_date"] == "2026-10-08"
    assert (snapshot, summary) == original


@pytest.mark.parametrize("raw", ["5–8 October 2026 09:00–10:30 GMT+8", "5–8 October 2026\nAmerica/New_York"])
def test_imported_day_interval_with_clock_or_timezone_keeps_date_classification(tmp_path, raw):
    from climate_delivery.templates.adapters import adapt_range_report, calendar_date_bounds
    snapshot = range_fixture()
    record = dict(snapshot["pdf_calendar"]["records"][0], raw_date=raw,
        start_date="2026-10-05", end_date="2026-10-08", date_precision="day")
    snapshot["pdf_calendar"]["records"] = [record]
    _refresh_snapshot_identity(snapshot)
    report = adapt_range_report(snapshot)
    assert report.key_dates[0][0] == raw
    assert calendar_date_bounds(report.key_dates[0][0]) == ("2026-10-05", "2026-10-08", "day")
    path = tmp_path / "clock-calendar.pdf"
    range_reports.render_range_report_pdf(snapshot, path)
    _, text = text_of(path)
    assert "BROADER WINDOWS - DAY UNCONFIRMED" not in text
    assert "Broader Windows - Day Unconfirmed" not in range_reports.render_range_report_html(snapshot)


def test_merged_collected_meeting_preserves_original_pdf_date_text_and_snapshot():
    from climate_delivery.templates.adapters import adapt_range_report
    snapshot = range_fixture()
    native = {"event_id": "event-shared", "name": "Climate conference", "event_type": "conference",
        "organizer": "Example Institute", "date_precision": "day", "start_date": "2026-10-05", "end_date": "2026-10-08"}
    original = dict(native, event_id="pdf-event", raw_date="5–8 October 2026", publisher="Example Institute",
        source_document_sha256="b" * 64, source_urls=["https://example.test/event"])
    imported = dict(original, event_id="event-shared", canonical_event_id="event-shared", pdf_observations=[original])
    snapshot["meeting"]["records"] = [native]
    snapshot["pdf_calendar"]["records"] = [imported]
    _refresh_snapshot_identity(snapshot)
    frozen = copy.deepcopy(snapshot)
    report = adapt_range_report(snapshot)
    assert len(report.key_dates) == 1 and report.key_dates[0][0] == original["raw_date"]
    assert report.key_dates[0][4].count("PDF import") == 1
    page = range_reports.render_range_report_html(snapshot)
    assert page.count("Climate conference") == 1
    assert 'href="https://example.test/event"' in page
    calendar = page.split('<h2 id="key-dates">', 1)[1].split('<h2 id="updates">', 1)[0]
    assert original["raw_date"] in calendar and calendar.count("PDF import") == 1
    assert snapshot == frozen


@pytest.mark.parametrize("start, end, raw, precision, marker", [
    ("2026-10", "2026-11", "October–November 2026", "month", "current"),
    ("2026-Q4", "2026-Q4", "Q4 2026", "quarter", "current"),
    ("2025", "2025", "2025", "year", "past"),
    (None, None, "TBA", "unknown", None),
])
def test_key_date_preserves_precision_and_all_existing_source_shapes(start, end, raw, precision, marker):
    from climate_delivery.templates.adapters import _key_date
    record = {"name": "Precision fixture", "start_date": start, "end_date": end,
        "raw_date": raw, "date_precision": precision, "organizer": "Meeting host",
        "relevance_reason": "Frozen relevance", "source_filename": "calendar.pdf", "page": 3,
        "source_url": "https://example.test/top-level", "sources": [
            {"source_url": "https://example.test/first"}, {"source_url": "https://example.test/second"}]}
    row = _key_date(record, "2026-10-06")
    for value in [raw, f"Precision: {precision}", *[value for value in (start, end) if value]]:
        assert value in row[0]
    assert row[2:4] == ("Meeting host", "Frozen relevance")
    for value in ["calendar.pdf, page 3", "https://example.test/top-level",
                  "https://example.test/first", "https://example.test/second"]:
        assert value in row[4]
    if marker:
        assert f"({marker})" in row[0]
    else:
        assert not any(f"({status})" in row[0] for status in ("past", "current", "future"))


def test_two_adapters_are_read_only_and_preserve_frozen_facts():
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report
    snapshot, summary = range_fixture(), weekly_fixture()
    originals = copy.deepcopy((snapshot, summary))
    range_model = adapt_range_report(snapshot)
    weekly_model = adapt_weekly_report(summary)
    assert (snapshot, summary) == originals
    assert range_model.input_sha256 == snapshot["snapshot_sha256"]
    assert weekly_model.input_sha256 == summary["report"]["sha256"]
    assert range_model.updates[0].article_id == "fixture-article"
    assert range_model.updates[0].content_version == "fixture-version"
    assert weekly_model.updates[0].content_version is None
    assert weekly_model.updates[0].content_sha256 == "c" * 64
    assert weekly_model.statistics == (("Sites checked", "4"), ("Succeeded", "3"), ("Failed", "1"),
                                       ("Pillar A updates", "1"), ("Pillar B updates", "0"))
    assert "failed" in " ".join(range_model.date_notes)
    assert range_model.updates[-1].publication_date is None
    assert range_model.updates[-1].coverage_period == ("2026-09-01", "2026-09-30")
    with pytest.raises(FrozenInstanceError):
        range_model.title = "changed"


@pytest.mark.parametrize("route", ["range", "weekly"])
@pytest.mark.parametrize("long", [False, True])
def test_shared_template_content_links_unicode_and_pagination(tmp_path, route, long):
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report
    input_value = range_fixture(long=long) if route == "range" else weekly_fixture(long=long)
    frozen = copy.deepcopy(input_value)
    path = tmp_path / f"{route}.pdf"
    if route == "range":
        range_reports.render_range_report_pdf(input_value, path)
    else:
        render_pdf(input_value, path)
    assert input_value == frozen
    reader, text = text_of(path)
    body_text = "\n".join("\n".join(line for line in page.extract_text().splitlines()
        if not line.startswith("IAA CSC Climate Risk Intelligence Report") and not line.startswith("AI-assisted") and not line.startswith("Page "))
        for page in reader.pages)
    expected_body = (input_value["articles"][0]["content"] if route == "range"
                     else input_value["highlights"][0]["summary"])
    assert "".join(expected_body.split()) in "".join(body_text.split())
    assert len(reader.pages) >= 3
    for page_number, page in enumerate(reader.pages, 1):
        assert f"Page {page_number} of {len(reader.pages)}" in page.extract_text()
    for value in ["Contents", "Executive Summary", "Key Dates", "中文", "café μ", "FINAL BODY SENTINEL", "PDF import"]:
        assert value in text
    model = adapt_range_report(input_value) if route == "range" else adapt_weekly_report(input_value)
    assert model.updates[0].article_id == "fixture-article"
    for value in ("fixture-article", "fixture-source.pdf", "SHA-256", "Template ID"):
        assert value not in text
    outlines = []
    def walk(entries):
        for entry in entries:
            if isinstance(entry, list):
                walk(entry)
            else:
                assert 0 <= reader.get_destination_page_number(entry) < len(reader.pages)
                outlines.append(entry.title)
    walk(reader.outline)
    assert "Executive Summary" in outlines and "Key Dates" in outlines
    assert "Alpha Institution" in outlines
    annotations = [ref.get_object() for page in reader.pages for ref in page.get("/Annots", [])]
    assert any(item.get("/Dest") for item in annotations)
    assert any(item.get("/A", {}).get("/URI", "").startswith("https://example.test/") for item in annotations)
    if long:
        assert sum("RELEVANCE" in page.extract_text() for page in reader.pages) > 1
    if route == "range":
        html = range_reports.render_range_report_html(input_value)
        for value in ["FINAL BODY SENTINEL", "PDF frozen summary."]:
            assert value in html and value in text
        assert "article publication date unconfirmed" in text.lower()
    else:
        assert "Appendix A" in text and "Appendix B" in text and "Appendix C" in text
        assert "Sites checked" in text and "reader failed" in text


def test_core_identity_date_and_citations_fail_explicitly(tmp_path):
    from climate_delivery.templates.adapters import adapt_range_report, adapt_weekly_report
    snapshot = range_fixture()
    snapshot["articles"][0]["citations"] = []
    snapshot["articles"][0]["canonical_url"] = None
    frozen = {key: value for key, value in snapshot.items() if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
    snapshot["snapshot_sha256"] = range_reports._digest(frozen)
    snapshot["snapshot_id"] = "range-report-" + snapshot["snapshot_sha256"][:24]
    with pytest.raises(ValueError, match="citation"):
        adapt_range_report(snapshot)
    weekly = weekly_fixture()
    weekly["report"]["sha256"] = "invalid"
    with pytest.raises(ValueError, match="sha256"):
        adapt_weekly_report(weekly)


def test_legacy_links_reuse_original_pdf_bytes(tmp_path, monkeypatch):
    import api_server
    snapshot = range_fixture()
    root = tmp_path / "range"
    folder = root / snapshot["snapshot_id"]
    folder.mkdir(parents=True)
    (folder / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setattr(api_server, "RANGE_REPORT_DIR", root)
    for version in ("range-report-v1", "range-report-v2"):
        original = b"%PDF-1.4\noriginal archived " + version.encode()
        path = folder / f"{snapshot['snapshot_id']}-{version}.pdf"
        path.write_bytes(original)
        response = api_server.registry_range_report_pdf(snapshot["snapshot_id"], version)
        assert response.body == original
        assert path.read_bytes() == original


def test_render_identity_changes_without_changing_snapshot_or_old_artifact(tmp_path, monkeypatch):
    from climate_delivery import templates
    snapshot = range_fixture()
    before = copy.deepcopy(snapshot)
    old = range_reports.ensure_range_report_pdf(snapshot, tmp_path)
    original = old.read_bytes()
    monkeypatch.setattr(templates, "TEMPLATE_VERSION", "2")
    new = range_reports.ensure_range_report_pdf(snapshot, tmp_path)
    assert new != old and new.exists()
    assert old.read_bytes() == original
    assert snapshot == before


def test_weekly_independent_render_versions_have_distinct_manifests(tmp_path, monkeypatch):
    from climate_delivery import templates
    from climate_delivery.pdf import ensure_weekly_report_pdf
    summary = weekly_fixture()
    old = ensure_weekly_report_pdf(summary, tmp_path)
    original = old.read_bytes()
    manifest = json.loads((old.parent / "manifest.json").read_text())
    assert manifest["rendering"] == templates.rendering_metadata()
    assert manifest["pdf"]["sha256"] == hashlib.sha256(original).hexdigest()
    monkeypatch.setattr(templates, "TEMPLATE_VERSION", "2")
    new = ensure_weekly_report_pdf(summary, tmp_path)
    assert new != old and new.exists()
    assert old.read_bytes() == original
    assert json.loads((new.parent / "manifest.json").read_text())["rendering"]["template_version"] == "2"


@pytest.mark.parametrize("route", ["range", "weekly"])
def test_empty_inputs_omit_optional_appendices_and_state_missing_data(tmp_path, route):
    if route == "range":
        snapshot = range_fixture()
        snapshot["articles"] = []
        snapshot["pdf_source_updates"] = []
        snapshot["executive_summary"] = []
        frozen = {key: value for key, value in snapshot.items() if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
        snapshot["snapshot_sha256"] = range_reports._digest(frozen)
        snapshot["snapshot_id"] = "range-report-" + snapshot["snapshot_sha256"][:24]
        range_reports.render_range_report_pdf(snapshot, tmp_path / "empty.pdf")
    else:
        summary = weekly_fixture()
        summary["highlights"] = []
        summary["executive_summary"] = []
        for field in ("key_dates", "coverage", "glossary", "route_corrections"):
            summary.pop(field)
        render_pdf(summary, tmp_path / "empty.pdf")
    _, text = text_of(tmp_path / "empty.pdf")
    assert "No selected updates" in text
    assert "Appendix A" not in text and "Appendix C" not in text
    if route == "weekly":
        assert "Key dates were not provided" in text and "not provided in frozen input" in text


def test_unsupported_glyph_fails_without_installing_an_incomplete_pdf(tmp_path):
    from climate_delivery.errors import GenerationError
    summary = weekly_fixture()
    summary["executive_summary"] = ["Missing glyph " + chr(0x10FFFF)]
    with pytest.raises(GenerationError, match="U\\+10FFFF"):
        render_pdf(summary, tmp_path / "unsupported.pdf")
    assert not (tmp_path / "unsupported.pdf").exists()


def test_real_cookie_consent_summary_is_preserved_and_rendered_as_cookie_marker(tmp_path):
    summary = (
        "The 2026 Progress Report Skip to content Cookies 🍪 This site uses cookies that need "
        "consent. Accept all Accept selected Reject all Open main menu Our work Get involved "
        "Resources About Ceres Donate Search Search Issues we work on Climate change Advancing "
        "business solutions for a cleaner, more resilient economy. Nature and biodiversity loss "
        "Restoring and preserving our natural ecosystems."
    )
    snapshot = range_fixture()
    snapshot["articles"][0]["summary"] = summary
    for point in snapshot["executive_summary"]:
        if point["kind"] == "registry_article":
            point["text"] = summary
    _refresh_snapshot_identity(snapshot)

    output = tmp_path / "cookie-source.pdf"
    range_reports.render_range_report_pdf(snapshot, output)
    _, pdf_text = text_of(output)
    html = range_reports.render_range_report_html(snapshot)

    assert "[cookie]" in pdf_text and "🍪" not in pdf_text
    assert "🍪" in html
    assert snapshot["articles"][0]["summary"] == summary


def test_large_cover_title_and_single_table_row_split_without_text_loss(tmp_path):
    summary = weekly_fixture()
    summary["report"]["title"] = ("Long cover 中文 🌡️ title " * 140) + "FINAL COVER SENTINEL"
    summary["glossary"] = [{"term": "Oversized row", "definition": ("Long table 中文 definition " * 600) + "FINAL TABLE SENTINEL"}]
    output = tmp_path / "oversized.pdf"
    render_pdf(summary, output)
    reader, text = text_of(output)
    assert "FINAL COVER SENTINEL" in text and "FINAL TABLE SENTINEL" in text
    assert "🌡" in text
    assert sum("DEFINITION" in page.extract_text() for page in reader.pages) >= 2


def test_range_html_and_pdf_preserve_caveats_optional_tables_and_date_basis(tmp_path):
    from climate_delivery.templates.adapters import adapt_range_report
    snapshot = range_fixture()
    snapshot["articles"][0]["caveat"] = "Frozen caveat 中文."
    snapshot["articles"][0]["provenance"] = {"publication_date": {"basis": "fixture-publisher-date", "selected": {"date": "2026-09-20"}}}
    for field in ("coverage", "route_corrections", "glossary"):
        snapshot[field] = weekly_fixture()[field]
    snapshot["cross_cutting_watch"] = ["Frozen watch text 中文."]
    frozen = {key: value for key, value in snapshot.items() if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
    snapshot["snapshot_sha256"] = range_reports._digest(frozen)
    snapshot["snapshot_id"] = "range-report-" + snapshot["snapshot_sha256"][:24]
    range_reports.render_range_report_pdf(snapshot, tmp_path / "optional.pdf")
    _, pdf = text_of(tmp_path / "optional.pdf")
    html = range_reports.render_range_report_html(snapshot)
    for value in ("Frozen caveat 中文.", "Frozen relevance 中文.", "Frozen definition 中文", "reader failed", "Frozen route correction", "Frozen watch text 中文."):
        assert value in html and value in pdf
    model = adapt_range_report(snapshot)
    assert ("Date basis", "publication date 2026-09-20") in model.updates[0].metadata
    assert "fixture-publisher-date" in html
    assert model.updates[-1].article_id == "fixture-pdf-only"
    assert "fixture-publisher-date" not in pdf and "fixture-pdf-only" not in pdf


def test_rendering_does_not_call_network_database_llm_or_mail(tmp_path, monkeypatch):
    import sqlite3
    import smtplib
    import urllib.request
    import httpx
    def forbidden(*args, **kwargs):
        pytest.fail("renderer attempted an external side effect")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    render_pdf(weekly_fixture(), tmp_path / "weekly.pdf")
    range_reports.render_range_report_pdf(range_fixture(), tmp_path / "range.pdf")


@pytest.mark.parametrize("legacy_manifest", [False, True])
def test_retained_email_retry_uses_original_pdf_after_template_change(tmp_path, monkeypatch, legacy_manifest):
    from climate_delivery import templates
    from climate_delivery.errors import DeliveryError
    from climate_delivery.pipeline import run_delivery
    from test_climate_delivery_pipeline import configure_env, delivery_report
    from test_climate_delivery_email import config_file
    configure_env(monkeypatch)
    report = delivery_report(tmp_path)
    output, state = tmp_path / "output", tmp_path / "state"
    config = config_file(tmp_path)
    attached_hashes = []
    reject = True
    class MockSMTP:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def starttls(self, **kwargs): pass
        def login(self, *args): pass
        def send_message(self, message):
            attachment = next(message.iter_attachments()).get_payload(decode=True)
            attached_hashes.append(hashlib.sha256(attachment).hexdigest())
            return {str(message["To"]): (550, b"fixture rejection")} if reject else {}
    with pytest.raises(DeliveryError):
        run_delivery(report, output, state, config, smtp_factory=MockSMTP)
    manifest_path = next(output.rglob("manifest.json"))
    original_manifest = json.loads(manifest_path.read_text())
    if legacy_manifest:
        original_manifest.pop("rendering")
        manifest_path.write_text(json.dumps(original_manifest), encoding="utf-8")
    pdf_path = manifest_path.parent / original_manifest["artifacts"]["pdf"]["path"]
    original_pdf, original_summary = pdf_path.read_bytes(), (manifest_path.parent / "summary.json").read_bytes()
    original_hash = hashlib.sha256(original_pdf).hexdigest()
    assert original_manifest["delivery"]["status"] == "failed"
    monkeypatch.setattr(templates, "TEMPLATE_VERSION", "2")
    monkeypatch.setattr("climate_delivery.pipeline.render_pdf", lambda *a, **k: pytest.fail("retry rendered a new attachment"))
    reject = False
    result = run_delivery(report, output, state, config, smtp_factory=MockSMTP)
    assert result["status"] == "sent"
    assert set(attached_hashes) == {original_hash}
    assert pdf_path.read_bytes() == original_pdf
    assert (manifest_path.parent / "summary.json").read_bytes() == original_summary
    final_manifest = json.loads(manifest_path.read_text())
    assert final_manifest.get("rendering") == original_manifest.get("rendering")
    retained = json.loads(next(state.glob("*.json")).read_text())
    assert retained["artifacts"]["pdf_sha256"] == original_hash


def test_unknown_weekly_statistics_remain_unknown_in_valid_repeat_archive(tmp_path):
    from climate_delivery.pipeline import run_delivery
    from test_climate_delivery_pipeline import delivery_report, DELIVERY_REPORT
    report = delivery_report(tmp_path, text=DELIVERY_REPORT.replace(
        "Sites checked: **3**, succeeded: **2**, failed: **1**",
        "Sites checked: **unknown**, succeeded: **unknown**, failed: **unknown**"))
    output, state = tmp_path / "output", tmp_path / "state"
    first = run_delivery(report, output, state, None, artifact_only=True)
    assert run_delivery(report, output, state, None, artifact_only=True) == first


def test_prior_shared_template_download_link_survives_new_default(tmp_path, monkeypatch):
    import api_server
    from climate_delivery import templates
    snapshot = range_fixture()
    root = tmp_path / "range"
    old = range_reports.ensure_range_report_pdf(snapshot, root)
    old_bytes = old.read_bytes()
    (old.parent / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    old_version = templates.render_identity()
    monkeypatch.setattr(api_server, "RANGE_REPORT_DIR", root)
    monkeypatch.setattr(templates, "TEMPLATE_VERSION", "2")
    monkeypatch.setattr(api_server, "RENDERER_VERSION", templates.render_identity())
    new = range_reports.ensure_range_report_pdf(snapshot, root)
    assert new != old
    assert api_server.registry_range_report_pdf(snapshot["snapshot_id"], old_version).body == old_bytes
    assert old_version in api_server.registry_range_report(snapshot["snapshot_id"], old_version).body.decode()
    assert old.read_bytes() == old_bytes
