from __future__ import annotations

import re
import sqlite3

from .schema import MIGRATIONS, apply_migrations


SCHEMA_VERSION = MIGRATIONS[-1][0]


class SchemaContractError(ValueError):
    """The database does not implement an exact supported registry contract."""


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
    "article_capture_resolutions": {
        "resolution_id", "report_id", "report_date", "report_sha256", "article_id",
        "canonical_url", "fetch_id", "failure_class", "http_status", "attempt_at",
        "fallback_source", "fallback_provenance", "bundle_json", "bundle_sha256",
        "validated_at",
    },
    "article_semantics": {
        "report_id", "report_sha256", "article_id", "canonical_url", "title", "summary",
        "categories_json", "keywords_json", "taxonomy_id", "taxonomy_raw_sha256",
        "bundle_sha256", "validated_at",
    },
}

# Tables introduced per migration. The contract is validated per deployed
# schema version, so an older database is never asked for a newer table and a
# newer database is never rejected for carrying one.
_V4_TABLES = frozenset({"article_capture_resolutions"})
_V5_TABLES = frozenset({"article_semantics"})

V3_TABLES = frozenset(REQUIRED_TABLE_COLUMNS) - _V4_TABLES - _V5_TABLES
V4_TABLES = frozenset(REQUIRED_TABLE_COLUMNS) - _V5_TABLES
V5_TABLES = frozenset(REQUIRED_TABLE_COLUMNS)
V6_TABLES = V5_TABLES

SUPPORTED_SCHEMA_VERSIONS = (3, 4, 5, 6)


def _required_tables(version: int) -> frozenset[str]:
    if version == 3:
        return V3_TABLES
    if version == 4:
        return V4_TABLES
    if version == 5:
        return V5_TABLES
    return V6_TABLES


def _required_columns(table: str, version: int) -> set[str]:
    columns = set(REQUIRED_TABLE_COLUMNS[table])
    if table == "article_semantics" and version == 5:
        columns.remove("report_id")
    return columns

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
    "article_capture_resolutions": {
        ("reports", ("report_id",), ("report_id",)),
        ("articles", ("article_id",), ("article_id",)),
        ("article_fetches", ("fetch_id",), ("fetch_id",)),
    },
    "article_semantics": {
        ("reports", ("report_id", "report_sha256"), ("report_id", "report_sha256")),
        ("report_appearances", ("report_id", "article_id"), ("report_id", "article_id")),
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
        "article_capture_resolutions_reject_replace",
        "article_capture_resolutions_validate_insert",
        "article_capture_resolutions_are_append_only_update",
        "article_capture_resolutions_are_append_only_delete",
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
        "idx_capture_resolutions_report_article",
        "idx_capture_resolutions_fetch",
        "idx_reports_id_sha256",
    }
)


def _normalize_sql(sql: str) -> str:
    normalized = re.sub(r"\s+", " ", sql.strip().rstrip(";")).casefold()
    return re.sub(r"\s*([(),])\s*", r"\1", normalized)


def _expected_object_sql(
    kind: str, names: frozenset[str], *, version: int
) -> dict[str, str]:
    migration_sql = "\n".join(
        migration[2] for migration in MIGRATIONS if migration[0] <= version
    )
    expected: dict[str, str] = {}
    for name in names:
        create = r"CREATE\s+(?:UNIQUE\s+)?INDEX" if kind == "INDEX" else rf"CREATE\s+{kind}"
        start = re.search(rf"{create}\s+{re.escape(name)}\b", migration_sql, re.IGNORECASE)
        if start is None:
            raise RuntimeError(f"schema migration is missing required {kind.lower()}: {name}")
        tail = migration_sql[start.start():]
        terminator = re.search(r"\bEND\s*;" if kind == "TRIGGER" else r";", tail, re.IGNORECASE)
        if terminator is None:
            raise RuntimeError(f"schema migration has incomplete {kind.lower()}: {name}")
        expected[name] = _normalize_sql(tail[: terminator.end()])
    return expected


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


def _golden_table_contracts(version: int) -> tuple[
    dict[str, str],
    dict[str, tuple[tuple[object, ...], ...]],
    dict[str, tuple[tuple[object, ...], ...]],
]:
    connection = sqlite3.connect(":memory:")
    try:
        apply_migrations(connection, target_version=version)
        required_tables = _required_tables(version)
        table_sql = {
            table: _normalize_sql(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).fetchone()[0]
            )
            for table in required_tables
        }
        columns = {
            table: _column_contract(connection, table)
            for table in required_tables
        }
        indexes = {
            table: _index_contract(connection, table)
            for table in required_tables
        }
        return table_sql, columns, indexes
    finally:
        connection.close()


