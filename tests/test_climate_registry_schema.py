import sqlite3

import pytest

from climate_registry.schema import apply_migrations


def test_migrations_are_idempotent_and_enable_foreign_keys():
    connection = sqlite3.connect(":memory:")

    assert apply_migrations(connection) == [1, 2, 3, 4, 5, 6]
    assert apply_migrations(connection) == []
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert connection.execute("PRAGMA user_version").fetchone() == (6,)
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "schema_migrations",
        "sources",
        "reports",
        "articles",
        "url_aliases",
        "article_versions",
        "article_fetches",
        "article_content_versions",
        "article_enrichments",
        "article_capture_resolutions",
        "discoveries",
        "report_appearances",
    } <= tables


def test_migration_enforces_report_article_uniqueness():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    with connection:
        connection.execute("INSERT INTO sources VALUES ('s', 'example.com', 'example.com', '2026-01-01', '2026-01-01')")
        connection.execute(
            "INSERT INTO reports VALUES ('r', '2026-01-01', 'r.md', 'R', 'sha', 'weekly', 'weekly-pillars-v1', 1, 1, 0, '[]')"
        )
        connection.execute(
            """
            INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen, current_version_id)
            VALUES ('a', 'https://example.com/a', 's', '2026-01-01', '2026-01-01', NULL)
            """
        )
        connection.execute(
            "INSERT INTO article_versions VALUES ('v', 'a', 'Title', 'title', 'Summary', 'fp', 'report-title-summary', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO discoveries VALUES ('d1', 'r', 1, 'pillar-a', 'A', 'a', 'v', 'https://example.com/a', 'Title', 'Summary', 1, NULL)"
        )
        connection.execute(
            """
            INSERT INTO report_appearances(
                report_id, article_id, version_id, discovery_id, section, pillar, ordinal, disposition
            ) VALUES ('r', 'a', 'v', 'd1', 'pillar-a', 'A', 1, 'new')
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO report_appearances(
                report_id, article_id, version_id, discovery_id, section, pillar, ordinal, disposition
            ) VALUES ('r', 'a', 'v', 'd1', 'pillar-b', 'B', 2, 'previously-seen')
            """
        )


def test_v1_registry_migrates_observation_semantics_without_claiming_external_change():
    connection = sqlite3.connect(":memory:")
    assert apply_migrations(connection, target_version=1) == [1]
    with connection:
        connection.execute("INSERT INTO sources VALUES ('s', 'example.com', 'example.com', '2026-01-01', '2026-01-01')")
        connection.execute(
            "INSERT INTO reports VALUES ('r', '2026-01-01', 'r.md', 'R', 'sha', 'weekly', 'weekly-pillars-v1', 1, 1, 0, '[]')"
        )
        connection.execute(
            "INSERT INTO articles VALUES ('a', 'https://example.com/a', 's', '2026-01-01', '2026-01-01', NULL)"
        )
        connection.execute(
            "INSERT INTO article_versions VALUES ('v', 'a', 'Title', 'title', 'Summary', 'fp', 'report-title-summary', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO discoveries VALUES ('d', 'r', 1, 'pillar-a', 'A', 'a', 'v', 'https://example.com/a', 'Title', 'Summary', 1, NULL)"
        )
        connection.execute("INSERT INTO report_appearances VALUES ('r', 'a', 'v', 'd', 'pillar-a', 'A', 1, 'updated')")

    assert apply_migrations(connection, target_version=2) == [2]
    assert connection.execute("PRAGMA user_version").fetchone() == (2,)
    assert connection.execute(
        "SELECT observation_status, external_content_change FROM report_appearances"
    ).fetchone() == ("new_report_representation", "unknown")


def test_migrations_refuse_to_commit_callers_active_transaction():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE caller_state(value TEXT)")
    connection.commit()
    connection.execute("INSERT INTO caller_state VALUES ('uncommitted')")

    with pytest.raises(sqlite3.ProgrammingError, match="active transaction"):
        apply_migrations(connection)

    assert connection.in_transaction
    connection.rollback()
    assert connection.execute("SELECT * FROM caller_state").fetchall() == []
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone() == (0,)


def test_migrations_refuse_downgrade_without_changing_database_or_connection_state():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE caller_state(value TEXT)")
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    schema_before = connection.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    total_changes_before = connection.total_changes

    with pytest.raises(ValueError, match="backward"):
        apply_migrations(connection, target_version=1)

    assert connection.execute("PRAGMA user_version").fetchone() == (2,)
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (0,)
    assert connection.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall() == schema_before
    assert connection.total_changes == total_changes_before
    assert not connection.in_transaction


