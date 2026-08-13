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
    (
        2,
        "persistent_registry_policy",
        """
        ALTER TABLE articles ADD COLUMN document_kind TEXT NOT NULL DEFAULT 'article'
            CHECK (document_kind IN ('article', 'report', 'topic_index', 'landing_page'));
        ALTER TABLE articles ADD COLUMN publication_eligible INTEGER NOT NULL DEFAULT 1
            CHECK (publication_eligible IN (0, 1));
        ALTER TABLE articles ADD COLUMN exclusion_reason TEXT;

        ALTER TABLE report_appearances ADD COLUMN observation_status TEXT NOT NULL DEFAULT 'previously_seen'
            CHECK (observation_status IN ('new_article', 'new_report_representation', 'previously_seen'));
        ALTER TABLE report_appearances ADD COLUMN external_content_change TEXT NOT NULL DEFAULT 'unknown'
            CHECK (external_content_change = 'unknown');

        UPDATE report_appearances
        SET observation_status = CASE disposition
            WHEN 'new' THEN 'new_article'
            WHEN 'updated' THEN 'new_report_representation'
            ELSE 'previously_seen'
        END;
        """,
    ),
    (
        3,
        "external_content_and_enrichment",
        """
        CREATE TABLE article_content_versions (
            content_version_id TEXT PRIMARY KEY,
            article_id TEXT NOT NULL REFERENCES articles(article_id),
            content_sha256 TEXT NOT NULL CHECK (
                length(content_sha256) = 64
                AND content_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            markdown_content TEXT NOT NULL,
            markdown_sha256 TEXT NOT NULL CHECK (
                length(markdown_sha256) = 64
                AND markdown_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            content_type TEXT NOT NULL,
            source_bytes INTEGER CHECK (source_bytes IS NULL OR source_bytes >= 0),
            extraction_method TEXT NOT NULL,
            extraction_version TEXT NOT NULL,
            first_fetched_at TEXT NOT NULL,
            UNIQUE (article_id, content_sha256),
            UNIQUE (article_id, content_version_id)
        );

        CREATE TABLE article_fetches (
            fetch_id TEXT PRIMARY KEY,
            article_id TEXT NOT NULL REFERENCES articles(article_id),
            requested_url TEXT NOT NULL,
            final_url TEXT,
            fetched_at TEXT NOT NULL,
            fetch_status TEXT NOT NULL CHECK (fetch_status IN ('success', 'not_modified', 'failed')),
            http_status INTEGER CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599),
            content_type TEXT,
            etag TEXT,
            last_modified TEXT,
            error_code TEXT,
            error_message TEXT,
            content_version_id TEXT REFERENCES article_content_versions(content_version_id),
            FOREIGN KEY (article_id, content_version_id)
                REFERENCES article_content_versions(article_id, content_version_id),
            CHECK (
                (fetch_status = 'success'
                    AND http_status IS NOT NULL
                    AND http_status BETWEEN 200 AND 299
                    AND content_version_id IS NOT NULL
                    AND final_url IS NOT NULL
                    AND length(trim(final_url)) > 0
                    AND error_code IS NULL
                    AND error_message IS NULL)
                OR
                (fetch_status = 'not_modified'
                    AND http_status IS NOT NULL
                    AND http_status = 304
                    AND content_version_id IS NOT NULL
                    AND final_url IS NOT NULL
                    AND length(trim(final_url)) > 0
                    AND error_code IS NULL
                    AND error_message IS NULL)
                OR
                (fetch_status = 'failed'
                    AND content_version_id IS NULL
                    AND error_code IS NOT NULL
                    AND length(trim(error_code)) > 0)
            )
        );

        CREATE TABLE article_enrichments (
            enrichment_id TEXT PRIMARY KEY,
            content_version_id TEXT NOT NULL REFERENCES article_content_versions(content_version_id),
            status TEXT NOT NULL CHECK (status IN ('complete', 'failed')),
            summary TEXT,
            categories_json TEXT,
            keywords_json TEXT,
            language TEXT,
            generator_kind TEXT NOT NULL CHECK (generator_kind IN ('deterministic', 'model')),
            generator_name TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            error_code TEXT,
            error_message TEXT,
            CHECK (
                (status = 'complete'
                    AND summary IS NOT NULL
                    AND categories_json IS NOT NULL
                    AND keywords_json IS NOT NULL
                    AND language IS NOT NULL
                    AND length(trim(summary)) > 0
                    AND length(trim(categories_json)) > 0
                    AND length(trim(keywords_json)) > 0
                    AND length(trim(language)) > 0
                    AND error_code IS NULL
                    AND error_message IS NULL)
                OR
                (status = 'failed'
                    AND summary IS NULL
                    AND categories_json IS NULL
                    AND keywords_json IS NULL
                    AND language IS NULL
                    AND error_code IS NOT NULL
                    AND length(trim(error_code)) > 0)
            )
        );

        ALTER TABLE articles ADD COLUMN current_content_version_id TEXT
            REFERENCES article_content_versions(content_version_id) DEFERRABLE INITIALLY DEFERRED;
        ALTER TABLE articles ADD COLUMN display_policy TEXT NOT NULL DEFAULT 'summary_excerpt'
            CHECK (display_policy IN ('metadata_only', 'summary_excerpt', 'full_markdown'));

        CREATE TRIGGER articles_current_content_matches_article_insert
        BEFORE INSERT ON articles
        WHEN NEW.current_content_version_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM article_content_versions
                 WHERE content_version_id = NEW.current_content_version_id
                   AND article_id = NEW.article_id
             )
        BEGIN
            SELECT RAISE(ABORT, 'current content version belongs to another article');
        END;

        CREATE TRIGGER articles_current_content_matches_article_update
        BEFORE UPDATE OF current_content_version_id ON articles
        WHEN NEW.current_content_version_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM article_content_versions
                 WHERE content_version_id = NEW.current_content_version_id
                   AND article_id = NEW.article_id
             )
        BEGIN
            SELECT RAISE(ABORT, 'current content version belongs to another article');
        END;

        CREATE TRIGGER article_content_versions_are_immutable_update
        BEFORE UPDATE ON article_content_versions
        BEGIN
            SELECT RAISE(ABORT, 'article content versions are immutable');
        END;

        CREATE TRIGGER article_content_versions_are_immutable_delete
        BEFORE DELETE ON article_content_versions
        BEGIN
            SELECT RAISE(ABORT, 'article content versions are immutable');
        END;

        CREATE TRIGGER article_fetches_are_append_only_update
        BEFORE UPDATE ON article_fetches
        BEGIN
            SELECT RAISE(ABORT, 'article fetches are append-only');
        END;

        CREATE TRIGGER article_fetches_are_append_only_delete
        BEFORE DELETE ON article_fetches
        BEGIN
            SELECT RAISE(ABORT, 'article fetches are append-only');
        END;

        CREATE TRIGGER article_enrichments_are_append_only_update
        BEFORE UPDATE ON article_enrichments
        BEGIN
            SELECT RAISE(ABORT, 'article enrichments are append-only');
        END;

        CREATE TRIGGER article_enrichments_are_append_only_delete
        BEFORE DELETE ON article_enrichments
        BEGIN
            SELECT RAISE(ABORT, 'article enrichments are append-only');
        END;

        CREATE INDEX idx_article_fetches_article_fetched
            ON article_fetches(article_id, fetched_at DESC);
        CREATE INDEX idx_article_fetches_content_version
            ON article_fetches(content_version_id);
        CREATE INDEX idx_content_versions_article_fetched
            ON article_content_versions(article_id, first_fetched_at DESC);
        CREATE INDEX idx_enrichments_content_generated
            ON article_enrichments(content_version_id, generated_at DESC);
        """,
    ),
)


def apply_migrations(connection: sqlite3.Connection, *, target_version: int | None = None) -> list[int]:
    """Apply all pending schema migrations and return their version numbers."""

    if connection.in_transaction:
        raise sqlite3.ProgrammingError("cannot apply migrations inside an active transaction")
    latest_version = MIGRATIONS[-1][0]
    target_version = latest_version if target_version is None else target_version
    if target_version < 1 or target_version > latest_version:
        raise ValueError(f"unsupported migration target: {target_version}")
    current_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if current_version > target_version:
        raise ValueError(
            f"refusing to migrate backward from version {current_version} to {target_version}"
        )
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
        if version > target_version:
            break
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
