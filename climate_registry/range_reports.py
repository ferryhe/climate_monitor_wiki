from __future__ import annotations

import hashlib
import html
import io
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

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
)
from reportlab.platypus.tableofcontents import TableOfContents

from climate_delivery.io import atomic_write_bytes, atomic_write_json, exclusive_lock
from climate_monitor.meetings import EVENT_TYPES, load_snapshot as load_meeting_snapshot
from climate_monitor.meetings import query_events

from .errors import RegistryBuildError, RegistryInputError
from .read_api import RegistryContractError, RegistryError, RegistryReader
from .wiki import snapshot_registry


SCHEMA_VERSION = "climate-range-report-snapshot.v1"
RENDERER_VERSION = "range-report-v2"
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
    r"(?:last|past|recent)\s+(\d{1,4})\s+days?|最近\s*(\d{1,4})\s*天",
    re.IGNORECASE,
)
_FOURTEEN_DAYS = re.compile(
    r"(?:last|past|recent)\s+(?:two\s+weeks|14\s+days?)|最近\s*(?:两周|十四天|14\s*天)",
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
    supported_range_request = bool(_REPORT_NOUN.search(text)) and (
        bool(_FOURTEEN_DAYS.search(text))
        or bool(_RELATIVE_DAYS.search(text))
        or len(explicit_values) == 2
    )
    report_requested = (
        type_safe["action"] == "generate_registry_report"
        if type_safe is not None
        else bool(_REPORT_CREATION.search(text)) or supported_range_request
    )
    if not report_requested:
        return ReportRoute("normal_chat")

    meeting_match = _MEETING_SNAPSHOT_ID.search(text)
    meeting_snapshot_id = meeting_match.group(0) if meeting_match else None
    relative = _RELATIVE_DAYS.search(text)
    if _FOURTEEN_DAYS.search(text):
        start, end = today - timedelta(days=13), today
    elif relative:
        days = int(relative.group(1) or relative.group(2))
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


def _range_source(
    reader: RegistryReader,
    start_date: str,
    end_date: str,
    *,
    acquisition_item_ids: set[str] | None = None,
    pdf_occurrence_ids: set[str] | None = None,
    additional_evidenced_dates: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Freeze persisted Registry evidence without consulting reports or the web."""
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    article_rows: dict[str, dict[str, Any]] = {}
    evidenced_dates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_identities: set[str] = set()
    pdf_observations: list[dict[str, Any]] = []

    with reader.connect() as connection:
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
        acquisition_rows = connection.execute(
            """
            SELECT i.acquisition_item_id, i.article_id, i.raw_url, i.source_name, i.title,
                   i.summary, i.discovered_at, i.origins_json, i.publication_date,
                   i.publication_date_evidence_json, i.content_version_id,
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
            article_id = row["article_id"]
            all_identities.add(article_id)
            evidence = _json_object(row["publication_date_evidence_json"], "publication evidence")
            publication_date = _day_precision_publication_date(row["publication_date"])
            if publication_date and evidence:
                evidenced_dates[article_id].append({
                    "date": publication_date,
                    "observation_id": row["acquisition_item_id"],
                    "evidence": evidence,
                })
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

        has_pdf = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pdf_intake_articles'"
        ).fetchone() is not None
        pdf_rows = connection.execute(
            """
            SELECT o.occurrence_id, o.article_id AS pdf_article_id,
                   p.canonical_url, p.title, p.core_article_id, p.confirmation_basis,
                   o.source_document_sha256, o.page, o.raw_url, o.publication_date,
                   o.content_sha256, o.occurrence_json,
                   d.filename, d.period_start, d.period_end, d.imported_at
            FROM pdf_intake_article_occurrences o
            JOIN pdf_intake_articles p ON p.article_id=o.article_id
            JOIN pdf_intake_documents d ON d.document_sha256=o.source_document_sha256
            ORDER BY o.source_document_sha256, o.occurrence_id
            """
        ).fetchall() if has_pdf else []
        for row in pdf_rows:
            if pdf_occurrence_ids is not None and row["occurrence_id"] not in pdf_occurrence_ids:
                continue
            article_id = row["core_article_id"]
            if isinstance(article_id, str):
                all_identities.add(article_id)
            raw = _json_object(row["occurrence_json"], "PDF occurrence") or {}
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
            }
            pdf_observations.append(observation)
            confirmed_link = (
                row["confirmation_basis"] == "exact_url_eligible_detail"
                and isinstance(article_id, str)
            )
            publication_date = _day_precision_publication_date(row["publication_date"])
            if confirmed_link and publication_date and evidence:
                evidenced_dates[article_id].append({
                    "date": publication_date,
                    "observation_id": row["occurrence_id"],
                    "evidence": evidence,
                })
            formal_link = (
                confirmed_link and article_id in article_rows
            )
            if not formal_link:
                continue
            observations[article_id].append(observation)
        for article_id, values in (additional_evidenced_dates or {}).items():
            if article_id in article_rows:
                known = {
                    (item["date"], item["observation_id"])
                    for item in evidenced_dates[article_id]
                }
                evidenced_dates[article_id].extend(
                    item for item in values
                    if (item["date"], item["observation_id"]) not in known
                )
        selected_ids = {
            article_id
            for article_id, values in evidenced_dates.items()
            if article_id in article_rows
            and (acquisition_item_ids is None or observations[article_id])
            and any(start <= date.fromisoformat(item["date"]) <= end for item in values)
        }
        articles = []
        for article_id in sorted(selected_ids):
            base = article_rows[article_id]
            exact_dates = sorted(
                (item for item in evidenced_dates[article_id]
                 if start <= date.fromisoformat(item["date"]) <= end),
                key=lambda item: (item["date"], item["observation_id"]),
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
                              generated_at
                       FROM article_enrichments
                       WHERE content_version_id=? AND status='complete'
                       ORDER BY generated_at DESC, enrichment_id DESC LIMIT 1""",
                    (version_id,),
                ).fetchone()
            if enrichment:
                summary = enrichment["summary"]
                categories = _json_list(enrichment["categories_json"], "enrichment categories")
                keywords = _json_list(enrichment["keywords_json"], "enrichment keywords")
                semantic_provenance = {
                    "basis": "article_enrichment", "enrichment_id": enrichment["enrichment_id"],
                    "content_version_id": version_id,
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
                    content_text = content["markdown_content"]
                elif policy == "summary_excerpt":
                    content_text = " ".join(content["markdown_content"].split())[:500]
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
            articles.append({
                "article_id": article_id,
                "canonical_url": base["canonical_url"],
                "title": title,
                "publisher": base.get("publisher"),
                "publication_date": exact_dates[0]["date"],
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
                        "selected": exact_dates[0],
                        "all_in_range": exact_dates,
                    },
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
            if (
                (formal_link and article_id in selected_ids)
                or (confirmed_link and evidenced_dates[article_id])
                or (
                    _day_precision_publication_date(item["publication_date"])
                    and item["publication_date_evidence"]
                )
            ):
                continue
            period_start = _day_precision_publication_date(item["period_start"])
            period_end = _day_precision_publication_date(item["period_end"])
            if period_start is None or period_end is None or period_start > period_end:
                pdf_exclusions["unknown_coverage"] += 1
                continue
            if date.fromisoformat(period_end) < start or date.fromisoformat(period_start) > end:
                pdf_exclusions["non_overlapping_coverage"] += 1
                continue
            pdf_source_updates.append({
                **item,
                "publication_date_label": "文章发布日期未确认",
                "coverage_period": {"start": period_start, "end": period_end},
                "citations": [{
                    "kind": "pdf_page",
                    "document_sha256": item["document_sha256"],
                    "filename": item["filename"],
                    "page": item["page"],
                    "url": item["url"],
                }],
            })

    unknown_ids = sorted(article_id for article_id in all_identities if not evidenced_dates[article_id])
    return {
        "articles": articles,
        "pdf_source_updates": pdf_source_updates,
        "pdf_source_exclusion_counts": pdf_exclusions,
        "unknown_publication_date_count": len(unknown_ids),
        "unknown_publication_date_article_ids": unknown_ids,
    }


def _meeting_status(coverage: dict[str, Any], records: list[Any]) -> str:
    if coverage.get("status") in {"succeeded", "succeeded_empty", "complete"}:
        return "included" if records else "empty"
    if coverage.get("status") == "partial":
        return "partial"
    return "unavailable"


def _default_meeting_query_filters() -> dict[str, Any]:
    return {
        "organizer": None, "event_types": sorted(EVENT_TYPES), "start_date": None,
        "end_date": None, "include_unknown": False, "include_deadlines": False,
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
            queried = query_events(reader.database, base_date=base_date, timezone_name=TIMEZONE)
        except (OSError, ValueError, sqlite3.Error) as exc:
            frozen_query = {
                "schema_version": "climate-meeting-query.v1", "base_date": base_date,
                "timezone": TIMEZONE, "filters": _default_meeting_query_filters(),
                "coverage": {"status": "unavailable", "error": type(exc).__name__}, "records": [],
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
    except (KeyError, OSError, ValueError):
        return {"status": "unavailable", "snapshot_id": snapshot_id, "snapshot_sha256": None, "records": []}
    coverage = snapshot.get("coverage") or {}
    records = snapshot.get("records") or []
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
    value, precision = item.get("end_date") or item.get("start_date"), item.get("date_precision")
    if not isinstance(value, str):
        return None
    try:
        if precision == "day":
            return date.fromisoformat(value)
        if precision == "month":
            year, month = map(int, value.split("-"))
            return date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
        if precision == "quarter":
            year, quarter = int(value[:4]), int(value[-1])
            month = quarter * 3
            return date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
        if precision == "year":
            return date(int(value), 12, 31)
    except ValueError:
        pass
    return None


def _pdf_calendar_available(reader: RegistryReader) -> bool:
    with reader.connect() as connection:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pdf_intake_calendar_items'"
        ).fetchone() is not None


def _pdf_calendar_payload(
    reader: RegistryReader,
    *,
    base_date: str,
    activated_pdf_occurrence_ids: set[str] | None = None,
) -> dict[str, Any]:
    try:
        if not _pdf_calendar_available(reader):
            return {
                "status": "unavailable", "coverage": {"status": "unavailable", "error": "pdf_calendar_unavailable"},
                "base_date": base_date, "records": [],
            }
        allowed_documents: set[str] | None = None
        if activated_pdf_occurrence_ids is not None:
            with reader.connect() as connection:
                allowed_documents = {
                    str(row["source_document_sha256"])
                    for row in connection.execute(
                        "SELECT occurrence_id, source_document_sha256 FROM pdf_intake_article_occurrences"
                    )
                    if row["occurrence_id"] in activated_pdf_occurrence_ids
                }
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            result = reader.pdf_calendar_items(page=page, page_size=100)
            batch = result["items"]
            items.extend(
                item for item in batch
                if allowed_documents is None
                or item.get("source_document_sha256") in allowed_documents
            )
            if page >= result["pagination"]["pages"]:
                break
            page += 1
    except (OSError, RegistryError, ValueError, KeyError, sqlite3.Error) as exc:
        return {
            "status": "unavailable", "coverage": {"status": "unavailable", "error": type(exc).__name__},
            "base_date": base_date, "records": [],
        }
    base = date.fromisoformat(base_date)
    records = [item for item in items if (end := _calendar_end_date(item)) is not None and end >= base]
    return {
        "status": "included" if records else "empty", "coverage": {"status": "complete"},
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
    return {
        "articles": sorted(articles.values(), key=lambda item: (item["publication_date"], item["article_id"])),
        "pdf_source_updates": list(updates.values()),
        "pdf_source_exclusion_counts": {
            key: base["pdf_source_exclusion_counts"].get(key, 0)
            + overlay["pdf_source_exclusion_counts"].get(key, 0)
            for key in {"non_overlapping_coverage", "unknown_coverage"}
        },
        "unknown_publication_date_count": len(unknown),
        "unknown_publication_date_article_ids": sorted(unknown),
    }


def _merge_pdf_calendars(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    if overlay["status"] == "unavailable":
        return base
    if base["status"] == "unavailable":
        return overlay
    records: dict[str, dict[str, Any]] = {}
    for item in [*base["records"], *overlay["records"]]:
        identity = str(item.get("occurrence_id") or item.get("event_id") or _digest(item))
        records[identity] = item
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
        selected("pdf", bool(manifest["pdf_occurrence_ids"])),
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
        return {
            item["article_id"]: list(
                item["provenance"]["publication_date"]["all_in_range"]
            )
            for item in (source or {}).get("articles", [])
        }

    pdf_overlay = None
    if pdf_overlay_reader is not None and activated_pdf_ids:
        pdf_overlay = _range_source(
            pdf_overlay_reader,
            start_date,
            end_date,
            acquisition_item_ids=set(),
            pdf_occurrence_ids=activated_pdf_ids,
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
        pdf_calendar = _pdf_calendar_payload(snapshot_reader, base_date=base_date)
    if web_overlay is not None:
        source = _merge_range_sources(source, web_overlay)
    if pdf_overlay is not None:
        source = _merge_range_sources(
            source,
            pdf_overlay,
            preferred_acquisition_item_ids=activated_web_ids,
        )
    if pdf_overlay_reader is not None and activated_pdf_ids:
        overlay_calendar = _pdf_calendar_payload(
            pdf_overlay_reader,
            base_date=base_date,
            activated_pdf_occurrence_ids=activated_pdf_ids,
        )
        pdf_calendar = _merge_pdf_calendars(pdf_calendar, overlay_calendar)
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
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise RangeReportError("invalid range report snapshot")
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
        or value.get("unknown_publication_date_count") != len(unknown_ids)
    ):
        raise RangeReportError("invalid range report schema")
    seen_ids: set[str] = set()
    for article in articles:
        if not isinstance(article, dict):
            raise RangeReportError("invalid range report schema")
        article_id = article.get("article_id")
        publication_date = article.get("publication_date")
        try:
            published = date.fromisoformat(publication_date)
        except (TypeError, ValueError) as exc:
            raise RangeReportError("invalid range report schema") from exc
        if (
            not isinstance(article_id, str)
            or not article_id
            or article_id in seen_ids
            or not start <= published <= end
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
        if (
            item.get("publication_date_label") != "文章发布日期未确认"
            or period_start > period_end
            or period_end < start
            or period_start > end
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
    }):
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


def pdf_path(artifact_root: str | Path, snapshot_id: str) -> Path:
    return Path(artifact_root).resolve(strict=False) / snapshot_id / f"{snapshot_id}-{RENDERER_VERSION}.pdf"


def ensure_range_report_pdf(snapshot: dict[str, Any], artifact_root: str | Path) -> Path:
    """Render a missing current-version PDF from the saved snapshot only."""
    target = pdf_path(artifact_root, snapshot["snapshot_id"])
    if not target.is_file():
        render_range_report_pdf(snapshot, target)
    return target


def _freeze_executive_summary(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Copy selected stored summaries and their locators into the digest-checked snapshot."""
    points = []
    for article in source["articles"]:
        summary = article.get("summary")
        if isinstance(summary, str) and summary.strip():
            points.append({
                "kind": "registry_article", "text": summary,
                "article_id": article["article_id"], "title": article["title"],
                "publication_date": article["publication_date"], "citations": article["citations"],
            })
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


def _executive_summary(snapshot: dict[str, Any]) -> list[str]:
    articles = snapshot["articles"]
    pdf_updates = snapshot.get("pdf_source_updates", [])
    exclusions = snapshot.get("pdf_source_exclusion_counts", {})
    start, end = snapshot["date_range"]["start"], snapshot["date_range"]["end"]
    summary = [f"{len(articles)} evidenced Registry article(s) were published from {start} through {end}."]
    points = snapshot.get("executive_summary")
    if points is None:  # Legacy snapshots predate frozen executive-summary points.
        points = _freeze_executive_summary(snapshot)
    article_points = [point for point in points if point["kind"] == "registry_article"]
    pdf_points = [point for point in points if point["kind"] == "pdf_source"]
    if not articles and not pdf_updates:
        summary.append("No selected Registry articles or PDF source updates matched this range.")
    if article_points:
        summary.extend(
            f"{point['title']}: {point['text']} (Article ID: {point['article_id']}; "
            f"publication date {point['publication_date']})." for point in article_points
        )
    elif articles:
        summary.append("No stored Registry article summaries are available for the selected range.")
    if "pdf_source_updates" in snapshot:
        summary.append(
            f"{len(pdf_updates)} PDF source update(s) overlap the range; their article publication dates are unconfirmed."
        )
        if pdf_points:
            summary.extend(
                f"{point['text']} ({point['filename']}, page {point['page']}, "
                f"{point['document_sha256']}; article publication date unconfirmed; "
                f"coverage period {point['coverage_period']['start']} through "
                f"{point['coverage_period']['end']})."
                for point in pdf_points
            )
        elif pdf_updates:
            summary.append("No stored PDF source summaries are available for the selected range.")
        summary.append(
            f"PDF source observations excluded: {exclusions.get('unknown_coverage', 0)} with unknown coverage; "
            f"{exclusions.get('non_overlapping_coverage', 0)} with non-overlapping coverage."
        )
    if snapshot["unknown_publication_date_count"]:
        summary.append(
            f"{snapshot['unknown_publication_date_count']} Registry article(s) with unknown publication dates were excluded."
        )
    return summary


def _meeting_metadata(meeting: dict[str, Any]) -> list[tuple[str, Any]]:
    source = meeting.get("source") or ("snapshot" if meeting.get("snapshot_id") else "unavailable")
    metadata = [("Meeting source", source)]
    if meeting.get("snapshot_id"):
        metadata.append(("Meeting snapshot ID", meeting["snapshot_id"]))
    elif meeting.get("query_id"):
        metadata.extend([
            ("Meeting query ID", meeting["query_id"]),
            ("Meeting query SHA-256", meeting.get("query_sha256") or "unavailable"),
        ])
    metadata.extend([
        ("Meeting query base date", meeting.get("base_date") or "unavailable"),
        ("Meeting query timezone", meeting.get("timezone") or "unavailable"),
        ("Meeting coverage", (meeting.get("coverage") or {}).get("status", "unavailable")),
    ])
    return metadata


def _articles_by_publisher_topic(snapshot: dict[str, Any]) -> dict[str, dict[str, list[tuple[int, dict[str, Any]]]]]:
    grouped: dict[str, dict[str, list[tuple[int, dict[str, Any]]]]] = {}
    for index, item in enumerate(snapshot["articles"], start=1):
        publisher = item.get("publisher") or "Publisher not recorded"
        categories = [category for category in item.get("categories", []) if category.strip()]
        topic = ", ".join(categories) or "Topic not recorded"
        grouped.setdefault(publisher, {}).setdefault(topic, []).append((index, item))
    return grouped


def render_range_report_html(snapshot: dict[str, Any]) -> str:
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
        f"Renderer <code>{RENDERER_VERSION}</code></p>",
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
        for record in meeting["records"]:
            blocks.append(
                f"<p><strong>{esc(record.get('name', 'Meeting'))}</strong>: "
                f"{esc(record.get('start_date') or record.get('raw_time_text') or 'date unavailable')}</p>"
            )
    if pdf_calendar is not None:
        blocks.append(f"<p>PDF calendar status: {esc(pdf_calendar['status'])}. Coverage: {esc(pdf_calendar['coverage'].get('status', 'unavailable'))}.</p>")
        records = pdf_calendar["records"]
        for heading, kinds in (("PDF Calendar Dates", {"event"}), ("PDF Deadlines", {"deadline"}), ("Other PDF Key Dates", None)):
            selected = [item for item in records if (item.get("kind") in kinds if kinds else item.get("kind") not in {"event", "deadline"})]
            if selected:
                blocks.append(f"<h3>{heading}</h3><ul>")
                for item in selected:
                    blocks.append(
                        f"<li><strong>{esc(item.get('name') or 'PDF key date')}</strong>: {esc(item.get('raw_date') or item.get('end_date'))} "
                        f"({esc(item.get('source_filename'))}, page {esc(item.get('page'))}, <code>{esc(item.get('source_document_sha256'))}</code>)</li>"
                    )
                blocks.append("</ul>")
    blocks.append('<h2 id="updates">Updates by Publisher / Institution</h2>')
    for group_index, (publisher, topics) in enumerate(grouped_articles.items(), start=1):
        blocks.append(f'<h3 id="publisher-{group_index}">{esc(publisher)}</h3>')
        for topic_index, (topic, items) in enumerate(topics.items(), start=1):
            blocks.append(f'<h4 id="publisher-{group_index}-topic-{topic_index}">{esc(topic)}</h4>')
            for index, item in items:
                blocks.extend([
                    f'<article id="article-{index}"><h5>{index}. {esc(item["title"])}</h5>',
                    f"<p><strong>Publication date:</strong> {esc(item['publication_date'])}<br>"
                    f"<strong>Article ID:</strong> <code>{esc(item['article_id'])}</code><br>"
                    f"<strong>Content version:</strong> <code>{esc(item['content_version_id'] or 'none')}</code></p>",
                ])
                if item.get("summary"):
                    blocks.append(f"<p>{esc(item['summary'])}</p>")
                if item.get("content"):
                    blocks.append("".join(f"<p>{esc(part)}</p>" for part in item["content"].split("\n\n") if part.strip()))
                if item["categories"]:
                    blocks.append(f"<p><strong>Categories:</strong> {esc(', '.join(item['categories']))}</p>")
                if item["keywords"]:
                    blocks.append(f"<p><strong>Keywords:</strong> {esc(', '.join(item['keywords']))}</p>")
                blocks.append("<h6>Sources</h6><ul>")
                for citation in item["citations"]:
                    if citation["kind"] == "url":
                        url = esc(citation["url"])
                        blocks.append(f'<li><a href="{url}" rel="noopener noreferrer">{url}</a></li>')
                    else:
                        label = f"{citation['filename']}, page {citation['page']}"
                        if citation.get("url"):
                            url = esc(citation["url"])
                            blocks.append(f'<li>{esc(label)} — <a href="{url}" rel="noopener noreferrer">{url}</a></li>')
                        else:
                            blocks.append(f"<li>{esc(label)}</li>")
                blocks.append("</ul></article>")
    pdf_updates = snapshot.get("pdf_source_updates", [])
    if pdf_updates:
        blocks.append("<h2>PDF 来源更新 / PDF Source Updates</h2>")
    for index, item in enumerate(pdf_updates, start=1):
        coverage = item["coverage_period"]
        blocks.extend([
            f'<article id="pdf-update-{index}"><h2>{index}. {esc(item["title"])}</h2>',
            f"<p><strong>{esc(item['publication_date_label'])}</strong><br>"
            f"<strong>PDF coverage period:</strong> {esc(coverage['start'])} through {esc(coverage['end'])}<br>"
            f"<strong>File:</strong> {esc(item['filename'])}, page {esc(item['page'])}<br>"
            f"<strong>SHA-256:</strong> <code>{esc(item['document_sha256'])}</code>",
        ])
        if item.get("core_article_id"):
            blocks.append(
                f"<br><strong>Core article ID:</strong> <code>{esc(item['core_article_id'])}</code>"
            )
        blocks.append("</p>")
        if item.get("summary"):
            blocks.append(f"<p>{esc(item['summary'])}</p>")
        url = esc(item["url"])
        blocks.append(
            f'<h3>Source</h3><p><a href="{url}" rel="noopener noreferrer">{url}</a></p></article>'
        )
    blocks.append("</body></html>")
    return "".join(blocks)


def _fonts() -> tuple[str, str]:
    candidates = [
        (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
         Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
        (Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
         Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")),
        (Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
         Path("/usr/share/fonts/truetype/freefont/FreeSansBold.ttf")),
        (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/arialbd.ttf")),
    ]
    for regular, bold in candidates:
        if regular.is_file() and bold.is_file():
            if "ClimateRangeRegular" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("ClimateRangeRegular", regular))
                pdfmetrics.registerFont(TTFont("ClimateRangeBold", bold))
                pdfmetrics.registerFontFamily(
                    "ClimateRangeRegular",
                    normal="ClimateRangeRegular",
                    bold="ClimateRangeBold",
                )
            return "ClimateRangeRegular", "ClimateRangeBold"
    return "Helvetica", "Helvetica-Bold"


class _RangeDocTemplate(BaseDocTemplate):
    def afterFlowable(self, flowable):
        if not isinstance(flowable, Paragraph) or flowable.style.name not in {"RangeH1", "RangeH2", "RangeH3"}:
            return
        level = {"RangeH1": 0, "RangeH2": 1, "RangeH3": 2}[flowable.style.name]
        text = flowable.getPlainText()
        key = f"heading-{self.seq.nextf('heading')}"
        self.canv.bookmarkPage(key)
        self.canv.addOutlineEntry(text, key, level=level, closed=False)
        self.notify("TOCEntry", (level, text, self.page, key))


def render_range_report_pdf(snapshot: dict[str, Any], output: str | Path) -> None:
    regular, bold = _fonts()
    base = getSampleStyleSheet()
    body = ParagraphStyle(
        "RangeBody", parent=base["BodyText"], fontName=regular, fontSize=9,
        leading=13, textColor=colors.HexColor("#33424d"), splitLongWords=True,
        spaceAfter=5,
    )
    h1 = ParagraphStyle(
        "RangeH1", parent=base["Heading1"], fontName=bold, fontSize=16,
        leading=20, textColor=colors.HexColor("#0b3d62"), spaceAfter=8,
    )
    h2 = ParagraphStyle(
        "RangeH2", parent=base["Heading2"], fontName=bold, fontSize=12,
        leading=16, textColor=colors.HexColor("#1f6f8b"), spaceBefore=6, spaceAfter=5,
    )
    h3 = ParagraphStyle(
        "RangeH3", parent=base["Heading3"], fontName=bold, fontSize=10,
        leading=13, textColor=colors.HexColor("#0b3d62"), spaceBefore=4, spaceAfter=3,
    )
    detail_label = ParagraphStyle("RangeLabel", parent=body, fontName=bold, spaceBefore=4, spaceAfter=3)
    esc = lambda value: html.escape(str(value), quote=True)
    story: list[Any] = []
    start, end = snapshot["date_range"]["start"], snapshot["date_range"]["end"]
    story.extend([
        Paragraph("CLIMATE REGISTRY RANGE REPORT", h1),
        Paragraph(f"{esc(start)} through {esc(end)} (inclusive, {TIMEZONE})", h2),
        Paragraph(f"Snapshot: {esc(snapshot['snapshot_id'])}", body),
        Paragraph(f"Renderer: {RENDERER_VERSION}", body),
        Spacer(1, 8 * mm), PageBreak(), Paragraph("Contents", h1),
    ])
    toc = TableOfContents()
    toc.levelStyles = [
        ParagraphStyle("TOC1", fontName=regular, fontSize=9, leading=13, leftIndent=0),
        ParagraphStyle("TOC2", fontName=regular, fontSize=8, leading=11, leftIndent=12),
        ParagraphStyle("TOC3", fontName=regular, fontSize=8, leading=10, leftIndent=24),
    ]
    story.extend([toc, PageBreak(), Paragraph("Executive Summary", h1)])
    story.extend(Paragraph(esc(line), body) for line in _executive_summary(snapshot))
    meeting = snapshot["meeting"]
    pdf_calendar = snapshot.get("pdf_calendar")
    story.append(Paragraph("Key Dates", h1))
    if meeting["status"] == "not_requested" and pdf_calendar is None:
        story.append(Paragraph("Key dates were not captured in this snapshot.", body))
    if meeting["status"] != "not_requested":
        label = "Meeting query" if meeting.get("source") == "query" else "Meeting snapshot"
        metadata = "<br/>".join(
            f"<b>{esc(key)}:</b> {esc(value)}" for key, value in _meeting_metadata(meeting)
        )
        story.append(Paragraph(f"{label} status: {esc(meeting['status'])}.<br/>{metadata}", body))
        if meeting["status"] == "empty":
            story.append(Paragraph("No future meetings.", body))
        for record in meeting["records"]:
            story.append(Paragraph(
                f"<b>{esc(record.get('name', 'Meeting'))}</b>: "
                f"{esc(record.get('start_date') or record.get('raw_time_text') or 'date unavailable')}", body
            ))
    if pdf_calendar is not None:
        story.append(Paragraph(
            f"PDF calendar status: {esc(pdf_calendar['status'])}. Coverage: {esc(pdf_calendar['coverage'].get('status', 'unavailable'))}.", body
        ))
        for heading, kinds in (("PDF Calendar Dates", {"event"}), ("PDF Deadlines", {"deadline"}), ("Other PDF Key Dates", None)):
            selected = [item for item in pdf_calendar["records"] if (item.get("kind") in kinds if kinds else item.get("kind") not in {"event", "deadline"})]
            if selected:
                story.append(Paragraph(heading, h2))
                for item in selected:
                    story.append(Paragraph(
                        f"<b>{esc(item.get('name') or 'PDF key date')}</b>: {esc(item.get('raw_date') or item.get('end_date'))}<br/>"
                        f"File: {esc(item.get('source_filename'))}, page {esc(item.get('page'))}<br/>"
                        f"SHA-256: {esc(item.get('source_document_sha256'))}", body
                    ))
    story.append(Paragraph("Updates by Publisher / Institution", h1))
    for publisher, topics in _articles_by_publisher_topic(snapshot).items():
        story.append(Paragraph(esc(publisher), h2))
        for topic, items in topics.items():
            story.append(Paragraph(esc(topic), h3))
            for index, item in items:
                story.append(Paragraph(f"{index}. {esc(item['title'])}", detail_label))
                story.append(Paragraph(
                    f"Publication date: {esc(item['publication_date'])}<br/>"
                    f"Article ID: {esc(item['article_id'])}<br/>"
                    f"Content version: {esc(item['content_version_id'] or 'none')}", body
                ))
                if item.get("summary"):
                    story.append(Paragraph(esc(item["summary"]), body))
                if item.get("content"):
                    for part in item["content"].split("\n\n"):
                        if part.strip():
                            story.append(Paragraph(esc(part), body))
                if item["categories"]:
                    story.append(Paragraph("Categories: " + esc(", ".join(item["categories"])), body))
                if item["keywords"]:
                    story.append(Paragraph("Keywords: " + esc(", ".join(item["keywords"])), body))
                story.append(Paragraph("Sources", detail_label))
                for citation in item["citations"]:
                    if citation["kind"] == "url":
                        url = esc(citation["url"])
                        line = f'<link href="{url}" color="#1a73e8">{url}</link>'
                    else:
                        line = esc(f"{citation['filename']}, page {citation['page']}")
                        if citation.get("url"):
                            url = esc(citation["url"])
                            line += f' — <link href="{url}" color="#1a73e8">{url}</link>'
                    story.append(Paragraph(line, body))

    pdf_updates = snapshot.get("pdf_source_updates", [])
    if pdf_updates:
        story.append(Paragraph("PDF Source Updates", h1))
    for index, item in enumerate(pdf_updates, start=1):
        coverage = item["coverage_period"]
        story.append(Paragraph(f"{index}. {esc(item['title'])}", h2))
        metadata = (
            "Article publication date unconfirmed<br/>"
            f"PDF coverage period: {esc(coverage['start'])} through {esc(coverage['end'])}<br/>"
            f"File: {esc(item['filename'])}, page {esc(item['page'])}<br/>"
            f"SHA-256: {esc(item['document_sha256'])}"
        )
        if item.get("core_article_id"):
            metadata += f"<br/>Core article ID: {esc(item['core_article_id'])}"
        story.append(Paragraph(metadata, body))
        if item.get("summary"):
            story.append(Paragraph(esc(item["summary"]), body))
        url = esc(item["url"])
        story.extend([
            Paragraph("Source", detail_label),
            Paragraph(f'<link href="{url}" color="#1a73e8">{url}</link>', body),
        ])

    output = Path(output)
    buffer = io.BytesIO()
    document = _RangeDocTemplate(
        buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=20 * mm,
        title=f"Climate Registry report {start} to {end}",
        author="Climate Monitor Wiki",
    )
    frame = Frame(document.leftMargin, document.bottomMargin, document.width, document.height, id="report")

    def page_footer(pdf_canvas, doc):
        pdf_canvas.saveState()
        pdf_canvas.setFont(regular, 8)
        pdf_canvas.drawString(18 * mm, 10 * mm, f"{snapshot['snapshot_id']} · {RENDERER_VERSION}")
        pdf_canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        pdf_canvas.restoreState()

    document.addPageTemplates(PageTemplate(id="report", frames=frame, onPage=page_footer))
    document.multiBuild(story)
    atomic_write_bytes(output, buffer.getvalue())
