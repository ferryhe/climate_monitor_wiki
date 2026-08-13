from __future__ import annotations

import re
import sqlite3

from .schema import MIGRATIONS, apply_migrations


SCHEMA_VERSION = MIGRATIONS[-1][0]


class SchemaContractError(ValueError):
    """The database does not implement the immutable registry v3 contract."""


REQUIRED_TABLE_COLUMNS = {
    "schema_migrations": {"version", "name", "applied_at"},
    "sources": {"source_id", "hostname", "display_name", "first_seen", "last_seen"},
    "reports": {
        "report_id", "report_date", "filename", "report_title", "report_sha256",
        "cadence", "report_format", "sites_checked", "sites_succeeded", "sites_failed",
        "parse_warnings_json",
    },
    "articles": {
        "article_id", "canonical_url", "source_id", "first_seen", "last_seen",
        "current_version_id", "document_kind", "publication_eligible", "exclusion_reason",
        "current_content_version_id", "display_policy",
    },
    "url_aliases": {
        "raw_url", "canonical_url", "article_id", "first_seen", "last_seen", "times_seen",
    },
    "article_versions": {
        "version_id", "article_id", "observed_title", "canonical_title", "observed_summary",
        "content_fingerprint", "content_basis", "first_seen", "last_seen",
    },
    "discoveries": {
        "discovery_id", "report_id", "ordinal", "section", "pillar", "article_id",
        "version_id", "raw_url", "observed_title", "observed_summary", "selected",
        "duplicate_of",
    },
    "report_appearances": {
        "report_id", "article_id", "version_id", "discovery_id", "section", "pillar",
        "ordinal", "disposition", "observation_status", "external_content_change",
    },
    "article_content_versions": {
        "content_version_id", "article_id", "content_sha256", "markdown_content",
        "markdown_sha256", "content_type", "source_bytes", "extraction_method",
        "extraction_version", "first_fetched_at",
    },
    "article_fetches": {
        "fetch_id", "article_id", "requested_url", "final_url", "fetched_at", "fetch_status",
        "http_status", "content_type", "etag", "last_modified", "error_code", "error_message",
        "content_version_id",
    },
    "article_enrichments": {
        "enrichment_id", "content_version_id", "status", "summary", "categories_json",
        "keywords_json", "language", "generator_kind", "generator_name", "generator_version",
        "generated_at", "error_code", "error_message",
    },
}

REQUIRED_FOREIGN_KEYS = {
    "articles": {
        ("sources", ("source_id",), ("source_id",)),
        ("article_versions", ("current_version_id",), ("version_id",)),
        ("article_content_versions", ("current_content_version_id",), ("content_version_id",)),
    },
    "article_content_versions": {
        ("articles", ("article_id",), ("article_id",)),
    },
    "url_aliases": {
        ("articles", ("article_id",), ("article_id",)),
    },
    "article_versions": {
        ("articles", ("article_id",), ("article_id",)),
    },
    "discoveries": {
        ("reports", ("report_id",), ("report_id",)),
        ("articles", ("article_id",), ("article_id",)),
        ("article_versions", ("version_id",), ("version_id",)),
        ("discoveries", ("duplicate_of",), ("discovery_id",)),
    },
    "report_appearances": {
        ("reports", ("report_id",), ("report_id",)),
        ("articles", ("article_id",), ("article_id",)),
        ("article_versions", ("version_id",), ("version_id",)),
        ("discoveries", ("discovery_id",), ("discovery_id",)),
    },
    "article_fetches": {
        ("articles", ("article_id",), ("article_id",)),
        ("article_content_versions", ("content_version_id",), ("content_version_id",)),
        (
            "article_content_versions",
            ("article_id", "content_version_id"),
            ("article_id", "content_version_id"),
        ),
    },
    "article_enrichments": {
        ("article_content_versions", ("content_version_id",), ("content_version_id",)),
    },
}

REQUIRED_TRIGGERS = frozenset(
    {
        "articles_current_content_matches_article_insert",
        "articles_current_content_matches_article_update",
        "article_content_versions_are_immutable_update",
        "article_content_versions_are_immutable_delete",
        "article_fetches_are_append_only_update",
        "article_fetches_are_append_only_delete",
        "article_enrichments_are_append_only_update",
        "article_enrichments_are_append_only_delete",
    }
)

REQUIRED_INDEXES = frozenset(
    {
        "idx_articles_title_versions",
        "idx_discoveries_article",
        "idx_appearances_article",
        "idx_article_fetches_article_fetched",
        "idx_article_fetches_content_version",
        "idx_content_versions_article_fetched",
        "idx_enrichments_content_generated",
    }
)


def _normalize_sql(sql: str) -> str:
    normalized = re.sub(r"\s+", " ", sql.strip().rstrip(";")).casefold()
    return re.sub(r"\s*([(),])\s*", r"\1", normalized)


def _expected_object_sql(kind: str, names: frozenset[str]) -> dict[str, str]:
    migration_sql = "\n".join(migration[2] for migration in MIGRATIONS)
    expected: dict[str, str] = {}
    for name in names:
        start = re.search(rf"CREATE\s+{kind}\s+{re.escape(name)}\b", migration_sql, re.IGNORECASE)
        if start is None:
            raise RuntimeError(f"schema migration is missing required {kind.lower()}: {name}")
        tail = migration_sql[start.start():]
        terminator = re.search(r"\bEND\s*;" if kind == "TRIGGER" else r";", tail, re.IGNORECASE)
        if terminator is None:
            raise RuntimeError(f"schema migration has incomplete {kind.lower()}: {name}")
        expected[name] = _normalize_sql(tail[: terminator.end()])
    return expected


