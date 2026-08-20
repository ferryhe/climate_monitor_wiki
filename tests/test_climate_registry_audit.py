import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from climate_registry import schema as registry_schema
from climate_registry.audit import build_audit_registry
from climate_registry.errors import RegistryBuildError, RegistryInputError


def _weekly(date: str, a_items: str, b_items: str) -> str:
    return f"""# Weekly Climate & Actuarial Monitor
**Report Date:** {date}
## Executive Summary
- Sites checked: **2**, succeeded: **2**, failed: **0**
## Pillar A — Changes
{a_items}
## Pillar B — Intelligence
{b_items}
## Original Links
- https://example.com/one
"""


def _item(title: str, summary: str, url: str) -> str:
    return f"- **{title}** (web)\n  - {summary}\n  🔗 {url}\n"


def test_builds_fresh_database_duplicate_audit_and_weekly_manifests(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    first = source_dir / "climate-monitor-2026-08-03.md"
    second = source_dir / "climate-monitor-2026-08-10.md"
    first.write_text(
        _weekly(
            "2026-08-03",
            _item("Original title", "First summary.", "https://example.com/one?utm_source=x"),
            _item("Same title", "A different article.", "https://other.example/item"),
        ),
        encoding="utf-8",
    )
    second.write_text(
        _weekly(
            "2026-08-10",
            _item("Original title revised", "Changed summary.", "https://example.com/one"),
            _item("Same title", "Title collision.", "https://third.example/item")
            + _item("Cross pillar copy", "Same URL in one report.", "https://example.com/one"),
        ),
        encoding="utf-8",
    )
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_dir.iterdir()}
    database = tmp_path / "registry.sqlite3"
    output = tmp_path / "audit"

    result = build_audit_registry(source_dir, database, output)

    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_dir.iterdir()}
    assert before == after
    assert result["reports"] == 2
    assert result["unique_articles"] == 3
    assert result["within_report_duplicates"] == 1
    assert result["content_version_changes"] == 1
    duplicate_report = json.loads((output / "duplicate-report.json").read_text(encoding="utf-8"))
    assert duplicate_report["counts"]["repeated_url_articles"] == 1
    assert duplicate_report["counts"]["title_collisions"] == 1
    assert duplicate_report["counts"]["cross_pillar_articles"] == 1
    assert "not external page content" in duplicate_report["content_version_basis"]

    manifest = json.loads(
        (output / "weekly-manifests" / "weekly-manifest-2026-08-10.json").read_text(encoding="utf-8")
    )
    assert manifest["counts"] == {
        "articles": 2,
        "eligible_articles": 2,
        "excluded_articles": 0,
        "new": 1,
        "pillar_a": 1,
        "pillar_b": 1,
        "previously_seen": 0,
        "updated": 1,
        "within_report_duplicates": 1,
    }
    assert all(article["article_id"].startswith("article-") for article in manifest["articles"])

    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (6,)
    assert connection.execute("SELECT COUNT(*) FROM discoveries").fetchone() == (5,)
    assert connection.execute("SELECT COUNT(*) FROM url_aliases").fetchone() == (4,)
    assert connection.execute(
        "SELECT COUNT(*) FROM article_versions WHERE article_id = ?",
        (manifest["articles"][0]["article_id"],),
    ).fetchone() == (3,)
    assert len(duplicate_report["content_versions"][0]["version_ids"]) == 3
    current_version = connection.execute(
        "SELECT current_version_id FROM articles WHERE article_id = ?",
        (manifest["articles"][0]["article_id"],),
    ).fetchone()[0]
    selected_version = connection.execute(
        """
        SELECT d.version_id FROM discoveries d JOIN reports r ON r.report_id = d.report_id
        WHERE d.article_id = ? AND r.report_date = '2026-08-10' AND d.selected = 1
        """,
        (manifest["articles"][0]["article_id"],),
    ).fetchone()[0]
    duplicate_version = connection.execute(
        """
        SELECT d.version_id FROM discoveries d JOIN reports r ON r.report_id = d.report_id
        WHERE d.article_id = ? AND r.report_date = '2026-08-10' AND d.selected = 0
        """,
        (manifest["articles"][0]["article_id"],),
    ).fetchone()[0]
    assert current_version == selected_version
    assert current_version != duplicate_version

    second_database = tmp_path / "registry-second.sqlite3"
    second_output = tmp_path / "audit-second"
    build_audit_registry(source_dir, second_database, second_output)
    assert (output / "duplicate-report.json").read_bytes() == (second_output / "duplicate-report.json").read_bytes()
    assert (output / "weekly-manifests" / "weekly-manifest-2026-08-10.json").read_bytes() == (
        second_output / "weekly-manifests" / "weekly-manifest-2026-08-10.json"
    ).read_bytes()


