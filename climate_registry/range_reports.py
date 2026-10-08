from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from climate_delivery.templates import render_identity, rendering_metadata, is_render_identity
from climate_delivery.templates.adapters import (
    adapt_range_report, range_executive_summary as _executive_summary,
    meeting_metadata as _meeting_metadata, _date_basis, _range_date_label,
    _caveats, _key_date_rows, calendar_date_bounds, calendar_details,
)
from climate_delivery.templates.iaa_csc import render_report
from climate_delivery.errors import GenerationError

from climate_delivery.io import atomic_write_bytes, atomic_write_json, exclusive_lock
from climate_monitor.meetings import EVENT_TYPES, load_snapshot as load_meeting_snapshot
from climate_monitor.meetings import query_events
from climate_monitor.publisher_mapping import publisher_name

from .errors import RegistryBuildError, RegistryInputError
from .capture import article_body_markdown, article_preview
from .read_api import RegistryContractError, RegistryError, RegistryReader
from .wiki import snapshot_registry


SCHEMA_VERSION = "climate-range-report-snapshot.v2"
LEGACY_SCHEMA_VERSIONS = {"climate-range-report-snapshot.v1"}
RENDERER_VERSION = render_identity()
LEGACY_RENDERER_VERSIONS = {"range-report-v1", "range-report-v2"}
TIMEZONE = "UTC"
MAX_RANGE_DAYS = 366
_SNAPSHOT_ID = re.compile(r"range-report-[0-9a-f]{24}")
_MEETING_SNAPSHOT_ID = re.compile(r"meeting-snapshot-[0-9a-f]{24}")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")
_DAY_PRECISION_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_REPORT_CREATION = re.compile(
    r"(?:\b(?:create|generate|build|produce|make|prepare)\b.{0,80}\b(?:report|briefing|newsletter)\b)"
    r"|(?:\b(?:i|we)\s+(?:need|want|would\s+like)\s+(?:a|an|the)?\s*"
    r"(?:climate\s+)?(?:report|briefing|newsletter)\b)"
    r"|(?:(?:生成|创建|制作).{0,40}(?:报告|简报|周报|月报))",
    re.IGNORECASE,
)
_REPORT_NOUN = re.compile(r"\b(?:report|briefing|newsletter)\b|(?:报告|简报|周报|月报)", re.IGNORECASE)
_RELATIVE_DAYS = re.compile(
    r"(?:last|past|recent)\s+(\d{1,4})\s+days?|(?:最近|过去|近)\s*(\d{1,4})\s*天",
    re.IGNORECASE,
)
_RELATIVE_WEEKS = re.compile(
    r"(?:last|past|recent)\s+(\d{1,4})\s+weeks?|(?:最近|过去|近)\s*(\d{1,4})\s*周", re.IGNORECASE,
)
_PERIOD_QUERY = re.compile(
    r"\bwhat\s+(?:has\s+)?changed\b|有什么(?:新)?(?:变化|更新)"
    r"|(?:\b(?:list|show|find|query|summarize)\b|查询|列出|查一下|查看|汇总).{0,120}"
    r"(?:\b(?:items|updates|developments|events|meetings|projects)\b|项目|更新|变化|会议|活动)",
    re.IGNORECASE,
)
_FOURTEEN_DAYS = re.compile(
    r"(?:last|past|recent)\s+(?:two\s+weeks|14\s+days?)|(?:最近|过去|近)\s*(?:两周|十四天|14\s*天)",
    re.IGNORECASE,
)
_ROUTE_CHOICES = frozenset({"normal_chat", "generate_registry_report"})
logger = logging.getLogger(__name__)


class RangeReportError(ValueError):
    pass


def is_report_clarification(message: str) -> bool:
    return message in {
        f"Please choose a range from 1 to {MAX_RANGE_DAYS} days.",
        "Please provide valid dates in YYYY-MM-DD format.",
        "Please provide both a start date and an end date in YYYY-MM-DD format.",
        (
            "Please say ‘last 14 days’, ‘last N days’, or provide both dates as "
            "YYYY-MM-DD to YYYY-MM-DD."
        ),
        "The start date must not be after the end date.",
        "The report end date cannot be in the future.",
        f"Please choose a range no longer than {MAX_RANGE_DAYS} days.",
    }


