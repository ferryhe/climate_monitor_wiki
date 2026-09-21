"""Meeting extraction from immutable Registry article bodies.

The model is an untrusted candidate extractor.  This module owns input binding,
validation, identity, persistence, conflict handling, queries, and snapshots.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from calendar import monthrange
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from climate_registry.contract import SCHEMA_VERSION, validate_registry_contract


EVENT_TYPES = {"meeting", "conference", "summit", "webinar", "deadline", "retrospective"}
EVENT_STATUSES = {"scheduled", "tentative", "postponed", "cancelled", "retrospective"}
DATE_PRECISIONS = {"day", "month", "quarter", "year", "unknown"}
DEADLINE_TYPES = {"registration", "consultation", "expert_review"}
EXTRACTION_FIELDS = {
    "name", "event_type", "organizer", "status", "date_precision", "start_date",
    "end_date", "raw_time_text", "timezone", "location", "online_url",
    "deadline_type", "deadline_date", "date_evidence", "deadline_evidence",
    "status_evidence", "relevance_reason",
}
_METADATA_DATE = re.compile(
    r"\b(?:published|publication|posted|updated|last\s+(?:updated?|modified)|"
    r"retrieved|fetched|report date|article\s+date)\b",
    re.IGNORECASE,
)
_RESCHEDULE_CONTEXT = re.compile(r"\b(?:rescheduled|new date|now scheduled)\b", re.I)
_CANCELLATION_WITHDRAWN = re.compile(
    r"\b(?:cancell?ation|cancelled|canceled)\b[^.;\n]{0,50}"
    r"\b(?:withdrawn|rescinded|revoked|reversed)\b|"
    r"\bno longer cancelle?d\b",
    re.I,
)
_RETROSPECTIVE = re.compile(r"\b(?:minutes|recording|recap|proceedings|meeting summary)\b", re.I)
_STATUS_CONTEXT = {
    "tentative": re.compile(r"\b(?:tentative(?:ly)?|provisional|proposed|to be confirmed|tbc)\b", re.I),
    "postponed": re.compile(r"\b(?:postponed|postponement|delay(?:ed)?|deferred|deferral|put off)\b", re.I),
    "cancelled": re.compile(r"\b(?:cancelled|canceled|cancellation|called off|will not (?:proceed|take place|be held))\b", re.I),
}
_DEADLINE_MARKER = re.compile(r"\b(?:deadline|due|closes?|closing|by)\b", re.I)
_DEADLINE_TYPE_CONTEXT = {
    "registration": re.compile(r"\b(?:registration|register|sign[ -]?up|applications?|apply)\b", re.I),
    "consultation": re.compile(r"\b(?:consultation|public comments?|consultation responses?|submissions?|submit)\b", re.I),
    "expert_review": re.compile(r"\b(?:expert review|peer review|reviewers?|review comments?|review)\b", re.I),
}
_UNRESOLVED_EVENT_END = re.compile(
    r"\b(?:end(?: date)?|duration|date)\b[^.;\n]{0,30}\b(?:tbc|unknown|to be confirmed)\b|"
    r"\bto be confirmed\b",
    re.I,
)
_SOURCE_TIMEZONE_LABEL = re.compile(
    r"(?:ET|EDT|BST|UTC(?:[+-](?:0?\d|1[0-4])(?::[0-5]\d)?)?)",
    re.I,
)
_DATE_ONLY_SEGMENT = re.compile(
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"(?:\s+\d{1,2}(?:\s*[-–—]\s*\d{1,2})?,?)?\s+20\d{2}",
    re.I,
)


def _now(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string" + (" or null" if optional else ""))
    return " ".join(value.split())


def _open(database: str | Path, *, write: bool = False) -> sqlite3.Connection:
    path = Path(database).resolve(strict=True)
    connection = sqlite3.connect(f"{path.as_uri()}?mode={'rw' if write else 'ro'}", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    version = validate_registry_contract(connection)
    if version != SCHEMA_VERSION:
        connection.close()
        raise ValueError(f"meeting processing requires Registry schema {SCHEMA_VERSION}; found {version}")
    return connection


def _partial_date(value: Any, precision: str, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a date string or null")
    patterns = {
        "day": r"\d{4}-\d{2}-\d{2}",
        "month": r"\d{4}-\d{2}",
        "quarter": r"\d{4}-Q[1-4]",
        "year": r"\d{4}",
    }
    if precision == "unknown" or not re.fullmatch(patterns[precision], value):
        raise ValueError(f"{field} does not match {precision} precision")
    try:
        if precision == "day":
            date.fromisoformat(value)
        elif precision == "month":
            date.fromisoformat(value + "-01")
        elif precision == "year":
            date(int(value), 1, 1)
    except ValueError as exc:
        raise ValueError(f"{field} is not a real calendar value") from exc
    return value


def _supported_evidence(body: str, value: Any, field: str) -> str | None:
    evidence = _text(value, field, optional=True)
    if evidence is not None and evidence.casefold() not in body.casefold():
        raise ValueError(f"{field} is not present in the bound article body")
    return evidence


def _evidence_contexts(body: str, evidence: str) -> list[str]:
    """Return the containing sentence/clause for every verbatim evidence match."""
    result = []
    folded_body, folded_evidence = body.casefold(), evidence.casefold()
    offset = 0
    while (start := folded_body.find(folded_evidence, offset)) >= 0:
        left = max(body.rfind(".", 0, start), body.rfind("\n", 0, start), body.rfind(";", 0, start)) + 1
        right_candidates = [position for marker in (".", "\n", ";")
                            if (position := body.find(marker, start + len(evidence))) >= 0]
        right = min(right_candidates) if right_candidates else len(body)
        result.append(body[left:right])
        offset = start + max(1, len(evidence))
    return result


def _valid_date_evidence_context(body: str, evidence: str) -> bool:
    if _METADATA_DATE.search(evidence):
        return False
    # A model may copy only the date and omit an adjacent "Published" label.
    # Accept duplicate date text when at least one occurrence has event context.
    return any(not _METADATA_DATE.search(context) for context in _evidence_contexts(body, evidence))


def _semantic_clauses(context: str) -> list[str]:
    return [
        clause for clause in re.split(r"\b(?:while|but|whereas)\b", context, flags=re.I)
        if clause.strip()
    ]


def _local_evidence_contexts(contexts: Sequence[str], evidence: str) -> list[str]:
    folded = evidence.casefold()
    return [
        clause for context in contexts for clause in _semantic_clauses(context)
        if folded in clause.casefold()
    ]


_ADJACENT_EVIDENCE_LABEL = re.compile(
    r"(?:"
    r"(?:the\s+)?(?:event\s+)?dates?\s*(?:(?:is|are)\s*)?[:\-–—]?|"
    r"(?:registration|applications?|entries|submissions?|consultation(?:\s+responses?)?|"
    r"expert\s+review(?:\s+comments?)?|review\s+comments?)\s+"
    r"(?:(?:is|are)\s+due|(?:deadline\s+)?(?:is|are|closes?|closing|due)|deadline)|"
    r"event\s+details|"
    r"it\s+(?:had\s+been|is|was)(?:\s+rescheduled\s+for)?|"
    r"it\s+takes?\s+place\s+on|"
    r"on"
    r")",
    re.I,
)


def _is_adjacent_evidence_label(context: str, evidence_offset: int) -> bool:
    prefix = context[:evidence_offset]
    if ";" in prefix or "|" in prefix or "/" in prefix:
        prefix = re.split(r"[;|/]", prefix)[-1]
    prefix = prefix.strip(" \t:|,-–—")
    return not prefix or _ADJACENT_EVIDENCE_LABEL.fullmatch(prefix) is not None


def _transparent_binding_segment(value: str) -> bool:
    segment = value.strip(" \t:|/,-–—")
    start_expression = re.fullmatch(
        rf"(?:on|starts?|begins?|commences?|opens?|held(?:\s+on)?|takes?\s+place(?:\s+on)?|"
        rf"scheduled(?:\s+for)?|rescheduled(?:\s+for)?)(?:\s+{_DATE_ONLY_SEGMENT.pattern})?",
        segment, re.I,
    )
    unresolved_end = re.fullmatch(
        r"(?:end(?:\s+date)?|duration|date)\s*(?:(?:is|are)\s+)?"
        r"(?:tbc|unknown|to\s+be\s+confirmed)",
        segment, re.I,
    )
    return bool(
        not segment
        or _ADJACENT_EVIDENCE_LABEL.fullmatch(segment)
        or _DATE_ONLY_SEGMENT.fullmatch(segment)
        or unresolved_end
        or start_expression
        or re.fullmatch(r"at\s+https?://\S+", segment, re.I)
    )


def _complete_name_pattern(name: str) -> re.Pattern[str] | None:
    words = [re.escape(word) for word in name.split()]
    if not words:
        return None
    flexible_name = r"\s+".join(words)
    return re.compile(
        rf"(?<![A-Za-z0-9_]){flexible_name}(?![A-Za-z0-9_])",
        re.I,
    )


def _subject_name_matches(value: str, name: str) -> bool:
    pattern = _complete_name_pattern(name)
    if pattern is None:
        return False
    for match in pattern.finditer(value):
        prefix = value[:match.start()].strip()
        if not prefix.strip(" \t#>*_`'\"([{:-–—"):
            return True
        if re.fullmatch(r"the", prefix, re.I):
            return True
        if re.search(
            r"\b(?:hosts?|presents?|announc(?:e[sd]?|ing)|confirms?|says?|states?|reports?|"
            r"organizes?|convenes?|holds?|welcomes?|opens?|lists?|join|attend)(?:\s+the)?$",
            prefix, re.I,
        ):
            return True
    return False


def _evidence_owner_matches(
    context: str, evidence_offset: int, evidence_length: int, name: str,
) -> bool | None:
    """Resolve the nearest subject around one evidence segment before matching its name."""
    evidence_end = evidence_offset + evidence_length
    if _subject_name_matches(context[evidence_offset:evidence_end], name):
        return True
    url_spans = [match.span() for match in re.finditer(r"https?://\S+", context, re.I)]
    separators = [
        match for match in re.finditer(r"[;|/]", context)
        if not (evidence_offset <= match.start() < evidence_end)
        and not any(left <= match.start() < right for left, right in url_spans)
    ]
    bounds = []
    start = 0
    for separator in separators:
        bounds.append((start, separator.start()))
        start = separator.end()
    bounds.append((start, len(context)))
    segment_index = next(
        index for index, (start, end) in enumerate(bounds)
        if start <= evidence_offset and evidence_end <= end
    )
    segment_left, segment_right = bounds[segment_index]
    left = context[segment_left:evidence_offset]
    if not _transparent_binding_segment(left):
        return _subject_name_matches(left, name)
    for index in range(segment_index - 1, -1, -1):
        part = context[slice(*bounds[index])]
        if not _transparent_binding_segment(part):
            return _subject_name_matches(part, name)

    right = context[evidence_end:segment_right]
    if re.fullmatch(
        r"\s*it\s+(?:had\s+been|is|was)(?:\s+rescheduled\s+for)?\s*",
        left, re.I,
    ):
        return None
    if not _transparent_binding_segment(right):
        return _subject_name_matches(right, name) and sum(
            _subject_name_matches(context[slice(*bound)], name) for bound in bounds
        ) == 1
    for index in range(segment_index + 1, len(bounds)):
        part = context[slice(*bounds[index])]
        if not _transparent_binding_segment(part):
            return _subject_name_matches(part, name) and sum(
                _subject_name_matches(context[slice(*bound)], name) for bound in bounds
            ) == 1
    return None


def _preceding_candidate_title(body: str, sentence_left: int, name: str) -> str | None:
    remaining = body[:sentence_left].rstrip(" .\r\n\t")
    while remaining:
        breaks = list(re.finditer(r"\.(?=\s)|\n", remaining))
        boundary = breaks[-1].start() if breaks else -1
        previous = remaining[boundary + 1:].strip()
        if _subject_name_matches(previous, name):
            return previous
        if not (
            _METADATA_DATE.match(previous)
            or _ADJACENT_EVIDENCE_LABEL.match(previous)
        ):
            return None
        remaining = remaining[:boundary].rstrip(" .\r\n\t")
    return None


def _bound_evidence_occurrences(
    body: str, evidence: str, name: str,
) -> list[tuple[str, int]]:
    result = []
    folded_body, folded_evidence = body.casefold(), evidence.casefold()
    offset = 0
    while (start := folded_body.find(folded_evidence, offset)) >= 0:
        sentence_left = max(body.rfind(".", 0, start), body.rfind("\n", 0, start)) + 1
        right_candidates = [position for marker in (".", "\n")
                            if (position := body.find(marker, start + len(evidence))) >= 0]
        sentence_right = min(right_candidates) if right_candidates else len(body)
        clause_left, clause_right = sentence_left, sentence_right
        for connector in re.finditer(
            r"\b(?:while|but|whereas)\b", body[sentence_left:sentence_right], re.I,
        ):
            connector_start = sentence_left + connector.start()
            connector_end = sentence_left + connector.end()
            if connector_end <= start:
                clause_left = connector_end
            elif connector_start >= start + len(evidence):
                clause_right = connector_start
                break
        context = body[clause_left:clause_right]
        evidence_offset = start - clause_left
        ownership = _evidence_owner_matches(context, evidence_offset, len(evidence), name)
        if ownership is True:
            result.append((context, evidence_offset))
        elif ownership is None:
            previous = _preceding_candidate_title(body, sentence_left, name)
            if (
                previous
                and _is_adjacent_evidence_label(context, evidence_offset)
            ):
                result.append((previous + "; " + context, len(previous) + 2 + evidence_offset))
        offset = start + max(1, len(evidence))
    return result


def _bound_evidence_contexts(body: str, evidence: str, name: str) -> list[str]:
    return [context for context, _ in _bound_evidence_occurrences(body, evidence, name)]


def _event_date_context(body: str, evidence: str, name: str) -> bool:
    escaped = re.escape(evidence)
    for context in _bound_evidence_contexts(body, evidence, name):
        for local in _local_evidence_contexts(_evidence_contexts(context, evidence), evidence):
            if _METADATA_DATE.search(local):
                continue
            deadline = _DEADLINE_MARKER.search(local)
            explicit_event = re.search(
                rf"\b(?:held|takes? place|scheduled(?: for)?|convenes?)\b[^.;\n]{{0,40}}{escaped}",
                local, re.I,
            ) or re.search(
                rf"\b(?:event|meeting|conference|summit|webinar)\b(?:\s+20\d{{2}})?"
                rf"\s+(?:is\s+)?on\s+{escaped}",
                local, re.I,
            )
            if not deadline or explicit_event:
                return True
    return False


def _deadline_context(body: str, evidence: str, deadline_type: str, name: str) -> bool:
    return any(
        not _METADATA_DATE.search(context)
        and _DEADLINE_MARKER.search(context)
        and _DEADLINE_TYPE_CONTEXT[deadline_type].search(context)
        for context in _bound_evidence_contexts(body, evidence, name)
    )


def _status_context(body: str, evidence: str, status: str, name: str) -> bool:
    pattern = _STATUS_CONTEXT[status]
    return any(
        pattern.search(local)
        for context in _bound_evidence_contexts(body, evidence, name)
        for local in _local_evidence_contexts(_evidence_contexts(context, evidence), evidence)
    )


def _owner_local_context(
    context: str, evidence_offset: int, evidence_length: int, name: str,
) -> str:
    evidence_end = evidence_offset + evidence_length
    url_spans = [match.span() for match in re.finditer(r"https?://\S+", context, re.I)]
    separators = [
        match for match in re.finditer(r"[;|/]", context)
        if not (evidence_offset <= match.start() < evidence_end)
        and not any(left <= match.start() < right for left, right in url_spans)
    ]
    bounds = []
    start = 0
    for separator in separators:
        bounds.append((start, separator.start()))
        start = separator.end()
    bounds.append((start, len(context)))
    evidence_index = next(
        index for index, (left, right) in enumerate(bounds)
        if left <= evidence_offset and evidence_end <= right
    )
    segment_left, segment_right = bounds[evidence_index]
    if _subject_name_matches(context[segment_left:segment_right], name):
        return context[segment_left:segment_right]
    for index in range(evidence_index - 1, -1, -1):
        part = context[slice(*bounds[index])]
        if not _transparent_binding_segment(part):
            return context[bounds[index][0]:segment_right]
    for index in range(evidence_index + 1, len(bounds)):
        part = context[slice(*bounds[index])]
        if not _transparent_binding_segment(part):
            return context[segment_left:bounds[index][1]]
    return context[segment_left:segment_right]


def _has_reschedule_context(body: str, evidence: str | None, name: str) -> bool:
    return bool(evidence and any(
        _RESCHEDULE_CONTEXT.search(local) and not _METADATA_DATE.search(local)
        for context, offset in _bound_evidence_occurrences(body, evidence, name)
        for local in [_owner_local_context(context, offset, len(evidence), name)]
    ))


def _has_cancellation_withdrawal(body: str, evidence: str | None, name: str) -> bool:
    return bool(evidence and any(
        _CANCELLATION_WITHDRAWN.search(context)
        for context in _bound_evidence_contexts(body, evidence, name)
    ))


def _timezone_label_in_body(body: str, label: str) -> bool:
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(label)}(?![A-Za-z0-9_+\-])",
        body, re.I,
    ) is not None


def _date_matches_evidence(value: str, precision: str, evidence: str) -> bool:
    if value[:4] not in evidence:
        return False
    if precision == "year":
        return True
    if precision == "quarter":
        quarter = value[-1]
        word = {"1": "first", "2": "second", "3": "third", "4": "fourth"}[quarter]
        return bool(re.search(rf"\b(?:q{quarter}|{word}\s+quarter)\b", evidence, re.I))
    parsed = date.fromisoformat(value + ("-01" if precision == "month" else ""))
    month_present = (
        re.search(rf"\b(?:{parsed.strftime('%B')}|{parsed.strftime('%b')})\b", evidence, re.I)
        or re.search(rf"(?:^|\D)0?{parsed.month}(?:\D|$)", evidence)
    )
    return bool(month_present) and (
        precision == "month" or bool(re.search(rf"(?<!\d)0?{parsed.day}(?!\d)", evidence))
    )


def validate_extraction(value: Any, *, body: str) -> list[dict[str, Any]]:
    """Strictly validate one model response against the exact bound body."""
    if not isinstance(value, Mapping) or set(value) != {"events"} or not isinstance(value["events"], list):
        raise ValueError("meeting extraction must be exactly a JSON object with an events array")
    result: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(value["events"], 1):
        if not isinstance(raw, Mapping) or set(raw) != EXTRACTION_FIELDS:
            raise ValueError(f"event {ordinal} has unexpected or missing fields")
        event_type = raw["event_type"]
        status = raw["status"]
        precision = raw["date_precision"]
        if event_type not in EVENT_TYPES:
            raise ValueError(f"event {ordinal} has unsupported event_type")
        if status not in EVENT_STATUSES:
            raise ValueError(f"event {ordinal} has unsupported status")
        if precision not in DATE_PRECISIONS:
            raise ValueError(f"event {ordinal} has unsupported date_precision")
        name = _text(raw["name"], "name")
        organizer = _text(raw["organizer"], "organizer", optional=True)
        start = _partial_date(raw["start_date"], precision, "start_date")
        end = _partial_date(raw["end_date"], precision, "end_date")
        if precision == "unknown" and (start is not None or end is not None):
            raise ValueError("unknown date precision cannot carry structured event dates")
        if end is not None and start is None:
            raise ValueError("end_date requires start_date")
        if start and end and _date_bounds(start, precision, end=True) > _date_bounds(end, precision, end=True):
            raise ValueError("event end_date precedes start_date")
        date_evidence = _supported_evidence(body, raw["date_evidence"], "date_evidence")
        if (start is not None or end is not None) and date_evidence is None:
            raise ValueError("structured event dates require verbatim date_evidence")
        if date_evidence and any(
            not _date_matches_evidence(value, precision, date_evidence)
            for value in (start, end) if value is not None
        ):
            raise ValueError("structured event date is inconsistent with date_evidence")
        if date_evidence and not _event_date_context(body, date_evidence, name):
            raise ValueError("publication, fetch, update, report, and deadline dates are not event-date evidence")
        deadline_type = raw["deadline_type"]
        if deadline_type is not None and deadline_type not in DEADLINE_TYPES:
            raise ValueError("deadline_type is unsupported")
        deadline_date = raw["deadline_date"]
        if deadline_date is not None:
            try:
                deadline_date = date.fromisoformat(str(deadline_date)).isoformat()
            except ValueError as exc:
                raise ValueError("deadline_date must be an ISO calendar date or null") from exc
        deadline_evidence = _supported_evidence(body, raw["deadline_evidence"], "deadline_evidence")
        if bool(deadline_type) != bool(deadline_date) or bool(deadline_date) != bool(deadline_evidence):
            raise ValueError("deadline type, date, and verbatim evidence must be supplied together")
        if deadline_evidence and not _valid_date_evidence_context(body, deadline_evidence):
            raise ValueError("publication, fetch, update, or report dates are not deadline evidence")
        if event_type == "deadline" and deadline_date is None:
            raise ValueError("deadline candidates require deadline fields")
        if deadline_date and not _date_matches_evidence(deadline_date, "day", deadline_evidence or ""):
            raise ValueError("deadline_date is inconsistent with deadline_evidence")
        if deadline_evidence and not _deadline_context(body, deadline_evidence, deadline_type, name):
            raise ValueError("deadline_evidence lacks a matching deadline type and closing structure")
        if status == "retrospective" and event_type != "retrospective":
            raise ValueError("retrospective status requires retrospective type")
        if event_type == "retrospective" and status != "retrospective":
            raise ValueError("retrospective type requires retrospective status")
        raw_time = _text(raw["raw_time_text"], "raw_time_text", optional=True)
        if raw_time is not None and raw_time.casefold() not in body.casefold():
            raise ValueError("raw_time_text is not present in the bound article body")
        if event_type != "retrospective" and _RETROSPECTIVE.search(" ".join(filter(None, [name, raw_time, date_evidence]))):
            raise ValueError("minutes, recordings, recaps, and proceedings must be retrospective")
        status_evidence = _supported_evidence(body, raw["status_evidence"], "status_evidence")
        if status in {"tentative", "postponed", "cancelled"} and status_evidence is None:
            raise ValueError("tentative, postponed, and cancelled status require verbatim status_evidence")
        if status_evidence and status not in {"tentative", "postponed", "cancelled"}:
            raise ValueError("status_evidence is only valid for non-scheduled current status")
        if status_evidence and not _status_context(body, status_evidence, status, name):
            raise ValueError("status_evidence does not support the candidate status")
        event_timezone = _text(raw["timezone"], "timezone", optional=True)
        if event_timezone:
            try:
                timezone.utc if event_timezone == "UTC" else ZoneInfo(event_timezone)
            except Exception as exc:
                if _SOURCE_TIMEZONE_LABEL.fullmatch(event_timezone) is None:
                    raise ValueError(
                        "timezone must be a valid IANA timezone or supported source label"
                    ) from exc
            if not _timezone_label_in_body(body, event_timezone):
                raise ValueError("timezone is not present in the bound article body")
        online_url = _text(raw["online_url"], "online_url", optional=True)
        if online_url:
            parsed = urlsplit(online_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or online_url not in body:
                raise ValueError("online_url must be an HTTP URL present in the bound article body")
        location = _text(raw["location"], "location", optional=True)
        if location and location.casefold() not in body.casefold():
            raise ValueError("location is not present in the bound article body")
        name_pattern = _complete_name_pattern(name)
        if name_pattern is None or name_pattern.search(body) is None:
            raise ValueError("event name is not present in the bound article body")
        if organizer and organizer.casefold() not in body.casefold():
            raise ValueError("organizer is not present in the bound article body")
        result.append({
            "name": name,
            "event_type": event_type,
            "organizer": organizer,
            "status": status,
            "date_precision": precision,
            "start_date": start,
            "end_date": end,
            "raw_time_text": raw_time,
            "timezone": event_timezone,
            "location": location,
            "online_url": online_url,
            "deadline_type": deadline_type,
            "deadline_date": deadline_date,
            "date_evidence": date_evidence,
            "deadline_evidence": deadline_evidence,
            "status_evidence": status_evidence,
            "relevance_reason": _text(raw["relevance_reason"], "relevance_reason", optional=True),
            "_reschedule_supported": _has_reschedule_context(body, date_evidence, name),
            "_cancellation_withdrawn": _has_cancellation_withdrawal(
                body, date_evidence, name,
            ),
            "_single_day_supported": _single_day_evidence(
                start, precision, end, date_evidence,
                _bound_evidence_contexts(body, date_evidence, name) if date_evidence else [],
            ),
        })
    return result


def _normalized_identity_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _base_event_identity(candidate: Mapping[str, Any], content_version_id: str) -> dict[str, Any]:
    name = _normalized_identity_text(candidate["name"])
    organizer = _normalized_identity_text(candidate.get("organizer"))
    raw = " ".join(filter(None, [candidate.get("start_date"), candidate.get("raw_time_text"), candidate["name"]]))
    year = next(iter(re.findall(r"\b20\d{2}\b", raw)), None)
    edition = next(iter(re.findall(r"\b\d+(?:st|nd|rd|th)\b", name)), None)
    # Unknown identity stays source-local: losing two real candidates is worse than a deferred merge.
    local = None if year or edition else content_version_id
    return {"name": name, "organizer": organizer, "year": year, "edition": edition, "local": local}


def _default_event_key(candidate: Mapping[str, Any], content_version_id: str) -> dict[str, Any]:
    base = _base_event_identity(candidate, content_version_id)
    if candidate.get("online_url"):
        discriminator = {
            "direct_event_url": candidate["online_url"],
            "occurrence_start": candidate.get("start_date"),
            "occurrence_end": candidate.get("end_date"),
            "occurrence_location": _normalized_identity_text(candidate.get("location")),
        }
    elif base["edition"]:
        discriminator = {"edition": base["edition"]}
    elif candidate.get("start_date"):
        discriminator = {
            "occurrence_start": candidate["start_date"],
            "occurrence_end": candidate.get("end_date"),
            "occurrence_location": _normalized_identity_text(candidate.get("location")),
        }
    else:
        discriminator = {"source_content": content_version_id}
    return base | discriminator


def _related_event_ids(
    connection: sqlite3.Connection, candidate: Mapping[str, Any], content_version_id: str,
    *, same_start: bool = False, historical_sources: Sequence[Mapping[str, Any]] | None = None,
) -> set[str]:
    expected = _base_event_identity(candidate, content_version_id)
    expected["local"] = None
    result = set()
    rows = historical_sources if historical_sources is not None else connection.execute(
        "SELECT event_id, content_version_id, candidate_json FROM climate_event_sources"
    )
    for row in rows:
        historical = json.loads(row["candidate_json"])
        identity = _base_event_identity(historical, row["content_version_id"])
        identity["local"] = None
        if identity == expected and (
            not same_start or historical.get("start_date") == candidate.get("start_date")
        ):
            result.add(row["event_id"])
    return result


def _same_content_event_ids(
    connection: sqlite3.Connection, candidate: Mapping[str, Any], content_version_id: str,
    historical_sources: Sequence[Mapping[str, Any]] | None = None,
) -> set[str]:
    """Find one prior interpretation that the current candidate can safely enrich."""
    if candidate.get("start_date") is None:
        return set()
    expected_name = _normalized_identity_text(candidate["name"])
    expected_organizer = _normalized_identity_text(candidate.get("organizer"))
    expected_url = candidate.get("online_url")
    rows = [
        row for row in (historical_sources if historical_sources is not None else connection.execute(
            """SELECT event_id, content_version_id, candidate_json, interpretation_seq
               FROM climate_event_sources"""
        ))
        if row["content_version_id"] == content_version_id
    ]
    latest = {
        event_id: max(row["interpretation_seq"] for row in rows if row["event_id"] == event_id)
        for event_id in {row["event_id"] for row in rows}
    }
    result = set()
    for row in rows:
        if row["interpretation_seq"] != latest[row["event_id"]]:
            continue
        historical = json.loads(row["candidate_json"])
        historical_organizer = _normalized_identity_text(historical.get("organizer"))
        historical_url = historical.get("online_url")
        historical_location = _normalized_identity_text(historical.get("location"))
        expected_location = _normalized_identity_text(candidate.get("location"))
        if (
            _normalized_identity_text(historical["name"]) != expected_name
            or historical["event_type"] != candidate["event_type"]
            or historical.get("start_date") != candidate.get("start_date")
            or (
                historical.get("end_date") is not None
                and candidate.get("end_date") is not None
                and historical["end_date"] != candidate["end_date"]
            )
            or (historical_organizer and expected_organizer and historical_organizer != expected_organizer)
            or (historical_url and expected_url and historical_url != expected_url)
            or (historical_location and expected_location and historical_location != expected_location)
        ):
            continue
        result.add(row["event_id"])
    return result


def _direct_update_event_ids(
    candidate: Mapping[str, Any], historical_sources: Sequence[Mapping[str, Any]],
) -> set[str]:
    direct_url = candidate.get("online_url")
    if not direct_url:
        return set()
    expected_name = _normalized_identity_text(candidate["name"])
    expected_organizer = _normalized_identity_text(candidate.get("organizer"))
    result = set()
    for row in historical_sources:
        historical = json.loads(row["candidate_json"])
        historical_organizer = _normalized_identity_text(historical.get("organizer"))
        if (
            historical.get("online_url") == direct_url
            and _normalized_identity_text(historical["name"]) == expected_name
            and not (
                historical_organizer and expected_organizer
                and historical_organizer != expected_organizer
            )
        ):
            result.add(row["event_id"])
    return result


def _historical_direct_event_ids(
    connection: sqlite3.Connection, candidate: Mapping[str, Any],
    historical_sources: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Match one established occurrence by its direct URL without using this run's siblings."""
    direct_url = candidate.get("online_url")
    start = candidate.get("start_date")
    if not direct_url or not start:
        return set()
    linked_ids = {
        row["event_id"] for row in historical_sources
        if json.loads(row["candidate_json"]).get("online_url") == direct_url
    }
    result = set()
    expected_organizer = _normalized_identity_text(candidate.get("organizer"))
    expected_location = _normalized_identity_text(candidate.get("location"))
    for event_id in linked_ids:
        current = connection.execute(
            "SELECT * FROM climate_events WHERE event_id=?", (event_id,),
        ).fetchone()
        if current is None:
            continue
        current_organizer = _normalized_identity_text(current["organizer"])
        current_location = _normalized_identity_text(current["location"])
        if (
            _normalized_identity_text(current["name"])
            != _normalized_identity_text(candidate["name"])
            or current["event_type"] != candidate["event_type"]
            or current["start_date"] != start
            or (current_organizer and expected_organizer and current_organizer != expected_organizer)
            or (current_location and expected_location and current_location != expected_location)
            or (current["end_date"] and candidate.get("end_date")
                and current["end_date"] != candidate["end_date"])
        ):
            continue
        result.add(event_id)
    return result


