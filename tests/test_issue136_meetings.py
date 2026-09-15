from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import types
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from climate_monitor.meetings import (
    freeze_snapshot,
    load_snapshot,
    meeting_status,
    process_batch,
    query_events,
    validate_extraction,
)
from climate_registry.contract import validate_registry_contract
from climate_registry.acquisition import PublicationDatePolicy, store_acquisition_batch
from climate_registry.schema import apply_migrations


def _database(tmp_path, bodies):
    database = tmp_path / "registry.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection)
    connection.execute("INSERT INTO sources VALUES ('source', 'example.com', 'Example', '2026-01-01', '2026-01-01')")
    connection.execute(
        "INSERT INTO acquisition_batches VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("batch", "pre-report-acquisition-batch.v1", "2026-01-05", "2026-01-05T00:00:00Z",
         "2026-01-05T00:10:00Z", '{"mode":"unlimited"}', "no_search", "site-only",
         "a" * 64, "2026-01-05T00:10:00Z"),
    )
    for ordinal, body in enumerate(bodies, 1):
        article_id, content_id, fetch_id = f"article-{ordinal}", f"content-{ordinal}", f"fetch-{ordinal}"
        url = f"https://example.com/{ordinal}"
        digest = hashlib.sha256(body.encode()).hexdigest()
        connection.execute(
            """INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen)
               VALUES (?, ?, 'source', '2026-01-01', '2026-01-01')""",
            (article_id, url),
        )
        connection.execute(
            """INSERT INTO article_content_versions VALUES
               (?, ?, ?, ?, ?, 'text/markdown', ?, 'reader', 'v1', '2026-01-01T00:00:00Z')""",
            (content_id, article_id, digest, body, digest, len(body.encode())),
        )
        connection.execute(
            "UPDATE articles SET current_content_version_id=? WHERE article_id=?",
            (content_id, article_id),
        )
        connection.execute(
            """INSERT INTO article_fetches(fetch_id, article_id, requested_url, final_url, fetched_at,
               fetch_status, http_status, content_type, content_version_id)
               VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z', 'success', 200, 'text/markdown', ?)""",
            (fetch_id, article_id, url, url, content_id),
        )
        connection.execute(
            """INSERT INTO acquisition_items(
               acquisition_item_id, batch_id, ordinal, article_id, raw_url, source_name, title,
               summary, discovered_at, discovery_kind, discovery_ref, origins_json, date_status,
               selection_status, selection_reason, update_status, material_status, fetch_id,
               content_version_id, attempts_json, processing_status)
               VALUES (?, 'batch', ?, ?, ?, 'Example', ?, '', '2026-01-01T00:00:00Z', 'site',
               ?, '[]', 'outside_window', 'unselected', 'old publication', 'baseline',
               'full_content', ?, ?, '[]', 'complete')""",
            (f"item-{ordinal}", ordinal, article_id, url, f"Title {ordinal}", f"ref-{ordinal}",
             fetch_id, content_id),
        )
    connection.commit()
    connection.close()
    return database


def _candidate(**changes):
    value = {
        "name": "World Climate Summit 2027", "event_type": "summit", "organizer": "Example",
        "status": "scheduled", "date_precision": "day", "start_date": "2027-06-10",
        "end_date": "2027-06-12", "raw_time_text": "June 10–12, 2027",
        "timezone": None, "location": "New York", "online_url": "https://example.com/register",
        "deadline_type": "registration", "deadline_date": "2027-05-01",
        "date_evidence": "June 10–12, 2027", "deadline_evidence": "May 1, 2027",
        "status_evidence": None, "relevance_reason": "Climate risk agenda",
    }
    value.update(changes)
    return value


def _add_unavailable_item(database):
    connection = sqlite3.connect(database)
    connection.execute(
        """INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen)
           VALUES ('article-missing', 'https://example.com/missing', 'source', '2026-01-01', '2026-01-01')"""
    )
    connection.execute(
        """INSERT INTO article_fetches(
           fetch_id, article_id, requested_url, fetched_at, fetch_status, error_code, error_message)
           VALUES ('fetch-missing', 'article-missing', 'https://example.com/missing',
                   '2026-01-01T00:00:00Z', 'failed', 'reader_failed', 'body unavailable')"""
    )
    ordinal = connection.execute(
        "SELECT coalesce(max(ordinal), 0) + 1 FROM acquisition_items WHERE batch_id='batch'"
    ).fetchone()[0]
    connection.execute(
        """INSERT INTO acquisition_items(
           acquisition_item_id, batch_id, ordinal, article_id, raw_url, source_name, title,
           summary, discovered_at, discovery_kind, discovery_ref, origins_json, date_status,
           selection_status, selection_reason, update_status, material_status, fetch_id,
           content_version_id, attempts_json, processing_status, processing_error)
           VALUES ('item-missing', 'batch', ?, 'article-missing', 'https://example.com/missing',
                   'Example', 'Missing', '', '2026-01-01T00:00:00Z', 'site', 'ref-missing',
                   '[]', 'unknown_pending_review', 'unselected', 'body unavailable', 'failed',
                   'error', 'fetch-missing', NULL, '[]', 'failed', 'body unavailable')""",
        (ordinal,),
    )
    connection.commit()
    connection.close()


def _stored_item(url, body, observed):
    digest = hashlib.sha256(body.encode()).hexdigest()
    return {
        "url": url, "title": "Article", "summary": "Summary", "source": "Example",
        "discovered_at": observed, "discovery_kind": "site", "discovery_ref": f"site:{url}",
        "discovery_search_ref": None, "published_date": observed[:10],
        "publication_date_evidence": {
            "kind": "publisher", "url": url, "text": f"Published {observed[:10]}",
        },
        "selected": True, "selection_reason": "relevant", "processing_status": "complete",
        "processing_error": None,
        "evidence": {
            "status": "ok", "fetched_at": observed, "final_url": url,
            "attempts": [{"engine": "web_http", "status": "success", "http_status": 200,
                          "attempted_at": observed}],
            "selected_method": "web_http", "content_type": "text/markdown", "content": body,
            "content_hash": digest, "content_ref": f"managed/{digest}.md",
            "raw_snapshot_ref": f"managed/{digest}.html", "raw_snapshot_sha256": digest,
            "classification": "full_content", "failure_reason": None, "http_status": 200,
        },
    }


def _stored_batch(batch_id, report_date, observed, item):
    policy = PublicationDatePolicy.resolve(
        None, anchor_date=date.fromisoformat(report_date), frozen_at=observed,
    )
    return {
        "schema_version": "pre-report-acquisition-batch.v1", "batch_id": batch_id,
        "report_date": report_date, "started_at": observed, "completed_at": observed,
        "date_policy": policy.to_dict(),
        "search_decision": {"status": "no_search", "reason": "site-only"},
        "searches": [], "items": [item],
    }