def _insert_article(connection: sqlite3.Connection, *, article_id: str = "a") -> None:
    connection.execute(
        "INSERT INTO sources VALUES ('s', 'example.com', 'Example', '2026-01-01', '2026-01-01')"
    )
    connection.execute(
        """
        INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen)
        VALUES (?, ?, 's', '2026-01-01', '2026-01-01')
        """,
        (article_id, f"https://example.com/{article_id}"),
    )


def test_v2_to_v3_preserves_existing_rows_and_defaults_to_summary_excerpt():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection, target_version=2)
    _insert_article(connection)
    connection.commit()
    counts_before = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("sources", "articles", "article_versions", "reports", "discoveries")
    }

    assert apply_migrations(connection) == [3, 4, 5, 6]

    assert connection.execute("PRAGMA user_version").fetchone() == (6,)
    assert connection.execute(
        "SELECT current_content_version_id, display_policy FROM articles WHERE article_id = 'a'"
    ).fetchone() == (None, "summary_excerpt")
    assert counts_before == {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in counts_before
    }


def test_v2_to_v3_preserves_the_historical_audit_baseline_counts():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection, target_version=2)
    connection.execute(
        "INSERT INTO sources VALUES ('s', 'example.com', 'Example', '2026-01-01', '2026-01-24')"
    )
    for index in range(24):
        report_number = index + 1
        connection.execute(
            """
            INSERT INTO reports VALUES (?, ?, ?, ?, ?, 'legacy-daily',
                                        'legacy-report-v1', NULL, NULL, NULL, '[]')
            """,
            (
                f"r{report_number}",
                f"2026-01-{report_number:02d}",
                f"report-{report_number}.md",
                f"Report {report_number}",
                f"sha-{report_number}",
            ),
        )
    for index in range(147):
        article_number = index + 1
        connection.execute(
            """
            INSERT INTO articles(
                article_id, canonical_url, source_id, first_seen, last_seen,
                current_version_id
            ) VALUES (?, ?, 's', '2026-01-01', '2026-01-24', ?)
            """,
            (f"a{article_number}", f"https://example.com/{article_number}", f"v{article_number}"),
        )
        connection.execute(
            """
            INSERT INTO article_versions VALUES (?, ?, ?, ?, ?, ?, 'report-title-summary',
                                                 '2026-01-01', '2026-01-24')
            """,
            (
                f"v{article_number}",
                f"a{article_number}",
                f"Title {article_number}",
                f"title {article_number}",
                f"Summary {article_number}",
                f"fingerprint-{article_number}",
            ),
        )
    for index in range(240):
        report_number = index % 24 + 1
        article_number = index % 147 + 1
        ordinal = index // 24 + 1
        connection.execute(
            """
            INSERT INTO discoveries VALUES (?, ?, ?, 'pillar-a', 'A', ?, ?, ?, ?, ?, 1, NULL)
            """,
            (
                f"d{index + 1}",
                f"r{report_number}",
                ordinal,
                f"a{article_number}",
                f"v{article_number}",
                f"https://example.com/{article_number}",
                f"Title {article_number}",
                f"Summary {article_number}",
            ),
        )
    connection.commit()
    counts_before = {
        "reports": 24,
        "discoveries": 240,
        "articles": 147,
    }
    assert {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in counts_before
    } == counts_before

    assert apply_migrations(connection) == [3, 4, 5, 6]

    assert {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in counts_before
    } == counts_before