def _event_id(
    connection: sqlite3.Connection, candidate: Mapping[str, Any], content_version_id: str,
    historical_sources: Sequence[Mapping[str, Any]],
) -> str:
    same_content = _same_content_event_ids(
        connection, candidate, content_version_id, historical_sources,
    )
    if len(same_content) == 1:
        return next(iter(same_content))
    if candidate.get("_reschedule_supported") or candidate.get("status") == "cancelled":
        direct = _direct_update_event_ids(candidate, historical_sources)
        if len(direct) == 1:
            return next(iter(direct))
        related = _related_event_ids(
            connection, candidate, content_version_id, historical_sources=historical_sources,
        )
        if len(related) == 1:
            return next(iter(related))
        if len(related) > 1 and candidate.get("status") == "cancelled" and not candidate.get("_reschedule_supported"):
            same_date = _related_event_ids(
                connection, candidate, content_version_id, same_start=True,
                historical_sources=historical_sources,
            )
            if len(same_date) == 1:
                return next(iter(same_date))
        if len(related) > 1:
            return "event-" + _digest(
                _base_event_identity(candidate, content_version_id) | {
                    "ambiguous_update_content": content_version_id,
                    "update_date": candidate.get("start_date"),
                }
            )[:24]
    direct = _historical_direct_event_ids(connection, candidate, historical_sources)
    if len(direct) == 1:
        return next(iter(direct))
    if len(direct) > 1:
        candidate["_identity_ambiguous"] = True
    return "event-" + _digest(_default_event_key(candidate, content_version_id))[:24]