def test_strict_extraction_rejects_metadata_dates_and_unsupported_fields():
    body = "World Climate Summit 2027. Published June 10, 2027."
    candidate = _candidate(
        start_date="2027-06-10", end_date="2027-06-10", raw_time_text="Published June 10, 2027",
        date_evidence="Published June 10, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    with pytest.raises(ValueError, match="publication"):
        validate_extraction({"events": [candidate]}, body=body)
    truncated = {**candidate, "raw_time_text": "June 10, 2027", "date_evidence": "June 10, 2027"}
    with pytest.raises(ValueError, match="publication"):
        validate_extraction({"events": [truncated]}, body=body)
    article_date_body = "World Climate Summit 2027. Article   date: June 10, 2027."
    with pytest.raises(ValueError, match="publication"):
        validate_extraction({"events": [truncated]}, body=article_date_body)
    with pytest.raises(ValueError, match="unexpected"):
        validate_extraction({"events": [{**candidate, "event_id": "model-chosen"}]}, body=body)

    repeated = (
        "Published June 10, 2027. World Climate Summit 2027 meets on June 10, 2027."
    )
    accepted = {**truncated, "organizer": None}
    assert validate_extraction({"events": [accepted]}, body=repeated)[0]["start_date"] == "2027-06-10"
    repeated_article_date = (
        "ARTICLE DATE: June 10, 2027. World Climate Summit 2027 meets on June 10, 2027."
    )
    assert validate_extraction(
        {"events": [accepted]}, body=repeated_article_date,
    )[0]["start_date"] == "2027-06-10"


@pytest.mark.parametrize(
    ("status", "other_status"),
    [("tentative", "postponed"), ("postponed", "cancelled"), ("cancelled", "tentative")],
)
def test_status_evidence_requires_the_candidate_status_in_its_matching_context(status, other_status):
    body = (
        f"Example says World Climate Summit 2027 is {other_status} and was planned for "
        "June 10, 2027."
    )
    candidate = _candidate(
        status=status, status_evidence="World Climate Summit 2027",
        start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
        date_evidence="June 10, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    with pytest.raises(ValueError, match="status_evidence"):
        validate_extraction({"events": [candidate]}, body=body)


@pytest.mark.parametrize(
    ("status", "wording"),
    [
        ("tentative", "is tentatively scheduled"),
        ("postponed", "has been postponed"),
        ("cancelled", "has been cancelled"),
    ],
)
def test_status_evidence_accepts_matching_status_context(status, wording):
    body = f"Example says World Climate Summit 2027 {wording} for June 10, 2027."
    candidate = _candidate(
        status=status, status_evidence="World Climate Summit 2027",
        start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
        date_evidence="June 10, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    assert validate_extraction({"events": [candidate]}, body=body)[0]["status"] == status


def test_status_evidence_accepts_one_valid_occurrence_among_repeated_text():
    body = (
        "World Climate Summit 2027 appears in the archive. "
        "Example confirms World Climate Summit 2027 has been cancelled."
    )
    candidate = _candidate(
        status="cancelled", status_evidence="World Climate Summit 2027",
        date_precision="unknown", start_date=None, end_date=None, raw_time_text=None,
        date_evidence=None, location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    assert validate_extraction({"events": [candidate]}, body=body)[0]["status"] == "cancelled"


def test_status_evidence_cannot_borrow_status_across_another_subject_clause():
    body = (
        "Example says World Climate Summit 2027 remains scheduled while "
        "Other Climate Forum has been cancelled."
    )
    candidate = _candidate(
        status="cancelled", status_evidence="World Climate Summit 2027",
        date_precision="unknown", start_date=None, end_date=None, raw_time_text=None,
        date_evidence=None, location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    with pytest.raises(ValueError, match="status_evidence"):
        validate_extraction({"events": [candidate]}, body=body)
    with pytest.raises(ValueError, match="status_evidence"):
        validate_extraction({"events": [{
            **candidate, "status_evidence": body.removesuffix("."),
        }]}, body=body)

    reverse = (
        "Example says World Climate Summit 2027 has been cancelled but "
        "Other Climate Forum is postponed."
    )
    with pytest.raises(ValueError, match="status_evidence"):
        validate_extraction({"events": [{
            **candidate, "name": "Other Climate Forum", "status": "cancelled",
            "status_evidence": "Other Climate Forum",
        }]}, body=reverse)

    shared = "Example confirms World Climate Summit 2027 and Climate Forum are cancelled."
    accepted = {**candidate, "status_evidence": "are cancelled"}
    assert validate_extraction({"events": [accepted]}, body=shared)[0]["status"] == "cancelled"


@pytest.mark.parametrize("label", ["Last modified", "Last   modified", "Last update", "Last updated"])
def test_last_modified_metadata_date_needs_a_separate_event_occurrence(label):
    candidate = _candidate(
        start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
        date_evidence="June 10, 2027", organizer=None, location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    metadata_only = f"World Climate Summit 2027. {label}: June 10, 2027."
    with pytest.raises(ValueError, match="publication"):
        validate_extraction({"events": [candidate]}, body=metadata_only)
    event_label = metadata_only + " Event dates: June 10, 2027."
    assert validate_extraction({"events": [candidate]}, body=event_label)[0]["start_date"] == "2027-06-10"


@pytest.mark.parametrize("connector", ["while", "but", "whereas"])
def test_event_date_cannot_borrow_event_context_across_deadline_clause(connector):
    body = (
        f"Example says World Climate Summit 2027 remains scheduled {connector} "
        "registration deadline is May 1, 2027."
    )
    candidate = _candidate(
        start_date="2027-05-01", end_date=None, raw_time_text="May 1, 2027",
        date_evidence="May 1, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    with pytest.raises(ValueError, match="deadline dates"):
        validate_extraction({"events": [candidate]}, body=body)


@pytest.mark.parametrize(
    "body",
    [
        "World Climate Summit 2027. Event dates: June 10, 2027.",
        "World Climate Summit 2027 — June 10, 2027.",
        "World Climate Summit 2027 | June 10, 2027 | Online.",
    ],
)
def test_event_date_labels_titles_and_tables_remain_valid(body):
    candidate = _candidate(
        organizer=None, start_date="2027-06-10", end_date=None,
        raw_time_text="June 10, 2027", date_evidence="June 10, 2027",
        location=None, online_url=None, deadline_type=None, deadline_date=None,
        deadline_evidence=None,
    )
    assert validate_extraction({"events": [candidate]}, body=body)[0]["start_date"] == "2027-06-10"


def test_same_sentence_duplicate_date_accepts_the_real_event_occurrence():
    body = (
        "Registration deadline is June 10, 2027 while Example holds "
        "World Climate Summit 2027 on June 10, 2027."
    )
    candidate = _candidate(
        start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
        date_evidence="June 10, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    assert validate_extraction({"events": [candidate]}, body=body)[0]["start_date"] == "2027-06-10"


def test_event_and_deadline_evidence_cannot_cross_alpha_beta_activity_subjects():
    event_body = (
        "Alpha Climate Summit 2027 has dates to be confirmed while "
        "Beta Climate Summit 2027 is on June 10, 2027."
    )
    event = _candidate(
        name="Alpha Climate Summit 2027", organizer=None, start_date="2027-06-10",
        end_date=None, raw_time_text="June 10, 2027", date_evidence="June 10, 2027",
        location=None, online_url=None, deadline_type=None, deadline_date=None,
        deadline_evidence=None,
    )
    with pytest.raises(ValueError, match="event-date evidence"):
        validate_extraction({"events": [event]}, body=event_body)

    deadline_body = (
        "Alpha Climate Summit 2027 has registration dates to be confirmed while "
        "Beta Climate Summit 2027 registration closes May 1, 2027."
    )
    deadline = {
        **event, "start_date": None, "raw_time_text": None, "date_precision": "unknown",
        "date_evidence": None, "deadline_type": "registration",
        "deadline_date": "2027-05-01", "deadline_evidence": "May 1, 2027",
    }
    with pytest.raises(ValueError, match="deadline_evidence"):
        validate_extraction({"events": [deadline]}, body=deadline_body)


def test_nonstandard_activity_names_cannot_cross_borrow_dates():
    event_body = (
        "COP30. Registration deadline is June 10, 2027. "
        "Climate Week NYC is on June 10, 2027."
    )
    event = _candidate(
        name="COP30", organizer=None, start_date="2027-06-10", end_date=None,
        raw_time_text="June 10, 2027", date_evidence="June 10, 2027",
        location=None, online_url=None, deadline_type=None, deadline_date=None,
        deadline_evidence=None,
    )
    with pytest.raises(ValueError, match="event-date evidence"):
        validate_extraction({"events": [event]}, body=event_body)

    deadline_body = (
        "COP30 is on July 20, 2027. "
        "Climate Week NYC registration deadline is June 10, 2027."
    )
    deadline = {
        **event, "start_date": "2027-07-20", "raw_time_text": "July 20, 2027",
        "date_evidence": "July 20, 2027", "deadline_type": "registration",
        "deadline_date": "2027-06-10", "deadline_evidence": "June 10, 2027",
    }
    with pytest.raises(ValueError, match="deadline_evidence"):
        validate_extraction({"events": [deadline]}, body=deadline_body)


@pytest.mark.parametrize("separator", [" / ", "; ", " | "])
def test_transparent_label_chain_stops_at_another_activity(tmp_path, separator):
    body = separator.join([
        "Alpha Climate Summit 2027", "Event details", "Beta Climate Summit 2027",
        "Dates", "June 10, 2027",
    ]) + "."
    common = dict(
        organizer=None, start_date="2027-06-10", end_date=None,
        raw_time_text="June 10, 2027", date_evidence="June 10, 2027",
        location=None, online_url=None, deadline_type=None, deadline_date=None,
        deadline_evidence=None,
    )
    with pytest.raises(ValueError, match="event-date evidence"):
        validate_extraction(
            {"events": [_candidate(name="Alpha Climate Summit 2027", **common)]}, body=body,
        )

    beta = _candidate(name="Beta Climate Summit 2027", **common)
    assert validate_extraction({"events": [beta]}, body=body)[0]["_single_day_supported"] is True
    database = _database(tmp_path, [body])
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [beta]},
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert [record["name"] for record in records] == ["Beta Climate Summit 2027"]


@pytest.mark.parametrize("separator", [" / ", "; ", " | "])
@pytest.mark.parametrize("swapped", [False, True])
@pytest.mark.parametrize("field", ["event", "deadline", "status"])
def test_bidirectional_evidence_binding_stops_at_another_subject(separator, swapped, field):
    if field == "deadline":
        alpha = "Alpha Meeting registration closes May 1, 2027"
    elif field == "status":
        alpha = "Alpha Meeting is cancelled"
    else:
        alpha = "Alpha Meeting is on May 1, 2027"
    beta = "Beta Meeting is on June 1, 2027"
    body = separator.join(([beta, alpha] if swapped else [alpha, beta])) + "."
    candidate = _candidate(
        name="Beta Meeting", event_type="meeting", organizer=None,
        start_date="2027-05-01" if field == "event" else "2027-06-01", end_date=None,
        raw_time_text="May 1, 2027" if field == "event" else "June 1, 2027",
        date_evidence="May 1, 2027" if field == "event" else "June 1, 2027",
        status="cancelled" if field == "status" else "scheduled",
        status_evidence="is cancelled" if field == "status" else None,
        date_precision="day", location=None, online_url=None,
        deadline_type="registration" if field == "deadline" else None,
        deadline_date="2027-05-01" if field == "deadline" else None,
        deadline_evidence="May 1, 2027" if field == "deadline" else None,
    )
    with pytest.raises(ValueError, match={
        "event": "event-date evidence", "deadline": "deadline_evidence",
        "status": "candidate status",
    }[field]):
        validate_extraction({"events": [candidate]}, body=body)


def test_reverse_table_date_cell_binds_to_following_candidate():
    body = "June 1, 2027 | Beta Meeting"
    candidate = _candidate(
        name="Beta Meeting", event_type="meeting", organizer=None,
        start_date="2027-06-01", end_date=None, raw_time_text="June 1, 2027",
        date_evidence="June 1, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    assert validate_extraction({"events": [candidate]}, body=body)[0]["_single_day_supported"] is True


@pytest.mark.parametrize("separator", [" / ", "; ", " | "])
@pytest.mark.parametrize("swapped", [False, True])
@pytest.mark.parametrize("field", ["event", "deadline", "status"])
def test_evidence_cell_owned_on_left_cannot_bind_later_subject(separator, swapped, field):
    owner, wrong = ("Beta Meeting", "Alpha Meeting") if swapped else ("Alpha Meeting", "Beta Meeting")
    if field == "event":
        cells = [owner, "May 1, 2027", wrong]
        candidate = _candidate(
            name=wrong, event_type="meeting", organizer=None,
            start_date="2027-05-01", end_date=None, raw_time_text="May 1, 2027",
            date_evidence="May 1, 2027", location=None, online_url=None,
            deadline_type=None, deadline_date=None, deadline_evidence=None,
        )
        error = "event-date evidence"
    elif field == "deadline":
        cells = [owner, "Registration deadline", "May 1, 2027", wrong]
        candidate = _candidate(
            name=wrong, event_type="deadline", organizer=None, date_precision="unknown",
            start_date=None, end_date=None, raw_time_text=None, date_evidence=None,
            location=None, online_url=None, deadline_type="registration",
            deadline_date="2027-05-01", deadline_evidence="May 1, 2027",
        )
        error = "deadline_evidence"
    else:
        cells = [owner, "cancelled", wrong]
        candidate = _candidate(
            name=wrong, event_type="meeting", organizer=None, status="cancelled",
            status_evidence="cancelled", date_precision="unknown", start_date=None,
            end_date=None, raw_time_text=None, date_evidence=None, location=None,
            online_url=None, deadline_type=None, deadline_date=None, deadline_evidence=None,
        )
        error = "candidate status"
    with pytest.raises(ValueError, match=error):
        validate_extraction({"events": [candidate]}, body=separator.join(cells))


def test_rejected_status_candidate_does_not_change_existing_event(tmp_path):
    body = (
        "Beta Meeting is on June 1, 2027. "
        "Alpha Meeting | cancelled | Beta Meeting"
    )
    database = _database(tmp_path, [body])
    scheduled = _candidate(
        name="Beta Meeting", event_type="meeting", organizer=None,
        start_date="2027-06-01", end_date=None, raw_time_text="June 1, 2027",
        date_evidence="June 1, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [scheduled]},
    )
    before = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"][0]
    rejected = {
        **scheduled, "status": "cancelled", "status_evidence": "cancelled",
    }
    failed = process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: {"events": [rejected]},
    )
    assert failed["status"] == "failed"
    after = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"][0]
    assert (after["event_id"], after["status"], after["record_version"]) == (
        before["event_id"], "scheduled", before["record_version"],
    )
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT count(*) FROM climate_event_sources").fetchone() == (1,)
    connection.close()


@pytest.mark.parametrize("opaque", ["Alpha end date TBC", "scheduled Alpha"])
@pytest.mark.parametrize("field", ["event", "deadline", "status"])
def test_transparent_binding_segment_does_not_accept_embedded_subject(opaque, field):
    if field == "event":
        tail = "Event date | May 1, 2027"
        candidate = _candidate(
            name="Beta", event_type="meeting", organizer=None,
            start_date="2027-05-01", end_date=None, raw_time_text="May 1, 2027",
            date_evidence="May 1, 2027", location=None, online_url=None,
            deadline_type=None, deadline_date=None, deadline_evidence=None,
        )
        error = "event-date evidence"
    elif field == "deadline":
        tail = "Registration deadline | May 1, 2027"
        candidate = _candidate(
            name="Beta", event_type="deadline", organizer=None,
            date_precision="unknown", start_date=None, end_date=None,
            raw_time_text=None, date_evidence=None, location=None, online_url=None,
            deadline_type="registration", deadline_date="2027-05-01",
            deadline_evidence="May 1, 2027",
        )
        error = "deadline_evidence"
    else:
        tail = "cancelled"
        candidate = _candidate(
            name="Beta", event_type="meeting", organizer=None, status="cancelled",
            status_evidence="cancelled", date_precision="unknown", start_date=None,
            end_date=None, raw_time_text=None, date_evidence=None, location=None,
            online_url=None, deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )
        error = "candidate status"
    with pytest.raises(ValueError, match=error):
        validate_extraction({"events": [candidate]}, body=f"Beta | {opaque} | {tail}")


def test_transparent_binding_segment_keeps_supported_complete_structures():
    candidate = _candidate(
        name="Beta", event_type="meeting", organizer=None,
        start_date="2027-05-01", end_date=None, raw_time_text="May 1, 2027",
        date_evidence="May 1, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    for body in (
        "Beta | Event details | Event date | May 1, 2027",
        "Beta | end date to be confirmed | Event date | May 1, 2027",
        "Beta | starts May 1, 2027",
    ):
        assert validate_extraction({"events": [candidate]}, body=body)[0]["name"] == "Beta"


@pytest.mark.parametrize("field", ["event", "deadline", "status"])
def test_structural_owner_requires_complete_candidate_name(field):
    if field == "event":
        body = "Alpha Beta Meeting | May 1, 2027 | Beta Meeting"
        candidate = _candidate(
            name="Beta Meeting", event_type="meeting", organizer=None,
            start_date="2027-05-01", end_date=None, raw_time_text="May 1, 2027",
            date_evidence="May 1, 2027", location=None, online_url=None,
            deadline_type=None, deadline_date=None, deadline_evidence=None,
        )
        error = "event-date evidence"
    elif field == "deadline":
        body = "Alpha Beta Meeting | Registration deadline | May 1, 2027 | Beta Meeting"
        candidate = _candidate(
            name="Beta Meeting", event_type="deadline", organizer=None,
            date_precision="unknown", start_date=None, end_date=None,
            raw_time_text=None, date_evidence=None, location=None, online_url=None,
            deadline_type="registration", deadline_date="2027-05-01",
            deadline_evidence="May 1, 2027",
        )
        error = "deadline_evidence"
    else:
        body = "Alpha Beta Meeting | cancelled | Beta Meeting"
        candidate = _candidate(
            name="Beta Meeting", event_type="meeting", organizer=None, status="cancelled",
            status_evidence="cancelled", date_precision="unknown", start_date=None,
            end_date=None, raw_time_text=None, date_evidence=None, location=None,
            online_url=None, deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )
        error = "candidate status"
    with pytest.raises(ValueError, match=error):
        validate_extraction({"events": [candidate]}, body=body)


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("Alpha Beta Meeting", "Alpha Beta Meeting | Event date | May 1, 2027"),
        ("Beta Meeting", "## Beta Meeting (2027) | Event date | May 1, 2027"),
        ("Beta Meeting", "Example hosts Beta   Meeting on May 1, 2027"),
    ],
)
def test_complete_subject_name_allows_title_markers_year_and_whitespace(name, body):
    candidate = _candidate(
        name=name, event_type="meeting", organizer=None,
        start_date="2027-05-01", end_date=None, raw_time_text="May 1, 2027",
        date_evidence="May 1, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    assert validate_extraction({"events": [candidate]}, body=body)[0]["name"] == name


@pytest.mark.parametrize("separator", [". ", " | "])
@pytest.mark.parametrize("wording", ["is rescheduled for", "has a new date of"])
def test_reschedule_context_is_bound_to_candidate_occurrence(separator, wording):
    body = separator.join([
        f"Alpha Meeting {wording} June 1, 2027",
        "Beta Meeting is on June 1, 2027",
    ])
    candidate = _candidate(
        name="Beta Meeting", event_type="meeting", organizer=None,
        start_date="2027-06-01", end_date=None, raw_time_text="June 1, 2027",
        date_evidence="June 1, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    assert validate_extraction({"events": [candidate]}, body=body)[0]["_reschedule_supported"] is False


def test_reschedule_context_accepts_own_bound_occurrence():
    candidate = _candidate(
        name="Beta Meeting", event_type="meeting", organizer=None,
        start_date="2027-06-01", end_date=None, raw_time_text="June 1, 2027",
        date_evidence="June 1, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    result = validate_extraction(
        {"events": [candidate]}, body="Beta Meeting is rescheduled for June 1, 2027",
    )[0]
    assert result["_reschedule_supported"] is True


@pytest.mark.parametrize(
    "body",
    [
        "Alpha Climate Summit 2027. June 10, 2027.",
        "Alpha Climate Summit 2027; June 10, 2027.",
        "Alpha Climate Summit 2027 | June 10, 2027 | Online.",
    ],
)
def test_candidate_bound_adjacent_title_semicolon_and_table_dates(body):
    candidate = _candidate(
        name="Alpha Climate Summit 2027", organizer=None, start_date="2027-06-10",
        end_date=None, raw_time_text="June 10, 2027", date_evidence="June 10, 2027",
        location=None, online_url=None, deadline_type=None, deadline_date=None,
        deadline_evidence=None,
    )
    validated = validate_extraction({"events": [candidate]}, body=body)[0]
    assert validated["_single_day_supported"] is True


@pytest.mark.parametrize(
    "body",
    [
        "Alpha Climate Summit 2027 starts June 10, 2027; end date TBC.",
        "Alpha Climate Summit 2027 begins June 10, 2027; duration unknown.",
        "Alpha Climate Summit 2027 runs from June 10, 2027; end to be confirmed.",
        "Alpha Climate Summit 2027 lists June 10, 2027 through a date to be confirmed.",
    ],
)
def test_start_or_unknown_duration_evidence_is_not_single_day(body):
    candidate = _candidate(
        name="Alpha Climate Summit 2027", organizer=None, start_date="2027-06-10",
        end_date=None, raw_time_text="June 10, 2027", date_evidence="June 10, 2027",
        location=None, online_url=None, deadline_type=None, deadline_date=None,
        deadline_evidence=None,
    )
    validated = validate_extraction({"events": [candidate]}, body=body)[0]
    assert validated["_single_day_supported"] is False


@pytest.mark.parametrize(
    "body",
    [
        "Example announces Climate Risk Meeting commences June 10, 2027.",
        "Example announces Climate Risk Meeting opens June 10, 2027; end unknown.",
    ],
)
def test_unproven_single_day_remains_queryable_as_unknown(tmp_path, body):
    database = _database(tmp_path, [body])
    candidate = _candidate(
        name="Climate Risk Meeting", event_type="meeting", organizer="Example",
        start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
        date_evidence="June 10, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [candidate]},
    )
    query = dict(
        base_date="2027-06-11", start_date="2027-06-11", end_date="2027-06-11",
        timezone_name="UTC",
    )
    assert query_events(database, **query)["records"] == []
    pending = query_events(database, include_unknown=True, **query)["records"]
    assert len(pending) == 1
    assert pending[0]["needs_confirmation"] == 1


def test_unresolved_end_is_saved_pending_and_queryable_after_start(tmp_path):
    body = "Alpha Climate Summit 2027 starts June 10, 2027; end date TBC."
    database = _database(tmp_path, [body])
    candidate = _candidate(
        name="Alpha Climate Summit 2027", organizer=None, start_date="2027-06-10",
        end_date=None, raw_time_text="June 10, 2027", date_evidence="June 10, 2027",
        location=None, online_url=None, deadline_type=None, deadline_date=None,
        deadline_evidence=None,
    )
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [candidate]},
    )
    record = query_events(
        database, base_date="2027-06-11", start_date="2027-06-11", end_date="2027-06-11",
        include_unknown=True, timezone_name="UTC",
    )["records"][0]
    assert record["needs_confirmation"] == 1
    assert "markdown_content" not in record["sources"][0]
    assert query_events(
        database, base_date="2027-06-11", start_date="2027-06-11", end_date="2027-06-11",
        timezone_name="UTC",
    )["records"] == []


def test_adjacent_deadline_title_binds_type_to_candidate():
    body = "Alpha Climate Consultation Deadline. Entries close May 1, 2027."
    candidate = _candidate(
        name="Alpha Climate Consultation Deadline", event_type="deadline", organizer=None,
        status="scheduled", date_precision="unknown", start_date=None, end_date=None,
        raw_time_text=None, date_evidence=None, location=None, online_url=None,
        deadline_type="consultation", deadline_date="2027-05-01",
        deadline_evidence="May 1, 2027",
    )
    assert validate_extraction({"events": [candidate]}, body=body)[0]["deadline_date"] == "2027-05-01"


def test_duplicate_candidate_date_does_not_hide_its_unresolved_end():
    body = (
        "Alpha Climate Summit 2027 is on June 10, 2027. "
        "Alpha Climate Summit 2027 starts June 10, 2027; end date TBC."
    )
    candidate = _candidate(
        name="Alpha Climate Summit 2027", organizer=None, start_date="2027-06-10",
        end_date=None, raw_time_text="June 10, 2027", date_evidence="June 10, 2027",
        location=None, online_url=None, deadline_type=None, deadline_date=None,
        deadline_evidence=None,
    )
    assert validate_extraction({"events": [candidate]}, body=body)[0]["_single_day_supported"] is False


def test_event_and_deadline_dates_require_local_semantic_context():
    deadline_only = (
        "Example announces World Climate Summit 2027. Registration deadline is May 1, 2027."
    )
    event_from_deadline = _candidate(
        start_date="2027-05-01", end_date=None, raw_time_text="May 1, 2027",
        date_evidence="May 1, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    with pytest.raises(ValueError, match="deadline dates"):
        validate_extraction({"events": [event_from_deadline]}, body=deadline_only)

    event_only = "Example holds World Climate Summit 2027 event on May 1, 2027."
    deadline_from_event = _candidate(
        start_date="2027-05-01", end_date=None, raw_time_text="May 1, 2027",
        date_evidence="May 1, 2027", location=None, online_url=None,
        deadline_type="registration", deadline_date="2027-05-01",
        deadline_evidence="May 1, 2027",
    )
    with pytest.raises(ValueError, match="deadline_evidence"):
        validate_extraction({"events": [deadline_from_event]}, body=event_only)

    wrong_type = (
        "Example opens Climate Consultation Deadline; consultation responses are due May 1, 2027."
    )
    wrong = _candidate(
        name="Climate Consultation Deadline", event_type="deadline", date_precision="unknown",
        start_date=None, end_date=None, raw_time_text=None, date_evidence=None,
        location=None, online_url=None, deadline_type="registration",
        deadline_date="2027-05-01", deadline_evidence="May 1, 2027",
    )
    with pytest.raises(ValueError, match="deadline_evidence"):
        validate_extraction({"events": [wrong]}, body=wrong_type)

    valid = dict(wrong)
    valid["deadline_type"] = "consultation"
    assert validate_extraction({"events": [valid]}, body=wrong_type)[0]["deadline_type"] == "consultation"

    repeated = (
        "Registration deadline is June 10, 2027. "
        "Example confirms World Climate Summit 2027 is held on June 10, 2027."
    )
    repeated_candidate = {
        **event_from_deadline, "organizer": "Example", "start_date": "2027-06-10",
        "raw_time_text": "June 10, 2027", "date_evidence": "June 10, 2027",
    }
    assert validate_extraction(
        {"events": [repeated_candidate]}, body=repeated,
    )[0]["start_date"] == "2027-06-10"


@pytest.mark.parametrize(
    ("deadline_type", "wording"),
    [
        ("registration", "Registration closes May 1, 2027"),
        ("consultation", "Consultation responses are due May 1, 2027"),
        ("expert_review", "Expert review comments are due May 1, 2027"),
    ],
)
def test_deadline_evidence_supports_each_deadline_type(deadline_type, wording):
    body = f"Example opens World Climate Summit 2027. {wording}."
    candidate = _candidate(
        event_type="deadline", date_precision="unknown", start_date=None, end_date=None,
        raw_time_text=None, date_evidence=None, location=None, online_url=None,
        deadline_type=deadline_type, deadline_date="2027-05-01",
        deadline_evidence="May 1, 2027",
    )
    assert validate_extraction({"events": [candidate]}, body=body)[0]["deadline_type"] == deadline_type


def test_deadline_type_cannot_be_borrowed_across_another_subject_clause():
    body = (
        "Example opens Climate Consultation Deadline. Registration remains open while "
        "consultation submissions are due May 1, 2027."
    )
    candidate = _candidate(
        name="Climate Consultation Deadline", event_type="deadline", date_precision="unknown",
        start_date=None, end_date=None, raw_time_text=None, date_evidence=None,
        location=None, online_url=None, deadline_type="registration",
        deadline_date="2027-05-01", deadline_evidence="May 1, 2027",
    )
    with pytest.raises(ValueError, match="deadline_evidence"):
        validate_extraction({"events": [candidate]}, body=body)


def test_processes_all_stored_bodies_and_is_idempotent(tmp_path):
    meeting_body = (
        "Example presents World Climate Summit 2027 in New York, June 10–12, 2027. "
        "Registration closes May 1, 2027 at https://example.com/register. "
        "It includes a climate risk agenda."
    )
    database = _database(tmp_path, [meeting_body, "Example published an ordinary climate article."])

    def extract(request):
        assert set(request) == {
            "schema_version", "content_version_id", "content_sha256", "source_url",
            "article_body", "prompt",
        }
        assert not {"publication_date", "fetch_date", "report_date"} & set(request)
        return {"events": [_candidate()]} if "Summit" in request["article_body"] else {"events": []}

    options = dict(
        prompt_text="extract candidates", prompt_version="v1", provider="openai-api",
        model="gpt-test", extractor=extract, now=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )
    first = process_batch(database, "batch", **options)
    second = process_batch(database, "batch", **options)

    assert first["status"] == "succeeded"
    assert first["item_count"] == 2
    assert first["candidate_count"] == 1
    assert second["reused"] is True
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT count(*) FROM climate_events").fetchone() == (1,)
    assert connection.execute("SELECT count(*) FROM meeting_runs").fetchone() == (1,)
    connection.close()

    queried = query_events(
        database, base_date="2027-06-11", start_date="2027-06-11", end_date="2027-06-11",
        timezone_name="UTC",
    )
    assert [row["name"] for row in queried["records"]] == ["World Climate Summit 2027"]
    assert queried["records"][0]["sources"][0]["date_evidence"] == "June 10–12, 2027"


def test_batch_coverage_counts_items_without_a_persisted_body(tmp_path):
    database = _database(tmp_path, ["Example published an ordinary climate article."])
    _add_unavailable_item(database)
    calls = []

    result = process_batch(
        database, "batch", prompt_text="prompt", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: calls.append(request["content_version_id"]) or {"events": []},
    )

    assert calls == ["content-1"]
    assert result["status"] == "partial"
    assert result["item_count"] == 2
    assert result["succeeded_count"] == 1
    assert result["unavailable_count"] == 1
    missing = next(item for item in result["items"] if item["acquisition_item_id"] == "item-missing")
    assert missing["status"] == "unavailable"
    assert missing["content_version_id"] is None


def test_batch_with_no_persisted_bodies_reports_real_no_content(tmp_path):
    database = _database(tmp_path, [])
    _add_unavailable_item(database)
    calls = []
    result = process_batch(
        database, "batch", prompt_text="prompt", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: calls.append(request) or {"events": []},
    )
    assert result["status"] == "no_content"
    assert result["item_count"] == 1
    assert result["unavailable_count"] == 1
    assert calls == []


def test_partial_dates_unknown_end_and_snapshots_are_stable(tmp_path):
    body = "Example announces Climate Webinar 2028 for March 2028."
    database = _database(tmp_path, [body])
    candidate = _candidate(
        name="Climate Webinar 2028", event_type="webinar", date_precision="month",
        start_date="2028-03", end_date=None, raw_time_text="March 2028",
        date_evidence="March 2028", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    process_batch(
        database, "batch", prompt_text="prompt", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [candidate]},
    )
    assert query_events(database, base_date="2028-04-01", timezone_name="UTC")["records"] == []
    pending = query_events(database, base_date="2028-04-01", include_unknown=True, timezone_name="UTC")
    assert pending["records"][0]["needs_confirmation"] == 1

    first = freeze_snapshot(database, base_date="2028-04-01", include_unknown=True, timezone_name="UTC")
    second = freeze_snapshot(database, base_date="2028-04-01", include_unknown=True, timezone_name="UTC")
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["created_at"] == second["created_at"]
    assert load_snapshot(database, first["snapshot_id"])["records"] == first["records"]
    connection = sqlite3.connect(database)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("UPDATE meeting_snapshots SET base_date='2099-01-01'")
    connection.close()


def test_coverage_and_snapshot_identity_include_enabled_target_batch_context(tmp_path):
    database = _database(tmp_path, ["Example published an ordinary climate article."])
    disabled = query_events(
        database, base_date="2027-01-01", timezone_name="UTC", meeting_enabled=False,
        target_batch_id="batch", task_version=1,
    )
    enabled = query_events(
        database, base_date="2027-01-01", timezone_name="UTC", meeting_enabled=True,
        target_batch_id="batch", task_version=2,
    )
    assert disabled["coverage"]["status"] == "disabled"
    assert enabled["coverage"]["status"] == "enabled_unprocessed"
    assert enabled["coverage"]["target_processed"] is False

    disabled_snapshot = freeze_snapshot(
        database, base_date="2027-01-01", timezone_name="UTC", meeting_enabled=False,
        target_batch_id="batch", task_version=1,
    )
    enabled_snapshot = freeze_snapshot(
        database, base_date="2027-01-01", timezone_name="UTC", meeting_enabled=True,
        target_batch_id="batch", task_version=2,
    )
    assert disabled_snapshot["snapshot_id"] != enabled_snapshot["snapshot_id"]
    assert disabled_snapshot["coverage"]["status"] == "disabled"
    assert enabled_snapshot["coverage"]["status"] == "enabled_unprocessed"

    process_batch(
        database, "batch", prompt_text="prompt", prompt_version="v1", provider="p", model="m",
        task_version=2, extractor=lambda request: {"events": []},
    )
    completed = query_events(
        database, base_date="2027-01-01", timezone_name="UTC", meeting_enabled=True,
        target_batch_id="batch", task_version=2,
    )
    assert completed["coverage"]["status"] == "succeeded_empty"
    assert completed["coverage"]["target_processed"] is True


def test_existing_records_do_not_claim_an_unprocessed_target_batch_is_complete(tmp_path):
    body = "Example presents World Climate Summit 2027 in New York, June 10–12, 2027."
    database = _database(tmp_path, [body])
    process_batch(
        database, "batch", prompt_text="prompt", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [_candidate(
            online_url=None, deadline_type=None, deadline_date=None, deadline_evidence=None,
        )]},
    )
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO acquisition_batches VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("next-batch", "pre-report-acquisition-batch.v1", "2026-01-12", "2026-01-12T00:00:00Z",
         None, '{"mode":"unlimited"}', "no_search", "site-only", "c" * 64, None),
    )
    connection.commit()
    connection.close()

    result = query_events(
        database, base_date="2027-01-01", timezone_name="UTC", meeting_enabled=True,
        target_batch_id="next-batch", task_version=2,
    )
    assert len(result["records"]) == 1
    assert result["coverage"]["status"] == "enabled_unprocessed"
    assert result["coverage"]["target_processed"] is False
    assert result["coverage"]["uses_existing_records"] is True


def test_explicit_single_day_and_deadline_use_query_intervals(tmp_path):
    bodies = [
        "Example hosts Climate Risk Meeting on June 10, 2027.",
        "Example opens Climate Consultation Deadline; submissions close May 1, 2027.",
    ]
    database = _database(tmp_path, bodies)

    def extract(request):
        if "Meeting" in request["article_body"]:
            return {"events": [_candidate(
                name="Climate Risk Meeting", event_type="meeting", start_date="2027-06-10",
                end_date=None, raw_time_text="June 10, 2027", date_evidence="June 10, 2027",
                location=None, online_url=None, deadline_type=None, deadline_date=None,
                deadline_evidence=None,
            )]}
        return {"events": [_candidate(
            name="Climate Consultation Deadline", event_type="deadline",
            date_precision="unknown", start_date=None, end_date=None, raw_time_text=None,
            date_evidence=None, location=None, online_url=None, deadline_type="consultation",
            deadline_date="2027-05-01", deadline_evidence="May 1, 2027",
        )]}

    process_batch(
        database, "batch", prompt_text="prompt", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    single = query_events(
        database, base_date="2027-06-10", start_date="2027-06-10", end_date="2027-06-10",
        timezone_name="UTC",
    )
    assert [record["name"] for record in single["records"]] == ["Climate Risk Meeting"]
    assert single["records"][0]["needs_confirmation"] == 0
    deadline = query_events(
        database, event_types=["deadline"], include_deadlines=True, base_date="2027-05-01",
        start_date="2027-05-01", end_date="2027-05-01", timezone_name="UTC",
    )
    assert [record["name"] for record in deadline["records"]] == ["Climate Consultation Deadline"]
    assert deadline["records"][0]["needs_confirmation"] == 0
    assert query_events(
        database, event_types=["deadline"], include_deadlines=True, include_unknown=True,
        base_date="2027-05-02", start_date="2027-05-02", end_date="2027-05-02",
        timezone_name="UTC",
    )["records"] == []


def test_meeting_deadline_is_an_independent_query_interval_and_snapshot_record(tmp_path):
    body = (
        "Example hosts World Climate Summit 2027 on June 10–12, 2027; "
        "registration closes May 1, 2027."
    )
    database = _database(tmp_path, [body])
    process_batch(
        database, "batch", prompt_text="prompt", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [_candidate(online_url=None, location=None)]},
    )

    may = dict(
        base_date="2027-05-01", start_date="2027-05-01", end_date="2027-05-01",
        timezone_name="UTC",
    )
    assert query_events(database, **may)["records"] == []
    deadline_match = query_events(database, include_deadlines=True, **may)
    assert len(deadline_match["records"]) == 1

    event_match = query_events(
        database, base_date="2027-06-11", start_date="2027-06-11", end_date="2027-06-11",
        timezone_name="UTC",
    )["records"]
    assert len(event_match) == 1
    assert event_match[0]["deadline_date"] == "2027-05-01"
    assert query_events(
        database, include_deadlines=True, base_date="2027-05-15", start_date="2027-05-15",
        end_date="2027-05-15", timezone_name="UTC",
    )["records"] == []
    assert query_events(
        database, include_deadlines=True, include_unknown=True, base_date="2027-05-15",
        start_date="2027-05-15", end_date="2027-05-15", timezone_name="UTC",
    )["records"] == []

    snapshot = freeze_snapshot(database, include_deadlines=True, **may)
    assert snapshot["records"] == deadline_match["records"]
    connection = sqlite3.connect(database)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE meeting_snapshots SET base_date='2099-01-01' WHERE snapshot_id=?",
            (snapshot["snapshot_id"],),
        )
    connection.close()


def test_event_and_deadline_on_same_day_return_one_record(tmp_path):
    body = (
        "Example hosts World Climate Summit 2027 on June 10, 2027; "
        "registration closes June 10, 2027."
    )
    database = _database(tmp_path, [body])
    process_batch(
        database, "batch", prompt_text="prompt", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [_candidate(
            start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
            date_evidence="June 10, 2027", location=None, online_url=None,
            deadline_date="2027-06-10", deadline_evidence="June 10, 2027",
        )]},
    )
    records = query_events(
        database, include_deadlines=True, base_date="2027-06-10", start_date="2027-06-10",
        end_date="2027-06-10", timezone_name="UTC",
    )["records"]
    assert len(records) == 1


def test_schema_10_upgrades_to_exact_versioned_meeting_contract():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection, target_version=10)
    assert validate_registry_contract(connection) == 10
    assert apply_migrations(connection) == [11, 12]
    assert validate_registry_contract(connection) == 12
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"meeting_runs", "meeting_run_items", "climate_events", "climate_event_sources",
            "climate_event_versions", "meeting_snapshots"} <= tables


