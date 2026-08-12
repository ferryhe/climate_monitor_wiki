import hashlib
import sqlite3
from pathlib import Path

import pytest

import climate_registry.persistent as persistent
from climate_registry.audit import build_audit_registry
from climate_registry.errors import RegistryBuildError, RegistryInputError, RegistryLockError
from climate_registry.schema import apply_migrations


def _item(title: str, summary: str, url: str) -> str:
    return f"- **{title}** (web)\n  - {summary}\n  🔗 {url}\n"


def _weekly(date: str, items: str) -> str:
    return f"""# Weekly Climate & Actuarial Monitor
**Report Date:** {date}
## Executive Summary
- Sites checked: **2**, succeeded: **2**, failed: **0**
## Pillar A — Changes
{items}
## Pillar B — Intelligence
## Original Links
- https://example.com/item
"""


def _write_report(source_dir: Path, date: str, items: str) -> Path:
    source_dir.mkdir(exist_ok=True)
    path = source_dir / f"climate-monitor-{date}.md"
    path.write_text(_weekly(date, items), encoding="utf-8")
    return path


def _build_current_registry(source_dir: Path, database: Path, tmp_path: Path) -> None:
    build_audit_registry(source_dir, database, tmp_path / f"audit-{database.stem}")


def test_plan_is_read_only_and_update_is_backed_up_incremental_and_idempotent(tmp_path):
    source_dir = tmp_path / "sources"
    first = _write_report(
        source_dir,
        "2026-08-03",
        _item("Original title", "First summary.", "https://example.com/item"),
    )
    database = tmp_path / "registry.sqlite3"
    _build_current_registry(source_dir, database, tmp_path)
    second = _write_report(
        source_dir,
        "2026-08-10",
        _item("Reworded title", "Reworded summary.", "https://example.com/item")
        + _item("Publisher home", "Landing page.", "https://www.worldbank.org/"),
    )
    source_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)}
    database_before = database.read_bytes()
    backup_dir = tmp_path / "backups"

    plan = persistent.plan_registry_update(source_dir, database)

    assert plan["status"] == "plan"
    assert plan["new_reports"] == [
        {
            "date": "2026-08-10",
            "filename": "climate-monitor-2026-08-10.md",
            "sha256": hashlib.sha256(second.read_bytes()).hexdigest(),
        }
    ]
    assert plan["conflicts"] == []
    assert database.read_bytes() == database_before
    assert not backup_dir.exists()

    result = persistent.update_registry(source_dir, database, backup_dir)

    assert result["status"] == "updated"
    assert result["imported_reports"] == ["2026-08-10"]
    assert result["pending_migrations"] == []
    assert result["mutation_required"] is False
    backup = Path(result["backup"])
    assert backup.parent == backup_dir
    assert backup.is_file()
    assert not database.with_name(f"{database.name}.lock").exists()
    assert source_hashes == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)
    }

    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone() == (2,)
    assert connection.execute("SELECT COUNT(*) FROM reports").fetchone() == (2,)
    assert connection.execute(
        """
        SELECT ra.observation_status, ra.external_content_change
        FROM report_appearances ra JOIN reports r ON r.report_id = ra.report_id
        JOIN articles a ON a.article_id = ra.article_id
        WHERE r.report_date = '2026-08-10' AND a.canonical_url = 'https://example.com/item'
        """
    ).fetchone() == ("new_report_representation", "unknown")
    assert connection.execute(
        """
        SELECT document_kind, publication_eligible, exclusion_reason
        FROM articles WHERE canonical_url = 'https://www.worldbank.org/'
        """
    ).fetchone() == ("landing_page", 0, "root-url")
    connection.close()

    backup_connection = sqlite3.connect(backup)
    assert backup_connection.execute("SELECT COUNT(*) FROM reports").fetchone() == (1,)
    backup_connection.close()
    backup_count = len(list(backup_dir.iterdir()))

    no_op = persistent.update_registry(source_dir, database, backup_dir)
    assert no_op["status"] == "no-op"
    assert no_op["backup"] is None
    assert len(list(backup_dir.iterdir())) == backup_count


