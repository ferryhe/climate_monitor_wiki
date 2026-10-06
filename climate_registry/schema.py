from __future__ import annotations

import sqlite3

_INFORMATION_CHECK_SQL = """
CREATE TABLE {kind}_check_runs (
    run_id TEXT PRIMARY KEY,
    input_json TEXT NOT NULL CHECK (json_valid(input_json)),
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256)=64),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending','running','complete','partial','failed')),
    item_count INTEGER NOT NULL CHECK (item_count >= 0),
    completed_count INTEGER NOT NULL DEFAULT 0 CHECK (completed_count >= 0),
    error_message TEXT
);
CREATE TABLE {kind}_check_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES {kind}_check_runs(run_id),
    occurrence_id TEXT NOT NULL REFERENCES {source_table}(occurrence_id),
    source_url TEXT NOT NULL,
    source_revision_sha256 TEXT NOT NULL CHECK (length(source_revision_sha256)=64),
    checked_at TEXT NOT NULL,
    access_status TEXT NOT NULL CHECK (access_status IN ('accessible','unavailable','failed')),
    verification_status TEXT NOT NULL CHECK (verification_status IN ('unchecked','partial','verified','conflict')),
    packet_json TEXT NOT NULL CHECK (json_valid(packet_json)),
    packet_sha256 TEXT NOT NULL CHECK (length(packet_sha256)=64),
    UNIQUE (run_id, occurrence_id, source_url)
);
CREATE INDEX idx_{kind}_checks_occurrence ON {kind}_check_attempts(occurrence_id, checked_at DESC);
CREATE TRIGGER {kind}_check_attempts_immutable_update BEFORE UPDATE ON {kind}_check_attempts BEGIN
    SELECT RAISE(ABORT, 'information check attempts are immutable');
END;
CREATE TRIGGER {kind}_check_attempts_immutable_delete BEFORE DELETE ON {kind}_check_attempts BEGIN
    SELECT RAISE(ABORT, 'information check attempts are immutable');
END;
CREATE TRIGGER {kind}_check_inputs_immutable BEFORE UPDATE OF run_id, input_json, input_sha256, created_at, item_count
ON {kind}_check_runs BEGIN
    SELECT RAISE(ABORT, 'information check inputs are immutable');
END;
"""

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
    (
        7,
        "pre_report_acquisition_batches",
        """
        CREATE TABLE acquisition_batches (
            batch_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL CHECK (schema_version = 'pre-report-acquisition-batch.v1'),
            report_date TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            date_policy_json TEXT NOT NULL,
            search_decision TEXT NOT NULL CHECK (search_decision IN ('attempted', 'no_search')),
            no_search_reason TEXT,
            payload_sha256 TEXT NOT NULL CHECK (
                length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            frozen_at TEXT,
            CHECK ((search_decision = 'no_search' AND length(trim(no_search_reason)) > 0)
                OR (search_decision = 'attempted' AND no_search_reason IS NULL))
        );

        CREATE TABLE acquisition_searches (
            search_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES acquisition_batches(batch_id),
            ordinal INTEGER NOT NULL CHECK (ordinal > 0),
            search_ref TEXT NOT NULL CHECK (length(trim(search_ref)) > 0),
            query TEXT NOT NULL CHECK (length(trim(query)) > 0),
            engine TEXT NOT NULL CHECK (length(trim(engine)) > 0),
            status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
            attempted_at TEXT NOT NULL,
            result_refs_json TEXT NOT NULL,
            budget_json TEXT NOT NULL,
            error_message TEXT,
            CHECK ((status = 'success' AND error_message IS NULL)
                OR (status = 'failed' AND length(trim(error_message)) > 0)),
            UNIQUE (batch_id, ordinal),
            UNIQUE (batch_id, search_ref)
        );

        CREATE TABLE acquisition_items (
            acquisition_item_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL REFERENCES acquisition_batches(batch_id),
            ordinal INTEGER NOT NULL CHECK (ordinal > 0),
            article_id TEXT NOT NULL REFERENCES articles(article_id),
            raw_url TEXT NOT NULL,
            source_name TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            discovered_at TEXT NOT NULL,
            discovery_kind TEXT NOT NULL CHECK (discovery_kind IN ('site', 'search')),
            discovery_ref TEXT NOT NULL,
            origins_json TEXT NOT NULL,
            search_id TEXT REFERENCES acquisition_searches(search_id),
            publication_date TEXT,
            publication_date_evidence_json TEXT,
            date_status TEXT NOT NULL CHECK (date_status IN ('eligible', 'outside_window', 'unknown_pending_review')),
            selection_status TEXT NOT NULL CHECK (selection_status IN ('selected', 'unselected')),
            selection_reason TEXT NOT NULL,
            update_status TEXT NOT NULL CHECK (update_status IN ('baseline', 'content_changed', 'unchanged', 'failed')),
            material_status TEXT NOT NULL CHECK (material_status IN ('full_content', 'snippet', 'error')),
            fetch_id TEXT NOT NULL UNIQUE REFERENCES article_fetches(fetch_id),
            content_version_id TEXT REFERENCES article_content_versions(content_version_id),
            content_ref TEXT,
            raw_snapshot_ref TEXT,
            raw_snapshot_sha256 TEXT,
            attempts_json TEXT NOT NULL,
            processing_status TEXT NOT NULL CHECK (processing_status IN ('pending', 'complete', 'failed')),
            processing_error TEXT,
            FOREIGN KEY (article_id, content_version_id)
                REFERENCES article_content_versions(article_id, content_version_id),
            UNIQUE (batch_id, ordinal)
        );

        CREATE TRIGGER acquisition_batches_are_append_only_update
        BEFORE UPDATE ON acquisition_batches BEGIN
            SELECT RAISE(ABORT, 'acquisition batches are append-only');
        END;
        CREATE TRIGGER acquisition_batches_are_append_only_delete
        BEFORE DELETE ON acquisition_batches BEGIN
            SELECT RAISE(ABORT, 'acquisition batches are append-only');
        END;
        CREATE TRIGGER acquisition_searches_are_append_only_update
        BEFORE UPDATE ON acquisition_searches BEGIN
            SELECT RAISE(ABORT, 'acquisition searches are append-only');
        END;
        CREATE TRIGGER acquisition_searches_are_append_only_delete
        BEFORE DELETE ON acquisition_searches BEGIN
            SELECT RAISE(ABORT, 'acquisition searches are append-only');
        END;
        CREATE TRIGGER acquisition_items_are_append_only_update
        BEFORE UPDATE ON acquisition_items BEGIN
            SELECT RAISE(ABORT, 'acquisition items are append-only');
        END;
        CREATE TRIGGER acquisition_items_are_append_only_delete
        BEFORE DELETE ON acquisition_items BEGIN
            SELECT RAISE(ABORT, 'acquisition items are append-only');
        END;

        CREATE INDEX idx_acquisition_searches_batch_status
            ON acquisition_searches(batch_id, status, ordinal);
        CREATE INDEX idx_acquisition_items_batch_selection
            ON acquisition_items(batch_id, selection_status, ordinal);
        CREATE INDEX idx_acquisition_items_article_discovered
            ON acquisition_items(article_id, discovered_at DESC);
        CREATE INDEX idx_acquisition_items_content_version
            ON acquisition_items(content_version_id);
        """,
    ),
    (
        8,
        "resolved_acquisition_fetch_observations",
        """
        ALTER TABLE acquisition_items ADD COLUMN resolved_by_fetch_id TEXT
            REFERENCES article_fetches(fetch_id);

        CREATE TRIGGER acquisition_item_resolution_is_valid_insert
        BEFORE INSERT ON acquisition_items
        WHEN NEW.resolved_by_fetch_id IS NOT NULL
             AND (
                 NOT EXISTS (
                     SELECT 1 FROM article_fetches own
                     WHERE own.fetch_id = NEW.fetch_id
                       AND own.article_id = NEW.article_id
                       AND own.fetch_status = 'failed'
                 )
                 OR NOT EXISTS (
                     SELECT 1 FROM article_fetches resolution
                     WHERE resolution.fetch_id = NEW.resolved_by_fetch_id
                       AND resolution.article_id = NEW.article_id
                       AND resolution.fetch_status = 'success'
                 )
             )
        BEGIN
            SELECT RAISE(ABORT, 'acquisition resolution must link failed and successful fetches for one article');
        END;

        CREATE INDEX idx_acquisition_items_resolution
            ON acquisition_items(resolved_by_fetch_id);

        """,
    ),
    (
        9,
        "reconcilable_unfrozen_acquisition_batches",
        """
        DROP TRIGGER acquisition_batches_are_append_only_update;
        DROP TRIGGER acquisition_searches_are_append_only_update;
        DROP TRIGGER acquisition_items_are_append_only_update;

        CREATE TRIGGER acquisition_batches_reconcile_before_freeze
        BEFORE UPDATE ON acquisition_batches
        WHEN OLD.frozen_at IS NOT NULL
          OR NEW.batch_id IS NOT OLD.batch_id
          OR NEW.schema_version IS NOT OLD.schema_version
          OR NEW.report_date IS NOT OLD.report_date
          OR NEW.started_at IS NOT OLD.started_at
          OR NEW.date_policy_json IS NOT OLD.date_policy_json
          OR NEW.search_decision IS NOT OLD.search_decision
          OR NEW.no_search_reason IS NOT OLD.no_search_reason
          OR (NEW.frozen_at IS NOT NULL AND (
                NEW.completed_at IS NOT OLD.completed_at
                OR NEW.payload_sha256 IS NOT OLD.payload_sha256
             ))
        BEGIN
            SELECT RAISE(ABORT, 'frozen or immutable acquisition batch fields cannot change');
        END;

        CREATE TRIGGER acquisition_searches_reconcile_failures_only
        BEFORE UPDATE ON acquisition_searches
        WHEN OLD.status != 'failed'
          OR NEW.status != 'success'
          OR NEW.search_id IS NOT OLD.search_id
          OR NEW.batch_id IS NOT OLD.batch_id
          OR NEW.ordinal IS NOT OLD.ordinal
          OR NEW.search_ref IS NOT OLD.search_ref
          OR NEW.query IS NOT OLD.query
          OR NEW.engine IS NOT OLD.engine
          OR EXISTS (
              SELECT 1 FROM acquisition_batches batch
              WHERE batch.batch_id = OLD.batch_id AND batch.frozen_at IS NOT NULL
          )
        BEGIN
            SELECT RAISE(ABORT, 'only unresolved searches in an unfrozen batch may be reconciled');
        END;

        CREATE TRIGGER acquisition_items_reconcile_resolution_only
        BEFORE UPDATE ON acquisition_items
        WHEN OLD.resolved_by_fetch_id IS NOT NULL
          OR NEW.resolved_by_fetch_id IS NULL
          OR NEW.acquisition_item_id IS NOT OLD.acquisition_item_id
          OR NEW.batch_id IS NOT OLD.batch_id
          OR NEW.ordinal IS NOT OLD.ordinal
          OR NEW.article_id IS NOT OLD.article_id
          OR NEW.raw_url IS NOT OLD.raw_url
          OR NEW.source_name IS NOT OLD.source_name
          OR NEW.title IS NOT OLD.title
          OR NEW.summary IS NOT OLD.summary
          OR NEW.discovered_at IS NOT OLD.discovered_at
          OR NEW.discovery_kind IS NOT OLD.discovery_kind
          OR NEW.discovery_ref IS NOT OLD.discovery_ref
          OR NEW.origins_json IS NOT OLD.origins_json
          OR NEW.search_id IS NOT OLD.search_id
          OR NEW.publication_date IS NOT OLD.publication_date
          OR NEW.publication_date_evidence_json IS NOT OLD.publication_date_evidence_json
          OR NEW.date_status IS NOT OLD.date_status
          OR NEW.selection_status IS NOT OLD.selection_status
          OR NEW.selection_reason IS NOT OLD.selection_reason
          OR NEW.update_status IS NOT OLD.update_status
          OR NEW.material_status IS NOT OLD.material_status
          OR NEW.fetch_id IS NOT OLD.fetch_id
          OR NEW.content_version_id IS NOT OLD.content_version_id
          OR NEW.content_ref IS NOT OLD.content_ref
          OR NEW.raw_snapshot_ref IS NOT OLD.raw_snapshot_ref
          OR NEW.raw_snapshot_sha256 IS NOT OLD.raw_snapshot_sha256
          OR NEW.attempts_json IS NOT OLD.attempts_json
          OR NEW.processing_status IS NOT OLD.processing_status
          OR NEW.processing_error IS NOT OLD.processing_error
          OR NOT EXISTS (
              SELECT 1 FROM article_fetches own
              WHERE own.fetch_id = NEW.fetch_id
                AND own.article_id = NEW.article_id
                AND own.fetch_status = 'failed'
          )
          OR NOT EXISTS (
              SELECT 1 FROM article_fetches resolution
              WHERE resolution.fetch_id = NEW.resolved_by_fetch_id
                AND resolution.article_id = NEW.article_id
                AND resolution.fetch_status = 'success'
          )
          OR EXISTS (
              SELECT 1 FROM acquisition_batches batch
              WHERE batch.batch_id = OLD.batch_id AND batch.frozen_at IS NOT NULL
          )
        BEGIN
            SELECT RAISE(ABORT, 'only unresolved item resolution in an unfrozen batch may change');
        END;
        """,
    ),
    (
        10,
        "monotonic_acquisition_search_decision",
        """
        DROP TRIGGER acquisition_batches_reconcile_before_freeze;

        CREATE TRIGGER acquisition_batches_reconcile_before_freeze
        BEFORE UPDATE ON acquisition_batches
        WHEN OLD.frozen_at IS NOT NULL
          OR NEW.batch_id IS NOT OLD.batch_id
          OR NEW.schema_version IS NOT OLD.schema_version
          OR NEW.report_date IS NOT OLD.report_date
          OR NEW.started_at IS NOT OLD.started_at
          OR NEW.date_policy_json IS NOT OLD.date_policy_json
          OR (
               (
                 NEW.search_decision IS NOT OLD.search_decision
                 OR NEW.no_search_reason IS NOT OLD.no_search_reason
               )
               AND NOT (
                 OLD.search_decision = 'no_search'
                 AND NEW.search_decision = 'attempted'
                 AND NEW.no_search_reason IS NULL
                 AND NEW.frozen_at IS NULL
                 AND EXISTS (
                   SELECT 1 FROM acquisition_searches search
                   WHERE search.batch_id = OLD.batch_id
                 )
               )
             )
          OR (NEW.frozen_at IS NOT NULL AND (
                NEW.completed_at IS NOT OLD.completed_at
                OR NEW.payload_sha256 IS NOT OLD.payload_sha256
             ))
        BEGIN
            SELECT RAISE(ABORT, 'frozen or immutable acquisition batch fields cannot change');
        END;
        """,
    ),
    (
        11,
        "meeting_extraction_and_snapshots",
        """
        CREATE TABLE meeting_runs (
            meeting_run_id TEXT PRIMARY KEY,
            processing_key TEXT NOT NULL,
            batch_id TEXT NOT NULL REFERENCES acquisition_batches(batch_id),
            attempt INTEGER NOT NULL CHECK (attempt > 0),
            status TEXT NOT NULL CHECK (status IN (
                'running', 'succeeded', 'partial', 'failed', 'no_content'
            )),
            prompt_version TEXT NOT NULL,
            prompt_sha256 TEXT NOT NULL CHECK (
                length(prompt_sha256) = 64 AND prompt_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            prompt_text TEXT NOT NULL CHECK (length(trim(prompt_text)) > 0),
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            task_version INTEGER NOT NULL CHECK (task_version > 0),
            retry_of_meeting_run_id TEXT REFERENCES meeting_runs(meeting_run_id),
            input_sha256 TEXT NOT NULL CHECK (
                length(input_sha256) = 64 AND input_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            item_count INTEGER NOT NULL CHECK (item_count >= 0),
            succeeded_count INTEGER NOT NULL DEFAULT 0 CHECK (succeeded_count >= 0),
            failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
            unavailable_count INTEGER NOT NULL DEFAULT 0 CHECK (unavailable_count >= 0),
            candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
            error_message TEXT,
            UNIQUE (processing_key, attempt)
        );

        CREATE TABLE meeting_run_items (
            meeting_run_id TEXT NOT NULL REFERENCES meeting_runs(meeting_run_id),
            acquisition_item_id TEXT NOT NULL REFERENCES acquisition_items(acquisition_item_id),
            content_version_id TEXT REFERENCES article_content_versions(content_version_id),
            article_id TEXT NOT NULL,
            source_url TEXT NOT NULL,
            content_sha256 TEXT CHECK (content_sha256 IS NULL OR (
                length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'
            )),
            status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed', 'unavailable')),
            candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
            error_message TEXT,
            processed_at TEXT,
            PRIMARY KEY (meeting_run_id, acquisition_item_id),
            FOREIGN KEY (article_id, content_version_id)
                REFERENCES article_content_versions(article_id, content_version_id)
        );

        CREATE TABLE climate_events (
            event_id TEXT PRIMARY KEY,
            record_version INTEGER NOT NULL CHECK (record_version > 0),
            name TEXT NOT NULL CHECK (length(trim(name)) > 0),
            event_type TEXT NOT NULL CHECK (event_type IN (
                'meeting', 'conference', 'summit', 'webinar', 'deadline', 'retrospective'
            )),
            organizer TEXT,
            status TEXT NOT NULL CHECK (status IN (
                'scheduled', 'tentative', 'postponed', 'cancelled', 'conflict', 'retrospective'
            )),
            date_precision TEXT NOT NULL CHECK (date_precision IN (
                'day', 'month', 'quarter', 'year', 'unknown'
            )),
            start_date TEXT,
            end_date TEXT,
            raw_time_text TEXT,
            event_timezone TEXT,
            location TEXT,
            online_url TEXT,
            deadline_type TEXT CHECK (deadline_type IS NULL OR deadline_type IN (
                'registration', 'consultation', 'expert_review'
            )),
            deadline_date TEXT,
            relevance_reason TEXT,
            needs_confirmation INTEGER NOT NULL CHECK (needs_confirmation IN (0, 1)),
            source_count INTEGER NOT NULL CHECK (source_count > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE climate_event_versions (
            event_id TEXT NOT NULL REFERENCES climate_events(event_id),
            record_version INTEGER NOT NULL CHECK (record_version > 0),
            state_json TEXT NOT NULL,
            state_sha256 TEXT NOT NULL CHECK (
                length(state_sha256) = 64 AND state_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            meeting_run_id TEXT NOT NULL REFERENCES meeting_runs(meeting_run_id),
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (event_id, record_version)
        );

        CREATE TABLE climate_event_sources (
            event_source_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES climate_events(event_id),
            content_version_id TEXT NOT NULL REFERENCES article_content_versions(content_version_id),
            article_id TEXT NOT NULL,
            meeting_run_id TEXT NOT NULL REFERENCES meeting_runs(meeting_run_id),
            source_url TEXT NOT NULL,
            content_sha256 TEXT NOT NULL CHECK (
                length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            candidate_json TEXT NOT NULL,
            candidate_sha256 TEXT NOT NULL CHECK (
                length(candidate_sha256) = 64 AND candidate_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            date_evidence TEXT,
            deadline_evidence TEXT,
            status_evidence TEXT,
            observed_at TEXT NOT NULL,
            FOREIGN KEY (article_id, content_version_id)
                REFERENCES article_content_versions(article_id, content_version_id),
            UNIQUE (event_id, content_version_id)
        );

        CREATE TABLE meeting_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            query_json TEXT NOT NULL,
            base_date TEXT NOT NULL,
            timezone TEXT NOT NULL,
            records_json TEXT NOT NULL,
            coverage_json TEXT NOT NULL,
            snapshot_sha256 TEXT NOT NULL UNIQUE CHECK (
                length(snapshot_sha256) = 64 AND snapshot_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        );

        CREATE INDEX idx_meeting_runs_batch_status
            ON meeting_runs(batch_id, status, started_at DESC);
        CREATE INDEX idx_meeting_run_items_status
            ON meeting_run_items(meeting_run_id, status);
        CREATE INDEX idx_climate_events_dates
            ON climate_events(start_date, end_date, event_type, status);
        CREATE INDEX idx_climate_events_organizer
            ON climate_events(organizer);
        CREATE INDEX idx_climate_event_sources_event
            ON climate_event_sources(event_id, observed_at DESC);
        CREATE INDEX idx_climate_event_sources_content
            ON climate_event_sources(content_version_id);

        CREATE TRIGGER climate_event_versions_are_append_only_update
        BEFORE UPDATE ON climate_event_versions BEGIN
            SELECT RAISE(ABORT, 'climate event versions are append-only');
        END;
        CREATE TRIGGER climate_event_versions_are_append_only_delete
        BEFORE DELETE ON climate_event_versions BEGIN
            SELECT RAISE(ABORT, 'climate event versions are append-only');
        END;
        CREATE TRIGGER meeting_snapshots_are_immutable_update
        BEFORE UPDATE ON meeting_snapshots BEGIN
            SELECT RAISE(ABORT, 'meeting snapshots are immutable');
        END;
        CREATE TRIGGER meeting_snapshots_are_immutable_delete
        BEFORE DELETE ON meeting_snapshots BEGIN
            SELECT RAISE(ABORT, 'meeting snapshots are immutable');
        END;
        """,
    ),
    (
        12,
        "versioned_meeting_interpretations",
        """
        ALTER TABLE climate_event_sources RENAME TO climate_event_sources_v11;

        CREATE TABLE climate_event_sources (
            event_source_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES climate_events(event_id),
            content_version_id TEXT NOT NULL REFERENCES article_content_versions(content_version_id),
            article_id TEXT NOT NULL,
            meeting_run_id TEXT NOT NULL REFERENCES meeting_runs(meeting_run_id),
            candidate_ordinal INTEGER NOT NULL CHECK (candidate_ordinal > 0),
            interpretation_seq INTEGER NOT NULL CHECK (interpretation_seq > 0),
            source_url TEXT NOT NULL,
            content_sha256 TEXT NOT NULL CHECK (
                length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            candidate_json TEXT NOT NULL,
            candidate_sha256 TEXT NOT NULL CHECK (
                length(candidate_sha256) = 64 AND candidate_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            date_evidence TEXT,
            deadline_evidence TEXT,
            status_evidence TEXT,
            observed_at TEXT NOT NULL,
            FOREIGN KEY (article_id, content_version_id)
                REFERENCES article_content_versions(article_id, content_version_id),
            UNIQUE (event_id, content_version_id, meeting_run_id, candidate_ordinal)
        );

        INSERT INTO climate_event_sources (
            event_source_id, event_id, content_version_id, article_id, meeting_run_id,
            candidate_ordinal, interpretation_seq, source_url, content_sha256,
            candidate_json, candidate_sha256, date_evidence, deadline_evidence,
            status_evidence, observed_at
        )
        SELECT event_source_id, event_id, content_version_id, article_id, meeting_run_id,
               1, 1, source_url, content_sha256, candidate_json, candidate_sha256,
               date_evidence, deadline_evidence, status_evidence, observed_at
        FROM climate_event_sources_v11;

        DROP TABLE climate_event_sources_v11;

        CREATE INDEX idx_climate_event_sources_event
            ON climate_event_sources(event_id, observed_at DESC);
        CREATE INDEX idx_climate_event_sources_content
            ON climate_event_sources(content_version_id);
        """,
    ),
    (
        13,
        "pdf_intake_sources",
        """
        CREATE TABLE pdf_intake_documents (
            document_sha256 TEXT PRIMARY KEY CHECK (
                length(document_sha256) = 64 AND document_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            source_path TEXT NOT NULL,
            filename TEXT NOT NULL,
            media_type TEXT NOT NULL CHECK (media_type = 'application/pdf'),
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            date_of_run TEXT,
            period_start TEXT,
            period_end TEXT,
            extracted_text_sha256 TEXT NOT NULL CHECK (
                length(extracted_text_sha256) = 64 AND extracted_text_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            document_json TEXT NOT NULL,
            imported_at TEXT NOT NULL
        );

        CREATE TABLE pdf_intake_articles (
            article_id TEXT PRIMARY KEY,
            canonical_url TEXT NOT NULL UNIQUE,
            title TEXT,
            type_safe_classification_json TEXT,
            imported_at TEXT NOT NULL
        );

        CREATE TABLE pdf_intake_article_occurrences (
            occurrence_id TEXT PRIMARY KEY,
            article_id TEXT NOT NULL REFERENCES pdf_intake_articles(article_id),
            source_document_sha256 TEXT NOT NULL REFERENCES pdf_intake_documents(document_sha256),
            page INTEGER NOT NULL CHECK (page > 0),
            raw_url TEXT NOT NULL,
            report_date TEXT,
            publication_date TEXT,
            content_sha256 TEXT NOT NULL CHECK (
                length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            page_sha256 TEXT NOT NULL CHECK (
                length(page_sha256) = 64 AND page_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            occurrence_json TEXT NOT NULL
        );

        CREATE TABLE pdf_intake_calendar_items (
            occurrence_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
            source_document_sha256 TEXT NOT NULL REFERENCES pdf_intake_documents(document_sha256),
            page INTEGER NOT NULL CHECK (page > 0),
            name TEXT,
            kind TEXT NOT NULL,
            raw_date TEXT NOT NULL,
            date_precision TEXT NOT NULL CHECK (
                date_precision IN ('day', 'month', 'quarter', 'year', 'unknown')
            ),
            start_date TEXT,
            end_date TEXT,
            summary TEXT NOT NULL,
            content_sha256 TEXT NOT NULL CHECK (
                length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            type_safe_classification_json TEXT,
            item_json TEXT NOT NULL
        );

        CREATE INDEX idx_pdf_article_occurrences_article
            ON pdf_intake_article_occurrences(article_id, report_date);
        CREATE INDEX idx_pdf_article_occurrences_document
            ON pdf_intake_article_occurrences(source_document_sha256, page);
        CREATE INDEX idx_pdf_calendar_event
            ON pdf_intake_calendar_items(event_id, start_date);
        CREATE INDEX idx_pdf_calendar_document
            ON pdf_intake_calendar_items(source_document_sha256, page);

        CREATE TRIGGER pdf_intake_documents_are_append_only_update
        BEFORE UPDATE ON pdf_intake_documents BEGIN
            SELECT RAISE(ABORT, 'PDF intake documents are append-only');
        END;
        CREATE TRIGGER pdf_intake_documents_are_append_only_delete
        BEFORE DELETE ON pdf_intake_documents BEGIN
            SELECT RAISE(ABORT, 'PDF intake documents are append-only');
        END;
        CREATE TRIGGER pdf_intake_article_occurrences_are_append_only_update
        BEFORE UPDATE ON pdf_intake_article_occurrences BEGIN
            SELECT RAISE(ABORT, 'PDF intake article occurrences are append-only');
        END;
        CREATE TRIGGER pdf_intake_article_occurrences_are_append_only_delete
        BEFORE DELETE ON pdf_intake_article_occurrences BEGIN
            SELECT RAISE(ABORT, 'PDF intake article occurrences are append-only');
        END;
        CREATE TRIGGER pdf_intake_calendar_items_are_append_only_update
        BEFORE UPDATE ON pdf_intake_calendar_items BEGIN
            SELECT RAISE(ABORT, 'PDF intake calendar items are append-only');
        END;
        CREATE TRIGGER pdf_intake_calendar_items_are_append_only_delete
        BEFORE DELETE ON pdf_intake_calendar_items BEGIN
            SELECT RAISE(ABORT, 'PDF intake calendar items are append-only');
        END;
        """,
    ),
    (
        14,
        "pdf_source_provenance_and_enrichment",
        """
        ALTER TABLE pdf_intake_documents ADD COLUMN pdf_created_at TEXT;
        ALTER TABLE pdf_intake_documents ADD COLUMN pdf_modified_at TEXT;
        ALTER TABLE pdf_intake_documents ADD COLUMN original_pdf BLOB CHECK (
            original_pdf IS NULL OR length(original_pdf) = size_bytes
        );

        CREATE TABLE pdf_intake_document_sources (
            document_sha256 TEXT NOT NULL REFERENCES pdf_intake_documents(document_sha256),
            source_path TEXT NOT NULL,
            filename TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (document_sha256, source_path)
        );

        INSERT INTO pdf_intake_document_sources (
            document_sha256, source_path, filename, observed_at
        )
        SELECT document_sha256, source_path, filename, imported_at
        FROM pdf_intake_documents;

        CREATE TRIGGER pdf_intake_document_sources_are_append_only_update
        BEFORE UPDATE ON pdf_intake_document_sources BEGIN
            SELECT RAISE(ABORT, 'PDF intake document sources are append-only');
        END;
        CREATE TRIGGER pdf_intake_document_sources_are_append_only_delete
        BEFORE DELETE ON pdf_intake_document_sources BEGIN
            SELECT RAISE(ABORT, 'PDF intake document sources are append-only');
        END;

        DROP TRIGGER pdf_intake_documents_are_append_only_update;
        CREATE TRIGGER pdf_intake_documents_are_append_only_update
        BEFORE UPDATE ON pdf_intake_documents
        WHEN OLD.document_sha256 IS NOT NEW.document_sha256
          OR OLD.source_path IS NOT NEW.source_path
          OR OLD.filename IS NOT NEW.filename
          OR OLD.media_type IS NOT NEW.media_type
          OR OLD.size_bytes IS NOT NEW.size_bytes
          OR OLD.date_of_run IS NOT NEW.date_of_run
          OR OLD.period_start IS NOT NEW.period_start
          OR OLD.period_end IS NOT NEW.period_end
          OR OLD.extracted_text_sha256 IS NOT NEW.extracted_text_sha256
          OR OLD.document_json IS NOT NEW.document_json
          OR OLD.imported_at IS NOT NEW.imported_at
          OR (OLD.pdf_created_at IS NOT NULL AND NEW.pdf_created_at IS NOT OLD.pdf_created_at)
          OR (OLD.pdf_modified_at IS NOT NULL AND NEW.pdf_modified_at IS NOT OLD.pdf_modified_at)
          OR (OLD.original_pdf IS NOT NULL AND NEW.original_pdf IS NOT OLD.original_pdf)
          OR NEW.original_pdf IS NULL BEGIN
            SELECT RAISE(ABORT, 'PDF intake documents are append-only');
        END;

        DROP TRIGGER pdf_intake_calendar_items_are_append_only_update;
        CREATE TRIGGER pdf_intake_calendar_items_are_append_only_update
        BEFORE UPDATE ON pdf_intake_calendar_items
        WHEN OLD.type_safe_classification_json IS NOT NULL
          OR NEW.type_safe_classification_json IS NULL
          OR OLD.occurrence_id IS NOT NEW.occurrence_id
          OR OLD.event_id IS NOT NEW.event_id
          OR OLD.source_document_sha256 IS NOT NEW.source_document_sha256
          OR OLD.page IS NOT NEW.page
          OR OLD.name IS NOT NEW.name
          OR OLD.kind IS NOT NEW.kind
          OR OLD.raw_date IS NOT NEW.raw_date
          OR OLD.date_precision IS NOT NEW.date_precision
          OR OLD.start_date IS NOT NEW.start_date
          OR OLD.end_date IS NOT NEW.end_date
          OR OLD.summary IS NOT NEW.summary
          OR OLD.content_sha256 IS NOT NEW.content_sha256
          OR OLD.item_json IS NOT NEW.item_json BEGIN
            SELECT RAISE(ABORT, 'PDF intake calendar items are append-only');
        END;
        """,
    ),
    (
        15,
        "pdf_raw_metadata",
        """
        ALTER TABLE pdf_intake_documents ADD COLUMN pdf_metadata_json TEXT;

        DROP TRIGGER pdf_intake_documents_are_append_only_update;
        CREATE TRIGGER pdf_intake_documents_are_append_only_update
        BEFORE UPDATE ON pdf_intake_documents
        WHEN OLD.document_sha256 IS NOT NEW.document_sha256
          OR OLD.source_path IS NOT NEW.source_path
          OR OLD.filename IS NOT NEW.filename
          OR OLD.media_type IS NOT NEW.media_type
          OR OLD.size_bytes IS NOT NEW.size_bytes
          OR OLD.date_of_run IS NOT NEW.date_of_run
          OR OLD.period_start IS NOT NEW.period_start
          OR OLD.period_end IS NOT NEW.period_end
          OR OLD.extracted_text_sha256 IS NOT NEW.extracted_text_sha256
          OR OLD.document_json IS NOT NEW.document_json
          OR OLD.imported_at IS NOT NEW.imported_at
          OR (OLD.pdf_created_at IS NOT NULL AND NEW.pdf_created_at IS NOT OLD.pdf_created_at)
          OR (OLD.pdf_modified_at IS NOT NULL AND NEW.pdf_modified_at IS NOT OLD.pdf_modified_at)
          OR (OLD.original_pdf IS NOT NULL AND NEW.original_pdf IS NOT OLD.original_pdf)
          OR (OLD.pdf_metadata_json IS NOT NULL AND NEW.pdf_metadata_json IS NOT OLD.pdf_metadata_json)
          OR NEW.original_pdf IS NULL BEGIN
            SELECT RAISE(ABORT, 'PDF intake documents are append-only');
        END;
        """,
    ),
    (
        16,
        "pdf_exact_article_identity_links",
        """
        ALTER TABLE pdf_intake_articles ADD COLUMN core_article_id TEXT
            REFERENCES articles(article_id);
        ALTER TABLE pdf_intake_articles ADD COLUMN confirmation_basis TEXT CHECK (
            confirmation_basis IS NULL
            OR confirmation_basis = 'exact_url_eligible_detail'
        );

        CREATE INDEX idx_pdf_articles_core
            ON pdf_intake_articles(core_article_id);

        CREATE TRIGGER pdf_intake_articles_confirmed_link_is_immutable
        BEFORE UPDATE OF core_article_id, confirmation_basis ON pdf_intake_articles
        WHEN OLD.core_article_id IS NOT NULL
          OR (NEW.core_article_id IS NULL AND NEW.confirmation_basis IS NOT NULL)
          OR (NEW.core_article_id IS NOT NULL
              AND NEW.confirmation_basis IS NOT 'exact_url_eligible_detail')
        BEGIN
            SELECT RAISE(ABORT, 'PDF intake article confirmation is immutable');
        END;
        """,
    ),
    (
        17,
        "independent_information_checks",
        "\n".join(_INFORMATION_CHECK_SQL.format(kind=kind, source_table=source_table)
                  for kind, source_table in (("meeting", "pdf_intake_calendar_items"),
                                              ("article", "pdf_intake_article_occurrences"))),
    ),
    (
        18,
        "article_date_observations",
        """
        CREATE TABLE article_date_observations (
            observation_id TEXT PRIMARY KEY,
            article_id TEXT NOT NULL REFERENCES articles(article_id),
            canonical_url TEXT NOT NULL,
            observation_kind TEXT NOT NULL CHECK (
                observation_kind IN ('collection', 'page_information')
            ),
            observed_at TEXT NOT NULL,
            source_system TEXT NOT NULL,
            source_database TEXT NOT NULL,
            source_table TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
            recorded_at TEXT NOT NULL,
            UNIQUE (
                article_id, observation_kind, source_system, source_database,
                source_table, source_record_id
            )
        );

        CREATE INDEX idx_article_date_observations_article_kind_time
            ON article_date_observations(article_id, observation_kind, observed_at);

        CREATE TRIGGER article_date_observations_are_append_only_update
        BEFORE UPDATE ON article_date_observations BEGIN
            SELECT RAISE(ABORT, 'article date observations are append-only');
        END;
        CREATE TRIGGER article_date_observations_are_append_only_delete
        BEFORE DELETE ON article_date_observations BEGIN
            SELECT RAISE(ABORT, 'article date observations are append-only');
        END;
        """,
    ),
)