def test_schema_11_upgrades_existing_meeting_sources_without_losing_evidence(tmp_path):
    database = tmp_path / "v11.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection, target_version=11)
    assert validate_registry_contract(connection) == 11
    assert apply_migrations(connection) == [12]
    assert validate_registry_contract(connection) == 12
    columns = {row[1] for row in connection.execute("PRAGMA table_info(climate_event_sources)")}
    assert {"candidate_ordinal", "interpretation_seq"} <= columns
    connection.close()


def test_multiple_sources_merge_but_conflicting_dates_stay_unresolved(tmp_path):
    bodies = [
        "Example announces 27th World Climate Summit 2027 in New York, June 10–12, 2027.",
        "Example lists 27th World Climate Summit 2027 in Paris, July 1–2, 2027.",
        "Example announces 28th World Climate Summit 2028 in London, June 10–12, 2028.",
    ]
    database = _database(tmp_path, bodies)

    def extract(request):
        body = request["article_body"]
        if "July" in body:
            return {"events": [_candidate(
                name="27th World Climate Summit 2027",
                start_date="2027-07-01", end_date="2027-07-02",
                raw_time_text="July 1–2, 2027", date_evidence="July 1–2, 2027",
                location="Paris", online_url=None, deadline_type=None, deadline_date=None,
                deadline_evidence=None,
            )]}
        if "2028" in body:
            return {"events": [_candidate(
                name="28th World Climate Summit 2028", start_date="2028-06-10", end_date="2028-06-12",
                raw_time_text="June 10–12, 2028", date_evidence="June 10–12, 2028",
                location="London", online_url=None, deadline_type=None, deadline_date=None,
                deadline_evidence=None,
            )]}
        return {"events": [_candidate(
            name="27th World Climate Summit 2027", location="New York", online_url=None,
            deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )]}

    result = process_batch(
        database, "batch", prompt_text="prompt", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    assert result["candidate_count"] == 3
    rows = query_events(database, base_date="2027-01-01", include_unknown=True, timezone_name="UTC")["records"]
    assert len(rows) == 2
    conflicted = next(row for row in rows if row["name"].endswith("2027"))
    assert conflicted["status"] == "conflict"
    assert conflicted["start_date"] is None
    assert conflicted["source_count"] == 2
    assert len(conflicted["sources"]) == 2


def test_cancelled_event_keeps_prior_version_and_failed_items_retry_only(tmp_path):
    bodies = [
        "Example announces World Climate Summit 2027 in New York, June 10–12, 2027.",
        "Example confirms World Climate Summit 2027 is cancelled. It had been June 10–12, 2027 in New York.",
    ]
    database = _database(tmp_path, bodies)
    calls = []

    def first(request):
        calls.append(request["content_version_id"])
        if request["content_version_id"] == "content-2":
            raise RuntimeError("temporary model failure")
        return {"events": [_candidate(
            location="New York", online_url=None, deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )]}

    options = dict(prompt_text="prompt", prompt_version="v1", provider="p", model="m")
    partial = process_batch(database, "batch", extractor=first, **options)
    assert partial["status"] == "partial"
    progress = meeting_status(database, batch_id="batch")
    failed_item = next(item for item in progress["runs"][0]["items"] if item["status"] == "failed")
    assert failed_item == {
        "acquisition_item_id": "item-2", "article_id": "article-2",
        "content_version_id": "content-2", "source_url": "https://example.com/2",
        "status": "failed", "candidate_count": 0,
        "error": "RuntimeError: temporary model failure", "processed_at": failed_item["processed_at"],
    }
    assert progress["runs"][0]["retry_of_meeting_run_id"] is None
    assert "prompt_text" not in progress["runs"][0]

    def retry(request):
        calls.append(request["content_version_id"])
        return {"events": [_candidate(
            status="cancelled", status_evidence="is cancelled",
            location="New York", online_url=None, deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )]}

    completed = process_batch(database, "batch", extractor=retry, retry_failed=True, **options)
    assert completed["status"] == "succeeded"
    assert calls == ["content-1", "content-2", "content-2"]
    retried_progress = meeting_status(database, batch_id="batch")
    assert retried_progress["runs"][0]["retry_of_meeting_run_id"] == partial["meeting_run_id"]
    assert query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"] == []
    cancelled = query_events(
        database, base_date="2027-01-01", include_cancelled=True, timezone_name="UTC",
    )["records"][0]
    assert cancelled["status"] == "cancelled"
    assert cancelled["sources"][1]["status_evidence"] == "is cancelled"
    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT count(*) FROM climate_event_versions WHERE event_id=?", (cancelled["event_id"],)
    ).fetchone()[0] == 2
    connection.close()


