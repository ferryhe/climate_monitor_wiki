from __future__ import annotations

import sqlite3

MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (
        1,
        "initial_article_registry",
        """
        CREATE TABLE sources (
            source_id TEXT PRIMARY KEY,
            hostname TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );

        CREATE TABLE reports (
            report_id TEXT PRIMARY KEY,
            report_date TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL UNIQUE,
            report_title TEXT NOT NULL,
            report_sha256 TEXT NOT NULL,
            cadence TEXT NOT NULL CHECK (cadence IN ('weekly', 'legacy-daily')),
            report_format TEXT NOT NULL,
            sites_checked INTEGER,
            sites_succeeded INTEGER,
            sites_failed INTEGER,
            parse_warnings_json TEXT NOT NULL
        );

        CREATE TABLE articles (
            article_id TEXT PRIMARY KEY,
            canonical_url TEXT NOT NULL UNIQUE,
            source_id TEXT NOT NULL REFERENCES sources(source_id),
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            current_version_id TEXT REFERENCES article_versions(version_id) DEFERRABLE INITIALLY DEFERRED
        );

        CREATE TABLE url_aliases (
            raw_url TEXT PRIMARY KEY,
            canonical_url TEXT NOT NULL,
            article_id TEXT NOT NULL REFERENCES articles(article_id),
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            times_seen INTEGER NOT NULL CHECK (times_seen > 0)
        );

        CREATE TABLE article_versions (
            version_id TEXT PRIMARY KEY,
            article_id TEXT NOT NULL REFERENCES articles(article_id),
            observed_title TEXT NOT NULL,
            canonical_title TEXT NOT NULL,
            observed_summary TEXT NOT NULL,
            content_fingerprint TEXT NOT NULL,
            content_basis TEXT NOT NULL CHECK (content_basis = 'report-title-summary'),
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            UNIQUE (article_id, content_fingerprint)
        );

        CREATE TABLE discoveries (
            discovery_id TEXT PRIMARY KEY,
            report_id TEXT NOT NULL REFERENCES reports(report_id),
            ordinal INTEGER NOT NULL CHECK (ordinal > 0),
            section TEXT NOT NULL,
            pillar TEXT CHECK (pillar IN ('A', 'B') OR pillar IS NULL),
            article_id TEXT NOT NULL REFERENCES articles(article_id),
            version_id TEXT NOT NULL REFERENCES article_versions(version_id),
            raw_url TEXT NOT NULL,
            observed_title TEXT NOT NULL,
            observed_summary TEXT NOT NULL,
            selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
            duplicate_of TEXT REFERENCES discoveries(discovery_id),
            UNIQUE (report_id, ordinal)
        );

        CREATE TABLE report_appearances (
            report_id TEXT NOT NULL REFERENCES reports(report_id),
            article_id TEXT NOT NULL REFERENCES articles(article_id),
            version_id TEXT NOT NULL REFERENCES article_versions(version_id),
            discovery_id TEXT NOT NULL UNIQUE REFERENCES discoveries(discovery_id),
            section TEXT NOT NULL,
            pillar TEXT CHECK (pillar IN ('A', 'B') OR pillar IS NULL),
            ordinal INTEGER NOT NULL CHECK (ordinal > 0),
            disposition TEXT NOT NULL CHECK (disposition IN ('new', 'updated', 'previously-seen')),
            PRIMARY KEY (report_id, article_id)
        );

        CREATE INDEX idx_articles_title_versions ON article_versions(canonical_title);
        CREATE INDEX idx_discoveries_article ON discoveries(article_id, report_id);
        CREATE INDEX idx_appearances_article ON report_appearances(article_id, report_id);
        """,
    ),
)


def apply_migrations(connection: sqlite3.Connection) -> list[int]:
    """Apply all pending schema migrations and return their version numbers."""

    if connection.in_transaction:
        raise sqlite3.ProgrammingError("cannot apply migrations inside an active transaction")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    installed: list[int] = []
    for version, name, sql in MIGRATIONS:
        if version in applied:
            continue
        escaped_name = name.replace("'", "''")
        try:
            connection.executescript(
                f"""
                BEGIN IMMEDIATE;
                {sql}
                INSERT INTO schema_migrations(version, name) VALUES ({version}, '{escaped_name}');
                PRAGMA user_version = {version};
                COMMIT;
                """
            )
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        installed.append(version)
    return installed
