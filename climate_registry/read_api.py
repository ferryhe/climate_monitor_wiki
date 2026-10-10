from __future__ import annotations

import json
import logging
import math
import sqlite3
from contextlib import contextmanager
from functools import wraps
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
from .information_checks import deduplicate_pdf_occurrences

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


def _public_read(function):
    @wraps(function)
    def read(self, *args, **kwargs):
        if self.public and not hasattr(self, "_snapshot_connection"):
            with self.public_snapshot():
                return function(self, *args, **kwargs)
        return function(self, *args, **kwargs)
    return read


class RegistryReader:
    @classmethod
    def from_public_export(cls, repository_root: str | Path):
        """Read a verified committed export without provisioning a business DB."""
        from .acquisition_review import digest
        root = Path(repository_root)
        try:
            artifact = json.loads((root / "wiki/public-registry.json").read_text(encoding="utf-8"))
            sha = artifact.pop("artifact_sha256")
            if artifact.get("schema_version") != "climate-public-snapshot.v1" or digest(artifact) != sha:
                raise RegistryContractError("Git public snapshot identity differs")
        except (OSError, ValueError, KeyError) as exc:
            raise RegistryUnavailableError("approved Git export is unavailable") from exc
        reader = cls.__new__(cls)
        reader.database = None
        reader.public = True
        reader.static_snapshot = artifact
        reader.source_dir = reader.metadata_dir = None
        return reader

    def __init__(
        self,
        database: str | Path,
        *,
        repository_root: str | Path,
        source_dir: str | Path | None = None,
        metadata_dir: str | Path | None = None,
        public: bool = True,
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
        self.public = public
        self.static_snapshot = None
        static_path = root / "wiki" / "public-registry.json"
        import os
        if public and os.getenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT") == "1" and static_path.is_file():
            from .acquisition_review import digest
            artifact = json.loads(static_path.read_text(encoding="utf-8"))
            sha = artifact.pop("artifact_sha256")
            if digest(artifact) != sha:
                raise RegistryContractError("Git public snapshot identity differs")
            self.static_snapshot = artifact
        self.source_dir = Path(source_dir).resolve(strict=False) if source_dir is not None else None
        self.metadata_dir = Path(metadata_dir).resolve(strict=False) if metadata_dir is not None else None

    def _source_report(
        self, report_date: str, filename: str, expected_sha256: str, *, archive: bool = False
    ) -> ParsedReport | None:
        if self.public and not archive:
            with self.connect() as connection:
                if connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                    if not hasattr(self,"_snapshot_reports"):
                        reports={}
                        for (encoded,) in connection.execute("SELECT snapshot_json FROM public_snapshot_metadata"):
                            reports.update(json.loads(encoded).get("report_fallbacks",{}))
                        self._snapshot_reports=reports
                    value=self._snapshot_reports.get(report_date)
                    if value and value["sha256"]==expected_sha256 and value["path"]==filename:
                        fields=dict(value)
                        fields["path"]=Path(filename)
                        fields["articles"]=tuple(ParsedArticle(**item) for item in fields["articles"])
                        return ParsedReport(**fields)
                    return None
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

    def _annotations(self):
        if hasattr(self, "_snapshot_annotations"):
            return self._snapshot_annotations
        if not self.public:
            return load_article_annotations(self.metadata_dir)
        with self.connect() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] < 21:
                return load_article_annotations(self.metadata_dir)
            result = {}
            for (encoded,) in connection.execute("SELECT snapshot_json FROM public_snapshot_metadata"):
                snapshot = json.loads(encoded)
                for url, annotation in snapshot.get("fallback_annotations", {}).items():
                    result[url] = ArticleAnnotation(**annotation)
            if hasattr(self, "_snapshot_connection"):
                self._snapshot_annotations = result
            return result

    @contextmanager
    def public_snapshot(self):
        if self.static_snapshot is not None:
            yield self
            return
        if hasattr(self, "_snapshot_connection"):
            yield self
            return
        with self.connect() as connection:
            self._snapshot_connection = connection
            try:
                yield self
            finally:
                del self._snapshot_connection
                if hasattr(self, "_snapshot_annotations"):
                    del self._snapshot_annotations
                if hasattr(self, "_snapshot_reports"):
                    del self._snapshot_reports

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if hasattr(self, "_snapshot_connection"):
            yield self._snapshot_connection
            return
        if self.database is None or not self.database.is_file():
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
            connection.execute("BEGIN")
            self._validate_contract(connection)
            from .publication import public_connection
            projection = public_connection(connection) if self.public else connection
            previous_source = getattr(self, "_snapshot_source_connection", None)
            if projection is not connection:
                self._snapshot_source_connection = connection
            try:
                yield projection
            finally:
                if projection is not connection:
                    projection.close()
                    if previous_source is None:
                        del self._snapshot_source_connection
                    else:
                        self._snapshot_source_connection = previous_source
        except RegistryContractError:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise RegistryContractError("invalid public Registry snapshot") from exc
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

    @_public_read
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

    @_public_read
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

    @_public_read
    def publishers(self) -> dict[str, Any]:
        with self.connect() as connection:
            if self.public and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                identities=[row[0] for row in connection.execute("SELECT article_id FROM articles")]
                approved=[self.article(identity) for identity in identities]
                choices=sorted({(item["source"],_publisher_label(item["source"],item["publisher"])) for item in approved},key=lambda value:(value[0].casefold(),value[1]))
                return {"items":[{"hostname":host,"label":label} for host,label in choices[:MAX_PUBLISHER_CHOICES]],
                    "total":len(choices),"truncated":len(choices)>MAX_PUBLISHER_CHOICES}
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

    @_public_read
    def pdf_reports_all(self, *, allowed_occurrence_ids: set[str] | None = None,
        allowed_calendar_ids: set[str] | None = None) -> list[dict[str, Any]]:
        if self.static_snapshot is not None and "pdf_reports" in self.static_snapshot:
            items = [dict(item) for item in self.static_snapshot["pdf_reports"]]
            if allowed_occurrence_ids is None and allowed_calendar_ids is None:
                return items
            articles, calendars = {}, {}
            for article in self.pdf_articles_all(include_linked=True,allowed_occurrence_ids=allowed_occurrence_ids):
                for occurrence in article.get("occurrences",[]):
                    if allowed_occurrence_ids is None or occurrence["occurrence_id"] in allowed_occurrence_ids:
                        articles.setdefault(occurrence["source_document_sha256"],set()).add(article["article_id"])
            for item in self.pdf_calendar_items_all(allowed_occurrence_ids=allowed_calendar_ids):
                sha = item["source_document_sha256"]
                calendars[sha] = calendars.get(sha,0) + 1
            return [{**item,"article_count":len(articles.get(item["document_sha256"],[])),"calendar_count":calendars.get(item["document_sha256"],0)}
                for item in items if item["document_sha256"] in articles or item["document_sha256"] in calendars]
        with self.connect() as connection:
            if not self._has_pdf_intake(connection):
                return []
            article_rows = connection.execute(
                "SELECT occurrence_id,source_document_sha256,article_id FROM pdf_intake_article_occurrences "
                "WHERE COALESCE(json_extract(occurrence_json, '$.summary_basis'), '') != 'verbatim_pdf_calendar_row'"
            ).fetchall()
            calendar_rows = connection.execute(
                "SELECT occurrence_id,source_document_sha256 FROM pdf_intake_calendar_items"
            ).fetchall()
            articles, calendars = {}, {}
            for row in article_rows:
                if allowed_occurrence_ids is None or row[0] in allowed_occurrence_ids:
                    articles.setdefault(row[1], set()).add(row[2])
            for row in calendar_rows:
                if allowed_calendar_ids is None or row[0] in allowed_calendar_ids:
                    calendars[row[1]] = calendars.get(row[1], 0) + 1
            items = []
            for row in connection.execute("SELECT document_sha256,filename,date_of_run,document_json FROM pdf_intake_documents"):
                sha = row["document_sha256"]
                if (allowed_occurrence_ids is not None or connection.execute("PRAGMA user_version").fetchone()[0] >= 21) and sha not in articles and sha not in calendars:
                    continue
                document = self._pdf_document_json(row["document_json"])
                items.append({"report_id": sha, "document_sha256": sha, "source_kind": "pdf",
                    "report_title": document.get("title") or row["filename"], "filename": row["filename"],
                    "report_date": row["date_of_run"], "article_count": len(articles.get(sha, [])),
                    "calendar_count": calendars.get(sha, 0), "page_count": document.get("page_count"),
                    "monitoring_status": "not_reported", "source_label": "PDF import"})
        return items

    @staticmethod
    def _pdf_document_json(value: str) -> dict[str, Any]:
        try:
            document = json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RegistryContractError("invalid PDF document data") from exc
        if not isinstance(document, dict):
            raise RegistryContractError("invalid PDF document data")
        return document

    @_public_read
    def pdf_report(self, document_sha256: str, *, allowed_occurrence_ids: set[str] | None = None,
        allowed_calendar_ids: set[str] | None = None, include_bytes: bool = False) -> dict[str, Any]:
        if len(document_sha256) != 64 or any(char not in "0123456789abcdef" for char in document_sha256):
            raise RegistryQueryError("invalid PDF document id")
        item = next((item for item in self.pdf_reports_all(allowed_occurrence_ids=allowed_occurrence_ids,
            allowed_calendar_ids=allowed_calendar_ids) if item["document_sha256"] == document_sha256), None)
        if item is None:
            raise RegistryNotFoundError("PDF report not found")
        if self.static_snapshot is not None and "pdf_report_details" in self.static_snapshot:
            stored = self.static_snapshot["pdf_report_details"].get(document_sha256)
            if include_bytes or stored is None:
                raise RegistryNotFoundError("PDF source is not available in the Git snapshot")
            from copy import deepcopy
            payload = deepcopy(stored)
            if allowed_occurrence_ids is not None:
                for article in payload["articles"]:
                    article["occurrences"] = [row for row in article["occurrences"] if row["occurrence_id"] in allowed_occurrence_ids]
                payload["articles"] = [article for article in payload["articles"] if article["occurrences"]]
            if allowed_calendar_ids is not None:
                payload["calendar_items"] = [row for row in payload["calendar_items"] if row["occurrence_id"] in allowed_calendar_ids]
            return {**payload,**item,"report_pdf":None}
        with self.connect() as connection:
            if self.public and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                visible_ids = {row[0] for row in connection.execute("SELECT occurrence_id FROM pdf_intake_article_occurrences WHERE source_document_sha256=? UNION SELECT occurrence_id FROM pdf_intake_calendar_items WHERE source_document_sha256=?", (document_sha256, document_sha256))}
                original = self._snapshot_source_connection
                source_ids = {row[0] for row in original.execute("SELECT occurrence_id FROM pdf_intake_article_occurrences WHERE source_document_sha256=? UNION SELECT occurrence_id FROM pdf_intake_calendar_items WHERE source_document_sha256=?", (document_sha256, document_sha256))}
                if visible_ids != source_ids:
                    raise RegistryNotFoundError("PDF source contains unapproved or invisible observations")
            row = dict(connection.execute("SELECT * FROM pdf_intake_documents WHERE document_sha256=?", (document_sha256,)).fetchone())
            if include_bytes:
                return dict(item, pdf_bytes=row.get("original_pdf"))
            document = self._pdf_document_json(row["document_json"])
            articles = []
            for article_row in connection.execute("""SELECT * FROM pdf_intake_articles a WHERE EXISTS (
                SELECT 1 FROM pdf_intake_article_occurrences o WHERE o.article_id=a.article_id AND o.source_document_sha256=?)""", (document_sha256,)):
                payload = self._pdf_article_payload(connection, article_row, allowed_occurrence_ids=allowed_occurrence_ids)
                occurrences = deduplicate_pdf_occurrences([value for value in payload["occurrences"]
                    if value.get("source_document_sha256") == document_sha256])
                if occurrences:
                    articles.append({**payload, "occurrences": occurrences})
            source = document.get("source") or {}
            item.update({"edition": document.get("edition"), "reporting_period": document.get("reporting_period"),
                "period_start": row["period_start"], "period_end": row["period_end"],
                "executive_summary": [document["executive_summary"]] if document.get("executive_summary") else [],
                "pdf_metadata": source.get("pdf_metadata") or {}, "pdf_created_at": row.get("pdf_created_at"),
                "pdf_modified_at": row.get("pdf_modified_at"), "imported_at": row["imported_at"],
                "source_filenames": sorted({value["filename"] for value in self._pdf_document_sources(connection, document_sha256)}),
                "pages": [{"page": page.get("page"), "text": page.get("text")} for page in document.get("pages", [])],
                "articles": articles,
                "report_pdf": {"filename": row["filename"], "download_url": f"/api/registry/pdf-intake/reports/{document_sha256}/pdf"}
                    if row.get("original_pdf") is not None else None})
            document_calendar_ids = {value[0] for value in connection.execute(
                "SELECT occurrence_id FROM pdf_intake_calendar_items WHERE source_document_sha256=?", (document_sha256,))}
        item["calendar_items"] = [value for value in self.pdf_calendar_items_all(
            allowed_occurrence_ids=document_calendar_ids if allowed_calendar_ids is None else document_calendar_ids & allowed_calendar_ids)
            if value.get("source_document_sha256") == document_sha256]
        for occurrence in [value for article in item["articles"] for value in article["occurrences"]] + item["calendar_items"]:
            occurrence["source_observations"] = [{key: value for key, value in observation.items() if key != "path"}
                for observation in occurrence.get("source_observations", [])]
        return item

    @_public_read
    def report(self, report_date: str) -> dict[str, Any]:
        payload, _identity = self.report_with_identity(report_date)
        return payload

    @_public_read
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
            manual_enrichments = (
                connection.execute("PRAGMA user_version").fetchone()[0] >= 20
            )
            approved_display = self.public and connection.execute("PRAGMA user_version").fetchone()[0] >= 21
            manual_projection = (
                "ae.article_version_id AS enrichment_article_version_id"
                if manual_enrichments
                else "NULL AS enrichment_article_version_id"
            )
            manual_match = (
                " OR (candidate.content_version_id IS NULL "
                "AND candidate.article_id = a.article_id "
                "AND candidate.article_version_id = av.version_id)"
                if manual_enrichments
                else ""
            )
            appearances = connection.execute(
                f"""
                SELECT ra.ordinal, ra.section, ra.pillar, ra.observation_status,
                       a.article_id, a.canonical_url, av.observed_title AS title,
                       av.observed_summary AS summary, s.display_name AS publisher,
                       s.hostname AS source,
                       ae.enrichment_id AS content_enrichment_id,
                       ae.generator_kind AS enrichment_generator_kind,
                       {manual_projection},
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
                    WHERE ((candidate.content_version_id = a.current_content_version_id
                            AND a.current_content_version_id IS NOT NULL)
                       {manual_match})
                      AND candidate.status = 'complete'
                    ORDER BY (candidate.content_version_id IS NOT NULL) DESC,
                             candidate.generated_at DESC, candidate.enrichment_id DESC
                    LIMIT 1
                )
                WHERE ra.report_id = ?
                ORDER BY ra.ordinal, a.article_id
                """,
                (report["report_id"],),
            ).fetchall()
        source_report = self._source_report(
            report["report_date"], report["filename"], report["report_sha256"],archive=True
        )
        annotations = self._annotations()
        articles = []
        for row in appearances:
            item = dict(row)
            has_db_enrichment = item.pop("content_enrichment_id") is not None
            enrichment_generator_kind = item.pop("enrichment_generator_kind")
            item.pop("enrichment_article_version_id")
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
                provenance = (
                    "manual_enrichment"
                    if enrichment_generator_kind == "manual"
                    else "content_enrichment"
                )
                item["summary_provenance"] = provenance
                item["categories"] = enrichment_categories
                item["keywords"] = enrichment_keywords
                item["metadata_provenance"] = {
                    "categories": provenance,
                    "keywords": provenance,
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
            if approved_display:
                detail = self.article(item["article_id"])
                for field in ("title","summary","summary_provenance","categories","keywords","metadata_provenance",
                    "source_annotation","publisher","source","canonical_url"):
                    item[field] = detail.get(field)
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

    @_public_read
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

    @_public_read
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
        if self.static_snapshot is not None:
            items = [dict(item) for item in self.static_snapshot.get("articles", [])
                if (not query or query.casefold() in " ".join(str(item.get(key) or "") for key in
                    ("title", "summary", "canonical_url", "categories", "keywords")).casefold())
                and (not source or source in {item.get("source"), item.get("publisher")})
                and (not report_date or any(row.get("report_date") == report_date for row in item.get("appearances", [])))
                and (not pillar or any(row.get("pillar") == pillar for row in item.get("appearances", [])))]
            offset = (page - 1) * page_size
            return {"items": items[offset:offset + page_size], "pagination": _pagination(page, page_size, len(items))}
        with self.connect() as connection:
            published = self.public and connection.execute("PRAGMA user_version").fetchone()[0] >= 21
        if published:
            items = []
            with self.public_snapshot():
                with self.connect() as connection:
                    identities = connection.execute("SELECT article_id,source_id FROM articles").fetchall()
                for identity,source_id in identities:
                    detail = self.article(identity)
                    if query.strip() and query.strip().casefold() not in " ".join(str(detail.get(key) or "") for key in ("title", "summary", "canonical_url", "categories", "keywords")).casefold():
                        continue
                    if source.strip() and source.strip() not in {detail["source"], detail["publisher"], source_id}:
                        continue
                    appearances = detail.get("appearances", [])
                    if report_date and not any(item["report_date"] == report_date for item in appearances):
                        continue
                    if pillar and not any(item["pillar"] == pillar and (not report_date or item["report_date"] == report_date) for item in appearances):
                        continue
                    item = {key: detail[key] for key in ("article_id", "canonical_url", "first_seen", "last_seen", "document_kind", "publication_eligible", "display_policy", "source", "publisher", "title", "report_summary")}
                    item.update(summary=detail["summary"], categories=detail["categories"], keywords=detail["keywords"],
                        pdf_occurrence_count=len(detail.get("pdf_occurrences", [])), is_visible=True,
                        published_candidate_sha256=detail["published_candidate_sha256"])
                    items.append(item)
            items.sort(key=lambda item: (item["last_seen"], item["first_seen"], item["article_id"]), reverse=True)
            offset = (page - 1) * page_size
            return {"items": items[offset:offset+page_size], "pagination": _pagination(page, page_size, len(items))}
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
        allowed_occurrence_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not RegistryReader._has_pdf_intake(connection):
            return []
        if pdf_article_id is not None:
            rows = connection.execute(
                """SELECT o.occurrence_id, o.occurrence_json, o.source_document_sha256, o.page, o.raw_url, o.publication_date, o.content_sha256 FROM pdf_intake_article_occurrences o
                   WHERE o.article_id=?
                   ORDER BY COALESCE(o.report_date, '' ) DESC, o.page DESC, o.occurrence_id DESC""",
                (pdf_article_id,),
            ).fetchall()
        elif RegistryReader._has_pdf_article_links(connection):
            rows = connection.execute(
                """SELECT o.occurrence_id, o.occurrence_json, o.source_document_sha256, o.page, o.raw_url, o.publication_date, o.content_sha256 FROM pdf_intake_article_occurrences o
                   JOIN pdf_intake_articles a ON a.article_id=o.article_id
                   WHERE a.core_article_id=? OR (a.canonical_url=? AND ?)
                   ORDER BY COALESCE(o.report_date, '' ) DESC, o.page DESC, o.occurrence_id DESC""",
                (article_id,canonical_url,bool(connection.execute("SELECT 1 FROM sqlite_temp_master WHERE name='public_snapshot_metadata'").fetchone())),
            ).fetchall()
        else:
            rows = connection.execute(
                """SELECT o.occurrence_id, o.occurrence_json, o.source_document_sha256, o.page, o.raw_url, o.publication_date, o.content_sha256 FROM pdf_intake_article_occurrences o
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
            value["source_document_sha256"] = row["source_document_sha256"]
            for field in ("page", "raw_url", "publication_date", "content_sha256"):
                value[field] = row[field]
            value["source_observations"] = sources[row["source_document_sha256"]]
        if connection.execute("SELECT 1 FROM sqlite_temp_master WHERE name='public_snapshot_metadata'").fetchone():
            from climate_monitor.pdf_intake import report_update_fields
            documents={sha:dict(connection.execute("SELECT filename,period_start,period_end,imported_at,document_json FROM pdf_intake_documents WHERE document_sha256=?",(sha,)).fetchone()) for sha in sources}
            for row,value in zip(rows,values):
                document=documents[row["source_document_sha256"]]
                parsed=json.loads(document["document_json"])
                value.update(source_filename=document["filename"],period_start=document["period_start"],period_end=document["period_end"],imported_at=document["imported_at"],
                    report_fields=report_update_fields(value,parsed),structured_report=any("IN WINDOW" in page["text"] for page in parsed.get("pages",[])))
        from .information_checks import latest_checks
        return [dict(value, **latest_checks(connection, "articles", value)) for value in values
            if value.get("summary_basis") != "verbatim_pdf_calendar_row"
            and (allowed_occurrence_ids is None or value["occurrence_id"] in allowed_occurrence_ids)]

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
            allowed_occurrence_ids=allowed_occurrence_ids,
        )
        latest = occurrences[0] if occurrences else {}
        try:
            classification = json.loads(row["type_safe_classification_json"]) if row["type_safe_classification_json"] else None
        except (json.JSONDecodeError, TypeError) as exc:
            raise RegistryContractError("invalid PDF article classification data") from exc
        host = (urlsplit(canonical_url).hostname or "unknown").removeprefix("www.").lower()
        dates = [item.get("report_date") for item in occurrences if item.get("report_date")]
        payload = {
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
        if connection.execute("SELECT 1 FROM sqlite_temp_master WHERE name='public_snapshot_metadata'").fetchone():
            payload.update(core_article_id=core_article_id,confirmation_basis=row["confirmation_basis"])
        from .publication import snapshot_metadata
        snapshot = snapshot_metadata(connection, "pdf_article", row["article_id"])
        supplements = [item for item in (snapshot or {}).get("supplements", []) if item["source_ref"].startswith("pdf-classification:") and item["entity_id"] == row["article_id"]]
        if supplements:
            evidence = json.loads(supplements[-1]["evidence_json"])
            pending = evidence["pending_enrichment"]
            payload.update(categories=pending["categories"], keywords=pending["keywords"],
                manual_enrichment={**pending, "generator_kind": "manual",
                    "evidence": evidence, "knowledge_id": supplements[-1]["knowledge_id"]})
        return payload

    @_public_read
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

    @_public_read
    def pdf_articles_all(self, *, allowed_occurrence_ids: set[str] | None = None,
        include_linked: bool = False) -> list[dict[str, Any]]:
        """Read full PDF article observations before a caller merges and paginates."""
        if self.static_snapshot:
            core_urls = {item["canonical_url"] for item in self.static_snapshot["articles"] if item["publication_eligible"] and item["document_kind"] == "article"}
            return [item for item in self.static_snapshot["pdf_articles"] if include_linked or item["canonical_url"] not in core_urls]

        with self.connect() as connection:
            if not self._has_pdf_intake(connection):
                return []
            rows = connection.execute(
                "SELECT * FROM pdf_intake_articles" if include_linked else (
                "SELECT * FROM pdf_intake_articles WHERE core_article_id IS NULL"
                if self._has_pdf_article_links(connection) else
                """SELECT * FROM pdf_intake_articles
                   WHERE NOT EXISTS (
                       SELECT 1 FROM articles core WHERE core.canonical_url=pdf_intake_articles.canonical_url
                   )"""
                )
            ).fetchall()
            items = []
            for row in rows:
                item = self._pdf_article_payload(connection, row, allowed_occurrence_ids=allowed_occurrence_ids)
                if not item["occurrences"]:
                    continue
                items.append(item)
        items.sort(key=lambda item: (item["last_seen"] or "", item["article_id"]), reverse=True)
        return items

    @_public_read
    def pdf_article(self, article_id: str, *, allowed_occurrence_ids: set[str] | None = None) -> dict[str, Any]:
        validate_article_id(article_id)
        if self.static_snapshot:
            item = next((item for item in self.pdf_articles_all(include_linked=True) if item["article_id"] == article_id), None)
            if item is None:
                raise RegistryNotFoundError("article not found")
            return item
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

    @_public_read
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

    @_public_read
    def pdf_calendar_items_all(
        self, *, allowed_occurrence_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Recover each allow-listed source PDF once, then return all calendar rows."""
        if self.static_snapshot:
            return [item for item in self.static_snapshot["meetings"] if allowed_occurrence_ids is None or item["occurrence_id"] in allowed_occurrence_ids]
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

    @_public_read
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

    @_public_read
    def knowledge_chronology(self) -> dict[str, dict[str, Any]]:
        """Only the chronology frozen in the approved public projection."""
        if self.static_snapshot is not None:
            return dict(self.static_snapshot.get("knowledge_chronology", {}))
        with self.connect() as connection:
            if not self.public or connection.execute("PRAGMA user_version").fetchone()[0] < 21:
                return {}
            rows = connection.execute("""SELECT entity_kind,entity_id,knowledge_id,
                material_sha256,first_ingested_at,substantive_updated_at,time_basis
                FROM knowledge_versions ORDER BY rowid""").fetchall()
        return {f"{row['entity_kind']}:{row['entity_id']}": {
            key: row[key] for key in ("knowledge_id", "material_sha256", "first_ingested_at",
                "substantive_updated_at", "time_basis")} for row in rows}

    @_public_read
    def meetings_all(self, *, base_date: str):
        """Read every approved meeting within the caller's frozen public view."""
        items, page = [], 1
        while True:
            payload = self.meetings(base_date=base_date,page=page,page_size=100)
            items.extend(payload["items"])
            if page >= payload["pagination"]["pages"]:
                return items
            page += 1

    @_public_read
    def meetings(self, *, page: int = 1, page_size: int = 20, query: str = "",
        verification_status: str = "", base_date: str | None = None,
        additional_calendar_items: list[dict[str, Any]] | None = None,
        additional_events: list[dict[str, Any]] | None = None,
        timezone_name: str = "UTC", include_unknown: bool = False) -> dict[str, Any]:
        """Meetings have their own reader; verified imports join collected records."""
        from climate_monitor.meetings import query_events
        from .range_reports import _calendar_end_date
        page, page_size = validate_page(page, page_size)
        if len(query) > 200 or verification_status not in {"", "unchecked", "partial", "conflict", "verified"}:
            raise RegistryQueryError("invalid meeting filter")
        base_date = validate_report_date(base_date or datetime.now(timezone.utc).date().isoformat())
        if self.static_snapshot is not None:
            records = self.static_snapshot.get("meeting_records",[])
            current = [item for item in records if (_calendar_end_date(item) is None or _calendar_end_date(item)>=date.fromisoformat(base_date))]
            items=[dict(item) for item in current if (not verification_status or item.get("verification_status")==verification_status)
                and (not query or query.casefold() in " ".join(str(item.get(key) or "") for key in ("name","organizer","relevance_reason","raw_date")).casefold())]
            items.sort(key=lambda item:(item.get("start_date") or item.get("deadline_date") or "9999",item.get("name") or ""))
            offset=(page-1)*page_size
            stored = self.static_snapshot.get("meeting_coverage") or {}
            from .publication import public_meeting_coverage
            coverage = {**public_meeting_coverage(stored), "source": "approved_git_snapshot"}
            if "returned_record_count" in coverage:
                coverage["returned_record_count"] = sum(item.get("origin") == "web_collection" for item in current)
            return {"items":items[offset:offset+page_size],"pagination":_pagination(page,page_size,len(items)),"base_date":base_date,
                "timezone":timezone_name,"coverage":coverage,
                "verification_counts":{status:sum(item.get("verification_status")==status for item in current) for status in ("unchecked","partial","conflict","verified")}}
        with self.connect() as connection:
            payload = query_events(self.database, base_date=base_date, timezone_name=timezone_name,
                include_deadlines=True, include_unknown=include_unknown,
                **({"registry_connection": connection, "coverage_connection": getattr(self, "_snapshot_source_connection", connection)} if connection.execute("PRAGMA user_version").fetchone()[0] >= 21 else {}))
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

    @_public_read
    def article(self, article_id: str) -> dict[str, Any]:
        if not article_id or len(article_id) > 128 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in article_id):
            raise RegistryQueryError("invalid article id")
        if self.static_snapshot is not None:
            item = next((item for item in self.static_snapshot.get("articles", []) if item["article_id"] == article_id), None)
            if item is None:
                raise RegistryNotFoundError("article not found")
            return dict(item)
        with self.connect() as connection:
            if self.public:
                from .publication import snapshot_metadata
                snapshot = snapshot_metadata(connection, "article", article_id)
                if snapshot and snapshot.get("static_payload"):
                    payload = dict(snapshot["static_payload"])
                    from .article_dates import ArticleDateImportError, select_article_dates
                    try:
                        payload.update(select_article_dates(payload.get("date_observations", []),
                            collection_times=[payload.get("collected_at"),
                                (payload.get("available_content") or {}).get("collected_at")],
                            acquisition_observations=payload.get("acquisition_observations", [])))
                    except ArticleDateImportError as exc:
                        raise RegistryContractError("invalid article date evidence") from exc
                    return payload
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
            manual_enrichments = (
                connection.execute("PRAGMA user_version").fetchone()[0] >= 20
            )
            article_version_column = (
                "article_version_id" if manual_enrichments else "NULL AS article_version_id"
            )
            manual_match = (
                " OR (content_version_id IS NULL AND article_id = ? "
                "AND article_version_id = ?)"
                if manual_enrichments
                else ""
            )
            enrichment_params = (
                (article["current_content_version_id"], article["current_content_version_id"],
                 article_id, article["current_version_id"])
                if manual_enrichments
                else (article["current_content_version_id"], article["current_content_version_id"])
            )
            enrichment = connection.execute(
                f"""
                SELECT summary, categories_json, keywords_json, language, generator_kind,
                       generator_name, generator_version, generated_at, {article_version_column}
                FROM article_enrichments
                WHERE status = 'complete'
                  AND ((content_version_id = ? AND ? IS NOT NULL){manual_match})
                ORDER BY (content_version_id IS NOT NULL) DESC,
                         generated_at DESC, enrichment_id DESC LIMIT 1
                """,
                enrichment_params,
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
        from .article_dates import ArticleDateImportError, select_article_dates
        try:
            date_fields = select_article_dates(date_observations,
                collection_times=[available_content.get("collected_at")],
                acquisition_observations=acquisition_observations)
        except ArticleDateImportError as exc:
            raise RegistryContractError("invalid article date evidence") from exc
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
        annotations = self._annotations()
        source_annotation: ArticleAnnotation | None = self._annotation_for_url(annotations, article["canonical_url"])
        if source_annotation:
            annotation_summary = source_annotation.summary
            fallback_categories = list(source_annotation.categories)
            fallback_keywords = list(source_annotation.keywords)
            fallback_provenance = source_annotation.provenance
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
            "article_version_id": enrichment["article_version_id"] if enrichment else None,
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
            "categories": (
                "manual_enrichment" if has_db_enrichment and enrichment["generator_kind"] == "manual"
                else "content_enrichment" if has_db_enrichment else fallback_provenance
            ),
            "keywords": (
                "manual_enrichment" if has_db_enrichment and enrichment["generator_kind"] == "manual"
                else "content_enrichment" if has_db_enrichment else fallback_provenance
            ),
        }
        original_url = appearance_payload[0]["original_url"] if appearance_payload else article["canonical_url"]
        payload = {
            "article_id": article["article_id"],
            "title": source_annotation.title if source_annotation else article["title"],
            "summary": summary,
            "summary_provenance": (
                "manual_enrichment" if has_db_enrichment and enrichment["generator_kind"] == "manual"
                else "content_enrichment"
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
            **date_fields,
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
            from .publication import snapshot_metadata
            snapshot = snapshot_metadata(connection, "article", article_id)
            if snapshot:
                from .publication import approved_display
                payload.update(approved_display(snapshot))
                if snapshot.get("sources"):
                    payload.update(source=snapshot["sources"][0]["hostname"], publisher=snapshot["sources"][0]["display_name"])
                payload["is_visible"] = True
                from .acquisition_review import digest
                payload["published_candidate_sha256"] = digest(snapshot)
                if snapshot.get("git_wiki"):
                    payload["available_content"] = {"markdown": snapshot["git_wiki"]["markdown"],
                        "selection_basis": "approved_git_wiki", "static_snapshot_sha256": snapshot["git_wiki"]["sha256"]}
        if pdf_occurrences:
            payload["pdf_occurrences"] = pdf_occurrences
        return payload