def test_retry_uses_failed_run_frozen_prompt_and_only_failed_item(tmp_path):
    database = _database(tmp_path, [
        "Example published an ordinary climate article.",
        "Example published another ordinary climate article.",
    ])
    first_calls = []

    def first(request):
        first_calls.append((request["content_version_id"], request["prompt"]))
        if request["content_version_id"] == "content-2":
            raise RuntimeError("temporary")
        return {"events": []}

    original = process_batch(
        database, "batch", prompt_text="frozen v1", prompt_version="v1",
        provider="provider-v1", model="model-v1", task_version=3, extractor=first,
    )
    retry_calls = []
    retried = process_batch(
        database, "batch", prompt_text="current v2", prompt_version="v2",
        provider="provider-v2", model="model-v2", task_version=4,
        retry_failed=True, retry_meeting_run_id=original["meeting_run_id"],
        extractor=lambda request: retry_calls.append(
            (request["content_version_id"], request["prompt"])
        ) or {"events": []},
    )

    assert first_calls == [("content-1", "frozen v1"), ("content-2", "frozen v1")]
    assert retry_calls == [("content-2", "frozen v1")]
    assert retried["attempt"] == 2
    assert retried["processing_key"] == original["processing_key"]
    assert retried["retry_of_meeting_run_id"] == original["meeting_run_id"]
    assert retried["prompt_version"] == "v1"
    assert (retried["provider"], retried["model"], retried["task_version"]) == (
        "provider-v1", "model-v1", 3,
    )
    assert [item["status"] for item in retried["items"]] == ["succeeded", "succeeded"]
    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT prompt_text FROM meeting_runs WHERE meeting_run_id=?", (retried["meeting_run_id"],),
    ).fetchone() == ("frozen v1",)
    connection.close()


