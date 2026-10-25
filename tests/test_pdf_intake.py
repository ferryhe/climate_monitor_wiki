from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import sys
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen.canvas import Canvas

from climate_monitor.pdf_intake import (
    _extract_calendar_items,
    _is_calendar_date_line,
    _parse_calendar_date,
    _reported_publication_date,
    recover_calendar_fields,
    _typesafe_classify,
    _input_pdfs,
    import_pdf_reports,
    main,
)
from climate_registry.persistent import initialize_registry
from climate_registry.schema import apply_migrations


def _report_pdf(path, calendar_date="4–5 Sep 2026", *, include_article=False):
    output = BytesIO()
    canvas = Canvas(output, pagesize=letter)
    canvas.setTitle("Climate intake source metadata")
    canvas.setAuthor("Test report author")
    canvas.setSubject("Original PDF metadata retention")
    canvas.setFont("Helvetica", 11)
    for y, line in zip(
        (760, 744, 728, 712),
        (
            "Climate Risk Intelligence Report Edition 9",
            "REPORTING PERIOD 1–2 September 2026",
            "DATE OF RUN 3 September 2026",
            "42 organisations monitored",
        ),
    ):
        canvas.drawString(50, y, line)
    canvas.showPage()
    canvas.drawString(50, 760, "Executive Summary")
    canvas.drawString(50, 740, "Climate summary preserved exactly from the report.")
    if include_article:
        canvas.drawString(50, 700, "Climate publication")
        canvas.drawString(50, 682, "IN WINDOW 2 SEP 2026 REPORT")
        canvas.drawString(50, 664, "Independent publication summary.")
        canvas.linkURL("https://example.org/climate-publication", (48, 696, 220, 712), relative=0)
    canvas.showPage()
    for y, line in zip(
        (760, 740, 720, 700, 680, 660),
        (
            "Key Dates",
            "DATE(S) EVENT HOST RELEVANCE",
            calendar_date,
            "EVENT",
            "Climate risk conference",
            "A conference relevant to insurers.",
        ),
    ):
        canvas.drawString(50, y, line)
    canvas.linkURL("https://example.org/climate-risk-conference/", (48, 676, 220, 694), relative=0)
    canvas.save()
    path.write_bytes(output.getvalue())


def _report_pdf_with_two_articles(path):
    output = BytesIO()
    canvas = Canvas(output, pagesize=letter)
    canvas.drawString(50, 760, "Climate Risk Intelligence Report")
    canvas.showPage()
    canvas.setFont("Helvetica", 11)
    for y, line in (
        (760, "UPDATES"),
        (740, "First climate risk study"),
        (720, "IN WINDOW 14 SEP 2026 REPORT"),
        (700, "First article summary stays inside its own record."),
        (680, "Source: First publisher"),
        (660, "47"),
        (640, "Second climate risk study"),
        (620, "IN WINDOW 16 SEP 2026 REPORT"),
        (600, "Second article summary stays inside its own record."),
        (580, "Source: Second publisher"),
        (560, "48"),
        (540, "Unrelated following note."),
    ):
        canvas.drawString(50, y, line)
    canvas.linkURL("https://example.org/first-study", (48, 738, 220, 754), relative=0)
    canvas.linkURL("https://example.org/second-study", (48, 638, 230, 654), relative=0)
    canvas.save()
    path.write_bytes(output.getvalue())


def _report_pdf_with_window_publication_date(path):
    output = BytesIO()
    canvas = Canvas(output, pagesize=letter)
    canvas.drawString(50, 760, "Climate Risk Intelligence Report")
    canvas.showPage()
    for y, line in (
        (760, "UPDATES"),
        (740, "AlliedOffsets Corporate Buyers Spotlight: July"),
        (720, "1–10 AUG 4 AUG 2026 MARKET DATA"),
        (700, "Voluntary market activity increased across the reporting period."),
    ):
        canvas.drawString(50, y, line)
    canvas.linkURL(
        "https://blog.alliedoffsets.com/corporate-buyers-spotlight",
        (48, 738, 310, 754), relative=0,
    )
    canvas.save()
    path.write_bytes(output.getvalue())


def _report_pdf_with_empty_anchor_links(path, first_date="4 September 2026"):
    output = BytesIO()
    canvas = Canvas(output, pagesize=letter)
    canvas.drawString(50, 760, "Climate Risk Intelligence Report")
    canvas.showPage()
    for y, line in zip(
        (760, 740, 720, 700, 680, 660, 630, 610, 590, 570),
        (
            "Key Dates",
            "DATE(S) EVENT HOST RELEVANCE",
            first_date,
            "EVENT",
            "Conference Alpha",
            "Hosted by A",
            "6 September 2026",
            "EVENT",
            "Conference Beta",
            "Hosted by B",
        ),
    ):
        canvas.drawString(50, y, line)
    # Each annotation covers the title text, but not the text fragment's origin at x=50.
    canvas.linkURL("https://example.org/alpha", (90, 676, 135, 692), relative=0)
    canvas.linkURL("https://example.org/beta", (90, 586, 135, 602), relative=0)
    canvas.setFont("Helvetica-Bold", 17)
    canvas.drawString(50, 540, "Following organization section")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(50, 515, "Publisher contact")
    canvas.linkURL("https://example.org/publisher", (90, 496, 135, 512), relative=0)
    canvas.drawString(50, 16, "IAA CSC Climate Risk Intelligence Report Page 3 of 3")
    canvas.save()
    path.write_bytes(output.getvalue())