def test_update_migrates_v1_registry_and_imports_a_new_report(tmp_path):
    source_dir = tmp_path / "sources"
    report = _write_report(source_dir, "2026-08-03", _item("Title", "Summary.", "https://example.com/item"))
    database = tmp_path / "registry.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection, target_version=1)
    connection.execute(
        """
        INSERT INTO reports VALUES (?, '2026-08-03', ?, 'Weekly', ?, 'weekly',
                                    'weekly-pillars-v1', 2, 2, 0, '[]')
        """,
        ("report-2026-08-03", report.name, hashlib.sha256(report.read_bytes()).hexdigest()),
    )
    connection.commit()
    connection.close()
    _write_report(source_dir, "2026-08-10", _item("New", "New summary.", "https://example.com/new"))

    plan = persistent.plan_registry_update(source_dir, database)
    assert plan["pending_migrations"] == [2]
    assert [item["date"] for item in plan["new_reports"]] == ["2026-08-10"]

    result = persistent.update_registry(source_dir, database, tmp_path / "backups")
    assert result["status"] == "updated"
    assert result["applied_migrations"] == [2]
    assert result["imported_reports"] == ["2026-08-10"]
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone() == (2,)
    assert connection.execute("SELECT COUNT(*) FROM reports").fetchone() == (2,)


def test_changed_or_out_of_order_report_fails_closed_without_mutation(tmp_path):
    source_dir = tmp_path / "sources"
    report = _write_report(source_dir, "2026-08-10", _item("Title", "Summary.", "https://example.com/item"))
    database = tmp_path / "registry.sqlite3"
    _build_current_registry(source_dir, database, tmp_path)
    database_before = database.read_bytes()
    report.write_text(_weekly("2026-08-10", _item("Changed", "Changed.", "https://example.com/item")), encoding="utf-8")

    plan = persistent.plan_registry_update(source_dir, database)
    assert [item["reason"] for item in plan["conflicts"]] == ["existing-report-identity-mismatch"]
    with pytest.raises(RegistryInputError, match="identity conflicts"):
        persistent.update_registry(source_dir, database, tmp_path / "backups")
    assert database.read_bytes() == database_before
    assert not (tmp_path / "backups").exists()

    report.write_text(_weekly("2026-08-10", _item("Title", "Summary.", "https://example.com/item")), encoding="utf-8")
    _write_report(source_dir, "2026-08-03", _item("Older", "Older.", "https://example.com/older"))
    assert any(
        item["reason"] == "out-of-order-history"
        for item in persistent.plan_registry_update(source_dir, database)["conflicts"]
    )

    (source_dir / "climate-monitor-2026-08-03.md").unlink()
    _write_report(source_dir, "2026-08-11", _item("Tuesday", "Off-cycle.", "https://example.com/tuesday"))
    with pytest.raises(RegistryBuildError, match="must be a Monday"):
        persistent.plan_registry_update(source_dir, database)


def test_plan_reports_previously_imported_source_file_as_missing(tmp_path):
    source_dir = tmp_path / "sources"
    first = _write_report(source_dir, "2026-08-03", _item("First", "First.", "https://example.com/first"))
    _write_report(source_dir, "2026-08-10", _item("Second", "Second.", "https://example.com/second"))
    database = tmp_path / "registry.sqlite3"
    _build_current_registry(source_dir, database, tmp_path)
    first.unlink()

    plan = persistent.plan_registry_update(source_dir, database)

    assert any(item["reason"] == "registry-report-missing-from-source" for item in plan["conflicts"])


def test_existing_lock_blocks_update_without_backup(tmp_path):
    source_dir = tmp_path / "sources"
    _write_report(source_dir, "2026-08-03", _item("Title", "Summary.", "https://example.com/item"))
    database = tmp_path / "registry.sqlite3"
    _build_current_registry(source_dir, database, tmp_path)
    lock = database.with_name(f"{database.name}.lock")
    lock.write_text("existing", encoding="ascii")

    with pytest.raises(RegistryLockError, match="locked"):
        persistent.update_registry(source_dir, database, tmp_path / "backups")
    assert lock.read_text(encoding="ascii") == "existing"
    assert not (tmp_path / "backups").exists()


