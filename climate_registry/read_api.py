from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Iterator


EXPECTED_SCHEMA_VERSION = 3
REQUIRED_TABLES = frozenset(
    {
        "sources",
        "reports",
        "articles",
        "article_versions",
        "discoveries",
        "report_appearances",
        "article_content_versions",
        "article_fetches",
        "article_enrichments",
    }
)
REQUIRED_COLUMNS = {
    "sources": {"source_id", "hostname", "display_name"},
    "reports": {
        "report_id", "report_date", "report_title", "cadence", "report_format",
        "sites_checked", "sites_succeeded", "sites_failed", "parse_warnings_json",
    },
    "articles": {
        "article_id", "canonical_url", "source_id", "first_seen", "last_seen",
        "current_version_id", "current_content_version_id", "document_kind",
        "publication_eligible", "display_policy",
    },
    "article_versions": {
        "version_id", "article_id", "observed_title", "observed_summary",
    },
    "discoveries": {
        "discovery_id", "report_id", "article_id", "version_id", "raw_url",
        "ordinal", "section", "pillar",
    },
    "report_appearances": {
        "report_id", "article_id", "version_id", "discovery_id", "section", "pillar",
        "ordinal", "observation_status",
    },
    "article_content_versions": {
        "content_version_id", "article_id", "markdown_content", "content_type",
        "source_bytes", "extraction_method", "extraction_version", "first_fetched_at",
    },
    "article_fetches": {
        "fetch_id", "article_id", "fetch_status", "fetched_at", "http_status",
        "content_type", "content_version_id",
    },
    "article_enrichments": {
        "enrichment_id",
        "content_version_id",
        "status",
        "summary",
        "categories_json",
        "keywords_json",
        "language",
        "generator_kind",
        "generator_name",
        "generator_version",
        "generated_at",
    },
}


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


class RegistryReader:
    def __init__(self, database: str | Path, *, repository_root: str | Path):
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
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != EXPECTED_SCHEMA_VERSION:
            raise RegistryContractError("unsupported registry schema")
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if not REQUIRED_TABLES <= tables:
            raise RegistryContractError("incomplete registry schema")
        for table, expected_columns in REQUIRED_COLUMNS.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not expected_columns <= columns:
                raise RegistryContractError("incomplete registry schema")
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
        invalid_current_content = connection.execute(
            """
            SELECT 1
            FROM articles a
            LEFT JOIN article_content_versions cv
              ON cv.content_version_id = a.current_content_version_id
            WHERE a.current_content_version_id IS NOT NULL
              AND (cv.content_version_id IS NULL OR cv.article_id IS NOT a.article_id)
            LIMIT 1
            """
        ).fetchone()
        if invalid_current_content:
            raise RegistryContractError("invalid article content ownership")
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
                "schema_version": EXPECTED_SCHEMA_VERSION,
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

    def report(self, report_date: str) -> dict[str, Any]:
        validate_report_date(report_date)
        with self.connect() as connection:
            report = connection.execute(
                """
                SELECT report_id, report_date, report_title, cadence, report_format,
                       sites_checked, sites_succeeded, sites_failed, parse_warnings_json
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
                       s.hostname AS source
                FROM report_appearances ra
                JOIN articles a ON a.article_id = ra.article_id
                JOIN article_versions av ON av.version_id = ra.version_id
                JOIN sources s ON s.source_id = a.source_id
                WHERE ra.report_id = ?
                ORDER BY ra.ordinal, a.article_id
                """,
                (report["report_id"],),
            ).fetchall()
        return {
            "report_date": report["report_date"],
            "report_title": report["report_title"],
            "cadence": report["cadence"],
            "report_format": report["report_format"],
            "monitoring": {
                "status": _monitoring_status(report),
                "sites_checked": report["sites_checked"],
                "sites_succeeded": report["sites_succeeded"],
                "sites_failed": report["sites_failed"],
                "warning_count": len(_json_string_list(report["parse_warnings_json"])),
            },
            "articles": [dict(row) for row in appearances],
        }

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
                SELECT r.report_date, r.report_title, ra.section, ra.pillar, ra.ordinal,
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
                SELECT fetched_at, fetch_status, http_status, content_type
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
        original_url = appearances[0]["original_url"] if appearances else article["canonical_url"]
        return {
            "article_id": article["article_id"],
            "title": article["title"],
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
            "appearances": [dict(row) for row in appearances],
            "latest_fetch": dict(fetch) if fetch else None,
            "content": content_payload,
            "enrichment": enrichment_payload,
        }
