from __future__ import annotations

import hashlib
import json
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
    _typesafe_classify,
    import_pdf_reports,
    main,
)
from climate_registry.persistent import initialize_registry


def _report_pdf(path, calendar_date="4–5 Sep 2026"):
    output = BytesIO()
    canvas = Canvas(output, pagesize=letter)
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
    canvas.drawString(50, 500, "Publisher contact")
    canvas.linkURL("https://example.org/publisher", (90, 496, 135, 512), relative=0)
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
    article = bundle["articles"][0]
    assert article["canonical_url"] == "https://example.org/climate-risk-conference"
    assert article["title"] == "Climate risk conference"
    assert article["occurrences"][0]["source_document_sha256"] == document["source"]["sha256"]
    assert article["occurrences"][0]["content_sha256"]


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

    assert bundle["articles"][0]["canonical_url"] == "https://example.org/climate-risk-conference"
    assert bundle["calendar_items"][0]["type_safe_classification"] == {
        "provider": "typesafe", "label": "event",
    }


def test_cli_requires_apply_before_writing_output(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    source = tmp_path / "source.pdf"
    output = tmp_path / "bundle.json"
    _report_pdf(source)

    with pytest.raises(SystemExit) as error:
        main(["--input", str(source), "--output", str(output)])

    assert error.value.code == 2
    assert not output.exists()


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
        assert connection.execute("PRAGMA user_version").fetchone() == (13,)
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_documents").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_articles").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_article_occurrences").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM pdf_intake_calendar_items").fetchone() == (1,)
        document_json = connection.execute("SELECT document_json FROM pdf_intake_documents").fetchone()[0]
        assert "Climate summary preserved exactly" in document_json
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
            assert connection.execute("PRAGMA user_version").fetchone() == (13,)
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