def test_pdf_adapter_preserves_provenance_links_text_and_calendar_dates(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "climate-report.pdf"
    _report_pdf(path)

    bundle = import_pdf_reports([path])

    assert bundle["schema_version"] == "climate-pdf-intake.v1"
    assert bundle["typesafe"]["status"] == "not_configured"
    assert len(bundle["documents"]) == 1
    document = bundle["documents"][0]
    assert document["source"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert base64.b64decode(document["source"]["original_pdf_base64"]) == path.read_bytes()
    assert document["source"]["pdf_metadata"]["/Title"] == "Climate intake source metadata"
    assert document["source"]["pdf_metadata"]["/Author"] == "Test report author"
    assert document["source"]["pdf_metadata"]["/Subject"] == "Original PDF metadata retention"
    assert document["source"]["pdf_metadata"]["/CreationDate"].startswith("D:")
    assert document["source"]["pdf_metadata"]["/ModDate"].startswith("D:")
    assert document["source"]["pdf_created_at"]
    assert document["source"]["pdf_modified_at"]
    assert document["date_of_run"] == "2026-09-03"
    assert (document["period_start"], document["period_end"]) == ("2026-09-01", "2026-09-02")
    assert "Climate summary preserved exactly" in document["executive_summary"]
    assert "Climate summary preserved exactly" in document["pages"][1]["text"]

    event = bundle["calendar_items"][0]
    assert event["name"] == "Climate risk conference"
    assert event["raw_date"] == "4–5 Sep 2026"
    assert (event["start_date"], event["end_date"], event["date_precision"]) == (
        "2026-09-04", "2026-09-05", "day",
    )
    assert "A conference relevant to insurers" in event["summary"]
    assert event["summary_basis"] == "verbatim_pdf_row"
    later_document = deepcopy(document)
    later_document["source"]["sha256"] = "a" * 64
    later_event = _extract_calendar_items(later_document)[0]
    assert later_event["event_id"] == event["event_id"]
    assert later_event["occurrence_id"] != event["occurrence_id"]
    assert bundle["articles"] == []  # Calendar observations belong only to meetings.
    assert event["source_urls"] == ["https://example.org/climate-risk-conference/"]
    assert event["content_sha256"]


def test_calendar_location_requires_explicit_row_text_and_stays_with_its_occurrence():
    document = {
        "source": {"sha256": "a" * 64},
        "pages": [{"page": 11, "links": [
            {"anchor_text": "Climate Conference", "url": "https://example.org/shared", "rect": []},
            {"anchor_text": "Climate Conference", "url": "https://example.org/shared", "rect": []},
        ], "text": "\n".join((
            "Key Dates", "DATE(S) EVENT HOST RELEVANCE",
            "27 Oct 2026", "EVENT", "Climate Conference", "Location: Paris, France", "Host A", "Relevance A",
            "28 Oct 2026", "EVENT", "Climate Conference", "Location: London", "Host B", "Relevance B",
            "29 Oct 2026", "EVENT", "Online", "Host C", "Relevance C",
            "30 Oct 2026", "EVENT", "Climate Webinar", "Online link: https://example.org/room", "Host D", "Relevance D",
        ))}],
    }

    items = _extract_calendar_items(document)

    assert [item["location"] for item in items] == ["Paris, France", "London", "Online", None]
    assert [item["online_url"] for item in items] == [None, None, None, "https://example.org/room"]
    assert len({item["occurrence_id"] for item in items}) == 4
    assert items[0]["source_urls"] == items[1]["source_urls"] == ["https://example.org/shared"]
    assert all(item["page"] == 11 and item["source_document_sha256"] == "a" * 64 for item in items)


@pytest.mark.parametrize(("event_cell", "stored_event_text", "expected_location", "expected_url"), [
    ("Climate Conference\nAddress: Conference Centre\n12 Main Street, Paris\nFrance\nOnline link: https://example.org/events/climate-\nconference-2026/registration",
     "Climate Conference\nAddress: Conference Centre\n12 Main Street, Paris\nFrance\nOnline link: https://example.org/events/climate-\nconference-2026/registration",
     "Conference Centre\n12 Main Street, Paris\nFrance",
     "https://example.org/events/climate-conference-2026/registration"),
    ("Climate Conference\nLocation:\nRoom 4, North Wing\n12 Main Street, Paris",
     "Climate Conference\nLocation:\nRoom 4, North Wing\n12 Main Street, Paris",
     "Room 4, North Wing\n12 Main Street, Paris", None),
    ("Climate Conference\nLocation:", "Climate Conference\nLocation:", None, None),
    ("Climate Conference\nAddress: Paris", "Different Conference", None, None),
])
def test_legacy_calendar_recovery_uses_bounded_raw_event_cell(
    monkeypatch, event_cell, stored_event_text, expected_location, expected_url,
):
    raw_pdf = b"legacy calendar fixture"
    bounds = [(0, 0, 100, 10), (100, 0, 250, 10), (250, 0, 320, 10), (320, 0, 400, 10)]
    values = ["27 Oct 2026 EVENT", "Climate Conference", "Host A", "Relevance A"]

    class Row:
        cells = bounds
        bbox = (0, 10, 400, 20)

    class Table:
        rows = [Row(), Row()]

        @staticmethod
        def extract():
            return [["DATE(S)", "EVENT", "HOST", "RELEVANCE"], values]

    class Page:
        hyperlinks = []

        @staticmethod
        def find_tables():
            return [Table()]

        @staticmethod
        def crop(box):
            column = next(index for index, cell in enumerate(bounds) if cell[0] <= box[0] < cell[2])
            text = event_cell if column == 1 else values[column]
            return SimpleNamespace(extract_text=lambda **_kwargs: text)

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(sys.modules, "pdfplumber", SimpleNamespace(open=lambda _raw: Pdf()))
    from climate_monitor.pdf_intake import _calendar_table_rows
    _calendar_table_rows.cache_clear()
    item = {
        "occurrence_id": "legacy-occurrence", "event_id": "legacy-event",
        "source_document_sha256": hashlib.sha256(raw_pdf).hexdigest(), "page": 1,
        "kind": "event", "raw_date": "27 Oct 2026",
        "source_urls": [],
        "raw_text": f"27 Oct 2026\nEVENT\n{stored_event_text}\nHost A\nRelevance A",
    }

    recovered = recover_calendar_fields(raw_pdf, [item])[0]

    assert recovered.get("location") == expected_location
    assert recovered.get("online_url") == expected_url
    if stored_event_text.startswith("Climate Conference"):
        assert recovered["name"] == "Climate Conference"
    assert recovered["page"] == 1
    assert recovered["source_document_sha256"] == hashlib.sha256(raw_pdf).hexdigest()


def test_legacy_calendar_recovery_disambiguates_same_title_rows_by_cell_evidence(monkeypatch):
    raw_pdf = b"duplicate calendar rows"
    bounds = [(0, 0, 100, 10), (100, 0, 250, 10), (250, 0, 320, 10), (320, 0, 400, 10)]
    values = [
        ["27 Oct 2026 EVENT", "Climate Conference", "Host A", "Relevance A"],
        ["27 Oct 2026 EVENT", "Climate Conference", "Host A", "Relevance A"],
    ]
    event_cells = [
        "Climate Conference\nAddress: Conference Centre\n12 Main Street, Paris\nFrance",
        "Climate Conference\nAddress: Conference Centre\n88 River Road, London\nUK",
    ]

    class Row:
        def __init__(self, index):
            self.cells = bounds
            self.bbox = (0, 10 + index * 20, 400, 30 + index * 20)

    class Table:
        rows = [Row(0), Row(0), Row(1)]

        @staticmethod
        def extract():
            return [["DATE(S)", "EVENT", "HOST", "RELEVANCE"], *values]

    class Page:
        hyperlinks = []

        @staticmethod
        def find_tables():
            return [Table()]

        @staticmethod
        def crop(box):
            index = 0 if box[1] < 30 else 1
            column = next(col for col, cell in enumerate(bounds) if cell[0] <= box[0] < cell[2])
            text = event_cells[index] if column == 1 else values[index][column]
            return SimpleNamespace(extract_text=lambda **_kwargs: text)

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(sys.modules, "pdfplumber", SimpleNamespace(open=lambda _raw: Pdf()))
    from climate_monitor.pdf_intake import _calendar_table_rows
    _calendar_table_rows.cache_clear()
    digest = hashlib.sha256(raw_pdf).hexdigest()
    items = [
        {
            "occurrence_id": f"occurrence-{index}", "event_id": "same-event-id",
            "source_document_sha256": digest, "page": 1, "kind": "event",
            "raw_date": "27 Oct 2026", "source_urls": ["https://example.org/shared"],
            "raw_text": f"27 Oct 2026\nEVENT\n{event_cells[index]}\nHost A\nRelevance A",
        }
        for index in range(2)
    ]

    recovered = recover_calendar_fields(raw_pdf, items)

    assert [item["location"] for item in recovered] == [
        "Conference Centre\n12 Main Street, Paris\nFrance",
        "Conference Centre\n88 River Road, London\nUK",
    ]
    assert [item["occurrence_id"] for item in recovered] == ["occurrence-0", "occurrence-1"]
    assert [item["event_id"] for item in recovered] == ["same-event-id", "same-event-id"]
    assert all(item["source_document_sha256"] == digest and item["page"] == 1 for item in recovered)


def test_collected_pdf_location_keeps_source_url_of_matching_verified_candidate():
    from climate_monitor.meeting_fields import collected_pdf_meeting

    candidate = {"name": "Climate Conference", "location": "Paris", "timezone": None}
    item = {
        "event_id": "pdf-event", "canonical_event_id": "web-event", "location": "London",
        "source_document_sha256": "a" * 64, "page": 3, "verification_status": "verified",
        "collected_candidate": candidate,
        "checks": [
            {"source_url": "https://example.org/old", "verification_status": "partial",
             "website_candidate": {"name": "Climate Conference", "location": "Rome", "timezone": None}},
            {"source_url": "https://example.org/current", "verification_status": "verified",
             "website_candidate": candidate},
        ],
    }

    collected = collected_pdf_meeting(item)

    assert collected["location"] == "Paris"
    assert collected["collected_candidate_source_url"] == "https://example.org/current"
    assert collected["pdf_observations"][0]["location"] == "London"


@pytest.mark.parametrize("canonical_event_id", [None, ...])
def test_collected_pdf_meeting_falls_back_to_pdf_identity_without_public_native_link(canonical_event_id):
    from climate_monitor.meeting_fields import collected_pdf_meeting

    candidate = {"name": "IAIS Annual Conference 2027", "timezone": None}
    item = {
        "event_id": "event-pdf", "verification_status": "verified",
        "collected_candidate": candidate,
    }
    if canonical_event_id is not ...:
        item["canonical_event_id"] = canonical_event_id

    collected = collected_pdf_meeting(item)

    assert collected["event_id"] == "event-pdf"
    assert collected["pdf_event_id"] == "event-pdf"
    assert collected["name"] == "IAIS Annual Conference 2027"
    assert collected["pdf_observations"] == [item]
    if canonical_event_id is ...:
        assert "canonical_event_id" not in collected
    else:
        assert collected["canonical_event_id"] is None


def test_article_context_is_bounded_to_its_record_and_keeps_publication_date(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "two-articles.pdf"
    _report_pdf_with_two_articles(path)

    bundle = import_pdf_reports([path])
    occurrences = {
        article["canonical_url"]: article["occurrences"][0]
        for article in bundle["articles"]
    }
    first = occurrences["https://example.org/first-study"]
    second = occurrences["https://example.org/second-study"]

    assert "First article summary" in first["summary"]
    assert "Second article summary" not in first["summary"]
    assert "Second article summary" in second["summary"]
    assert "Unrelated following note" not in second["summary"]
    assert first["publication_date_evidence"] == "14 SEP 2026"
    assert first["publication_date"] == "2026-09-14"
    assert second["publication_date"] == "2026-09-16"


def test_article_publication_date_from_window_date_kind_record(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "window-date-kind.pdf"
    _report_pdf_with_window_publication_date(path)

    bundle = import_pdf_reports([path])
    article = next(
        item for item in bundle["articles"]
        if item["canonical_url"] == "https://blog.alliedoffsets.com/corporate-buyers-spotlight"
    )
    occurrence = article["occurrences"][0]

    assert "1–10 AUG 4 AUG 2026 MARKET DATA" in occurrence["summary"]
    assert occurrence["publication_date_evidence"] == "4 AUG 2026"
    assert occurrence["publication_date"] == "2026-08-04"


def test_empty_anchor_links_match_only_their_calendar_title_geometry(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    original_path = tmp_path / "original.pdf"
    revised_path = tmp_path / "revised.pdf"
    _report_pdf_with_empty_anchor_links(original_path)
    _report_pdf_with_empty_anchor_links(revised_path, first_date="5 September 2026")

    original = import_pdf_reports([original_path])
    revised = import_pdf_reports([revised_path])
    events = original["calendar_items"]
    revised_first = revised["calendar_items"][0]

    assert [event["source_urls"] for event in events] == [
        ["https://example.org/alpha"], ["https://example.org/beta"],
    ]
    assert [event["name"] for event in events] == ["Conference Alpha", "Conference Beta"]
    assert all("Following organization section" not in event["raw_text"] for event in events)
    assert all("Publisher contact" not in event["raw_text"] for event in events)
    assert all("Page 3 of 3" not in event["raw_text"] for event in events)
    source_links = original["documents"][0]["pages"][1]["links"]
    publisher_link = next(link for link in source_links if link["url"] == "https://example.org/publisher")
    assert publisher_link["rect"] == [90.0, 496.0, 135.0, 512.0]
    publisher_article = next(
        article for article in original["articles"]
        if article["canonical_url"] == "https://example.org/publisher"
    )
    assert publisher_article["occurrences"][0]["raw_url"] == "https://example.org/publisher"
    assert events[0]["event_id"] == revised_first["event_id"]
    assert events[0]["occurrence_id"] != revised_first["occurrence_id"]
    assert (events[0]["raw_date"], revised_first["raw_date"]) == (
        "4 September 2026", "5 September 2026",
    )


def test_calendar_link_with_year_mismatch_matches_title_geometry():
    def extract(date):
        return _extract_calendar_items({
            "source": {"sha256": "a" * 64},
            "pages": [{
                "page": 2,
                "text": f"Key Dates\nDATE(S) EVENT HOST RELEVANCE\n{date}\nEVENT\nConference Alpha\nHosted by A.",
                "links": [{
                    "url": "https://example.org/conference-alpha",
                    "anchor_text": "Conference Alpha 2026",
                    "page": 2,
                    "rect": [90, 676, 135, 692],
                }],
                "_text_fragments": [{
                    "x": 50, "right": 160, "y": 680, "text": "Conference Alpha",
                }],
            }],
        })[0]

    original = extract("4 September 2026")
    revised = extract("5 September 2026")

    assert original["name"] == "Conference Alpha 2026"
    assert original["source_urls"] == ["https://example.org/conference-alpha"]
    assert original["event_id"] == revised["event_id"]
    assert original["occurrence_id"] != revised["occurrence_id"]


def test_year_on_its_own_line_stays_part_of_calendar_title():
    document = {
        "source": {"sha256": "a" * 64},
        "pages": [{
            "page": 4,
            "text": (
                "Key Dates\nDATE(S) EVENT HOST RELEVANCE\n7–8 Oct 2026\nE V E N T\n"
                "Chief Investment Officer Forum\n2026\nGeneva Association\n"
                "Insurer CIO forum."
            ),
            "links": [{
                "url": "https://example.org/cio-forum-2026",
                "anchor_text": "Chief Investment Officer Forum 2026",
                "page": 4,
                "rect": [90, 676, 135, 692],
            }],
        }],
    }

    event = _extract_calendar_items(document)[0]

    assert event["name"] == "Chief Investment Officer Forum 2026"
    assert event["kind"] == "event"
    assert event["raw_kind"] == "E V E N T"
    assert event["source_urls"] == ["https://example.org/cio-forum-2026"]
    assert "Geneva Association" in event["summary"]


def test_unresolvable_empty_anchor_link_stays_page_level():
    document = {
        "source": {"sha256": "a" * 64},
        "pages": [{
            "page": 3,
            "text": "Key Dates\nDATE(S) EVENT HOST RELEVANCE\n4 September 2026\nEVENT\nClimate conference\nFor insurers.",
            "links": [{
                "url": "https://example.org/unknown",
                "anchor_text": "",
                "page": 3,
                "rect": [],
            }],
        }],
    }

    event = _extract_calendar_items(document)[0]

    assert event["source_urls"] == []
    assert document["pages"][0]["links"][0] == {
        "url": "https://example.org/unknown", "anchor_text": "", "page": 3, "rect": [],
    }


def test_calendar_event_identity_uses_canonical_source_urls():
    def extract(url, *, date="4 September 2026", title="Climate conference"):
        return _extract_calendar_items({
            "source": {"sha256": "a" * 64},
            "pages": [{
                "page": 8,
                "text": f"Key Dates\nDATE(S) EVENT HOST RELEVANCE\n{date}\nEVENT\n{title}\nFor insurers.",
                "links": [{"url": url, "anchor_text": title}],
            }],
        })[0]

    tracked = extract("https://example.org/event/?utm_source=pdf")
    equivalent = extract("https://EXAMPLE.ORG/event")
    rescheduled = extract("https://example.org/event", date="5 September 2026")
    renamed = extract("https://example.org/event", title="Climate risk conference")

    assert tracked["event_id"] == equivalent["event_id"]
    assert tracked["event_id"] == rescheduled["event_id"]
    assert tracked["event_id"] != renamed["event_id"]
    assert tracked["occurrence_id"] != rescheduled["occurrence_id"]
    assert (tracked["raw_date"], rescheduled["raw_date"]) == ("4 September 2026", "5 September 2026")
    assert tracked["source_urls"] == ["https://example.org/event/?utm_source=pdf"]
    assert equivalent["source_urls"] == ["https://EXAMPLE.ORG/event"]


def test_pdf_date_parser_keeps_partial_and_unfamiliar_dates():
    assert _parse_calendar_date("November 2026.") == {
        "start_date": "2026-11", "end_date": None, "date_precision": "month",
    }
    assert _parse_calendar_date("Q4 2026") == {
        "start_date": "2026-Q4", "end_date": None, "date_precision": "quarter",
    }
    assert _parse_calendar_date("Nov 2026 – Jan 2027") == {
        "start_date": "2026-11", "end_date": "2027-01", "date_precision": "month",
    }
    assert _parse_calendar_date("date to be announced") == {
        "start_date": None, "end_date": None, "date_precision": "unknown",
    }
    assert _is_calendar_date_line("November 2026.")
    assert _is_calendar_date_line("Date to be announced")
    assert not _is_calendar_date_line("Climate Week NYC 2026 UNEP FI /")
    assert not _is_calendar_date_line("Training Week 2026")
    assert _reported_publication_date("IN WINDOW September 2026 WORKING PAPER") == (
        "September 2026", "2026-09",
    )


@pytest.mark.parametrize(("text", "evidence", "normalized"), [
    ("UPDATED 4 AUG PUBLISHED 31 JUL 2026 COMMENTARY", "31 JUL 2026", "2026-07-31"),
    ("Emissions disclosures, published on 29 July 2026.", "29 July 2026", "2026-07-29"),
    ("Publication date: September 2026", "September 2026", "2026-09"),
])
def test_publication_date_uses_labeled_bounded_record_text(text, evidence, normalized):
    assert _reported_publication_date(text, "Article title") == (evidence, normalized)


def test_publication_date_does_not_use_unlabeled_dates_in_record_text():
    assert _reported_publication_date(
        "Published no new material; the consultation opened 29 July 2026.", "Article title",
    ) == (None, None)
    assert _reported_publication_date(
        "1–10 AUG 4 AUG 2026 INTERNAL NOTE", "Article title",
    ) == (None, None)


def test_typesafe_classification_is_a_non_blocking_routing_hint(monkeypatch):
    class FakeChoice:
        def __init__(self, *, instructions, criteria):
            assert "routing suggestion" in instructions
            assert set(criteria) == {"article", "event", "landing_page", "other"}

    class FakeClient:
        def __init__(self, *, api_key, timeout):
            assert api_key == "test-key"
            assert timeout == 20.0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def system_one(self, state, questions):
            assert state["items"][0]["anchor_text"] == "Climate risk conference"
            return SimpleNamespace(choices={key: SimpleNamespace(choice="event") for key in questions})

    monkeypatch.setitem(sys.modules, "typesafe_sdk", SimpleNamespace(Choice=FakeChoice, TypeSafeClient=FakeClient))
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    records = [{
        "canonical_url": "https://example.org/climate-risk-conference",
        "title": "Climate risk conference",
        "occurrences": [{"summary": "4–5 Sep 2026 Climate risk conference for insurers."}],
        "type_safe_classification": None,
    }]

    result = _typesafe_classify(records)

    assert result == {"status": "complete", "classified": 1, "failed_batches": 0}
    assert records[0]["type_safe_classification"] == {"provider": "typesafe", "label": "event"}


def test_typesafe_client_failure_keeps_records_unclassified(monkeypatch):
    class FakeChoice:
        def __init__(self, **_kwargs):
            pass

    class FailingClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            raise RuntimeError("temporary provider failure")

        def __exit__(self, *_args):
            return None

    monkeypatch.setitem(
        sys.modules,
        "typesafe_sdk",
        SimpleNamespace(Choice=FakeChoice, TypeSafeClient=FailingClient),
    )
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    records = [{
        "canonical_url": "https://example.org/report",
        "title": "Climate report",
        "occurrences": [{"summary": "A climate risk report."}],
        "type_safe_classification": None,
    }]

    result = _typesafe_classify(records)

    assert result == {"status": "partial", "classified": 0, "failed_batches": 1}
    assert records[0]["type_safe_classification"] is None


def test_calendar_classification_uses_canonical_source_url(tmp_path, monkeypatch):
    class FakeChoice:
        def __init__(self, **_kwargs):
            pass

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def system_one(self, _state, questions):
            return SimpleNamespace(choices={key: SimpleNamespace(choice="event") for key in questions})

    monkeypatch.setitem(sys.modules, "typesafe_sdk", SimpleNamespace(Choice=FakeChoice, TypeSafeClient=FakeClient))
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    path = tmp_path / "climate-report.pdf"
    _report_pdf(path)

    bundle = import_pdf_reports([path])

    assert bundle["articles"] == []
    assert bundle["calendar_typesafe"]["classified"] == 1
    assert bundle["calendar_items"][0]["type_safe_classification"] == {
        "provider": "typesafe", "label": "event",
    }


def test_cli_exports_complete_bundle_without_apply(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "source.pdf"
    output = tmp_path / "bundle.json"
    _report_pdf(source)

    assert main(["--input", str(source), "--output", str(output)]) == 0
    bundle = json.loads(output.read_text(encoding="utf-8"))
    assert base64.b64decode(bundle["documents"][0]["source"]["original_pdf_base64"]) == source.read_bytes()
    assert bundle["source_coverage"][0]["bindings"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bundle.json", "source.pdf"]


def _coverage_layout(path, *, changed=False, shift=0, cross_links=False):
    output = BytesIO()
    canvas = Canvas(output, pagesize=letter, invariant=1)
    canvas.translate(0, shift)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(50, 760, "Unlinked material")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(50, 740, "Exact original material without a PDF hyperlink.")
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(50, 690, "Shared anchor")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(50, 670, "First distinct material shares a publisher URL.")
    canvas.linkURL("https://example.org/shared", (48.125, 688.25, 230.5, 708.75), relative=0)
    canvas.drawString(50, 650, "Source: shared publisher")
    canvas.linkURL("https://example.org/shared", (48, 648, 230, 665), relative=0)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(50, 590, "Shared anchor")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(50, 570, "Second material is independent despite its URL.")
    canvas.linkURL("https://example.org/shared", (48, 588, 230, 609), relative=0)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(50, 510, "Two materials in one source section")
    canvas.setFont("Helvetica", 11)
    canvas.drawString(50, 490, "Material A and material B require a reviewed split.")
    canvas.drawString(50, 470, "Source: first")
    canvas.linkURL("https://example.org/a", (48, 468, 200, 485), relative=0)
    canvas.drawString(50, 450, "Source: second")
    canvas.linkURL("https://example.org/b", (48, 448, 200, 465), relative=0)
    canvas.setFont("Helvetica-Bold", 16)
    canvas.drawString(50, 390, "Cross-page material")
    if cross_links:
        canvas.linkURL("https://example.org/cross-page", (48, 388, 250, 408), relative=0)
    canvas.setFont("Helvetica", 11)
    canvas.drawString(50, 370, "The same material continues on the following page.")
    canvas.showPage()
    canvas.drawString(50, 760, "Continuation of the cross-page material.")
    if cross_links:
        canvas.linkURL("https://example.org/cross-page", (48, 758, 300, 778), relative=0)
    canvas.drawString(50, 740, "Changed evidence." if changed else "Original evidence.")
    canvas.save()
    path.write_bytes(output.getvalue())


def _source_decision(source, key, bindings, disposition="mapped", **extra):
    ids = [item["binding_id"] for item in bindings]
    return {"source_document_sha256": source["source_document_sha256"], "entry_key": key,
        "binding_ids": ids, "disposition": disposition,
        "observation_ids": [key for key, required in source["observation_bindings"].items() if set(required) == set(ids)],
        "agent": "coverage-reviewer/test-task", "reason": "Read the exact source region and its native links", **extra}


def _material_bindings(source, index=0):
    required = list(source["observation_bindings"].values())[index]
    return [item for item in source["bindings"] if item["binding_id"] in required]


def test_source_coverage_partitions_real_layout_and_keeps_native_regions(tmp_path, monkeypatch):
    from climate_registry.pdf_intake import validate_source_coverage
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "actual-source.pdf"
    _coverage_layout(path)
    bundle = import_pdf_reports([path])
    document, source = bundle["documents"][0], bundle["source_coverage"][0]
    assert base64.b64decode(document["source"]["original_pdf_base64"]) == path.read_bytes()
    assert source["source_document_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    for page in document["pages"]:
        bindings = [item for item in source["bindings"] if item["page"] == page["page"]]
        assert "".join(item["raw_text"] for item in bindings) == page["text"]
        assert [item["char_start"] for item in bindings] == [0] + [item["char_end"] for item in bindings[:-1]]
        for item in bindings:
            assert item["raw_text"] == page["text"][item["char_start"]:item["char_end"]]
            assert item["page_rect"] == [0, 0, 612, 792]
            assert item["text_sha256"] == hashlib.sha256(item["raw_text"].encode()).hexdigest()
            assert item["page_sha256"] == hashlib.sha256(page["text"].encode()).hexdigest()
        assert [dict((key, value) for key, value in link.items() if key != "source_occurrence_id")
            for item in bindings for link in item["native_links"]] == page["native_links"]
    assert source["bindings"][0]["native_links"] == []
    assert source["bindings"][0]["text_positions"][0]["x"] == 50
    assert source["bindings"][0]["text_positions"][0]["y"] == 760
    native_ids = [link["source_occurrence_id"] for item in source["bindings"] for link in item["native_links"]]
    assert len(native_ids) == len(set(native_ids)) == 5
    assert [item["disposition"] for item in source["entries"]].count("gap") >= 2
    assert len([item for item in source["bindings"] if item["raw_text"].startswith("Shared anchor")]) == 2
    assert document["pages"][0]["native_links"][0]["rect"] == [48.125, 688.25, 230.5, 708.75]
    assert all(item["canonical_identity"] is None and item["content_version_id"] is None for item in source["entries"])
    validate_source_coverage(bundle)


def test_source_review_split_join_all_dispositions_and_independent_handoff(tmp_path, monkeypatch):
    from climate_monitor.pdf_intake import reconcile_source_coverage, source_coverage_status
    from climate_registry.pdf_intake import validate_source_coverage
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "layout.pdf"
    _coverage_layout(path)
    bundle = import_pdf_reports([path])
    source = bundle["source_coverage"][0]
    unlinked = source["bindings"][0]
    shared = [item for item in source["bindings"] if item["raw_text"].startswith("Shared anchor")]
    multiple = next(item for item in source["bindings"] if item["raw_text"].startswith("Two materials"))
    cross = [item for item in source["bindings"] if "Cross-page material" in item["raw_text"] or item["page"] == 2]
    decisions = [
        _source_decision(source, "unlinked", [unlinked], supplement_text=unlinked["raw_text"]),
        _source_decision(source, "material-a", [multiple]),
        _source_decision(source, "material-b", [multiple]),
        _source_decision(source, "joined-pages", cross, supplement_text="".join(item["raw_text"] for item in cross)),
        _source_decision(source, "duplicate-source-anchor", [shared[0]], "excluded"),
        _source_decision(source, "uncertain-distinct-material", [shared[1]], "gap"),
    ]
    reconcile_source_coverage(bundle, decisions)
    status = source_coverage_status(bundle["source_coverage"])
    assert not status["source_complete"] and len(status["handoff_entry_ids"]) == 2
    assert not status["registry_complete"] and not status["display_complete"]
    reviewed = [item for item in source["entries"] if item["revisions"]]
    assert len({item["entry_id"] for item in reviewed}) == 6
    assert len(next(item for item in reviewed if len(item["binding_ids"]) > 1)["binding_ids"]) == 2
    assert len([item for item in reviewed if item["binding_ids"] == [multiple["binding_id"]]]) == 2
    reconcile_source_coverage(bundle, [_source_decision(source, "related-pending", [multiple], "pending", downstream="G4")])
    assert len(source_coverage_status(bundle["source_coverage"])["handoff_entry_ids"]) == 0
    unsupported = [item for item in reviewed if item["revisions"][-1].get("supplement_text")]
    assert all(item["disposition"] == "pending" and item["revisions"][-1]["downstream"] == "G8" for item in unsupported)
    validate_source_coverage(bundle)
    assert bundle["source_coverage"][0]["bindings"][0]["native_links"] == []  # A supplement never creates a PDF annotation.


def test_unlinked_supplement_remains_pending_without_a_real_candidate(tmp_path, monkeypatch):
    from climate_monitor.pdf_intake import reconcile_source_coverage, source_coverage_status
    from climate_registry.pdf_intake import persist_pdf_intake, validate_source_coverage
    from climate_registry.read_api import RegistryReader
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "unlinked-material.pdf"
    _coverage_layout(path)
    bundle = import_pdf_reports([path])
    source = bundle["source_coverage"][0]
    unlinked = source["bindings"][0]
    with pytest.raises(ValueError, match="requires a real source-bound"):
        reconcile_source_coverage(bundle, [_source_decision(source, "missing", [unlinked])])
    proposal = _source_decision(source, "unlinked-supplement", [unlinked], supplement_text=unlinked["raw_text"])
    reconcile_source_coverage(bundle, [proposal, proposal])
    entry = source["entries"][-1]
    assert entry["disposition"] == "pending" and entry["observation_ids"] == []
    assert len(entry["revisions"]) == 1
    revision = entry["revisions"][0]
    assert revision["downstream"] == "G8" and revision["supplement_text"] == unlinked["raw_text"]
    assert revision["agent"] == proposal["agent"] and revision["reason"] == proposal["reason"]
    assert not source_coverage_status(bundle["source_coverage"])["source_complete"]
    assert source_coverage_status(bundle["source_coverage"])["handoff_entry_ids"] == []
    validate_source_coverage(bundle)
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    persist_pdf_intake(database, tmp_path / "backups", bundle)
    persist_pdf_intake(database, tmp_path / "backups", bundle)
    stored = RegistryReader(database, repository_root=tmp_path / "application", public=False).pdf_source_coverage(source["source_document_sha256"])
    assert len(stored["history"]) == 1 and stored["status"]["handoff_entry_ids"] == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_article_occurrences").fetchone()[0] == 5


def test_source_mapping_requires_selected_regions_and_complete_cross_page_join(tmp_path, monkeypatch):
    from climate_monitor.pdf_intake import reconcile_source_coverage, source_coverage_status
    from climate_registry.pdf_intake import validate_source_coverage
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "cross-page.pdf"
    _coverage_layout(path, cross_links=True)
    bundle = import_pdf_reports([path])
    source = bundle["source_coverage"][0]
    unlinked = source["bindings"][0]
    other = next(iter(source["observation_bindings"]))
    with pytest.raises(ValueError, match="every original text/link binding"):
        reconcile_source_coverage(bundle, [_source_decision(source, "adjacent-item-mixup", [unlinked], observation_ids=[other])])
    cross = next(item for item in bundle["articles"] if item["canonical_url"] == "https://example.org/cross-page")
    identities = [item["occurrence_id"] for item in cross["occurrences"]]
    assert len(identities) == 2
    required = {key for identity in identities for key in source["observation_bindings"][identity]}
    bindings = [item for item in source["bindings"] if item["binding_id"] in required]
    assert {item["page"] for item in bindings} == {1, 2}
    with pytest.raises(ValueError, match="every original text/link binding"):
        reconcile_source_coverage(bundle, [_source_decision(source, "partial-join", [item for item in bindings if item["page"] == 1], observation_ids=identities)])
    with pytest.raises(ValueError, match="region without a source-bound observation"):
        reconcile_source_coverage(bundle, [_source_decision(source, "hide-unlinked-gap", bindings + [unlinked], observation_ids=identities)])
    reconcile_source_coverage(bundle, [_source_decision(source, "complete-join", bindings, observation_ids=identities,
        supplement_text="".join(item["raw_text"] for item in bindings))])
    validate_source_coverage(bundle)
    assert len(source_coverage_status(bundle["source_coverage"])["handoff_entry_ids"]) == 1
    assert source["entries"][-1]["canonical_identity"] is None


@pytest.mark.parametrize("shared_url", [False, True])
def test_ordinary_font_adjacent_materials_handoff_independently_without_rewriting_context(tmp_path, monkeypatch, shared_url):
    from climate_monitor.pdf_intake import reconcile_source_coverage, source_coverage_status
    from climate_registry.pdf_intake import persist_pdf_intake, validate_source_coverage
    from climate_registry.read_api import RegistryReader
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "ordinary-adjacent.pdf"
    pdf = Canvas(str(path), invariant=1)
    pdf.setFont("Helvetica", 11)
    for y, text in ((760, "First climate material"), (740, "First independent source statement."),
        (680, "Second climate material"), (660, "Second independent source statement.")):
        pdf.drawString(50, y, text)
    pdf.linkURL("https://example.org/first", (48, 758, 270, 775), relative=0)
    pdf.linkURL("https://example.org/first" if shared_url else "https://example.org/second", (48, 678, 270, 695), relative=0)
    pdf.save()
    bundle = import_pdf_reports([path])
    source = bundle["source_coverage"][0]
    first, second = source["bindings"]
    occurrences = [item for article in bundle["articles"] for item in article["occurrences"]]
    first_observation = next(item for item in occurrences if item["anchor_text"] == "First climate material")
    second_observation = next(item for item in occurrences if item["anchor_text"] == "Second climate material")
    original_occurrences = deepcopy(occurrences)
    assert all("First independent source statement." in item["summary"]
        and "Second independent source statement." in item["summary"] for item in occurrences)
    assert source["observation_bindings"][first_observation["occurrence_id"]] == [first["binding_id"]]
    assert source["observation_bindings"][second_observation["occurrence_id"]] == [second["binding_id"]]
    reconcile_source_coverage(bundle, [
        _source_decision(source, "first-material", [first], observation_ids=[first_observation["occurrence_id"]]),
        _source_decision(source, "second-needs-check", [second], "gap", observation_ids=[]),
    ])
    validate_source_coverage(bundle)
    status = source_coverage_status(bundle["source_coverage"])
    assert not status["source_complete"] and status["unreviewed_binding_ids"] == []
    assert status["handoff_entry_ids"] == [source["entries"][-2]["entry_id"]]
    assert status["pending_entry_ids"] == [source["entries"][-1]["entry_id"]]
    assert occurrences == original_occurrences
    assert first["raw_text"] + second["raw_text"] == bundle["documents"][0]["pages"][0]["text"]
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    persist_pdf_intake(database, tmp_path / "backups", bundle)
    stored = RegistryReader(database, repository_root=tmp_path / "application", public=False).pdf_source_coverage(source["source_document_sha256"])
    assert stored["status"] == status
    with sqlite3.connect(database) as connection:
        raw = [json.loads(row[0]) for row in connection.execute("SELECT occurrence_json FROM pdf_intake_article_occurrences")]
    assert sorted(raw, key=lambda item: item["occurrence_id"]) == sorted(original_occurrences, key=lambda item: item["occurrence_id"])


def test_native_link_material_keeps_all_its_unlinked_continuation_blocks(tmp_path, monkeypatch):
    from climate_monitor.pdf_intake import reconcile_source_coverage, source_coverage_status
    from climate_registry.pdf_intake import validate_source_coverage
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "material-blocks.pdf"
    pdf = Canvas(str(path), invariant=1)
    pdf.setFont("Helvetica", 11)
    for y, text in ((760, "First climate material"), (740, "First independent source statement."),
        (720, "2026"), (700, "Continuation belongs only to the first material."),
        (660, "Second climate material"), (640, "Second independent source statement.")):
        pdf.drawString(50, y, text)
    pdf.linkURL("https://example.org/first", (48, 758, 270, 775), relative=0)
    pdf.linkURL("https://example.org/supporting-reference", (48, 698, 130, 715), relative=0)
    pdf.linkURL("https://example.org/second", (48, 658, 270, 675), relative=0)
    pdf.save()
    bundle = import_pdf_reports([path])
    source = bundle["source_coverage"][0]
    occurrence = bundle["articles"][0]["occurrences"][0]
    required = source["observation_bindings"][occurrence["occurrence_id"]]
    bindings = [item for item in source["bindings"] if item["binding_id"] in required]
    assert len(bindings) == 3
    assert "Continuation belongs only to the first material." in "".join(item["raw_text"] for item in bindings)
    assert all("Second climate material" not in item["raw_text"] for item in bindings)
    with pytest.raises(ValueError, match="every original text/link binding"):
        reconcile_source_coverage(bundle, [_source_decision(source, "incomplete-first", bindings[:1], observation_ids=[occurrence["occurrence_id"]])])
    reconcile_source_coverage(bundle, [_source_decision(source, "whole-first", bindings, observation_ids=[occurrence["occurrence_id"]])])
    validate_source_coverage(bundle)
    assert len(source_coverage_status(bundle["source_coverage"])["handoff_entry_ids"]) == 1


@pytest.mark.parametrize("shared_url", [False, True])
@pytest.mark.parametrize("reverse_text", [False, True])
@pytest.mark.parametrize("bold", [False, True])
def test_duplicate_titles_use_native_geometry_with_reversed_annotations(tmp_path, monkeypatch, shared_url, reverse_text, bold):
    from climate_monitor.pdf_intake import reconcile_source_coverage, source_coverage_status
    from climate_registry.pdf_intake import validate_source_coverage
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "reverse-annotations.pdf"
    pdf = Canvas(str(path), invariant=1)
    materials = [(760, "First independent body."), (650, "Second independent body.")]
    for y, body in reversed(materials) if reverse_text else materials:
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", 16 if bold else 11)
        pdf.drawString(50, y, "Shared title")
        pdf.setFont("Helvetica", 11)
        pdf.drawString(50, y - 20, body)
    top_rect, bottom_rect = [48, 758, 270, 780], [48, 648, 270, 670]
    pdf.textAnnotation("Original non-link PDF annotation", Rect=(350, 740, 400, 780))
    pdf.linkURL("https://example.org/shared" if shared_url else "https://example.org/second", bottom_rect, relative=0)
    pdf.linkURL("https://example.org/shared" if shared_url else "https://example.org/first", top_rect, relative=0)
    pdf.save()
    bundle = import_pdf_reports([path])
    source = bundle["source_coverage"][0]
    occurrences = [item for article in bundle["articles"] for item in article["occurrences"]]
    original = deepcopy(occurrences)
    upper = next(item for item in occurrences if item["link_rects"] == [top_rect])
    lower = next(item for item in occurrences if item["link_rects"] == [bottom_rect])
    upper_bindings = [item for item in source["bindings"] if item["binding_id"] in source["observation_bindings"][upper["occurrence_id"]]]
    lower_bindings = [item for item in source["bindings"] if item["binding_id"] in source["observation_bindings"][lower["occurrence_id"]]]
    assert len(upper_bindings) == len(lower_bindings) == 1
    assert "First independent body." in upper_bindings[0]["raw_text"] and "Second independent body." not in upper_bindings[0]["raw_text"]
    assert "Second independent body." in lower_bindings[0]["raw_text"] and "First independent body." not in lower_bindings[0]["raw_text"]
    assert upper_bindings[0]["native_links"][0]["rect"] == top_rect
    assert upper_bindings[0]["native_links"][0]["annotation_index"] == 2
    assert lower_bindings[0]["native_links"][0]["rect"] == bottom_rect
    assert lower_bindings[0]["native_links"][0]["annotation_index"] == 1
    if bold:
        assert "First independent body." in upper["summary"] and "Second independent body." not in upper["summary"]
        assert "Second independent body." in lower["summary"] and "First independent body." not in lower["summary"]
    reconcile_source_coverage(bundle, [_source_decision(source, "upper", upper_bindings, observation_ids=[upper["occurrence_id"]]),
        _source_decision(source, "lower-pending", lower_bindings, "gap", observation_ids=[])])
    validate_source_coverage(bundle)
    assert len(source_coverage_status(bundle["source_coverage"])["handoff_entry_ids"]) == 1
    assert not source["localization_gaps"] and not source["unlocalized_native_links"]
    assert occurrences == original
    assert "".join(item["raw_text"] for item in source["bindings"]) == bundle["documents"][0]["pages"][0]["text"]


def test_nonunique_native_title_geometry_remains_gap_and_cannot_handoff(tmp_path, monkeypatch):
    from climate_monitor.pdf_intake import reconcile_source_coverage, source_coverage_status
    from climate_registry.pdf_intake import validate_source_coverage
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "ambiguous-geometry.pdf"
    pdf = Canvas(str(path), invariant=1)
    for y, body in ((760, "First independent body."), (650, "Second independent body.")):
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(50, y, "Shared title")
        pdf.setFont("Helvetica", 11)
        pdf.drawString(50, y - 20, body)
    pdf.linkURL("https://example.org/shared", (48, 648, 270, 780), relative=0)
    pdf.save()
    bundle = import_pdf_reports([path])
    source = bundle["source_coverage"][0]
    observation = bundle["articles"][0]["occurrences"][0]
    assert observation["occurrence_id"] in source["localization_gaps"]
    assert source["unlocalized_native_links"][0]["annotation_index"] == 0
    assert source["unlocalized_native_links"][0]["disposition"] == "gap"
    assert source["unlocalized_native_links"][0]["rect"] == [48, 648, 270, 780]
    assert all(not item["native_links"] for item in source["bindings"])
    with pytest.raises(ValueError, match="unique native-link source region"):
        reconcile_source_coverage(bundle, [_source_decision(source, "cannot-guess", source["bindings"], observation_ids=[observation["occurrence_id"]])])
    reconcile_source_coverage(bundle, [_source_decision(source, "nonmaterial-" + str(index), [item], "excluded", observation_ids=[])
        for index, item in enumerate(source["bindings"])])
    validate_source_coverage(bundle)
    status = source_coverage_status(bundle["source_coverage"])
    assert not status["source_complete"] and status["handoff_entry_ids"] == []
    assert status["localization_gaps"] and status["unlocalized_native_link_ids"]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_duplicate_calendar_titles_bind_links_to_geometric_rows(tmp_path, monkeypatch, ambiguous):
    from climate_monitor.pdf_intake import reconcile_source_coverage, source_coverage_status
    from climate_registry.pdf_intake import validate_source_coverage
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "calendar-geometry.pdf"
    pdf = Canvas(str(path), invariant=1)
    pdf.setFont("Helvetica", 11)
    for y, text in ((760, "Key Dates"), (740, "DATE(S) EVENT HOST RELEVANCE"),
        (700, "4 September 2026"), (680, "EVENT"), (660, "Shared conference"), (640, "First calendar body."),
        (600, "5 September 2026"), (580, "EVENT"), (560, "Shared conference"), (540, "Second calendar body.")):
        pdf.drawString(50, y, text)
    if ambiguous:
        pdf.linkURL("https://example.org/uncertain", (48, 558, 270, 674), relative=0)
    else:
        pdf.linkURL("https://example.org/second", (48, 558, 270, 574), relative=0)
        pdf.linkURL("https://example.org/first", (48, 658, 270, 674), relative=0)
    pdf.save()
    bundle = import_pdf_reports([path])
    source = bundle["source_coverage"][0]
    first, second = bundle["calendar_items"]
    assert "First calendar body." in first["raw_text"] and "Second calendar body." not in first["raw_text"]
    assert "Second calendar body." in second["raw_text"] and "First calendar body." not in second["raw_text"]
    required = source["observation_bindings"][first["occurrence_id"]]
    bindings = [item for item in source["bindings"] if item["binding_id"] in required]
    if ambiguous:
        assert first["source_urls"] == second["source_urls"] == []
        assert first["occurrence_id"] in source["localization_gaps"] and second["occurrence_id"] in source["localization_gaps"]
        with pytest.raises(ValueError, match="unique native-link source region"):
            reconcile_source_coverage(bundle, [_source_decision(source, "first", bindings, observation_ids=[first["occurrence_id"]])])
        assert source_coverage_status(bundle["source_coverage"])["handoff_entry_ids"] == []
    else:
        assert first["source_urls"] == ["https://example.org/first"]
        assert second["source_urls"] == ["https://example.org/second"]
        reconcile_source_coverage(bundle, [_source_decision(source, "first", bindings, observation_ids=[first["occurrence_id"]])])
        assert len(source_coverage_status(bundle["source_coverage"])["handoff_entry_ids"]) == 1
    validate_source_coverage(bundle)


@pytest.mark.parametrize("kind,mutation", [(kind, mutation) for kind in ("article", "calendar")
    for mutation in ("text", "content_hash", "page_hash", "identity", "unknown_field")])
def test_pdf_exact_replay_rejects_rewritten_original_occurrence_before_write(tmp_path, monkeypatch, kind, mutation):
    from climate_registry.pdf_intake import persist_pdf_intake, validate_source_coverage
    from climate_registry.errors import RegistryInputError
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "source.pdf"
    _report_pdf(path, include_article=True)
    bundle = import_pdf_reports([path])
    occurrence = bundle["articles"][0]["occurrences"][0] if kind == "article" else bundle["calendar_items"][0]
    if mutation == "text":
        occurrence["summary"] = "Later website summary absent from the retained original PDF."
        if kind == "calendar":
            occurrence["raw_text"] = occurrence["summary"]
            occurrence["summary_sha256"] = hashlib.sha256(occurrence["summary"].encode()).hexdigest()
        occurrence["content_sha256"] = hashlib.sha256(occurrence["summary"].encode()).hexdigest()
        occurrence["page_sha256"] = "0" * 64
    elif mutation == "content_hash": occurrence["content_sha256"] = "0" * 64
    elif mutation == "page_hash": occurrence["page_sha256"] = "0" * 64
    elif mutation == "identity": occurrence["occurrence_id"] = "pretend-original-occurrence"
    else: occurrence["current_web_summary"] = "This is not an immutable PDF fact."
    with pytest.raises(RegistryInputError, match="coverage.*original text, hashes or source fields"):
        validate_source_coverage(bundle)
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    before = database.read_bytes()
    with pytest.raises(RegistryInputError, match="coverage.*original text, hashes or source fields"):
        persist_pdf_intake(database, tmp_path / "backups", bundle)
    assert database.read_bytes() == before and not (tmp_path / "backups").exists()


def test_exact_pdf_replay_allows_only_known_classification_and_management_routing(tmp_path, monkeypatch):
    from climate_registry.pdf_intake import persist_pdf_intake, validate_source_coverage
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "actual-upload-name.pdf"
    _report_pdf(path, include_article=True)
    bundle = import_pdf_reports([path])
    bundle["articles"][0]["type_safe_classification"] = {"provider": "typesafe", "label": "article"}
    bundle["articles"][0]["occurrences"][0]["management_batch_id"] = "actual-management-batch"
    bundle["calendar_items"][0]["type_safe_classification"] = {"provider": "typesafe", "label": "event"}
    validate_source_coverage(bundle)
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    assert persist_pdf_intake(database, tmp_path / "backups", bundle)["status"] == "updated"
    with sqlite3.connect(database) as connection:
        stored = json.loads(connection.execute("SELECT occurrence_json FROM pdf_intake_article_occurrences").fetchone()[0])
        assert stored["management_batch_id"] == "actual-management-batch"
        assert stored["source_document"] == "actual-upload-name.pdf"


def test_source_decisions_require_current_evidence_and_append_only_parent(tmp_path, monkeypatch):
    from climate_monitor.pdf_intake import reconcile_source_coverage
    from climate_registry.pdf_intake import validate_source_coverage
    from climate_registry.errors import RegistryInputError
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "source.pdf"
    _coverage_layout(path)
    bundle = import_pdf_reports([path])
    source = bundle["source_coverage"][0]
    decision = _source_decision(source, "material", [source["bindings"][0]], "excluded")
    reconcile_source_coverage(bundle, [decision, dict(decision, previous_revision_id=None)])
    entry = source["entries"][-1]
    assert len(entry["revisions"]) == 1
    first_revision = entry["revisions"][0]["revision_id"]
    with pytest.raises(ValueError, match="re-read"):
        reconcile_source_coverage(bundle, [dict(decision, disposition="gap")])
    reconcile_source_coverage(bundle, [dict(decision, previous_revision_id=first_revision, reuse_revision_id=first_revision)])
    assert len(entry["revisions"]) == 2
    validate_source_coverage(bundle)
    for mutation in ("region", "link", "identity"):
        bad = deepcopy(bundle)
        if mutation == "region": bad["source_coverage"][0]["bindings"][0]["char_start"] += 1
        elif mutation == "link": bad["documents"][0]["pages"][0]["native_links"][0]["rect"][0] += 1
        else: bad["source_coverage"][0]["entries"][-1]["canonical_identity"] = "fabricated-core-id"
        with pytest.raises(RegistryInputError, match="coverage"):
            validate_source_coverage(bad)
    with pytest.raises(ValueError, match="exact bound"):
        reconcile_source_coverage(bundle, [dict(decision, entry_key="bad-supplement", supplement_text="Website content pretending to be PDF text")])
    with pytest.raises(ValueError, match="pending"):
        reconcile_source_coverage(bundle, [dict(decision, entry_key="unsupported", downstream="G8", disposition="mapped")])
    _coverage_layout(path, changed=True)
    changed = import_pdf_reports([path])
    with pytest.raises(ValueError, match="relocalization"):
        reconcile_source_coverage(changed, [decision])
    assert not set(item["binding_id"] for item in source["bindings"]) & set(item["binding_id"] for item in changed["source_coverage"][0]["bindings"])


def test_source_coverage_ledger_idempotency_history_and_late_retry(tmp_path, monkeypatch):
    from climate_monitor.pdf_intake import reconcile_source_coverage
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.read_api import RegistryReader, RegistryQueryError
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "original-upload.pdf"
    _coverage_layout(path)
    original = import_pdf_reports([path])
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    persist_pdf_intake(database, tmp_path / "backups", original)
    persist_pdf_intake(database, tmp_path / "backups", original)
    reviewed = deepcopy(original)
    source = reviewed["source_coverage"][0]
    reconcile_source_coverage(reviewed, [_source_decision(source, "resolved", _material_bindings(source))])
    persist_pdf_intake(database, tmp_path / "backups", reviewed)
    persist_pdf_intake(database, tmp_path / "backups", original)  # Late source retry cannot roll back the review.
    reader = RegistryReader(database, repository_root=tmp_path / "application", public=False)
    stored = reader.pdf_source_coverage(source["source_document_sha256"])
    assert len(stored["history"]) == 2
    assert len(stored["status"]["handoff_entry_ids"]) == 1
    assert stored["history"][0]["coverage"] == original["source_coverage"][0]
    assert stored["history"][1]["coverage"] == reviewed["source_coverage"][0]
    assert all(item["read_at"] and item["requires_downstream_recheck"] for item in stored["history"][1]["published_baselines"])
    assert all(item["status"] == "pending_g4" for item in stored["history"][1]["published_baselines"])
    # A reformatted source has a new binding but no new canonical article/body.
    shifted = tmp_path / "reformatted.pdf"
    _coverage_layout(shifted, shift=-1)
    reformatted = import_pdf_reports([shifted])
    assert reformatted["documents"][0]["extracted_text_sha256"] == original["documents"][0]["extracted_text_sha256"]
    persist_pdf_intake(database, tmp_path / "backups", reformatted)
    with pytest.raises(RegistryQueryError, match="management evidence"):
        RegistryReader(database, repository_root=tmp_path / "application").pdf_source_coverage(source["source_document_sha256"])
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pdf_source_coverage").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_articles").fetchone()[0] == len(original["articles"])
        assert connection.execute("SELECT COUNT(*) FROM article_content_versions").fetchone()[0] == 0
        for sql in ("UPDATE pdf_source_coverage SET recorded_at='later'", "DELETE FROM pdf_source_coverage"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(sql)


def test_source_coverage_rejects_stale_review_and_preserves_independent_review(tmp_path, monkeypatch):
    from climate_monitor.pdf_intake import reconcile_source_coverage
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.errors import RegistryInputError
    from climate_registry.read_api import RegistryReader
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "source.pdf"
    _coverage_layout(path)
    original = import_pdf_reports([path])
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    first = deepcopy(original)
    source = first["source_coverage"][0]
    decision = _source_decision(source, "material", _material_bindings(source))
    reconcile_source_coverage(first, [decision])
    persist_pdf_intake(database, tmp_path / "backups", first)
    stale = deepcopy(original)
    reconcile_source_coverage(stale, [dict(decision, disposition="excluded")])
    before = database.read_bytes()
    with pytest.raises(RegistryInputError, match="re-read"):
        persist_pdf_intake(database, tmp_path / "backups", stale)
    assert database.read_bytes() == before
    independent = deepcopy(original)
    reconcile_source_coverage(independent, [_source_decision(source, "other-material", _material_bindings(source, -1))])
    persist_pdf_intake(database, tmp_path / "backups", independent)
    current = RegistryReader(database, repository_root=tmp_path / "application", public=False).pdf_source_coverage(source["source_document_sha256"])
    assert len(current["status"]["handoff_entry_ids"]) == 2


def test_cli_coverage_review_history_exports_without_registry_writes(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "source.pdf"
    _coverage_layout(source)
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    assert main(["--input", str(source), "--output", str(first)]) == 0
    bundle = json.loads(first.read_text(encoding="utf-8"))
    coverage = bundle["source_coverage"][0]
    decisions = tmp_path / "decisions.json"
    decisions.write_text(json.dumps([_source_decision(coverage, "material", _material_bindings(coverage))]), encoding="utf-8")
    assert main(["--input", str(source), "--output", str(second), "--coverage-history", str(first), "--coverage-decisions", str(decisions)]) == 0
    assert len(json.loads(second.read_text(encoding="utf-8"))["source_coverage"][0]["entries"][-1]["revisions"]) == 1
    assert not list(tmp_path.glob("*.sqlite3")) and not (tmp_path / "backups").exists()
    before = first.read_bytes()
    with pytest.raises(SystemExit) as error:
        main(["--input", str(source), "--output", str(first), "--coverage-history", str(first)])
    assert error.value.code == 2 and first.read_bytes() == before


def test_cli_without_apply_only_prints_dry_run_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "source.pdf"
    _report_pdf(source)

    assert main(["--input", str(source)]) == 0

    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"
    assert list(tmp_path.iterdir()) == [source]


def test_cli_writes_and_persists_bundle_idempotently(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "source.pdf"
    output = tmp_path / "out" / "bundle.json"
    database = tmp_path / "climate_registry.sqlite3"
    backup_dir = tmp_path / "backups"
    _report_pdf(source)
    initialize_registry(database)

    args = ["--input", str(source), "--output", str(output), "--registry-db", str(database),
            "--backup-dir", str(backup_dir), "--apply"]
    assert main(args) == 0
    assert main(args) == 0
    bundle = json.loads(output.read_text(encoding="utf-8"))
    assert bundle["schema_version"] == "climate-pdf-intake.v1"
    assert bundle["documents"][0]["source"]["filename"] == "source.pdf"
    import sqlite3
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (26,)
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_documents").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_articles").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_article_occurrences").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_calendar_items").fetchone() == (1,)
        original_pdf, created_at, modified_at, document_json = connection.execute(
            "SELECT original_pdf, pdf_created_at, pdf_modified_at, document_json FROM pdf_intake_documents"
        ).fetchone()
        assert original_pdf == source.read_bytes()
        assert created_at and modified_at
        assert "Climate summary preserved exactly" in document_json
        assert "original_pdf_base64" not in document_json
        assert json.loads(document_json)["source"]["pdf_metadata"]["/Title"] == "Climate intake source metadata"
    assert len(list(backup_dir.glob("*.bak"))) == 2


def test_cli_rejects_output_aliases_before_overwriting_pdf_or_registry(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "source.pdf"
    database = tmp_path / "climate_registry.sqlite3"
    backup_dir = tmp_path / "backups"
    _report_pdf(source)
    original_pdf = source.read_bytes()
    initialize_registry(database)

    for output in (source, database):
        with pytest.raises(SystemExit) as error:
            main(["--input", str(source), "--output", str(output), "--registry-db", str(database),
                  "--backup-dir", str(backup_dir), "--apply"])
        assert error.value.code == 2
        assert source.read_bytes() == original_pdf
        import sqlite3
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (26,)
    assert not backup_dir.exists()


def test_registry_tracks_rescheduled_calendar_occurrence(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "second.pdf"
    _report_pdf(first_pdf, "4–5 Sep 2026")
    _report_pdf(second_pdf, "5–6 Sep 2026")
    first_event = import_pdf_reports([first_pdf])["calendar_items"][0]
    second_event = import_pdf_reports([second_pdf])["calendar_items"][0]
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)

    from climate_registry.pdf_intake import persist_pdf_intake

    persist_pdf_intake(database, tmp_path / "backups", import_pdf_reports([first_pdf]))
    persist_pdf_intake(database, tmp_path / "backups", import_pdf_reports([second_pdf]))

    import sqlite3
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT event_id, occurrence_id, start_date FROM pdf_intake_calendar_items ORDER BY start_date"
        ).fetchall()
    assert first_event["event_id"] == second_event["event_id"]
    assert first_event["occurrence_id"] != second_event["occurrence_id"]
    assert len(rows) == 2
    assert len({row[0] for row in rows}) == 1
    assert len({row[1] for row in rows}) == 2
    assert [row[2] for row in rows] == ["2026-09-04", "2026-09-05"]


def test_pdf_links_only_an_existing_eligible_core_and_reimport_keeps_aliases_stable(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "report.pdf"
    _report_pdf_with_two_articles(source)
    bundle = import_pdf_reports([source])
    bundle["articles"][0]["type_safe_classification"] = {"provider": "typesafe", "label": "article"}
    bundle["articles"].append({
        "article_id": "pdf-homepage", "canonical_url": "https://example.org/news",
        "title": "News", "type_safe_classification": {"provider": "typesafe", "label": "landing_page"},
        "occurrences": [{
            "occurrence_id": "pdf-homepage-occurrence", "raw_url": "https://example.org/news?from=pdf",
            "page": 2, "report_date": "2026-09-03", "publication_date": None,
            "source_document_sha256": bundle["documents"][0]["source"]["sha256"],
            "content_sha256": "a" * 64, "page_sha256": "b" * 64,
            "summary": "PDF-only homepage observation", "summary_basis": "verbatim_pdf_page_context",
        }],
    })
    # This hand-authored legacy v1 link-confirmation fixture predates exact G2 replay.
    bundle.pop("source_coverage")
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.read_api import RegistryReader

    first = bundle["articles"][0]
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO sources VALUES ('source-example', 'example.org', 'Example', '2026-09-01', '2026-09-01')")
        connection.execute(
            """INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen,
               current_version_id, document_kind, publication_eligible, exclusion_reason)
               VALUES ('core-first', ?, 'source-example', '2026-09-01', '2026-09-01',
               'version-first', 'article', 1, NULL)""", (first["canonical_url"],),
        )
        connection.execute(
            """INSERT INTO article_versions VALUES ('version-first', 'core-first', 'Core title',
               'core title', 'Core report summary.', ?, 'report-title-summary', '2026-09-01', '2026-09-01')""",
            ("c" * 64,),
        )
        connection.execute(
            """INSERT INTO articles VALUES ('core-news', 'https://example.org/news', 'source-example',
               '2026-09-01', '2026-09-01', 'version-news', 'article', 1, NULL, NULL, 'summary_excerpt')"""
        )
        connection.execute(
            """INSERT INTO article_versions VALUES ('version-news', 'core-news', 'News', 'news', 'News summary.',
               ?, 'report-title-summary', '2026-09-01', '2026-09-01')""", ("e" * 64,)
        )
        connection.commit()
    connection.close()

    persist_pdf_intake(database, tmp_path / "backups", bundle)
    reader = RegistryReader(database, public=False, repository_root=tmp_path / "app")
    core = reader.articles(page_size=10)["items"]
    assert {item["article_id"] for item in core} == {"core-first", "core-news"}
    assert {item["article_id"]: item["pdf_occurrence_count"] for item in core} == {
        "core-first": 1, "core-news": 0,
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM article_versions").fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM pdf_intake_articles WHERE core_article_id IS NOT NULL"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT core_article_id, confirmation_basis FROM pdf_intake_articles WHERE article_id='pdf-homepage'"
        ).fetchone() == (None, None)
        alias_before = connection.execute(
            "SELECT first_seen, last_seen, times_seen FROM url_aliases WHERE raw_url=?",
            (first["occurrences"][0]["raw_url"],),
        ).fetchone()
    connection.close()
    detail = reader.article("core-first")
    assert detail["summary"] is None and detail["report_summary"] == "Core report summary."
    assert "summary" in detail["pdf_occurrences"][0]
    assert {item["article_id"] for item in reader.pdf_articles()["items"]} == {
        "pdf-homepage", bundle["articles"][1]["article_id"],
    }
    persist_pdf_intake(database, tmp_path / "second-backups", bundle)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT first_seen, last_seen, times_seen FROM url_aliases WHERE raw_url=?",
            (first["occurrences"][0]["raw_url"],),
        ).fetchone() == alias_before
    connection.close()


def test_existing_pdf_observation_reconciles_after_an_exact_core_article_arrives(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "report.pdf"
    _report_pdf_with_two_articles(source)
    bundle = import_pdf_reports([source])
    bundle["articles"][0]["type_safe_classification"] = {"provider": "typesafe", "label": "article"}
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.schema import reconcile_pdf_article_links

    persist_pdf_intake(database, tmp_path / "backups", bundle)
    article = bundle["articles"][0]
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO sources VALUES ('s', 'example.org', 'Example', '2026-09-01', '2026-09-01')")
        connection.execute(
            """INSERT INTO articles VALUES ('core', ?, 's', '2026-09-01', '2026-09-01',
               'version', 'article', 1, NULL, NULL, 'summary_excerpt')""", (article["canonical_url"],),
        )
        connection.execute(
            "INSERT INTO article_versions VALUES ('version', 'core', 'Title', 'title', 'Summary', ?, 'report-title-summary', '2026-09-01', '2026-09-01')",
            ("d" * 64,),
        )
        reconcile_pdf_article_links(connection, observed_at="2026-09-02")
        assert connection.execute(
            "SELECT core_article_id FROM pdf_intake_articles WHERE article_id=?", (article["article_id"],)
        ).fetchone() == ("core",)
        assert connection.execute(
            "SELECT article_id FROM url_aliases WHERE raw_url=?", (article["occurrences"][0]["raw_url"],)
        ).fetchone() == ("core",)
        connection.commit()
    connection.close()


def test_pdf_reimport_with_landing_page_label_does_not_confirm_an_unclassified_observation(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "report.pdf"
    _report_pdf_with_two_articles(source)
    bundle = import_pdf_reports([source])
    article = bundle["articles"][0]
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    from climate_registry.pdf_intake import persist_pdf_intake
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO sources VALUES ('s', 'example.org', 'Example', '2026-09-01', '2026-09-01')")
        connection.execute("INSERT INTO articles VALUES ('core', ?, 's', '2026-09-01', '2026-09-01', 'v', 'article', 1, NULL, NULL, 'summary_excerpt')", (article["canonical_url"],))
        connection.execute("INSERT INTO article_versions VALUES ('v', 'core', 'Title', 'title', 'Summary', ?, 'report-title-summary', '2026-09-01', '2026-09-01')", ("a" * 64,))
        connection.commit()
    connection.close()
    persist_pdf_intake(database, tmp_path / "backups", bundle)
    article["type_safe_classification"] = {"provider": "typesafe", "label": "landing_page"}
    persist_pdf_intake(database, tmp_path / "retry-backups", bundle)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT core_article_id FROM pdf_intake_articles WHERE article_id=?", (article["article_id"],)).fetchone() == (None,)
    connection.close()
    from climate_registry.read_api import RegistryReader
    reader = RegistryReader(database, public=False, repository_root=tmp_path / "app")
    assert "pdf_occurrences" not in reader.article("core")
    assert article["article_id"] in {item["article_id"] for item in reader.pdf_articles()["items"]}


def test_v15_migration_reconciles_only_exact_evidenced_pdf_articles(tmp_path):
    database = tmp_path / "registry.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection, target_version=15)
    connection.execute("INSERT INTO sources VALUES ('s', 'example.org', 'Example', '2026-09-01', '2026-09-01')")
    connection.execute(
        "INSERT INTO articles VALUES ('core', 'https://example.org/study', 's', '2026-09-01', '2026-09-01', 'version', 'article', 1, NULL, NULL, 'summary_excerpt')"
    )
    connection.execute("INSERT INTO articles VALUES ('core-news', 'https://example.org/news', 's', '2026-09-01', '2026-09-01', 'version-news', 'article', 1, NULL, NULL, 'summary_excerpt')")
    connection.execute("INSERT INTO article_versions VALUES ('version-news', 'core-news', 'News', 'news', 'News.', ?, 'report-title-summary', '2026-09-01', '2026-09-01')", ("d" * 64,))
    connection.execute(
        "INSERT INTO article_versions VALUES ('version', 'core', 'Core', 'core', 'Core summary.', ?, 'report-title-summary', '2026-09-01', '2026-09-01')",
        ("c" * 64,),
    )
    connection.execute(
        """INSERT INTO pdf_intake_documents(
           document_sha256, source_path, filename, media_type, size_bytes, extracted_text_sha256,
           document_json, imported_at
        ) VALUES (?, 'C:/report.pdf', 'report.pdf', 'application/pdf', 1, ?, '{}', '2026-09-02T00:00:00Z')""",
        ("a" * 64, "b" * 64),
    )
    connection.execute("INSERT INTO pdf_intake_articles VALUES ('pdf-exact', 'https://example.org/study', 'PDF title', '{\"provider\":\"typesafe\",\"label\":\"article\"}', '2026-09-02T00:00:00Z')")
    connection.execute("INSERT INTO pdf_intake_articles VALUES ('pdf-home', 'https://example.org/news', 'PDF title', NULL, '2026-09-02T00:00:00Z')")
    connection.executemany(
        """INSERT INTO pdf_intake_article_occurrences VALUES (?, ?, ?, 2, ?, NULL, NULL, ?, ?, ?)""",
        (("occ-exact", "pdf-exact", "a" * 64, "https://example.org/study?from=pdf", "d" * 64, "e" * 64,
          json.dumps({"occurrence_id": "occ-exact"})),
         ("occ-home", "pdf-home", "a" * 64, "https://example.org/news", "f" * 64, "e" * 64,
          json.dumps({"occurrence_id": "occ-home"}))),
    )
    connection.commit()
    assert apply_migrations(connection) == [16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26]
    assert connection.execute(
        "SELECT core_article_id, confirmation_basis FROM pdf_intake_articles WHERE article_id='pdf-exact'"
    ).fetchone() == ("core", "exact_url_eligible_detail")
    assert connection.execute(
        "SELECT core_article_id FROM pdf_intake_articles WHERE article_id='pdf-home'"
    ).fetchone() == (None,)
    connection.close()
    from climate_registry.read_api import RegistryReader
    detail = RegistryReader(database, public=False, repository_root=tmp_path / "app").article("core")
    assert detail["pdf_occurrences"][0]["occurrence_id"] == "occ-exact"
    assert {item["article_id"] for item in RegistryReader(
        database, public=False, repository_root=tmp_path / "app"
    ).pdf_articles()["items"]} == {"pdf-home"}


def test_registry_backfills_classification_and_tracks_duplicate_pdf_sources(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    first_pdf, second_pdf, third_pdf = (tmp_path / name for name in ("first.pdf", "second.pdf", "third.pdf"))
    _report_pdf(first_pdf, include_article=True)
    second_pdf.write_bytes(first_pdf.read_bytes())
    third_pdf.write_bytes(first_pdf.read_bytes())

    combined = import_pdf_reports([first_pdf, second_pdf])
    assert len(combined["documents"]) == 1
    assert {item["filename"] for item in combined["documents"][0]["source"]["source_observations"]} == {
        "first.pdf", "second.pdf",
    }
    retry = import_pdf_reports([third_pdf])
    retry["articles"][0]["type_safe_classification"] = {
        "provider": "typesafe", "label": "event",
    }
    retry["calendar_items"][0]["type_safe_classification"] = {
        "provider": "typesafe", "label": "event",
    }
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)

    from climate_registry.pdf_intake import persist_pdf_intake

    persist_pdf_intake(database, tmp_path / "backups", combined)
    persist_pdf_intake(database, tmp_path / "backups", retry)

    import sqlite3
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_documents").fetchone() == (1,)
        source_rows = connection.execute(
            "SELECT filename FROM pdf_intake_document_sources ORDER BY filename"
        ).fetchall()
        classification = connection.execute(
            "SELECT type_safe_classification_json FROM pdf_intake_calendar_items"
        ).fetchone()[0]
        article_classification = connection.execute(
            "SELECT type_safe_classification_json FROM pdf_intake_articles"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE pdf_intake_calendar_items SET name='rewritten'")
    assert [row[0] for row in source_rows] == ["first.pdf", "second.pdf", "third.pdf"]
    assert json.loads(classification) == {"provider": "typesafe", "label": "event"}
    assert json.loads(article_classification) == {"provider": "typesafe", "label": "event"}
    from climate_registry.read_api import RegistryReader

    calendar = RegistryReader(
        database, public=False, repository_root=tmp_path / "app-root",
    ).pdf_calendar_items()["items"][0]
    assert {item["filename"] for item in calendar["source_observations"]} == {
        "first.pdf", "second.pdf", "third.pdf",
    }
    assert calendar["type_safe_classification"] == {"provider": "typesafe", "label": "event"}


@pytest.mark.parametrize("layout", ["calendar_only", "mixed", "corrected_event"])
def test_source_handoff_captures_published_calendar_baseline_from_persisted_occurrence(tmp_path, monkeypatch, layout):
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.publication import stage_entities, _approve
    from climate_registry.owner_dates import current_date_view
    from climate_registry.read_api import RegistryReader
    from verified_fixtures import seed_verified_articles, seed_verified_pdf_meetings
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "published-calendar.pdf"
    if layout == "corrected_event":
        pdf = Canvas(str(path), invariant=1)
        for y, text in ((760, "Key Dates"), (740, "DATE(S) EVENT HOST RELEVANCE"),
            (700, "4 September 2026"), (680, "EVENT"), (660, "Shared conference"), (640, "First calendar body."),
            (600, "5 September 2026"), (580, "EVENT"), (560, "Shared conference"), (540, "Second calendar body.")):
            pdf.drawString(50, y, text)
        pdf.linkURL("https://example.org/second", (48, 558, 270, 574), relative=0)
        pdf.linkURL("https://example.org/first", (48, 658, 270, 674), relative=0)
        pdf.save()
    else:
        _report_pdf(path, include_article=layout == "mixed")
    bundle = import_pdf_reports([path])
    for article in bundle["articles"]:
        article["type_safe_classification"] = {"provider": "typesafe", "label": "article"}
    legacy = deepcopy(bundle)
    legacy.pop("source_coverage")
    if layout == "corrected_event":
        legacy["calendar_items"] = _extract_calendar_items(legacy["documents"][0])
        assert all(old["event_id"] != new["event_id"] for old, new in zip(legacy["calendar_items"], bundle["calendar_items"]))
    translated = {}
    for index, (old, new) in enumerate(zip(legacy["calendar_items"], bundle["calendar_items"])):
        assert old["raw_text"] == new["raw_text"] and old["content_sha256"] == new["content_sha256"]
        old["occurrence_id"] = "legacy-calendar-" + str(index)
        translated[new["occurrence_id"]] = (old["occurrence_id"], old["event_id"])
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    persist_pdf_intake(database, tmp_path / "backups", legacy)
    connection = sqlite3.connect(database)
    try:
        seed_verified_articles(connection)
        seed_verified_pdf_meetings(connection)
        for candidate in stage_entities(connection):
            _approve(connection, candidate, {"basis": "existing isolated publication"}, status="accepted_legacy")
        connection.commit()
    finally:
        connection.close()
    if layout == "mixed":
        from climate_registry.owner_checks import plan_owner_check, validate_manifest, _ingest_one
        article_id = bundle["articles"][0]["article_id"]
        manifest = plan_owner_check(database, "pdf_article", article_id,
            date_fields=["publication_date"], human_date_override=True)
        manifest.update(actor={"id": "operator", "task_id": "calendar-baseline-fixture"},
            checked_at="2026-10-24T12:00:00Z", authorization_reference="human-message:calendar-baseline",
            audit_reference="audit:calendar-baseline", manual_authorization={"instruction": "Set this article publication date to 2026-10-02.",
                "authorized_by": "owner", "scope": "record"}, observations=[{"target": manifest["plan"]["targets"][0],
                    "public_fields": {"publication_date": "2026-10-02"},
                    "field_evidence": {"publication_date": {"text": "2026-10-02", "precision": "day"}}}])
        validate_manifest(manifest, strict_date_types=True)
        connection = sqlite3.connect(database)
        try:
            assert _ingest_one(connection, database, manifest, True)["status"] == "approved"
            connection.commit()
        finally:
            connection.close()
    connection = sqlite3.connect(database)
    try:
        raw_before = connection.execute("SELECT occurrence_id,event_id,item_json FROM pdf_intake_calendar_items ORDER BY rowid").fetchall()
        published = {(kind, identity): (candidate, json.loads(snapshot), current_date_view(connection, kind, identity))
            for kind, identity, candidate, snapshot in connection.execute("""SELECT p.entity_kind,p.entity_id,p.published_candidate_sha256,c.snapshot_json
                FROM registry_publication p JOIN registry_candidates c ON c.candidate_sha256=p.published_candidate_sha256""")}
        assert all(view for _, _, view in published.values())
        for old in legacy["calendar_items"]:
            view = published[("pdf_meeting", old["event_id"])][2]
            observed = next(row for row in view["tables"]["pdf_intake_calendar_items"] if row["occurrence_id"] == old["occurrence_id"])
            assert {field: observed[field] for field in ("raw_date", "date_precision", "start_date", "end_date")} == {
                field: old[field] for field in ("raw_date", "date_precision", "start_date", "end_date")}
        if layout == "mixed":
            assert published[("pdf_article", article_id)][2]["derived_display"]["manual_date_overrides"]["publication_date"]["new_value"] == "2026-10-02"
    finally:
        connection.close()
    persist_pdf_intake(database, tmp_path / "backups", bundle)
    reader = RegistryReader(database, repository_root=tmp_path / "app", public=False)
    history = reader.pdf_source_coverage(bundle["documents"][0]["source"]["sha256"])["history"]
    assert len(history) == 1
    expected = []
    for article in bundle["articles"]:
        candidate, snapshot, view = published[("pdf_article", article["article_id"])]
        expected.append({"observation_article_id": article["article_id"], "entity_kind": "pdf_article", "entity_id": article["article_id"],
            "status": "published", "candidate_sha256": candidate, "snapshot": snapshot, "public_view": view,
            "read_at": history[0]["recorded_at"], "requires_downstream_recheck": True})
    for item in bundle["calendar_items"]:
        occurrence_id, event_id = translated[item["occurrence_id"]]
        candidate, snapshot, view = published[("pdf_meeting", event_id)]
        expected.append({"observation_occurrence_id": item["occurrence_id"], "registry_occurrence_id": occurrence_id,
            "entity_kind": "pdf_meeting", "entity_id": event_id, "status": "published", "candidate_sha256": candidate,
            "snapshot": snapshot, "public_view": view, "read_at": history[0]["recorded_at"], "requires_downstream_recheck": True})
    assert history[0]["published_baselines"] == expected
    assert history[0]["recorded_at"]
    for new_id, (old_id, _) in translated.items():
        assert history[0]["registry_occurrence_bindings"][new_id] == old_id
    persist_pdf_intake(database, tmp_path / "backups", bundle)
    assert reader.pdf_source_coverage(bundle["documents"][0]["source"]["sha256"])["history"] == history
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT occurrence_id,event_id,item_json FROM pdf_intake_calendar_items ORDER BY rowid").fetchall() == raw_before
        for (kind, identity), (candidate, snapshot, view) in published.items():
            assert connection.execute("SELECT published_candidate_sha256 FROM registry_publication WHERE entity_kind=? AND entity_id=?", (kind, identity)).fetchone()[0] == candidate
            assert current_date_view(connection, kind, identity) == view
    finally:
        connection.close()


@pytest.mark.parametrize("existing_published", [False, True])
def test_calendar_baseline_does_not_infer_publication_from_new_event_key(tmp_path, monkeypatch, existing_published):
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.publication import stage_entities, _approve
    from climate_registry.read_api import RegistryReader
    from verified_fixtures import seed_verified_pdf_meetings
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "existing.pdf"
    _report_pdf(path)
    bundle = import_pdf_reports([path])
    legacy = deepcopy(bundle)
    legacy.pop("source_coverage")
    legacy["calendar_items"][0]["occurrence_id"] = "legacy-calendar"
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    persist_pdf_intake(database, tmp_path / "backups", legacy)
    if existing_published:
        connection = sqlite3.connect(database)
        try:
            seed_verified_pdf_meetings(connection)
            for candidate in stage_entities(connection):
                _approve(connection, candidate, {"basis": "isolated previous source"}, status="accepted_legacy")
            connection.commit()
        finally:
            connection.close()
        other = tmp_path / "changed-source.pdf"
        _report_pdf(other, calendar_date="6–7 Sep 2026")
        bundle = import_pdf_reports([other])
        assert bundle["calendar_items"][0]["event_id"] == legacy["calendar_items"][0]["event_id"]
        assert bundle["documents"][0]["source"]["sha256"] != legacy["documents"][0]["source"]["sha256"]
    persist_pdf_intake(database, tmp_path / "backups", bundle)
    history = RegistryReader(database, repository_root=tmp_path / "app", public=False).pdf_source_coverage(bundle["documents"][0]["source"]["sha256"])["history"]
    key = bundle["calendar_items"][0]["occurrence_id"]
    assert history[0]["published_baselines"] == [{"observation_occurrence_id": key,
        "registry_occurrence_id": key if existing_published else "legacy-calendar",
        "entity_kind": None, "entity_id": None, "status": "pending_g4", "candidate_sha256": None,
        "snapshot": None, "public_view": None, "read_at": history[0]["recorded_at"], "requires_downstream_recheck": True}]


@pytest.mark.parametrize("association", ["native", "ambiguous", "unapproved"])
def test_calendar_baseline_requires_unambiguous_approved_native_association(tmp_path, monkeypatch, association):
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.publication import stage_entities, _approve
    from climate_registry.owner_dates import current_date_view
    from climate_registry.read_api import RegistryReader
    from climate_registry.information_checks import _json, _sha
    from verified_fixtures import seed_verified_pdf_meetings
    from datetime import datetime, timedelta, timezone
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "native-association.pdf"
    _report_pdf(path)
    bundle = import_pdf_reports([path])
    legacy = deepcopy(bundle)
    legacy.pop("source_coverage")
    item = legacy["calendar_items"][0]
    item["occurrence_id"] = "legacy-calendar"
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    persist_pdf_intake(database, tmp_path / "backups", legacy)
    connection = sqlite3.connect(database)
    try:
        seed_verified_pdf_meetings(connection)
        if association == "ambiguous":
            for candidate in stage_entities(connection):
                _approve(connection, candidate, {"basis": "existing PDF publication"}, status="accepted_legacy")
        # Existing native identity, with the real source-bound PDF check as its approved observation.
        connection.execute("""INSERT INTO climate_events(event_id,record_version,name,event_type,status,date_precision,
            start_date,end_date,needs_confirmation,source_count,created_at,updated_at)
            VALUES('known-native',1,?,'conference','scheduled',?,?,?,0,1,'2026-09-01','2026-09-01')""",
            (item["name"], item["date_precision"], item["start_date"], item["end_date"]))
        connection.row_factory = sqlite3.Row
        check = dict(connection.execute("SELECT * FROM meeting_check_attempts WHERE occurrence_id=?", (item["occurrence_id"],)).fetchone())
        check["attempt_id"] = "confirmed-native-link"
        packet = json.loads(check["packet_json"])
        check["checked_at"] = packet["checked_at"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        packet["canonical_event_id"] = "known-native"
        run = dict(connection.execute("SELECT * FROM meeting_check_runs WHERE run_id=?", (check["run_id"],)).fetchone())
        run.update(run_id="confirmed-native-run", created_at=check["checked_at"], completed_at=check["checked_at"])
        connection.execute("INSERT INTO meeting_check_runs(" + ",".join(run) + ") VALUES(" + ",".join("?" for _ in run) + ")", tuple(run.values()))
        check["run_id"] = run["run_id"]
        check.update(packet_json=_json(packet), packet_sha256=_sha(packet))
        connection.execute("INSERT INTO meeting_check_attempts(" + ",".join(check) + ") VALUES(" + ",".join("?" for _ in check) + ")", tuple(check.values()))
        connection.row_factory = None
        stage_entities(connection)
        candidate, snapshot = connection.execute("""SELECT c.candidate_sha256,c.snapshot_json FROM registry_publication p
            JOIN registry_candidates c ON c.candidate_sha256=p.latest_candidate_sha256 WHERE p.entity_kind='meeting' AND p.entity_id='known-native'""").fetchone()
        if association != "unapproved":
            _approve(connection, candidate, {"basis": "existing approved native association"}, status="accepted_legacy")
            view = current_date_view(connection, "meeting", "known-native")
            assert view and any(row["occurrence_id"] == item["occurrence_id"] for row in view["tables"]["pdf_intake_calendar_items"])
        raw_before = connection.execute("SELECT occurrence_id,event_id,item_json FROM pdf_intake_calendar_items").fetchall()
        connection.commit()
    finally:
        connection.close()
    persist_pdf_intake(database, tmp_path / "backups", bundle)
    history = RegistryReader(database, repository_root=tmp_path / "app", public=False).pdf_source_coverage(bundle["documents"][0]["source"]["sha256"])["history"]
    expected = {"observation_occurrence_id": bundle["calendar_items"][0]["occurrence_id"], "registry_occurrence_id": item["occurrence_id"],
        "entity_kind": "meeting" if association == "native" else None, "entity_id": "known-native" if association == "native" else None,
        "status": "published" if association == "native" else "pending_g4", "candidate_sha256": candidate if association == "native" else None,
        "snapshot": json.loads(snapshot) if association == "native" else None, "public_view": view if association == "native" else None,
        "read_at": history[0]["recorded_at"], "requires_downstream_recheck": True}
    assert history[0]["published_baselines"] == [expected]
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT occurrence_id,event_id,item_json FROM pdf_intake_calendar_items").fetchall() == raw_before
    finally:
        connection.close()


@pytest.mark.parametrize("history", ["reverse_rowid", "legacy_ids", "legacy_calendar_routing"])
def test_duplicate_source_reimport_matches_evidence_not_rowid(tmp_path, monkeypatch, history):
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.read_api import RegistryReader
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "duplicate-history.pdf"
    pdf = Canvas(str(path), invariant=1)
    for y, body in ((760, "First independent body."), (650, "Second independent body.")):
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(50, y, "Shared title")
        pdf.setFont("Helvetica", 11)
        pdf.drawString(50, y - 20, body)
    pdf.linkURL("https://example.org/shared", (48, 648, 270, 670), relative=0)
    pdf.linkURL("https://example.org/shared", (48, 758, 270, 780), relative=0)
    pdf.showPage()
    for y, text in ((760, "Key Dates"), (740, "DATE(S) EVENT HOST RELEVANCE"),
        (700, "4 September 2026"), (680, "EVENT"), (660, "Shared conference"), (640, "First calendar body."),
        (600, "5 September 2026"), (580, "EVENT"), (560, "Shared conference"), (540, "Second calendar body.")):
        pdf.drawString(50, y, text)
    pdf.linkURL("https://example.org/calendar-second", (48, 558, 270, 574), relative=0)
    pdf.linkURL("https://example.org/calendar-first", (48, 658, 270, 674), relative=0)
    pdf.save()
    bundle = import_pdf_reports([path])
    assert len(bundle["articles"][0]["occurrences"]) == len(bundle["calendar_items"]) == 2
    legacy = deepcopy(bundle)
    legacy.pop("source_coverage")
    if history == "legacy_calendar_routing":
        # Re-run the supported pre-geometry path on the exact extracted source page.
        legacy["calendar_items"] = _extract_calendar_items(legacy["documents"][0])
        assert [item["occurrence_id"] for item in legacy["calendar_items"]] == [item["occurrence_id"] for item in bundle["calendar_items"]]
        assert all(old["event_id"] != current["event_id"] for old, current in zip(legacy["calendar_items"], bundle["calendar_items"]))
        legacy["documents"][0]["calendar_items"] = deepcopy(legacy["calendar_items"])
    expected = {}
    for index, occurrence in enumerate(legacy["articles"][0]["occurrences"]):
        key = occurrence["occurrence_id"]
        if history == "legacy_ids":
            occurrence["occurrence_id"] = f"legacy-article-{index}"
        expected[key] = occurrence["occurrence_id"]
    for index, item in enumerate(legacy["calendar_items"]):
        key = item["occurrence_id"]
        if history in {"legacy_ids", "legacy_calendar_routing"}:
            item["occurrence_id"] = f"legacy-calendar-{index}"
        expected[key] = item["occurrence_id"]
    legacy["articles"][0]["occurrences"].reverse()
    legacy["calendar_items"].reverse()
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    persist_pdf_intake(database, tmp_path / "backups", legacy)

    def raw_rows():
        connection = sqlite3.connect(database)
        try:
            return (connection.execute("SELECT occurrence_id, occurrence_json FROM pdf_intake_article_occurrences ORDER BY rowid").fetchall(),
                connection.execute("SELECT occurrence_id, event_id, item_json FROM pdf_intake_calendar_items ORDER BY rowid").fetchall())
        finally:
            connection.close()

    before = raw_rows()
    first = persist_pdf_intake(database, tmp_path / "backups", bundle)
    retry = persist_pdf_intake(database, tmp_path / "backups", bundle)
    assert first["added"] == retry["added"] == {"documents": 0, "article_occurrences": 0, "calendar_items": 0}
    assert raw_rows() == before
    coverage = RegistryReader(database, repository_root=tmp_path / "application", public=False).pdf_source_coverage(bundle["documents"][0]["source"]["sha256"])
    assert len(coverage["history"]) == 1
    assert coverage["history"][0]["registry_occurrence_bindings"] == expected


@pytest.mark.parametrize("kind", ["article", "calendar"])
def test_ambiguous_legacy_source_reimport_does_not_guess_or_change_history(tmp_path, monkeypatch, kind):
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.errors import RegistryInputError
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    path = tmp_path / "ambiguous-history.pdf"
    pdf = Canvas(str(path), invariant=1)
    pdf.setFont("Helvetica", 11)
    if kind == "article":
        for y in (760, 650):
            pdf.drawString(50, y, "Repeated title")
            pdf.drawString(50, y - 20, "Repeated original body.")
            pdf.linkURL("https://example.org/shared", (48, y - 2, 270, y + 14), relative=0)
    else:
        pdf.drawString(50, 760, "Key Dates")
        pdf.drawString(50, 740, "DATE(S) EVENT HOST RELEVANCE")
        for y in (700, 600):
            for offset, line in ((0, "4 September 2026"), (20, "EVENT"), (40, "Shared conference"), (60, "Identical source row.")):
                pdf.drawString(50, y - offset, line)
            pdf.linkURL("https://example.org/shared", (48, y - 42, 270, y - 26), relative=0)
    pdf.save()
    bundle = import_pdf_reports([path])
    legacy = deepcopy(bundle)
    legacy.pop("source_coverage")
    items = legacy["articles"][0]["occurrences"] if kind == "article" else legacy["calendar_items"]
    assert len(items) == 2
    for index, item in enumerate(items):
        item["occurrence_id"] = f"legacy-{kind}-{index}"
        item.pop("link_rects" if kind == "article" else "source_region")
    if kind == "calendar":
        assert items[0]["raw_text"] == items[1]["raw_text"]
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    persist_pdf_intake(database, tmp_path / "backups", legacy)
    before = database.read_bytes()
    with pytest.raises(RegistryInputError, match="no unique source rectangle|source evidence is ambiguous"):
        persist_pdf_intake(database, tmp_path / "backups", bundle)
    assert database.read_bytes() == before
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM pdf_source_coverage").fetchone()[0] == 0
    finally:
        connection.close()


def test_registry_v13_reimport_preserves_occurrence_ids_and_raw_metadata(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "report.pdf"
    _report_pdf(source, include_article=True)
    bundle = import_pdf_reports([source])
    document = bundle["documents"][0]
    database = tmp_path / "registry.sqlite3"
    import sqlite3

    with sqlite3.connect(database) as connection:
        apply_migrations(connection, target_version=13)
        connection.execute(
            """INSERT INTO pdf_intake_documents (
                document_sha256, source_path, filename, media_type, size_bytes,
                date_of_run, period_start, period_end, extracted_text_sha256,
                document_json, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)""",
            (document["source"]["sha256"], document["source"]["path"],
             document["source"]["filename"], "application/pdf", document["source"]["size_bytes"],
             document["date_of_run"], document["period_start"], document["period_end"],
             document["extracted_text_sha256"], "2026-09-01T00:00:00Z"),
        )
        article = bundle["articles"][0]
        occurrence = article["occurrences"][0]
        legacy_article_id = "legacy-v13-article-occurrence"
        connection.execute(
            """INSERT INTO pdf_intake_articles (
                article_id, canonical_url, title, type_safe_classification_json, imported_at
            ) VALUES (?, ?, ?, NULL, ?)""",
            (article["article_id"], article["canonical_url"], article["title"],
             "2026-09-01T00:00:00Z"),
        )
        legacy_occurrence = dict(occurrence, occurrence_id=legacy_article_id)
        connection.execute(
            """INSERT INTO pdf_intake_article_occurrences (
                occurrence_id, article_id, source_document_sha256, page, raw_url,
                report_date, publication_date, content_sha256, page_sha256, occurrence_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (legacy_article_id, article["article_id"], occurrence["source_document_sha256"],
             occurrence["page"], occurrence["raw_url"], occurrence["report_date"],
             occurrence["publication_date"], occurrence["content_sha256"],
             occurrence["page_sha256"], json.dumps(legacy_occurrence)),
        )
        item = bundle["calendar_items"][0]
        legacy_calendar_id = "legacy-v13-calendar-occurrence"
        legacy_item = dict(item, occurrence_id=legacy_calendar_id)
        connection.execute(
            """INSERT INTO pdf_intake_calendar_items (
                occurrence_id, event_id, source_document_sha256, page, name, kind, raw_date,
                date_precision, start_date, end_date, summary, content_sha256,
                type_safe_classification_json, item_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (legacy_calendar_id, item["event_id"], item["source_document_sha256"],
             item["page"], item["name"], item["kind"], item["raw_date"],
             item["date_precision"], item["start_date"], item["end_date"], item["summary"],
             item["content_sha256"], json.dumps(legacy_item)),
        )
        connection.commit()

    connection.close()

    from climate_registry.pdf_intake import persist_pdf_intake

    before = database.read_bytes()
    from climate_registry.errors import RegistryInputError
    with pytest.raises(RegistryInputError, match="migrate-publication"):
        persist_pdf_intake(database, tmp_path / "backups", bundle)
    assert database.read_bytes() == before
    from climate_registry.publication import migrate_publication
    migrate_publication(database, tmp_path / "migration-backups", apply=True)
    persist_pdf_intake(database, tmp_path / "backups", bundle)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (26,)
        original_pdf, created_at, modified_at, raw_metadata = connection.execute(
            """SELECT original_pdf, pdf_created_at, pdf_modified_at, pdf_metadata_json
               FROM pdf_intake_documents"""
        ).fetchone()
        article_rows = connection.execute(
            "SELECT occurrence_id, occurrence_json FROM pdf_intake_article_occurrences"
        ).fetchall()
        calendar_rows = connection.execute(
            "SELECT occurrence_id, item_json FROM pdf_intake_calendar_items"
        ).fetchall()
    assert original_pdf == source.read_bytes()
    assert created_at == document["source"]["pdf_created_at"]
    assert modified_at == document["source"]["pdf_modified_at"]
    assert json.loads(raw_metadata) == document["source"]["pdf_metadata"]
    assert len(article_rows) == len(calendar_rows) == 1
    assert article_rows[0][0] == json.loads(article_rows[0][1])["occurrence_id"] == legacy_article_id
    assert calendar_rows[0][0] == json.loads(calendar_rows[0][1])["occurrence_id"] == legacy_calendar_id
    from climate_registry.read_api import RegistryReader
    coverage = RegistryReader(database, repository_root=tmp_path / "application", public=False).pdf_source_coverage(document["source"]["sha256"])
    occurrence_bindings = coverage["history"][0]["registry_occurrence_bindings"]
    assert occurrence_bindings[bundle["articles"][0]["occurrences"][0]["occurrence_id"]] == legacy_article_id
    assert occurrence_bindings[bundle["calendar_items"][0]["occurrence_id"]] == legacy_calendar_id


def test_article_classification_retry_does_not_replace_existing_label(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "report.pdf"
    _report_pdf(source, include_article=True)
    first = import_pdf_reports([source])
    first["articles"][0]["type_safe_classification"] = {"provider": "typesafe", "label": "event"}
    first["calendar_items"][0]["type_safe_classification"] = {"provider": "typesafe", "label": "event"}
    retry = import_pdf_reports([source])
    retry["articles"][0]["type_safe_classification"] = {"provider": "typesafe", "label": "article"}
    retry["calendar_items"][0]["type_safe_classification"] = {"provider": "typesafe", "label": "article"}
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)

    from climate_registry.pdf_intake import persist_pdf_intake

    persist_pdf_intake(database, tmp_path / "backups", first)
    persist_pdf_intake(database, tmp_path / "backups", retry)

    import sqlite3
    with sqlite3.connect(database) as connection:
        article_classification = connection.execute(
            "SELECT type_safe_classification_json FROM pdf_intake_articles"
        ).fetchone()[0]
        calendar_classification = connection.execute(
            "SELECT type_safe_classification_json FROM pdf_intake_calendar_items"
        ).fetchone()[0]
    assert json.loads(article_classification) == {"provider": "typesafe", "label": "event"}
    assert json.loads(calendar_classification) == {"provider": "typesafe", "label": "event"}


def test_input_directory_finds_uppercase_pdf_suffix(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "Report.PDF"
    _report_pdf(source)

    assert _input_pdfs([tmp_path]) == [source.resolve()]
    assert import_pdf_reports([tmp_path])["documents"][0]["source"]["filename"] == "Report.PDF"


def test_reviewed_legacy_pdf_observation_never_supplies_target_facts():
    from climate_monitor.meeting_fields import MEETING_FIELDS, collected_pdf_meeting, merge_meeting_observations
    from climate_delivery.templates.adapters import _calendar_source_alias_groups
    source = {"event_id": "source", "canonical_pdf_event_id": "target", "occurrence_id": "legacy",
        "source_document_sha256": "a"*64, "verification_status": "partial", "access_status": "accessible",
        "collection_status": "pending", "collected_candidate": None, "summary": "Legacy PDF text",
        "name": "Old name", "location": "Unverified venue", "event_timezone": "UTC", "online_url": "https://old.test/",
        "deadline_type": "registration", "deadline_date": "2099-01-01", "raw_time_text": "Old time",
        "relevance_reason": "Old reason", "raw_date": "Old date", "source_urls": ["https://old.test/"]}
    candidate = {"name": "Target", "timezone": None, "location": None, "online_url": None,
        "deadline_type": None, "deadline_date": None, "raw_time_text": None, "status": "scheduled"}
    target = {"event_id": "target", "canonical_pdf_event_id": "target", "occurrence_id": "current",
        "source_document_sha256": "b"*64, "verification_status": "verified", "collected_candidate": candidate,
        "summary": "Target PDF text", "source_urls": ["https://current.test/"], "checks": [{"verification_status": "verified",
            "website_candidate": candidate, "source_url": "https://current.test/exact"}]}
    original = deepcopy((source, target))
    wrapped, primary = collected_pdf_meeting(source), collected_pdf_meeting(target)
    assert wrapped["event_id"] == wrapped["pdf_event_id"] == "source"
    assert wrapped["collected_candidate"] is None and wrapped["verification_status"] == "partial"
    assert wrapped["pdf_observations"] == [source]
    results = []
    for records in ([wrapped, primary], [primary, wrapped]):
        merged = merge_meeting_observations(records)
        assert len(merged) == 1
        result = merged[0]
        assert {key: result.get(key) for key in MEETING_FIELDS} == {key: primary.get(key) for key in MEETING_FIELDS}
        for key in ("event_id", "summary", "verification_status", "checks", "collected_candidate", "collected_candidate_source_url"):
            assert result.get(key) == primary.get(key)
        assert result["pdf_observations"] == [target, source]
        assert _calendar_source_alias_groups(records) == (("C1", "C2"),)
        results.append(result)
    assert results[0] == results[1] and (source, target) == original


def test_verified_pdf_target_wins_over_unchecked_same_event_occurrence_in_every_order():
    from itertools import permutations
    from climate_monitor.meeting_fields import collected_pdf_meeting, merge_meeting_observations, pdf_meeting_fields
    from climate_registry.read_api import RegistryReader
    from climate_registry.range_reports import _pdf_calendar_payload
    def extract(sha, day, url):
        return _extract_calendar_items({"source": {"sha256": sha*64}, "pages": [{"page": 8,
            "text": f"Key Dates\nDATE(S) EVENT HOST RELEVANCE\n{day}\nEVENT\nClimate conference\nFor insurers.",
            "links": [{"url": url, "anchor_text": "Climate conference"}]}]})[0]
    old = extract("b", "4 September 2026", "https://example.org/event")
    new = extract("c", "5 September 2026", "https://EXAMPLE.ORG/event/?utm_source=pdf")
    source = extract("a", "4 September 2026", "https://example.org/legacy")
    assert old["event_id"] == new["event_id"] and old["occurrence_id"] != new["occurrence_id"]
    for item in (source, old, new):
        item.update(canonical_pdf_event_id=new["event_id"], verification_status="unchecked", collected_candidate=None)
    candidate = {"event_type": "event", "name": "Climate conference", "date_precision": "day",
        "start_date": "2026-09-05", "end_date": "2026-09-05", "status": "scheduled", "timezone": None}
    new.update(verification_status="verified", collected_candidate=candidate,
        checks=[{"verification_status": "verified", "website_candidate": candidate, "source_url": "https://example.org/event"}])
    original = deepcopy((source, old, new))
    primary = collected_pdf_meeting(pdf_meeting_fields(new))
    for values in permutations((source, old, new)):
        rows = [pdf_meeting_fields(item) for item in values]
        merged = merge_meeting_observations([collected_pdf_meeting(item) for item in rows])
        assert len(merged) == 1
        assert {key: value for key, value in merged[0].items() if key != "pdf_observations"} == {
            key: value for key, value in primary.items() if key != "pdf_observations"}
        assert {item["occurrence_id"]: item for item in merged[0]["pdf_observations"]} == {item["occurrence_id"]: item for item in rows}
        reader = RegistryReader.__new__(RegistryReader)
        reader.database, reader.public, reader.static_snapshot = None, True, {"meetings": rows}
        assert len(_pdf_calendar_payload(reader, base_date="2026-09-05")["records"]) == 1
    assert (source, old, new) == original