def test_dead_worker_resumes_frozen_running_run_without_repeating_success(tmp_path):
    database = _database(tmp_path, ["ordinary one", "ordinary two"])
    code = r'''
import os, sys
from climate_monitor.meetings import process_batch
def extract(request):
    if request["content_version_id"] == "content-2":
        os._exit(23)
    return {"events": []}
process_batch(sys.argv[1], "batch", prompt_text="frozen v1", prompt_version="v1",
              provider="provider-v1", model="model-v1", task_version=3, extractor=extract)
'''
    child = subprocess.run(
        [sys.executable, "-c", code, str(database)], cwd=str(database.parent),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
    )
    assert child.returncode == 23
    connection = sqlite3.connect(database)
    run_id = connection.execute("SELECT meeting_run_id FROM meeting_runs").fetchone()[0]
    assert connection.execute(
        "SELECT status FROM meeting_runs WHERE meeting_run_id=?", (run_id,),
    ).fetchone() == ("running",)
    assert connection.execute(
        "SELECT status FROM meeting_run_items ORDER BY acquisition_item_id"
    ).fetchall() == [("succeeded",), ("pending",)]
    connection.close()

    calls = []
    resumed = process_batch(
        database, "batch", prompt_text="current v2", prompt_version="v2",
        provider="provider-v2", model="model-v2", task_version=4,
        extractor=lambda request: calls.append(
            (request["content_version_id"], request["prompt"])
        ) or {"events": []},
    )
    assert calls == [("content-2", "frozen v1")]
    assert resumed["meeting_run_id"] == run_id
    assert resumed["attempt"] == 1
    assert resumed["status"] == "succeeded"
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT count(*) FROM meeting_runs").fetchone() == (1,)
    connection.close()


def test_live_worker_lock_reuses_running_run_without_second_extractor(tmp_path):
    database = _database(tmp_path, ["ordinary article"])
    marker = tmp_path / "extracting"
    code = r'''
import pathlib, sys, time
from climate_monitor.meetings import process_batch
def extract(request):
    pathlib.Path(sys.argv[2]).write_text("active")
    time.sleep(30)
    return {"events": []}
process_batch(sys.argv[1], "batch", prompt_text="v1", prompt_version="v1",
              provider="p", model="m", extractor=extract)
'''
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(database), str(marker)], cwd=str(database.parent),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])},
    )
    try:
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.02)
        assert marker.exists()
        attached = process_batch(
            database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
            extractor=lambda request: pytest.fail("a live worker item must not run twice"),
        )
        assert attached["status"] == "running"
        assert attached["reused"] is True
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_run_confirmation_summary_is_scoped_and_deduplicated(tmp_path):
    bodies = [
        "Example announces Climate Webinar 2028 for March 2028.",
        "Example confirms Climate Webinar 2028 for March 2028.",
        "Example announces Climate Risk Summit 2027 beginning June 10–12, 2027.",
        "Example hosts Climate Risk Meeting on July 1, 2027.",
    ]
    database = _database(tmp_path, bodies)

    def first(request):
        body = request["article_body"]
        if "Webinar" in body:
            return {"events": [_candidate(
                name="Climate Webinar 2028", event_type="webinar", date_precision="month",
                start_date="2028-03", end_date=None, raw_time_text="March 2028",
                date_evidence="March 2028", location=None, online_url=None,
                deadline_type=None, deadline_date=None, deadline_evidence=None,
            )]}
        if "Summit" in body:
            return {"events": [_candidate(
                name="Climate Risk Summit 2027", start_date="2027-06-10", end_date=None,
                raw_time_text="June 10–12, 2027", date_evidence="June 10–12, 2027",
                location=None, online_url=None, deadline_type=None, deadline_date=None,
                deadline_evidence=None,
            )]}
        return {"events": []}

    original = process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=first,
    )
    assert original["current_needs_confirmation_count"] == 2
    assert {row["name"] for row in original["current_needs_confirmation_events"]} == {
        "Climate Webinar 2028", "Climate Risk Summit 2027",
    }
    webinar = next(
        row for row in query_events(
            database, base_date="2027-01-01", include_unknown=True, timezone_name="UTC",
        )["records"] if row["name"] == "Climate Webinar 2028"
    )
    assert webinar["source_count"] == 2
    assert sum(
        row["event_id"] == webinar["event_id"]
        for row in original["current_needs_confirmation_events"]
    ) == 1

    definite = _candidate(
        name="Climate Risk Meeting", event_type="meeting", start_date="2027-07-01",
        end_date=None, raw_time_text="July 1, 2027", date_evidence="July 1, 2027",
        location=None, online_url=None, deadline_type=None, deadline_date=None,
        deadline_evidence=None,
    )
    independent = process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: {"events": [definite]}
        if "Meeting" in request["article_body"] else {"events": []},
    )
    assert independent["current_needs_confirmation_count"] == 0
    assert independent["current_needs_confirmation_events"] == []
    status = meeting_status(database, batch_id="batch")
    assert status["runs"][0]["current_needs_confirmation_count"] == 0
    assert status["runs"][1]["current_needs_confirmation_count"] == 2


def test_retry_confirmation_summary_includes_reused_success_lineage(tmp_path):
    database = _database(tmp_path, [
        "Example announces Climate Webinar 2028 for March 2028.",
        "Example published an ordinary climate article.",
    ])
    candidate = _candidate(
        name="Climate Webinar 2028", event_type="webinar", date_precision="month",
        start_date="2028-03", end_date=None, raw_time_text="March 2028",
        date_evidence="March 2028", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )

    def first(request):
        if "ordinary" in request["article_body"]:
            raise RuntimeError("temporary")
        return {"events": [candidate]}

    original = process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=first,
    )
    assert original["status"] == "partial"
    assert original["current_needs_confirmation_count"] == 1
    retried = process_batch(
        database, "batch", prompt_text="ignored", prompt_version="ignored", provider="x", model="x",
        retry_failed=True, retry_meeting_run_id=original["meeting_run_id"],
        extractor=lambda request: {"events": []},
    )
    assert retried["status"] == "succeeded"
    assert retried["current_needs_confirmation_count"] == 1
    assert retried["current_needs_confirmation_events"] == original["current_needs_confirmation_events"]


def test_run_confirmation_summary_includes_existing_peer_updated_by_event_version(tmp_path):
    urls = ["https://example.com/register-a", "https://example.com/register-b"]
    database = _database(tmp_path, [
        f"Example hosts World Climate Summit 2027 in Paris on June 10, 2027. {url}"
        for url in urls
    ])

    def candidate_for(request):
        url = next(value for value in urls if value in request["article_body"])
        return _candidate(
            start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
            date_evidence="June 10, 2027", location="Paris", online_url=url,
            deadline_type=None, deadline_date=None, deadline_evidence=None,
        )

    first = process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [candidate_for(request)]}
        if urls[0] in request["article_body"] else {"events": []},
    )
    assert first["current_needs_confirmation_count"] == 0
    second = process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: {"events": [candidate_for(request)]}
        if urls[1] in request["article_body"] else {"events": []},
    )
    assert second["current_needs_confirmation_count"] == 2
    assert len({row["event_id"] for row in second["current_needs_confirmation_events"]}) == 2


def test_current_content_per_article_resolves_conflict_without_erasing_history(tmp_path):
    june = "Example announces 27th World Climate Summit 2027 on June 10–12, 2027."
    july = "Example announces 27th World Climate Summit 2027 on July 1–2, 2027."
    database = _database(tmp_path, [june, july])

    def initial(request):
        if "July" in request["article_body"]:
            return {"events": [_candidate(
                name="27th World Climate Summit 2027",
                start_date="2027-07-01", end_date="2027-07-02",
                raw_time_text="July 1–2, 2027", date_evidence="July 1–2, 2027",
                location=None, online_url=None, deadline_type=None, deadline_date=None,
                deadline_evidence=None,
            )]}
        return {"events": [_candidate(
            name="27th World Climate Summit 2027", location=None, online_url=None,
            deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )]}

    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=initial,
    )
    assert query_events(
        database, base_date="2027-01-01", include_unknown=True, timezone_name="UTC",
    )["records"][0]["status"] == "conflict"

    replacement = (
        "Example confirms 27th World Climate Summit 2027 is rescheduled for June 10–12, 2027."
    )
    digest = hashlib.sha256(replacement.encode()).hexdigest()
    connection = sqlite3.connect(database)
    connection.execute(
        """INSERT INTO article_content_versions VALUES
           ('content-2-new', 'article-2', ?, ?, ?, 'text/markdown', ?, 'reader', 'v1',
            '2026-02-01T00:00:00Z')""",
        (digest, replacement, digest, len(replacement.encode())),
    )
    connection.execute(
        "UPDATE articles SET current_content_version_id='content-2-new' WHERE article_id='article-2'"
    )
    connection.execute(
        """INSERT INTO article_fetches(fetch_id, article_id, requested_url, final_url, fetched_at,
           fetch_status, http_status, content_type, content_version_id)
           VALUES ('fetch-2-new', 'article-2', 'https://example.com/2', 'https://example.com/2',
                   '2026-02-01T00:00:00Z', 'success', 200, 'text/markdown', 'content-2-new')"""
    )
    connection.execute(
        """INSERT INTO acquisition_batches VALUES (?,?,?,?,?,?,?,?,?,?)""",
        ("batch-new", "pre-report-acquisition-batch.v1", "2026-02-02", "2026-02-02T00:00:00Z",
         "2026-02-02T00:10:00Z", '{"mode":"unlimited"}', "no_search", "site-only",
         "b" * 64, "2026-02-02T00:10:00Z"),
    )
    connection.execute(
        """INSERT INTO acquisition_items(
           acquisition_item_id, batch_id, ordinal, article_id, raw_url, source_name, title,
           summary, discovered_at, discovery_kind, discovery_ref, origins_json, date_status,
           selection_status, selection_reason, update_status, material_status, fetch_id,
           content_version_id, attempts_json, processing_status)
           VALUES ('item-2-new', 'batch-new', 1, 'article-2', 'https://example.com/2', 'Example',
                   'Updated', '', '2026-02-01T00:00:00Z', 'site', 'ref-new', '[]', 'eligible',
                   'selected', 'updated', 'content_changed', 'full_content', 'fetch-2-new',
                   'content-2-new', '[]', 'complete')"""
    )
    connection.commit()
    connection.close()

    process_batch(
        database, "batch-new", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [_candidate(
            name="27th World Climate Summit 2027",
            start_date="2027-06-10", end_date="2027-06-12", status="scheduled",
            raw_time_text="June 10–12, 2027", date_evidence="June 10–12, 2027",
            location=None, online_url=None, deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )]},
    )
    current = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"][0]
    assert (current["status"], current["start_date"], current["source_count"]) == (
        "scheduled", "2027-06-10", 2,
    )
    assert len(current["sources"]) == 3
    old_july = next(
        source for source in current["sources"] if source["date_evidence"] == "July 1–2, 2027"
    )
    assert old_july["is_current"] == 0
    assert sum(source["is_current"] for source in current["sources"]) == 2

    process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=initial,
    )
    rerun = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"][0]
    assert (rerun["status"], rerun["start_date"], rerun["source_count"]) == (
        "scheduled", "2027-06-10", 2,
    )