def test_refuses_to_modify_existing_database_or_output(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "climate-monitor-2026-08-10.md").write_text(
        _weekly("2026-08-10", _item("A", "Summary.", "https://example.com/a"), ""),
        encoding="utf-8",
    )
    database = tmp_path / "existing.sqlite3"
    database.write_bytes(b"do not replace")

    with pytest.raises(RegistryInputError, match="refusing to modify"):
        build_audit_registry(source_dir, database, tmp_path / "output")
    assert database.read_bytes() == b"do not replace"

    database.unlink()
    output = tmp_path / "output"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(RegistryInputError, match="refusing to overwrite"):
        build_audit_registry(source_dir, database, output)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_refuses_destinations_inside_source_directory(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "climate-monitor-2026-08-10.md").write_text(
        _weekly("2026-08-10", _item("A", "Summary.", "https://example.com/a"), ""),
        encoding="utf-8",
    )

    with pytest.raises(RegistryInputError, match="outside the read-only source"):
        build_audit_registry(source_dir, source_dir / "registry.sqlite3", tmp_path / "output")
    with pytest.raises(RegistryInputError, match="outside the read-only source"):
        build_audit_registry(source_dir, tmp_path / "registry.sqlite3", source_dir / "output")


def test_parse_failure_creates_no_database_or_output(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "climate-monitor-2026-08-10.md").write_text(
        "# Weekly Climate Monitor\n## Pillar A\n## Pillar B\n",
        encoding="utf-8",
    )
    database = tmp_path / "registry.sqlite3"
    output = tmp_path / "output"

    with pytest.raises(RegistryBuildError, match="could not parse report history"):
        build_audit_registry(source_dir, database, output)
    assert not database.exists()
    assert not output.exists()


def test_weekly_manifest_separates_ineligible_landing_and_topic_pages(tmp_path):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "climate-monitor-2026-08-10.md").write_text(
        _weekly(
            "2026-08-10",
            _item("Article", "Article summary.", "https://example.com/news/article"),
            _item("Home", "Home summary.", "https://www.worldbank.org/")
            + _item("Topic", "Topic summary.", "https://www.iais.org/activities-topics/climate-risk/"),
        ),
        encoding="utf-8",
    )

    output = tmp_path / "audit"
    build_audit_registry(source_dir, tmp_path / "registry.sqlite3", output)
    manifest = json.loads(
        (output / "weekly-manifests" / "weekly-manifest-2026-08-10.json").read_text(encoding="utf-8")
    )

    assert manifest["counts"]["eligible_articles"] == 1
    assert manifest["counts"]["excluded_articles"] == 2
    assert [item["document_kind"] for item in manifest["articles"]] == ["article"]
    assert {item["document_kind"] for item in manifest["excluded_articles"]} == {
        "landing_page",
        "topic_index",
    }
    assert all(item["publication_eligible"] is False for item in manifest["excluded_articles"])


def test_audit_schema_metadata_tracks_latest_migration(tmp_path, monkeypatch):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "climate-monitor-2026-08-10.md").write_text(
        _weekly("2026-08-10", _item("A", "Summary.", "https://example.com/a"), ""),
        encoding="utf-8",
    )
    future_version = registry_schema.MIGRATIONS[-1][0] + 1
    monkeypatch.setattr(
        registry_schema,
        "MIGRATIONS",
        (
            *registry_schema.MIGRATIONS,
            (
                future_version,
                "test_future_migration",
                "CREATE INDEX idx_test_reports_cadence ON reports(cadence);",
            ),
        ),
    )
    database = tmp_path / "registry.sqlite3"
    output = tmp_path / "audit"
    build_audit_registry(source_dir, database, output)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (future_version,)
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone() == (future_version,)
    finally:
        connection.close()
    duplicate_report = json.loads((output / "duplicate-report.json").read_text(encoding="utf-8"))
    weekly_manifest = json.loads(
        (output / "weekly-manifests" / "weekly-manifest-2026-08-10.json").read_text(
            encoding="utf-8"
        )
    )
    assert duplicate_report["schema_version"] == future_version
    assert weekly_manifest["schema_version"] == future_version