def _single_day_evidence(
    start: str | None, precision: str, end: str | None, evidence: str | None,
    contexts: Sequence[str],
) -> bool:
    if precision != "day" or start is None or end is not None or not evidence:
        return False
    if not _date_matches_evidence(start, "day", evidence):
        return False
    escaped = re.escape(evidence)
    if any(
        _UNRESOLVED_EVENT_END.search(context)
        or re.search(rf"\b(?:from|starts?|begins?)\b[^.;\n]{{0,20}}{escaped}", context, re.I)
        or re.search(rf"{escaped}[^.;\n]{{0,20}}\b(?:through|to|until)\b", context, re.I)
        for context in contexts
    ):
        return False
    if re.search(r"\b(?:from|through|until)\b|\bto\s+\d{1,2}\b|\b(?:and|&)\s+\d{1,2}\b", evidence, re.I):
        return False
    if re.search(r"\b\d{1,2}\s*[-–—]\s*\d{1,2}\b", evidence):
        return False
    if re.search(r"\brescheduled\s+for\b", evidence, re.I):
        return True
    escaped = re.escape(evidence)
    for context in contexts:
        if re.search(
            rf"\b(?:on|held(?:\s+on)?|takes?\s+place(?:\s+on)?|"
            rf"scheduled\s+for|convenes?\s+on)\s+{escaped}",
            context, re.I,
        ):
            return True
        folded_context, folded_evidence = context.casefold(), evidence.casefold()
        offset = 0
        while (position := folded_context.find(folded_evidence, offset)) >= 0:
            if _is_adjacent_evidence_label(context, position):
                return True
            offset = position + max(1, len(evidence))
    return False