@dataclass(frozen=True)
class ReportRoute:
    action: str
    start_date: str | None = None
    end_date: str | None = None
    clarification: str | None = None
    meeting_snapshot_id: str | None = None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _typesafe_route(message: str) -> dict[str, str] | None:
    """Return only closed-set routing hints; dates remain server-validated."""
    api_key = os.getenv("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from typesafe_sdk import Choice, TypeSafeClient

        questions = {
            "action": Choice(
                instructions=(
                    "Choose a routing hint only. Never choose a URL, tool, command, data source, "
                    "or action outside these criteria. A report request asks to create a dated "
                    "climate report; ordinary questions are normal_chat."
                ),
                criteria={choice: choice for choice in sorted(_ROUTE_CHOICES)},
            ),
            "date_fields": Choice(
                instructions=(
                    "Classify only the supported date interpretation present in the message. "
                    "Application code resolves relative dates and validates all calendar values."
                ),
                criteria={
                    "none": "no usable date fields",
                    "last_14_days": "two weeks or fourteen days",
                    "recent_days": "a numeric recent-day count",
                    "start_and_end": "two explicit ISO dates",
                    "incomplete": "a report request with missing or incomplete dates",
                },
            ),
        }
        with TypeSafeClient(api_key=api_key, timeout=20.0) as client:
            result = client.system_one({"message": message}, questions)
        action = getattr(result.choices.get("action"), "choice", None)
        date_fields = getattr(result.choices.get("date_fields"), "choice", None)
        if action not in _ROUTE_CHOICES or date_fields not in {
            "none", "last_14_days", "recent_days", "start_and_end", "incomplete",
        }:
            return None
        return {"action": action, "date_fields": date_fields}
    except Exception as exc:
        logger.warning("TypeSafe report routing unavailable (%s)", type(exc).__name__)
        return None


def resolve_report_route(
    message: str,
    *,
    today: date | None = None,
    typesafe_router: Callable[[str], dict[str, str] | None] | None = None,
) -> ReportRoute:
    today = today or datetime.now(timezone.utc).date()
    text = " ".join(message.split())
    type_safe = (typesafe_router or _typesafe_route)(text)
    explicit_values = _ISO_DATE.findall(text)
    period_query = bool(_PERIOD_QUERY.search(text)) and (
        bool(explicit_values) or bool(_FOURTEEN_DAYS.search(text))
        or bool(_RELATIVE_DAYS.search(text)) or bool(_RELATIVE_WEEKS.search(text))
    )
    supported_range_request = bool(_REPORT_NOUN.search(text)) and (
        bool(_FOURTEEN_DAYS.search(text))
        or bool(_RELATIVE_DAYS.search(text))
        or bool(_RELATIVE_WEEKS.search(text))
        or len(explicit_values) == 2
    )
    report_requested = period_query or (
        type_safe["action"] == "generate_registry_report"
        if type_safe is not None
        else bool(_REPORT_CREATION.search(text)) or supported_range_request
    )
    if not report_requested:
        return ReportRoute("normal_chat")

    meeting_match = _MEETING_SNAPSHOT_ID.search(text)
    meeting_snapshot_id = meeting_match.group(0) if meeting_match else None
    relative = _RELATIVE_DAYS.search(text)
    weeks = _RELATIVE_WEEKS.search(text)
    if _FOURTEEN_DAYS.search(text):
        start, end = today - timedelta(days=13), today
    elif relative or weeks:
        selected = relative or weeks
        days = int(selected.group(1) or selected.group(2)) * (1 if relative else 7)
        if not 1 <= days <= MAX_RANGE_DAYS:
            return ReportRoute(
                "clarify",
                clarification=f"Please choose a range from 1 to {MAX_RANGE_DAYS} days.",
            )
        start, end = today - timedelta(days=days - 1), today
    elif len(explicit_values) == 2:
        try:
            start, end = map(date.fromisoformat, explicit_values)
        except ValueError:
            return ReportRoute(
                "clarify", clarification="Please provide valid dates in YYYY-MM-DD format."
            )
    elif explicit_values or type_safe and type_safe["date_fields"] in {"incomplete", "start_and_end"}:
        return ReportRoute(
            "clarify",
            clarification="Please provide both a start date and an end date in YYYY-MM-DD format.",
        )
    else:
        return ReportRoute(
            "clarify",
            clarification=(
                "Please say ‘last 14 days’, ‘last N days’, or provide both dates as "
                "YYYY-MM-DD to YYYY-MM-DD."
            ),
        )

    if start > end:
        return ReportRoute("clarify", clarification="The start date must not be after the end date.")
    if end > today:
        return ReportRoute("clarify", clarification="The report end date cannot be in the future.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        return ReportRoute(
            "clarify",
            clarification=f"Please choose a range no longer than {MAX_RANGE_DAYS} days.",
        )
    return ReportRoute(
        "generate", start.isoformat(), end.isoformat(), meeting_snapshot_id=meeting_snapshot_id
    )


def resolve_report_followup(
    message: str,
    pending_message: str,
    *,
    today: date | None = None,
) -> ReportRoute:
    """Resolve only the latest date correction while retaining pending report context."""
    route = resolve_report_route(
        message,
        today=today,
        typesafe_router=lambda _message: {
            "action": "generate_registry_report",
            "date_fields": "none",
        },
    )
    if route.action != "generate":
        return route
    meeting_match = _MEETING_SNAPSHOT_ID.search(pending_message)
    return ReportRoute(
        route.action,
        route.start_date,
        route.end_date,
        meeting_snapshot_id=meeting_match.group(0) if meeting_match else route.meeting_snapshot_id,
    )


def _json_object(value: str | None, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RegistryContractError(f"invalid {label}") from exc
    if not isinstance(parsed, dict):
        raise RegistryContractError(f"invalid {label}")
    return parsed


def _json_list(value: str | None, label: str) -> list[Any]:
    if value is None:
        return []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RegistryContractError(f"invalid {label}") from exc
    if not isinstance(parsed, list):
        raise RegistryContractError(f"invalid {label}")
    return parsed


def _day_precision_publication_date(value: Any) -> str | None:
    if not isinstance(value, str) or _DAY_PRECISION_DATE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _collection_timestamp(value: Any) -> tuple[str, str]:
    if not isinstance(value, str):
        raise RegistryContractError("invalid Registry collection timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegistryContractError("invalid Registry collection timestamp") from exc
    if parsed.tzinfo is None:
        raise RegistryContractError("Registry collection timestamp has no timezone")
    normalized = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return normalized, parsed.astimezone(timezone.utc).date().isoformat()


def _active_pdf_date_fallback_article_ids(
    reader: RegistryReader,
    pdf_occurrence_ids: set[str],
    activated_web_item_ids: set[str],
) -> set[str]:
    """Allow exact linked PDF dates only when no unactivated web item owns the core row."""
    if not pdf_occurrence_ids:
        return set()
    with reader.connect() as connection:
        linked_articles = {
            str(row["core_article_id"])
            for row in connection.execute(
                """SELECT o.occurrence_id, p.core_article_id, p.confirmation_basis
                   FROM pdf_intake_article_occurrences o
                   JOIN pdf_intake_articles p ON p.article_id=o.article_id
                   WHERE p.confirmation_basis='exact_url_eligible_detail'
                     AND p.core_article_id IS NOT NULL"""
            )
            if row["occurrence_id"] in pdf_occurrence_ids
        }
        unactivated_articles = {
            str(row["article_id"])
            for row in connection.execute(
                "SELECT acquisition_item_id, article_id FROM acquisition_items WHERE article_id IS NOT NULL"
            )
            if row["acquisition_item_id"] not in activated_web_item_ids
        }
    return linked_articles - unactivated_articles


def _range_source(reader, start_date, end_date, **filters):
    with reader.public_snapshot():
        return _range_source_read(reader,start_date,end_date,**filters)


def _range_source_read(
    reader: RegistryReader,
    start_date: str,
    end_date: str,
    *,
    acquisition_item_ids: set[str] | None = None,
    pdf_occurrence_ids: set[str] | None = None,
    pdf_date_fallback_article_ids: set[str] | None = None,
    additional_evidenced_dates: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Freeze persisted Registry evidence without consulting reports or the web."""
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    article_rows: dict[str, dict[str, Any]] = {}
    evidenced_dates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    publication_dates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pdf_publication_dates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    collection_dates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    information_dates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    report_dates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    selected_fetch_ids: set[str] = set()
    all_identities: set[str] = set()
    pdf_observations: list[dict[str, Any]] = []

    with reader.connect() as connection:
        manual_enrichments = connection.execute("PRAGMA user_version").fetchone()[0] >= 20
        public_details = {}
        article_version_projection = (
            "article_version_id" if manual_enrichments else "NULL AS article_version_id"
        )
        for row in connection.execute(
            """SELECT a.article_id, a.canonical_url, a.current_version_id,
                      a.current_content_version_id, a.display_policy,
                      s.display_name AS publisher, av.observed_title AS current_title,
                      av.observed_summary AS current_summary
               FROM articles a
               JOIN sources s ON s.source_id=a.source_id
               LEFT JOIN article_versions av ON av.version_id=a.current_version_id
               WHERE a.publication_eligible=1 AND a.document_kind='article'
               ORDER BY a.article_id"""
        ):
            if acquisition_item_ids is None:
                all_identities.add(row["article_id"])
            article_rows[row["article_id"]] = dict(row)
        if reader.public and connection.execute("PRAGMA user_version").fetchone()[0]>=21:
            public_details = {identity:reader.article(identity) for identity in article_rows}
        acquisition_rows = connection.execute(
            """
            SELECT i.acquisition_item_id, i.article_id, i.raw_url, i.source_name, i.title,
                   i.summary, i.discovered_at, i.origins_json, i.publication_date,
                   i.publication_date_evidence_json, i.content_version_id, i.fetch_id,
                   i.resolved_by_fetch_id,
                   a.canonical_url, a.current_version_id, a.current_content_version_id,
                   a.display_policy, s.display_name AS publisher,
                   av.observed_title AS current_title
            FROM acquisition_items i
            JOIN articles a ON a.article_id=i.article_id
            JOIN sources s ON s.source_id=a.source_id
            LEFT JOIN article_versions av ON av.version_id=a.current_version_id
            ORDER BY i.article_id, i.discovered_at, i.acquisition_item_id
            """
        ).fetchall() if RegistryReader._has_acquisition_projection(connection) else []
        for row in acquisition_rows:
            if acquisition_item_ids is not None and row["acquisition_item_id"] not in acquisition_item_ids:
                continue
            selected_fetch_ids.add(row["fetch_id"])
            if row["resolved_by_fetch_id"]:
                selected_fetch_ids.add(row["resolved_by_fetch_id"])
            article_id = row["article_id"]
            all_identities.add(article_id)
            evidence = _json_object(row["publication_date_evidence_json"], "publication evidence")
            publication_date = _day_precision_publication_date(row["publication_date"])
            if publication_date and evidence:
                date_evidence = {
                    "date": publication_date,
                    "observation_id": row["acquisition_item_id"],
                    "evidence": evidence,
                }
                evidenced_dates[article_id].append(date_evidence)
                publication_dates[article_id].append(date_evidence)
            if article_id not in article_rows:
                continue
            origins = _json_list(row["origins_json"], "acquisition origins")
            if any(not isinstance(item, dict) for item in origins):
                raise RegistryContractError("invalid acquisition origins")
            observation = {
                "kind": "registry_acquisition",
                "observation_id": row["acquisition_item_id"],
                "url": row["raw_url"],
                "source_name": row["source_name"],
                "title": row["title"],
                "summary": row["summary"],
                "observed_at": row["discovered_at"],
                "publication_date": row["publication_date"],
                "publication_date_evidence": evidence,
                "content_version_id": row["content_version_id"],
                "origins": origins,
            }
            observations[article_id].append(observation)

        successful_fetches = connection.execute(
            """SELECT fetch_id, article_id, requested_url, final_url, fetched_at
               FROM article_fetches WHERE fetch_status='success' AND http_status BETWEEN 200 AND 299
                 AND content_version_id IS NOT NULL ORDER BY fetched_at, fetch_id"""
        ).fetchall()
        for row in successful_fetches:
            if acquisition_item_ids is not None and row["fetch_id"] not in selected_fetch_ids:
                continue
            if row["article_id"] not in article_rows:
                continue
            collected_at, collected_date = _collection_timestamp(row["fetched_at"])
            collection_dates[row["article_id"]].append({
                "date": collected_date,
                "collected_at": collected_at,
                "observation_id": row["fetch_id"],
                "basis": "registry_fetch",
                "evidence": {"requested_url": row["requested_url"], "final_url": row["final_url"]},
            })

        has_date_observations = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='article_date_observations'"
        ).fetchone() is not None
        if has_date_observations:
            date_rows = (dict(item,article_id=identity,canonical_url=detail["canonical_url"],
                observation_kind=item["kind"],evidence_json=json.dumps(item["evidence"]))
                for identity,detail in public_details.items() for item in detail.get("date_observations",[])) if public_details else connection.execute(
                """SELECT observation_id, article_id, canonical_url, observation_kind,
                          observed_at, evidence_json
                   FROM article_date_observations ORDER BY article_id, observed_at, observation_id"""
            )
            for row in date_rows:
                if row["article_id"] not in article_rows:
                    continue
                if article_rows[row["article_id"]]["canonical_url"] != row["canonical_url"]:
                    raise RegistryContractError("article date observation URL differs from Registry identity")
                evidence = _json_object(row["evidence_json"], "article date evidence")
                observation = {
                    "date": None,
                    "observation_id": row["observation_id"],
                    "evidence": evidence,
                }
                if row["observation_kind"] == "collection":
                    from .article_dates import report_observation_date
                    report_day = report_observation_date(row["observed_at"], evidence)
                    if report_day:
                        observation.update(date=report_day, basis="daily_or_weekly_report_date")
                        report_dates[row["article_id"]].append(observation)
                    else:
                        collected_at, collected_date = _collection_timestamp(row["observed_at"])
                        observation.update(date=collected_date, collected_at=collected_at,
                                           basis="web_listening_snapshot")
                        collection_dates[row["article_id"]].append(observation)
                elif row["observation_kind"] == "page_information":
                    publication_date = _day_precision_publication_date(row["observed_at"])
                    if publication_date is None:
                        raise RegistryContractError("invalid Registry page information date")
                    observation.update(date=publication_date, basis="page_information")
                    information_dates[row["article_id"]].append(observation)
                else:
                    raise RegistryContractError("invalid Registry article date observation kind")

        # Render stores the approved DTO, without private capture/check rows.
        # Fill only public evidence absent from SQL; never invent source facts.
        for identity,detail in public_details.items():
            known={item["observation_id"] for item in observations[identity]}
            for item in detail.get("acquisition_observations",[]):
                key=item["acquisition_item_id"]
                if key in known or item.get("processing_status","complete")!="complete" or item.get("selection_status","selected")!="selected":
                    continue
                if acquisition_item_ids is not None and key not in acquisition_item_ids:
                    continue
                observations[identity].append({"kind":"registry_acquisition","observation_id":key,
                    "url":item["raw_url"],"title":item.get("title"),"summary":item.get("summary"),
                    "observed_at":item.get("discovered_at"),"content_version_id":item.get("content_version_id"),
                    "publication_date":item.get("publication_date"),"publication_date_evidence":item.get("publication_date_evidence"),
                    "source_name":item.get("source_name"),"origins":item.get("origins",[])})
                if item.get("collected_at"):
                    stamp,day=_collection_timestamp(item["collected_at"])
                    collection_dates[identity].append({"date":day,"collected_at":stamp,"observation_id":key,"basis":"registry_fetch","evidence":{"requested_url":item["raw_url"]}})
                published=_day_precision_publication_date(item.get("publication_date"))
                if published and item.get("publication_date_evidence"):
                    evidence={"date":published,"observation_id":key,"evidence":item["publication_date_evidence"]}
                    publication_dates[identity].append(evidence);evidenced_dates[identity].append(evidence)
            if not collection_dates[identity] and detail.get("collected_at"):
                stamp,day=_collection_timestamp(detail["collected_at"])
                available=detail.get("available_content") or {}
                collection_dates[identity].append({"date":day,"collected_at":stamp,
                    "observation_id":available.get("fetch_id") or available.get("content_version_id") or identity,"basis":"approved_public_content",
                    "evidence":{"source_url":detail["canonical_url"]}})

        has_pdf = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pdf_intake_articles'"
        ).fetchone() is not None
        pdf_rows = connection.execute(
            """
            SELECT o.occurrence_id, o.article_id AS pdf_article_id,
                   p.canonical_url, p.title, p.core_article_id, p.confirmation_basis,
                   p.type_safe_classification_json,
                   o.source_document_sha256, o.page, o.raw_url, o.publication_date,
                   o.content_sha256, o.occurrence_json,
                   d.filename, d.period_start, d.period_end, d.imported_at
            FROM pdf_intake_article_occurrences o
            JOIN pdf_intake_articles p ON p.article_id=o.article_id
            JOIN pdf_intake_documents d ON d.document_sha256=o.source_document_sha256
            ORDER BY o.source_document_sha256, o.occurrence_id
            """
        ).fetchall() if has_pdf else []
        pdf_documents = {}
        if has_pdf:
            pdf_documents = {row["document_sha256"]: _json_object(row["document_json"], "PDF document") or {}
                for row in connection.execute("SELECT document_sha256, document_json FROM pdf_intake_documents")}
        from climate_monitor.pdf_intake import report_update_fields
        for row in pdf_rows:
            if pdf_occurrence_ids is not None and row["occurrence_id"] not in pdf_occurrence_ids:
                continue
            article_id = row["core_article_id"]
            if isinstance(article_id, str):
                all_identities.add(article_id)
            raw = _json_object(row["occurrence_json"], "PDF occurrence") or {}
            if raw.get("summary_basis") == "verbatim_pdf_calendar_row":
                continue
            document = pdf_documents[row["source_document_sha256"]]
            report_fields = report_update_fields(dict(raw, raw_url=row["raw_url"], page=row["page"]), document)
            evidence_text = raw.get("publication_date_evidence")
            evidence = (
                {"kind": "pdf_text", "text": evidence_text,
                 "document_sha256": row["source_document_sha256"], "page": row["page"]}
                if isinstance(evidence_text, str) and evidence_text.strip() else None
            )
            source_rows = RegistryReader._pdf_document_sources(
                connection, row["source_document_sha256"]
            )
            pdf_title = (
                raw.get("anchor_text") or row["title"] or row["raw_url"]
                if pdf_occurrence_ids is None
                else raw.get("anchor_text") or raw.get("title") or row["raw_url"]
            )
            observation = {
                "kind": "registry_pdf",
                "observation_id": row["occurrence_id"],
                "pdf_article_id": row["pdf_article_id"],
                "core_article_id": article_id,
                "confirmation_basis": row["confirmation_basis"],
                "document_sha256": row["source_document_sha256"],
                "content_sha256": row["content_sha256"],
                "batch_id": raw.get("management_batch_id"),
                "filename": row["filename"],
                "period_start": row["period_start"],
                "period_end": row["period_end"],
                "page": row["page"],
                "url": row["raw_url"],
                "title": pdf_title,
                "summary": raw.get("summary"),
                "observed_at": row["imported_at"],
                "publication_date": row["publication_date"],
                "publication_date_evidence": evidence,
                "source_observations": source_rows,
                "report_fields": report_fields,
                "pdf_classification": _json_object(row["type_safe_classification_json"], "PDF classification") or {},
                "structured_report": any("IN WINDOW" in page["text"] for page in document.get("pages", [])),
            }
            pdf_observations.append(observation)
            confirmed_link = (
                row["confirmation_basis"] == "exact_url_eligible_detail"
                and isinstance(article_id, str)
            )
            formal_link = confirmed_link and article_id in article_rows
            publication_date = _day_precision_publication_date(row["publication_date"])
            if confirmed_link and publication_date and evidence:
                date_evidence = {
                    "date": publication_date,
                    "observation_id": row["occurrence_id"],
                    "basis": "pdf_stated_publication_date",
                    "evidence": evidence,
                }
                evidenced_dates[article_id].append(date_evidence)
                if formal_link:
                    pdf_publication_dates[article_id].append(date_evidence)
            if not formal_link:
                continue
            observations[article_id].append(observation)
        if reader.static_snapshot is not None:
            for article in reader.pdf_articles_all(include_linked=True):
                for item in article["occurrences"]:
                    if pdf_occurrence_ids is not None and item["occurrence_id"] not in pdf_occurrence_ids:
                        continue
                    evidence_text=item.get("publication_date_evidence")
                    evidence={"kind":"pdf_text","text":evidence_text,"document_sha256":item["source_document_sha256"],"page":item["page"]} if isinstance(evidence_text,str) and evidence_text.strip() else None
                    observation={"kind":"registry_pdf","observation_id":item["occurrence_id"],"pdf_article_id":article["article_id"],
                        "core_article_id":article.get("core_article_id"),"confirmation_basis":article.get("confirmation_basis"),
                        "document_sha256":item["source_document_sha256"],"content_sha256":item.get("content_sha256"),
                        "batch_id":item.get("management_batch_id"),"filename":item.get("source_filename") or item.get("source_document"),
                        "period_start":item.get("period_start"),"period_end":item.get("period_end"),"page":item["page"],
                        "url":item.get("raw_url") or article["canonical_url"],"title":item.get("anchor_text") or article["title"],
                        "summary":item.get("summary"),"observed_at":item.get("imported_at"),"publication_date":item.get("publication_date"),
                        "publication_date_evidence":evidence,"source_observations":[],"report_fields":item.get("report_fields"),
                        "pdf_classification":article.get("type_safe_classification") or {},"structured_report":item.get("structured_report",False)}
                    pdf_observations.append(observation)
                    identity=observation["core_article_id"]
                    if observation["confirmation_basis"]=="exact_url_eligible_detail" and identity in article_rows:
                        observations[identity].append(observation)
                        published=_day_precision_publication_date(observation["publication_date"])
                        if published and evidence:
                            value={"date":published,"observation_id":item["occurrence_id"],"basis":"pdf_stated_publication_date","evidence":evidence}
                            pdf_publication_dates[identity].append(value);evidenced_dates[identity].append(value)
        for article_id, values in (additional_evidenced_dates or {}).items():
            if article_id in article_rows:
                for item in values:
                    basis = item.get("date_basis", "publication_date")
                    target = (
                        collection_dates[article_id] if basis == "collection_time"
                        else information_dates[article_id] if basis == "information_date"
                        else report_dates[article_id] if basis == "report_date"
                        else pdf_publication_dates[article_id] if basis == "pdf_stated_date"
                        else publication_dates[article_id]
                    )
                    identity = (item["date"], item["observation_id"])
                    if not any((existing["date"], existing["observation_id"]) == identity for existing in target):
                        target.append(item)
                    if basis not in {"collection_time", "information_date", "report_date"}:
                        evidenced_dates[article_id].append(item)
        selected_dates: dict[str, tuple[str, dict[str, Any]]] = {}
        for article_id in article_rows:
            if acquisition_item_ids is not None and not observations[article_id]:
                continue
            collection_values = collection_dates[article_id]
            precise_publication_dates = publication_dates[article_id] or (
                pdf_publication_dates[article_id] if pdf_date_fallback_article_ids is None
                or article_id in pdf_date_fallback_article_ids else [])
            candidates = (collection_values or information_dates[article_id]
                          or precise_publication_dates or report_dates[article_id])
            in_range = [item for item in candidates
                        if start <= date.fromisoformat(item["date"]) <= end]
            if not in_range:
                continue
            basis = ("collection_time" if collection_values else
                     "information_date" if information_dates[article_id] else
                     "publication_date" if precise_publication_dates else "report_date")
            selected = max(
                in_range,
                key=lambda item: (item.get("collected_at") or item["date"], item["observation_id"]),
            )
            selected_dates[article_id] = (basis, selected)
        selected_ids = set(selected_dates)
        articles = []
        for article_id in sorted(selected_ids):
            base = article_rows[article_id]
            exact_dates = sorted(
                (item for item in evidenced_dates[article_id]
                 if start <= date.fromisoformat(item["date"]) <= end),
                key=lambda item: (item["date"], item["observation_id"]),
            )
            basis, selected_date = selected_dates[article_id]
            all_publication_dates = sorted(
                publication_dates[article_id] or pdf_publication_dates[article_id],
                key=lambda item: (item["date"], item["observation_id"]),
            )
            publication_date = all_publication_dates[-1]["date"] if all_publication_dates else None
            page_information_values = information_dates[article_id]
            latest_information_date = (
                max(page_information_values, key=lambda item: (item["date"], item["observation_id"]))
                if page_information_values else None
            )
            information_date = (
                latest_information_date["date"] if latest_information_date else None
            )
            in_range_collections = sorted(
                (item for item in collection_dates[article_id]
                 if start <= date.fromisoformat(item["date"]) <= end),
                key=lambda item: (item["collected_at"], item["observation_id"]),
            )
            source_items = observations[article_id]
            latest_source = (
                max(source_items, key=lambda item: (
                    item.get("observed_at") or "", item["observation_id"]
                ))
                if source_items else None
            )
            content = None
            enrichment = None
            pinned_version = next((
                item["content_version_id"] for item in reversed(sorted(
                    source_items,
                    key=lambda value: (value.get("observed_at") or "", value["observation_id"]),
                ))
                if item["kind"] == "registry_acquisition"
                and item.get("content_version_id")
            ), None)
            version_id = (
                pinned_version
                if acquisition_item_ids is not None
                else base.get("current_content_version_id")
            )
            if version_id:
                content = connection.execute(
                    """SELECT content_version_id, markdown_content, content_sha256,
                              extraction_method, extraction_version, first_fetched_at
                       FROM article_content_versions
                       WHERE article_id=? AND content_version_id=?""",
                    (article_id, version_id),
                ).fetchone()
                enrichment = connection.execute(
                    """SELECT enrichment_id, summary, categories_json, keywords_json,
                              language, generator_kind, generator_name, generator_version,
                              generated_at, """ + article_version_projection + """
                       FROM article_enrichments
                       WHERE content_version_id=? AND status='complete'
                       ORDER BY generated_at DESC, enrichment_id DESC LIMIT 1""",
                    (version_id,),
                ).fetchone()
            elif base.get("current_version_id") and manual_enrichments:
                enrichment = connection.execute(
                    """SELECT enrichment_id, summary, categories_json, keywords_json,
                              language, generator_kind, generator_name, generator_version,
                              generated_at, article_version_id
                       FROM article_enrichments
                       WHERE content_version_id IS NULL AND article_id=?
                         AND article_version_id=? AND status='complete'
                       ORDER BY generated_at DESC, enrichment_id DESC LIMIT 1""",
                    (article_id, base["current_version_id"]),
                ).fetchone()
            if enrichment:
                summary = enrichment["summary"]
                categories = _json_list(enrichment["categories_json"], "enrichment categories")
                keywords = _json_list(enrichment["keywords_json"], "enrichment keywords")
                semantic_provenance = {
                    "basis": (
                        "manual_enrichment"
                        if enrichment["generator_kind"] == "manual"
                        else "article_enrichment"
                    ),
                    "enrichment_id": enrichment["enrichment_id"],
                    "content_version_id": version_id,
                    "article_version_id": enrichment["article_version_id"],
                    "generator": {
                        "kind": enrichment["generator_kind"], "name": enrichment["generator_name"],
                        "version": enrichment["generator_version"], "generated_at": enrichment["generated_at"],
                    },
                }
            else:
                summary = (
                    latest_source.get("summary")
                    if latest_source else base.get("current_summary")
                )
                categories, keywords = [], []
                semantic_provenance = (
                    {
                        "basis": latest_source["kind"],
                        "observation_id": latest_source["observation_id"],
                    }
                    if latest_source else {
                        "basis": "current_article_version",
                        "version_id": base.get("current_version_id"),
                    }
                )
            content_text = None
            if content is not None:
                policy = base.get("display_policy")
                if policy == "full_markdown":
                    content_text = article_body_markdown(content["markdown_content"])
                elif policy == "summary_excerpt":
                    content_text = article_preview(content["markdown_content"])
            if acquisition_item_ids is None:
                title = (
                    base.get("current_title")
                    or (latest_source or {}).get("title")
                    or base["canonical_url"]
                )
                title_provenance = {
                    "basis": (
                        "current_article_version"
                        if base.get("current_title")
                        else latest_source["kind"] if latest_source else "canonical_url"
                    ),
                    "version_id": base.get("current_version_id"),
                }
            else:
                title_source = next(
                    (
                        item for item in reversed(sorted(
                            source_items,
                            key=lambda value: (
                                value.get("observed_at") or "",
                                value["observation_id"],
                            ),
                        ))
                        if item.get("title")
                    ),
                    None,
                )
                title = (
                    title_source["title"] if title_source else base["canonical_url"]
                )
                title_provenance = {
                    "basis": title_source["kind"] if title_source else "canonical_url",
                    "observation_id": (
                        title_source["observation_id"] if title_source else None
                    ),
                }
            citations = []
            seen_citations: set[tuple[Any, ...]] = set()
            for item in source_items:
                if item["kind"] == "registry_pdf":
                    key = ("pdf", item["document_sha256"], item["page"], item.get("url"))
                    citation = {
                        "kind": "pdf_page", "document_sha256": item["document_sha256"],
                        "filename": item["filename"], "page": item["page"], "url": item.get("url"),
                    }
                else:
                    urls = [item["url"], *(
                        origin.get("url") for origin in item.get("origins", [])
                        if isinstance(origin.get("url"), str)
                    )]
                    for url in urls:
                        key = ("url", url)
                        if key not in seen_citations:
                            citations.append({"kind": "url", "url": url})
                            seen_citations.add(key)
                    continue
                if key not in seen_citations:
                    citations.append(citation)
                    seen_citations.add(key)
            from .publication import snapshot_metadata, approved_display
            approved = snapshot_metadata(connection, "article", article_id)
            if approved:
                display = approved_display(approved)
                if approved.get("sources"):
                    base["publisher"] = approved["sources"][0]["display_name"]
                title, summary = display.get("title", title), display.get("summary", summary)
                categories, keywords = display.get("categories", categories), display.get("keywords", keywords)
                if display:
                    semantic_provenance = {"basis": display.get("summary_provenance"),
                        "generator": display.get("supplement_generator"),
                        "field_sources": display.get("supplement_provenance", {}).get("field_sources")}
            detail=public_details.get(article_id)
            if detail:
                title=detail.get("title") or detail["canonical_url"]
                summary=detail.get("summary") if detail.get("summary") is not None else detail.get("report_summary")
                categories,keywords=detail.get("categories",[]),detail.get("keywords",[])
                base["publisher"]=detail.get("publisher")
                available=detail.get("available_content") or {}
                content_text=available.get("markdown") or available.get("supporting_excerpt")
                version_id=available.get("content_version_id") or version_id
                content=connection.execute("SELECT content_version_id,content_sha256,extraction_method,extraction_version FROM article_content_versions WHERE article_id=? AND content_version_id=?",(article_id,version_id)).fetchone() if version_id else None
                if content is None and version_id:
                    metadata=detail.get("content") or {}
                    if metadata.get("content_version_id")!=version_id:
                        metadata=available
                    content={key:metadata.get(key) for key in ("content_sha256","extraction_method","extraction_version")}
                title_provenance={"basis":"approved_public_version","candidate_sha256":detail["published_candidate_sha256"]}
                semantic_provenance={**title_provenance,"summary_basis":detail.get("summary_provenance") or ("source_report" if detail.get("report_summary") else None),
                    "metadata_provenance":detail.get("metadata_provenance")}
                if not citations:
                    citations=[{"kind":"url","url":detail["canonical_url"]}]
            articles.append({
                "article_id": article_id,
                "canonical_url": base["canonical_url"],
                "title": title,
                "publisher": base.get("publisher"),
                "publication_date": publication_date,
                "information_date": information_date,
                "date_basis": basis,
                "range_date": selected_date["date"],
                "collected_at": selected_date.get("collected_at") if basis == "collection_time" else None,
                "summary": summary,
                "categories": categories,
                "keywords": keywords,
                "content_version_id": version_id,
                "content": content_text,
                "source_observations": source_items,
                "citations": citations,
                "provenance": {
                    "publication_date": {
                        "basis": "evidenced_registry_observation",
                        "selected": next((item for item in all_publication_dates
                                           if item["date"] == publication_date), None),
                        "all_in_range": exact_dates,
                    },
                    "information_date": {
                        "basis": "publisher_page_information" if latest_information_date else "none",
                        "selected": latest_information_date,
                        "all_in_range": sorted(information_dates[article_id],
                            key=lambda item: (item["date"], item["observation_id"])),
                    },
                    "collection_time": {
                        "selected": selected_date if basis == "collection_time" else None,
                        "all_in_range": in_range_collections,
                        "all": sorted(collection_dates[article_id],
                                      key=lambda item: (item["collected_at"], item["observation_id"])),
                    },
                    **({"report_date": {
                        "selected": selected_date if basis == "report_date" else None,
                        "all": sorted(report_dates[article_id],
                                      key=lambda item: (item["date"], item["observation_id"])),
                    }} if report_dates[article_id] else {}),
                    "date_basis": basis,
                    "title": title_provenance,
                    "summary": semantic_provenance,
                    "categories": semantic_provenance,
                    "keywords": semantic_provenance,
                    "content_version": {
                        "content_version_id": version_id,
                        "content_sha256": content["content_sha256"] if content else None,
                        "extraction_method": content["extraction_method"] if content else None,
                        "extraction_version": content["extraction_version"] if content else None,
                    },
                },
            })

        selected_ids = {item["article_id"] for item in articles}
        pdf_source_updates = []
        pdf_exclusions = {"non_overlapping_coverage": 0, "unknown_coverage": 0}
        seen_pdf_observations: set[tuple[Any, ...]] = set()
        seen_pdf_updates: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in pdf_observations:
            identity = (
                item["document_sha256"], item["page"], item["url"], item["content_sha256"]
            )
            if identity in seen_pdf_observations:
                continue
            seen_pdf_observations.add(identity)
            article_id = item["core_article_id"]
            confirmed_link = (
                item["confirmation_basis"] == "exact_url_eligible_detail"
                and isinstance(article_id, str)
            )
            formal_link = confirmed_link and article_id in article_rows
            published = (_day_precision_publication_date(item["publication_date"])
                if item["publication_date_evidence"] else None)
            if formal_link and article_id in selected_ids:
                continue
            if confirmed_link and not published and article_id and evidenced_dates[article_id]:
                continue
            fields = item["report_fields"]
            if fields is None and (item["structured_report"] or item["pdf_classification"].get("label") == "landing_page"):
                continue
            if published and not start <= date.fromisoformat(published) <= end:
                continue
            period_start = _day_precision_publication_date(item["period_start"])
            period_end = _day_precision_publication_date(item["period_end"])
            if period_start is None or period_end is None or period_start > period_end:
                pdf_exclusions["unknown_coverage"] += 1
                continue
            if not published and (date.fromisoformat(period_end) < start or date.fromisoformat(period_start) > end):
                pdf_exclusions["non_overlapping_coverage"] += 1
                continue
            update = {
                **item,
                **(fields or {}),
                "publication_date": published,
                "publication_date_label": "PDF-stated publication date" if published else "文章发布日期未确认",
                "coverage_period": {"start": period_start, "end": period_end},
                "citations": [{
                    "kind": "pdf_page",
                    "document_sha256": item["document_sha256"],
                    "filename": item["filename"],
                    "page": item["page"],
                    "url": item["url"],
                }],
            }
            display_key = (item["document_sha256"], item["url"], update["publication_date"],
                " ".join(update["title"].split()), " ".join((update.get("summary") or "").split()))
            if fields and display_key in seen_pdf_updates:
                previous = seen_pdf_updates[display_key]
                previous["citations"].extend(c for c in update["citations"] if c not in previous["citations"])
                continue
            seen_pdf_updates[display_key] = update
            pdf_source_updates.append(update)

    unknown_publication_ids = sorted(article_id for article_id in all_identities
                                     if not evidenced_dates[article_id])
    unknown_date_ids = sorted(
        article_id for article_id in all_identities
        if not (collection_dates[article_id] or information_dates[article_id]
                or publication_dates[article_id] or evidenced_dates[article_id] or report_dates[article_id])
    )
    return {
        "articles": articles,
        "pdf_source_updates": pdf_source_updates,
        "pdf_source_exclusion_counts": pdf_exclusions,
        "unknown_publication_date_count": len(unknown_publication_ids),
        "unknown_publication_date_article_ids": unknown_publication_ids,
        "date_unknown_count": len(unknown_date_ids),
        "date_unknown_article_ids": unknown_date_ids,
    }


def _meeting_status(coverage: dict[str, Any], records: list[Any]) -> str:
    if coverage.get("status") in {"succeeded", "succeeded_empty", "complete", "approved_git_snapshot"} or (
        coverage.get("status") == "processed" and coverage.get("records_scope") == "approved_versions"
    ):
        return "included" if records else "empty"
    if coverage.get("status") == "partial":
        return "partial"
    return "unavailable"


def _default_meeting_query_filters() -> dict[str, Any]:
    return {
        "organizer": None, "event_types": sorted(EVENT_TYPES), "start_date": None,
        "end_date": None, "include_unknown": False, "include_deadlines": True,
        "include_cancelled": False, "include_retrospective": False,
    }


def _query_identity(payload: dict[str, Any]) -> tuple[str, str]:
    digest = _digest(payload)
    return "meeting-query-" + digest[:24], digest


def _meeting_payload(
    reader: RegistryReader, snapshot_id: str | None, *, base_date: str
) -> dict[str, Any]:
    if snapshot_id is None:
        try:
            if reader.static_snapshot is not None:
                public_meetings = reader.meetings(base_date=base_date, page_size=1)
                queried={"schema_version":"climate-meeting-query.v1","base_date":base_date,"timezone":TIMEZONE,
                    "filters":_default_meeting_query_filters(),"coverage":public_meetings["coverage"],
                    "records":[item for item in reader.meetings_all(base_date=base_date) if item.get("origin")=="web_collection"]}
            else:
                with reader.connect() as connection:
                    queried = query_events(reader.database, base_date=base_date, timezone_name=TIMEZONE, include_deadlines=True,
                        **({"registry_connection": connection,"coverage_connection":getattr(reader,"_snapshot_source_connection",connection)} if connection.execute("PRAGMA user_version").fetchone()[0] >= 21 else {}))
        except (OSError, RegistryError, ValueError, sqlite3.Error) as exc:
            frozen_query = {
                "schema_version": "climate-meeting-query.v1", "base_date": base_date,
                "timezone": TIMEZONE, "filters": _default_meeting_query_filters(),
                "coverage": {"status": "unavailable", "error": type(exc.__cause__ or exc).__name__}, "records": [],
            }
            query_id, query_sha256 = _query_identity(frozen_query)
            return {
                "status": "unavailable", "source": "query", "snapshot_id": None,
                "snapshot_sha256": None, "query_id": query_id, "query_sha256": query_sha256,
                "base_date": base_date, "timezone": TIMEZONE,
                "query": frozen_query["filters"], "query_payload": frozen_query,
                "coverage": frozen_query["coverage"], "records": [],
            }
        records = queried.get("records") if isinstance(queried.get("records"), list) else []
        coverage = queried.get("coverage") if isinstance(queried.get("coverage"), dict) else {
            "status": "unavailable", "error": "invalid_query_coverage"
        }
        frozen_query = {
            "schema_version": queried.get("schema_version"), "base_date": queried.get("base_date"),
            "timezone": queried.get("timezone"), "filters": queried.get("filters"),
            "coverage": coverage, "records": records,
        }
        query_id, query_sha256 = _query_identity(frozen_query)
        return {
            "status": _meeting_status(coverage, records), "source": "query", "snapshot_id": None,
            "snapshot_sha256": None, "query_id": query_id,
            "query_sha256": query_sha256, "base_date": queried.get("base_date"),
            "timezone": queried.get("timezone"), "query": queried.get("filters"),
            "query_payload": frozen_query, "coverage": coverage, "records": records,
        }
    try:
        snapshot = load_meeting_snapshot(reader.database, snapshot_id)
    except (KeyError, OSError, RegistryError, ValueError):
        return {"status": "unavailable", "snapshot_id": snapshot_id, "snapshot_sha256": None, "records": []}
    coverage = snapshot.get("coverage") or {}
    records = snapshot.get("records") or []
    with reader.connect() as connection:
        if reader.public and connection.execute("PRAGMA user_version").fetchone()[0]>=21:
            current=query_events(reader.database,base_date=snapshot["base_date"],timezone_name=snapshot["timezone"],
                registry_connection=connection,**snapshot["query"])["records"]
            approved={_digest(item) for item in current}
            selected=[item for item in records if _digest(item) in approved]
            coverage={**coverage,"excluded_unapproved_count":len(records)-len(selected)}
            records=selected
    return {
        "status": "failed" if coverage.get("status") in {"failed", "partial"} else _meeting_status(coverage, records), "source": "snapshot",
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "base_date": snapshot.get("base_date"), "timezone": snapshot.get("timezone"),
        "query": snapshot.get("query"),
        "coverage": coverage,
        "records": records,
    }


def _calendar_end_date(item: dict[str, Any]) -> date | None:
    from climate_monitor.meetings import _date_bounds
    ends = []
    for value, precision in ((item.get("end_date") or item.get("start_date"), item.get("date_precision")),
        (item.get("deadline_date"), "day")):
        if isinstance(value, str):
            try:
                ends.append(_date_bounds(value, precision, end=True))
            except ValueError:
                pass
    return max(ends, default=None)


def _pdf_calendar_available(reader: RegistryReader) -> bool:
    with reader.connect() as connection:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pdf_intake_calendar_items'"
        ).fetchone() is not None


def _pdf_calendar_payload(
    reader: RegistryReader,
    *,
    base_date: str,
    overlay_reader: RegistryReader | None = None,
    activated_pdf_occurrence_ids: set[str] | None = None,
    activated_calendar_ids: set[str] | None = None,
    identity_reader: RegistryReader | None = None,
) -> dict[str, Any]:
    def observations(
        source: RegistryReader,
        *,
        allowed_pdf_ids: set[str] | None = None,
        allowed_calendar_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        try:
            if not _pdf_calendar_available(source):
                return {
                    "status": "unavailable", "coverage": {"status": "unavailable", "error": "pdf_calendar_unavailable"},
                    "base_date": base_date, "records": [],
                }
            allowed = allowed_calendar_ids
            if allowed_pdf_ids is not None:
                with source.connect() as connection:
                    allowed_documents = {
                        str(row["source_document_sha256"])
                        for row in connection.execute(
                            "SELECT occurrence_id, source_document_sha256 FROM pdf_intake_article_occurrences"
                        )
                        if row["occurrence_id"] in allowed_pdf_ids
                    }
                    if allowed is None:
                        allowed = {
                            str(row["occurrence_id"])
                            for row in connection.execute(
                                "SELECT occurrence_id, source_document_sha256 FROM pdf_intake_calendar_items"
                            )
                            if row["source_document_sha256"] in allowed_documents
                        }
            items = source.pdf_calendar_items_all(allowed_occurrence_ids=allowed)
        except (OSError, RegistryError, ValueError, KeyError, sqlite3.Error) as exc:
            return {
                "status": "unavailable", "coverage": {"status": "unavailable", "error": type(exc).__name__},
                "base_date": base_date, "records": [],
            }
        return {
            "status": "included" if items else "empty", "coverage": {"status": "complete"},
            "base_date": base_date, "records": items,
        }

    try:
        if overlay_reader is None:
            payload = observations(
                reader,
                allowed_pdf_ids=activated_pdf_occurrence_ids,
                allowed_calendar_ids=activated_calendar_ids,
            )
        else:
            payload = observations(reader)
            payload = _merge_pdf_calendars(payload, observations(
                overlay_reader,
                allowed_pdf_ids=activated_pdf_occurrence_ids,
                allowed_calendar_ids=activated_calendar_ids,
            ))
        if payload["status"] == "unavailable":
            return payload
        items = (identity_reader or reader).resolve_pdf_meeting_identities(payload["records"])
    except (OSError, RegistryError, ValueError, KeyError, sqlite3.Error) as exc:
        return {
            "status": "unavailable", "coverage": {"status": "unavailable", "error": type(exc).__name__},
            "base_date": base_date, "records": [],
        }
    base = date.fromisoformat(base_date)
    from climate_monitor.meeting_fields import collected_pdf_meeting
    records = [collected_pdf_meeting(item) for item in items]
    records = [item for item in records if (end := _calendar_end_date(item)) is not None and end >= base]
    records = [item for item in records if item.get("status") not in {"cancelled", "retrospective"}]
    return {
        "status": "included" if records else "empty", "coverage": payload["coverage"],
        "base_date": base_date, "records": records,
    }


def _merge_range_sources(
    base: dict[str, Any],
    overlay: dict[str, Any],
    *,
    preferred_acquisition_item_ids: set[str] | None = None,
) -> dict[str, Any]:
    articles = {item["article_id"]: item for item in base["articles"]}
    for incoming in overlay["articles"]:
        existing = articles.get(incoming["article_id"])
        if existing is None:
            articles[incoming["article_id"]] = incoming
            continue
        existing_has_web = bool(preferred_acquisition_item_ids) and any(
            item.get("kind") == "registry_acquisition"
            and item.get("observation_id") in preferred_acquisition_item_ids
            for item in existing["source_observations"]
        )
        incoming_has_web = bool(preferred_acquisition_item_ids) and any(
            item.get("kind") == "registry_acquisition"
            and item.get("observation_id") in preferred_acquisition_item_ids
            for item in incoming["source_observations"]
        )
        incoming_is_pdf_only = bool(incoming["source_observations"]) and all(
            item.get("kind") == "registry_pdf"
            for item in incoming["source_observations"]
        )
        primary, secondary = (
            (existing, incoming)
            if (
                existing_has_web and not incoming_has_web
                or incoming_is_pdf_only
            )
            else (incoming, existing)
        )
        merged = dict(primary)
        observations = {}
        for item in [*primary["source_observations"], *secondary["source_observations"]]:
            observations.setdefault((item["kind"], item["observation_id"]), item)
        citations = {}
        for item in [*primary["citations"], *secondary["citations"]]:
            identity = (
                ("url", item.get("url"))
                if item["kind"] == "url"
                else (
                    "pdf_page", item.get("document_sha256"),
                    item.get("page"), item.get("url"),
                )
            )
            citations.setdefault(identity, item)
        primary_dates = primary["provenance"]["publication_date"]
        date_evidence = {
            (item["date"], item["observation_id"]): item
            for article in (primary, secondary)
            for item in article["provenance"]["publication_date"]["all_in_range"]
        }
        merged["source_observations"] = list(observations.values())
        merged["citations"] = list(citations.values())
        merged["provenance"] = {
            **primary["provenance"],
            "publication_date": {
                **primary_dates,
                "all_in_range": [date_evidence[key] for key in sorted(date_evidence)],
            },
        }
        articles[incoming["article_id"]] = merged
    updates: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in [*base["pdf_source_updates"], *overlay["pdf_source_updates"]]:
        updates[(item["document_sha256"], item["page"], item.get("url"), item["content_sha256"])] = item
    unknown = (
        set(base["unknown_publication_date_article_ids"])
        | set(overlay["unknown_publication_date_article_ids"])
    ) - set(articles)
    date_unknown = (
        set(base.get("date_unknown_article_ids", []))
        | set(overlay.get("date_unknown_article_ids", []))
    ) - set(articles)
    return {
        "articles": sorted(articles.values(), key=lambda item: (item["range_date"], item["article_id"])),
        "pdf_source_updates": list(updates.values()),
        "pdf_source_exclusion_counts": {
            key: base["pdf_source_exclusion_counts"].get(key, 0)
            + overlay["pdf_source_exclusion_counts"].get(key, 0)
            for key in {"non_overlapping_coverage", "unknown_coverage"}
        },
        "unknown_publication_date_count": len(unknown),
        "unknown_publication_date_article_ids": sorted(unknown),
        "date_unknown_count": len(date_unknown),
        "date_unknown_article_ids": sorted(date_unknown),
    }


def _merge_pdf_calendars(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    if overlay["status"] == "unavailable":
        return base
    if base["status"] == "unavailable":
        return overlay
    from .information_checks import merge_checked_observation
    records: dict[str, dict[str, Any]] = {}
    for item in [*base["records"], *overlay["records"]]:
        identity = str(item.get("occurrence_id") or item.get("event_id") or _digest(item))
        records[identity] = (
            merge_checked_observation(records[identity], item)
            if identity in records else item
        )
    return {
        "status": "included" if records else "empty",
        "coverage": {"status": "complete"},
        "base_date": overlay["base_date"],
        "records": list(records.values()),
    }


def load_active_range_overlay(
    runtime_dir: Path | None,
    queue_dir: Path | None,
    *,
    repository_root: Path,
) -> tuple[RegistryReader | None, RegistryReader | None, dict[str, Any] | None]:
    """Open the immutable Registry snapshots selected by the intake writer."""
    if runtime_dir is None:
        return None, None, None
    if queue_dir is None:
        raise RegistryContractError("runtime Wiki requires the durable intake queue")

    from .pdf_pipeline import load_active_projection, load_projection_manifest
    from .web_ingest_pipeline import _active_pdf_ids, _active_pdf_snapshot

    generation, metadata = load_active_projection(runtime_dir, queue_dir / "active.json")
    if generation is None and metadata is None:
        return None, None, {"web_items": [], "pdf_occurrence_ids": []}
    manifest = load_projection_manifest(generation, metadata)
    if manifest is None:
        pdf_ids = _active_pdf_ids(generation, manifest)
        if not pdf_ids:
            raise RegistryContractError(
                "active legacy PDF projection has no validated identities"
            )
        try:
            snapshot_path, _ = _active_pdf_snapshot(runtime_dir, metadata, pdf_ids)
            pdf_reader = RegistryReader(snapshot_path, repository_root=repository_root)
            with pdf_reader.connect() as connection:
                found = {
                    str(row["occurrence_id"])
                    for row in connection.execute(
                        "SELECT occurrence_id FROM pdf_intake_article_occurrences"
                    )
                    if row["occurrence_id"] in pdf_ids
                }
        except (OSError, RuntimeError, RegistryContractError) as exc:
            raise RegistryContractError("active legacy PDF projection is invalid") from exc
        if found != pdf_ids:
            raise RegistryContractError("active legacy PDF projection is invalid")
        return None, pdf_reader, {
            "web_items": [], "pdf_occurrence_ids": sorted(pdf_ids),
        }

    expected_parent = (runtime_dir / "registry-snapshots").resolve()

    def selected(kind: str, required: bool) -> RegistryReader | None:
        raw_path = metadata.get(f"{kind}_registry_snapshot")
        raw_sha256 = metadata.get(f"{kind}_registry_sha256")
        if not raw_path and not required:
            return None
        snapshot = Path(str(raw_path or "")).resolve()
        if (
            snapshot.parent != expected_parent
            or not snapshot.is_file()
            or not isinstance(raw_sha256, str)
            or hashlib.sha256(snapshot.read_bytes()).hexdigest() != raw_sha256
        ):
            raise RegistryContractError("active intake Registry projection is invalid")
        return RegistryReader(snapshot, repository_root=repository_root)

    return (
        selected("web", bool(manifest["web_items"])),
        selected("pdf", bool(manifest["pdf_occurrence_ids"] or manifest.get("pdf_calendar_occurrence_ids"))),
        manifest,
    )


def freeze_range_report(
    reader: RegistryReader,
    artifact_root: str | Path,
    *,
    start_date: str,
    end_date: str,
    meeting_snapshot_id: str | None = None,
    generated_at: datetime | None = None,
    overlay_reader: RegistryReader | None = None,
    pdf_overlay_reader: RegistryReader | None = None,
    overlay_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with reader.connect() as connection:
        if connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
            overlay_reader = pdf_overlay_reader = None
            overlay_manifest = None
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end or (end - start).days + 1 > MAX_RANGE_DAYS:
        raise RangeReportError("invalid report date range")
    generated = generated_at or datetime.now(timezone.utc)
    base_date = generated.astimezone(timezone.utc).date().isoformat()
    activated_pdf_ids = (
        {
            str(value)
            for value in overlay_manifest.get("pdf_occurrence_ids", [])
        }
        if overlay_manifest is not None
        else None
    )
    activated_web_ids = {
        str(item["acquisition_item_id"])
        for item in (overlay_manifest or {}).get("web_items", [])
    }

    def selected_dates(source: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
        selected = {}
        for item in (source or {}).get("articles", []):
            basis = item["date_basis"]
            if basis == "collection_time":
                observation = item["provenance"]["collection_time"]["selected"]
            elif basis == "information_date":
                observation = item["provenance"]["information_date"]["selected"]
            elif basis == "report_date":
                observation = item["provenance"]["report_date"]["selected"]
            else:
                matching_dates = [
                    value for value in item["provenance"]["publication_date"]["all_in_range"]
                    if value["date"] == item["range_date"]
                ]
                observation = next(
                    (value for value in matching_dates
                     if value.get("basis") != "pdf_stated_publication_date"),
                    matching_dates[0] if matching_dates else None,
                )
            if observation is not None:
                selected_basis = (
                    "pdf_stated_date"
                    if observation.get("basis") == "pdf_stated_publication_date"
                    else basis
                )
                selected[item["article_id"]] = [{
                    **observation, "date_basis": selected_basis,
                }]
        return selected

    pdf_overlay = None
    if pdf_overlay_reader is not None and (activated_pdf_ids or (overlay_manifest or {}).get("pdf_calendar_occurrence_ids")):
        pdf_fallback_article_ids = _active_pdf_date_fallback_article_ids(
            pdf_overlay_reader, activated_pdf_ids, activated_web_ids
        )
        pdf_overlay = _range_source(
            pdf_overlay_reader,
            start_date,
            end_date,
            acquisition_item_ids=set(),
            pdf_occurrence_ids=activated_pdf_ids,
            pdf_date_fallback_article_ids=pdf_fallback_article_ids,
        )
    pdf_dates = selected_dates(pdf_overlay)

    with tempfile.TemporaryDirectory(prefix="climate-range-report-") as temporary:
        temporary_root = Path(temporary)
        database = temporary_root / "registry.sqlite3"
        try:
            snapshot_registry(reader.database, database)
        except (RegistryBuildError, RegistryInputError, sqlite3.DatabaseError, OSError) as exc:
            raise RegistryContractError("Registry snapshot is invalid") from exc
        snapshot_reader = RegistryReader(
            database,
            repository_root=temporary_root / "application",
            source_dir=reader.source_dir,
            metadata_dir=reader.metadata_dir,
        )
        snapshot_reader.static_snapshot=reader.static_snapshot
        public_source = _range_source(snapshot_reader, start_date, end_date)
        public_dates = selected_dates(public_source)
        web_selection_dates = dict(public_dates)
        for article_id, values in pdf_dates.items():
            bucket = web_selection_dates.setdefault(article_id, [])
            known = {(item["date"], item["observation_id"]) for item in bucket}
            bucket.extend(
                item for item in values
                if (item["date"], item["observation_id"]) not in known
            )
        web_overlay = None
        if overlay_reader is not None and overlay_manifest is not None:
            web_overlay = _range_source(
                overlay_reader,
                start_date,
                end_date,
                acquisition_item_ids=activated_web_ids,
                pdf_occurrence_ids=set(),
                additional_evidenced_dates=web_selection_dates,
            )
        selection_dates = dict(web_selection_dates)
        for article_id, values in selected_dates(web_overlay).items():
            bucket = selection_dates.setdefault(article_id, [])
            known = {(item["date"], item["observation_id"]) for item in bucket}
            bucket.extend(
                item for item in values
                if (item["date"], item["observation_id"]) not in known
            )
        source = _range_source(
            snapshot_reader,
            start_date,
            end_date,
            additional_evidenced_dates=selection_dates,
        )
        meeting = _meeting_payload(
            snapshot_reader, meeting_snapshot_id, base_date=base_date
        )
        activated_calendar_ids = (
            set(overlay_manifest["pdf_calendar_occurrence_ids"])
            if overlay_manifest is not None and "pdf_calendar_occurrence_ids" in overlay_manifest else None
        )
        include_pdf_overlay = pdf_overlay_reader is not None and (
            activated_pdf_ids or activated_calendar_ids
        )
        pdf_calendar = _pdf_calendar_payload(
            snapshot_reader,
            base_date=base_date,
            overlay_reader=pdf_overlay_reader if include_pdf_overlay else None,
            activated_pdf_occurrence_ids=activated_pdf_ids if include_pdf_overlay else None,
            activated_calendar_ids=activated_calendar_ids if include_pdf_overlay else None,
            identity_reader=snapshot_reader,
        )
    if web_overlay is not None:
        source = _merge_range_sources(source, web_overlay)
    if pdf_overlay is not None:
        source = _merge_range_sources(
            source,
            pdf_overlay,
            preferred_acquisition_item_ids=activated_web_ids,
        )
    frozen = {
        "schema_version": SCHEMA_VERSION,
        "date_range": {"start": start_date, "end": end_date, "inclusive": True},
        "timezone": TIMEZONE,
        "articles": source["articles"],
        "pdf_source_updates": source["pdf_source_updates"],
        "executive_summary": _freeze_executive_summary(source),
        "pdf_source_exclusion_counts": source["pdf_source_exclusion_counts"],
        "unknown_publication_date_count": source["unknown_publication_date_count"],
        "unknown_publication_date_article_ids": source["unknown_publication_date_article_ids"],
        "date_unknown_count": source.get("date_unknown_count", 0),
        "date_unknown_article_ids": source.get("date_unknown_article_ids", []),
        "meeting": meeting,
        "pdf_calendar": pdf_calendar,
    }
    digest = _digest(frozen)
    snapshot_id = "range-report-" + digest[:24]
    root = Path(artifact_root).resolve(strict=False)
    target = root / snapshot_id / "snapshot.json"
    with exclusive_lock(root, snapshot_id):
        if target.exists():
            existing = load_range_report(root, snapshot_id)
            if existing["snapshot_sha256"] != digest:
                raise RangeReportError("stored range report identity mismatch")
            payload = existing
        else:
            payload = {
                **frozen,
                "snapshot_id": snapshot_id,
                "snapshot_sha256": digest,
                "created_at": generated.isoformat(),
            }
            atomic_write_json(target, payload)
    ensure_range_report_pdf(payload, root)
    return payload


def _validate_snapshot(value: Any, snapshot_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") not in {SCHEMA_VERSION, *LEGACY_SCHEMA_VERSIONS}:
        raise RangeReportError("invalid range report snapshot")
    legacy = value["schema_version"] in LEGACY_SCHEMA_VERSIONS
    if value.get("snapshot_id") != snapshot_id:
        raise RangeReportError("invalid range report identity")
    frozen = {key: item for key, item in value.items() if key not in {"snapshot_id", "snapshot_sha256", "created_at"}}
    digest = _digest(frozen)
    if value.get("snapshot_sha256") != digest:
        raise RangeReportError("range report snapshot hash mismatch")
    if snapshot_id != "range-report-" + digest[:24]:
        raise RangeReportError("invalid range report identity")
    date_range = value.get("date_range")
    articles = value.get("articles")
    has_pdf_updates = "pdf_source_updates" in value
    has_pdf_exclusions = "pdf_source_exclusion_counts" in value
    if has_pdf_updates != has_pdf_exclusions:
        raise RangeReportError("invalid range report schema")
    pdf_source_updates = value.get("pdf_source_updates", [])
    pdf_exclusions = value.get("pdf_source_exclusion_counts", {
        "non_overlapping_coverage": 0,
        "unknown_coverage": 0,
    })
    has_executive_summary = "executive_summary" in value
    executive_summary = value.get("executive_summary")
    meeting = value.get("meeting")
    pdf_calendar = value.get("pdf_calendar")
    unknown_ids = value.get("unknown_publication_date_article_ids")
    date_unknown_ids = value.get("date_unknown_article_ids")
    try:
        start = date.fromisoformat(date_range["start"])
        end = date.fromisoformat(date_range["end"])
        datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise RangeReportError("invalid range report schema") from exc
    if (
        date_range.get("inclusive") is not True
        or value.get("timezone") != TIMEZONE
        or start > end
        or not isinstance(articles, list)
        or not isinstance(pdf_source_updates, list)
        or has_executive_summary and not isinstance(executive_summary, list)
        or not isinstance(pdf_exclusions, dict)
        or set(pdf_exclusions) != {"non_overlapping_coverage", "unknown_coverage"}
        or any(not isinstance(count, int) or count < 0 for count in pdf_exclusions.values())
        or not isinstance(meeting, dict)
        or not isinstance(unknown_ids, list)
        or any(not isinstance(item, str) for item in unknown_ids)
        or len(set(unknown_ids)) != len(unknown_ids)
        or value.get("unknown_publication_date_count") != len(unknown_ids)
        or (not legacy and (
            not isinstance(date_unknown_ids, list)
            or any(not isinstance(item, str) for item in date_unknown_ids)
            or len(set(date_unknown_ids)) != len(date_unknown_ids)
            or value.get("date_unknown_count") != len(date_unknown_ids)
        ))
    ):
        raise RangeReportError("invalid range report schema")
    seen_ids: set[str] = set()
    for article in articles:
        if not isinstance(article, dict):
            raise RangeReportError("invalid range report schema")
        article_id = article.get("article_id")
        publication_date = article.get("publication_date")
        try:
            published = date.fromisoformat(publication_date) if publication_date else None
            if legacy:
                range_date = published
                information_date = None
            else:
                range_date_raw = article["range_date"]
                range_date = date.fromisoformat(range_date_raw)
                information_date_raw = article.get("information_date")
                information_date = (
                    date.fromisoformat(information_date_raw)
                    if information_date_raw is not None else None
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise RangeReportError("invalid range report schema") from exc
        basis = article.get("date_basis")
        collected_at = article.get("collected_at")
        if not legacy:
            if basis not in {"collection_time", "information_date", "publication_date", "report_date"}:
                raise RangeReportError("invalid range report schema")
            if (
                publication_date is not None
                and (published is None or published.isoformat() != publication_date)
                or range_date.isoformat() != range_date_raw
                or information_date_raw is not None
                and (information_date is None or information_date.isoformat() != information_date_raw)
            ):
                raise RangeReportError("invalid range report schema")
            if basis == "collection_time":
                try:
                    normalized, collected_date = _collection_timestamp(collected_at)
                except RegistryContractError as exc:
                    raise RangeReportError("invalid range report schema") from exc
                if normalized != collected_at or date.fromisoformat(collected_date) != range_date:
                    raise RangeReportError("invalid range report schema")
            elif collected_at is not None:
                raise RangeReportError("invalid range report schema")
            if basis == "information_date" and (information_date is None or information_date != range_date):
                raise RangeReportError("invalid range report schema")
            if basis == "publication_date" and (published is None or published != range_date):
                raise RangeReportError("invalid range report schema")
            if basis == "report_date":
                provenance = article.get("provenance")
                report_provenance = provenance.get("report_date") if isinstance(provenance, dict) else None
                selected_report = report_provenance.get("selected") if isinstance(report_provenance, dict) else None
                report_evidence = selected_report.get("evidence") if isinstance(selected_report, dict) else None
                if (not isinstance(selected_report, dict) or not isinstance(report_evidence, dict)
                        or selected_report.get("date") != range_date_raw
                        or selected_report.get("basis") != "daily_or_weekly_report_date"
                        or selected_report.get("collected_at") is not None
                        or report_evidence.get("date_basis") != "daily_or_weekly_report_date"
                        or report_evidence.get("report_date") != range_date_raw):
                    raise RangeReportError("invalid range report schema")
        if (
            not isinstance(article_id, str)
            or not article_id
            or article_id in seen_ids
            or range_date is None
            or not start <= range_date <= end
            or publication_date is not None and published is None
            or not isinstance(article.get("title"), str)
            or not isinstance(article.get("source_observations"), list)
            or not isinstance(article.get("citations"), list)
            or not isinstance(article.get("provenance"), dict)
            or any(not isinstance(item, str) for item in article.get("categories", []))
            or any(not isinstance(item, str) for item in article.get("keywords", []))
        ):
            raise RangeReportError("invalid range report schema")
        seen_ids.add(article_id)
    seen_pdf_ids: set[tuple[Any, ...]] = set()
    for item in pdf_source_updates:
        if not isinstance(item, dict):
            raise RangeReportError("invalid range report schema")
        coverage = item.get("coverage_period")
        try:
            period_start = date.fromisoformat(coverage["start"])
            period_end = date.fromisoformat(coverage["end"])
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise RangeReportError("invalid range report schema") from exc
        identity = (
            item.get("document_sha256"), item.get("page"), item.get("url"),
            item.get("content_sha256"),
        )
        published = _day_precision_publication_date(item.get("publication_date"))
        if (
            (item.get("publication_date_label") != "PDF-stated publication date" if published
             else item.get("publication_date_label") != "文章发布日期未确认")
            or (published is not None and (not start <= date.fromisoformat(published) <= end
                or not item.get("publication_date_evidence")))
            or period_start > period_end
            or (published is None and (period_end < start or period_start > end))
            or identity in seen_pdf_ids
            or not isinstance(item.get("pdf_article_id"), str)
            or not isinstance(item.get("filename"), str)
            or not isinstance(item.get("page"), int)
            or not isinstance(item.get("document_sha256"), str)
            or not isinstance(item.get("citations"), list)
        ):
            raise RangeReportError("invalid range report schema")
        seen_pdf_ids.add(identity)
    if has_executive_summary and executive_summary != _freeze_executive_summary({
        "articles": articles, "pdf_source_updates": pdf_source_updates,
    }, legacy=legacy):
        raise RangeReportError("invalid frozen executive summary")
    if meeting.get("status") not in {"not_requested", "included", "empty", "partial", "failed", "unavailable"}:
        raise RangeReportError("invalid range report schema")
    if meeting.get("source") == "query":
        query_payload = meeting.get("query_payload")
        digest = _digest(query_payload) if isinstance(query_payload, dict) else None
        if (
            digest is None or meeting.get("query_sha256") != digest
            or meeting.get("query_id") != "meeting-query-" + digest[:24]
            or meeting.get("query") != query_payload.get("filters")
            or meeting.get("base_date") != query_payload.get("base_date")
            or meeting.get("timezone") != query_payload.get("timezone")
            or meeting.get("coverage") != query_payload.get("coverage")
            or meeting.get("records") != query_payload.get("records")
        ):
            raise RangeReportError("invalid frozen meeting query identity")
        try:
            parsed_base = date.fromisoformat(query_payload["base_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RangeReportError("invalid frozen meeting query metadata") from exc
        if (
            query_payload.get("schema_version") != "climate-meeting-query.v1"
            or parsed_base.isoformat() != query_payload["base_date"]
            or query_payload.get("timezone") != TIMEZONE
        ):
            raise RangeReportError("invalid frozen meeting query metadata")
        if (query_payload.get("coverage") or {}).get("status") == "unavailable":
            if (
                query_payload.get("filters") != _default_meeting_query_filters()
                or query_payload.get("records") != []
                or (query_payload.get("coverage") or {}).get("status") != "unavailable"
                or not isinstance((query_payload.get("coverage") or {}).get("error"), str)
                or not (query_payload["coverage"]["error"]).strip()
            ):
                raise RangeReportError("invalid unavailable meeting query")
    if pdf_calendar is not None and (
        not isinstance(pdf_calendar, dict)
        or pdf_calendar.get("status") not in {"included", "empty", "unavailable"}
        or not isinstance(pdf_calendar.get("coverage"), dict)
        or not isinstance(pdf_calendar.get("records"), list)
    ):
        raise RangeReportError("invalid range report schema")
    return value


def load_range_report(artifact_root: str | Path, snapshot_id: str) -> dict[str, Any]:
    if _SNAPSHOT_ID.fullmatch(snapshot_id) is None:
        raise RangeReportError("invalid range report id")
    path = Path(artifact_root).resolve(strict=False) / snapshot_id / "snapshot.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KeyError(snapshot_id) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RangeReportError("invalid range report snapshot") from exc
    return _validate_snapshot(value, snapshot_id)


def pdf_path(artifact_root: str | Path, snapshot_id: str, renderer_version: str | None = None) -> Path:
    version = renderer_version or render_identity()
    if version not in LEGACY_RENDERER_VERSIONS and not is_render_identity(version):
        raise RangeReportError("unknown report renderer")
    return Path(artifact_root).resolve(strict=False) / snapshot_id / f"{snapshot_id}-{version}.pdf"


def ensure_range_report_pdf(snapshot: dict[str, Any], artifact_root: str | Path, renderer_version: str | None = None) -> Path:
    """Reuse exact archived bytes; render only a missing artifact from frozen input."""
    target = pdf_path(artifact_root, snapshot["snapshot_id"], renderer_version)
    if not target.is_file():
        if renderer_version and renderer_version not in LEGACY_RENDERER_VERSIONS | {render_identity()}:
            raise RangeReportError("archived report renderer is unavailable")
        current = pdf_path(artifact_root, snapshot["snapshot_id"])
        if not current.is_file():
            render_range_report_pdf(snapshot, current)
            atomic_write_json(current.with_suffix(".render.json"), {
                "schema_version": "climate-pdf-render.v1",
                "input": {"snapshot_id": snapshot["snapshot_id"], "snapshot_sha256": snapshot["snapshot_sha256"]},
                "rendering": rendering_metadata(),
                "pdf": {"path": current.name, "sha256": hashlib.sha256(current.read_bytes()).hexdigest()},
            })
        if target != current:
            atomic_write_bytes(target, current.read_bytes())
    return target


def _freeze_executive_summary(source: dict[str, Any], *, legacy: bool = False) -> list[dict[str, Any]]:
    """Copy selected stored summaries and their locators into the digest-checked snapshot."""
    points = []
    for article in source["articles"]:
        summary = article.get("summary")
        if isinstance(summary, str) and summary.strip():
            point = {
                "kind": "registry_article", "text": summary,
                "article_id": article["article_id"], "title": article["title"],
                "publication_date": article["publication_date"], "citations": article["citations"],
            }
            if not legacy:
                point.update(date_basis=article.get("date_basis"), range_date=article.get("range_date"),
                             information_date=article.get("information_date"),
                             collected_at=article.get("collected_at"))
            points.append(point)
    for item in source.get("pdf_source_updates", []):
        summary = item.get("summary")
        if isinstance(summary, str) and summary.strip():
            points.append({
                "kind": "pdf_source", "text": summary, "filename": item["filename"],
                "title": item["title"], "page": item["page"],
                "document_sha256": item["document_sha256"], "coverage_period": item["coverage_period"],
                "citations": item["citations"],
            })
    return points


def _articles_by_publisher_topic(snapshot: dict[str, Any]) -> dict[str, dict[str, list[tuple[int, dict[str, Any]]]]]:
    grouped: dict[str, dict[str, list[tuple[int, dict[str, Any]]]]] = {}
    for index, item in enumerate(snapshot["articles"], start=1):
        urls = [item.get("canonical_url"), *(citation.get("url") for citation in item.get("citations", []))]
        publisher = publisher_name(item.get("publisher"), urls)
        categories = [category for category in item.get("categories", []) if category.strip()]
        topic = ", ".join(categories) or "Topic not recorded"
        grouped.setdefault(publisher, {}).setdefault(topic, []).append((index, item))
    return {publisher: dict(sorted(topics.items(), key=lambda pair: pair[0].casefold()))
            for publisher, topics in sorted(grouped.items(), key=lambda pair: pair[0].casefold())}


def render_range_report_html(snapshot: dict[str, Any], *, renderer_version: str | None = None) -> str:
    esc = lambda value: html.escape(str(value), quote=True)
    start, end = snapshot["date_range"]["start"], snapshot["date_range"]["end"]
    grouped_articles = _articles_by_publisher_topic(snapshot)
    contents_groups = "".join(
        f'<li><a href="#publisher-{group_index}">{esc(publisher)}</a><ol>' + "".join(
            f'<li><a href="#publisher-{group_index}-topic-{topic_index}">{esc(topic)}</a><ol>' + "".join(
                f'<li><a href="#article-{index}">{esc(item["title"])}</a></li>'
                for index, item in items
            ) + "</ol></li>"
            for topic_index, (topic, items) in enumerate(topics.items(), start=1)
        ) + "</ol></li>"
        for group_index, (publisher, topics) in enumerate(grouped_articles.items(), start=1)
    )
    blocks = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>Climate Registry report {esc(start)} to {esc(end)}</title>",
        "<style>body{font:16px/1.55 system-ui,sans-serif;max-width:980px;margin:auto;padding:2rem;color:#23303b}"
        "h1,h2{color:#0b3d62}a{overflow-wrap:anywhere}.meta{background:#f4f7f9;padding:1rem}"
        "article{border-top:1px solid #d7dee4;padding-top:1rem;margin-top:1.5rem}code{overflow-wrap:anywhere}</style>",
        "</head><body>",
        f"<h1>Climate Registry report</h1><p class=\"meta\"><strong>{esc(start)} through {esc(end)}</strong> "
        f"(inclusive, {TIMEZONE})<br>Snapshot <code>{esc(snapshot['snapshot_id'])}</code><br>"
        f"Renderer <code>{esc(renderer_version or RENDERER_VERSION)}</code></p>",
        "<h2>Contents</h2><ol>"
        '<li><a href="#executive-summary">Executive Summary</a></li>'
        '<li><a href="#key-dates">Key Dates</a></li>'
        '<li><a href="#updates">Updates by Publisher / Institution</a><ol>' + contents_groups + "</ol></li>" + "".join(
            f'<li><a href="#pdf-update-{index}">{esc(item["title"])}</a></li>'
            for index, item in enumerate(snapshot.get("pdf_source_updates", []), start=1)
        ) + "</ol>",
        '<h2 id="executive-summary">Executive Summary</h2>' + "".join(
            f"<p>{esc(line)}</p>" for line in _executive_summary(snapshot)
        ),
    ]
    meeting = snapshot["meeting"]
    pdf_calendar = snapshot.get("pdf_calendar")
    from climate_monitor.meeting_fields import merge_meeting_observations
    calendar_records = merge_meeting_observations(
        meeting["records"] + (pdf_calendar["records"] if pdf_calendar else []))
    is_pdf = lambda item: bool(item.get("source_document_sha256") or item.get("pdf_observations"))
    run_date = datetime.fromisoformat(snapshot["created_at"].replace("Z", "+00:00")).date().isoformat() if snapshot.get("created_at") else None
    blocks.append('<h2 id="key-dates">Key Dates</h2>')
    if meeting["status"] == "not_requested" and pdf_calendar is None:
        blocks.append("<p>Key dates were not captured in this snapshot.</p>")
    if meeting["status"] != "not_requested":
        label = "Meeting query" if meeting.get("source") == "query" else "Meeting snapshot"
        metadata = "<br>".join(
            f"<strong>{esc(key)}:</strong> {esc(value)}" for key, value in _meeting_metadata(meeting)
        )
        blocks.append(f"<p>{label} status: {esc(meeting['status'])}.<br>{metadata}</p>")
        if meeting["status"] == "empty":
            blocks.append("<p>No future meetings.</p>")
        for record in calendar_records:
            if is_pdf(record):
                continue
            for when, event, institution, relevance, source in _key_date_rows(record, run_date):
                urls = re.findall(r"https?://[^\s]+", source)
                event_html = (
                    f'<a href="{esc(urls[0])}" rel="noopener noreferrer">{esc(event)}</a>'
                    if urls else esc(event)
                )
                details = calendar_details(record)
                details_html = (
                    "<br><strong>Calendar details:</strong> " + "<br>".join(esc(value) for value in details)
                    if details else ""
                )
                marker = " · PDF import" if "PDF import" in source else ""
                blocks.append(
                    f"<p><strong>{event_html}</strong>: {esc(when)}<br>"
                    f"Institution: {esc(institution)}; Relevance: {esc(relevance)}{esc(marker)}{details_html}</p>"
                )
    if pdf_calendar is not None:
        blocks.append(f"<p>PDF calendar status: {esc(pdf_calendar['status'])}. Coverage: {esc(pdf_calendar['coverage'].get('status', 'unavailable'))}.</p>")
        records = [item for item in calendar_records if is_pdf(item)]
        calendar_kind = lambda item: item.get("kind") or next(
            (source.get("kind") for source in item.get("pdf_observations", []) if source.get("kind")), None)
        for heading, kinds in (("PDF Calendar Dates", {"event"}), ("PDF Deadlines", {"deadline"}), ("Other PDF Key Dates", None)):
            selected = [item for item in records if (calendar_kind(item) in kinds if kinds else calendar_kind(item) not in {"event", "deadline"})]
            if selected:
                blocks.append(f"<h3>{heading}</h3><ul>")
                for item in selected:
                    for when, event, institution, relevance, source in _key_date_rows(item, run_date):
                        urls = re.findall(r"https?://[^\s]+", source)
                        event_html = (
                            f'<a href="{esc(urls[0])}" rel="noopener noreferrer">{esc(event)}</a>'
                            if urls else esc(event)
                        )
                        details = calendar_details(item)
                        details_html = (
                            "<br><strong>Calendar details:</strong> " + "<br>".join(esc(value) for value in details)
                            if details else ""
                        )
                        marker = " · PDF import" if "PDF import" in source else ""
                        blocks.append(
                            f"<li><strong>{event_html}</strong>: {esc(when)}<br>"
                            f"Institution: {esc(institution)}; Relevance: {esc(relevance)}{esc(marker)}{details_html}</li>"
                        )
                blocks.append("</ul>")
    blocks.append('<h2 id="updates">Updates by Publisher / Institution</h2>')
    display_number = 0
    for group_index, (publisher, topics) in enumerate(grouped_articles.items(), start=1):
        blocks.append(f'<h3 id="publisher-{group_index}">{esc(publisher)}</h3>')
        for topic_index, (topic, items) in enumerate(topics.items(), start=1):
            blocks.append(f'<h4 id="publisher-{group_index}-topic-{topic_index}">{esc(topic)}</h4>')
            for index, item in items:
                display_number += 1
                date_label = (
                    _range_date_label(item)
                    if item.get("date_basis")
                    else f"publication date {item.get('publication_date') or 'unconfirmed'}"
                )
                other_dates = []
                if item.get("publication_date") and item.get("date_basis") != "publication_date":
                    other_dates.append(f"Publication date: {item['publication_date']}")
                if item.get("information_date") and item.get("date_basis") != "information_date":
                    other_dates.append(f"Information date: {item['information_date']}")
                if item.get("date_basis") == "report_date" and item.get("material_versions"):
                    other_dates.append(f"Report date: {item['range_date']} (article publication date unconfirmed)")
                other_dates_html = "<br>".join(esc(value) for value in other_dates)
                date_details = f"<strong>Date used for range:</strong> {esc(date_label)}<br>"
                if other_dates_html:
                    date_details += other_dates_html + "<br>"
                blocks.extend([
                    f'<article id="article-{index}"><h5>{display_number}. {esc(item["title"])}</h5>',
                    f"<p>{date_details}"
                    f"<strong>Article ID:</strong> <code>{esc(item['article_id'])}</code><br>"
                    f"<strong>Content version:</strong> <code>{esc(item['content_version_id'] or 'none')}</code></p>",
                ])
                for _key, value in _date_basis(item.get("provenance", {}).get("publication_date")):
                    blocks.append(f"<p><strong>Publication date provenance:</strong> {esc(value)}</p>")
                for _key, value in _date_basis(item.get("provenance", {}).get("report_date")):
                    blocks.append(f"<p><strong>Report date provenance:</strong> {esc(value)}</p>")
                content_sha = item.get("provenance", {}).get("content_version", {}).get("content_sha256")
                if content_sha:
                    blocks.append(f"<p><strong>Content SHA-256:</strong> <code>{esc(content_sha)}</code></p>")
                if item.get("summary"):
                    blocks.append(f"<p>{esc(item['summary'])}</p>")
                if item.get("content"):
                    blocks.append("".join(f"<p>{esc(part)}</p>" for part in item["content"].split("\n\n") if part.strip()))
                blocks.extend(f"<p>{esc(caveat)}</p>" for caveat in _caveats(item))
                if item["categories"]:
                    blocks.append(f"<p><strong>Categories:</strong> {esc(', '.join(item['categories']))}</p>")
                if item["keywords"]:
                    blocks.append(f"<p><strong>Keywords:</strong> {esc(', '.join(item['keywords']))}</p>")
                blocks.append("<h6>Sources</h6><ul>")
                citations = item["citations"] or ([{"kind": "url", "url": item["canonical_url"]}] if item.get("canonical_url") else [])
                for url in dict.fromkeys(c["url"] for c in citations if c.get("url")):
                    url = esc(url)
                    blocks.append(f'<li><a href="{url}" rel="noopener noreferrer">{url}</a></li>')
                if any(c["kind"] in {"pdf", "pdf_page"} for c in citations):
                    blocks.append("<li>PDF import</li>")
                blocks.append("</ul></article>")
    pdf_updates = snapshot.get("pdf_source_updates", [])
    if pdf_updates:
        blocks.append("<h2>PDF 来源更新 / PDF Source Updates</h2>")
    for index, item in enumerate(pdf_updates, start=1):
        display_number += 1
        publisher = publisher_name(item.get("publisher"), [item.get("url")])
        blocks.extend([
            f'<article id="pdf-update-{index}"><h2>{display_number}. {esc(item["title"])}</h2>',
            f"<p><strong>Publisher / Institution:</strong> {esc(publisher)}</p>",
            f"<p><strong>{esc(item.get('publication_date') or item['publication_date_label'])}</strong></p>",
        ])
        if item.get("summary"):
            blocks.append(f"<p>{esc(item['summary'])}</p>")
        blocks.extend(f"<p>{esc(caveat)}</p>" for caveat in _caveats(item))
        url = esc(item["url"])
        blocks.append(
            f'<h3>Source</h3><p><a href="{url}" rel="noopener noreferrer">{url}</a> · PDF import</p></article>'
        )
    if snapshot.get("cross_cutting_watch"):
        blocks.append("<h2>Cross-Cutting Watch</h2>")
        blocks.extend(f"<p>{esc(line)}</p>" for line in snapshot["cross_cutting_watch"])
    for title, field, columns in (
        ("Appendix A — Source Coverage", "coverage", ("institution", "status", "detail")),
        ("Appendix B — Access / Route Corrections", "route_corrections", ("source", "detail")),
        ("Appendix C — Glossary", "glossary", ("term", "definition")),
    ):
        if snapshot.get(field):
            blocks.append(f"<h2>{esc(title)}</h2><table><thead><tr>" + "".join(f"<th>{esc(column)}</th>" for column in columns) + "</tr></thead><tbody>")
            for row in snapshot[field]:
                blocks.append("<tr>" + "".join(f"<td>{esc(row.get(column) or 'Not provided')}</td>" for column in columns) + "</tr>")
            blocks.append("</tbody></table>")
    blocks.append("</body></html>")
    return "".join(blocks)


def render_range_report_chat(snapshot: dict[str, Any], *, web_url: str, pdf_url: str) -> str:
    """List the same frozen projects/calendar as the PDF, using Chat Markdown."""
    report = adapt_range_report(snapshot)

    def plain(value):
        return " ".join(str(value).split()).replace("|", "│")

    def link(label, url):
        label = plain(label).replace("[", "(").replace("]", ")")
        encoded = quote(url, safe=":/?#@!$&'+,;=%")
        return f"[{label}]({encoded})"

    def sources(citations):
        return "; ".join(link(url, url) for url in dict.fromkeys(c.url for c in citations if c.url)) or "URL not provided"

    lines = ["# Climate Risk Intelligence Report", f"**Reporting period:** {report.window}",
        f"**Calendar as at:** {report.run_date} (UTC)",
        link("Open web report", web_url) + " · " + link("Download PDF", pdf_url),
        "## Executive Summary", *report.executive_summary, "## Projects in the Reporting Period"]
    number = 0
    for pdf_context in (False, True):
        updates = sorted((u for u in report.updates
                          if (u.date_basis is None and u.imported_from_pdf and u.publication_date is None) == pdf_context),
            key=lambda u: (u.institution.casefold(), u.topic.casefold()))
        if pdf_context:
            if not updates:
                continue
            lines += ["## PDF Source Context - Publication Date Unconfirmed",
                "These source records overlap the requested period by PDF coverage; they are not confirmed publications inside the date window."]
        elif not updates:
            lines.append("No date-confirmed projects matched this reporting period.")
        previous = None
        for update in updates:
            if update.institution != previous:
                lines.append("### " + plain(update.institution))
                previous = update.institution
            number += 1
            url = next((c.url for c in update.citations if c.url), None)
            title = link(update.title, url) if url else plain(update.title)
            if update.date_basis == "collection_time":
                date_label = "**Collected at:** " + (update.collected_at or "Not recorded")
            elif update.date_basis == "information_date":
                date_label = "**Information date:** " + (update.information_date or "Not recorded")
            elif update.date_basis == "publication_date":
                date_label = "**Publication date:** " + (update.publication_date or "Not recorded")
            elif update.date_basis == "report_date":
                date_label = "**Report date:** " + (update.report_date or "Not recorded")
            else:
                date_label = "**Publication date:** " + (update.publication_date or "Unconfirmed")
            lines += [f"#### {number}. {title}",
                date_label + " · **Topic:** " + plain(update.topic)]
            if update.publication_date and update.date_basis != "publication_date":
                lines.append("**Publication date:** " + update.publication_date)
            if update.information_date and update.date_basis != "information_date":
                lines.append("**Information date:** " + update.information_date)
            if update.coverage_period and not update.imported_from_pdf:
                lines.append("**PDF coverage:** " + " through ".join(update.coverage_period))
            lines += [plain(part) for part in update.paragraphs]
            lines.append("**Source:** " + sources(update.citations) + (" · PDF import" if update.imported_from_pdf else ""))

    lines += ["## Current Meetings and Key Dates",
        "Current and future calendar entries are listed as at the date above, independently of the historical project window."]
    lines += [plain(line) for line in report.date_notes
        if not line.startswith("Calendar details: ")
        and not any(token in line for token in (" ID:", "SHA-256:", "timezone:", "base date:"))]
    calendar_sources = [
        "- " + plain(line.removeprefix("Calendar details: "))
        for line in report.date_notes if line.startswith("Calendar details: ")
    ]
    for precise in (True, False):
        rows = [(i, row) for i, row in enumerate(report.key_dates, 1)
            if (calendar_date_bounds(row[0])[2] == "day") == precise]
        if not rows:
            continue
        if not precise:
            lines.append("### Broader Windows - Day Unconfirmed")
        table = ["| Date(s) | Event | Host | Relevance |", "| --- | --- | --- | --- |"]
        for index, (when, event, host, relevance, raw_sources) in sorted(rows, key=lambda pair: calendar_date_bounds(pair[1][0])[0] or "9999"):
            urls = re.findall(r"https?://[^\s]+", raw_sources)
            event_text = link(event, urls[0]) if urls else plain(event)
            if "PDF import" in raw_sources:
                event_text += " · PDF import"
            table.append("| " + " | ".join((plain(when), event_text, plain(host), plain(relevance))) + " |")
        lines.append("\n".join(table))
    if not report.key_dates:
        lines.append("No current calendar entries are available in this snapshot; see the coverage status above.")
    lines.append("## Source Notes")
    lines += list(report.coverage_notes) + calendar_sources
    lines.append("Dates, summaries and citations are retained from stored evidence. Consult the original sources before relying on individual statements.")
    return "\n\n".join(lines)


def render_range_report_pdf(snapshot: dict[str, Any], output: str | Path) -> None:
    """Render this saved snapshot with the shared default template."""
    try:
        render_report(adapt_range_report(snapshot), output)
    except GenerationError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise GenerationError("range PDF input is invalid") from exc