def test_content_fetch_and_enrichment_contract_round_trips_unicode_metadata():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    _insert_article(connection)
    markdown = "# 风险摘要\n\n北极升温与保险损失。"
    with connection:
        connection.execute(
            """
            INSERT INTO article_content_versions(
                content_version_id, article_id, content_sha256, markdown_content,
                markdown_sha256, content_type, source_bytes, extraction_method,
                extraction_version, first_fetched_at
            ) VALUES ('cv1', 'a', ?, ?, ?, 'text/html', 2048, 'html-to-markdown', '1', ?)
            """,
            ("a" * 64, markdown, "b" * 64, "2026-08-13T12:00:00Z"),
        )
        connection.execute(
            """
            INSERT INTO article_fetches(
                fetch_id, article_id, requested_url, final_url, fetched_at,
                fetch_status, http_status, content_type, etag, last_modified,
                content_version_id
            ) VALUES ('fetch1', 'a', 'https://example.com/a', 'https://example.com/a', ?,
                      'success', 200, 'text/html', 'etag-1', 'Wed, 13 Aug 2026 12:00:00 GMT', 'cv1')
            """,
            ("2026-08-13T12:00:00Z",),
        )
        connection.execute(
            """
            INSERT INTO article_enrichments(
                enrichment_id, content_version_id, status, summary, categories_json,
                keywords_json, language, generator_kind, generator_name,
                generator_version, generated_at
            ) VALUES ('e1', 'cv1', 'complete', ?, ?, ?, 'zh', 'deterministic',
                      'fixture-enricher', '1', ?)
            """,
            (
                "本周气候风险摘要。",
                '["气候风险","保险"]',
                '["北极","损失"]',
                "2026-08-13T12:01:00Z",
            ),
        )
        connection.execute(
            "UPDATE articles SET current_content_version_id = 'cv1', display_policy = 'full_markdown' WHERE article_id = 'a'"
        )

    assert connection.execute(
        """
        SELECT c.markdown_content, e.summary, e.categories_json, e.keywords_json,
               a.current_content_version_id, a.display_policy
        FROM articles a
        JOIN article_content_versions c ON c.content_version_id = a.current_content_version_id
        JOIN article_enrichments e ON e.content_version_id = c.content_version_id
        """
    ).fetchone() == (
        markdown,
        "本周气候风险摘要。",
        '["气候风险","保险"]',
        '["北极","损失"]',
        "cv1",
        "full_markdown",
    )


@pytest.mark.parametrize(
    ("status", "http_status", "content_version_id", "error_code"),
    (
        ("success", 200, None, None),
        ("success", 500, "cv1", None),
        ("success", None, "cv1", None),
        ("not_modified", 304, None, None),
        ("not_modified", 200, "cv1", None),
        ("not_modified", None, "cv1", None),
        ("failed", 500, "cv1", "http-error"),
        ("failed", 500, None, None),
    ),
)
def test_fetch_status_rejects_inconsistent_http_and_content_results(
    status, http_status, content_version_id, error_code
):
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    _insert_article(connection)
    connection.execute(
        """
        INSERT INTO article_content_versions(
            content_version_id, article_id, content_sha256, markdown_content,
            markdown_sha256, content_type, extraction_method, extraction_version, first_fetched_at
        ) VALUES ('cv1', 'a', ?, '# A', ?, 'text/html', 'fixture', '1', '2026-08-13T12:00:00Z')
        """,
        ("a" * 64, "b" * 64),
    )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO article_fetches(
                fetch_id, article_id, requested_url, final_url, fetched_at,
                fetch_status, http_status, error_code, content_version_id
            ) VALUES ('fetch', 'a', 'https://example.com/a', 'https://example.com/a',
                      '2026-08-13T12:00:00Z', ?, ?, ?, ?)
            """,
            (status, http_status, error_code, content_version_id),
        )


def test_not_modified_fetch_identifies_the_existing_content_version():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    _insert_article(connection)
    with connection:
        connection.execute(
            """
            INSERT INTO article_content_versions(
                content_version_id, article_id, content_sha256, markdown_content,
                markdown_sha256, content_type, extraction_method, extraction_version, first_fetched_at
            ) VALUES ('cv1', 'a', ?, '# A', ?, 'text/html', 'fixture', '1', '2026-08-13T12:00:00Z')
            """,
            ("a" * 64, "b" * 64),
        )
        connection.execute(
            """
            INSERT INTO article_fetches(
                fetch_id, article_id, requested_url, final_url, fetched_at,
                fetch_status, http_status, content_version_id
            ) VALUES ('fetch', 'a', 'https://example.com/a', 'https://example.com/a',
                      '2026-08-13T13:00:00Z', 'not_modified', 304, 'cv1')
            """
        )

    assert connection.execute(
        "SELECT fetch_status, content_version_id FROM article_fetches"
    ).fetchone() == ("not_modified", "cv1")


def test_failed_fetch_accepts_transport_success_http_errors_or_no_response():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    _insert_article(connection)
    with connection:
        connection.execute(
            """
            INSERT INTO article_fetches(
                fetch_id, article_id, requested_url, final_url, fetched_at,
                fetch_status, http_status, error_code, error_message
            ) VALUES ('http-failure', 'a', 'https://example.com/a', 'https://example.com/a',
                      '2026-08-13T13:00:00Z', 'failed', 503, 'http-error', 'temporarily unavailable')
            """
        )
        connection.execute(
            """
            INSERT INTO article_fetches(
                fetch_id, article_id, requested_url, final_url, fetched_at,
                fetch_status, http_status, error_code, error_message
            ) VALUES ('extraction-failure', 'a', 'https://example.com/a', 'https://example.com/a',
                      '2026-08-13T13:00:30Z', 'failed', 200, 'extract-error', 'unsupported body')
            """
        )
        connection.execute(
            """
            INSERT INTO article_fetches(
                fetch_id, article_id, requested_url, fetched_at,
                fetch_status, error_code
            ) VALUES ('network-failure', 'a', 'https://example.com/a',
                      '2026-08-13T13:01:00Z', 'failed', 'network-error')
            """
        )

    assert connection.execute(
        "SELECT fetch_id, http_status FROM article_fetches ORDER BY fetch_id"
    ).fetchall() == [
        ("extraction-failure", 200),
        ("http-failure", 503),
        ("network-failure", None),
    ]


def test_content_versions_are_unique_per_article_and_foreign_keys_are_enforced():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    _insert_article(connection)
    values = ("a" * 64, "b" * 64)
    connection.execute(
        """
        INSERT INTO article_content_versions(
            content_version_id, article_id, content_sha256, markdown_content,
            markdown_sha256, content_type, extraction_method, extraction_version, first_fetched_at
        ) VALUES ('cv1', 'a', ?, '# A', ?, 'text/html', 'fixture', '1', '2026-08-13T12:00:00Z')
        """,
        values,
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO article_content_versions(
                content_version_id, article_id, content_sha256, markdown_content,
                markdown_sha256, content_type, extraction_method, extraction_version, first_fetched_at
            ) VALUES ('cv2', 'a', ?, '# B', ?, 'text/html', 'fixture', '1', '2026-08-13T12:01:00Z')
            """,
            values,
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO article_content_versions(
                content_version_id, article_id, content_sha256, markdown_content,
                markdown_sha256, content_type, extraction_method, extraction_version, first_fetched_at
            ) VALUES ('missing', 'missing', ?, '# Missing', ?, 'text/html', 'fixture', '1', '2026-08-13T12:00:00Z')
            """,
            ("c" * 64, "d" * 64),
        )
    connection.execute(
        """
        INSERT INTO articles(article_id, canonical_url, source_id, first_seen, last_seen)
        VALUES ('b', 'https://example.com/b', 's', '2026-01-01', '2026-01-01')
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO article_fetches(
                fetch_id, article_id, requested_url, final_url, fetched_at,
                fetch_status, http_status, content_version_id
            ) VALUES ('wrong-owner', 'b', 'https://example.com/b', 'https://example.com/b',
                      '2026-08-13T12:00:00Z', 'success', 200, 'cv1')
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="another article"):
        connection.execute(
            "UPDATE articles SET current_content_version_id = 'cv1' WHERE article_id = 'b'"
        )