EXPECTED_TRIGGER_SQL = _expected_object_sql("TRIGGER", REQUIRED_TRIGGERS)
EXPECTED_INDEX_SQL = _expected_object_sql("INDEX", REQUIRED_INDEXES)


def _column_contract(connection: sqlite3.Connection, table: str) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (row[1], row[2].casefold(), row[3], row[4], row[5])
        for row in connection.execute(f"PRAGMA table_info({table})")
    )


def _index_contract(
    connection: sqlite3.Connection, table: str
) -> tuple[tuple[object, ...], ...]:
    contracts = []
    for row in connection.execute(f"PRAGMA index_list({table})"):
        name, unique, origin, partial = row[1], row[2], row[3], row[4]
        columns = tuple(
            detail[2]
            for detail in connection.execute(f"PRAGMA index_info({name})")
        )
        # Autoindex names are SQLite implementation details; their origin and
        # ordered column contract are stable and security-relevant.
        public_name = None if name.startswith("sqlite_autoindex_") else name
        contracts.append((public_name, unique, origin, partial, columns))
    return tuple(sorted(contracts, key=repr))


def _golden_table_contracts() -> tuple[
    dict[str, str],
    dict[str, tuple[tuple[object, ...], ...]],
    dict[str, tuple[tuple[object, ...], ...]],
]:
    connection = sqlite3.connect(":memory:")
    try:
        apply_migrations(connection)
        table_sql = {
            table: _normalize_sql(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()[0]
            )
            for table in REQUIRED_TABLE_COLUMNS
        }
        columns = {
            table: _column_contract(connection, table)
            for table in REQUIRED_TABLE_COLUMNS
        }
        indexes = {
            table: _index_contract(connection, table)
            for table in REQUIRED_TABLE_COLUMNS
        }
        return table_sql, columns, indexes
    finally:
        connection.close()


EXPECTED_TABLE_SQL, EXPECTED_COLUMN_CONTRACTS, EXPECTED_TABLE_INDEX_CONTRACTS = (
    _golden_table_contracts()
)


def _foreign_key_contracts(
    connection: sqlite3.Connection, table: str
) -> set[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    grouped: dict[int, tuple[str, list[tuple[int, str]], list[tuple[int, str]]]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
        identifier, sequence, target_table, source_column, target_column = row[:5]
        contract = grouped.setdefault(identifier, (target_table, [], []))
        contract[1].append((sequence, source_column))
        contract[2].append((sequence, target_column))
    return {
        (
            contract[0],
            tuple(column for _, column in sorted(contract[1])),
            tuple(column for _, column in sorted(contract[2])),
        )
        for contract in grouped.values()
    }


def validate_v3_contract(connection: sqlite3.Connection) -> None:
    """Validate registry v3 using read-only SQL and PRAGMA queries only."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise SchemaContractError("unsupported registry schema")

    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for table, expected_columns in REQUIRED_TABLE_COLUMNS.items():
        if table not in tables:
            raise SchemaContractError(f"incomplete registry schema: {table}")
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not expected_columns <= actual:
            raise SchemaContractError(f"incomplete registry schema: {table}")
        actual_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if actual_sql_row is None or _normalize_sql(actual_sql_row[0]) != EXPECTED_TABLE_SQL[table]:
            raise SchemaContractError(f"invalid registry table constraints: {table}")
        if _column_contract(connection, table) != EXPECTED_COLUMN_CONTRACTS[table]:
            raise SchemaContractError(f"invalid registry column contract: {table}")
        expected_indexes = set(EXPECTED_TABLE_INDEX_CONTRACTS[table])
        actual_indexes = set(_index_contract(connection, table))
        if not expected_indexes <= actual_indexes:
            raise SchemaContractError(f"invalid registry unique/index contract: {table}")
        expected_unique = {contract for contract in expected_indexes if contract[1] == 1}
        actual_unique = {contract for contract in actual_indexes if contract[1] == 1}
        if actual_unique != expected_unique:
            raise SchemaContractError(f"unexpected registry unique index: {table}")

    applied = [
        (row[0], row[1])
        for row in connection.execute("SELECT version, name FROM schema_migrations ORDER BY version")
    ]
    expected_migrations = [(version, name) for version, name, _ in MIGRATIONS]
    if applied != expected_migrations:
        raise SchemaContractError("invalid migration metadata")

    for table, expected_keys in REQUIRED_FOREIGN_KEYS.items():
        if not expected_keys <= _foreign_key_contracts(connection, table):
            raise SchemaContractError("incomplete registry foreign keys")

    actual_triggers = {
        row[0]: _normalize_sql(row[1])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND sql IS NOT NULL"
        )
    }
    if actual_triggers != EXPECTED_TRIGGER_SQL:
        raise SchemaContractError("invalid registry triggers contract")

    actual_indexes = {
        row[0]: _normalize_sql(row[1])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        )
    }
    if any(actual_indexes.get(name) != sql for name, sql in EXPECTED_INDEX_SQL.items()):
        raise SchemaContractError("incomplete registry indexes")

    invalid_pointer = connection.execute(
        """
        SELECT 1
        FROM articles a
        LEFT JOIN article_content_versions c
          ON c.content_version_id = a.current_content_version_id
         AND c.article_id = a.article_id
        WHERE a.current_content_version_id IS NOT NULL
          AND c.content_version_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if invalid_pointer is not None:
        raise SchemaContractError("invalid article content ownership: current content version")
