from __future__ import annotations

import json
import hashlib
from copy import deepcopy
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
from climate_monitor.publisher_mapping import publisher_name

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


def select_snapshot_article_content(snapshot, article_id, current_version_id):
    """Select the body exposed by available_content, without observation rendering."""
    tables = snapshot.get("tables", {})
    fetches = {row.get("fetch_id"): row for row in tables.get("article_fetches", [])}
    candidates = []
    for row in sorted(tables.get("acquisition_items", []), key=lambda value:
        (value.get("discovered_at") or "", value.get("acquisition_item_id") or "")):
        if row.get("article_id") != article_id:
            continue
        fetch = fetches.get(row.get("fetch_id"), {})
        resolved = fetches.get(row.get("resolved_by_fetch_id"), {})
        version_id = fetch.get("content_version_id") if fetch.get("fetch_status") == "success" else None
        fetched_at = fetch.get("fetched_at") if version_id else None
        if version_id is None and resolved.get("fetch_status") == "success":
            version_id, fetched_at = resolved.get("content_version_id"), resolved.get("fetched_at")
        if version_id and row.get("material_status") == "full_content" and row.get("processing_status") == "complete":
            candidates.append({"content_version_id": version_id, "fetched_at": fetched_at,
                "fetch_id": row.get("fetch_id"), "acquisition_item_id": row.get("acquisition_item_id")})
    if current_version_id:
        current = max((row for row in tables.get("article_fetches", []) if row.get("article_id") == article_id
            and row.get("content_version_id") == current_version_id and row.get("fetch_status") == "success"),
            key=lambda row: (row.get("fetched_at") or "", row.get("fetch_id") or ""), default=None)
        candidates.append({"content_version_id": current_version_id, "fetched_at": current.get("fetched_at") if current else None,
            "fetch_id": current.get("fetch_id") if current else None, "acquisition_item_id": None})
    if not candidates:
        return {}, None
    selected = max(candidates, key=lambda item: (RegistryReader._timestamp_key(item["fetched_at"]),
        item["content_version_id"] == current_version_id, item["content_version_id"], item.get("fetch_id") or ""))
    content = next((row for row in tables.get("article_content_versions", []) if row.get("article_id") == article_id
        and row.get("content_version_id") == selected["content_version_id"]), None)
    return selected, content


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


def _complete_source_report_bundle(article: ParsedArticle | None) -> tuple[str, list[str], list[str]] | None:
    """Return report metadata only when summary, categories, and keywords are complete."""
    if article is None or not article.summary.strip() or not article.categories or not article.keywords:
        return None
    return article.summary, list(article.categories), list(article.keywords)


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
    selected = filter_pdf_articles(items, query=query, source=source, report_date=report_date)
    total = len(selected)
    offset = (page - 1) * page_size
    return {"items": selected[offset:offset + page_size], "pagination": _pagination(page, page_size, total)}


def filter_pdf_articles(
    items: list[dict[str, Any]], *, query: str = "", source: str = "", report_date: str = "",
) -> list[dict[str, Any]]:
    """Filter an already assembled PDF article collection without paging it repeatedly."""
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
    return selected


def filter_article_cards(
    items: list[dict[str, Any]], *, query: str = "", source: str = "", pillar: str = "", report_date: str = "",
    include_internal: bool = False,
) -> list[dict[str, Any]]:
    """Filter an assembled approved core collection, optionally retaining appearances for merging."""
    if len(query) > 200 or len(source) > 253:
        raise RegistryQueryError("filter is too long")
    if pillar and pillar not in {"A", "B"}:
        raise RegistryQueryError("invalid pillar")
    if report_date:
        validate_report_date(report_date)
    query_value, source_value = query.strip().casefold(), source.strip()
    selected = []
    for item in items:
        if query_value and query_value not in " ".join(str(item.get(key) or "") for key in
            ("title", "summary", "canonical_url", "categories", "keywords")).casefold():
            continue
        if source_value and source_value not in {item.get("source"), item.get("publisher"), item.get("source_id")}:
            continue
        appearances = item.get("appearances", [])
        if (report_date or pillar) and not any(
            (not report_date or row.get("report_date") == report_date)
            and (not pillar or row.get("pillar") == pillar) for row in appearances
        ):
            continue
        selected.append(dict(item) if include_internal else {
            key: value for key, value in item.items() if key not in {"appearances", "source_id"}
        })
    selected.sort(key=lambda item: (item.get("last_seen") or "", item.get("first_seen") or "", item["article_id"]), reverse=True)
    return selected


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
    return publisher_name(display_name, [hostname])


def _public_read(function):
    @wraps(function)
    def read(self, *args, **kwargs):
        if self.public and not hasattr(self, "_snapshot_connection"):
            with self.public_snapshot():
                return function(self, *args, **kwargs)
        return function(self, *args, **kwargs)
    return read


