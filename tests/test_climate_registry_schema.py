import sqlite3

from climate_registry.schema import apply_migrations


def test_migrations_are_idempotent_and_enable_foreign_keys():
    connection = sqlite3.connect(":memory:")

    assert apply_migrations(connection) == [1]
    assert apply_migrations(connection) == []
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert connection.execute("PRAGMA user_version").fetchone() == (1,)
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
        connection.execute("INSERT INTO articles VALUES ('a', 'https://example.com/a', 's', '2026-01-01', '2026-01-01', NULL)")
        connection.execute(
            "INSERT INTO article_versions VALUES ('v', 'a', 'Title', 'title', 'Summary', 'fp', 'report-title-summary', '2026-01-01', '2026-01-01')"
        )
        connection.execute(
            "INSERT INTO discoveries VALUES ('d1', 'r', 1, 'pillar-a', 'A', 'a', 'v', 'https://example.com/a', 'Title', 'Summary', 1, NULL)"
        )
        connection.execute("INSERT INTO report_appearances VALUES ('r', 'a', 'v', 'd1', 'pillar-a', 'A', 1, 'new')")

    with __import__("pytest").raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO report_appearances VALUES ('r', 'a', 'v', 'd1', 'pillar-b', 'B', 2, 'previously-seen')"
        )


def test_migrations_refuse_to_commit_callers_active_transaction():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE caller_state(value TEXT)")
    connection.commit()
    connection.execute("INSERT INTO caller_state VALUES ('uncommitted')")

    with __import__("pytest").raises(sqlite3.ProgrammingError, match="active transaction"):
        apply_migrations(connection)

    assert connection.in_transaction
    connection.rollback()
    assert connection.execute("SELECT * FROM caller_state").fetchall() == []
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone() == (0,)