def _event_state(candidate: Mapping[str, Any], *, source_count: int, conflict: bool = False) -> dict[str, Any]:
    status = "conflict" if conflict else candidate["status"]
    unknown_end = (
        candidate.get("start_date") is not None
        and candidate.get("end_date") is None
        and not candidate.get("_single_day_supported", False)
    )
    return {
        "name": candidate["name"], "event_type": candidate["event_type"],
        "organizer": candidate.get("organizer"), "status": status,
        "date_precision": candidate["date_precision"],
        "start_date": None if conflict else candidate.get("start_date"),
        "end_date": None if conflict else candidate.get("end_date"),
        "raw_time_text": candidate.get("raw_time_text"), "event_timezone": candidate.get("timezone"),
        "location": candidate.get("location"), "online_url": candidate.get("online_url"),
        "deadline_type": candidate.get("deadline_type"), "deadline_date": candidate.get("deadline_date"),
        "relevance_reason": candidate.get("relevance_reason"),
        "needs_confirmation": int(
            conflict
            or candidate.get("_identity_ambiguous")
            or (candidate["date_precision"] != "day" and not (
                candidate["event_type"] == "deadline" and candidate.get("deadline_date")
            ))
            or unknown_end
            or status in {"tentative", "postponed"}
        ),
        "source_count": source_count,
    }


