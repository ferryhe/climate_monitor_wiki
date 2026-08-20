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
    (
        4,
        "validated_capture_fallback_resolutions",
        """
        CREATE TABLE article_capture_resolutions (
            resolution_id TEXT PRIMARY KEY CHECK (
                length(resolution_id) = 75
                AND resolution_id GLOB 'resolution-*'
                AND substr(resolution_id, 12) NOT GLOB '*[^0-9a-f]*'
            ),
            report_id TEXT NOT NULL REFERENCES reports(report_id),
            report_date TEXT NOT NULL,
            report_sha256 TEXT NOT NULL CHECK (
                length(report_sha256) = 64
                AND report_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            article_id TEXT NOT NULL REFERENCES articles(article_id),
            canonical_url TEXT NOT NULL,
            fetch_id TEXT NOT NULL UNIQUE REFERENCES article_fetches(fetch_id),
            failure_class TEXT NOT NULL CHECK (
                failure_class = 'http_403_publisher_bot_wall'
            ),
            http_status INTEGER NOT NULL CHECK (http_status = 403),
            attempt_at TEXT NOT NULL CHECK (length(trim(attempt_at)) > 0),
            fallback_source TEXT NOT NULL CHECK (
                fallback_source IN ('json_annotation', 'source_report')
            ),
            fallback_provenance TEXT NOT NULL CHECK (
                (fallback_source = 'source_report' AND fallback_provenance = 'source_report')
                OR
                (fallback_source = 'json_annotation' AND fallback_provenance IN (
                    'original_content_annotation',
                    'official_replacement_annotation',
                    'publisher_excerpt_annotation',
                    'report_fallback_annotation'
                ))
            ),
            bundle_json TEXT NOT NULL CHECK (length(trim(bundle_json)) > 0),
            bundle_sha256 TEXT NOT NULL CHECK (
                length(bundle_sha256) = 64
                AND bundle_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            validated_at TEXT NOT NULL CHECK (length(trim(validated_at)) > 0)
        );

        CREATE TRIGGER article_capture_resolutions_reject_replace
        BEFORE INSERT ON article_capture_resolutions
        WHEN EXISTS (
                SELECT 1 FROM article_capture_resolutions existing
                WHERE existing.resolution_id = NEW.resolution_id
                   OR existing.fetch_id = NEW.fetch_id
             )
        BEGIN
            SELECT RAISE(ABORT, 'capture fallback resolution identity already exists');
        END;

        CREATE TRIGGER article_capture_resolutions_validate_insert
        BEFORE INSERT ON article_capture_resolutions
        WHEN NOT EXISTS (
                SELECT 1 FROM reports r
                WHERE r.report_id = NEW.report_id
                  AND r.report_date = NEW.report_date
                  AND r.report_sha256 = NEW.report_sha256
             )
          OR NOT EXISTS (
                SELECT 1 FROM articles a
                WHERE a.article_id = NEW.article_id
                  AND a.canonical_url = NEW.canonical_url
                  AND a.publication_eligible = 1
             )
          OR NOT EXISTS (
                SELECT 1 FROM report_appearances ra
                WHERE ra.report_id = NEW.report_id
                  AND ra.article_id = NEW.article_id
             )
          OR NOT EXISTS (
                SELECT 1 FROM article_fetches f
                WHERE f.fetch_id = NEW.fetch_id
                  AND f.article_id = NEW.article_id
                  AND f.requested_url = NEW.canonical_url
                  AND f.fetch_status = 'failed'
                  AND f.error_code = 'http_error'
                  AND f.http_status = 403
                  AND f.content_version_id IS NULL
                  AND f.fetched_at = NEW.attempt_at
             )
          OR EXISTS (
                SELECT 1 FROM article_fetches later
                WHERE later.article_id = NEW.article_id
                  AND (
                    later.fetched_at > NEW.attempt_at
                    OR (later.fetched_at = NEW.attempt_at AND later.fetch_id > NEW.fetch_id)
                  )
             )
        BEGIN
            SELECT RAISE(ABORT, 'invalid capture fallback resolution identity');
        END;

        CREATE TRIGGER article_capture_resolutions_are_append_only_update
        BEFORE UPDATE ON article_capture_resolutions
        BEGIN
            SELECT RAISE(ABORT, 'capture fallback resolutions are append-only');
        END;

        CREATE TRIGGER article_capture_resolutions_are_append_only_delete
        BEFORE DELETE ON article_capture_resolutions
        BEGIN
            SELECT RAISE(ABORT, 'capture fallback resolutions are append-only');
        END;

        CREATE INDEX idx_capture_resolutions_report_article
            ON article_capture_resolutions(report_id, article_id, validated_at DESC);
        CREATE INDEX idx_capture_resolutions_fetch
            ON article_capture_resolutions(fetch_id);
        """,
    ),
    (
        5,
        "article_semantics_import",
        """
        CREATE TABLE article_semantics (
            report_sha256 TEXT NOT NULL
                CHECK (length(report_sha256) = 64 AND report_sha256 NOT GLOB '*[^0-9a-f]*'),
            article_id TEXT NOT NULL,
            canonical_url TEXT,
            title TEXT,
            summary TEXT,
            categories_json TEXT,
            keywords_json TEXT,
            taxonomy_id TEXT,
            taxonomy_raw_sha256 TEXT
                CHECK (taxonomy_raw_sha256 IS NULL OR (
                    length(taxonomy_raw_sha256) = 64 AND taxonomy_raw_sha256 NOT GLOB '*[^0-9a-f]*'
                )),
            bundle_sha256 TEXT
                CHECK (bundle_sha256 IS NULL OR (
                    length(bundle_sha256) = 64 AND bundle_sha256 NOT GLOB '*[^0-9a-f]*'
                )),
            validated_at TEXT NOT NULL,
            PRIMARY KEY (report_sha256, article_id)
        );
        """,
    ),
    (
        6,
        "article_semantics_relational_constraints",
        """
        CREATE UNIQUE INDEX idx_reports_id_sha256
            ON reports(report_id, report_sha256);

        ALTER TABLE article_semantics RENAME TO article_semantics_v5;

        CREATE TABLE article_semantics (
            report_id TEXT NOT NULL,
            report_sha256 TEXT NOT NULL
                CHECK (length(report_sha256) = 64 AND report_sha256 NOT GLOB '*[^0-9a-f]*'),
            article_id TEXT NOT NULL,
            canonical_url TEXT,
            title TEXT,
            summary TEXT,
            categories_json TEXT,
            keywords_json TEXT,
            taxonomy_id TEXT,
            taxonomy_raw_sha256 TEXT
                CHECK (taxonomy_raw_sha256 IS NULL OR (
                    length(taxonomy_raw_sha256) = 64 AND taxonomy_raw_sha256 NOT GLOB '*[^0-9a-f]*'
                )),
            bundle_sha256 TEXT
                CHECK (bundle_sha256 IS NULL OR (
                    length(bundle_sha256) = 64 AND bundle_sha256 NOT GLOB '*[^0-9a-f]*'
                )),
            validated_at TEXT NOT NULL,
            PRIMARY KEY (report_id, article_id),
            FOREIGN KEY (report_id, report_sha256)
                REFERENCES reports(report_id, report_sha256),
            FOREIGN KEY (report_id, article_id)
                REFERENCES report_appearances(report_id, article_id)
        );

        INSERT INTO article_semantics (
            report_id, report_sha256, article_id, canonical_url, title, summary,
            categories_json, keywords_json, taxonomy_id, taxonomy_raw_sha256,
            bundle_sha256, validated_at
        )
        SELECT r.report_id,
            old.report_sha256, old.article_id, old.canonical_url, old.title,
            old.summary, old.categories_json, old.keywords_json, old.taxonomy_id,
            old.taxonomy_raw_sha256, old.bundle_sha256, old.validated_at
        FROM article_semantics_v5 old
        JOIN reports r ON r.report_sha256 = old.report_sha256;

        DROP TABLE article_semantics_v5;
        """,
    ),
)


def _preflight_migration(connection: sqlite3.Connection, version: int) -> None:
    if version != 6:
        return
    row = connection.execute(
        """
        SELECT old.report_sha256, COUNT(r.report_id) AS report_count
        FROM (SELECT DISTINCT report_sha256 FROM article_semantics) old
        LEFT JOIN reports r ON r.report_sha256 = old.report_sha256
        GROUP BY old.report_sha256
        HAVING report_count <> 1
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        raise sqlite3.IntegrityError(
            "cannot migrate article_semantics: ambiguous or missing report_sha256 mapping"
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
            _preflight_migration(connection, version)
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