MIGRATIONS += ((19, "material_knowledge_versions", """
    CREATE TABLE knowledge_versions (
        knowledge_id TEXT PRIMARY KEY,
        entity_kind TEXT NOT NULL CHECK(entity_kind IN ('article','meeting')),
        entity_id TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK(source_kind IN ('site','search','pdf','information_check')),
        source_ref TEXT NOT NULL,
        material_sha256 TEXT NOT NULL,
        fields_json TEXT NOT NULL CHECK(json_valid(fields_json)),
        evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
        first_ingested_at TEXT,
        substantive_updated_at TEXT,
        recorded_at TEXT NOT NULL,
        time_basis TEXT NOT NULL
    );
    CREATE INDEX idx_knowledge_entity ON knowledge_versions(entity_kind, entity_id, recorded_at);
    CREATE TRIGGER knowledge_versions_append_only_update BEFORE UPDATE ON knowledge_versions BEGIN
        SELECT RAISE(ABORT, 'knowledge versions are append-only');
    END;
    CREATE TRIGGER knowledge_versions_append_only_delete BEFORE DELETE ON knowledge_versions BEGIN
        SELECT RAISE(ABORT, 'knowledge versions are append-only');
    END;
"""),)


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


def reconcile_pdf_article_links(
    connection: sqlite3.Connection, *, observed_at: str | None = None, canonical_url: str | None = None,
) -> None:
    """Link only PDF records with an exact, already evidenced core article."""
    where = "AND pdf.canonical_url=?" if canonical_url else ""
    rows = connection.execute(
        """SELECT pdf.article_id, core.article_id, core.canonical_url, pdf.imported_at
           FROM pdf_intake_articles pdf JOIN articles core ON core.canonical_url=pdf.canonical_url
           WHERE pdf.core_article_id IS NULL AND core.document_kind='article'
             AND core.publication_eligible=1
             AND json_valid(pdf.type_safe_classification_json)=1
             AND json_extract(pdf.type_safe_classification_json, '$.label')='article'
             AND (core.current_version_id IS NOT NULL
                  OR EXISTS (SELECT 1 FROM article_content_versions content WHERE content.article_id=core.article_id)) """ + where,
        (canonical_url,) if canonical_url else (),
    ).fetchall()
    for pdf_article_id, core_article_id, canonical, imported_at in rows:
        if not connection.execute(
            """UPDATE pdf_intake_articles SET core_article_id=?, confirmation_basis='exact_url_eligible_detail'
               WHERE article_id=? AND core_article_id IS NULL""", (core_article_id, pdf_article_id),
        ).rowcount:
            continue
        for (raw_url,) in connection.execute(
            "SELECT raw_url FROM pdf_intake_article_occurrences WHERE article_id=?", (pdf_article_id,),
        ):
            connection.execute(
                """INSERT INTO url_aliases(raw_url, canonical_url, article_id, first_seen, last_seen, times_seen)
                   VALUES (?, ?, ?, ?, ?, 1)
                   ON CONFLICT(raw_url) DO UPDATE SET first_seen=MIN(first_seen, excluded.first_seen),
                     last_seen=MAX(last_seen, excluded.last_seen), times_seen=times_seen+1""",
                (raw_url, canonical, core_article_id, observed_at or imported_at, observed_at or imported_at),
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
    if 16 in installed:
        reconcile_pdf_article_links(connection)
        connection.commit()
    return installed