def test_real_acquisition_batches_order_current_body_without_article_pointer(tmp_path, monkeypatch):
    if sys.platform == "win32":
        monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace(
            LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=lambda *args: None,
        ))
    database = tmp_path / "stored.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection)
    connection.close()
    url = "https://example.org/summit"
    old_body = "Example announces World Climate Summit 2027 on June 10–12, 2027."
    new_body = "Example confirms World Climate Summit 2027 is rescheduled for July 1–2, 2027."
    store_acquisition_batch(database, _stored_batch(
        "stored-old", "2026-01-05", "2026-01-05T08:00:00Z",
        _stored_item(url, old_body, "2026-01-05T08:00:00Z"),
    ))
    store_acquisition_batch(database, _stored_batch(
        "stored-new", "2026-02-02", "2026-02-02T08:00:00Z",
        _stored_item(url, new_body, "2026-02-02T08:00:00Z"),
    ))
    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT current_content_version_id FROM articles WHERE canonical_url=?", (url,),
    ).fetchone() == (None,)
    connection.close()

    def extract(request):
        if "July" in request["article_body"]:
            return {"events": [_candidate(
                start_date="2027-07-01", end_date="2027-07-02",
                raw_time_text="July 1–2, 2027", date_evidence="July 1–2, 2027",
                location=None, online_url=None, deadline_type=None, deadline_date=None,
                deadline_evidence=None,
            )]}
        return {"events": [_candidate(
            location=None, online_url=None, deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )]}

    process_batch(
        database, "stored-old", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    process_batch(
        database, "stored-new", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    current = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"][0]
    assert current["start_date"] == "2027-07-01"
    process_batch(
        database, "stored-old", prompt_text="old rerun", prompt_version="v2",
        provider="p", model="m", extractor=extract,
    )
    rerun = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"][0]
    assert rerun["start_date"] == "2027-07-01"
    assert sum(source["is_current"] for source in rerun["sources"]) == 1


def test_new_prompt_interpretation_of_same_body_can_cancel_without_inflating_sources(tmp_path):
    body = (
        "Example announced World Climate Summit 2027 for June 10–12, 2027. "
        "The World Climate Summit 2027 is cancelled."
    )
    database = _database(tmp_path, [body])
    scheduled = _candidate(
        location=None, online_url=None, deadline_type=None, deadline_date=None,
        deadline_evidence=None,
    )
    first = process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [scheduled]},
    )
    cancelled = _candidate(
        status="cancelled", status_evidence="is cancelled", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    second = process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: {"events": [cancelled]},
    )
    current = query_events(
        database, base_date="2027-01-01", include_cancelled=True, timezone_name="UTC",
    )["records"][0]
    assert current["status"] == "cancelled"
    assert current["source_count"] == 1
    assert len(current["sources"]) == 2
    assert sum(source["is_current"] for source in current["sources"]) == 1
    assert {source["interpretation_seq"] for source in current["sources"]} == {1, 2}
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT count(*) FROM meeting_runs").fetchone() == (2,)
    assert connection.execute(
        "SELECT count(*) FROM climate_event_versions WHERE event_id=?", (current["event_id"],),
    ).fetchone() == (2,)
    connection.close()
    assert first["processing_key"] != second["processing_key"]


def test_same_page_same_name_and_year_keeps_distinct_occurrences(tmp_path):
    body = (
        "Example hosts World Climate Summit 2027 on June 10, 2027. "
        "Example also hosts World Climate Summit 2027 on July 12, 2027."
    )
    database = _database(tmp_path, [body])
    candidates = [
        _candidate(
            start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
            date_evidence="June 10, 2027", location=None, online_url=None,
            deadline_type=None, deadline_date=None, deadline_evidence=None,
        ),
        _candidate(
            start_date="2027-07-12", end_date=None, raw_time_text="July 12, 2027",
            date_evidence="July 12, 2027", location=None, online_url=None,
            deadline_type=None, deadline_date=None, deadline_evidence=None,
        ),
    ]
    first = process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": candidates},
    )
    second = process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": candidates},
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert [(record["start_date"], record["sources"][0]["date_evidence"]) for record in records] == [
        ("2027-06-10", "June 10, 2027"), ("2027-07-12", "July 12, 2027"),
    ]
    assert records[0]["event_id"] != records[1]["event_id"]
    assert first["candidate_count"] == 2
    assert second["reused"] is True


def test_cross_source_dates_split_by_default_but_same_day_merges(tmp_path):
    database = _database(tmp_path, [
        "Example hosts World Climate Summit 2027 on June 10, 2027.",
        "Example hosts World Climate Summit 2027 on July 12, 2027.",
        "Example confirms World Climate Summit 2027 on June 10, 2027.",
    ])

    def extract(request):
        july = "July" in request["article_body"]
        return {"events": [_candidate(
            start_date="2027-07-12" if july else "2027-06-10", end_date=None,
            raw_time_text="July 12, 2027" if july else "June 10, 2027",
            date_evidence="July 12, 2027" if july else "June 10, 2027",
            location=None, online_url=None, deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )]}

    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert [record["start_date"] for record in records] == ["2027-06-10", "2027-07-12"]
    assert [record["source_count"] for record in records] == [2, 1]


def test_cross_source_identity_with_different_direct_urls_is_flagged_and_idempotent(tmp_path):
    urls = ["https://example.com/register-a", "https://example.com/register-b"]
    database = _database(tmp_path, [
        f"Example hosts World Climate Summit 2027 in Paris on June 10, 2027. {url}"
        for url in urls
    ])

    def extract(request):
        url = next(value for value in urls if value in request["article_body"])
        first_source = url == urls[0]
        return {"events": [_candidate(
            organizer=None if first_source else "Example",
            start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
            date_evidence="June 10, 2027", location=None if first_source else "Paris",
            online_url=url,
            deadline_type=None, deadline_date=None, deadline_evidence=None,
        )]}

    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    first = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    first_versions = {row["event_id"]: row["record_version"] for row in first}
    assert len(first) == 2
    assert all(row["needs_confirmation"] == 1 for row in first)
    assert all(row["source_count"] == 1 and len(row["sources"]) == 1 for row in first)
    assert sorted(first_versions.values()) == [1, 2]

    process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=extract,
    )
    second = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert {row["event_id"]: row["record_version"] for row in second} == first_versions
    assert all(row["needs_confirmation"] == 1 for row in second)
    assert all(len(row["sources"]) == 2 for row in second)


def test_suspected_identity_is_recomputed_after_sources_gain_distinct_locations(tmp_path):
    urls = ["https://example.com/register-a", "https://example.com/register-b"]
    locations = {urls[0]: "Paris", urls[1]: "London"}

    def scenario(root, ordered_urls):
        root.mkdir()
        database = _database(root, [
            f"Example hosts World Climate Summit 2027 in {locations[url]} on June 10, 2027. {url}"
            for url in ordered_urls
        ])

        def extract(location_known):
            def run(request):
                url = next(value for value in urls if value in request["article_body"])
                return {"events": [_candidate(
                    start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
                    date_evidence="June 10, 2027",
                    location=locations[url] if location_known else None, online_url=url,
                    deadline_type=None, deadline_date=None, deadline_evidence=None,
                )]}
            return run

        process_batch(
            database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
            extractor=extract(False),
        )
        ambiguous = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
        assert len(ambiguous) == 2
        assert all(row["needs_confirmation"] == 1 for row in ambiguous)
        assert sorted(row["record_version"] for row in ambiguous) == [1, 2]

        process_batch(
            database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
            extractor=extract(True),
        )
        resolved = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
        assert all(row["needs_confirmation"] == 0 for row in resolved)
        assert sorted(row["record_version"] for row in resolved) == [2, 4]
        assert {row["location"] for row in resolved} == {"Paris", "London"}
        source_counts = {row["event_id"]: len(row["sources"]) for row in resolved}

        reused = process_batch(
            database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
            extractor=lambda request: (_ for _ in ()).throw(AssertionError("idempotent run extracted")),
        )
        assert reused["reused"] is True
        repeated = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
        assert {row["event_id"]: row["record_version"] for row in repeated} == {
            row["event_id"]: row["record_version"] for row in resolved
        }
        assert {row["event_id"]: len(row["sources"]) for row in repeated} == source_counts
        return {
            row["online_url"]: (
                row["event_id"], row["location"], row["needs_confirmation"],
                len(row["sources"]),
            )
            for row in repeated
        }

    assert scenario(tmp_path / "forward", urls) == scenario(tmp_path / "reverse", list(reversed(urls)))


def test_different_core_facts_do_not_create_suspected_identity_groups(tmp_path):
    cases = [
        ("Example", "Paris", "2027-06-10", "June 10, 2027"),
        ("Example", "London", "2027-06-10", "June 10, 2027"),
        ("Other", "Paris", "2027-06-10", "June 10, 2027"),
        ("Example", "Paris", "2027-07-12", "July 12, 2027"),
    ]
    urls = [f"https://example.com/core-{index}" for index in range(len(cases))]
    database = _database(tmp_path, [
        f"{organizer} hosts World Climate Summit 2027 in {location} on {evidence}. {url}"
        for (organizer, location, _start, evidence), url in zip(cases, urls)
    ])

    def extract(request):
        index = next(index for index, url in enumerate(urls) if url in request["article_body"])
        organizer, location, start, evidence = cases[index]
        return {"events": [_candidate(
            organizer=organizer, start_date=start, end_date=None, raw_time_text=evidence,
            date_evidence=evidence, location=location, online_url=urls[index],
            deadline_type=None, deadline_date=None, deadline_evidence=None,
        )]}

    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert len(records) == 4
    assert all(record["needs_confirmation"] == 0 for record in records)


def test_same_direct_url_and_core_facts_merge_without_identity_confirmation(tmp_path):
    url = "https://example.com/shared-registration"
    database = _database(tmp_path, [
        f"Example hosts World Climate Summit 2027 in Paris on June 10, 2027. {url}",
        f"Example confirms World Climate Summit 2027 in Paris on June 10, 2027. {url}",
    ])
    candidate = _candidate(
        start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
        date_evidence="June 10, 2027", location="Paris", online_url=url,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [candidate]},
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert len(records) == 1
    assert records[0]["source_count"] == 2
    assert records[0]["needs_confirmation"] == 0


@pytest.mark.parametrize("known_first", [False, True])
def test_unique_historical_direct_url_merges_optional_fact_enrichment_in_either_order(
    tmp_path, known_first,
):
    url = "https://example.com/shared-registration"
    omitted = f"Example lists World Climate Summit 2027 on June 10, 2027. {url}"
    known = f"Example hosts World Climate Summit 2027 in Paris on June 10–12, 2027. {url}"
    database = _database(tmp_path, [known, omitted] if known_first else [omitted, known])

    def extract(request):
        enriched = "Paris" in request["article_body"]
        return {"events": [_candidate(
            organizer="Example" if enriched else None,
            start_date="2027-06-10", end_date="2027-06-12" if enriched else None,
            raw_time_text="June 10–12, 2027" if enriched else "June 10, 2027",
            date_evidence="June 10–12, 2027" if enriched else "June 10, 2027",
            location="Paris" if enriched else None, online_url=url,
            deadline_type=None, deadline_date=None, deadline_evidence=None,
        )]}

    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert len(records) == 1
    assert records[0]["source_count"] == 2
    assert records[0]["organizer"] == "Example"
    assert records[0]["location"] == "Paris"
    assert records[0]["end_date"] == "2027-06-12"
    assert len(records[0]["sources"]) == 2