def _inherit_same_content_interpretation(
    candidate: dict[str, Any], prior_candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    inherited = dict(candidate)
    for prior in reversed(prior_candidates):
        for field in ("organizer", "location", "online_url", "timezone"):
            if inherited.get(field) is None and prior.get(field) is not None:
                inherited[field] = prior[field]
        compatible = all(
            inherited.get(field) == prior.get(field)
            for field in ("status", "date_precision", "start_date")
        )
        if not compatible:
            continue
        for field in ("raw_time_text", "end_date"):
            if inherited.get(field) is None and prior.get(field) is not None:
                inherited[field] = prior[field]
        current_deadline = tuple(inherited.get(field) for field in (
            "deadline_type", "deadline_date", "deadline_evidence",
        ))
        prior_deadline = tuple(prior.get(field) for field in (
            "deadline_type", "deadline_date", "deadline_evidence",
        ))
        if current_deadline == (None, None, None) and all(prior_deadline):
            inherited.update(zip(
                ("deadline_type", "deadline_date", "deadline_evidence"), prior_deadline,
            ))
    return inherited


def _active_event_candidates(connection: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    rows = [dict(row) for row in connection.execute(
        """SELECT s.article_id, s.content_version_id, s.candidate_json,
                  s.interpretation_seq, s.candidate_ordinal, cv.first_fetched_at,
                  b.report_date, b.started_at AS batch_started_at, b.batch_id
           FROM climate_event_sources s
           JOIN article_content_versions cv ON cv.content_version_id=s.content_version_id
           JOIN meeting_runs mr ON mr.meeting_run_id=s.meeting_run_id
           JOIN acquisition_batches b ON b.batch_id=mr.batch_id
           WHERE s.event_id=?
           ORDER BY s.article_id""",
        (event_id,),
    )]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["article_id"], []).append(row)
    active = []
    for article_rows in grouped.values():
        def body_rank(row: Mapping[str, Any]) -> tuple[str, ...]:
            return (
                row["report_date"], row["batch_started_at"], row["first_fetched_at"],
                row["batch_id"], row["content_version_id"],
            )

        selected_rank = max(body_rank(row) for row in article_rows)
        current_rows = [row for row in article_rows if body_rank(row) == selected_rank]
        interpretation = max(row["interpretation_seq"] for row in current_rows)
        for selected in current_rows:
            if selected["interpretation_seq"] != interpretation:
                continue
            candidate = json.loads(selected["candidate_json"])
            prior_candidates = [
                json.loads(row["candidate_json"])
                for row in sorted(current_rows, key=lambda value: value["interpretation_seq"])
                if row["interpretation_seq"] < interpretation
            ]
            candidate = _inherit_same_content_interpretation(candidate, prior_candidates)
            candidate["_source_article_id"] = selected["article_id"]
            candidate["_source_order"] = (
                *selected_rank, selected["interpretation_seq"], selected["candidate_ordinal"],
                selected["article_id"],
            )
            active.append(candidate)
    return active


def _resolved_event_state(candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not candidates:
        raise ValueError("an event must have at least one current source body")
    ordered = sorted(candidates, key=lambda value: value["_source_order"])
    rescheduled = [value for value in ordered if value["status"] == "scheduled"
                   and value.get("_reschedule_supported")]
    cancellation_withdrawals = [
        value for value in rescheduled if value.get("_cancellation_withdrawn")
    ]
    cancelled = [value for value in ordered if value["status"] == "cancelled"]
    postponed = [value for value in ordered if value["status"] == "postponed"]
    status_transitions = sorted(
        [*cancelled, *cancellation_withdrawals], key=lambda value: value["_source_order"],
    )
    if status_transitions:
        authoritative, conflict = status_transitions[-1], False
    elif rescheduled:
        authoritative, conflict = rescheduled[-1], False
    elif postponed:
        authoritative, conflict = postponed[-1], False
    else:
        authoritative = ordered[-1]
        starts = {value["start_date"] for value in ordered if value.get("start_date")}
        ends = {value["end_date"] for value in ordered if value.get("end_date")}
        conflict = len(starts) > 1 or len(ends) > 1
    authoritative = dict(authoritative)
    if not conflict:
        for field in ("organizer", "location", "online_url"):
            if authoritative.get(field) is None:
                authoritative[field] = next(
                    (value[field] for value in reversed(ordered) if value.get(field) is not None),
                    None,
                )
        if authoritative.get("end_date") is None:
            authoritative["end_date"] = next((
                value["end_date"] for value in reversed(ordered)
                if value.get("end_date") is not None
                and all(value.get(field) == authoritative.get(field) for field in (
                    "status", "date_precision", "start_date",
                ))
            ), None)
        if authoritative.get("end_date") is None and authoritative.get("start_date"):
            same_occurrence = [
                value for value in ordered
                if all(value.get(field) == authoritative.get(field) for field in (
                    "status", "date_precision", "start_date", "end_date",
                ))
            ]
            authoritative["_single_day_supported"] = bool(same_occurrence) and all(
                value.get("_single_day_supported", False) for value in same_occurrence
            )
    return _event_state(
        authoritative,
        source_count=len({value["_source_article_id"] for value in ordered}),
        conflict=conflict,
    )


_EVENT_STATE_KEYS = (
    "name", "event_type", "organizer", "status", "date_precision", "start_date", "end_date",
    "raw_time_text", "event_timezone", "location", "online_url", "deadline_type",
    "deadline_date", "relevance_reason", "needs_confirmation", "source_count",
)


def _ambiguous_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_organizer = _normalized_identity_text(left["organizer"])
    right_organizer = _normalized_identity_text(right["organizer"])
    left_location = _normalized_identity_text(left["location"])
    right_location = _normalized_identity_text(right["location"])
    return (
        left["status"] == right["status"] == "scheduled"
        and bool(left.get("online_url"))
        and bool(right.get("online_url"))
        and left["online_url"] != right["online_url"]
        and bool(left.get("start_date"))
        and _normalized_identity_text(left["name"]) == _normalized_identity_text(right["name"])
        and left["event_type"] == right["event_type"]
        and not (left_organizer and right_organizer and left_organizer != right_organizer)
        and left["date_precision"] == right["date_precision"]
        and left["start_date"] == right["start_date"]
        and not (left["end_date"] and right["end_date"] and left["end_date"] != right["end_date"])
        and not (left_location and right_location and left_location != right_location)
    )


def _refresh_event_states(
    connection: sqlite3.Connection, run_id: str, observed_at: str,
) -> None:
    rows = list(connection.execute("SELECT * FROM climate_events ORDER BY event_id"))
    states = {
        row["event_id"]: _resolved_event_state(
            _active_event_candidates(connection, row["event_id"]),
        )
        for row in rows
    }
    articles = {
        event_id: {
            row[0] for row in connection.execute(
                "SELECT DISTINCT article_id FROM climate_event_sources WHERE event_id=?",
                (event_id,),
            )
        }
        for event_id in states
    }
    event_ids = list(states)
    for index, event_id in enumerate(event_ids):
        for peer_id in event_ids[index + 1:]:
            if articles[event_id].isdisjoint(articles[peer_id]) and _ambiguous_identity(
                states[event_id], states[peer_id],
            ):
                states[event_id]["needs_confirmation"] = 1
                states[peer_id]["needs_confirmation"] = 1

    for row in rows:
        event_id = row["event_id"]
        state = states[event_id]
        previous = {key: row[key] for key in _EVENT_STATE_KEYS}
        recorded = connection.execute(
            "SELECT 1 FROM climate_event_versions WHERE event_id=? LIMIT 1", (event_id,),
        ).fetchone()
        if recorded is not None and previous == state:
            continue
        version = 1 if recorded is None else int(row["record_version"]) + 1
        connection.execute(
            """UPDATE climate_events SET record_version=?, name=?, event_type=?, organizer=?, status=?,
               date_precision=?, start_date=?, end_date=?, raw_time_text=?, event_timezone=?, location=?,
               online_url=?, deadline_type=?, deadline_date=?, relevance_reason=?, needs_confirmation=?,
               source_count=?, updated_at=? WHERE event_id=?""",
            (version, *(state[key] for key in _EVENT_STATE_KEYS), observed_at, event_id),
        )
        connection.execute(
            "INSERT INTO climate_event_versions VALUES (?,?,?,?,?,?)",
            (event_id, version, _canonical(state), _digest(state), run_id, observed_at),
        )


def _store_candidate(
    connection: sqlite3.Connection, run_id: str, item: Mapping[str, Any],
    candidate: Mapping[str, Any], observed_at: str, *, candidate_ordinal: int,
    historical_sources: Sequence[Mapping[str, Any]],
) -> str:
    event_id = _event_id(
        connection, candidate, item["content_version_id"], historical_sources,
    )
    source_id = "event-source-" + _digest({
        "event": event_id, "content": item["content_version_id"], "run": run_id,
        "candidate_ordinal": candidate_ordinal,
    })[:24]
    candidate = dict(candidate)
    candidate_json = _canonical(candidate)
    existing = connection.execute("SELECT * FROM climate_events WHERE event_id=?", (event_id,)).fetchone()
    if existing is None:
        initial = _event_state(candidate, source_count=1)
        connection.execute(
            "INSERT INTO climate_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, 1, *(initial[key] for key in _EVENT_STATE_KEYS), observed_at, observed_at),
        )
    interpretation = connection.execute(
        """SELECT interpretation_seq FROM climate_event_sources
           WHERE article_id=? AND content_version_id=? AND meeting_run_id=? LIMIT 1""",
        (item["article_id"], item["content_version_id"], run_id),
    ).fetchone()
    if interpretation is None:
        interpretation_seq = 1 + connection.execute(
            """SELECT coalesce(max(interpretation_seq), 0) FROM climate_event_sources
               WHERE article_id=? AND content_version_id=?""",
            (item["article_id"], item["content_version_id"]),
        ).fetchone()[0]
    else:
        interpretation_seq = int(interpretation[0])
    connection.execute(
        """INSERT INTO climate_event_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (source_id, event_id, item["content_version_id"], item["article_id"], run_id,
         candidate_ordinal, interpretation_seq,
         item["source_url"], item["content_sha256"], candidate_json, _digest(candidate),
         candidate.get("date_evidence"), candidate.get("deadline_evidence"),
         candidate.get("status_evidence"), observed_at),
    )
    return event_id


Extractor = Callable[[Mapping[str, Any]], Any]


def _meeting_lock_path(database: str | Path, batch_id: str) -> Path:
    path = Path(database).resolve()
    suffix = hashlib.sha256(f"{path}|{batch_id}".encode()).hexdigest()[:20]
    return path.parent / f".{path.name}.meeting-{suffix}.lock"


@contextmanager
def _meeting_batch_lock(database: str | Path, batch_id: str, *, blocking: bool):
    path = _meeting_lock_path(database, batch_id)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(
                    descriptor, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1,
                )
                acquired = True
            except OSError:
                if blocking:
                    raise
        else:
            import fcntl

            try:
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB),
                )
                acquired = True
            except BlockingIOError:
                if blocking:
                    raise
        yield acquired
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def active_meeting_run(database: str | Path, batch_id: str) -> dict[str, Any] | None:
    """Return a running row only while another worker still owns the batch lock."""
    with _meeting_batch_lock(database, batch_id, blocking=False) as acquired:
        if acquired:
            return None
        connection = _open(database)
        try:
            row = connection.execute(
                """SELECT * FROM meeting_runs WHERE batch_id=? AND status='running'
                   ORDER BY started_at DESC, attempt DESC LIMIT 1""",
                (batch_id,),
            ).fetchone()
            return dict(row) if row is not None else {"meeting_run_id": None}
        finally:
            connection.close()


def process_batch(
    database: str | Path,
    batch_id: str,
    *,
    prompt_text: str,
    prompt_version: str,
    provider: str,
    model: str,
    extractor: Extractor,
    retry_failed: bool = False,
    retry_meeting_run_id: str | None = None,
    task_version: int = 1,
    now: datetime | None = None,
) -> dict[str, Any]:
    if (provider, model) != ("", "") and not all(
        isinstance(value, str) and value.strip() for value in (provider, model)
    ):
        raise ValueError("provider and model must both be empty or non-empty strings")
    with _meeting_batch_lock(database, batch_id, blocking=False) as acquired:
        if not acquired:
            connection = _open(database)
            try:
                running = connection.execute(
                    """SELECT meeting_run_id FROM meeting_runs
                       WHERE batch_id=? AND status='running'
                       ORDER BY started_at DESC, attempt DESC LIMIT 1""",
                    (batch_id,),
                ).fetchone()
                if running is not None:
                    return _run_result(connection, running["meeting_run_id"], reused=True)
            finally:
                connection.close()
            raise RuntimeError("meeting batch is already being processed")
        return _process_batch(
            database, batch_id, prompt_text=prompt_text, prompt_version=prompt_version,
            provider=provider, model=model, extractor=extractor, retry_failed=retry_failed,
            retry_meeting_run_id=retry_meeting_run_id, task_version=task_version, now=now,
        )


def _process_batch(
    database: str | Path,
    batch_id: str,
    *,
    prompt_text: str,
    prompt_version: str,
    provider: str,
    model: str,
    extractor: Extractor,
    retry_failed: bool = False,
    retry_meeting_run_id: str | None = None,
    task_version: int = 1,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Extract and persist meetings for every saved body in one acquisition batch."""
    prompt_text = prompt_text.replace("\r\n", "\n").replace("\r", "\n")
    # Empty identity fields persist the absence of a task override (Hermes defaults).
    if not prompt_text.strip() or not prompt_version.strip():
        raise ValueError("prompt version/text are required")
    if type(task_version) is not int or task_version < 1:
        raise ValueError("task_version must be a positive integer")
    connection = _open(database, write=True)
    stamp = _now(now)
    try:
        batch = connection.execute("SELECT batch_id FROM acquisition_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if batch is None:
            raise KeyError(f"unknown acquisition batch: {batch_id}")
        prior = None
        recovering = False
        if retry_failed:
            if retry_meeting_run_id:
                prior = connection.execute(
                    "SELECT * FROM meeting_runs WHERE meeting_run_id=?", (retry_meeting_run_id,),
                ).fetchone()
            else:
                prior = connection.execute(
                    """SELECT * FROM meeting_runs WHERE batch_id=? AND status IN ('partial', 'failed')
                       ORDER BY started_at DESC, attempt DESC LIMIT 1""",
                    (batch_id,),
                ).fetchone()
            if prior is None or prior["batch_id"] != batch_id:
                raise ValueError("meeting retry requires a partial or failed run for the target batch")
            latest = connection.execute(
                "SELECT * FROM meeting_runs WHERE processing_key=? ORDER BY attempt DESC LIMIT 1",
                (prior["processing_key"],),
            ).fetchone()
            assert latest is not None
            if latest["status"] in {"succeeded", "no_content", "running"}:
                return _run_result(connection, latest["meeting_run_id"], reused=True)
            prior = latest
            prompt_text = prior["prompt_text"]
            prompt_version = prior["prompt_version"]
            provider = prior["provider"]
            model = prior["model"]
            task_version = int(prior["task_version"])
            if hashlib.sha256(prompt_text.encode("utf-8")).hexdigest() != prior["prompt_sha256"]:
                raise ValueError("stored meeting retry prompt hash mismatch")
            items = [dict(row) for row in connection.execute(
                """SELECT ri.acquisition_item_id, ri.article_id, ri.content_version_id,
                          ri.source_url, ri.content_sha256, ri.status AS prior_status,
                          cv.markdown_content
                   FROM meeting_run_items ri
                   LEFT JOIN article_content_versions cv
                     ON cv.content_version_id=ri.content_version_id AND cv.article_id=ri.article_id
                   LEFT JOIN acquisition_items ai ON ai.acquisition_item_id=ri.acquisition_item_id
                   WHERE ri.meeting_run_id=?
                   ORDER BY ai.ordinal""",
                (prior["meeting_run_id"],),
            )]
        else:
            prior = connection.execute(
                """SELECT * FROM meeting_runs WHERE batch_id=? AND status='running'
                   ORDER BY started_at DESC, attempt DESC LIMIT 1""",
                (batch_id,),
            ).fetchone()
            recovering = prior is not None
            if recovering:
                prompt_text = prior["prompt_text"]
                prompt_version = prior["prompt_version"]
                provider = prior["provider"]
                model = prior["model"]
                task_version = int(prior["task_version"])
                if hashlib.sha256(prompt_text.encode("utf-8")).hexdigest() != prior["prompt_sha256"]:
                    raise ValueError("stored meeting prompt hash mismatch")
                items = [dict(row) for row in connection.execute(
                    """SELECT ri.acquisition_item_id, ri.article_id, ri.content_version_id,
                              ri.source_url, ri.content_sha256, ri.status AS prior_status,
                              cv.markdown_content
                       FROM meeting_run_items ri
                       LEFT JOIN article_content_versions cv
                         ON cv.content_version_id=ri.content_version_id AND cv.article_id=ri.article_id
                       LEFT JOIN acquisition_items ai ON ai.acquisition_item_id=ri.acquisition_item_id
                       WHERE ri.meeting_run_id=?
                       ORDER BY ai.ordinal""",
                    (prior["meeting_run_id"],),
                )]
            else:
                items = [dict(row) for row in connection.execute(
                    """SELECT ai.acquisition_item_id, ai.article_id, ai.content_version_id,
                              a.canonical_url AS source_url, cv.content_sha256, cv.markdown_content,
                              NULL AS prior_status
                       FROM acquisition_items ai
                       JOIN articles a ON a.article_id=ai.article_id
                       LEFT JOIN article_content_versions cv
                         ON cv.content_version_id=ai.content_version_id AND cv.article_id=ai.article_id
                       WHERE ai.batch_id=?
                       ORDER BY ai.ordinal""",
                    (batch_id,),
                )]
        # Retry/recovery can replace the caller's identity with a persisted pair.
        if (provider, model) != ("", "") and not all(
            isinstance(value, str) and value.strip() for value in (provider, model)
        ):
            raise ValueError("provider and model must both be empty or non-empty strings")
        inputs = [{key: item[key] for key in (
            "acquisition_item_id", "article_id", "content_version_id", "source_url", "content_sha256"
        )} for item in items]
        available = [item for item in items if item["content_version_id"] and (item["markdown_content"] or "").strip()]
        input_sha = _digest(inputs)
        prompt_sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
        processing_key = _digest({
            "batch_id": batch_id, "input_sha256": input_sha, "prompt_sha256": prompt_sha,
            "provider": provider, "model": model,
        })
        if retry_failed:
            processing_key = prior["processing_key"]
            input_sha = prior["input_sha256"]
            prompt_sha = prior["prompt_sha256"]
        elif recovering:
            processing_key = prior["processing_key"]
            input_sha = prior["input_sha256"]
            prompt_sha = prior["prompt_sha256"]
        else:
            prior = connection.execute(
                "SELECT * FROM meeting_runs WHERE processing_key=? ORDER BY attempt DESC LIMIT 1",
                (processing_key,),
            ).fetchone()
            if prior is not None:
                return _run_result(connection, prior["meeting_run_id"], reused=True)
        attempt = int(prior["attempt"]) if recovering else 1 if prior is None else int(prior["attempt"]) + 1
        run_id = prior["meeting_run_id"] if recovering else f"meeting-{processing_key[:20]}-{attempt}"
        if not recovering:
            connection.execute(
                """INSERT INTO meeting_runs(
                   meeting_run_id, processing_key, batch_id, attempt, status, prompt_version,
                   prompt_sha256, prompt_text, provider, model, task_version,
                   retry_of_meeting_run_id, input_sha256, started_at, completed_at, item_count,
                   succeeded_count, failed_count, unavailable_count, candidate_count, error_message)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, processing_key, batch_id, attempt, "running", prompt_version, prompt_sha,
                 prompt_text, provider, model, task_version,
                 prior["meeting_run_id"] if retry_failed else None,
                 input_sha, stamp, None, len(items), 0, 0, 0, 0, None),
            )
        prior_success: dict[str, sqlite3.Row] = {}
        if prior is not None:
            prior_success = {row["acquisition_item_id"]: row for row in connection.execute(
                "SELECT * FROM meeting_run_items WHERE meeting_run_id=? AND status='succeeded'",
                (prior["meeting_run_id"],),
            )}
        if not recovering:
            for item in items:
                previous = prior_success.get(item["acquisition_item_id"])
                usable = item["content_version_id"] and (item["markdown_content"] or "").strip()
                status = "succeeded" if previous else "pending" if usable else "unavailable"
                count = int(previous["candidate_count"]) if previous else 0
                connection.execute(
                    "INSERT INTO meeting_run_items VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (run_id, item["acquisition_item_id"], item["content_version_id"],
                     item["article_id"], item["source_url"], item["content_sha256"], status, count,
                     None if usable else "no persisted article body", stamp if previous or not usable else None),
                )
            connection.commit()
        if prior is None and not available:
            connection.execute(
                """UPDATE meeting_runs SET status='no_content', completed_at=?, unavailable_count=?
                   WHERE meeting_run_id=?""",
                (stamp, len(items), run_id),
            )
            connection.commit()
            return _run_result(connection, run_id)
        for item in items:
            if item["acquisition_item_id"] in prior_success or item not in available:
                continue
            try:
                request = {
                    "schema_version": "climate-meeting-extraction-input.v1",
                    "content_version_id": item["content_version_id"],
                    "content_sha256": item["content_sha256"],
                    "source_url": item["source_url"],
                    "article_body": item["markdown_content"],
                    "prompt": prompt_text,
                }
                candidates = validate_extraction(extractor(request), body=item["markdown_content"])
                with connection:
                    historical_sources = [dict(row) for row in connection.execute(
                        """SELECT event_id, content_version_id, candidate_json, interpretation_seq
                           FROM climate_event_sources"""
                    )]
                    for ordinal, candidate in enumerate(candidates, 1):
                        _store_candidate(
                            connection, run_id, item, candidate, _now(),
                            candidate_ordinal=ordinal, historical_sources=historical_sources,
                        )
                    _refresh_event_states(connection, run_id, _now())
                    connection.execute(
                        """UPDATE meeting_run_items SET status='succeeded', candidate_count=?,
                           error_message=NULL, processed_at=? WHERE meeting_run_id=? AND acquisition_item_id=?""",
                        (len(candidates), _now(), run_id, item["acquisition_item_id"]),
                    )
            except Exception as exc:
                with connection:
                    connection.execute(
                        """UPDATE meeting_run_items SET status='failed', error_message=?, processed_at=?
                           WHERE meeting_run_id=? AND acquisition_item_id=?""",
                        (f"{type(exc).__name__}: {str(exc)[:800]}", _now(), run_id, item["acquisition_item_id"]),
                    )
        counts = connection.execute(
            """SELECT count(*) AS total,
               sum(status='succeeded') AS succeeded, sum(status='failed') AS failed,
               sum(status='unavailable') AS unavailable,
               sum(candidate_count) AS candidates FROM meeting_run_items WHERE meeting_run_id=?""",
            (run_id,),
        ).fetchone()
        succeeded, failed = int(counts["succeeded"] or 0), int(counts["failed"] or 0)
        unavailable = int(counts["unavailable"] or 0)
        status = "succeeded" if not failed and not unavailable else "partial" if succeeded else "failed"
        error = None if not failed and not unavailable else (
            f"{failed} failed and {unavailable} unavailable of {counts['total']} acquisition items"
        )
        with connection:
            connection.execute(
                """UPDATE meeting_runs SET status=?, completed_at=?, succeeded_count=?, failed_count=?,
                   unavailable_count=?, candidate_count=?, error_message=? WHERE meeting_run_id=?""",
                (status, _now(), succeeded, failed, unavailable,
                 int(counts["candidates"] or 0), error, run_id),
            )
        return _run_result(connection, run_id)
    finally:
        connection.close()


def _run_result(connection: sqlite3.Connection, run_id: str, *, reused: bool = False) -> dict[str, Any]:
    run = connection.execute("SELECT * FROM meeting_runs WHERE meeting_run_id=?", (run_id,)).fetchone()
    if run is None:
        raise KeyError(f"unknown meeting run: {run_id}")
    return {**_public_run(connection, run), "reused": reused}


def _run_lineage_ids(connection: sqlite3.Connection, run: sqlite3.Row) -> list[str]:
    result = []
    current = run
    while current is not None and current["meeting_run_id"] not in result:
        result.append(current["meeting_run_id"])
        parent = current["retry_of_meeting_run_id"]
        current = None if parent is None else connection.execute(
            "SELECT * FROM meeting_runs WHERE meeting_run_id=?", (parent,),
        ).fetchone()
    return result


def _run_confirmation_events(connection: sqlite3.Connection, run: sqlite3.Row) -> list[dict[str, Any]]:
    lineage = _run_lineage_ids(connection, run)
    placeholders = ",".join("?" for _ in lineage)
    return [dict(row) for row in connection.execute(
        f"""SELECT event_id, name, record_version FROM climate_events
            WHERE needs_confirmation=1 AND event_id IN (
                SELECT event_id FROM climate_event_sources WHERE meeting_run_id IN ({placeholders})
                UNION
                SELECT event_id FROM climate_event_versions WHERE meeting_run_id IN ({placeholders})
            ) ORDER BY event_id""",
        (*lineage, *lineage),
    )]


def _public_run(connection: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
    value = {key: run[key] for key in run.keys() if key != "prompt_text"}
    value["items"] = [{
        "acquisition_item_id": row["acquisition_item_id"],
        "article_id": row["article_id"],
        "content_version_id": row["content_version_id"],
        "source_url": row["source_url"],
        "status": row["status"],
        "candidate_count": row["candidate_count"],
        "error": row["error_message"],
        "processed_at": row["processed_at"],
    } for row in connection.execute(
        "SELECT * FROM meeting_run_items WHERE meeting_run_id=? ORDER BY acquisition_item_id",
        (run["meeting_run_id"],),
    )]
    value["current_needs_confirmation_events"] = _run_confirmation_events(connection, run)
    value["current_needs_confirmation_count"] = len(value["current_needs_confirmation_events"])
    return value


def meeting_status(database: str | Path, *, batch_id: str | None = None) -> dict[str, Any]:
    connection = _open(database)
    try:
        where, parameters = ("WHERE batch_id=?", (batch_id,)) if batch_id else ("", ())
        raw_rows = list(connection.execute(
            f"SELECT * FROM meeting_runs {where} ORDER BY started_at DESC, attempt DESC", parameters
        ))
        rows = [_public_run(connection, row) for row in raw_rows]
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest.setdefault(row["batch_id"], row)
        statuses = [row["status"] for row in latest.values()]
        aggregate = (
            "not_processed" if not statuses else
            "partial" if any(value in {"partial", "failed", "running"} for value in statuses) else
            "complete"
        )
        return {"status": aggregate, "batches": list(latest.values()), "runs": rows}
    finally:
        connection.close()


def meeting_retry_run(database: str | Path, *, batch_id: str) -> dict[str, Any] | None:
    """Return the latest retryable run including its frozen prompt for an internal worker binding."""
    connection = _open(database)
    try:
        row = connection.execute(
            """SELECT * FROM meeting_runs WHERE batch_id=? AND status IN ('partial', 'failed')
               ORDER BY started_at DESC, attempt DESC LIMIT 1""",
            (batch_id,),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def _meeting_coverage(
    database: str | Path, *, meeting_enabled: bool | None, target_batch_id: str | None,
    task_version: int | None, returned_record_count: int,
) -> dict[str, Any]:
    if meeting_enabled is None:
        return meeting_status(database)
    status = meeting_status(database, batch_id=target_batch_id) if target_batch_id else {
        "status": "not_processed", "batches": [], "runs": [],
    }
    latest = status["batches"][0] if status["batches"] else None
    if not meeting_enabled:
        coverage_status = "disabled"
    elif latest is None:
        coverage_status = "enabled_unprocessed"
    elif latest["status"] == "succeeded" and latest["candidate_count"] == 0:
        coverage_status = "succeeded_empty"
    else:
        coverage_status = latest["status"]
    return {
        "status": coverage_status,
        "meeting_enabled": meeting_enabled,
        "task_version": task_version,
        "target_batch_id": target_batch_id,
        "target_meeting_run_id": latest["meeting_run_id"] if latest else None,
        "target_processed": latest is not None and latest["status"] not in {"running"},
        "returned_record_count": returned_record_count,
        "records_scope": "registry_current_records",
        "uses_existing_records": returned_record_count > 0,
    }


def _date_bounds(value: str, precision: str, *, end: bool) -> date:
    if precision == "day":
        return date.fromisoformat(value)
    if precision == "month":
        year, month = map(int, value.split("-"))
        return date(year, month, monthrange(year, month)[1] if end else 1)
    if precision == "quarter":
        year, quarter = int(value[:4]), int(value[-1])
        month = quarter * 3 if end else (quarter - 1) * 3 + 1
        return date(year, month, monthrange(year, month)[1] if end else 1)
    if precision == "year":
        return date(int(value), 12 if end else 1, 31 if end else 1)
    raise ValueError("unknown dates do not have calendar bounds")


def _explicit_single_day(event: Mapping[str, Any], sources: Sequence[Mapping[str, Any]]) -> bool:
    if event["date_precision"] != "day" or event["start_date"] is None or event["end_date"] is not None:
        return False
    decisions = []
    for source in sources:
        if not source.get("is_current"):
            continue
        candidate = json.loads(source["candidate_json"])
        if candidate.get("start_date") != event["start_date"] or candidate.get("end_date") is not None:
            continue
        decision = candidate.get("_single_day_supported")
        if decision is None:
            evidence = candidate.get("date_evidence")
            contexts = _bound_evidence_contexts(
                source["markdown_content"], evidence, candidate["name"],
            ) if evidence else []
            decision = _single_day_evidence(
                candidate.get("start_date"), candidate["date_precision"],
                candidate.get("end_date"), evidence, contexts,
            )
        decisions.append(bool(decision))
    return bool(decisions) and all(decisions)


def query_events(
    database: str | Path,
    *,
    organizer: str | None = None,
    event_types: Sequence[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_unknown: bool = False,
    include_deadlines: bool = False,
    include_cancelled: bool = False,
    include_retrospective: bool = False,
    base_date: str | None = None,
    timezone_name: str = "America/New_York",
    meeting_enabled: bool | None = None,
    target_batch_id: str | None = None,
    task_version: int | None = None,
) -> dict[str, Any]:
    """Query event intervals and, when requested, deadline dates with inclusive overlap."""
    try:
        zone = timezone.utc if timezone_name == "UTC" else ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError("timezone_name must be a valid IANA timezone") from exc
    base = date.fromisoformat(base_date) if base_date else datetime.now(zone).date()
    lower = date.fromisoformat(start_date) if start_date else base
    upper = date.fromisoformat(end_date) if end_date else None
    if upper and lower > upper:
        raise ValueError("start_date must not be after end_date")
    requested_types = set(event_types or EVENT_TYPES)
    if not requested_types <= EVENT_TYPES:
        raise ValueError("event_types contains an unsupported value")
    connection = _open(database)
    try:
        result = []
        for row in connection.execute("SELECT * FROM climate_events"):
            event = dict(row)
            if event["event_type"] not in requested_types:
                continue
            if event["event_type"] == "deadline" and not include_deadlines:
                continue
            if event["status"] == "cancelled" and not include_cancelled:
                continue
            if event["event_type"] == "retrospective" and not include_retrospective:
                continue
            if organizer and organizer.casefold() not in (event["organizer"] or "").casefold():
                continue
            source_rows = [dict(source) for source in connection.execute(
                """SELECT s.event_source_id, s.content_version_id, s.article_id, s.source_url,
                          s.content_sha256, s.candidate_json, s.date_evidence, s.deadline_evidence,
                          s.status_evidence, s.observed_at, s.interpretation_seq,
                          s.candidate_ordinal, cv.first_fetched_at, b.report_date,
                          cv.markdown_content, b.started_at AS batch_started_at, b.batch_id
                   FROM climate_event_sources s
                   JOIN article_content_versions cv ON cv.content_version_id=s.content_version_id
                   JOIN meeting_runs mr ON mr.meeting_run_id=s.meeting_run_id
                   JOIN acquisition_batches b ON b.batch_id=mr.batch_id
                   WHERE s.event_id=? ORDER BY s.source_url, s.content_version_id""",
                (event["event_id"],),
            )]
            for source in source_rows:
                same_article = [row for row in source_rows if row["article_id"] == source["article_id"]]
                rank = lambda row: (
                    row["report_date"], row["batch_started_at"], row["first_fetched_at"],
                    row["batch_id"], row["content_version_id"],
                )
                current_rank = max(rank(row) for row in same_article)
                current_interpretation = max(
                    row["interpretation_seq"] for row in same_article if rank(row) == current_rank
                )
                source["is_current"] = int(
                    rank(source) == current_rank
                    and source["interpretation_seq"] == current_interpretation
                )
            event_intervals = []
            if event["start_date"] is not None and event["end_date"] is not None:
                event_intervals.append((
                    _date_bounds(event["start_date"], event["date_precision"], end=False),
                    _date_bounds(event["end_date"], event["date_precision"], end=True),
                ))
            elif _explicit_single_day(event, source_rows):
                event_day = date.fromisoformat(event["start_date"])
                event_intervals.append((event_day, event_day))
            deadline_intervals = []
            if include_deadlines and event["deadline_date"]:
                deadline_day = date.fromisoformat(event["deadline_date"])
                deadline_intervals.append((deadline_day, deadline_day))
            event_matches = any(
                interval_end >= lower and (upper is None or interval_start <= upper)
                for interval_start, interval_end in event_intervals
            )
            deadline_matches = any(
                interval_end >= lower and (upper is None or interval_start <= upper)
                for interval_start, interval_end in deadline_intervals
            )
            unknown_event_interval = not event_intervals and event["event_type"] != "deadline"
            if not (
                event_matches or deadline_matches
                or (include_unknown and unknown_event_interval)
            ):
                continue
            event["sources"] = [
                {key: value for key, value in source.items() if key not in {
                    "candidate_json", "markdown_content", "first_fetched_at", "report_date",
                    "batch_started_at", "batch_id",
                }}
                for source in source_rows
            ]
            result.append(event)
        result.sort(key=lambda item: (
            (item["start_date"] or item["deadline_date"]) is None,
            item["start_date"] or item["deadline_date"] or "9999",
            item["name"].casefold(), item["event_id"]
        ))
        filters = {
            "organizer": organizer, "event_types": sorted(requested_types),
            "start_date": start_date, "end_date": end_date, "include_unknown": include_unknown,
            "include_deadlines": include_deadlines, "include_cancelled": include_cancelled,
            "include_retrospective": include_retrospective,
        }
        return {
            "schema_version": "climate-meeting-query.v1", "base_date": base.isoformat(),
            "timezone": timezone_name, "filters": filters, "records": result,
            "coverage": _meeting_coverage(
                database, meeting_enabled=meeting_enabled, target_batch_id=target_batch_id,
                task_version=task_version, returned_record_count=len(result),
            ),
        }
    finally:
        connection.close()


def freeze_snapshot(database: str | Path, **query: Any) -> dict[str, Any]:
    """Persist one canonical immutable query result for downstream PDF rendering."""
    queried = query_events(database, **query)
    payload = {
        "schema_version": "climate-meeting-snapshot.v1",
        "query": queried["filters"], "base_date": queried["base_date"],
        "timezone": queried["timezone"], "records": queried["records"],
        "coverage": queried["coverage"],
    }
    digest = _digest(payload)
    snapshot_id = "meeting-snapshot-" + digest[:24]
    connection = _open(database, write=True)
    try:
        created_at = _now()
        with connection:
            connection.execute(
                "INSERT OR IGNORE INTO meeting_snapshots VALUES (?,?,?,?,?,?,?,?)",
                (snapshot_id, created_at, _canonical(payload["query"]), payload["base_date"],
                 payload["timezone"], _canonical(payload["records"]),
                 _canonical(payload["coverage"]), digest),
            )
        row = connection.execute("SELECT * FROM meeting_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        return {**payload, "snapshot_id": snapshot_id, "snapshot_sha256": digest, "created_at": row["created_at"]}
    finally:
        connection.close()


def load_snapshot(database: str | Path, snapshot_id: str) -> dict[str, Any]:
    connection = _open(database)
    try:
        row = connection.execute("SELECT * FROM meeting_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown meeting snapshot: {snapshot_id}")
        return {
            "schema_version": "climate-meeting-snapshot.v1", "snapshot_id": row["snapshot_id"],
            "snapshot_sha256": row["snapshot_sha256"], "created_at": row["created_at"],
            "query": json.loads(row["query_json"]), "base_date": row["base_date"],
            "timezone": row["timezone"], "records": json.loads(row["records_json"]),
            "coverage": json.loads(row["coverage_json"]),
        }
    finally:
        connection.close()
