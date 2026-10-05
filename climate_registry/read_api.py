from __future__ import annotations

import json
import logging
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit

from climate_delivery.errors import ClimateDeliveryError
from climate_monitor.dedupe import canonical_url

from .annotations import ArticleAnnotation, load_article_annotations
from .capture import article_body_markdown, article_preview
from .contract import SCHEMA_VERSION, SchemaContractError, validate_registry_contract
from .reports import ParsedArticle, ParsedReport, parse_historical_report

EXPECTED_SCHEMA_VERSION = SCHEMA_VERSION
MAX_PUBLISHER_CHOICES = 500
logger = logging.getLogger(__name__)


class RegistryError(RuntimeError):
    """Base class for safe, public registry failures."""


class RegistryUnavailableError(RegistryError):
    """The configured registry cannot currently be opened."""


class RegistryContractError(RegistryError):
    """The configured database does not satisfy the read API contract."""


class RegistryLocationError(RegistryContractError):
    """The configured path violates the external-database boundary."""


class RegistryNotFoundError(RegistryError):
    """The requested report or article does not exist."""


class RegistryQueryError(RegistryError):
    """A query or identifier is invalid."""


@dataclass(frozen=True)
class RegistryReportIdentity:
    report_date: str
    filename: str
    report_title: str
    report_sha256: str


def validate_page(page: int, page_size: int) -> tuple[int, int]:
    if (
        isinstance(page, bool)
        or isinstance(page_size, bool)
        or not 1 <= page <= 1_000_000
        or not 1 <= page_size <= 100
    ):
        raise RegistryQueryError("invalid pagination")
    return page, page_size


def validate_article_id(article_id: str) -> str:
    if not article_id or len(article_id) > 128 or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in article_id
    ):
        raise RegistryQueryError("invalid article id")
    return article_id


def validate_report_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise RegistryQueryError("invalid report date") from exc
    if parsed.isoformat() != value:
        raise RegistryQueryError("invalid report date")
    return value


def _json_string_list(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        return []
    return decoded


def _pagination(page: int, page_size: int, total: int) -> dict[str, int]:
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": math.ceil(total / page_size) if total else 0,
    }


def page_pdf_articles(
    items: list[dict[str, Any]], *, page: int, page_size: int, query: str = "",
    source: str = "", report_date: str = "",
) -> dict[str, Any]:
    page, page_size = validate_page(page, page_size)
    if len(query) > 200 or len(source) > 253:
        raise RegistryQueryError("filter is too long")
    if report_date:
        validate_report_date(report_date)
    selected = []
    for item in items:
        if source and item["source"] != source.strip().lower().removeprefix("www."):
            continue
        if report_date and not any(value.get("report_date") == report_date for value in item["occurrences"]):
            continue
        searchable = " ".join((item["title"] or "", item["summary"] or "", item["canonical_url"])).casefold()
        if query and query.casefold() not in searchable:
            continue
        latest = item["occurrences"][0] if item["occurrences"] else {}
        selected.append({key: value for key, value in item.items() if key != "occurrences"} | {
            "occurrence_count": len(item["occurrences"]), "latest_occurrence": latest,
        })
    selected.sort(key=lambda item: (item["last_seen"] or "", item["article_id"]), reverse=True)
    total = len(selected)
    offset = (page - 1) * page_size
    return {"items": selected[offset:offset + page_size], "pagination": _pagination(page, page_size, total)}


def page_pdf_calendar_items(
    items: list[dict[str, Any]], *, page: int, page_size: int, query: str = "", kind: str = "",
) -> dict[str, Any]:
    page, page_size = validate_page(page, page_size)
    if len(query) > 200 or len(kind) > 80:
        raise RegistryQueryError("filter is too long")
    selected = []
    for item in items:
        if kind and kind.casefold() not in str(item.get("kind", "")).casefold():
            continue
        searchable = " ".join((
            item.get("name") or "", item.get("kind") or "", item.get("raw_date") or "",
            item.get("summary") or "", " ".join(item.get("source_urls") or []),
        )).casefold()
        if query and query.casefold() not in searchable:
            continue
        selected.append(item)
    selected.sort(key=lambda item: (
        item.get("start_date") or "9999", item.get("name") or "", item.get("occurrence_id") or "",
    ))
    total = len(selected)
    offset = (page - 1) * page_size
    return {"items": selected[offset:offset + page_size], "pagination": _pagination(page, page_size, total)}


def _monitoring_status(row: sqlite3.Row) -> str:
    checked, succeeded, failed = row["sites_checked"], row["sites_succeeded"], row["sites_failed"]
    if checked is None or succeeded is None or failed is None:
        return "not_reported"
    if failed == 0 and succeeded == checked:
        return "complete"
    return "partial"