@pytest.mark.parametrize(
    ("field", "first", "second"),
    [
        ("organizer", "Example", "Other"),
        ("location", "Paris", "London"),
        ("end_date", "2027-06-11", "2027-06-12"),
    ],
)
def test_direct_url_does_not_merge_explicit_optional_fact_conflicts(tmp_path, field, first, second):
    url = "https://example.com/shared-registration"
    bodies = [
        f"First: Example Other host World Climate Summit 2027 in Paris London; June 10–11, 2027; June 10–12, 2027. {url}",
        f"Second: Example Other host World Climate Summit 2027 in Paris London; June 10–11, 2027; June 10–12, 2027. {url}",
    ]
    database = _database(tmp_path, bodies)

    def extract(request):
        index = 0 if "First:" in request["article_body"] else 1
        values = [first, second]
        changes = {
            "organizer": "Example", "location": "Paris", "end_date": "2027-06-11",
        }
        changes[field] = values[index]
        end = changes["end_date"]
        evidence = "June 10–12, 2027" if end == "2027-06-12" else "June 10–11, 2027"
        return {"events": [_candidate(
            **changes, start_date="2027-06-10", raw_time_text=evidence,
            date_evidence=evidence, online_url=url,
            deadline_type=None, deadline_date=None, deadline_evidence=None,
        )]}

    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert len(records) == 2
    assert all(record["source_count"] == 1 for record in records)


def test_multiple_compatible_direct_url_histories_create_a_pending_candidate(tmp_path):
    url = "https://example.com/shared-registration"
    bodies = [
        f"Example hosts World Climate Summit 2027 in Paris on June 10, 2027. {url}",
        f"Example hosts World Climate Summit 2027 in London on June 10, 2027. {url}",
        f"Example lists World Climate Summit 2027 on June 10, 2027. {url}",
    ]
    database = _database(tmp_path, bodies)

    def extract(request):
        body = request["article_body"]
        location = "Paris" if "Paris" in body else "London" if "London" in body else None
        return {"events": [_candidate(
            start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
            date_evidence="June 10, 2027", location=location, online_url=url,
            deadline_type=None, deadline_date=None, deadline_evidence=None,
        )]}

    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert len(records) == 3
    pending = next(record for record in records if record["location"] is None)
    assert pending["needs_confirmation"] == 1
    assert pending["source_count"] == 1


@pytest.mark.parametrize("first_count", [1, 2])
def test_prompt_candidate_count_changes_do_not_change_occurrence_ids(tmp_path, first_count):
    body = (
        "Example hosts World Climate Summit 2027 on June 10, 2027. "
        "Example also hosts World Climate Summit 2027 on July 12, 2027."
    )
    database = _database(tmp_path, [body])
    june = _candidate(
        start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
        date_evidence="June 10, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    july = _candidate(
        start_date="2027-07-12", end_date=None, raw_time_text="July 12, 2027",
        date_evidence="July 12, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    first_candidates = [june] if first_count == 1 else [june, july]
    second_candidates = [june, july] if first_count == 1 else [june]
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": first_candidates},
    )
    june_id = next(
        record["event_id"] for record in query_events(
            database, base_date="2027-01-01", timezone_name="UTC",
        )["records"] if record["start_date"] == "2027-06-10"
    )
    process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: {"events": second_candidates},
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert [record["start_date"] for record in records] == ["2027-06-10", "2027-07-12"]
    june_record = next(record for record in records if record["start_date"] == "2027-06-10")
    assert june_record["event_id"] == june_id
    assert len(june_record["sources"]) == 2


@pytest.mark.parametrize(
    ("field", "enriched"),
    [
        ("online_url", "https://example.com/register"),
        ("organizer", "Example"),
        ("location", "New York"),
    ],
)
def test_same_content_interpretation_can_enrich_optional_identity_field(tmp_path, field, enriched):
    body = (
        "Example hosts World Climate Summit 2027 in New York on June 10–12, 2027. "
        "Register at https://example.com/register."
    )
    database = _database(tmp_path, [body])
    common = dict(location=None, deadline_type=None, deadline_date=None, deadline_evidence=None)
    first_candidate = _candidate(**common)
    first_candidate[field] = None
    second_candidate = _candidate(**common)
    second_candidate[field] = enriched

    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [first_candidate]},
    )
    first_id = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"][0]["event_id"]
    process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: {"events": [second_candidate]},
    )

    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert len(records) == 1
    assert records[0]["event_id"] == first_id
    assert records[0][field] == enriched
    assert records[0]["record_version"] == 2
    assert records[0]["source_count"] == 1
    assert len(records[0]["sources"]) == 2
    assert {source["interpretation_seq"] for source in records[0]["sources"]} == {1, 2}


@pytest.mark.parametrize("known_first", [False, True])
def test_same_content_null_interpretation_preserves_compatible_fact_groups(tmp_path, known_first):
    body = (
        "Example hosts World Climate Summit 2027 in Paris on June 10–12, 2027 UTC. "
        "Registration closes May 1, 2027 at https://example.com/register."
    )
    database = _database(tmp_path, [body])
    known = _candidate(timezone="UTC", location="Paris")
    missing = _candidate(
        organizer=None, end_date=None, raw_time_text=None, timezone=None, location=None,
        online_url=None, deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    first, second = (known, missing) if known_first else (missing, known)
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [first]},
    )
    process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: {"events": [second]},
    )
    record = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"][0]
    assert record["organizer"] == "Example"
    assert record["location"] == "Paris"
    assert record["online_url"] == "https://example.com/register"
    assert record["event_timezone"] == "UTC"
    assert record["raw_time_text"] == "June 10–12, 2027"
    assert record["end_date"] == "2027-06-12"
    assert (record["deadline_type"], record["deadline_date"]) == ("registration", "2027-05-01")
    versions = record["record_version"]
    sources = len(record["sources"])

    reused = process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: (_ for _ in ()).throw(AssertionError("idempotent run extracted")),
    )
    assert reused["reused"] is True
    repeated = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"][0]
    assert repeated["record_version"] == versions
    assert len(repeated["sources"]) == sources == 2

    connection = sqlite3.connect(database)
    candidates = [json.loads(row[0]) for row in connection.execute(
        "SELECT candidate_json FROM climate_event_sources ORDER BY interpretation_seq",
    )]
    connection.close()
    latest = candidates[-1]
    if known_first:
        assert latest["organizer"] is None
        assert latest["deadline_type"] is None
        assert latest["deadline_evidence"] is None
    else:
        assert latest["organizer"] == "Example"
        assert latest["deadline_type"] == "registration"


def test_same_content_reschedule_does_not_restore_prior_raw_time_or_end_date(tmp_path):
    url = "https://example.com/register"
    body = (
        f"Example hosts World Climate Summit 2027 on June 10–12, 2027 at {url}. "
        "It is rescheduled for July 1, 2027."
    )
    database = _database(tmp_path, [body])
    original = _candidate(
        location=None, online_url=url, deadline_type=None, deadline_date=None,
        deadline_evidence=None,
    )
    moved = _candidate(
        start_date="2027-07-01", end_date=None, raw_time_text=None,
        date_evidence="rescheduled for July 1, 2027", location=None, online_url=url,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [original]},
    )
    process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: {"events": [moved]},
    )
    record = query_events(
        database, base_date="2027-01-01", include_unknown=True, timezone_name="UTC",
    )["records"][0]
    assert record["start_date"] == "2027-07-01"
    assert record["end_date"] is None
    assert record["raw_time_text"] is None


def test_explicit_cancellation_withdrawal_and_later_cancellation_are_ordered(tmp_path):
    url = "https://example.com/world-summit"
    bodies = [
        f"Example hosts World Climate Summit on June 10, 2027. Register at {url}.",
        f"Example confirms World Climate Summit is cancelled. Register at {url}.",
        (
            "Example confirms World Climate Summit cancellation is withdrawn and the summit is "
            f"rescheduled for July 1, 2027. Register at {url}."
        ),
        f"Example later confirms World Climate Summit is cancelled. Register at {url}.",
    ]
    database = _database(tmp_path, bodies)

    def extraction(request, *, include_last=False):
        body = request["article_body"]
        if "later confirms" in body and not include_last:
            return {"events": []}
        if "cancellation is withdrawn" in body:
            return {"events": [_candidate(
                name="World Climate Summit", start_date="2027-07-01", end_date=None,
                raw_time_text="July 1, 2027",
                date_evidence=(
                    "cancellation is withdrawn and the summit is rescheduled for July 1, 2027"
                ),
                location=None, online_url=url, deadline_type=None, deadline_date=None,
                deadline_evidence=None,
            )]}
        cancelled = "cancelled" in body
        return {"events": [_candidate(
            name="World Climate Summit", status="cancelled" if cancelled else "scheduled",
            status_evidence="is cancelled" if cancelled else None,
            start_date=None if cancelled else "2027-06-10", end_date=None,
            raw_time_text=None if cancelled else "June 10, 2027",
            date_evidence=None if cancelled else "June 10, 2027",
            date_precision="unknown" if cancelled else "day",
            location=None, online_url=url, deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )]}

    first = process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=extraction,
    )
    current = query_events(
        database, base_date="2027-01-01", include_cancelled=True, include_unknown=True,
        timezone_name="UTC",
    )["records"][0]
    assert current["status"] == "scheduled"
    assert current["start_date"] == "2027-07-01"
    snapshot = freeze_snapshot(
        database, base_date="2027-01-01", include_cancelled=True, include_unknown=True,
        timezone_name="UTC",
    )

    process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: extraction(request, include_last=True),
    )
    latest = query_events(
        database, base_date="2027-01-01", include_cancelled=True, include_unknown=True,
        timezone_name="UTC",
    )["records"][0]
    assert latest["event_id"] == current["event_id"]
    assert latest["status"] == "cancelled"
    assert load_snapshot(database, snapshot["snapshot_id"])["records"][0]["status"] == "scheduled"
    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT count(*) FROM climate_event_versions WHERE event_id=?", (latest["event_id"],),
    ).fetchone()[0] >= 3
    assert connection.execute("SELECT count(*) FROM meeting_runs").fetchone() == (2,)
    connection.close()


def test_start_unknown_end_and_deadline_have_separate_evidence_roles(tmp_path):
    body = (
        "Climate Risk Meeting starts June 10, 2027; end date TBC; "
        "registration closes May 1, 2027."
    )
    candidate = _candidate(
        name="Climate Risk Meeting", event_type="meeting", organizer=None,
        start_date="2027-06-10", end_date=None, raw_time_text="starts June 10, 2027",
        date_evidence="starts June 10, 2027", location=None, online_url=None,
        deadline_type="registration", deadline_date="2027-05-01",
        deadline_evidence="May 1, 2027",
    )
    validated = validate_extraction({"events": [candidate]}, body=body)[0]
    assert validated["_single_day_supported"] is False
    database = _database(tmp_path, [body])
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [candidate]},
    )
    query = dict(
        base_date="2027-06-11", start_date="2027-06-11", end_date="2027-06-11",
        include_deadlines=True, timezone_name="UTC",
    )
    assert query_events(database, **query)["records"] == []
    pending = query_events(database, include_unknown=True, **query)["records"]
    assert len(pending) == 1
    assert pending[0]["needs_confirmation"] == 1


@pytest.mark.parametrize(
    "body",
    [
        "Climate Risk Meeting. It takes place on June 10, 2027.",
        "Climate Risk Meeting / Event details / Dates / June 10, 2027.",
        "Climate Risk Meeting\nEvent details\nDates\nJune 10, 2027.",
    ],
)
def test_transparent_event_date_labels_remain_single_day(body):
    candidate = _candidate(
        name="Climate Risk Meeting", event_type="meeting", organizer=None,
        start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
        date_evidence="June 10, 2027", location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    assert validate_extraction({"events": [candidate]}, body=body)[0]["_single_day_supported"] is True


@pytest.mark.parametrize(
    ("label", "displayed"),
    [
        ("ET", "ET"), ("ET", "(ET)"), ("EDT", "EDT"), ("BST", "BST"),
        ("UTC+1", "UTC+1"), ("America/New_York", "America/New_York"),
    ],
)
def test_source_timezone_labels_are_preserved_without_guessing(tmp_path, monkeypatch, label, displayed):
    if "/" in label:
        import climate_monitor.meetings as meetings

        monkeypatch.setattr(meetings, "ZoneInfo", lambda value: object())
    body = f"Example hosts Climate Risk Meeting on June 10, 2027 at 09:00 {displayed}."
    database = _database(tmp_path, [body])
    candidate = _candidate(
        name="Climate Risk Meeting", event_type="meeting", organizer="Example",
        start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
        date_evidence="June 10, 2027", timezone=label, location=None, online_url=None,
        deadline_type=None, deadline_date=None, deadline_evidence=None,
    )
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [candidate]},
    )
    record = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"][0]
    assert record["event_timezone"] == label
    if label == "ET":
        with pytest.raises(ValueError, match="valid IANA"):
            query_events(database, base_date="2027-01-01", timezone_name=label)