GOLDEN_CONTRACTS = {
    version: _golden_table_contracts(version) for version in SUPPORTED_SCHEMA_VERSIONS
}


def _required_triggers(version: int) -> frozenset[str]:
    if version == 3:
        return frozenset(
            name for name in REQUIRED_TRIGGERS
            if not name.startswith("article_capture_resolutions_")
        )
    return REQUIRED_TRIGGERS


def _required_indexes(version: int) -> frozenset[str]:
    names = REQUIRED_INDEXES
    if version < 6:
        names = frozenset(name for name in names if name != "idx_reports_id_sha256")
    if version == 3:
        names = frozenset(
            name for name in names if not name.startswith("idx_capture_resolutions_")
        )
    return names


def _required_foreign_keys(
    table: str, version: int
) -> set[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    keys = set(REQUIRED_FOREIGN_KEYS.get(table, set()))
    if table == "article_semantics" and version < 6:
        keys.clear()
    return keys


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


def validate_registry_contract(connection: sqlite3.Connection) -> int:
    """Validate an exact supported registry contract using read-only queries."""
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise SchemaContractError("unsupported registry schema")

    required_tables = _required_tables(version)
    expected_table_sql, expected_columns, expected_table_indexes = GOLDEN_CONTRACTS[version]

    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not row[0].startswith("sqlite_")
    }
    missing_tables = set(required_tables) - tables
    if missing_tables:
        raise SchemaContractError(
            f"incomplete registry schema: {sorted(missing_tables)[0]}"
        )
    if tables - set(required_tables):
        raise SchemaContractError("invalid registry tables contract")
    for table in required_tables:
        required_columns = _required_columns(table, version)
        if table not in tables:
            raise SchemaContractError(f"incomplete registry schema: {table}")
        actual = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not required_columns <= actual:
            raise SchemaContractError(f"incomplete registry schema: {table}")
        actual_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if actual_sql_row is None or _normalize_sql(actual_sql_row[0]) != expected_table_sql[table]:
            raise SchemaContractError(f"invalid registry table constraints: {table}")
        if _column_contract(connection, table) != expected_columns[table]:
            raise SchemaContractError(f"invalid registry column contract: {table}")
        expected_indexes_for_table = set(expected_table_indexes[table])
        actual_indexes = set(_index_contract(connection, table))
        if not expected_indexes_for_table <= actual_indexes:
            raise SchemaContractError(f"invalid registry unique/index contract: {table}")
        expected_unique = {contract for contract in expected_indexes_for_table if contract[1] == 1}
        actual_unique = {contract for contract in actual_indexes if contract[1] == 1}
        if actual_unique != expected_unique:
            raise SchemaContractError(f"unexpected registry unique index: {table}")

    applied = [
        (row[0], row[1])
        for row in connection.execute("SELECT version, name FROM schema_migrations ORDER BY version")
    ]
    expected_migrations = [
        (migration_version, name)
        for migration_version, name, _ in MIGRATIONS
        if migration_version <= version
    ]
    if applied != expected_migrations:
        raise SchemaContractError("invalid migration metadata")

    for table in required_tables:
        expected_keys = _required_foreign_keys(table, version)
        if not expected_keys <= _foreign_key_contracts(connection, table):
            raise SchemaContractError("incomplete registry foreign keys")

    actual_triggers = {
        row[0]: _normalize_sql(row[1])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND sql IS NOT NULL"
        )
    }
    expected_trigger_sql = _expected_object_sql(
        "TRIGGER", _required_triggers(version), version=version
    )
    if actual_triggers != expected_trigger_sql:
        raise SchemaContractError("invalid registry triggers contract")

    actual_indexes = {
        row[0]: _normalize_sql(row[1])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        )
    }
    expected_index_sql = _expected_object_sql(
        "INDEX", _required_indexes(version), version=version
    )
    if not expected_index_sql.items() <= actual_indexes.items():
        raise SchemaContractError("invalid registry indexes contract")

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
    return version


def validate_v3_contract(connection: sqlite3.Connection) -> None:
    """Compatibility wrapper validating either supported deployment schema."""
    validate_registry_contract(connection)