def _like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _ordered_unique(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if value and key not in seen:
            output.append(value)
            seen.add(key)
    return output


def _log_db_annotation_precedence(
    summary: str,
    categories: list[str],
    keywords: list[str],
    annotation: ArticleAnnotation | None,
) -> None:
    if annotation is None:
        return
    conflicts = [
        field
        for field, differs in (
            ("summary", summary != annotation.summary),
            ("categories", categories != list(annotation.categories)),
            ("keywords", keywords != list(annotation.keywords)),
        )
        if differs
    ]
    # Keep overlap diagnostics out of API payloads and avoid logging article
    # URLs, semantic values, metadata paths, or other source-identifying data.
    logger.debug(
        "Registry DB enrichment takes precedence over JSON annotation; "
        "overlap_fields=summary,categories,keywords conflict_fields=%s",
        ",".join(conflicts) or "none",
    )


def _publisher_label(hostname: str, display_name: str) -> str:
    host = hostname.casefold().removeprefix("www.")
    display = " ".join(display_name.split()).strip()
    if display and display.casefold().removeprefix("www.") != host:
        return display[:80]
    labels = host.split(".")
    if len(labels) == 1:
        return labels[0][:80]
    if labels[-1] == "gov" and len(labels) >= 3:
        agency = labels[-3].replace("-", " ")
        jurisdiction = labels[-2].upper()
        return f"{jurisdiction} {agency}"[:80]
    if len(labels[-1]) == 2 and len(labels) >= 3 and labels[-2] in {
        "ac", "co", "com", "edu", "gov", "net", "org",
    }:
        return labels[-3][:80]
    return labels[-2][:80]


class RegistryReader:
    def __init__(
        self,
        database: str | Path,
        *,
        repository_root: str | Path,
        source_dir: str | Path | None = None,
        metadata_dir: str | Path | None = None,
    ):
        configured = Path(database).expanduser()
        if not configured.is_absolute():
            raise RegistryLocationError("registry path must be absolute and external")
        try:
            root = Path(repository_root).resolve(strict=False)
            resolved = configured.resolve(strict=False)
        except OSError as exc:
            raise RegistryUnavailableError("registry database is unavailable") from exc
        try:
            resolved.relative_to(root)
        except ValueError:
            pass
        else:
            raise RegistryLocationError("registry must be outside the application repository")
        self.database = resolved
        self.source_dir = Path(source_dir).resolve(strict=False) if source_dir is not None else None
        self.metadata_dir = Path(metadata_dir).resolve(strict=False) if metadata_dir is not None else None

    def _source_report(
        self, report_date: str, filename: str, expected_sha256: str
    ) -> ParsedReport | None:
        if self.source_dir is None or filename != f"climate-monitor-{report_date}.md":
            return None
        path = self.source_dir / filename
        try:
            # Read-tolerant: an explicitly accepted off-cycle report is valid
            # persisted history; Monday-only policy lives at ingestion.
            report = parse_historical_report(path, allow_offcycle=True)
        except (ClimateDeliveryError, OSError, UnicodeError, ValueError):
            return None
        if report.report_date != report_date or report.sha256 != expected_sha256:
            return None
        return report

    @staticmethod
    def _source_article(
        report: ParsedReport | None, ordinal: int, url: str
    ) -> ParsedArticle | None:
        if report is None or ordinal < 1 or ordinal > len(report.articles):
            return None
        article = report.articles[ordinal - 1]
        try:
            matches = canonical_url(article.url) == canonical_url(url)
        except (TypeError, UnicodeError, ValueError):
            return None
        return article if matches else None

    @staticmethod
    def _annotation_for_url(
        annotations: dict[str, ArticleAnnotation], url: str
    ) -> ArticleAnnotation | None:
        try:
            return annotations.get(canonical_url(url))
        except (TypeError, UnicodeError, ValueError):
            return None

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if not self.database.is_file():
            raise RegistryUnavailableError("registry database is unavailable")
        try:
            connection = sqlite3.connect(
                f"{self.database.as_uri()}?mode=ro",
                uri=True,
                timeout=2,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            self._validate_contract(connection)
            yield connection
        except RegistryContractError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise RegistryUnavailableError("registry database is unavailable") from exc
        finally:
            if "connection" in locals():
                connection.close()

    @staticmethod
    def _validate_contract(connection: sqlite3.Connection) -> None:
        try:
            validate_registry_contract(connection)
        except SchemaContractError as exc:
            raise RegistryContractError(str(exc)) from exc
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise RegistryContractError("invalid registry relationships")
        invalid_current_version = connection.execute(
            """
            SELECT 1
            FROM articles a
            LEFT JOIN article_versions av ON av.version_id = a.current_version_id
            WHERE a.current_version_id IS NOT NULL AND av.article_id IS NOT a.article_id
            LIMIT 1
            """
        ).fetchone()
        if invalid_current_version:
            raise RegistryContractError("invalid article version ownership")
        invalid_appearance = connection.execute(
            """
            SELECT 1
            FROM report_appearances ra
            LEFT JOIN article_versions av ON av.version_id = ra.version_id
            LEFT JOIN discoveries d ON d.discovery_id = ra.discovery_id
            WHERE av.article_id IS NOT ra.article_id
               OR d.article_id IS NOT ra.article_id
               OR d.report_id IS NOT ra.report_id
               OR d.version_id IS NOT ra.version_id
               OR d.ordinal IS NOT ra.ordinal
               OR d.section IS NOT ra.section
               OR d.pillar IS NOT ra.pillar
            LIMIT 1
            """
        ).fetchone()
        if invalid_appearance:
            raise RegistryContractError("invalid report appearance ownership")

    def status(self) -> dict[str, Any]:
        with self.connect() as connection:
            return {
                "available": True,
                "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
                "reports": connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
                "articles": connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
                "discoveries": connection.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0],
                "latest_report_date": connection.execute("SELECT MAX(report_date) FROM reports").fetchone()[0],
            }

    def reports(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        page, page_size = validate_page(page, page_size)
        with self.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
            rows = connection.execute(
                """
                SELECT r.report_date, r.report_title, r.cadence, r.report_format,
                       r.sites_checked, r.sites_succeeded, r.sites_failed,
                       COUNT(ra.article_id) AS article_count
                FROM reports r
                LEFT JOIN report_appearances ra ON ra.report_id = r.report_id
                GROUP BY r.report_id
                ORDER BY r.report_date DESC, r.report_id
                LIMIT ? OFFSET ?
                """,
                (page_size, (page - 1) * page_size),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["monitoring_status"] = _monitoring_status(row)
            items.append(item)
        return {"items": items, "pagination": _pagination(page, page_size, total)}

    def publishers(self) -> dict[str, Any]:
        with self.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            rows = connection.execute(
                """
                SELECT hostname, display_name
                FROM sources
                ORDER BY hostname COLLATE NOCASE, source_id
                LIMIT ?
                """,
                (MAX_PUBLISHER_CHOICES,),
            ).fetchall()
        return {
            "items": [
                {
                    "hostname": row["hostname"],
                    "label": _publisher_label(row["hostname"], row["display_name"]),
                }
                for row in rows
            ],
            "total": total,
            "truncated": total > MAX_PUBLISHER_CHOICES,
        }

    def report(self, report_date: str) -> dict[str, Any]:
        payload, _identity = self.report_with_identity(report_date)
        return payload

    def report_with_identity(
        self, report_date: str
    ) -> tuple[dict[str, Any], RegistryReportIdentity]:
        validate_report_date(report_date)
        with self.connect() as connection:
            report = connection.execute(
                """
                SELECT report_id, report_date, filename, report_title, report_sha256,
                       cadence, report_format, sites_checked, sites_succeeded,
                       sites_failed, parse_warnings_json
                FROM reports WHERE report_date = ?
                """,
                (report_date,),
            ).fetchone()
            if report is None:
                raise RegistryNotFoundError("report not found")
            appearances = connection.execute(
                """
                SELECT ra.ordinal, ra.section, ra.pillar, ra.observation_status,
                       a.article_id, a.canonical_url, av.observed_title AS title,
                       av.observed_summary AS summary, s.display_name AS publisher,
                       s.hostname AS source,
                       ae.enrichment_id AS content_enrichment_id,
                       ae.summary AS enrichment_summary,
                       ae.categories_json AS enrichment_categories_json,
                       ae.keywords_json AS enrichment_keywords_json
                FROM report_appearances ra
                JOIN articles a ON a.article_id = ra.article_id
                JOIN article_versions av ON av.version_id = ra.version_id
                JOIN sources s ON s.source_id = a.source_id
                LEFT JOIN article_enrichments ae ON ae.enrichment_id = (
                    SELECT candidate.enrichment_id
                    FROM article_enrichments candidate
                    WHERE candidate.content_version_id = a.current_content_version_id
                      AND candidate.status = 'complete'
                    ORDER BY candidate.generated_at DESC, candidate.enrichment_id DESC
                    LIMIT 1
                )
                WHERE ra.report_id = ?
                ORDER BY ra.ordinal, a.article_id
                """,
                (report["report_id"],),
            ).fetchall()
        source_report = self._source_report(
            report["report_date"], report["filename"], report["report_sha256"]
        )
        annotations = load_article_annotations(self.metadata_dir)
        articles = []
        for row in appearances:
            item = dict(row)
            has_db_enrichment = item.pop("content_enrichment_id") is not None
            enrichment_summary = item.pop("enrichment_summary")
            enrichment_categories = _json_string_list(
                item.pop("enrichment_categories_json")
            )
            enrichment_keywords = _json_string_list(
                item.pop("enrichment_keywords_json")
            )
            source_article = self._source_article(
                source_report, item["ordinal"], item["canonical_url"]
            )
            annotation = self._annotation_for_url(annotations, item["canonical_url"])
            source_categories = source_article.categories if source_article else ()
            source_keywords = source_article.keywords if source_article else ()
            report_summary = item["summary"]
            item["report_summary"] = report_summary
            item["title"] = annotation.title if annotation else item["title"]
            if has_db_enrichment:
                # A complete current-content enrichment is one semantic bundle.
                # Empty or invalid lists fail closed and never splice with JSON
                # or report metadata. JSON may still supply compatibility-only
                # fields such as title and source_annotation.
                item["summary"] = enrichment_summary
                item["summary_provenance"] = "content_enrichment"
                item["categories"] = enrichment_categories
                item["keywords"] = enrichment_keywords
                item["metadata_provenance"] = {
                    "categories": "content_enrichment",
                    "keywords": "content_enrichment",
                }
                _log_db_annotation_precedence(
                    enrichment_summary,
                    enrichment_categories,
                    enrichment_keywords,
                    annotation,
                )
            elif annotation:
                item["summary"] = annotation.summary
                item["summary_provenance"] = annotation.provenance
                item["categories"] = list(annotation.categories)
                item["keywords"] = list(annotation.keywords)
                item["metadata_provenance"] = {
                    "categories": annotation.provenance,
                    "keywords": annotation.provenance,
                }
            elif (
                source_article is not None
                and source_article.summary.strip()
                and source_categories
                and source_keywords
            ):
                item["summary"] = source_article.summary
                item["summary_provenance"] = "source_report"
                item["categories"] = list(source_categories)
                item["keywords"] = list(source_keywords)
                item["metadata_provenance"] = {
                    "categories": "source_report",
                    "keywords": "source_report",
                }
            else:
                # Preserve the observed report text separately, but do not expose
                # a semantically partial fallback bundle.
                item["summary"] = None
                item["summary_provenance"] = None
                item["categories"] = []
                item["keywords"] = []
                item["metadata_provenance"] = {
                    "categories": None,
                    "keywords": None,
                }
            item["source_annotation"] = (
                {
                    "source_basis": annotation.source_basis,
                    "source_url": annotation.source_url,
                    "generated_on": annotation.generated_on,
                }
                if annotation
                else None
            )
            articles.append(item)
        payload = {
            "report_date": report["report_date"],
            "report_title": report["report_title"],
            "cadence": report["cadence"],
            "report_format": report["report_format"],
            "executive_summary": list(source_report.executive_summary) if source_report else [],
            "monitoring": {
                "status": _monitoring_status(report),
                "sites_checked": report["sites_checked"],
                "sites_succeeded": report["sites_succeeded"],
                "sites_failed": report["sites_failed"],
                "warning_count": len(_json_string_list(report["parse_warnings_json"])),
            },
            "articles": articles,
        }
        identity = RegistryReportIdentity(
            report_date=report["report_date"],
            filename=report["filename"],
            report_title=report["report_title"],
            report_sha256=report["report_sha256"],
        )
        return payload, identity

    def report_identity(self, report_date: str) -> RegistryReportIdentity:
        validate_report_date(report_date)
        with self.connect() as connection:
            report = connection.execute(
                """
                SELECT report_date, filename, report_title, report_sha256
                FROM reports WHERE report_date = ?
                """,
                (report_date,),
            ).fetchone()
        if report is None:
            raise RegistryNotFoundError("report not found")
        return RegistryReportIdentity(
            report_date=report["report_date"],
            filename=report["filename"],
            report_title=report["report_title"],
            report_sha256=report["report_sha256"],
        )

    def articles(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        query: str = "",
        source: str = "",
        pillar: str = "",
        report_date: str = "",
    ) -> dict[str, Any]:
        page, page_size = validate_page(page, page_size)
        if len(query) > 200 or len(source) > 253:
            raise RegistryQueryError("filter is too long")
        if pillar and pillar not in {"A", "B"}:
            raise RegistryQueryError("invalid pillar")
        if report_date:
            validate_report_date(report_date)
        clauses: list[str] = []
        params: list[Any] = []
        if query.strip():
            pattern = f"%{_like_literal(query.strip())}%"
            clauses.append("(av.observed_title LIKE ? ESCAPE '\\' OR av.observed_summary LIKE ? ESCAPE '\\')")
            params.extend((pattern, pattern))
        if source.strip():
            clauses.append("(s.hostname = ? OR s.source_id = ?)")
            params.extend((source.strip().lower(), source.strip()))
        if pillar and report_date:
            clauses.append(
                "EXISTS (SELECT 1 FROM report_appearances raf JOIN reports rf ON rf.report_id = raf.report_id WHERE raf.article_id = a.article_id AND raf.pillar = ? AND rf.report_date = ?)"
            )
            params.extend((pillar, report_date))
        elif pillar:
            clauses.append("EXISTS (SELECT 1 FROM report_appearances rap WHERE rap.article_id = a.article_id AND rap.pillar = ?)")
            params.append(pillar)
        elif report_date:
            clauses.append(
                "EXISTS (SELECT 1 FROM report_appearances rad JOIN reports rd ON rd.report_id = rad.report_id WHERE rad.article_id = a.article_id AND rd.report_date = ?)"
            )
            params.append(report_date)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        base = (
            " FROM articles a JOIN sources s ON s.source_id = a.source_id "
            "LEFT JOIN article_versions av ON av.version_id = a.current_version_id " + where
        )
        with self.connect() as connection:
            total = connection.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
            if self._has_pdf_intake(connection):
                pdf_occurrence_column = (
                    ", (SELECT COUNT(*) FROM pdf_intake_article_occurrences po "
                    "JOIN pdf_intake_articles pa ON pa.article_id = po.article_id "
                    "WHERE pa.core_article_id = a.article_id) AS pdf_occurrence_count"
                    if self._has_pdf_article_links(connection)
                    else ", (SELECT COUNT(*) FROM pdf_intake_article_occurrences po "
                    "JOIN pdf_intake_articles pa ON pa.article_id = po.article_id "
                    "WHERE pa.canonical_url = a.canonical_url) AS pdf_occurrence_count"
                )
            else:
                pdf_occurrence_column = ", 0 AS pdf_occurrence_count"
            rows = connection.execute(
                """
                SELECT a.article_id, a.canonical_url, a.first_seen, a.last_seen,
                       a.document_kind, a.publication_eligible, a.display_policy,
                       s.hostname AS source, s.display_name AS publisher,
                       av.observed_title AS title, av.observed_summary AS report_summary
                """
                + pdf_occurrence_column
                + base
                + " ORDER BY a.last_seen DESC, a.first_seen DESC, a.article_id DESC LIMIT ? OFFSET ?",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "pagination": _pagination(page, page_size, total)}

    @staticmethod
    def _has_pdf_intake(connection: sqlite3.Connection) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pdf_intake_articles'"
        ).fetchone() is not None

    @staticmethod
    def _has_pdf_article_links(connection: sqlite3.Connection) -> bool:
        return any(row[1] == "core_article_id" for row in connection.execute(
            "PRAGMA table_info(pdf_intake_articles)"
        ))

    @staticmethod
    def _pdf_document_sources(connection: sqlite3.Connection, document_sha256: str) -> list[dict[str, str]]:
        has_source_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pdf_intake_document_sources'"
        ).fetchone() is not None
        rows = connection.execute(
            """SELECT source_path, filename, observed_at FROM pdf_intake_document_sources
               WHERE document_sha256=? ORDER BY source_path""", (document_sha256,),
        ).fetchall() if has_source_table else []
        if rows:
            return [{"path": row["source_path"], "filename": row["filename"],
                     "observed_at": row["observed_at"]} for row in rows]
        row = connection.execute(
            """SELECT source_path, filename, imported_at FROM pdf_intake_documents
               WHERE document_sha256=?""", (document_sha256,),
        ).fetchone()
        return ([{"path": row["source_path"], "filename": row["filename"],
                  "observed_at": row["imported_at"]}] if row else [])

    @staticmethod
    def _pdf_occurrences(
        connection: sqlite3.Connection, article_id: str, canonical_url: str,
        *, pdf_article_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not RegistryReader._has_pdf_intake(connection):
            return []
        if pdf_article_id is not None:
            rows = connection.execute(
                """SELECT o.occurrence_id, o.occurrence_json, o.source_document_sha256 FROM pdf_intake_article_occurrences o
                   WHERE o.article_id=?
                   ORDER BY COALESCE(o.report_date, '' ) DESC, o.page DESC, o.occurrence_id DESC""",
                (pdf_article_id,),
            ).fetchall()
        elif RegistryReader._has_pdf_article_links(connection):
            rows = connection.execute(
                """SELECT o.occurrence_id, o.occurrence_json, o.source_document_sha256 FROM pdf_intake_article_occurrences o
                   JOIN pdf_intake_articles a ON a.article_id=o.article_id
                   WHERE a.core_article_id=?
                   ORDER BY COALESCE(o.report_date, '' ) DESC, o.page DESC, o.occurrence_id DESC""",
                (article_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT o.occurrence_id, o.occurrence_json, o.source_document_sha256 FROM pdf_intake_article_occurrences o
                   JOIN pdf_intake_articles a ON a.article_id=o.article_id
                   WHERE a.canonical_url=?
                   ORDER BY COALESCE(o.report_date, '' ) DESC, o.page DESC, o.occurrence_id DESC""",
                (canonical_url,),
            ).fetchall()
        try:
            values = [json.loads(row["occurrence_json"]) for row in rows]
        except (json.JSONDecodeError, TypeError) as exc:
            raise RegistryContractError("invalid PDF article occurrence data") from exc
        if any(not isinstance(value, dict) for value in values):
            raise RegistryContractError("invalid PDF article occurrence data")
        sources: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            sha = row["source_document_sha256"]
            if sha not in sources:
                sources[sha] = RegistryReader._pdf_document_sources(connection, sha)
        for row, value in zip(rows, values):
            value["occurrence_id"] = row["occurrence_id"]
            value["source_observations"] = sources[row["source_document_sha256"]]
        from .information_checks import latest_checks
        return [dict(value, **latest_checks(connection, "articles", value)) for value in values
            if value.get("summary_basis") != "verbatim_pdf_calendar_row"]

    @classmethod
    def _pdf_article_payload(cls, connection: sqlite3.Connection, row: sqlite3.Row,
        *, allowed_occurrence_ids: set[str] | None = None) -> dict[str, Any]:
        canonical_url = row["canonical_url"]
        core_article_id = (
            row["core_article_id"] if cls._has_pdf_article_links(connection) else row["article_id"]
        )
        occurrences = cls._pdf_occurrences(
            connection, core_article_id or row["article_id"], canonical_url,
            pdf_article_id=row["article_id"],
        )
        if allowed_occurrence_ids is not None:
            occurrences = [item for item in occurrences if item["occurrence_id"] in allowed_occurrence_ids]
        latest = occurrences[0] if occurrences else {}
        try:
            classification = json.loads(row["type_safe_classification_json"]) if row["type_safe_classification_json"] else None
        except (json.JSONDecodeError, TypeError) as exc:
            raise RegistryContractError("invalid PDF article classification data") from exc
        host = (urlsplit(canonical_url).hostname or "unknown").removeprefix("www.").lower()
        dates = [item.get("report_date") for item in occurrences if item.get("report_date")]
        return {
            "article_id": row["article_id"],
            "canonical_url": canonical_url,
            "title": row["title"] or latest.get("anchor_text") or latest.get("title"),
            "summary": latest.get("summary"),
            "source": host,
            "publisher": host,
            "first_seen": min(dates) if dates else None,
            "last_seen": max(dates) if dates else None,
            "source_kind": "pdf",
            "type_safe_classification": classification,
            "occurrences": occurrences,
        }

    def pdf_articles(
        self, *, page: int = 1, page_size: int = 20, query: str = "",
        source: str = "", report_date: str = "",
        allowed_occurrence_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        validate_page(page, page_size)
        if len(query) > 200 or len(source) > 253:
            raise RegistryQueryError("filter is too long")
        if report_date:
            validate_report_date(report_date)
        return page_pdf_articles(
            self.pdf_articles_all(allowed_occurrence_ids=allowed_occurrence_ids),
            page=page, page_size=page_size, query=query, source=source, report_date=report_date,
        )

    def pdf_articles_all(self, *, allowed_occurrence_ids: set[str] | None = None) -> list[dict[str, Any]]:
        """Read full PDF article observations before a caller merges and paginates."""
        with self.connect() as connection:
            if not self._has_pdf_intake(connection):
                return []
            rows = connection.execute(
                "SELECT * FROM pdf_intake_articles WHERE core_article_id IS NULL"
                if self._has_pdf_article_links(connection) else
                """SELECT * FROM pdf_intake_articles
                   WHERE NOT EXISTS (
                       SELECT 1 FROM articles core WHERE core.canonical_url=pdf_intake_articles.canonical_url
                   )"""
            ).fetchall()
            items = []
            for row in rows:
                item = self._pdf_article_payload(connection, row, allowed_occurrence_ids=allowed_occurrence_ids)
                if not item["occurrences"]:
                    continue
                items.append(item)
        items.sort(key=lambda item: (item["last_seen"] or "", item["article_id"]), reverse=True)
        return items

    def pdf_article(self, article_id: str, *, allowed_occurrence_ids: set[str] | None = None) -> dict[str, Any]:
        validate_article_id(article_id)
        with self.connect() as connection:
            if not self._has_pdf_intake(connection):
                raise RegistryNotFoundError("article not found")
            row = connection.execute(
                "SELECT * FROM pdf_intake_articles WHERE article_id=?", (article_id,),
            ).fetchone()
            if row is None:
                raise RegistryNotFoundError("article not found")
            payload = self._pdf_article_payload(connection, row, allowed_occurrence_ids=allowed_occurrence_ids)
            if allowed_occurrence_ids is not None and not payload["occurrences"]:
                raise RegistryNotFoundError("article not found")
            return payload

    def pdf_calendar_items(
        self, *, page: int = 1, page_size: int = 20, query: str = "", kind: str = "",
        allowed_occurrence_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        validate_page(page, page_size)
        if len(query) > 200 or len(kind) > 80:
            raise RegistryQueryError("filter is too long")
        return page_pdf_calendar_items(
            self.pdf_calendar_items_all(allowed_occurrence_ids=allowed_occurrence_ids),
            page=page, page_size=page_size, query=query, kind=kind,
        )

    def pdf_calendar_items_all(
        self, *, allowed_occurrence_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Recover each allow-listed source PDF once, then return all calendar rows."""
        with self.connect() as connection:
            if not self._has_pdf_intake(connection):
                return []
            rows = connection.execute(
                """SELECT c.occurrence_id, c.item_json, c.source_document_sha256,
                          c.type_safe_classification_json, d.filename AS source_filename
                   FROM pdf_intake_calendar_items c
                   JOIN pdf_intake_documents d ON d.document_sha256=c.source_document_sha256"""
            ).fetchall()
            if allowed_occurrence_ids is not None:
                rows = [row for row in rows if row["occurrence_id"] in allowed_occurrence_ids]
            sources = {
                sha: self._pdf_document_sources(connection, sha)
                for sha in {row["source_document_sha256"] for row in rows}
            }
            originals = {}
            if "original_pdf" in {column[1] for column in connection.execute("PRAGMA table_info(pdf_intake_documents)")}:
                for sha in sources:
                    row = connection.execute(
                        "SELECT original_pdf FROM pdf_intake_documents WHERE document_sha256=?", (sha,),
                    ).fetchone()
                    if row is not None:
                        originals[sha] = row["original_pdf"]
        items = []
        for row in rows:
            try:
                item = json.loads(row["item_json"])
            except (json.JSONDecodeError, TypeError) as exc:
                raise RegistryContractError("invalid PDF calendar item data") from exc
            if not isinstance(item, dict):
                raise RegistryContractError("invalid PDF calendar item data")
            item["occurrence_id"] = row["occurrence_id"]
            try:
                item["type_safe_classification"] = (
                    json.loads(row["type_safe_classification_json"])
                    if row["type_safe_classification_json"] else None
                )
            except (json.JSONDecodeError, TypeError) as exc:
                raise RegistryContractError("invalid PDF calendar classification data") from exc
            item["source_kind"] = "pdf"
            item["source_filename"] = row["source_filename"]
            item["source_observations"] = sources[row["source_document_sha256"]]
            items.append(item)
        if originals:
            from climate_monitor.pdf_intake import recover_calendar_fields
            indexes_by_sha: dict[str, list[int]] = {}
            for index, item in enumerate(items):
                indexes_by_sha.setdefault(item["source_document_sha256"], []).append(index)
            for sha, raw in originals.items():
                if raw is not None:
                    indexes = indexes_by_sha.get(sha, [])
                    recovered = recover_calendar_fields(raw, [items[index] for index in indexes])
                    by_id = {item["occurrence_id"]: item for item in recovered}
                    for index in indexes:
                        items[index] = by_id.get(items[index]["occurrence_id"], items[index])
        from climate_monitor.meeting_fields import pdf_meeting_fields
        from .information_checks import latest_checks
        with self.connect() as connection:
            items = [dict(pdf_meeting_fields(item), **latest_checks(connection, "meetings", item)) for item in items]
        items.sort(key=lambda item: (
            item.get("start_date") or "9999", item.get("name") or "", item.get("occurrence_id") or "",
        ))
        return items

    def resolve_pdf_meeting_identities(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Bind verified overlays to identities in this reader's native event history."""
        from climate_monitor.meetings import resolve_event_identity
        result = []
        with self.connect() as connection:
            has_events = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='climate_event_sources'").fetchone()
            for raw in items:
                item = dict(raw)
                if has_events and item.get("verification_status") == "verified" and item.get("collected_candidate"):
                    body_hash = next((check.get("body_sha256") for check in item.get("checks", [])
                        if check.get("verification_status") == "verified"), None)
                    if body_hash:
                        identity = resolve_event_identity(connection, item["collected_candidate"], body_hash)
                        if connection.execute("SELECT 1 FROM climate_events WHERE event_id=?", (identity,)).fetchone():
                            item["canonical_event_id"] = identity
                result.append(item)
        return result

    def meetings(self, *, page: int = 1, page_size: int = 20, query: str = "",
        verification_status: str = "", base_date: str | None = None,
        additional_calendar_items: list[dict[str, Any]] | None = None,
        additional_events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Meetings have their own reader; verified imports join collected records."""
        from climate_monitor.meetings import query_events
        from .range_reports import _calendar_end_date
        page, page_size = validate_page(page, page_size)
        if len(query) > 200 or verification_status not in {"", "unchecked", "partial", "conflict", "verified"}:
            raise RegistryQueryError("invalid meeting filter")
        base_date = validate_report_date(base_date or datetime.now(timezone.utc).date().isoformat())
        payload = query_events(self.database, base_date=base_date, timezone_name="UTC", include_deadlines=True)
        collected = {record["event_id"]: dict(record, origin="web_collection", collection_status="collected",
            verification_status="verified", access_status="accessible") for record in payload["records"] + (additional_events or [])}
        from climate_monitor.meeting_fields import collected_pdf_meeting, merge_meeting_observations
        from .information_checks import merge_checked_observation
        imported_by_id = {}
        for item in self.pdf_calendar_items_all() + (additional_calendar_items or []):
            occurrence_id = item["occurrence_id"]
            imported_by_id[occurrence_id] = (
                merge_checked_observation(imported_by_id[occurrence_id], item)
                if occurrence_id in imported_by_id else item
            )
        imported = list(imported_by_id.values())
        for item in self.resolve_pdf_meeting_identities(imported):
            item = collected_pdf_meeting(item)
            end = _calendar_end_date(item)
            if end is not None and end < date.fromisoformat(base_date):
                continue
            candidate = item.get("collected_candidate")
            identity = item.get("canonical_event_id")
            if candidate and identity:
                if candidate.get("status") in {"cancelled", "retrospective"}:
                    continue
                if identity in collected:
                    collected[identity] = merge_meeting_observations([collected[identity], item])[0]
                    continue
            else:
                identity = item["occurrence_id"]
            collected[identity] = item
        items = [item for item in collected.values()
            if (not verification_status or item["verification_status"] == verification_status)
            and (not query or query.casefold() in " ".join(str(item.get(key) or "") for key in
                ("name", "organizer", "relevance_reason", "raw_date")).casefold())]
        items.sort(key=lambda item: (item.get("start_date") or item.get("deadline_date") or "9999", item.get("name") or ""))
        counts = {status: sum(item.get("verification_status") == status for item in collected.values())
            for status in ("unchecked", "partial", "conflict", "verified")}
        offset = (page - 1) * page_size
        return {"items": items[offset:offset + page_size], "pagination": _pagination(page, page_size, len(items)),
            "base_date": base_date, "verification_counts": counts, "coverage": payload["coverage"]}

    @staticmethod
    def _has_acquisition_projection(connection: sqlite3.Connection) -> bool:
        return all(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone() is not None
            for name in ("acquisition_batches", "acquisition_items", "acquisition_searches")
        )

    @staticmethod
    def _timestamp_key(value: str | None) -> datetime:
        if not value:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    @classmethod
    def _acquisition_projection(
        cls, connection: sqlite3.Connection, article_id: str, current_version_id: str | None,
        display_policy: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not cls._has_acquisition_projection(connection):
            return [], {}
        has_resolution = any(
            row[1] == "resolved_by_fetch_id"
            for row in connection.execute("PRAGMA table_info(acquisition_items)")
        )
        resolution = "i.resolved_by_fetch_id" if has_resolution else "NULL"
        rows = connection.execute(
            f"""
            SELECT i.acquisition_item_id, i.batch_id, i.ordinal, i.raw_url, i.source_name,
                   i.title, i.summary, i.discovered_at, i.discovery_kind, i.discovery_ref,
                   i.origins_json, i.publication_date, i.publication_date_evidence_json,
                   i.date_status, i.selection_status, i.selection_reason, i.update_status,
                   i.material_status, i.fetch_id, i.content_version_id, i.processing_status,
                   i.processing_error, {resolution} AS resolved_by_fetch_id,
                   f.fetch_status, f.fetched_at, f.content_version_id AS fetch_content_version_id,
                   r.fetch_status AS resolved_fetch_status, r.fetched_at AS resolved_fetched_at,
                   r.content_version_id AS resolved_content_version_id
            FROM acquisition_items i
            LEFT JOIN article_fetches f ON f.fetch_id = i.fetch_id
            LEFT JOIN article_fetches r ON r.fetch_id = {resolution}
            WHERE i.article_id = ?
            ORDER BY i.discovered_at, i.acquisition_item_id
            """,
            (article_id,),
        ).fetchall()
        batch_ids = sorted({row["batch_id"] for row in rows})
        search_rows = connection.execute(
            "SELECT batch_id, search_ref, search_id, query, engine, status, attempted_at "
            "FROM acquisition_searches WHERE batch_id IN (" + ",".join("?" for _ in batch_ids) + ")",
            batch_ids,
        ).fetchall() if batch_ids else []
        searches = {(row["batch_id"], row["search_ref"]): dict(row) for row in search_rows}
        observations: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        for row in rows:
            try:
                origins = json.loads(row["origins_json"])
                date_evidence = json.loads(row["publication_date_evidence_json"]) if row["publication_date_evidence_json"] else None
            except (json.JSONDecodeError, TypeError) as exc:
                raise RegistryContractError("invalid acquisition observation data") from exc
            if not isinstance(origins, list) or any(not isinstance(origin, dict) for origin in origins):
                raise RegistryContractError("invalid acquisition observation data")
            for origin in origins:
                search = searches.get((row["batch_id"], origin.get("search_ref")))
                if search:
                    origin["search"] = search
            item = {
                key: row[key] for key in (
                    "acquisition_item_id", "batch_id", "ordinal", "raw_url", "source_name", "title",
                    "summary", "discovered_at", "discovery_kind", "discovery_ref", "publication_date",
                    "date_status", "selection_status", "selection_reason", "update_status", "material_status",
                    "fetch_id", "content_version_id", "processing_status", "processing_error", "resolved_by_fetch_id",
                )
            }
            item["publication_date_evidence"] = date_evidence
            item["origins"] = origins
            item["collected_at"] = (
                row["fetched_at"] if row["fetch_status"] == "success"
                else row["resolved_fetched_at"]
                if row["resolved_fetch_status"] == "success" else None
            )
            item["fetch"] = {
                "status": row["fetch_status"], "fetched_at": row["fetched_at"],
                "content_version_id": row["fetch_content_version_id"],
            }
            if row["resolved_by_fetch_id"]:
                item["resolved_fetch"] = {
                    "status": row["resolved_fetch_status"], "fetched_at": row["resolved_fetched_at"],
                    "content_version_id": row["resolved_content_version_id"],
                }
            observations.append(item)
            version_id = row["fetch_content_version_id"] if row["fetch_status"] == "success" else None
            fetched_at = row["fetched_at"] if version_id else None
            if version_id is None and row["resolved_fetch_status"] == "success":
                version_id, fetched_at = row["resolved_content_version_id"], row["resolved_fetched_at"]
            if version_id and row["material_status"] == "full_content" and row["processing_status"] == "complete":
                candidates.append({"content_version_id": version_id, "fetched_at": fetched_at,
                                   "fetch_id": row["fetch_id"], "acquisition_item_id": row["acquisition_item_id"]})
        if current_version_id:
            current = connection.execute(
                """SELECT fetch_id, fetched_at FROM article_fetches
                   WHERE article_id=? AND content_version_id=? AND fetch_status='success'
                   ORDER BY fetched_at DESC, fetch_id DESC LIMIT 1""",
                (article_id, current_version_id),
            ).fetchone()
            candidates.append({"content_version_id": current_version_id,
                               "fetched_at": current["fetched_at"] if current else None,
                               "fetch_id": current["fetch_id"] if current else None,
                               "acquisition_item_id": None})
        if not candidates:
            return observations, {}
        selected = max(candidates, key=lambda item: (
            cls._timestamp_key(item["fetched_at"]),
            item["content_version_id"] == current_version_id,
            item["content_version_id"], item.get("fetch_id") or "",
        ))
        content = connection.execute(
            """SELECT content_version_id, content_sha256, markdown_content, content_type, source_bytes,
                      extraction_method, extraction_version, first_fetched_at
               FROM article_content_versions WHERE article_id=? AND content_version_id=?""",
            (article_id, selected["content_version_id"]),
        ).fetchone()
        if content is None:
            return observations, {}
        available = {
            key: content[key]
            for key in content.keys()
            if key not in {"markdown_content", "content_sha256"}
        }
        available.update({key: selected[key] for key in ("fetch_id", "acquisition_item_id")})
        available["collected_at"] = selected["fetched_at"]
        available["selection_basis"] = "latest_successful_acquisition_or_current_content"
        if display_policy == "full_markdown":
            available["markdown"] = article_body_markdown(content["markdown_content"])
        elif display_policy == "summary_excerpt":
            available["supporting_excerpt"] = article_preview(content["markdown_content"])
        return observations, available

    def article(self, article_id: str) -> dict[str, Any]:
        if not article_id or len(article_id) > 128 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in article_id):
            raise RegistryQueryError("invalid article id")
        with self.connect() as connection:
            article = connection.execute(
                """
                SELECT a.article_id, a.canonical_url, a.first_seen, a.last_seen,
                       a.document_kind, a.publication_eligible, a.display_policy,
                       a.current_version_id, a.current_content_version_id, s.hostname AS source,
                       s.display_name AS publisher, av.observed_title AS title,
                       av.observed_summary AS report_summary
                FROM articles a
                JOIN sources s ON s.source_id = a.source_id
                LEFT JOIN article_versions av ON av.version_id = a.current_version_id
                WHERE a.article_id = ?
                """,
                (article_id,),
            ).fetchone()
            if article is None:
                raise RegistryNotFoundError("article not found")
            appearances = connection.execute(
                """
                SELECT r.report_date, r.filename AS source_filename,
                       r.report_sha256 AS source_sha256, r.report_title,
                       ra.version_id, ra.section, ra.pillar, ra.ordinal,
                       ra.observation_status, av.observed_title AS title,
                       av.observed_summary AS summary, d.raw_url AS original_url
                FROM report_appearances ra
                JOIN reports r ON r.report_id = ra.report_id
                JOIN article_versions av ON av.version_id = ra.version_id
                JOIN discoveries d ON d.discovery_id = ra.discovery_id
                WHERE ra.article_id = ?
                ORDER BY r.report_date DESC, ra.ordinal, r.report_id
                """,
                (article_id,),
            ).fetchall()
            fetch = connection.execute(
                """
                SELECT fetched_at, fetch_status, http_status, content_type, error_code
                FROM article_fetches WHERE article_id = ?
                ORDER BY fetched_at DESC, fetch_id DESC LIMIT 1
                """,
                (article_id,),
            ).fetchone()
            content = None
            enrichment = None
            if article["current_content_version_id"]:
                content = connection.execute(
                    """
                    SELECT markdown_content, content_type, source_bytes, extraction_method,
                           extraction_version, first_fetched_at
                    FROM article_content_versions
                    WHERE content_version_id = ? AND article_id = ?
                    """,
                    (article["current_content_version_id"], article_id),
                ).fetchone()
                enrichment = connection.execute(
                    """
                    SELECT summary, categories_json, keywords_json, language, generator_kind,
                           generator_name, generator_version, generated_at
                    FROM article_enrichments
                    WHERE content_version_id = ? AND status = 'complete'
                    ORDER BY generated_at DESC, enrichment_id DESC LIMIT 1
                    """,
                    (article["current_content_version_id"],),
                ).fetchone()
            acquisition_observations, available_content = self._acquisition_projection(
                connection, article_id, article["current_content_version_id"], article["display_policy"],
            )
            has_date_observations = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='article_date_observations'"
            ).fetchone() is not None
            date_observations = []
            if has_date_observations:
                for row in connection.execute(
                    """SELECT observation_id, observation_kind, observed_at, evidence_json, recorded_at
                       FROM article_date_observations WHERE article_id=?
                       ORDER BY observed_at, observation_id""",
                    (article_id,),
                ):
                    try:
                        evidence = json.loads(row["evidence_json"])
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise RegistryContractError("invalid article date observation data") from exc
                    date_observations.append({
                        "observation_id": row["observation_id"],
                        "kind": row["observation_kind"],
                        "observed_at": row["observed_at"],
                        "evidence": evidence,
                        "recorded_at": row["recorded_at"],
                    })
        collected_values = [
            item["observed_at"] for item in date_observations if item["kind"] == "collection"
        ]
        if available_content.get("collected_at"):
            collected_values.append(available_content["collected_at"])
        collected_at = max(collected_values, key=self._timestamp_key) if collected_values else None
        information_dates = []
        for item in date_observations:
            if item["kind"] != "page_information":
                continue
            try:
                value = date.fromisoformat(item["observed_at"])
            except (TypeError, ValueError) as exc:
                raise RegistryContractError("invalid article information date") from exc
            if value.isoformat() != item["observed_at"]:
                raise RegistryContractError("invalid article information date")
            information_dates.append(value.isoformat())
        information_date = max(information_dates) if information_dates else None
        date_basis = "collection_time" if collected_at else "information_date" if information_date else "unknown"
        appearance_payload = []
        report_categories: list[str] = []
        report_keywords: list[str] = []
        effective_report_categories: list[str] = []
        effective_report_keywords: list[str] = []
        annotation_summary: str | None = None
        fallback_categories: list[str] = []
        fallback_keywords: list[str] = []
        fallback_provenance: str | None = None
        category_provenance: str | None = None
        keyword_provenance: str | None = None
        contexts: dict[tuple[str, str, str], ParsedReport | None] = {}
        annotations = load_article_annotations(self.metadata_dir)
        source_annotation: ArticleAnnotation | None = None
        for row in appearances:
            item = dict(row)
            source_filename = item["source_filename"]
            source_sha256 = item["source_sha256"]
            context_key = (item["report_date"], source_filename, source_sha256)
            if context_key not in contexts:
                contexts[context_key] = self._source_report(*context_key)
            source_report = contexts[context_key]
            source_article = self._source_article(
                source_report, item["ordinal"], item["original_url"]
            )
            annotation = self._annotation_for_url(annotations, item["original_url"])
            source_annotation = source_annotation or annotation
            source_categories = source_article.categories if source_article else ()
            source_keywords = source_article.keywords if source_article else ()
            categories = list((annotation.categories if annotation else ()) or source_categories)
            keywords = list((annotation.keywords if annotation else ()) or source_keywords)
            if annotation:
                item["summary"] = annotation.summary
                annotation_summary = annotation_summary or annotation.summary
                if fallback_provenance is None:
                    fallback_categories = list(annotation.categories)
                    fallback_keywords = list(annotation.keywords)
                    fallback_provenance = annotation.provenance
            elif (
                fallback_provenance is None
                and source_article is not None
                and source_article.summary.strip()
                and source_article.categories
                and source_article.keywords
            ):
                annotation_summary = source_article.summary
                fallback_categories = list(source_article.categories)
                fallback_keywords = list(source_article.keywords)
                fallback_provenance = "source_report"
            item["summary_provenance"] = (
                annotation.provenance if annotation else "source_report"
            )
            item["categories"] = categories
            item["keywords"] = keywords
            item["metadata_provenance"] = {
                "categories": (
                    annotation.provenance
                    if annotation
                    else "source_report" if source_categories else None
                ),
                "keywords": (
                    annotation.provenance
                    if annotation
                    else "source_report" if source_keywords else None
                ),
            }
            item["source_annotation"] = (
                {
                    "source_basis": annotation.source_basis,
                    "source_url": annotation.source_url,
                    "generated_on": annotation.generated_on,
                }
                if annotation
                else None
            )
            if annotation:
                category_provenance = annotation.provenance
                keyword_provenance = annotation.provenance
            else:
                if source_categories and category_provenance is None:
                    category_provenance = "source_report"
                if source_keywords and keyword_provenance is None:
                    keyword_provenance = "source_report"
            report_categories.extend(source_categories)
            report_keywords.extend(source_keywords)
            effective_report_categories.extend(categories)
            effective_report_keywords.extend(keywords)
            appearance_payload.append(item)

        policy = article["display_policy"]
        content_payload: dict[str, Any] = {}
        if content:
            content_payload.update(
                {
                    "content_version_id": article["current_content_version_id"],
                    "content_type": content["content_type"],
                    "source_bytes": content["source_bytes"],
                    "extraction_method": content["extraction_method"],
                    "extraction_version": content["extraction_version"],
                    "fetched_at": content["first_fetched_at"],
                    "collected_at": available_content.get("collected_at"),
                }
            )
            if policy == "summary_excerpt":
                content_payload["supporting_excerpt"] = article_preview(content["markdown_content"])
            elif policy == "full_markdown":
                content_payload["markdown"] = article_body_markdown(content["markdown_content"])
        enrichment_payload = {
            "summary": enrichment["summary"] if enrichment else None,
            "categories": _json_string_list(enrichment["categories_json"]) if enrichment else [],
            "keywords": _json_string_list(enrichment["keywords_json"]) if enrichment else [],
            "language": enrichment["language"] if enrichment else None,
            "content_version_id": article["current_content_version_id"] if enrichment else None,
            "generator": (
                {
                    "kind": enrichment["generator_kind"],
                    "name": enrichment["generator_name"],
                    "version": enrichment["generator_version"],
                    "generated_at": enrichment["generated_at"],
                }
                if enrichment
                else None
            ),
        }
        has_db_enrichment = enrichment is not None
        if has_db_enrichment:
            _log_db_annotation_precedence(
                enrichment_payload["summary"],
                enrichment_payload["categories"],
                enrichment_payload["keywords"],
                source_annotation,
            )
        report_metadata = {
            "categories": _ordered_unique(report_categories),
            "keywords": _ordered_unique(report_keywords),
        }
        # Presence of a complete row, rather than truthiness of any one field,
        # controls DB-first precedence for the whole semantic bundle.
        categories = (
            enrichment_payload["categories"]
            if has_db_enrichment
            else fallback_categories
        )
        keywords = (
            enrichment_payload["keywords"]
            if has_db_enrichment
            else fallback_keywords
        )
        summary = (
            enrichment_payload["summary"]
            if has_db_enrichment
            else annotation_summary
        )
        metadata_provenance = {
            "categories": "content_enrichment" if has_db_enrichment else fallback_provenance,
            "keywords": "content_enrichment" if has_db_enrichment else fallback_provenance,
        }
        original_url = appearance_payload[0]["original_url"] if appearance_payload else article["canonical_url"]
        payload = {
            "article_id": article["article_id"],
            "title": source_annotation.title if source_annotation else article["title"],
            "summary": summary,
            "summary_provenance": (
                "content_enrichment"
                if has_db_enrichment
                else fallback_provenance
            ),
            "report_summary": article["report_summary"],
            "current_version_id": article["current_version_id"],
            "canonical_url": article["canonical_url"],
            "original_url": original_url,
            "source": article["source"],
            "publisher": article["publisher"],
            "first_seen": article["first_seen"],
            "last_seen": article["last_seen"],
            "collected_at": collected_at,
            "information_date": information_date,
            "date_basis": date_basis,
            "date_observations": date_observations,
            "document_kind": article["document_kind"],
            "publication_eligible": bool(article["publication_eligible"]),
            "display_policy": policy,
            "appearances": appearance_payload,
            "latest_fetch": dict(fetch) if fetch else None,
            "content": content_payload,
            "available_content": available_content,
            "acquisition_observations": acquisition_observations,
            "enrichment": enrichment_payload,
            "report_metadata": report_metadata,
            "categories": categories,
            "keywords": keywords,
            "metadata_provenance": metadata_provenance,
            "source_annotation": (
                {
                    "source_basis": source_annotation.source_basis,
                    "source_url": source_annotation.source_url,
                    "generated_on": source_annotation.generated_on,
                }
                if source_annotation
                else None
            ),
        }
        with self.connect() as connection:
            pdf_occurrences = self._pdf_occurrences(
                connection, article["article_id"], article["canonical_url"],
            )
        if pdf_occurrences:
            payload["pdf_occurrences"] = pdf_occurrences
        return payload