def test_source_timezone_label_requires_verbatim_body_evidence():
    candidate = _candidate(
        name="Climate Risk Meeting", organizer="Example", start_date="2027-06-10",
        end_date=None, raw_time_text="June 10, 2027", date_evidence="June 10, 2027",
        timezone="ET", location=None, online_url=None, deadline_type=None,
        deadline_date=None, deadline_evidence=None,
    )
    with pytest.raises(ValueError, match="not present"):
        validate_extraction(
            {"events": [candidate]},
            body="Example hosts Climate Risk Meeting on June 10, 2027.",
        )


def test_missing_timezone_remains_null():
    candidate = _candidate(
        name="Climate Risk Meeting", organizer="Example", start_date="2027-06-10",
        end_date=None, raw_time_text="June 10, 2027", date_evidence="June 10, 2027",
        timezone=None, location=None, online_url=None, deadline_type=None,
        deadline_date=None, deadline_evidence=None,
    )
    assert validate_extraction(
        {"events": [candidate]},
        body="Example hosts Climate Risk Meeting on June 10, 2027.",
    )[0]["timezone"] is None


@pytest.mark.parametrize(
    ("field", "first_value", "second_value", "body"),
    [
        (
            "organizer", "Alpha", "Beta",
            "Alpha and Beta host World Climate Summit 2027 on June 10–12, 2027.",
        ),
        (
            "online_url", "https://example.com/alpha", "https://example.com/beta",
            "Example hosts World Climate Summit 2027 on June 10–12, 2027. "
            "Links: https://example.com/alpha and https://example.com/beta.",
        ),
        (
            "location", "Paris", "London",
            "Example hosts World Climate Summit 2027 in Paris and London on June 10–12, 2027.",
        ),
    ],
)
def test_same_content_explicit_identity_conflict_keeps_events_separate(
    tmp_path, field, first_value, second_value, body,
):
    database = _database(tmp_path, [body])
    common = dict(
        location=None, online_url=None, deadline_type=None, deadline_date=None,
        deadline_evidence=None,
    )
    first_candidate = _candidate(**common)
    first_candidate[field] = first_value
    second_candidate = _candidate(**common)
    second_candidate[field] = second_value
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [first_candidate]},
    )
    process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: {"events": [second_candidate]},
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert len(records) == 2
    assert len({record["event_id"] for record in records}) == 2


def test_same_content_ambiguous_optional_match_does_not_choose_an_event(tmp_path):
    body = "Alpha and Beta host World Climate Summit 2027 on June 10–12, 2027."
    database = _database(tmp_path, [body])
    common = dict(
        location=None, online_url=None, deadline_type=None, deadline_date=None,
        deadline_evidence=None,
    )
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [
            _candidate(**common, organizer="Alpha"),
            _candidate(**common, organizer="Beta"),
        ]},
    )
    process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: {"events": [_candidate(**common, organizer=None)]},
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert len(records) == 3
    assert len({record["event_id"] for record in records}) == 3


def test_same_interpretation_same_day_different_locations_stay_distinct_and_stable(tmp_path):
    body = (
        "Example hosts World Climate Summit 2027 in Paris on June 10, 2027. "
        "Example hosts World Climate Summit 2027 in London on June 10, 2027."
    )
    database = _database(tmp_path, [body])
    candidates = [
        _candidate(
            start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
            date_evidence="June 10, 2027", location=location, online_url=None,
            deadline_type=None, deadline_date=None, deadline_evidence=None,
        )
        for location in ("Paris", "London")
    ]
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": candidates},
    )
    first = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    first_ids = {record["location"]: record["event_id"] for record in first}
    process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: {"events": list(reversed(candidates))},
    )
    second = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert {record["location"]: record["event_id"] for record in second} == first_ids
    assert all(len(record["sources"]) == 2 for record in second)


def test_shared_url_does_not_collapse_same_day_locations_or_reverse_reinterpretation(tmp_path):
    url = "https://example.com/world-summit"
    body = (
        f"Example hosts World Climate Summit 2027 in Paris on June 10, 2027. {url} "
        f"Example hosts World Climate Summit 2027 in London on June 10, 2027. {url}"
    )
    database = _database(tmp_path, [body])
    candidates = [
        _candidate(
            start_date="2027-06-10", end_date=None, raw_time_text="June 10, 2027",
            date_evidence="June 10, 2027", location=location, online_url=url,
            deadline_type=None, deadline_date=None, deadline_evidence=None,
        )
        for location in ("Paris", "London")
    ]
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": candidates},
    )
    first = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    first_ids = {record["location"]: record["event_id"] for record in first}
    process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: {"events": list(reversed(candidates))},
    )
    second = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert len(first_ids) == 2
    assert {record["location"]: record["event_id"] for record in second} == first_ids
    assert all(len(record["sources"]) == 2 for record in second)


def test_direct_url_links_explicit_cross_year_reschedule(tmp_path):
    url = "https://example.com/world-summit"
    database = _database(tmp_path, [
        f"Example hosts World Climate Summit on June 10, 2027. Register at {url}.",
        f"Example says World Climate Summit is rescheduled for July 1, 2028. Register at {url}.",
    ])

    def extract(request):
        moved = "rescheduled" in request["article_body"]
        return {"events": [_candidate(
            name="World Climate Summit", start_date="2028-07-01" if moved else "2027-06-10",
            end_date=None,
            raw_time_text="July 1, 2028" if moved else "June 10, 2027",
            date_evidence="rescheduled for July 1, 2028" if moved else "June 10, 2027",
            location=None, online_url=url, deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )]}

    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert len(records) == 1
    assert records[0]["start_date"] == "2028-07-01"
    assert records[0]["record_version"] == 2
    assert len(records[0]["sources"]) == 2


def test_direct_url_links_explicit_undated_cancellation(tmp_path):
    url = "https://example.com/world-summit"
    database = _database(tmp_path, [
        f"Example hosts World Climate Summit on June 10, 2027. Register at {url}.",
        f"Example says World Climate Summit is cancelled. Details at {url}.",
    ])

    def extract(request):
        cancelled = "cancelled" in request["article_body"]
        return {"events": [_candidate(
            name="World Climate Summit", status="cancelled" if cancelled else "scheduled",
            status_evidence="is cancelled" if cancelled else None,
            date_precision="unknown" if cancelled else "day",
            start_date=None if cancelled else "2027-06-10", end_date=None,
            raw_time_text=None if cancelled else "June 10, 2027",
            date_evidence=None if cancelled else "June 10, 2027",
            location=None, online_url=url, deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )]}

    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    records = query_events(
        database, base_date="2027-01-01", include_cancelled=True, include_unknown=True,
        timezone_name="UTC",
    )["records"]
    assert len(records) == 1
    assert records[0]["status"] == "cancelled"
    assert records[0]["record_version"] == 2
    assert len(records[0]["sources"]) == 2


def test_direct_url_does_not_merge_ordinary_cross_year_occurrences(tmp_path):
    url = "https://example.com/world-summit"
    database = _database(tmp_path, [
        f"Example hosts World Climate Summit on June 10, 2027. Register at {url}.",
        f"Example hosts World Climate Summit on July 12, 2027. Register at {url}.",
        f"Example hosts World Climate Summit on June 10, 2028. Register at {url}.",
    ])

    def extract(request):
        body = request["article_body"]
        event_date = (
            "2028-06-10" if "2028" in body else
            "2027-07-12" if "July" in body else "2027-06-10"
        )
        evidence = (
            "June 10, 2028" if "2028" in body else
            "July 12, 2027" if "July" in body else "June 10, 2027"
        )
        return {"events": [_candidate(
            name="World Climate Summit", start_date=event_date, end_date=None,
            raw_time_text=evidence, date_evidence=evidence,
            location=None, online_url=url, deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )]}

    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert [record["start_date"] for record in records] == [
        "2027-06-10", "2027-07-12", "2028-06-10",
    ]
    assert len({record["event_id"] for record in records}) == 3


def test_ambiguous_direct_url_update_does_not_choose_a_historical_occurrence(tmp_path):
    url = "https://example.com/world-summit"
    database = _database(tmp_path, [
        f"Example hosts World Climate Summit on June 10, 2027. Register at {url}.",
        f"Example hosts World Climate Summit on June 10, 2028. Register at {url}.",
        f"Example says World Climate Summit is cancelled. Details at {url}.",
    ])

    def extract(request):
        body = request["article_body"]
        cancelled = "cancelled" in body
        year = "2028" if "2028" in body else "2027"
        return {"events": [_candidate(
            name="World Climate Summit", status="cancelled" if cancelled else "scheduled",
            status_evidence="is cancelled" if cancelled else None,
            date_precision="unknown" if cancelled else "day",
            start_date=None if cancelled else f"{year}-06-10", end_date=None,
            raw_time_text=None if cancelled else f"June 10, {year}",
            date_evidence=None if cancelled else f"June 10, {year}", location=None,
            online_url=url, deadline_type=None, deadline_date=None, deadline_evidence=None,
        )]}

    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    records = query_events(
        database, base_date="2027-01-01", include_cancelled=True, include_unknown=True,
        timezone_name="UTC",
    )["records"]
    assert len(records) == 3
    cancelled = next(record for record in records if record["status"] == "cancelled")
    assert cancelled["needs_confirmation"] == 1
    assert cancelled["event_id"] not in {
        record["event_id"] for record in records if record["status"] == "scheduled"
    }


def test_zero_candidate_new_processing_does_not_cancel_existing_event(tmp_path):
    body = "Example hosts World Climate Summit 2027 on June 10–12, 2027."
    database = _database(tmp_path, [body])
    process_batch(
        database, "batch", prompt_text="v1", prompt_version="v1", provider="p", model="m",
        extractor=lambda request: {"events": [_candidate(
            location=None, online_url=None, deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )]},
    )
    process_batch(
        database, "batch", prompt_text="v2", prompt_version="v2", provider="p", model="m",
        extractor=lambda request: {"events": []},
    )
    records = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"]
    assert len(records) == 1
    assert records[0]["status"] == "scheduled"
    assert records[0]["source_count"] == 1
    assert records[0]["sources"][0]["is_current"] == 1


def test_confirmed_reschedule_replaces_postponed_current_date_and_keeps_history(tmp_path):
    bodies = [
        "Example says World Climate Summit 2027 is postponed from June 10–12, 2027.",
        "Example confirms World Climate Summit 2027 is rescheduled for July 1–2, 2027.",
    ]
    database = _database(tmp_path, bodies)

    def extract(request):
        if "June" in request["article_body"]:
            return {"events": [_candidate(
                status="postponed", status_evidence="is postponed",
                raw_time_text="June 10–12, 2027", date_evidence="June 10–12, 2027",
                location=None, online_url=None, deadline_type=None, deadline_date=None,
                deadline_evidence=None,
            )]}
        return {"events": [_candidate(
            status="scheduled", start_date="2027-07-01", end_date="2027-07-02",
            raw_time_text="July 1–2, 2027", date_evidence="July 1–2, 2027",
            location=None, online_url=None, deadline_type=None, deadline_date=None,
            deadline_evidence=None,
        )]}

    process_batch(
        database, "batch", prompt_text="prompt", prompt_version="v1", provider="p", model="m",
        extractor=extract,
    )
    current = query_events(database, base_date="2027-01-01", timezone_name="UTC")["records"][0]
    assert (current["status"], current["start_date"], current["end_date"]) == (
        "scheduled", "2027-07-01", "2027-07-02",
    )
    assert current["source_count"] == 2
    connection = sqlite3.connect(database)
    versions = connection.execute(
        "SELECT state_json FROM climate_event_versions WHERE event_id=? ORDER BY record_version",
        (current["event_id"],),
    ).fetchall()
    assert [json.loads(row[0])["status"] for row in versions] == ["postponed", "scheduled"]
    connection.close()
