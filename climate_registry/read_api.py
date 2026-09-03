from __future__ import annotations

import json
import logging
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Iterator

from climate_delivery.errors import ClimateDeliveryError
from climate_monitor.dedupe import canonical_url

from .annotations import ArticleAnnotation, load_article_annotations
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
                f"{self.database.as_uri()}?mode=ro&immutable=1",
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
            WHERE a.current_version_id IS NULL OR av.article_id IS NOT a.article_id
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
            "JOIN article_versions av ON av.version_id = a.current_version_id " + where
        )
        with self.connect() as connection:
            total = connection.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
            rows = connection.execute(
                """
                SELECT a.article_id, a.canonical_url, a.first_seen, a.last_seen,
                       a.document_kind, a.publication_eligible, a.display_policy,
                       s.hostname AS source, s.display_name AS publisher,
                       av.observed_title AS title, av.observed_summary AS report_summary
                """
                + base
                + " ORDER BY a.last_seen DESC, a.first_seen DESC, a.article_id DESC LIMIT ? OFFSET ?",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "pagination": _pagination(page, page_size, total)}

    def article(self, article_id: str) -> dict[str, Any]:
        if not article_id or len(article_id) > 128 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in article_id):
            raise RegistryQueryError("invalid article id")
        with self.connect() as connection:
            article = connection.execute(
                """
                SELECT a.article_id, a.canonical_url, a.first_seen, a.last_seen,
                       a.document_kind, a.publication_eligible, a.display_policy,
                       a.current_content_version_id, s.hostname AS source,
                       s.display_name AS publisher, av.observed_title AS title,
                       av.observed_summary AS report_summary
                FROM articles a
                JOIN sources s ON s.source_id = a.source_id
                JOIN article_versions av ON av.version_id = a.current_version_id
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
                       ra.section, ra.pillar, ra.ordinal,
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
            source_filename = item.pop("source_filename")
            source_sha256 = item.pop("source_sha256")
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
                    "content_type": content["content_type"],
                    "source_bytes": content["source_bytes"],
                    "extraction_method": content["extraction_method"],
                    "extraction_version": content["extraction_version"],
                    "fetched_at": content["first_fetched_at"],
                }
            )
            if policy == "summary_excerpt":
                compact = " ".join(content["markdown_content"].split())
                content_payload["supporting_excerpt"] = compact[:500]
            elif policy == "full_markdown":
                content_payload["markdown"] = content["markdown_content"]
        enrichment_payload = {
            "summary": enrichment["summary"] if enrichment else None,
            "categories": _json_string_list(enrichment["categories_json"]) if enrichment else [],
            "keywords": _json_string_list(enrichment["keywords_json"]) if enrichment else [],
            "language": enrichment["language"] if enrichment else None,
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
        return {
            "article_id": article["article_id"],
            "title": source_annotation.title if source_annotation else article["title"],
            "summary": summary,
            "summary_provenance": (
                "content_enrichment"
                if has_db_enrichment
                else fallback_provenance
            ),
            "report_summary": article["report_summary"],
            "canonical_url": article["canonical_url"],
            "original_url": original_url,
            "source": article["source"],
            "publisher": article["publisher"],
            "first_seen": article["first_seen"],
            "last_seen": article["last_seen"],
            "document_kind": article["document_kind"],
            "publication_eligible": bool(article["publication_eligible"]),
            "display_policy": policy,
            "appearances": appearance_payload,
            "latest_fetch": dict(fetch) if fetch else None,
            "content": content_payload,
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
