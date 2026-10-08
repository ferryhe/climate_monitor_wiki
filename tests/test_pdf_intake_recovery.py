"""Recover the actual calendar columns without changing imported evidence."""
import hashlib
import json
import sqlite3
from copy import deepcopy

import pytest
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

from climate_monitor.pdf_intake import _read_pdf, recover_calendar_fields, report_update_fields


def _calendar_pdf(path, name, date_text):
    style = getSampleStyleSheet()["BodyText"]
    cells = [date_text + "<br/>EVENT", name, "Example Institute", "Climate risk and insurance research."]
    table = Table([["DATE(S)", "EVENT", "HOST", "RELEVANCE"],
                   [Paragraph(value, style) for value in cells]], colWidths=[95, 155, 100, 165])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .5, "black"),
                              ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    SimpleDocTemplate(str(path)).build([Paragraph("Key Dates", style), table])


def test_calendar_geometry_recovers_full_cells_for_new_and_existing_imports(tmp_path):
    path = tmp_path / "calendar.pdf"
    style = getSampleStyleSheet()["BodyText"]
    cells = ["5-8 Oct 2026<br/>EVENT", "Climate finance<br/>ministerial meeting",
             "UNFCCC / Fiji", "Adaptation finance<br/>and loss and damage."]
    table = Table([["DATE(S)", "EVENT", "HOST", "RELEVANCE"],
                   [Paragraph(value, style) for value in cells]], colWidths=[95, 155, 100, 165])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .5, "black"),
                              ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    SimpleDocTemplate(str(path)).build([Paragraph("Key Dates", style), table])
    document = _read_pdf(path)
    assert len(document["calendar_items"]) == 1
    recovered = document["calendar_items"][0]
    assert recovered["name"] == "Climate finance ministerial meeting"
    assert recovered["publisher"] == "UNFCCC / Fiji"
    assert recovered["relevance"] == "Adaptation finance and loss and damage."
    assert (recovered["start_date"], recovered["end_date"]) == ("2026-10-05", "2026-10-08")

    old = {key: value for key, value in recovered.items()
           if key not in {"publisher", "relevance", "calendar_field_basis"}}
    old["name"] = "Climate finance"
    original = deepcopy(old)
    assert recover_calendar_fields(path.read_bytes(), [old])[0] == recovered
    assert old == original
    mismatch = dict(old, raw_text="A different row with the same date")
    assert recover_calendar_fields(path.read_bytes(), [mismatch]) == [mismatch]
    with pytest.raises(ValueError, match="source document"):
        recover_calendar_fields(b"different PDF bytes", [old])

    # A read of an older saved observation derives fields without writing the DB.
    from climate_registry.persistent import initialize_registry
    from climate_registry.read_api import RegistryReader
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(database) as connection:
        connection.execute("""INSERT INTO pdf_intake_documents(
            document_sha256, source_path, filename, media_type, size_bytes, original_pdf,
            extracted_text_sha256, document_json, imported_at)
            VALUES (?, ?, 'calendar.pdf', 'application/pdf', ?, ?, ?, '{}', '2026-10-04')""",
            (digest, str(path), path.stat().st_size, path.read_bytes(), "a" * 64))
        connection.execute("""INSERT INTO pdf_intake_calendar_items VALUES
            (?, ?, ?, 1, ?, 'event', ?, 'day', ?, ?, ?, ?, NULL, ?)""",
            (old["event_id"], old["occurrence_id"], digest, old["name"], old["raw_date"],
             old["start_date"], old["end_date"], old["summary"], old["content_sha256"], json.dumps(old)))
    before = database.read_bytes()
    item = RegistryReader(database, public=False, repository_root=tmp_path / "application").pdf_calendar_items()["items"][0]
    assert item["name"] == recovered["name"] and item["publisher"] == recovered["publisher"]
    assert item["relevance"] == recovered["relevance"] and item["raw_text"] == old["raw_text"]
    assert database.read_bytes() == before


