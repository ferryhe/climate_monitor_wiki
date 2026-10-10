"""Common reader fields; PDF source claims remain distinct from collected facts."""
from __future__ import annotations

import re
from typing import Any

MEETING_FIELDS = (
    "name", "event_type", "organizer", "status", "raw_date", "date_precision",
    "start_date", "end_date", "raw_time_text", "event_timezone", "location",
    "online_url", "deadline_type", "deadline_date", "relevance_reason", "source_urls",
)
TIMEZONE_TEXT = re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)+|"
    r"(?:UTC|GMT)(?:[+-]\d{1,2}(?::\d{2})?)?|ET|CET|CEST|EST|EDT|PST|PDT|BST|JST)\b"
)
TIME_TEXT = re.compile(
    r"\b\d{1,2}:\d{2}(?:\s*(?:AM|PM)\b)?(?:\s*[-–—]\s*\d{1,2}:\d{2}(?:\s*(?:AM|PM)\b)?)?", re.I
)


def pdf_meeting_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize aliases without inventing event kind, status, venue or timezone."""
    result = dict(item)
    result.update(
        event_type=item.get("event_type"), organizer=item.get("organizer") or item.get("publisher"),
        status=item.get("status"), event_timezone=item.get("event_timezone") or item.get("timezone"),
        relevance_reason=item.get("relevance_reason") or item.get("relevance"),
        origin="pdf_import", collection_status=item.get("collection_status") or "pending",
    )
    # DATE(S) is date evidence. Only printed clock text belongs to raw_time_text.
    text = item.get("raw_date") or ""
    clock = TIME_TEXT.search(text)
    if clock and not result.get("raw_time_text"):
        result["raw_time_text"] = clock[0]
    zone = TIMEZONE_TEXT.search(text)
    if zone and not result.get("event_timezone"):
        result["event_timezone"] = zone[0]
    suffixes = [match.start() for match in (clock, zone) if match]
    if suffixes and result.get("date_precision") == "unknown":
        from .pdf_intake import _parse_calendar_date
        result.update(_parse_calendar_date(text[:min(suffixes)].rstrip(" ,;(")))
    if item.get("kind") == "deadline":
        result["event_type"] = "deadline"
        if result.get("date_precision") == "day" and result.get("start_date") and result.get("end_date") in {None, result["start_date"]}:
            result["deadline_date"] = item.get("deadline_date") or result["start_date"]
            result["start_date"] = result["end_date"] = None
            result["date_evidence"] = None
            result["deadline_evidence"] = item.get("deadline_evidence") or text
        if not result.get("deadline_type"):
            wording = " ".join(str(item.get(key) or "") for key in ("name", "summary"))
            for label, pattern in (("registration", r"\bregistration\b"), ("consultation", r"\bconsultation\b"),
                ("expert_review", r"\bexpert review\b")):
                if re.search(pattern, wording, re.I):
                    result["deadline_type"] = label
                    break
    for field in MEETING_FIELDS:
        result.setdefault(field, [] if field == "source_urls" else None)
    return result


def collected_pdf_meeting(item: dict[str, Any]) -> dict[str, Any]:
    candidate = item.get("collected_candidate")
    if not candidate or item.get("verification_status") != "verified":
        return item
    source_url = next((
        check.get("source_url") for check in item.get("checks", [])
        if check.get("verification_status") == "verified" and check.get("website_candidate") == candidate
    ), None)
    return {**item, **candidate, "event_id": item["canonical_event_id"], "pdf_event_id": item["event_id"],
        "event_timezone": candidate.get("timezone"), "pdf_observations": [dict(item)],
        "collected_candidate_source_url": source_url,
        "relevance_reason": item.get("relevance_reason"), "website_relevance_reason": candidate.get("relevance_reason")}


def merge_meeting_observations(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge validated identities, retaining source-local pending observations."""
    merged = {}
    for raw in records:
        item = dict(raw)
        imported = bool(item.get("source_document_sha256"))
        identity = (item.get("canonical_event_id") or item.get("occurrence_id")) if imported else item.get("event_id")
        identity = identity or str(len(merged))
        if identity not in merged:
            merged[identity] = item
            continue
        existing = merged[identity]
        for field in MEETING_FIELDS:
            if field not in {"source_urls", "relevance_reason"} and not existing.get(field):
                existing[field] = item.get(field)
        existing["source_urls"] = list(dict.fromkeys(existing.get("source_urls", []) + item.get("source_urls", [])))
        observations = existing.get("pdf_observations", []) + item.get("pdf_observations", [])
        if observations:
            existing["pdf_observations"] = observations
            existing["source_kind"] = "pdf"
            if not existing.get("raw_date"):
                existing["raw_date"] = observations[0].get("raw_date")
            existing.setdefault("relevance", observations[0].get("relevance_reason"))
    return list(merged.values())
