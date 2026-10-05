"""Recover the actual calendar columns without changing imported evidence."""
import hashlib
import json
import sqlite3
from copy import deepcopy

import pytest
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

from climate_monitor.pdf_intake import _read_pdf, recover_calendar_fields, report_update_fields


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
    item = RegistryReader(database, repository_root=tmp_path / "application").pdf_calendar_items()["items"][0]
    assert item["name"] == recovered["name"] and item["publisher"] == recovered["publisher"]
    assert item["relevance"] == recovered["relevance"] and item["raw_text"] == old["raw_text"]
    assert database.read_bytes() == before


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