def test_full_calendar_read_recovers_only_allow_listed_pdfs_once(tmp_path, monkeypatch):
    from climate_monitor.pdf_intake import import_pdf_reports
    from climate_registry.persistent import initialize_registry
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_registry.read_api import RegistryReader
    import climate_monitor.pdf_intake as pdf_intake

    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    occurrence_ids = []
    for index, (name, date_text) in enumerate((
        ("Climate risk meeting one", "5-8 Oct 2026"),
        ("Climate risk meeting two", "6-9 Oct 2026"),
        ("Climate risk meeting three", "7-10 Oct 2026"),
    )):
        path = tmp_path / f"calendar-{index}.pdf"
        _calendar_pdf(path, name, date_text)
        bundle = import_pdf_reports([path])
        occurrence_ids.extend(item["occurrence_id"] for item in bundle["calendar_items"])
        persist_pdf_intake(database, tmp_path / "backups", bundle)

    # Make one PDF exceed the old 100-row read page so meeting assembly proves
    # it recovers each original once for the complete authorized set.
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT * FROM pdf_intake_calendar_items ORDER BY rowid LIMIT 1"
        ).fetchone()
        names = [column[1] for column in connection.execute("PRAGMA table_info(pdf_intake_calendar_items)")]
        original_item = json.loads(row[names.index("item_json")])
        for index in range(100):
            synthetic = dict(original_item)
            identity = hashlib.sha256(f"extra-calendar-{index}".encode()).hexdigest()[:24]
            synthetic.update({
                "occurrence_id": f"pdf-event-occurrence-{identity}",
                "event_id": f"event-{identity}",
                "name": f"Additional climate meeting {index}",
                "raw_text": f"Synthetic calendar observation {index}",
                "summary": f"Synthetic calendar observation {index}",
                "content_sha256": hashlib.sha256(f"extra-{index}".encode()).hexdigest(),
            })
            connection.execute(
                """INSERT INTO pdf_intake_calendar_items(
                       occurrence_id, event_id, source_document_sha256, page, name, kind,
                       raw_date, date_precision, start_date, end_date, summary, content_sha256,
                       type_safe_classification_json, item_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    synthetic["occurrence_id"], synthetic["event_id"],
                    synthetic["source_document_sha256"], synthetic["page"], synthetic["name"],
                    synthetic["kind"], synthetic["raw_date"], synthetic["date_precision"],
                    synthetic.get("start_date"), synthetic.get("end_date"), synthetic["summary"],
                    synthetic["content_sha256"], None,
                    json.dumps(synthetic, ensure_ascii=False, sort_keys=True),
                ),
            )

    calls = []
    original = pdf_intake.recover_calendar_fields

    def count_recovery(raw_pdf, items):
        calls.append((hashlib.sha256(raw_pdf).hexdigest(), tuple(item["occurrence_id"] for item in items)))
        return original(raw_pdf, items)

    monkeypatch.setattr(pdf_intake, "recover_calendar_fields", count_recovery)
    reader = RegistryReader(database, public=False, repository_root=tmp_path / "application")
    selected = reader.pdf_calendar_items_all(allowed_occurrence_ids={occurrence_ids[1]})
    assert len(selected) == 1 and selected[0]["occurrence_id"] == occurrence_ids[1]
    assert len(calls) == 1 and calls[0][1] == (occurrence_ids[1],)

    calls.clear()
    meetings = reader.meetings(page_size=20, base_date="2026-10-01")
    assert meetings["pagination"]["total"] == 103
    assert len(calls) == 3 and sorted(len(items) for _, items in calls) == [1, 1, 101]


def test_update_fields_keep_summary_and_caveat_but_remove_report_badges():
    title, url = "Climate finance update", "https://example.org/article"
    document = {"pages": [{"page": 1, "text": "Example Institution\nWebsite: https://example.org\n" + title,
                          "links": [{"url": url, "anchor_text": title}]}]}
    occurrence = {"page": 1, "raw_url": url, "publication_date_evidence": "21 SEP 2026",
        "summary": title + "\nIN WINDOW 21 SEP 2026 REPORT\nStored source summary.\n\nCaveat: verify assumptions.\nSource: Example"}
    result = report_update_fields(occurrence, document)
    assert result["title"] == title and result["publisher"] == "Example Institution"
    assert "Stored source summary." in result["summary"]
    assert "Caveat: verify assumptions." in result["summary"]
    assert "IN WINDOW" not in result["summary"] and "Source: Example" not in result["summary"]
    assert report_update_fields(dict(occurrence, summary="Example website metadata"), document) is None