def _direct_public_read(function):
    @wraps(function)
    def read(self, *args, **kwargs):
        if self.public and self.static_snapshot is None and not hasattr(self, "_snapshot_connection"):
            with self.public_snapshot(direct=True):
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
    def public_snapshot(self, *, direct: bool = False, manage_wal: bool = False, source_connection=None):
        if self.static_snapshot is not None:
            yield self
            return
        if hasattr(self, "_snapshot_connection"):
            yield self
            return
        if direct:
            from contextlib import nullcontext
            # A category correction already owns the source transaction. A second
            # reader would block its writer when a large snapshot spills pages.
            with (nullcontext(source_connection) if source_connection is not None
                    else self._source_connection(manage_wal=manage_wal)) as connection:
                self._snapshot_connection = connection
                self._snapshot_direct = True
                self._approved_snapshot_cache = {}
                self._approved_candidate_cache = {}
                self._approved_candidate_sha_by_entity = {}
                self._approved_article_cards = None
                self._pdf_document_cache = {}
                self._pdf_source_cache = {}
                self._pdf_article_collection_cache = {}
                self._pdf_calendar_collection_cache = {}
                self._meeting_records_cache = {}
                self._direct_meeting_collection_cache = {}
                self._pdf_check_packet_cache = {}
                self._legacy_acquisition_search_cache = {}
                self._legacy_acquisition_searches_loaded = set()
                try:
                    yield self
                finally:
                    del self._snapshot_connection
                    del self._snapshot_direct
                    del self._approved_snapshot_cache
                    del self._approved_candidate_cache
                    del self._approved_candidate_sha_by_entity
                    del self._approved_article_cards
                    del self._pdf_document_cache
                    del self._pdf_source_cache
                    del self._pdf_article_collection_cache
                    del self._pdf_calendar_collection_cache
                    del self._meeting_records_cache
                    del self._direct_meeting_collection_cache
                    del self._pdf_check_packet_cache
                    del self._legacy_acquisition_search_cache
                    del self._legacy_acquisition_searches_loaded
                    for name in ("_snapshot_annotations", "_snapshot_reports"):
                        if hasattr(self, name):
                            delattr(self, name)
            return
        with self.connect() as connection:
            self._snapshot_connection = connection
            try:
                yield self
            finally:
                del self._snapshot_connection
                for name in ("_snapshot_annotations", "_snapshot_reports"):
                    if hasattr(self, name):
                        delattr(self, name)

    @contextmanager
    def _source_connection(self, *, manage_wal: bool = False, validate: bool = True) -> Iterator[sqlite3.Connection]:
        if self.database is None or not self.database.is_file():
            raise RegistryUnavailableError("registry database is unavailable")
        connection = None
        try:
            connection = sqlite3.connect(
                f"{self.database.as_uri()}?mode={'rw' if manage_wal else 'ro'}", uri=True, timeout=2,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("BEGIN")
            if validate:
                self._validate_contract(connection)
            yield connection
        except RegistryContractError:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise RegistryContractError("invalid public Registry snapshot") from exc
        except (OSError, sqlite3.Error) as exc:
            raise RegistryUnavailableError("registry database is unavailable") from exc
        finally:
            if connection is not None:
                connection.close()

    def _public_candidates(self, *kinds: str, entity_ids=None) -> dict[tuple[str, str], dict[str, Any]]:
        if not getattr(self, "_snapshot_direct", False):
            return {}
        kinds_key = tuple(sorted(set(kinds)))
        ids_key = tuple(sorted((kind, tuple(sorted(set(values)))) for kind, values in entity_ids.items())) if isinstance(entity_ids, dict) else tuple(sorted(set(entity_ids))) if entity_ids is not None else None
        key = (kinds_key, ids_key)
        cached = self._approved_snapshot_cache.setdefault(key, None)
        if cached is None:
            requested_ids = ({(kind, identity) for kind, values in entity_ids.items() for identity in set(values)}
                if isinstance(entity_ids, dict) else {(kind, identity) for kind in kinds_key for identity in set(entity_ids)}
                if entity_ids is not None else None)
            for (available_kinds, available_ids), available in self._approved_snapshot_cache.items():
                if available is None or not set(kinds_key) <= set(available_kinds):
                    continue
                if available_ids is not None:
                    if requested_ids is None:
                        continue
                    covered = ({(kind, identity) for kind, values in available_ids for identity in values}
                        if available_ids and isinstance(available_ids[0], tuple) else
                        {(kind, identity) for kind in available_kinds for identity in available_ids})
                    if not requested_ids <= covered:
                        continue
                cached = {identity: snapshot for identity, snapshot in available.items()
                    if identity[0] in kinds_key and (requested_ids is None or identity in requested_ids)}
                self._approved_snapshot_cache[key] = cached
                return cached
            try:
                from .publication import published_snapshots
                cached = published_snapshots(self._snapshot_connection, kinds_key, entity_ids=entity_ids,
                    candidate_cache=self._approved_candidate_cache,
                    candidate_sha_out=self._approved_candidate_sha_by_entity,
                    packet_cache=self._pdf_check_packet_cache)
            except (ValueError, KeyError, TypeError) as exc:
                raise RegistryContractError("invalid published Registry candidate") from exc
            self._approved_snapshot_cache[key] = cached
        return cached

    def _published_candidate_sha(self, snapshot: dict[str, Any]) -> str:
        key = (snapshot.get("entity_kind"), snapshot.get("entity_id"))
        cached = getattr(self, "_approved_candidate_sha_by_entity", {}).get(key)
        if cached:
            return cached
        from .publication import digest
        return digest(snapshot)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if hasattr(self, "_snapshot_connection"):
            yield self._snapshot_connection
            return
        with self._source_connection() as connection:
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

    @_direct_public_read
    def status(self) -> dict[str, Any]:
        with self.connect() as connection:
            if self.public and getattr(self, "_snapshot_direct", False) and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                candidates = self._public_candidates("article")
                discoveries = {row.get("discovery_id") for snapshot in candidates.values()
                    for row in snapshot.get("tables", {}).get("discoveries", [])}
                return {
                    "available": True,
                    "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
                    "reports": connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
                    "articles": len(candidates),
                    "discoveries": len(discoveries),
                    "latest_report_date": connection.execute("SELECT MAX(report_date) FROM reports").fetchone()[0],
                }
            return {
                "available": True,
                "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
                "reports": connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
                "articles": connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0],
                "discoveries": connection.execute("SELECT COUNT(*) FROM discoveries").fetchone()[0],
                "latest_report_date": connection.execute("SELECT MAX(report_date) FROM reports").fetchone()[0],
            }

    def approved_merged_article_count(self) -> int:
        """Count the approved core/PDF URL union without assembling article DTOs."""
        if self.static_snapshot is not None:
            urls = {item.get("canonical_url") for item in self.static_snapshot.get("articles", [])}
            urls.update(item.get("canonical_url") for item in self.static_snapshot.get("pdf_articles", [])
                if item.get("occurrences"))
            return len(urls - {None})
        with self.connect() as connection:
            if not (self.public and getattr(self, "_snapshot_direct", False)
                    and connection.execute("PRAGMA user_version").fetchone()[0] >= 21):
                raise RegistryContractError("approved article count requires a direct published snapshot")
            urls = set()
            for (kind, identity), snapshot in self._public_candidates("article", "pdf_article").items():
                tables = snapshot.get("tables", {})
                if kind == "article":
                    urls.update(row.get("canonical_url") for row in tables.get("articles", [])
                        if row.get("canonical_url"))
                    payload_url = (snapshot.get("static_payload") or {}).get("canonical_url")
                    if payload_url:
                        urls.add(payload_url)
                eligible_pdf_articles = {row.get("article_id") for row in tables.get("pdf_intake_article_occurrences", [])
                    if row.get("summary_basis") != "verbatim_pdf_calendar_row"}
                for article in tables.get("pdf_intake_articles", []):
                    article_id = article.get("article_id")
                    if ((kind == "pdf_article" and article_id != identity)
                            or article_id not in eligible_pdf_articles):
                        continue
                    if article.get("canonical_url"):
                        urls.add(article["canonical_url"])
            return len(urls)

    @_direct_public_read
    def reports(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        page, page_size = validate_page(page, page_size)
        with self.connect() as connection:
            direct_published = (self.public and getattr(self, "_snapshot_direct", False)
                and connection.execute("PRAGMA user_version").fetchone()[0] >= 21)
            total = connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
            rows = connection.execute(
                """
                SELECT r.report_id, r.report_date, r.report_title, r.cadence, r.report_format,
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
            visible_counts = self._approved_report_appearance_counts(
                connection, [row["report_id"] for row in rows]) if direct_published else {}
        items = []
        for row in rows:
            item = dict(row)
            if direct_published:
                item["article_count"] = visible_counts.get(item["report_id"], 0)
            item.pop("report_id", None)
            item["monitoring_status"] = _monitoring_status(row)
            items.append(item)
        return {"items": items, "pagination": _pagination(page, page_size, total)}

    def _approved_report_appearance_counts(self, connection, report_ids: list[str]) -> dict[str, int]:
        """Count only appearances in the exact visible, approved article candidates."""
        counts: dict[str, set[str]] = {identity: set() for identity in report_ids}
        for start in range(0, len(report_ids), 400):
            chunk = report_ids[start:start + 400]
            if not chunk:
                continue
            rows = connection.execute(
                "SELECT report_id,article_id FROM report_appearances WHERE report_id IN (" +
                ",".join("?" for _ in chunk) + ")", chunk).fetchall()
            article_ids = sorted({row["article_id"] for row in rows})
            for offset in range(0, len(article_ids), 500):
                candidates = self._public_candidates("article", entity_ids={
                    "article": article_ids[offset:offset + 500]})
                for row in rows:
                    snapshot = candidates.get(("article", row["article_id"]))
                    if snapshot and any(appearance.get("report_id") == row["report_id"]
                            and appearance.get("article_id") == row["article_id"]
                            for appearance in snapshot.get("tables", {}).get("report_appearances", [])):
                        counts[row["report_id"]].add(row["article_id"])
        return {identity: len(values) for identity, values in counts.items()}

    def approved_reports_all(self) -> list[dict[str, Any]]:
        """Read all public report cards in one query inside a direct snapshot."""
        with self.connect() as connection:
            if not (self.public and getattr(self, "_snapshot_direct", False)
                    and connection.execute("PRAGMA user_version").fetchone()[0] >= 21):
                raise RegistryContractError("approved report collection requires a direct published snapshot")
            rows = connection.execute(
                """SELECT r.report_id, r.report_date, r.report_title, r.cadence, r.report_format,
                          r.sites_checked, r.sites_succeeded, r.sites_failed,
                          COUNT(ra.article_id) AS article_count
                   FROM reports r LEFT JOIN report_appearances ra ON ra.report_id=r.report_id
                   GROUP BY r.report_id ORDER BY r.report_date DESC, r.report_id"""
            ).fetchall()
            counts = self._approved_report_appearance_counts(connection, [row["report_id"] for row in rows])
        return [{**{key: value for key, value in dict(row).items() if key != "report_id"},
            "article_count": counts.get(row["report_id"], 0),
            "monitoring_status": _monitoring_status(row)} for row in rows]

    @_direct_public_read
    def publishers(self, *, include_pdf: bool = False) -> dict[str, Any]:
        if self.static_snapshot is not None:
            articles = self.static_snapshot.get("articles", [])
            core_urls = {item.get("canonical_url") for item in articles}
            core_choices = sorted({(item.get("source", ""),
                _publisher_label(item.get("source", ""), item.get("publisher", ""))) for item in articles},
                key=lambda value: (value[0].casefold(), value[1]))
            if not include_pdf:
                choices = core_choices
            else:
                by_host = {}
                for hostname, label in core_choices[:MAX_PUBLISHER_CHOICES]:
                    by_host[hostname] = label
                for item in self.static_snapshot.get("pdf_articles", []):
                    if item.get("canonical_url") in core_urls or not item.get("occurrences"):
                        continue
                    hostname = item.get("source", "")
                    by_host.setdefault(hostname, _publisher_label(hostname, item.get("publisher", "")))
                choices = sorted(by_host.items(), key=lambda value: (value[0].casefold(), value[1]))
            return {"items": [{"hostname": host, "label": label} for host, label in choices[:MAX_PUBLISHER_CHOICES]],
                "total": len(choices), "truncated": len(choices) > MAX_PUBLISHER_CHOICES}
        with self.connect() as connection:
            if self.public and getattr(self, "_snapshot_direct", False) and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                candidates = self._public_candidates(*( ("article", "pdf_article") if include_pdf else ("article",) ))
                core_urls = set()
                core_sources = []
                for (kind, _identity), snapshot in candidates.items():
                    if kind != "article":
                        continue
                    payload = snapshot.get("static_payload") or {}
                    rows = snapshot.get("tables", {}).get("articles", [])
                    canonical = payload.get("canonical_url") or (rows[0].get("canonical_url") if rows else None)
                    if canonical:
                        core_urls.add(canonical)
                    if payload:
                        hostname, display = payload.get("source", ""), payload.get("publisher", "")
                        source_id = payload.get("source_id", "")
                    elif rows:
                        article = rows[0]
                        source = next((item for item in snapshot.get("sources", []) if item.get("source_id") == article.get("source_id")), {})
                        hostname, display, source_id = source.get("hostname", ""), source.get("display_name", ""), article.get("source_id", "")
                    else:
                        continue
                    core_sources.append((hostname, source_id, _publisher_label(hostname, display)))
                if not include_pdf:
                    choices = sorted({(hostname, label) for hostname, _source_id, label in core_sources},
                        key=lambda value: (value[0].casefold(), value[1]))
                    return {"items": [{"hostname": host, "label": label}
                            for host, label in choices[:MAX_PUBLISHER_CHOICES]],
                        "total": len(choices), "truncated": len(choices) > MAX_PUBLISHER_CHOICES}
                by_host = {}
                core_choices = sorted({(hostname, label) for hostname, _source_id, label in core_sources},
                    key=lambda value: (value[0].casefold(), value[1]))[:MAX_PUBLISHER_CHOICES]
                for hostname, label in core_choices:
                    by_host[hostname] = label
                for (kind, _identity), snapshot in candidates.items():
                    if kind == "pdf_article":
                        if not any(row.get("summary_basis") != "verbatim_pdf_calendar_row"
                            for row in snapshot.get("tables", {}).get("pdf_intake_article_occurrences", [])):
                            continue
                        pdf = next(iter(snapshot.get("tables", {}).get("pdf_intake_articles", [])), {})
                        if pdf.get("canonical_url") in core_urls:
                            continue
                        hostname = (urlsplit(pdf.get("canonical_url", "")).hostname or "unknown").removeprefix("www.").lower()
                        by_host.setdefault(hostname, _publisher_label(hostname, hostname))
                choices = sorted(by_host.items(), key=lambda value: (value[0].casefold(), value[1]))
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

    @_direct_public_read
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
                for item in items]
        with self.connect() as connection:
            if self.public and getattr(self, "_snapshot_direct", False) and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                if not self._has_pdf_intake(connection):
                    return []
                candidates = self._public_candidates("article", "pdf_article", "meeting", "pdf_meeting")
                article_ids, calendar_ids = {}, {}
                for (_kind, _identity), snapshot in candidates.items():
                    for table in ("pdf_intake_article_occurrences",):
                        for row in snapshot.get("tables", {}).get(table, []):
                            if row.get("summary_basis") == "verbatim_pdf_calendar_row":
                                continue
                            occurrence_id = row.get("occurrence_id")
                            if allowed_occurrence_ids is None or occurrence_id in allowed_occurrence_ids:
                                article_ids.setdefault(row.get("source_document_sha256"), set()).add(row.get("article_id"))
                    for row in snapshot.get("tables", {}).get("pdf_intake_calendar_items", []):
                        occurrence_id = row.get("occurrence_id")
                        if allowed_calendar_ids is None or occurrence_id in allowed_calendar_ids:
                            calendar_ids[row.get("source_document_sha256")] = calendar_ids.get(row.get("source_document_sha256"), 0) + 1
                items = []
                for row in connection.execute("SELECT document_sha256,filename,date_of_run,document_json FROM pdf_intake_documents"):
                    sha = row["document_sha256"]
                    article_count = len(article_ids.get(sha, set()))
                    calendar_count = calendar_ids.get(sha, 0)
                    document = self._pdf_document_json(row["document_json"], document_sha256=sha)
                    items.append({"report_id": sha, "document_sha256": sha, "source_kind": "pdf",
                        "report_title": document.get("title") or row["filename"], "filename": row["filename"],
                        "report_date": row["date_of_run"], "article_count": article_count,
                        "calendar_count": calendar_count, "page_count": document.get("page_count"),
                        "monitoring_status": "not_reported", "source_label": "PDF import"})
                items.sort(key=lambda item: (item.get("report_date") or "", item["document_sha256"]), reverse=True)
                return items
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
                document = self._pdf_document_json(row["document_json"])
                items.append({"report_id": sha, "document_sha256": sha, "source_kind": "pdf",
                    "report_title": document.get("title") or row["filename"], "filename": row["filename"],
                    "report_date": row["date_of_run"], "article_count": len(articles.get(sha, [])),
                    "calendar_count": calendars.get(sha, 0), "page_count": document.get("page_count"),
                    "monitoring_status": "not_reported", "source_label": "PDF import"})
        return items

    def _pdf_document_json(self, value: str, *, document_sha256: str | None = None) -> dict[str, Any]:
        if document_sha256 and getattr(self, "_snapshot_direct", False):
            cached = getattr(self, "_pdf_document_cache", {}).get(document_sha256)
            if cached is not None:
                return cached
        try:
            document = json.loads(value)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RegistryContractError("invalid PDF document data") from exc
        if not isinstance(document, dict):
            raise RegistryContractError("invalid PDF document data")
        if document_sha256 and getattr(self, "_snapshot_direct", False):
            self._pdf_document_cache[document_sha256] = document
        return document

    def _pdf_report_from_published_candidates(self, connection, document_sha256: str, *,
        allowed_occurrence_ids: set[str] | None, allowed_calendar_ids: set[str] | None,
        include_bytes: bool, page: int | None = None, page_size: int = 5) -> dict[str, Any]:
        candidates = self._pdf_document_candidates(connection, document_sha256)
        document_row = connection.execute(
            "SELECT * FROM pdf_intake_documents WHERE document_sha256=?", (document_sha256,)
        ).fetchone()
        document_row = dict(document_row) if document_row is not None else None
        if document_row is None:
            raise RegistryNotFoundError("PDF report not found")

        approved_article_ids, approved_calendar_ids = set(), set()
        source_rows = []
        approved_documents = {}
        for snapshot in candidates.values():
            dependencies = snapshot.get("dependencies", {})
            for row in dependencies.get("pdf_intake_documents", []):
                if row.get("document_sha256") == document_sha256:
                    existing = approved_documents.get(document_sha256)
                    if existing is not None and existing.get("original_pdf") != row.get("original_pdf"):
                        raise RegistryContractError("approved PDF source identity differs")
                    approved_documents[document_sha256] = row
            source_rows.extend(row for row in dependencies.get("pdf_intake_document_sources", [])
                if row.get("document_sha256") == document_sha256)
            for row in snapshot.get("tables", {}).get("pdf_intake_article_occurrences", []):
                if (row.get("source_document_sha256") == document_sha256
                        and row.get("summary_basis") != "verbatim_pdf_calendar_row"):
                    approved_article_ids.add(row.get("occurrence_id"))
            for row in snapshot.get("tables", {}).get("pdf_intake_calendar_items", []):
                if row.get("source_document_sha256") == document_sha256:
                    approved_calendar_ids.add(row.get("occurrence_id"))
        document = self._pdf_document_json(document_row.get("document_json"), document_sha256=document_sha256)
        document_sources = self._snapshot_pdf_document_sources({
            "pdf_intake_documents": [document_row],
            "pdf_intake_document_sources": source_rows,
        }).get(document_sha256, [])
        source_filenames = sorted({row.get("filename") for row in document_sources if row.get("filename")})
        article_ids_for_document = {row.get("article_id") for (kind, identity), snapshot in candidates.items()
            for row in snapshot.get("tables", {}).get("pdf_intake_article_occurrences", [])
            if kind in {"article", "pdf_article"}
            and (kind != "pdf_article" or row.get("article_id") == identity)
            and row.get("source_document_sha256") == document_sha256
            and json.loads(row["occurrence_json"]).get("summary_basis") != "verbatim_pdf_calendar_row"
            and (allowed_occurrence_ids is None or row.get("occurrence_id") in allowed_occurrence_ids)}
        if allowed_calendar_ids is None:
            calendar_count = len(approved_calendar_ids)
        else:
            calendar_count = len(approved_calendar_ids & allowed_calendar_ids)
        item = {"report_id": document_sha256, "document_sha256": document_sha256,
            "source_kind": "pdf", "report_title": document.get("title") or document_row.get("filename"),
            "filename": document_row.get("filename"), "report_date": document_row.get("date_of_run"),
            "article_count": len(article_ids_for_document), "calendar_count": calendar_count,
            "page_count": document.get("page_count"), "monitoring_status": "not_reported",
            "source_label": "PDF import"}
        if include_bytes:
            row = connection.execute("SELECT original_pdf FROM pdf_intake_documents WHERE document_sha256=?",
                (document_sha256,)).fetchone()
            body = row[0] if row is not None else None
            if body is None:
                raise RegistryNotFoundError("original PDF unavailable")
            if hashlib.sha256(body).hexdigest() != document_sha256:
                raise RegistryContractError("original PDF hash mismatch")
            return dict(item, pdf_bytes=body)

        article_order = [row[0] for row in connection.execute(
            "SELECT article_id FROM pdf_intake_articles a WHERE EXISTS "
            "(SELECT 1 FROM pdf_intake_article_occurrences o WHERE o.article_id=a.article_id "
            "AND o.source_document_sha256=?)", (document_sha256,)).fetchall()
            if row[0] in article_ids_for_document]
        selected_articles = set(article_order)
        selected_calendars = approved_calendar_ids if allowed_calendar_ids is None else approved_calendar_ids & allowed_calendar_ids
        if page is not None:
            validate_page(page, page_size)
            # Select module identities before constructing article/check payloads.
            calendar_rows = {row["occurrence_id"]: json.loads(row["item_json"])
                for snapshot in candidates.values()
                for row in snapshot.get("tables", {}).get("pdf_intake_calendar_items", [])
                if row.get("occurrence_id") in selected_calendars}
            calendar_order = sorted(calendar_rows, key=lambda identity: (
                calendar_rows[identity].get("start_date") or "9999",
                calendar_rows[identity].get("name") or "", identity))
            offset = (page - 1) * page_size
            modules = [("article", identity) for identity in article_order] + [("calendar", identity) for identity in calendar_order]
            selected = modules[offset:offset + page_size]
            selected_articles = {identity for kind, identity in selected if kind == "article"}
            selected_calendars = {identity for kind, identity in selected if kind == "calendar"}
            item["pagination"] = _pagination(page, page_size, len(modules))
            item["article_offset"] = min(offset, len(article_order))
        source_observations = document_sources
        allowed_articles = allowed_occurrence_ids
        articles_by_id = {}
        for (kind, identity), snapshot in candidates.items():
            if kind not in {"article", "pdf_article"}:
                continue
            for row in snapshot.get("tables", {}).get("pdf_intake_articles", []):
                article_id = row.get("article_id")
                if article_id not in selected_articles or (kind == "pdf_article" and article_id != identity):
                    continue
                scoped = {**snapshot, "entity_id": article_id,
                    "_approved_candidate_sha256": self._published_candidate_sha(snapshot)}
                try:
                    article = self._pdf_article_from_snapshot(scoped)
                except RegistryNotFoundError:
                    continue
                occurrences = [value for value in article["occurrences"]
                    if value.get("source_document_sha256") == document_sha256
                    and (allowed_articles is None or value.get("occurrence_id") in allowed_articles)]
                if occurrences:
                    articles_by_id[article_id] = {**article, "occurrences": deduplicate_pdf_occurrences(
                        occurrences, preserve_first_identity=True)}
        articles = [articles_by_id[identity] for identity in article_order if identity in articles_by_id]

        calendars_by_id = {}
        for (kind, _identity), snapshot in candidates.items():
            if kind not in {"meeting", "pdf_meeting"}:
                continue
            tables, dependencies = snapshot.get("tables", {}), snapshot.get("dependencies", {})
            documents = {row.get("document_sha256"): row
                for row in dependencies.get("pdf_intake_documents", [])}
            sources = self._snapshot_pdf_document_sources(dependencies)
            attempts = tables.get("meeting_check_attempts", [])
            for row in tables.get("pdf_intake_calendar_items", []):
                occurrence_id = row.get("occurrence_id")
                if (row.get("source_document_sha256") != document_sha256 or occurrence_id not in selected_calendars
                        or (allowed_calendar_ids is not None and occurrence_id not in allowed_calendar_ids)):
                    continue
                try:
                    calendar = json.loads(row.get("item_json") or "{}")
                    classification = (json.loads(row["type_safe_classification_json"])
                        if row.get("type_safe_classification_json") else None)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise RegistryContractError("invalid PDF calendar item data") from exc
                if not isinstance(calendar, dict):
                    raise RegistryContractError("invalid PDF calendar item data")
                calendar.update(occurrence_id=occurrence_id, type_safe_classification=classification,
                    source_kind="pdf", source_document_sha256=document_sha256)
                source_document = documents.get(document_sha256)
                if source_document:
                    calendar["source_filename"] = source_document.get("filename")
                    calendar["source_observations"] = sources.get(document_sha256, [])
                else:
                    calendar["source_observations"] = source_observations
                from .information_checks import latest_checks_from_attempts
                calendar.update(latest_checks_from_attempts("meetings", calendar, attempts,
                    packet_cache=self._pdf_check_packet_cache,
                    candidate_binding=self._published_candidate_sha(snapshot)))
                from climate_monitor.meeting_fields import pdf_meeting_fields
                calendars_by_id[occurrence_id] = pdf_meeting_fields(calendar)
        calendars = list(calendars_by_id.values())
        self._restore_approved_calendar_fields(connection, calendars, approved_documents)
        if calendars:
            calendars = self.resolve_pdf_meeting_identities(calendars)
        calendars.sort(key=lambda value: (value.get("start_date") or "9999", value.get("name") or "",
            value.get("occurrence_id") or ""))
        item.update({"edition": document.get("edition"), "reporting_period": document.get("reporting_period"),
            "period_start": document_row.get("period_start"), "period_end": document_row.get("period_end"),
            "executive_summary": [],
            "pdf_metadata": (document.get("source") or {}).get("pdf_metadata") or {},
            "pdf_created_at": document_row.get("pdf_created_at"),
            "pdf_modified_at": document_row.get("pdf_modified_at"), "imported_at": document_row.get("imported_at"),
            "source_filenames": source_filenames,
            "pages": [],
            "articles": articles, "calendar_items": calendars,
            "report_pdf": {"filename": document_row["filename"],
                "download_url": f"/api/registry/pdf-intake/reports/{document_sha256}/pdf"}
                if document_row.get("original_pdf") else None})
        if page is None:
            item["article_count"] = len(articles)
        else:
            item.pop("pages", None)
        for occurrence in [value for article in articles for value in article["occurrences"]] + calendars:
            occurrence["source_observations"] = [{key: value for key, value in observation.items() if key != "path"}
                for observation in occurrence.get("source_observations", [])]
        return item

    @staticmethod
    def paginate_pdf_report(report: dict[str, Any], page: int, page_size: int) -> dict[str, Any]:
        validate_page(page, page_size)
        articles, calendars = report["articles"], report["calendar_items"]
        offset = (page - 1) * page_size
        end = offset + page_size
        payload = {key: value for key, value in report.items() if key != "pages"}
        payload.update(articles=articles[offset:end],
            calendar_items=calendars[max(0, offset - len(articles)):max(0, end - len(articles))],
            article_offset=min(offset, len(articles)),
            pagination=_pagination(page, page_size, len(articles) + len(calendars)))
        return payload

    def pdf_report_module_catalog(self, document_sha256: str, *, allowed_occurrence_ids=None,
        allowed_calendar_ids=None) -> list[tuple[str, str]]:
        """Lightweight identities for legacy public/runtime report merging."""
        if self.static_snapshot is not None and "pdf_report_details" in self.static_snapshot:
            stored = self.static_snapshot["pdf_report_details"].get(document_sha256, {})
            return [("article", article["canonical_url"]) for article in stored.get("articles", [])
                if any(allowed_occurrence_ids is None or row["occurrence_id"] in allowed_occurrence_ids
                    for row in article["occurrences"])] + [("calendar", row["occurrence_id"])
                for row in stored.get("calendar_items", [])
                if allowed_calendar_ids is None or row["occurrence_id"] in allowed_calendar_ids]
        with self.connect() as connection:
            articles = connection.execute("SELECT a.canonical_url, o.occurrence_id "
                "FROM pdf_intake_articles a JOIN pdf_intake_article_occurrences o ON o.article_id=a.article_id "
                "WHERE o.source_document_sha256=? AND COALESCE(json_extract(o.occurrence_json, '$.summary_basis'), '') "
                "!= 'verbatim_pdf_calendar_row' ORDER BY a.rowid", (document_sha256,)).fetchall()
            calendars = connection.execute("SELECT occurrence_id FROM pdf_intake_calendar_items "
                "WHERE source_document_sha256=? ORDER BY start_date, name, occurrence_id", (document_sha256,)).fetchall()
        return list(dict.fromkeys([("article", row[0]) for row in articles
            if allowed_occurrence_ids is None or row[1] in allowed_occurrence_ids])) + [
            ("calendar", row[0]) for row in calendars
            if allowed_calendar_ids is None or row[0] in allowed_calendar_ids]

    @_direct_public_read
    def pdf_report(self, document_sha256: str, *, allowed_occurrence_ids: set[str] | None = None,
        allowed_calendar_ids: set[str] | None = None, include_bytes: bool = False,
        page: int | None = None, page_size: int = 5,
        module_keys: set[tuple[str, str]] | None = None) -> dict[str, Any]:
        if len(document_sha256) != 64 or any(char not in "0123456789abcdef" for char in document_sha256):
            raise RegistryQueryError("invalid PDF document id")
        if self.public and getattr(self, "_snapshot_direct", False):
            with self.connect() as connection:
                if connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                    return self._pdf_report_from_published_candidates(connection, document_sha256,
                        allowed_occurrence_ids=allowed_occurrence_ids,
                        allowed_calendar_ids=allowed_calendar_ids, include_bytes=include_bytes, page=page, page_size=page_size)
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
            if module_keys is not None:
                payload["articles"] = [row for row in payload["articles"] if ("article", row["canonical_url"]) in module_keys]
                payload["calendar_items"] = [row for row in payload["calendar_items"] if ("calendar", row["occurrence_id"]) in module_keys]
            return {**payload,**item,"report_pdf":None}
        with self.connect() as connection:
            if module_keys is not None:
                occurrence_ids = {row[1] for row in connection.execute(
                    "SELECT a.canonical_url,o.occurrence_id FROM pdf_intake_articles a "
                    "JOIN pdf_intake_article_occurrences o ON o.article_id=a.article_id "
                    "WHERE o.source_document_sha256=?", (document_sha256,)) if ("article", row[0]) in module_keys}
                allowed_occurrence_ids = occurrence_ids if allowed_occurrence_ids is None else occurrence_ids & allowed_occurrence_ids
                calendar_ids = {identity for kind, identity in module_keys if kind == "calendar"}
                allowed_calendar_ids = calendar_ids if allowed_calendar_ids is None else calendar_ids & allowed_calendar_ids
            if self.public and getattr(self, "_snapshot_direct", False) and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                candidates = self._public_candidates("article", "pdf_article", "meeting", "pdf_meeting")
                document_row = next((row for snapshot in candidates.values()
                    for row in snapshot.get("dependencies", {}).get("pdf_intake_documents", [])
                    if row.get("document_sha256") == document_sha256), None)
                if document_row is None:
                    raise RegistryNotFoundError("PDF report not found")
                approved_article_ids, approved_calendar_ids = set(), set()
                source_rows = [row for snapshot in candidates.values()
                    for row in snapshot.get("dependencies", {}).get("pdf_intake_document_sources", [])
                    if row.get("document_sha256") == document_sha256]
                source_filenames = {source["filename"] for source in self._snapshot_pdf_document_sources({
                    "pdf_intake_documents": [document_row],
                    "pdf_intake_document_sources": source_rows,
                }).get(document_sha256, []) if source.get("filename")}
                for snapshot in candidates.values():
                    for row in snapshot.get("tables", {}).get("pdf_intake_article_occurrences", []):
                        if row.get("source_document_sha256") == document_sha256 and row.get("summary_basis") != "verbatim_pdf_calendar_row":
                            approved_article_ids.add(row.get("occurrence_id"))
                    for row in snapshot.get("tables", {}).get("pdf_intake_calendar_items", []):
                        if row.get("source_document_sha256") == document_sha256:
                            approved_calendar_ids.add(row.get("occurrence_id"))
                if include_bytes:
                    row = connection.execute("SELECT original_pdf FROM pdf_intake_documents WHERE document_sha256=?", (document_sha256,)).fetchone()
                    body = row[0] if row is not None else None
                    if body is None:
                        raise RegistryNotFoundError("original PDF unavailable")
                    if hashlib.sha256(body).hexdigest() != document_sha256:
                        raise RegistryContractError("original PDF hash mismatch")
                    return dict(item, pdf_bytes=body)
                document = self._pdf_document_json(document_row["document_json"], document_sha256=document_sha256)
                articles_by_id = {}
                for article in self.pdf_articles_all(include_linked=True, allowed_occurrence_ids=allowed_occurrence_ids):
                    occurrences = [value for value in article["occurrences"] if value.get("source_document_sha256") == document_sha256]
                    if occurrences:
                        articles_by_id[article["article_id"]] = {**article, "occurrences": occurrences}
                article_order = [row[0] for row in connection.execute(
                    "SELECT article_id FROM pdf_intake_articles WHERE EXISTS (SELECT 1 FROM pdf_intake_article_occurrences o "
                    "WHERE o.article_id=pdf_intake_articles.article_id AND o.source_document_sha256=?)",
                    (document_sha256,)).fetchall()]
                articles = [articles_by_id[identity] for identity in article_order if identity in articles_by_id]
                calendars = [value for value in self.pdf_calendar_items_all(allowed_occurrence_ids=allowed_calendar_ids)
                    if value.get("source_document_sha256") == document_sha256]
                source = document.get("source") or {}
                item.update({"edition": document.get("edition"), "reporting_period": document.get("reporting_period"),
                    "period_start": document_row.get("period_start"), "period_end": document_row.get("period_end"),
                    "executive_summary": [],
                    "pdf_metadata": source.get("pdf_metadata") or {}, "pdf_created_at": document_row.get("pdf_created_at"),
                    "pdf_modified_at": document_row.get("pdf_modified_at"), "imported_at": document_row.get("imported_at"),
                    "source_filenames": sorted(source_filenames),
                    "pages": [],
                    "articles": articles, "calendar_items": calendars,
                    "report_pdf": {"filename": document_row["filename"],
                        "download_url": f"/api/registry/pdf-intake/reports/{document_sha256}/pdf"}
                        if document_row.get("original_pdf") else None})
                for occurrence in [value for article in articles for value in article["occurrences"]] + calendars:
                    occurrence["source_observations"] = [{key: value for key, value in observation.items() if key != "path"}
                        for observation in occurrence.get("source_observations", [])]
                return item
            row = dict(connection.execute("SELECT * FROM pdf_intake_documents WHERE document_sha256=?", (document_sha256,)).fetchone())
            if include_bytes:
                body = row.get("original_pdf")
                if body is None:
                    raise RegistryNotFoundError("original PDF unavailable")
                if hashlib.sha256(body).hexdigest() != document_sha256:
                    raise RegistryContractError("original PDF hash mismatch")
                return dict(item, pdf_bytes=body)
            document = self._pdf_document_json(row["document_json"], document_sha256=document_sha256)
            articles = []
            for article_row in connection.execute("""SELECT * FROM pdf_intake_articles a WHERE EXISTS (
                SELECT 1 FROM pdf_intake_article_occurrences o WHERE o.article_id=a.article_id AND o.source_document_sha256=?)""", (document_sha256,)):
                if module_keys is not None and ("article", article_row["canonical_url"]) not in module_keys:
                    continue
                payload = self._pdf_article_payload(connection, article_row, allowed_occurrence_ids=allowed_occurrence_ids)
                occurrences = deduplicate_pdf_occurrences([value for value in payload["occurrences"]
                    if value.get("source_document_sha256") == document_sha256])
                if occurrences:
                    articles.append({**payload, "occurrences": occurrences})
            source = document.get("source") or {}
            item.update({"edition": document.get("edition"), "reporting_period": document.get("reporting_period"),
                "period_start": row["period_start"], "period_end": row["period_end"],
                "executive_summary": [],
                "pdf_metadata": source.get("pdf_metadata") or {}, "pdf_created_at": row.get("pdf_created_at"),
                "pdf_modified_at": row.get("pdf_modified_at"), "imported_at": row["imported_at"],
                "source_filenames": sorted({value["filename"] for value in self._pdf_document_sources(connection, document_sha256)}),
                "pages": [],
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

    @_direct_public_read
    def report(self, report_date: str) -> dict[str, Any]:
        payload, _identity = self.report_with_identity(report_date)
        return payload

    @_direct_public_read
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
            if self.public and getattr(self, "_snapshot_direct", False) and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                identities = [row[0] for row in connection.execute(
                    "SELECT article_id FROM report_appearances WHERE report_id=? ORDER BY ordinal,article_id",
                    (report["report_id"],)).fetchall()]
                candidates = self._public_candidates("article", entity_ids={"article": identities}) if identities else {}
                source_report = self._source_report(report["report_date"], report["filename"], report["report_sha256"], archive=True)
                articles = []
                self._prepare_legacy_acquisition_searches(candidates.values())
                for article_id in identities:
                    snapshot = candidates.get(("article", article_id))
                    if snapshot is None:
                        continue
                    detail = self._article_from_snapshot(snapshot)
                    appearance = next((item for item in detail.get("appearances", [])
                        if item.get("report_date") == report["report_date"]), None)
                    frozen = next((item for item in snapshot.get("tables", {}).get("report_appearances", [])
                        if item.get("report_id") == report["report_id"]), None)
                    if appearance is None or frozen is None:
                        continue
                    articles.append({"ordinal": frozen.get("ordinal"), "section": frozen.get("section"),
                        "pillar": frozen.get("pillar"), "observation_status": frozen.get("observation_status"),
                        "article_id": article_id, "canonical_url": detail["canonical_url"],
                        "title": detail.get("title"), "summary": detail.get("summary"),
                        "report_summary": appearance.get("summary"), "publisher": detail.get("publisher"),
                        "source": detail.get("source"), "categories": detail.get("categories", []),
                        "keywords": detail.get("keywords", []), "summary_provenance": detail.get("summary_provenance"),
                        "metadata_provenance": detail.get("metadata_provenance"),
                        "source_annotation": detail.get("source_annotation"),
                        "current_version_id": detail.get("current_version_id"),
                        "record_reference": detail.get("record_reference")})
                payload = {"report_date": report["report_date"], "report_title": report["report_title"],
                    "cadence": report["cadence"], "report_format": report["report_format"],
                    "executive_summary": list(source_report.executive_summary) if source_report else [],
                    "monitoring": {"status": _monitoring_status(report), "sites_checked": report["sites_checked"],
                        "sites_succeeded": report["sites_succeeded"], "sites_failed": report["sites_failed"],
                        "warning_count": len(_json_string_list(report["parse_warnings_json"]))}, "articles": articles}
                identity = RegistryReportIdentity(report_date=report["report_date"], filename=report["filename"],
                    report_title=report["report_title"], report_sha256=report["report_sha256"])
                return payload, identity
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

    @_direct_public_read
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

    def approved_article_cards_all(self) -> list[dict[str, Any]]:
        """Return the full approved core collection once inside a direct read transaction."""
        if self.static_snapshot is not None:
            return deepcopy(self.static_snapshot.get("articles", []))
        with self.connect() as connection:
            if not (self.public and getattr(self, "_snapshot_direct", False)
                    and connection.execute("PRAGMA user_version").fetchone()[0] >= 21):
                raise RegistryContractError("approved article collection requires a direct published snapshot")
            cached = getattr(self, "_approved_article_cards", None)
            if cached is None:
                cards = []
                for snapshot in self._public_candidates("article").values():
                    item = self._article_card_from_snapshot(snapshot)
                    article = next((row for row in snapshot.get("tables", {}).get("articles", [])
                        if row.get("article_id") == snapshot.get("entity_id")), {})
                    item["source_id"] = article.get("source_id")
                    cards.append(item)
                cards.sort(key=lambda item: (item.get("last_seen") or "", item.get("first_seen") or "", item["article_id"]), reverse=True)
                self._approved_article_cards = cards
                cached = cards
            return deepcopy(cached)

    @_direct_public_read
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
        if getattr(self, "_snapshot_direct", False):
            with self.connect() as connection:
                if connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                    items = filter_article_cards(self.approved_article_cards_all(), query=query, source=source,
                        pillar=pillar, report_date=report_date)
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
    def _snapshot_pdf_document_sources(dependencies: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
        """Recreate the legacy source observation shape from frozen dependencies."""
        sources: dict[str, list[dict[str, str]]] = {}
        for row in dependencies.get("pdf_intake_document_sources", []):
            sources.setdefault(row.get("document_sha256"), []).append({
                "path": row.get("source_path"),
                "filename": row.get("filename"),
                "observed_at": row.get("observed_at"),
            })
        documents = {
            row.get("document_sha256"): row
            for row in dependencies.get("pdf_intake_documents", [])
        }
        for sha, document in documents.items():
            if not sources.get(sha):
                sources[sha] = [{
                    "path": document.get("source_path"),
                    "filename": document.get("filename"),
                    "observed_at": document.get("imported_at"),
                }]
        return sources

    def _pdf_document_candidates(self, connection, document_sha256: str):
        """Select only published entities tied to one PDF's stable identities."""
        if not self._has_pdf_intake(connection):
            return {}
        core_column = ("p.core_article_id" if self._has_pdf_article_links(connection)
            else "NULL AS core_article_id")
        article_rows = connection.execute(
            "SELECT DISTINCT p.article_id,p.canonical_url," + core_column +
            " FROM pdf_intake_article_occurrences o JOIN pdf_intake_articles p ON p.article_id=o.article_id "
            "WHERE o.source_document_sha256=? ORDER BY p.article_id", (document_sha256,),
        ).fetchall()
        calendar_rows = connection.execute(
            "SELECT DISTINCT event_id,occurrence_id FROM pdf_intake_calendar_items "
            "WHERE source_document_sha256=? ORDER BY event_id,occurrence_id", (document_sha256,),
        ).fetchall()
        pdf_article_ids = {row["article_id"] for row in article_rows}
        canonical_urls = {row["canonical_url"] for row in article_rows if row["canonical_url"]}
        core_ids = {row["core_article_id"] for row in article_rows if row["core_article_id"]}
        if canonical_urls:
            urls = sorted(canonical_urls)
            core_ids.update(row[0] for row in connection.execute(
                "SELECT article_id FROM articles WHERE canonical_url IN (" +
                ",".join("?" for _ in urls) + ")", urls).fetchall())
        calendar_ids = {row["event_id"] for row in calendar_rows}
        occurrence_ids = {row["occurrence_id"] for row in calendar_rows}
        native_ids = set()
        if occurrence_ids and connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meeting_check_attempts'").fetchone():
            values = sorted(occurrence_ids)
            native_ids.update(row[0] for row in connection.execute(
                "SELECT DISTINCT json_extract(packet_json,'$.canonical_event_id') "
                "FROM meeting_check_attempts WHERE verification_status='verified' "
                "AND occurrence_id IN (" + ",".join("?" for _ in values) + ") "
                "AND json_extract(packet_json,'$.canonical_event_id') IS NOT NULL", values).fetchall())
        entity_ids = {
            "article": core_ids,
            "pdf_article": pdf_article_ids,
            "meeting": native_ids,
            "pdf_meeting": calendar_ids,
        }
        if not any(entity_ids.values()):
            return {}
        return self._public_candidates("article", "pdf_article", "meeting", "pdf_meeting",
            entity_ids=entity_ids)

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
        from .publication import snapshot_metadata, approved_display, digest
        snapshot = snapshot_metadata(connection, "pdf_article", row["article_id"])
        supplements = [item for item in (snapshot or {}).get("supplements", []) if item["source_ref"].startswith("pdf-classification:") and item["entity_id"] == row["article_id"]]
        if supplements:
            evidence = json.loads(supplements[-1]["evidence_json"])
            pending = evidence["pending_enrichment"]
            payload.update(categories=pending["categories"], keywords=pending["keywords"],
                manual_enrichment={**pending, "generator_kind": "manual",
                    "evidence": evidence, "knowledge_id": supplements[-1]["knowledge_id"]})
        if snapshot is not None:
            payload.update(approved_display(snapshot))
            payload["published_candidate_sha256"] = digest(snapshot)
            payload["record_reference"] = {"entity_kind": snapshot["entity_kind"], "entity_id": snapshot["entity_id"],
                "candidate_sha256": payload["published_candidate_sha256"], "version_id": None}
        return payload

    @_direct_public_read
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

    @_direct_public_read
    def pdf_articles_all(self, *, allowed_occurrence_ids: set[str] | None = None,
        include_linked: bool = False) -> list[dict[str, Any]]:
        """Read full PDF article observations before a caller merges and paginates."""
        if self.static_snapshot:
            core_urls = {item["canonical_url"] for item in self.static_snapshot["articles"] if item["publication_eligible"] and item["document_kind"] == "article"}
            return [item for item in self.static_snapshot["pdf_articles"] if include_linked or item["canonical_url"] not in core_urls]

        with self.connect() as connection:
            if self.public and getattr(self, "_snapshot_direct", False) and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                cache_key = (tuple(sorted(allowed_occurrence_ids)) if allowed_occurrence_ids is not None else None, include_linked)
                if cache_key in self._pdf_article_collection_cache:
                    return deepcopy(self._pdf_article_collection_cache[cache_key])
                candidates = self._public_candidates("article", "pdf_article")
                items_by_id = {}
                for (kind, identity), snapshot in candidates.items():
                    if kind == "pdf_article" and not include_linked:
                        row = next(iter(snapshot.get("tables", {}).get("pdf_intake_articles", [])), {})
                        if row.get("core_article_id"):
                            continue
                    rows = snapshot.get("tables", {}).get("pdf_intake_articles", [])
                    if kind == "pdf_article" and not rows:
                        raise RegistryContractError("published PDF article snapshot has no article row")
                    for row in rows:
                        article_id = row.get("article_id")
                        if not article_id:
                            raise RegistryContractError("published PDF article identity is missing")
                        if not include_linked and row.get("core_article_id"):
                            continue
                        if kind == "pdf_article" and article_id != identity:
                            continue
                        scoped = {**snapshot, "entity_id": article_id,
                            "_approved_candidate_sha256": self._published_candidate_sha(snapshot)}
                        try:
                            payload = self._pdf_article_from_snapshot(scoped, allowed_occurrence_ids=allowed_occurrence_ids)
                        except RegistryNotFoundError:
                            continue
                        items_by_id[article_id] = payload
                items = list(items_by_id.values())
                items.sort(key=lambda item: (item["last_seen"] or "", item["article_id"]), reverse=True)
                self._pdf_article_collection_cache[cache_key] = items
                return items
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

    @_direct_public_read
    def pdf_article(self, article_id: str, *, allowed_occurrence_ids: set[str] | None = None) -> dict[str, Any]:
        validate_article_id(article_id)
        if self.static_snapshot:
            item = next((item for item in self.pdf_articles_all(include_linked=True) if item["article_id"] == article_id), None)
            if item is None:
                raise RegistryNotFoundError("article not found")
            return item
        with self.connect() as connection:
            if self.public and getattr(self, "_snapshot_direct", False) and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                pdf_candidates = self._public_candidates("pdf_article", entity_ids={"pdf_article": [article_id]})
                standalone = pdf_candidates.get(("pdf_article", article_id))
                row = connection.execute("SELECT core_article_id,canonical_url FROM pdf_intake_articles WHERE article_id=?", (article_id,)).fetchone()
                core_ids = set()
                if standalone is not None:
                    frozen = next((item for item in standalone.get("tables", {}).get("pdf_intake_articles", [])
                        if item.get("article_id") == article_id), {})
                    core_ids.add(frozen.get("core_article_id"))
                    canonical_url = frozen.get("canonical_url")
                else:
                    canonical_url = row["canonical_url"] if row is not None else None
                    if row is not None:
                        core_ids.add(row["core_article_id"])
                if canonical_url:
                    core_ids.update(value[0] for value in connection.execute(
                        "SELECT article_id FROM articles WHERE canonical_url=? ORDER BY article_id", (canonical_url,)).fetchall())
                core_ids.discard(None)
                if core_ids:
                    candidates = self._public_candidates("article", "pdf_article", entity_ids={
                        "article": core_ids, "pdf_article": [article_id]})
                    owner = next((snapshot for (kind, _identity), snapshot in candidates.items()
                        if kind == "article" and any(item.get("article_id") == article_id
                            for item in snapshot.get("tables", {}).get("pdf_intake_articles", []))), None)
                    if owner is not None:
                        return self._pdf_article_from_snapshot({**owner, "entity_id": article_id,
                            "_approved_candidate_sha256": self._published_candidate_sha(owner)},
                            allowed_occurrence_ids=allowed_occurrence_ids)
                    standalone = candidates.get(("pdf_article", article_id), standalone)
                if standalone is not None:
                    return self._pdf_article_from_snapshot(standalone, allowed_occurrence_ids=allowed_occurrence_ids)
                raise RegistryNotFoundError("article not found")
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

    @_direct_public_read
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

    @_direct_public_read
    def pdf_calendar_items_all(
        self, *, allowed_occurrence_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Recover each allow-listed source PDF once, then return all calendar rows."""
        if self.static_snapshot:
            return [item for item in self.static_snapshot["meetings"] if allowed_occurrence_ids is None or item["occurrence_id"] in allowed_occurrence_ids]
        with self.connect() as connection:
            if self.public and getattr(self, "_snapshot_direct", False) and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                cache_key = tuple(sorted(allowed_occurrence_ids)) if allowed_occurrence_ids is not None else None
                if cache_key in self._pdf_calendar_collection_cache:
                    return deepcopy(self._pdf_calendar_collection_cache[cache_key])
                candidates = self._public_candidates("meeting", "pdf_meeting")
                items_by_id = {}
                approved_documents = {}
                for snapshot in candidates.values():
                    tables, dependencies = snapshot.get("tables", {}), snapshot.get("dependencies", {})
                    documents = {row.get("document_sha256"): row for row in dependencies.get("pdf_intake_documents", [])}
                    for sha, document in documents.items():
                        if sha in approved_documents and approved_documents[sha].get("original_pdf") != document.get("original_pdf"):
                            raise RegistryContractError("approved PDF source identity differs")
                        approved_documents[sha] = document
                    sources = self._snapshot_pdf_document_sources(dependencies)
                    attempts = tables.get("meeting_check_attempts", [])
                    for row in tables.get("pdf_intake_calendar_items", []):
                        occurrence_id = row.get("occurrence_id")
                        if not occurrence_id or (allowed_occurrence_ids is not None and occurrence_id not in allowed_occurrence_ids):
                            continue
                        try:
                            item = json.loads(row.get("item_json") or "{}")
                            classification = json.loads(row["type_safe_classification_json"]) if row.get("type_safe_classification_json") else None
                        except (json.JSONDecodeError, TypeError) as exc:
                            raise RegistryContractError("invalid PDF calendar item data") from exc
                        if not isinstance(item, dict):
                            raise RegistryContractError("invalid PDF calendar item data")
                        item.update(occurrence_id=occurrence_id, type_safe_classification=classification,
                            source_kind="pdf", source_document_sha256=row.get("source_document_sha256"))
                        document = documents.get(row.get("source_document_sha256"))
                        if document:
                            item["source_filename"] = document.get("filename")
                            item["source_observations"] = sources.get(row.get("source_document_sha256"), [])
                        else:
                            item["source_observations"] = []
                        from .information_checks import latest_checks_from_attempts
                        item.update(latest_checks_from_attempts("meetings", item, attempts,
                            packet_cache=self._pdf_check_packet_cache,
                            candidate_binding=self._published_candidate_sha(snapshot)))
                        from climate_monitor.meeting_fields import pdf_meeting_fields
                        items_by_id[occurrence_id] = pdf_meeting_fields(item)
                items = list(items_by_id.values())
                self._restore_approved_calendar_fields(connection, items, approved_documents)
                if items:
                    items = self.resolve_pdf_meeting_identities(items)
                items.sort(key=lambda item: (item.get("start_date") or "9999", item.get("name") or "", item.get("occurrence_id") or ""))
                self._pdf_calendar_collection_cache[cache_key] = items
                return items
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
        if self.public and items:
            items = self.resolve_pdf_meeting_identities(items)
        items.sort(key=lambda item: (
            item.get("start_date") or "9999", item.get("name") or "", item.get("occurrence_id") or "",
        ))
        return items

    def _restore_approved_calendar_fields(self, connection, items, approved_documents):
        """Restore legacy table-cell fields using only bytes pinned by approved snapshots."""
        by_document = {}
        for item in items:
            if item.get("raw_text") and item.get("source_document_sha256"):
                by_document.setdefault(item["source_document_sha256"], []).append(item)
        if not by_document:
            return
        from climate_monitor.pdf_intake import recover_calendar_fields
        restored_by_id = {}
        for document_sha256, observations in by_document.items():
            document = approved_documents.get(document_sha256) or {}
            blob_identity = document.get("original_pdf")
            if not isinstance(blob_identity, dict) or set(blob_identity) != {"blob_sha256", "blob_size"}:
                continue
            row = connection.execute(
                "SELECT original_pdf FROM pdf_intake_documents WHERE document_sha256=?",
                (document_sha256,),
            ).fetchone()
            raw_pdf = row[0] if row is not None else None
            if (raw_pdf is None or len(raw_pdf) != blob_identity["blob_size"]
                    or hashlib.sha256(raw_pdf).hexdigest() != blob_identity["blob_sha256"]):
                raise RegistryContractError("approved PDF bytes identity differs")
            restored = recover_calendar_fields(raw_pdf, observations)
            restored_by_id.update({item["occurrence_id"]: item for item in restored})
        for index, item in enumerate(items):
            replacement = restored_by_id.get(item.get("occurrence_id"))
            if replacement is not None:
                items[index] = replacement

    @_direct_public_read
    def resolve_pdf_meeting_identities(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Bind verified overlays to identities in this reader's native event history."""
        from climate_monitor.meetings import resolve_event_identity
        result = []
        with self.connect() as connection:
            if self.public and getattr(self, "_snapshot_direct", False) and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                native = self._public_candidates("meeting")
                visible_ids = set(native)
                for raw in items:
                    item = dict(raw)
                    candidate_id = item.get("canonical_event_id")
                    if candidate_id and ("meeting", candidate_id) in visible_ids:
                        item["canonical_event_id"] = candidate_id
                    else:
                        if candidate_id:
                            item.pop("canonical_event_id", None)
                    result.append(item)
                return result
            if self.public and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                visible_ids = {row[0] for row in connection.execute("SELECT event_id FROM climate_events")}
                for raw in items:
                    item = dict(raw)
                    candidate_id = item.get("canonical_event_id")
                    if candidate_id and candidate_id in visible_ids:
                        item["canonical_event_id"] = candidate_id
                        result.append(item)
                        continue
                    if candidate_id:
                        item.pop("canonical_event_id", None)
                    if item.get("verification_status") == "verified" and item.get("collected_candidate"):
                        body_hash = next((check.get("body_sha256") for check in item.get("checks", [])
                            if check.get("verification_status") == "verified"), None)
                        if body_hash:
                            from climate_monitor.meetings import resolve_event_identity
                            identity = resolve_event_identity(connection, item["collected_candidate"], body_hash)
                            if identity in visible_ids:
                                item["canonical_event_id"] = identity
                    result.append(item)
                return result
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

    @_direct_public_read
    def knowledge_chronology(self) -> dict[str, dict[str, Any]]:
        """Only the chronology frozen in the approved public projection."""
        if self.static_snapshot is not None:
            return dict(self.static_snapshot.get("knowledge_chronology", {}))
        with self.connect() as connection:
            if self.public and getattr(self, "_snapshot_direct", False) and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                candidates = self._public_candidates("article", "pdf_article", "meeting", "pdf_meeting")
                values = {}
                for (kind, identity), snapshot in candidates.items():
                    for row in snapshot.get("tables", {}).get("knowledge_versions", []):
                        if row.get("source_ref", "").startswith(("registry-supplement:", "pdf-classification:")):
                            continue
                        key = f"{row.get('entity_kind')}:{row.get('entity_id')}"
                        values.setdefault(key, []).append(row)
                return {key: {field: row.get(field) for field in ("knowledge_id", "material_sha256", "first_ingested_at", "substantive_updated_at", "time_basis")}
                    for key, rows in values.items() for row in [sorted(rows, key=lambda item: item.get("rowid", 0))[-1]]}
            if not self.public or connection.execute("PRAGMA user_version").fetchone()[0] < 21:
                return {}
            rows = connection.execute("""SELECT entity_kind,entity_id,knowledge_id,
                material_sha256,first_ingested_at,substantive_updated_at,time_basis
                FROM knowledge_versions ORDER BY rowid""").fetchall()
        return {f"{row['entity_kind']}:{row['entity_id']}": {
            key: row[key] for key in ("knowledge_id", "material_sha256", "first_ingested_at",
                "substantive_updated_at", "time_basis")} for row in rows}

    def _direct_meeting_collection(self, *, base_date: str, timezone_name: str,
        include_unknown: bool, additional_calendar_items: list[dict[str, Any]] | None = None,
        additional_events: list[dict[str, Any]] | None = None):
        cache_key = (base_date, timezone_name, include_unknown)
        cacheable = not additional_calendar_items and not additional_events
        if cacheable and cache_key in self._direct_meeting_collection_cache:
            return deepcopy(self._direct_meeting_collection_cache[cache_key])
        native, coverage = self._approved_meeting_records(base_date=base_date, timezone_name=timezone_name,
            include_unknown=include_unknown)
        # Approved meeting content stays frozen. Coverage reflects the current
        # latest run state, using only run headers from this same read transaction.
        with self.connect() as connection:
            from climate_monitor.meetings import _meeting_status_selection
            current_status, _latest_runs, _raw_runs = _meeting_status_selection(connection)
        coverage["status"] = "processed" if current_status in {"complete", "not_processed"} else current_status
        collected = {record["event_id"]: dict(record, origin="web_collection", collection_status="collected",
            verification_status="verified", access_status="accessible") for record in native + (additional_events or [])}
        from climate_monitor.meeting_fields import collected_pdf_meeting, merge_meeting_observations
        from .information_checks import merge_checked_observation
        imported_by_id = {}
        for item in self.pdf_calendar_items_all() + (additional_calendar_items or []):
            occurrence_id = item["occurrence_id"]
            imported_by_id[occurrence_id] = merge_checked_observation(imported_by_id[occurrence_id], item) if occurrence_id in imported_by_id else item
        from .range_reports import _calendar_end_date
        for item in self.resolve_pdf_meeting_identities(list(imported_by_id.values())):
            item = collected_pdf_meeting(item)
            end = _calendar_end_date(item)
            if end is not None and end < date.fromisoformat(base_date):
                continue
            candidate, identity = item.get("collected_candidate"), item.get("canonical_event_id")
            if candidate and identity:
                if candidate.get("status") in {"cancelled", "retrospective"}:
                    continue
                if identity in collected:
                    collected[identity] = merge_meeting_observations([collected[identity], item])[0]
                    continue
            else:
                identity = item["occurrence_id"]
            collected[identity] = item
        records = list(collected.values())
        records.sort(key=lambda item: (item.get("start_date") or item.get("deadline_date") or "9999", item.get("name") or ""))
        counts = {state: sum(item.get("verification_status") == state for item in records)
            for state in ("unchecked", "partial", "conflict", "verified")}
        result = (records, counts, coverage)
        if cacheable:
            self._direct_meeting_collection_cache[cache_key] = deepcopy(result)
        return result

    @_direct_public_read
    def meetings_all(self, *, base_date: str, timezone_name: str = "UTC",
        include_unknown: bool = False, include_metadata: bool = False):
        """Read the complete meeting collection once inside the public snapshot."""
        base_date = validate_report_date(base_date)
        if self.static_snapshot is not None:
            from .range_reports import _calendar_end_date
            records = [dict(item) for item in self.static_snapshot.get("meeting_records", [])
                if (_calendar_end_date(item) is None or _calendar_end_date(item) >= date.fromisoformat(base_date))]
            if include_metadata:
                from .publication import public_meeting_coverage
                coverage = public_meeting_coverage(self.static_snapshot.get("meeting_coverage") or {})
                if "returned_record_count" in coverage:
                    coverage["returned_record_count"] = sum(item.get("origin") == "web_collection" for item in records)
                return {"items": records, "verification_counts": {
                    state: sum(item.get("verification_status") == state for item in records)
                    for state in ("unchecked", "partial", "conflict", "verified")}, "coverage": coverage}
            return records
        with self.connect() as connection:
            if self.public and getattr(self, "_snapshot_direct", False) and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                records, counts, coverage = self._direct_meeting_collection(
                    base_date=base_date, timezone_name=timezone_name, include_unknown=include_unknown)
                return {"items": records, "verification_counts": counts, "coverage": coverage} if include_metadata else records
        items, page = [], 1
        while True:
            payload = self.meetings(base_date=base_date,page=page,page_size=100,
                timezone_name=timezone_name, include_unknown=include_unknown)
            items.extend(payload["items"])
            if page >= payload["pagination"]["pages"]:
                return {"items": items, "verification_counts": payload.get("verification_counts", {}),
                    "coverage": payload.get("coverage", {})} if include_metadata else items
            page += 1

    @_direct_public_read
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
            if self.public and getattr(self, "_snapshot_direct", False) and connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                records, counts, coverage = self._direct_meeting_collection(base_date=base_date,
                    timezone_name=timezone_name, include_unknown=include_unknown,
                    additional_calendar_items=additional_calendar_items, additional_events=additional_events)
                selected = [item for item in records if (not verification_status or item.get("verification_status") == verification_status)
                    and (not query or query.casefold() in " ".join(str(item.get(key) or "") for key in ("name", "organizer", "relevance_reason", "raw_date")).casefold())]
                offset = (page - 1) * page_size
                return {"items": selected[offset:offset + page_size], "pagination": _pagination(page, page_size, len(selected)),
                    "base_date": base_date, "verification_counts": counts, "coverage": coverage}
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

    def _approved_meeting_records(self, *, base_date: str, timezone_name: str, include_unknown: bool):
        from climate_monitor.meetings import _date_bounds, _explicit_single_day
        cache_key = (base_date, timezone_name, include_unknown)
        if cache_key in self._meeting_records_cache:
            return deepcopy(self._meeting_records_cache[cache_key])
        try:
            zone = timezone.utc if timezone_name == "UTC" else __import__("zoneinfo").ZoneInfo(timezone_name)
        except Exception as exc:
            raise RegistryQueryError("invalid meeting timezone") from exc
        lower = date.fromisoformat(base_date)
        candidates = self._public_candidates("meeting", "pdf_meeting")
        legacy_batch_ids = sorted({run.get("batch_id")
            for (kind, _identity), snapshot in candidates.items() if kind == "meeting"
            for run in snapshot.get("dependencies", {}).get("meeting_runs", [])
            if run.get("batch_id") and "acquisition_batches" not in snapshot.get("dependencies", {})})
        legacy_batches = {}
        if legacy_batch_ids:
            with self.connect() as connection:
                if self._has_acquisition_projection(connection):
                    for start in range(0, len(legacy_batch_ids), 400):
                        chunk = legacy_batch_ids[start:start + 400]
                        rows = connection.execute("SELECT * FROM acquisition_batches WHERE batch_id IN (" +
                            ",".join("?" for _ in chunk) + ")", chunk).fetchall()
                        legacy_batches.update({row["batch_id"]: dict(row) for row in rows})
        records, runs = [], {}
        for (kind, identity), snapshot in candidates.items():
            if kind != "meeting":
                continue
            tables, dependencies = snapshot.get("tables", {}), snapshot.get("dependencies", {})
            event = next((dict(row) for row in tables.get("climate_events", []) if row.get("event_id") == identity), None)
            if event is None:
                raise RegistryContractError("published meeting snapshot has no event row")
            if event.get("status") == "cancelled" or event.get("event_type") == "retrospective":
                continue
            for run in dependencies.get("meeting_runs", []):
                runs[run.get("meeting_run_id")] = run
            content = {row.get("content_version_id"): row for row in dependencies.get("article_content_versions", [])}
            batch_rows = (dependencies["acquisition_batches"] if "acquisition_batches" in dependencies
                else legacy_batches.values())
            batch = {row.get("batch_id"): row for row in batch_rows}
            run_rows = {row.get("meeting_run_id"): row for row in dependencies.get("meeting_runs", [])}
            source_rows = []
            for source in tables.get("climate_event_sources", []):
                version = content.get(source.get("content_version_id"), {})
                run = run_rows.get(source.get("meeting_run_id"), {})
                acquired = batch.get(run.get("batch_id"), {})
                source_rows.append({**source, "candidate_json": source.get("candidate_json") or "{}",
                    "markdown_content": version.get("markdown_content", ""), "first_fetched_at": version.get("first_fetched_at"),
                    "report_date": acquired.get("report_date", ""), "batch_started_at": acquired.get("started_at", run.get("started_at", "")),
                    "batch_id": run.get("batch_id", ""), "is_current": 0})
            source_rows.sort(key=lambda row: (row.get("source_url") or "", row.get("content_version_id") or ""))
            by_article = {}
            for source in source_rows:
                by_article.setdefault(source.get("article_id"), []).append(source)
            for rows in by_article.values():
                rank = lambda row: (row.get("report_date") or "", row.get("batch_started_at") or "",
                    row.get("first_fetched_at") or "", row.get("batch_id") or "", row.get("content_version_id") or "")
                latest_rank = max((rank(row) for row in rows), default=())
                latest_seq = max((row.get("interpretation_seq", 0) for row in rows if rank(row) == latest_rank), default=0)
                for source in rows:
                    source["is_current"] = int(rank(source) == latest_rank and source.get("interpretation_seq", 0) == latest_seq)
            intervals = []
            if event.get("start_date") and event.get("end_date"):
                intervals.append((_date_bounds(event["start_date"], event["date_precision"], end=False),
                    _date_bounds(event["end_date"], event["date_precision"], end=True)))
            elif source_rows and _explicit_single_day(event, source_rows):
                event_day = date.fromisoformat(event["start_date"])
                intervals.append((event_day, event_day))
            deadline = date.fromisoformat(event["deadline_date"]) if event.get("deadline_date") else None
            matches = any(end >= lower for _start, end in intervals) or (deadline is not None and deadline >= lower)
            if not matches and not (include_unknown and not intervals and event.get("event_type") != "deadline"):
                continue
            event["sources"] = [{key: value for key, value in source.items() if key not in {
                "candidate_json", "candidate_sha256", "event_id", "meeting_run_id",
                "markdown_content", "first_fetched_at", "report_date", "batch_started_at", "batch_id"}}
                | {"collected_at": source.get("first_fetched_at")} for source in source_rows]
            records.append(event)
        records.sort(key=lambda item: ((item.get("start_date") or item.get("deadline_date")) is None,
            item.get("start_date") or item.get("deadline_date") or "9999", item.get("name", "").casefold(), item.get("event_id", "")))
        statuses = [row.get("status") for row in runs.values()]
        status = "not_processed" if not statuses else "partial" if any(value in {"partial", "failed", "running"} for value in statuses) else "complete"
        coverage_status = "processed" if status in {"complete", "not_processed"} else status
        result = (records, {"status": coverage_status, "records_scope": "approved_versions", "returned_record_count": len(records)})
        self._meeting_records_cache[cache_key] = result
        return deepcopy(result)

    @staticmethod
    def _has_acquisition_projection(connection: sqlite3.Connection) -> bool:
        return all(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone() is not None
            for name in ("acquisition_batches", "acquisition_items", "acquisition_searches")
        )

    @staticmethod
    def _legacy_search_refs(snapshot: dict[str, Any]) -> set[tuple[str, str]]:
        dependencies = snapshot.get("dependencies", {})
        if "acquisition_searches" in dependencies:
            return set()
        refs = set()
        for row in snapshot.get("tables", {}).get("acquisition_items", []):
            try:
                origins = json.loads(row.get("origins_json") or "[]")
            except (json.JSONDecodeError, TypeError) as exc:
                raise RegistryContractError("invalid acquisition observation data") from exc
            if not isinstance(origins, list) or any(not isinstance(origin, dict) for origin in origins):
                raise RegistryContractError("invalid acquisition observation data")
            for origin in origins:
                batch_id, search_ref = row.get("batch_id"), origin.get("search_ref")
                if batch_id and search_ref:
                    refs.add((batch_id, search_ref))
        return refs

    def _prepare_legacy_acquisition_searches(self, snapshots) -> None:
        refs = set()
        for snapshot in snapshots:
            refs.update(self._legacy_search_refs(snapshot))
        if not refs:
            return
        loaded = self._legacy_acquisition_searches_loaded
        missing = sorted(refs - loaded)
        if not missing:
            return
        cache = self._legacy_acquisition_search_cache
        with self.connect() as connection:
            if self._has_acquisition_projection(connection):
                for start in range(0, len(missing), 400):
                    chunk = missing[start:start + 400]
                    predicate = " OR ".join("(batch_id=? AND search_ref=?)" for _ in chunk)
                    params = [value for pair in chunk for value in pair]
                    rows = connection.execute("SELECT batch_id,search_ref,search_id,query,engine,status,attempted_at "
                        "FROM acquisition_searches WHERE " + predicate, params).fetchall()
                    cache.update({(row["batch_id"], row["search_ref"]): dict(row) for row in rows})
        loaded.update(missing)

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

    def _article_from_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        if snapshot.get("static_payload"):
            payload = dict(snapshot["static_payload"])
            from .publication import approved_display
            payload.update(approved_display(snapshot))
            payload.setdefault("published_candidate_sha256", self._published_candidate_sha(snapshot))
            payload["record_reference"] = {"entity_kind": "article", "entity_id": snapshot["entity_id"],
                "candidate_sha256": payload["published_candidate_sha256"], "version_id": payload.get("current_version_id")}
            from .article_dates import ArticleDateImportError, select_article_dates
            try:
                payload.update(select_article_dates(payload.get("date_observations", []),
                    collection_times=[payload.get("collected_at"), (payload.get("available_content") or {}).get("collected_at")],
                    acquisition_observations=payload.get("acquisition_observations", [])))
            except ArticleDateImportError as exc:
                raise RegistryContractError("invalid article date evidence") from exc
            return payload

        tables = snapshot.get("tables", {})
        article = next((row for row in tables.get("articles", []) if row.get("article_id") == snapshot["entity_id"]), None)
        if article is None:
            raise RegistryContractError("published article snapshot has no article row")
        article_id = article["article_id"]
        versions = {row["version_id"]: row for row in tables.get("article_versions", [])}
        current_version = versions.get(article.get("current_version_id"), {})
        source = next((row for row in snapshot.get("sources", []) if row.get("source_id") == article.get("source_id")), {})
        reports = {row["report_id"]: row for row in snapshot.get("dependencies", {}).get("reports", [])}
        discoveries = {row["discovery_id"]: row for row in tables.get("discoveries", [])}
        annotation_values = snapshot.get("fallback_annotations", {})
        annotations = {url: ArticleAnnotation(**value) for url, value in annotation_values.items()}

        def parsed_report(report_row):
            value = snapshot.get("report_fallbacks", {}).get(report_row["report_date"])
            if not value or value.get("sha256") != report_row.get("report_sha256") or value.get("path") != report_row.get("filename"):
                return None
            fields = dict(value)
            fields["path"] = Path(value["path"])
            fields["articles"] = tuple(ParsedArticle(**item) for item in fields["articles"])
            return ParsedReport(**fields)

        appearances = []
        report_categories, report_keywords = [], []
        effective_report_categories, effective_report_keywords = [], []
        annotation_summary = None
        fallback_categories, fallback_keywords, fallback_provenance = [], [], None
        category_provenance = keyword_provenance = None
        source_annotation = self._annotation_for_url(annotations, article["canonical_url"])
        if source_annotation:
            annotation_summary = source_annotation.summary
            fallback_categories = list(source_annotation.categories)
            fallback_keywords = list(source_annotation.keywords)
            fallback_provenance = source_annotation.provenance
        contexts = {}
        appearance_rows = []
        for row in tables.get("report_appearances", []):
            if row.get("article_id") != article_id:
                continue
            report = reports.get(row.get("report_id"))
            discovery = discoveries.get(row.get("discovery_id"))
            version = versions.get(row.get("version_id"), {})
            if report is None or discovery is None:
                continue
            appearance_rows.append({
                "report_date": report["report_date"], "source_filename": report["filename"],
                "source_sha256": report["report_sha256"], "report_title": report["report_title"],
                "version_id": row.get("version_id"), "section": row.get("section"),
                "pillar": row.get("pillar"), "ordinal": row.get("ordinal"),
                "observation_status": row.get("observation_status"), "title": version.get("observed_title"),
                "summary": version.get("observed_summary"), "original_url": discovery.get("raw_url"),
            })
        appearance_rows.sort(key=lambda item: item["source_filename"])
        appearance_rows.sort(key=lambda item: item["ordinal"] or 0)
        appearance_rows.sort(key=lambda item: item["report_date"], reverse=True)
        for item in appearance_rows:
            context_key = (item["report_date"], item["source_filename"], item["source_sha256"])
            if context_key not in contexts:
                contexts[context_key] = parsed_report({"report_date": context_key[0], "filename": context_key[1], "report_sha256": context_key[2]})
            source_article = self._source_article(contexts[context_key], item["ordinal"] or 0, item["original_url"] or "")
            annotation = self._annotation_for_url(annotations, item["original_url"] or "")
            if annotation is None:
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
            elif fallback_provenance is None and (bundle := _complete_source_report_bundle(source_article)) is not None:
                annotation_summary, fallback_categories, fallback_keywords = bundle
                fallback_provenance = "source_report"
            item["summary_provenance"] = annotation.provenance if annotation else "source_report"
            item["categories"] = categories
            item["keywords"] = keywords
            item["metadata_provenance"] = {
                "categories": annotation.provenance if annotation else "source_report" if source_categories else None,
                "keywords": annotation.provenance if annotation else "source_report" if source_keywords else None,
            }
            item["source_annotation"] = ({"source_basis": annotation.source_basis, "source_url": annotation.source_url,
                "generated_on": annotation.generated_on} if annotation else None)
            if annotation:
                category_provenance = keyword_provenance = annotation.provenance
            else:
                if source_categories and category_provenance is None:
                    category_provenance = "source_report"
                if source_keywords and keyword_provenance is None:
                    keyword_provenance = "source_report"
            report_categories.extend(source_categories)
            report_keywords.extend(source_keywords)
            effective_report_categories.extend(categories)
            effective_report_keywords.extend(keywords)
            appearances.append(item)

        content_versions = {row["content_version_id"]: row for row in tables.get("article_content_versions", [])}
        content = content_versions.get(article.get("current_content_version_id"))
        fetches = tables.get("article_fetches", [])
        fetch = max(fetches, key=lambda row: (row.get("fetched_at") or "", row.get("fetch_id") or ""), default=None)
        enrichments = [row for row in tables.get("article_enrichments", []) if row.get("status") == "complete" and (
            (article.get("current_content_version_id") is not None and row.get("content_version_id") == article.get("current_content_version_id"))
            or (row.get("content_version_id") is None and row.get("article_id") == article_id
                and row.get("article_version_id") == article.get("current_version_id")))]
        enrichment = max(enrichments, key=lambda row: (row.get("content_version_id") is not None,
            row.get("generated_at") or "", row.get("enrichment_id") or ""), default=None)
        content = next((row for row in tables.get("article_content_versions", [])
            if row.get("article_id") == article_id
            and row.get("content_version_id") == article.get("current_content_version_id")), None)

        acquisition_observations, available_content = self._acquisition_snapshot_projection(
            snapshot, article_id, article.get("current_content_version_id"), article.get("display_policy"))
        date_observations = []
        for row in tables.get("article_date_observations", []):
            if row.get("article_id") != article_id:
                continue
            try:
                evidence = json.loads(row["evidence_json"])
            except (json.JSONDecodeError, TypeError) as exc:
                raise RegistryContractError("invalid article date observation data") from exc
            date_observations.append({"observation_id": row["observation_id"], "kind": row["observation_kind"],
                "observed_at": row["observed_at"], "evidence": evidence, "recorded_at": row["recorded_at"]})
        date_observations.sort(key=lambda item: (item["observed_at"], item["observation_id"]))
        from .article_dates import ArticleDateImportError, select_article_dates
        try:
            date_fields = select_article_dates(date_observations, collection_times=[available_content.get("collected_at")],
                acquisition_observations=acquisition_observations)
        except ArticleDateImportError as exc:
            raise RegistryContractError("invalid article date evidence") from exc

        policy = article.get("display_policy")
        content_payload = {}
        if content:
            content_payload = {"content_version_id": article.get("current_content_version_id"),
                "content_type": content.get("content_type"), "source_bytes": content.get("source_bytes"),
                "extraction_method": content.get("extraction_method"), "extraction_version": content.get("extraction_version"),
                "fetched_at": content.get("first_fetched_at"), "collected_at": available_content.get("collected_at")}
            if policy == "summary_excerpt":
                content_payload["supporting_excerpt"] = article_preview(content["markdown_content"])
            elif policy == "full_markdown":
                content_payload["markdown"] = article_body_markdown(content["markdown_content"])
        enrichment_payload = {
            "summary": enrichment.get("summary") if enrichment else None,
            "categories": _json_string_list(enrichment.get("categories_json")) if enrichment else [],
            "keywords": _json_string_list(enrichment.get("keywords_json")) if enrichment else [],
            "language": enrichment.get("language") if enrichment else None,
            "content_version_id": article.get("current_content_version_id") if enrichment else None,
            "article_version_id": enrichment.get("article_version_id") if enrichment else None,
            "generator": ({"kind": enrichment.get("generator_kind"), "name": enrichment.get("generator_name"),
                "version": enrichment.get("generator_version"), "generated_at": enrichment.get("generated_at")} if enrichment else None),
        }
        has_enrichment = enrichment is not None
        if has_enrichment:
            _log_db_annotation_precedence(enrichment_payload["summary"], enrichment_payload["categories"],
                enrichment_payload["keywords"], source_annotation)
        report_metadata = {"categories": _ordered_unique(report_categories), "keywords": _ordered_unique(report_keywords)}
        categories = enrichment_payload["categories"] if has_enrichment else fallback_categories
        keywords = enrichment_payload["keywords"] if has_enrichment else fallback_keywords
        summary = enrichment_payload["summary"] if has_enrichment else annotation_summary
        provenance = ("manual_enrichment" if has_enrichment and enrichment.get("generator_kind") == "manual"
            else "content_enrichment" if has_enrichment else fallback_provenance)
        metadata_provenance = {"categories": provenance, "keywords": provenance}
        original_url = appearances[0]["original_url"] if appearances else article["canonical_url"]
        payload = {
            "article_id": article_id, "title": source_annotation.title if source_annotation else current_version.get("observed_title"),
            "summary": summary, "summary_provenance": provenance, "report_summary": current_version.get("observed_summary"),
            "current_version_id": article.get("current_version_id"), "canonical_url": article["canonical_url"],
            "original_url": original_url, "source": source.get("hostname"), "publisher": source.get("display_name"),
            "first_seen": article.get("first_seen"), "last_seen": article.get("last_seen"), **date_fields,
            "date_observations": date_observations, "document_kind": article.get("document_kind"),
            "publication_eligible": bool(article.get("publication_eligible")), "display_policy": policy,
            "appearances": appearances,
            "latest_fetch": ({key: fetch.get(key) for key in ("fetched_at", "fetch_status", "http_status", "content_type", "error_code")} if fetch else None),
            "content": content_payload, "available_content": available_content,
            "acquisition_observations": acquisition_observations, "enrichment": enrichment_payload,
            "report_metadata": report_metadata, "categories": categories, "keywords": keywords,
            "metadata_provenance": metadata_provenance,
            "source_annotation": ({"source_basis": source_annotation.source_basis, "source_url": source_annotation.source_url,
                "generated_on": source_annotation.generated_on} if source_annotation else None),
        }
        from .publication import approved_display, digest
        payload.update(approved_display(snapshot))
        payload["is_visible"] = True
        payload["published_candidate_sha256"] = self._published_candidate_sha(snapshot)
        payload["record_reference"] = {"entity_kind": "article", "entity_id": article_id,
            "candidate_sha256": payload["published_candidate_sha256"], "version_id": article.get("current_version_id")}
        if snapshot.get("sources"):
            payload.update(source=snapshot["sources"][0]["hostname"], publisher=snapshot["sources"][0]["display_name"])
        if snapshot.get("git_wiki"):
            payload["available_content"] = {"markdown": snapshot["git_wiki"]["markdown"],
                "selection_basis": "approved_git_wiki", "static_snapshot_sha256": snapshot["git_wiki"]["sha256"]}
        pdf_occurrences = self._pdf_occurrences_from_snapshot(snapshot)
        if pdf_occurrences:
            payload["pdf_occurrences"] = pdf_occurrences
        return payload

    def _article_card_from_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        if snapshot.get("static_payload"):
            payload = dict(snapshot["static_payload"])
            from .publication import approved_display
            payload.update(approved_display(snapshot))
            if not payload.get("content_version_id"):
                enrichment = payload.get("enrichment") or {}
                content = payload.get("content") or {}
                payload["content_version_id"] = enrichment.get("content_version_id") or content.get("content_version_id")
            payload.setdefault("published_candidate_sha256", self._published_candidate_sha(snapshot))
            payload["record_reference"] = {"entity_kind": "article", "entity_id": snapshot["entity_id"],
                "candidate_sha256": payload["published_candidate_sha256"], "version_id": payload.get("current_version_id")}
            return payload
        tables = snapshot.get("tables", {})
        article = next((row for row in tables.get("articles", []) if row.get("article_id") == snapshot["entity_id"]), None)
        if article is None:
            raise RegistryContractError("published article snapshot has no article row")
        article_id = article["article_id"]
        version = next((row for row in tables.get("article_versions", []) if row.get("version_id") == article.get("current_version_id")), {})
        source = next((row for row in snapshot.get("sources", []) if row.get("source_id") == article.get("source_id")), {})
        annotation_data = snapshot.get("fallback_annotations", {}).get(article.get("canonical_url"))
        annotation = ArticleAnnotation(**annotation_data) if annotation_data else None
        enrichments = [row for row in tables.get("article_enrichments", []) if row.get("status") == "complete" and (
            (article.get("current_content_version_id") is not None and row.get("content_version_id") == article.get("current_content_version_id"))
            or (row.get("content_version_id") is None and row.get("article_id") == article_id
                and row.get("article_version_id") == article.get("current_version_id")))]
        enrichment = max(enrichments, key=lambda row: (row.get("content_version_id") is not None,
            row.get("generated_at") or "", row.get("enrichment_id") or ""), default=None)
        content = next((row for row in tables.get("article_content_versions", [])
            if row.get("article_id") == article_id
            and row.get("content_version_id") == article.get("current_content_version_id")), None)
        summary = enrichment.get("summary") if enrichment else annotation.summary if annotation else None
        categories = _json_string_list(enrichment.get("categories_json")) if enrichment else list(annotation.categories) if annotation else []
        keywords = _json_string_list(enrichment.get("keywords_json")) if enrichment else list(annotation.keywords) if annotation else []
        provenance = ("manual_enrichment" if enrichment and enrichment.get("generator_kind") == "manual"
            else "content_enrichment" if enrichment else annotation.provenance if annotation else None)

        report_by_id = {row.get("report_id"): row for row in snapshot.get("dependencies", {}).get("reports", [])}
        discoveries = {row.get("discovery_id"): row for row in tables.get("discoveries", [])}
        versions = {row.get("version_id"): row for row in tables.get("article_versions", [])}
        appearances = []
        for row in tables.get("report_appearances", []):
            report, discovery = report_by_id.get(row.get("report_id")), discoveries.get(row.get("discovery_id"))
            if row.get("article_id") != article_id or report is None or discovery is None:
                continue
            appeared_version = versions.get(row.get("version_id"), {})
            appearances.append({"report_date": report.get("report_date"), "section": row.get("section"),
                "pillar": row.get("pillar"), "ordinal": row.get("ordinal"), "title": appeared_version.get("observed_title"),
                "summary": appeared_version.get("observed_summary"), "original_url": discovery.get("raw_url")})
        displays = {}
        for row in appearances:
            fallback = snapshot.get("report_fallbacks", {}).get(row["report_date"])
            if not fallback:
                continue
            try:
                parsed = ParsedReport(**{**fallback, "path": Path(fallback["path"]),
                    "articles": tuple(ParsedArticle(**item) for item in fallback["articles"])})
            except (KeyError, TypeError, ValueError) as exc:
                raise RegistryContractError("invalid approved report fallback") from exc
            source_article = self._source_article(parsed, row["ordinal"] or 0, row["original_url"] or "")
            item_annotation = self._annotation_for_url({url: ArticleAnnotation(**value) for url, value in snapshot.get("fallback_annotations", {}).items()}, row["original_url"] or "")
            bundle = _complete_source_report_bundle(source_article)
            if not enrichment and source_article and not annotation and bundle is not None:
                summary, categories, keywords = bundle
                provenance = "source_report"
                row["summary"] = source_article.summary
            if item_annotation and not enrichment:
                summary, categories, keywords = item_annotation.summary, list(item_annotation.categories), list(item_annotation.keywords)
                provenance = item_annotation.provenance
                row["summary"] = item_annotation.summary
            row["categories"] = list(item_annotation.categories) if item_annotation else list(source_article.categories) if source_article else []
            row["keywords"] = list(item_annotation.keywords) if item_annotation else list(source_article.keywords) if source_article else []
        appearances.sort(key=lambda item: (item.get("report_date") or "", item.get("ordinal") or 0), reverse=True)
        from .publication import approved_display, digest
        payload = {
            "article_id": article_id, "canonical_url": article["canonical_url"], "first_seen": article.get("first_seen"),
            "last_seen": article.get("last_seen"), "document_kind": article.get("document_kind"),
            "publication_eligible": bool(article.get("publication_eligible")), "display_policy": article.get("display_policy"),
            "source": source.get("hostname"), "publisher": source.get("display_name"),
            "title": annotation.title if annotation else version.get("observed_title"),
            "report_summary": version.get("observed_summary"), "summary": summary, "categories": categories, "keywords": keywords,
            "pdf_occurrence_count": sum(1 for row in tables.get("pdf_intake_article_occurrences", [])
                if row.get("summary_basis") != "verbatim_pdf_calendar_row"),
            "is_visible": True, "published_candidate_sha256": self._published_candidate_sha(snapshot),
            "current_version_id": article.get("current_version_id"),
            "content_version_id": article.get("current_content_version_id") if enrichment or content else None,
            "metadata_provenance": {"categories": provenance, "keywords": provenance},
            "record_reference": {"entity_kind": "article", "entity_id": article_id,
                "candidate_sha256": self._published_candidate_sha(snapshot), "version_id": article.get("current_version_id")},
            "appearances": appearances,
        }
        payload.update(approved_display(snapshot))
        return payload

    def _pdf_article_from_snapshot(self, snapshot: dict[str, Any], *, allowed_occurrence_ids=None) -> dict[str, Any]:
        tables = snapshot.get("tables", {})
        row = next((item for item in tables.get("pdf_intake_articles", []) if item.get("article_id") == snapshot["entity_id"]), None)
        if row is None:
            raise RegistryContractError("published PDF article snapshot has no article row")
        all_occurrences = self._pdf_occurrences_from_snapshot(snapshot)
        occurrences = [item for item in all_occurrences if allowed_occurrence_ids is None or item["occurrence_id"] in allowed_occurrence_ids]
        if not occurrences:
            raise RegistryNotFoundError("article not found")
        canonical_url = row["canonical_url"]
        try:
            classification = json.loads(row["type_safe_classification_json"]) if row.get("type_safe_classification_json") else None
        except (json.JSONDecodeError, TypeError) as exc:
            raise RegistryContractError("invalid PDF article classification data") from exc
        host = (urlsplit(canonical_url).hostname or "unknown").removeprefix("www.").lower()
        dates = [item.get("report_date") for item in occurrences if item.get("report_date")]
        payload = {"article_id": row["article_id"], "canonical_url": canonical_url,
            "title": row.get("title") or occurrences[0].get("anchor_text") or occurrences[0].get("title"),
            "summary": occurrences[0].get("summary"), "source": host, "publisher": host,
            "first_seen": min(dates) if dates else None, "last_seen": max(dates) if dates else None,
            "source_kind": "pdf", "type_safe_classification": classification, "occurrences": occurrences,
            "core_article_id": row.get("core_article_id"), "confirmation_basis": row.get("confirmation_basis")}
        from .publication import approved_display
        supplements = [item for item in snapshot.get("supplements", []) if item.get("source_ref", "").startswith("pdf-classification:")
            and item.get("entity_id") == row["article_id"]]
        if supplements:
            evidence = json.loads(supplements[-1]["evidence_json"])
            pending = evidence["pending_enrichment"]
            payload.update(categories=pending["categories"], keywords=pending["keywords"],
                manual_enrichment={**pending, "generator_kind": "manual", "evidence": evidence,
                    "knowledge_id": supplements[-1]["knowledge_id"]})
        payload.update(approved_display(snapshot))
        payload["published_candidate_sha256"] = snapshot.get("_approved_candidate_sha256") or self._published_candidate_sha(snapshot)
        reference_id = (tables["articles"][0]["article_id"]
            if snapshot["entity_kind"] == "article" else row["article_id"])
        payload["record_reference"] = {"entity_kind": snapshot["entity_kind"], "entity_id": reference_id,
            "candidate_sha256": payload["published_candidate_sha256"], "version_id": None}
        return payload

    def _acquisition_snapshot_projection(self, snapshot, article_id, current_version_id, display_policy):
        tables = snapshot.get("tables", {})
        dependencies = snapshot.get("dependencies", {})
        fetches = {row.get("fetch_id"): row for row in tables.get("article_fetches", [])}
        items = [row for row in tables.get("acquisition_items", []) if row.get("article_id") == article_id]
        if "acquisition_searches" in dependencies:
            searches = {(row.get("batch_id"), row.get("search_ref")): row
                for row in dependencies["acquisition_searches"]}
        else:
            self._prepare_legacy_acquisition_searches((snapshot,))
            searches = self._legacy_acquisition_search_cache
        observations = []
        for row in sorted(items, key=lambda value: (value.get("discovered_at") or "", value.get("acquisition_item_id") or "")):
            try:
                origins = json.loads(row.get("origins_json") or "[]")
                date_evidence = json.loads(row["publication_date_evidence_json"]) if row.get("publication_date_evidence_json") else None
            except (json.JSONDecodeError, TypeError) as exc:
                raise RegistryContractError("invalid acquisition observation data") from exc
            if not isinstance(origins, list) or any(not isinstance(origin, dict) for origin in origins):
                raise RegistryContractError("invalid acquisition observation data")
            for origin in origins:
                search = searches.get((row.get("batch_id"), origin.get("search_ref")))
                if search:
                    origin["search"] = {key: search.get(key) for key in ("batch_id", "search_ref", "search_id", "query", "engine", "status", "attempted_at")}
            item = {key: row.get(key) for key in (
                "acquisition_item_id", "batch_id", "ordinal", "raw_url", "source_name", "title", "summary",
                "discovered_at", "discovery_kind", "discovery_ref", "publication_date", "date_status",
                "selection_status", "selection_reason", "update_status", "material_status", "fetch_id",
                "content_version_id", "processing_status", "processing_error", "resolved_by_fetch_id")}
            item["publication_date_evidence"] = date_evidence
            item["origins"] = origins
            fetch = fetches.get(row.get("fetch_id"), {})
            resolved = fetches.get(row.get("resolved_by_fetch_id"), {})
            selected_fetch = fetch if fetch.get("fetch_status") == "success" else resolved if resolved.get("fetch_status") == "success" else {}
            item["collected_at"] = selected_fetch.get("fetched_at")
            item["fetch"] = {"status": fetch.get("fetch_status"), "fetched_at": fetch.get("fetched_at"), "content_version_id": fetch.get("content_version_id")}
            if row.get("resolved_by_fetch_id"):
                item["resolved_fetch"] = {"status": resolved.get("fetch_status"), "fetched_at": resolved.get("fetched_at"), "content_version_id": resolved.get("content_version_id")}
            observations.append(item)
        selected, content = select_snapshot_article_content(snapshot, article_id, current_version_id)
        if content is None:
            return observations, {}
        available = {key: content.get(key) for key in ("content_version_id", "content_type", "source_bytes", "extraction_method", "extraction_version", "first_fetched_at")}
        available.update({key: selected[key] for key in ("fetch_id", "acquisition_item_id")})
        available["collected_at"] = selected["fetched_at"]
        available["selection_basis"] = "latest_successful_acquisition_or_current_content"
        if display_policy == "full_markdown":
            available["markdown"] = article_body_markdown(content["markdown_content"])
        elif display_policy == "summary_excerpt":
            available["supporting_excerpt"] = article_preview(content["markdown_content"])
        return observations, available

    def _pdf_occurrences_from_snapshot(self, snapshot):
        tables, dependencies = snapshot.get("tables", {}), snapshot.get("dependencies", {})
        documents = {row.get("document_sha256"): row for row in dependencies.get("pdf_intake_documents", [])}
        sources = self._snapshot_pdf_document_sources(dependencies)
        attempts = tables.get("article_check_attempts", [])
        values = []
        for row in tables.get("pdf_intake_article_occurrences", []):
            try:
                value = json.loads(row["occurrence_json"])
            except (json.JSONDecodeError, TypeError) as exc:
                raise RegistryContractError("invalid PDF article occurrence data") from exc
            if not isinstance(value, dict) or value.get("summary_basis") == "verbatim_pdf_calendar_row":
                continue
            value.update(occurrence_id=row["occurrence_id"], source_document_sha256=row["source_document_sha256"])
            for field in ("page", "raw_url", "publication_date", "content_sha256"):
                value[field] = row.get(field)
            source_sha = row["source_document_sha256"]
            source_cache_key = (snapshot.get("_approved_candidate_sha256")
                or self._published_candidate_sha(snapshot), source_sha)
            value["source_observations"] = self._pdf_source_cache.setdefault(
                source_cache_key, sources.get(source_sha, []))
            document = documents.get(row["source_document_sha256"])
            if document:
                value.update(source_filename=document.get("filename"), period_start=document.get("period_start"),
                    period_end=document.get("period_end"), imported_at=document.get("imported_at"))
                parsed = self._pdf_document_cache.get(source_sha)
                if parsed is None:
                    try:
                        parsed = json.loads(document.get("document_json") or "{}")
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise RegistryContractError("invalid PDF document data") from exc
                    if not isinstance(parsed, dict):
                        raise RegistryContractError("invalid PDF document data")
                    self._pdf_document_cache[source_sha] = parsed
                from climate_monitor.pdf_intake import report_update_fields
                value["report_fields"] = report_update_fields(value, parsed)
                value["structured_report"] = any("IN WINDOW" in page.get("text", "") for page in parsed.get("pages", []))
            from .information_checks import latest_checks_from_attempts
            value.update(latest_checks_from_attempts("articles", value, attempts,
                packet_cache=getattr(self, "_pdf_check_packet_cache", None),
                candidate_binding=(snapshot.get("_approved_candidate_sha256")
                    or self._published_candidate_sha(snapshot))))
            # report_date is the legacy ordering key, but it was never added
            # to the public occurrence DTO from the storage row.
            values.append(((row.get("report_date") or "", row.get("page") or -1,
                row.get("occurrence_id") or ""), value))
        values.sort(key=lambda item: item[0], reverse=True)
        # Keep the identity of every approved occurrence in the read DTO.
        # Wiki renderers deduplicate identical passages at their display edge;
        # API callers and review/archive flows still need the exact checked ID.
        return [value for _sort_key, value in values]

    @_direct_public_read
    def article(self, article_id: str) -> dict[str, Any]:
        if not article_id or len(article_id) > 128 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in article_id):
            raise RegistryQueryError("invalid article id")
        if self.static_snapshot is not None:
            item = next((item for item in self.static_snapshot.get("articles", []) if item["article_id"] == article_id), None)
            if item is None:
                raise RegistryNotFoundError("article not found")
            return dict(item)
        if getattr(self, "_snapshot_direct", False):
            with self.connect() as connection:
                if connection.execute("PRAGMA user_version").fetchone()[0] >= 21:
                    snapshot = self._public_candidates("article", entity_ids={"article": [article_id]}).get(("article", article_id))
                    if snapshot is None:
                        raise RegistryNotFoundError("article not found")
                    return self._article_from_snapshot(snapshot)
        with self.connect() as connection:
            if self.public:
                from .publication import snapshot_metadata
                snapshot = snapshot_metadata(connection, "article", article_id)
                if snapshot and snapshot.get("static_payload"):
                    payload = dict(snapshot["static_payload"])
                    from .publication import approved_display
                    payload.update(approved_display(snapshot))
                    payload.setdefault("published_candidate_sha256", self._published_candidate_sha(snapshot))
                    payload["record_reference"] = {"entity_kind": "article", "entity_id": snapshot["entity_id"],
                        "candidate_sha256": payload["published_candidate_sha256"], "version_id": payload.get("current_version_id")}
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
                payload["record_reference"] = {"entity_kind": "article", "entity_id": article_id,
                    "candidate_sha256": payload["published_candidate_sha256"], "version_id": payload.get("current_version_id")}
                if snapshot.get("git_wiki"):
                    payload["available_content"] = {"markdown": snapshot["git_wiki"]["markdown"],
                        "selection_basis": "approved_git_wiki", "static_snapshot_sha256": snapshot["git_wiki"]["sha256"]}
        if pdf_occurrences:
            payload["pdf_occurrences"] = pdf_occurrences
        return payload