def test_content_versions_are_immutable_and_require_sha256_values():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    _insert_article(connection)
    connection.execute(
        """
        INSERT INTO article_content_versions(
            content_version_id, article_id, content_sha256, markdown_content,
            markdown_sha256, content_type, extraction_method, extraction_version, first_fetched_at
        ) VALUES ('cv1', 'a', ?, '# A', ?, 'text/html', 'fixture', '1', '2026-08-13T12:00:00Z')
        """,
        ("a" * 64, "b" * 64),
    )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE article_content_versions SET markdown_content = '# Changed' WHERE content_version_id = 'cv1'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "DELETE FROM article_content_versions WHERE content_version_id = 'cv1'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO article_content_versions(
                content_version_id, article_id, content_sha256, markdown_content,
                markdown_sha256, content_type, extraction_method, extraction_version, first_fetched_at
            ) VALUES ('bad', 'a', ?, '# B', ?, 'text/html', 'fixture', '1', '2026-08-13T13:00:00Z')
            """,
            ("z" * 64, "b" * 64),
        )


@pytest.mark.parametrize(
    "values",
    (
        ("complete", None, None, None, None, None, None),
        ("failed", "summary", "[]", "[]", "en", "failure", "details"),
        ("failed", None, None, None, None, None, None),
        ("failed", None, None, None, None, "", "details"),
        ("failed", None, None, None, None, "   ", "details"),
    ),
)
def test_enrichment_status_rejects_incomplete_or_misleading_rows(values):
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    _insert_article(connection)
    connection.execute(
        """
        INSERT INTO article_content_versions(
            content_version_id, article_id, content_sha256, markdown_content,
            markdown_sha256, content_type, extraction_method, extraction_version, first_fetched_at
        ) VALUES ('cv1', 'a', ?, '# A', ?, 'text/html', 'fixture', '1', '2026-08-13T12:00:00Z')
        """,
        ("a" * 64, "b" * 64),
    )
    status, summary, categories, keywords, language, error_code, error_message = values

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO article_enrichments(
                enrichment_id, content_version_id, status, summary, categories_json,
                keywords_json, language, generator_kind, generator_name,
                generator_version, generated_at, error_code, error_message
            ) VALUES ('e1', 'cv1', ?, ?, ?, ?, ?, 'deterministic', 'fixture', '1',
                      '2026-08-13T12:01:00Z', ?, ?)
            """,
            (status, summary, categories, keywords, language, error_code, error_message),
        )


