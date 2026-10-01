from __future__ import annotations

import hashlib
import html
import io
import json
import logging
import os
import re
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
from climate_monitor.meetings import load_snapshot as load_meeting_snapshot

from .read_api import RegistryContractError, RegistryReader


SCHEMA_VERSION = "climate-range-report-snapshot.v1"
RENDERER_VERSION = "range-report-v1"
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


def _range_source(reader: RegistryReader, start_date: str, end_date: str) -> dict[str, Any]:
    """Freeze persisted Registry evidence without consulting reports or the web."""
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    article_rows: dict[str, dict[str, Any]] = {}
    evidenced_dates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_identities: set[str] = set()

    with reader.connect() as connection:
        for row in connection.execute(
            """SELECT a.article_id, a.canonical_url, a.current_version_id,
                      a.current_content_version_id, a.display_policy,
                      s.display_name AS publisher, av.observed_title AS current_title
               FROM articles a
               JOIN sources s ON s.source_id=a.source_id
               LEFT JOIN article_versions av ON av.version_id=a.current_version_id
               WHERE a.publication_eligible=1 AND a.document_kind='article'
               ORDER BY a.article_id"""
        ):
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
            WHERE a.publication_eligible=1 AND a.document_kind='article'
            ORDER BY i.article_id, i.discovered_at, i.acquisition_item_id
            """
        ).fetchall() if RegistryReader._has_acquisition_projection(connection) else []
        for row in acquisition_rows:
            article_id = row["article_id"]
            all_identities.add(article_id)
            origins = _json_list(row["origins_json"], "acquisition origins")
            if any(not isinstance(item, dict) for item in origins):
                raise RegistryContractError("invalid acquisition origins")
            evidence = _json_object(row["publication_date_evidence_json"], "publication evidence")
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
            publication_date = _day_precision_publication_date(row["publication_date"])
            if publication_date and evidence:
                evidenced_dates[article_id].append({
                    "date": publication_date,
                    "observation_id": row["acquisition_item_id"],
                    "evidence": evidence,
                })
            article_rows.setdefault(article_id, dict(row))

        has_pdf = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pdf_intake_articles'"
        ).fetchone() is not None
        pdf_rows = connection.execute(
            """
            SELECT o.occurrence_id, o.article_id AS pdf_article_id,
                   p.core_article_id AS article_id,
                   p.canonical_url, p.title, p.core_article_id, o.source_document_sha256,
                   o.page, o.raw_url, o.publication_date, o.occurrence_json,
                   d.filename, d.period_start, d.period_end, d.imported_at
            FROM pdf_intake_article_occurrences o
            JOIN pdf_intake_articles p ON p.article_id=o.article_id
            JOIN articles core ON core.article_id=p.core_article_id
            JOIN pdf_intake_documents d ON d.document_sha256=o.source_document_sha256
            WHERE p.core_article_id IS NOT NULL
              AND p.confirmation_basis='exact_url_eligible_detail'
              AND core.document_kind='article'
              AND core.publication_eligible=1
            ORDER BY article_id, o.occurrence_id
            """
        ).fetchall() if has_pdf else []
        for row in pdf_rows:
            article_id = row["article_id"]
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
            observation = {
                "kind": "registry_pdf",
                "observation_id": row["occurrence_id"],
                "pdf_article_id": row["pdf_article_id"],
                "document_sha256": row["source_document_sha256"],
                "batch_id": raw.get("management_batch_id"),
                "filename": row["filename"],
                "period_start": row["period_start"],
                "period_end": row["period_end"],
                "page": row["page"],
                "url": row["raw_url"],
                "title": raw.get("anchor_text") or row["title"],
                "summary": raw.get("summary"),
                "observed_at": row["imported_at"],
                "publication_date": row["publication_date"],
                "publication_date_evidence": evidence,
                "source_observations": source_rows,
            }
            observations[article_id].append(observation)
            publication_date = _day_precision_publication_date(row["publication_date"])
            if publication_date and evidence:
                evidenced_dates[article_id].append({
                    "date": publication_date,
                    "observation_id": row["occurrence_id"],
                    "evidence": evidence,
                })
        selected_ids = {
            article_id
            for article_id, values in evidenced_dates.items()
            if any(start <= date.fromisoformat(item["date"]) <= end for item in values)
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
            latest_source = max(source_items, key=lambda item: (
                item.get("observed_at") or "", item["observation_id"]
            ))
            content = None
            enrichment = None
            version_id = base.get("current_content_version_id")
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
                summary = latest_source.get("summary")
                categories, keywords = [], []
                semantic_provenance = {
                    "basis": latest_source["kind"],
                    "observation_id": latest_source["observation_id"],
                }
            content_text = None
            if content is not None:
                policy = base.get("display_policy")
                if policy == "full_markdown":
                    content_text = content["markdown_content"]
                elif policy == "summary_excerpt":
                    content_text = " ".join(content["markdown_content"].split())[:500]
            title = base.get("current_title") or latest_source.get("title") or base["canonical_url"]
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
                    "title": {
                        "basis": "current_article_version" if base.get("current_title") else latest_source["kind"],
                        "version_id": base.get("current_version_id"),
                    },
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

    unknown_ids = sorted(article_id for article_id in all_identities if not evidenced_dates[article_id])
    return {
        "articles": articles,
        "unknown_publication_date_count": len(unknown_ids),
        "unknown_publication_date_article_ids": unknown_ids,
    }


def _meeting_payload(reader: RegistryReader, snapshot_id: str | None) -> dict[str, Any]:
    if snapshot_id is None:
        return {"status": "not_requested", "snapshot_id": None, "snapshot_sha256": None, "records": []}
    try:
        snapshot = load_meeting_snapshot(reader.database, snapshot_id)
    except (KeyError, OSError, ValueError):
        return {"status": "unavailable", "snapshot_id": snapshot_id, "snapshot_sha256": None, "records": []}
    coverage = snapshot.get("coverage") or {}
    records = snapshot.get("records") or []
    coverage_status = coverage.get("status")
    if coverage_status in {"failed", "partial"}:
        status = "failed"
    elif coverage_status in {"succeeded", "succeeded_empty"}:
        status = "included" if records else "empty"
    else:
        status = "unavailable"
    return {
        "status": status,
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "coverage": coverage,
        "records": records,
    }


def freeze_range_report(
    reader: RegistryReader,
    artifact_root: str | Path,
    *,
    start_date: str,
    end_date: str,
    meeting_snapshot_id: str | None = None,
) -> dict[str, Any]:
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end or (end - start).days + 1 > MAX_RANGE_DAYS:
        raise RangeReportError("invalid report date range")
    source = _range_source(reader, start_date, end_date)
    meeting = _meeting_payload(reader, meeting_snapshot_id)
    frozen = {
        "schema_version": SCHEMA_VERSION,
        "date_range": {"start": start_date, "end": end_date, "inclusive": True},
        "timezone": TIMEZONE,
        "articles": source["articles"],
        "unknown_publication_date_count": source["unknown_publication_date_count"],
        "unknown_publication_date_article_ids": source["unknown_publication_date_article_ids"],
        "meeting": meeting,
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
                "created_at": datetime.now(timezone.utc).isoformat(),
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
    meeting = value.get("meeting")
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
    if meeting.get("status") not in {"not_requested", "included", "empty", "failed", "unavailable"}:
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


def _executive_summary(snapshot: dict[str, Any]) -> list[str]:
    articles = snapshot["articles"]
    start, end = snapshot["date_range"]["start"], snapshot["date_range"]["end"]
    summary = [f"{len(articles)} evidenced Registry article(s) were published from {start} through {end}."]
    if snapshot["unknown_publication_date_count"]:
        summary.append(
            f"{snapshot['unknown_publication_date_count']} Registry article(s) with unknown publication dates were excluded."
        )
    if articles:
        titles = "; ".join(item["title"] for item in articles[:3])
        summary.append(f"The frozen range includes: {titles}.")
    return summary


def render_range_report_html(snapshot: dict[str, Any]) -> str:
    esc = lambda value: html.escape(str(value), quote=True)
    start, end = snapshot["date_range"]["start"], snapshot["date_range"]["end"]
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
        "<h2>Contents</h2><ol>" + "".join(
            f'<li><a href="#article-{index}">{esc(item["title"])}</a></li>'
            for index, item in enumerate(snapshot["articles"], start=1)
        ) + "</ol>",
        "<h2>Executive Summary</h2>" + "".join(
            f"<p>{esc(line)}</p>" for line in _executive_summary(snapshot)
        ),
    ]
    meeting = snapshot["meeting"]
    if meeting["status"] != "not_requested":
        blocks.append(f"<h2>Key Dates</h2><p>Meeting snapshot status: {esc(meeting['status'])}.</p>")
        for record in meeting["records"]:
            blocks.append(
                f"<p><strong>{esc(record.get('name', 'Meeting'))}</strong>: "
                f"{esc(record.get('start_date') or record.get('raw_time_text') or 'date unavailable')}</p>"
            )
    blocks.append("<h2>Articles</h2>")
    for index, item in enumerate(snapshot["articles"], start=1):
        blocks.extend([
            f'<article id="article-{index}"><h2>{index}. {esc(item["title"])}</h2>',
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
        blocks.append("<h3>Sources</h3><ul>")
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
        if not isinstance(flowable, Paragraph) or flowable.style.name not in {"RangeH1", "RangeH2"}:
            return
        level = 0 if flowable.style.name == "RangeH1" else 1
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
        wordWrap="CJK", spaceAfter=5,
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
        "RangeH3", parent=body, fontName=bold, fontSize=9,
        textColor=colors.HexColor("#0b3d62"), spaceBefore=4, spaceAfter=3,
    )
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
    ]
    story.extend([toc, PageBreak(), Paragraph("Executive Summary", h1)])
    story.extend(Paragraph(esc(line), body) for line in _executive_summary(snapshot))
    meeting = snapshot["meeting"]
    if meeting["status"] != "not_requested":
        story.append(Paragraph("Key Dates", h1))
        story.append(Paragraph(f"Meeting snapshot status: {esc(meeting['status'])}.", body))
        for record in meeting["records"]:
            story.append(Paragraph(
                f"<b>{esc(record.get('name', 'Meeting'))}</b>: "
                f"{esc(record.get('start_date') or record.get('raw_time_text') or 'date unavailable')}", body
            ))
    story.append(Paragraph("Articles", h1))
    for index, item in enumerate(snapshot["articles"], start=1):
        story.append(Paragraph(f"{index}. {esc(item['title'])}", h2))
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
        story.append(Paragraph("Sources", h3))
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