def test_backup_directory_must_not_contain_live_database(tmp_path):
    source_dir = tmp_path / "sources"
    _write_report(source_dir, "2026-08-03", _item("Title", "Summary.", "https://example.com/item"))
    database = tmp_path / "registry.sqlite3"
    _build_current_registry(source_dir, database, tmp_path)

    with pytest.raises(RegistryInputError, match="must not contain"):
        persistent.update_registry(source_dir, database, tmp_path)


def test_plan_rejects_inconsistent_migration_metadata(tmp_path):
    source_dir = tmp_path / "sources"
    _write_report(source_dir, "2026-08-03", _item("Title", "Summary.", "https://example.com/item"))
    database = tmp_path / "registry.sqlite3"
    _build_current_registry(source_dir, database, tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM schema_migrations WHERE version = 2")
    connection.commit()
    connection.close()

    with pytest.raises(RegistryInputError, match="do not agree"):
        persistent.plan_registry_update(source_dir, database)


def test_sqlite_sidecar_fails_closed_before_backup_or_replacement(tmp_path):
    source_dir = tmp_path / "sources"
    _write_report(source_dir, "2026-08-03", _item("Title", "Summary.", "https://example.com/item"))
    database = tmp_path / "registry.sqlite3"
    _build_current_registry(source_dir, database, tmp_path)
    database_before = database.read_bytes()
    wal = Path(f"{database}-wal")
    wal.write_bytes(b"unreconciled")

    with pytest.raises(RegistryInputError, match="sidecar"):
        persistent.update_registry(source_dir, database, tmp_path / "backups")

    assert database.read_bytes() == database_before
    assert wal.read_bytes() == b"unreconciled"
    assert not (tmp_path / "backups").exists()
    assert not database.with_name(f"{database.name}.lock").exists()


def test_failed_candidate_import_leaves_live_database_unchanged(tmp_path, monkeypatch):
    source_dir = tmp_path / "sources"
    _write_report(source_dir, "2026-08-03", _item("Title", "Summary.", "https://example.com/item"))
    database = tmp_path / "registry.sqlite3"
    _build_current_registry(source_dir, database, tmp_path)
    _write_report(source_dir, "2026-08-10", _item("New", "New.", "https://example.com/new"))
    database_before = database.read_bytes()

    def fail_import(*_args, **_kwargs):
        raise RuntimeError("injected import failure")

    monkeypatch.setattr(persistent, "_insert_report", fail_import)
    backup_dir = tmp_path / "backups"
    with pytest.raises(RegistryBuildError, match="injected import failure"):
        persistent.update_registry(source_dir, database, backup_dir)

    assert database.read_bytes() == database_before
    assert len(list(backup_dir.iterdir())) == 1
    assert not database.with_name(f"{database.name}.lock").exists()
    assert not list(database.parent.glob(f".{database.name}.*.candidate"))


def test_live_database_change_detected_before_atomic_replace(tmp_path, monkeypatch):
    source_dir = tmp_path / "sources"
    _write_report(source_dir, "2026-08-03", _item("Title", "Summary.", "https://example.com/item"))
    database = tmp_path / "registry.sqlite3"
    _build_current_registry(source_dir, database, tmp_path)
    _write_report(source_dir, "2026-08-10", _item("New", "New.", "https://example.com/new"))
    database_before = database.read_bytes()
    real_fingerprint = persistent._file_sha256(database)
    fingerprints = iter((real_fingerprint, "changed-by-another-writer"))
    monkeypatch.setattr(persistent, "_file_sha256", lambda _path: next(fingerprints))

    with pytest.raises(RegistryLockError, match="changed"):
        persistent.update_registry(source_dir, database, tmp_path / "backups")

    assert database.read_bytes() == database_before
    assert len(list((tmp_path / "backups").iterdir())) == 1
    assert not database.with_name(f"{database.name}.lock").exists()