def test_fetches_and_enrichments_are_append_only_audit_records():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    _insert_article(connection)
    with connection:
        connection.execute(
            """
            INSERT INTO article_content_versions(
                content_version_id, article_id, content_sha256, markdown_content,
                markdown_sha256, content_type, extraction_method, extraction_version, first_fetched_at
            ) VALUES ('cv1', 'a', ?, '# A', ?, 'text/html', 'fixture', '1', '2026-08-13T12:00:00Z')
            """,
            ("a" * 64, "b" * 64),
        )
        connection.execute(
            """
            INSERT INTO article_fetches(
                fetch_id, article_id, requested_url, final_url, fetched_at,
                fetch_status, http_status, content_version_id
            ) VALUES ('fetch1', 'a', 'https://example.com/a', 'https://example.com/a',
                      '2026-08-13T12:00:00Z', 'success', 200, 'cv1')
            """
        )
        connection.execute(
            """
            INSERT INTO article_enrichments(
                enrichment_id, content_version_id, status, summary, categories_json,
                keywords_json, language, generator_kind, generator_name,
                generator_version, generated_at
            ) VALUES ('e1', 'cv1', 'complete', 'Summary', '[]', '[]', 'en',
                      'deterministic', 'fixture', '1', '2026-08-13T12:01:00Z')
            """
        )

    for statement in (
        "UPDATE article_fetches SET fetched_at = 'changed' WHERE fetch_id = 'fetch1'",
        "DELETE FROM article_fetches WHERE fetch_id = 'fetch1'",
        "UPDATE article_enrichments SET generated_at = 'changed' WHERE enrichment_id = 'e1'",
        "DELETE FROM article_enrichments WHERE enrichment_id = 'e1'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(statement)

    assert connection.execute(
        "SELECT fetched_at FROM article_fetches WHERE fetch_id = 'fetch1'"
    ).fetchone() == ("2026-08-13T12:00:00Z",)
    assert connection.execute(
        "SELECT generated_at FROM article_enrichments WHERE enrichment_id = 'e1'"
    ).fetchone() == ("2026-08-13T12:01:00Z",)


def test_failed_enrichment_accepts_a_real_error_code_without_display_content():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    _insert_article(connection)
    with connection:
        connection.execute(
            """
            INSERT INTO article_content_versions(
                content_version_id, article_id, content_sha256, markdown_content,
                markdown_sha256, content_type, extraction_method, extraction_version, first_fetched_at
            ) VALUES ('cv1', 'a', ?, '# A', ?, 'text/html', 'fixture', '1',
                      '2026-08-13T12:00:00Z')
            """,
            ("a" * 64, "b" * 64),
        )
        connection.execute(
            """
            INSERT INTO article_enrichments(
                enrichment_id, content_version_id, status, generator_kind,
                generator_name, generator_version, generated_at, error_code, error_message
            ) VALUES ('failed', 'cv1', 'failed', 'deterministic', 'fixture', '1',
                      '2026-08-13T12:01:00Z', 'summary-error', 'could not summarize')
            """
        )

    assert connection.execute(
        """
        SELECT summary, categories_json, keywords_json, language, error_code, error_message
        FROM article_enrichments WHERE enrichment_id = 'failed'
        """
    ).fetchone() == (None, None, None, None, "summary-error", "could not summarize")


def test_v3_indexes_support_article_history_queries():
    connection = sqlite3.connect(":memory:")
    apply_migrations(connection)
    indexes = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name IN (?, ?, ?)",
            ("article_fetches", "article_content_versions", "article_enrichments"),
        )
    }

    assert {
        "idx_article_fetches_article_fetched",
        "idx_content_versions_article_fetched",
        "idx_enrichments_content_generated",
    } <= indexes


def test_article_semantics_have_relational_constraints_without_redundant_index():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(connection)

    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(article_semantics)")
    }
    assert "report_id" in columns
    indexes = {
        row[1]
        for row in connection.execute("PRAGMA index_list(article_semantics)")
    }
    assert "idx_article_semantics_report" not in indexes

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO article_semantics(
                report_id, report_sha256, article_id, validated_at
            ) VALUES ('missing-report', ?, 'missing-article', '2026-08-17T00:00:00Z')
            """,
            ("a" * 64,),
        )
