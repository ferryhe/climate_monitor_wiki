from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .audit import _insert_report, refresh_article_policy
from .errors import RegistryBuildError, RegistryInputError, RegistryLockError
from .reports import ParsedReport, parse_report_directory
from .schema import MIGRATIONS, apply_migrations

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_sidecars(database: Path) -> list[Path]:
    return [
        Path(f"{database}{suffix}")
        for suffix in ("-wal", "-shm", "-journal")
        if Path(f"{database}{suffix}").exists()
    ]


def _validate_paths(source_dir: Path, database: Path) -> tuple[Path, Path]:
    source_dir = source_dir.resolve()
    database = database.resolve()
    if not source_dir.is_dir():
        raise RegistryInputError(f"source directory does not exist: {source_dir}")
    if not database.is_file():
        raise RegistryInputError(f"registry database does not exist: {database}")
    if database == source_dir or source_dir in database.parents:
        raise RegistryInputError("registry database must be outside the read-only source directory")
    return source_dir, database


def _read_only_connection(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)


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


def _validate_database(connection: sqlite3.Connection) -> int:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < 1 or version > LATEST_SCHEMA_VERSION:
        raise RegistryInputError(f"unsupported registry schema version: {version}")
    try:
        applied = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
    except sqlite3.DatabaseError as exc:
        raise RegistryInputError("registry database is missing migration metadata") from exc
    expected = [migration[0] for migration in MIGRATIONS if migration[0] <= version]
    if applied != expected:
        raise RegistryInputError("registry schema version and migration metadata do not agree")
    if version >= 2:
        article_columns = {row[1] for row in connection.execute("PRAGMA table_info(articles)")}
        appearance_columns = {row[1] for row in connection.execute("PRAGMA table_info(report_appearances)")}
        if not {"document_kind", "publication_eligible", "exclusion_reason"} <= article_columns or not {
            "observation_status",
            "external_content_change",
        } <= appearance_columns:
            raise RegistryInputError("registry schema 2 columns are incomplete")
    if version >= 3:
        required_columns = {
            "articles": {"current_content_version_id", "display_policy"},
            "article_content_versions": {
                "content_version_id",
                "article_id",
                "content_sha256",
                "markdown_content",
                "markdown_sha256",
                "content_type",
                "source_bytes",
                "extraction_method",
                "extraction_version",
                "first_fetched_at",
            },
            "article_fetches": {
                "fetch_id",
                "article_id",
                "requested_url",
                "final_url",
                "fetched_at",
                "fetch_status",
                "http_status",
                "content_type",
                "etag",
                "last_modified",
                "error_code",
                "error_message",
                "content_version_id",
            },
            "article_enrichments": {
                "enrichment_id",
                "content_version_id",
                "status",
                "summary",
                "categories_json",
                "keywords_json",
                "language",
                "generator_kind",
                "generator_name",
                "generator_version",
                "generated_at",
                "error_code",
                "error_message",
            },
        }
        for table, expected_columns in required_columns.items():
            actual_columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not expected_columns <= actual_columns:
                raise RegistryInputError(f"registry schema 3 table is incomplete: {table}")
        required_foreign_keys = {
            "articles": {
                (
                    "article_content_versions",
                    ("current_content_version_id",),
                    ("content_version_id",),
                ),
            },
            "article_content_versions": {
                ("articles", ("article_id",), ("article_id",)),
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
        for table, expected_foreign_keys in required_foreign_keys.items():
            if not expected_foreign_keys <= _foreign_key_contracts(connection, table):
                raise RegistryInputError(f"registry schema 3 foreign keys are incomplete: {table}")
        required_triggers = {
            "articles_current_content_matches_article_insert",
            "articles_current_content_matches_article_update",
            "article_content_versions_are_immutable_update",
            "article_content_versions_are_immutable_delete",
            "article_fetches_are_append_only_update",
            "article_fetches_are_append_only_delete",
            "article_enrichments_are_append_only_update",
            "article_enrichments_are_append_only_delete",
        }
        actual_triggers = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        if not required_triggers <= actual_triggers:
            raise RegistryInputError("registry schema 3 triggers are incomplete")
        required_indexes = {
            "idx_article_fetches_article_fetched": (
                "article_fetches",
                ("article_id", "fetched_at"),
            ),
            "idx_article_fetches_content_version": (
                "article_fetches",
                ("content_version_id",),
            ),
            "idx_content_versions_article_fetched": (
                "article_content_versions",
                ("article_id", "first_fetched_at"),
            ),
            "idx_enrichments_content_generated": (
                "article_enrichments",
                ("content_version_id", "generated_at"),
            ),
        }
        for index_name, (expected_table, expected_columns) in required_indexes.items():
            index_row = connection.execute(
                "SELECT tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            actual_columns = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                    (index_name,),
                )
            )
            if index_row != (expected_table,) or actual_columns != expected_columns:
                raise RegistryInputError(f"registry schema 3 index is incomplete: {index_name}")
        invalid_pointer = connection.execute(
            """
            SELECT a.article_id
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
            raise RegistryInputError("registry article current content version has invalid ownership")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RegistryBuildError("registry database failed SQLite integrity_check")
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise RegistryBuildError("registry database failed foreign_key_check")
    return version


def _parse_sources(source_dir: Path) -> tuple[ParsedReport, ...]:
    try:
        reports = parse_report_directory(source_dir)
        if not reports:
            raise RegistryInputError(f"no climate-monitor Markdown reports found in: {source_dir}")
        return reports
    except RegistryInputError:
        raise
    except Exception as exc:
        raise RegistryBuildError(f"could not parse report history: {exc}") from exc


def plan_registry_update(source_dir: Path, database: Path) -> dict:
    """Plan an append-only update without modifying the database or sources."""

    source_dir, database = _validate_paths(source_dir, database)
    reports = _parse_sources(source_dir)
    connection = _read_only_connection(database)
    try:
        version = _validate_database(connection)
        existing = {
            row[0]: {"filename": row[1], "sha256": row[2]}
            for row in connection.execute(
                "SELECT report_date, filename, report_sha256 FROM reports ORDER BY report_date"
            )
        }
    finally:
        connection.close()

    latest_existing = max(existing, default=None)
    new_reports: list[dict] = []
    unchanged_reports: list[dict] = []
    conflicts: list[dict] = []
    source_dates = {report.report_date for report in reports}
    for report_date, stored in existing.items():
        if report_date not in source_dates:
            conflicts.append(
                {
                    "date": report_date,
                    "filename": stored["filename"],
                    "sha256": stored["sha256"],
                    "reason": "registry-report-missing-from-source",
                }
            )
    for report in reports:
        stored = existing.get(report.report_date)
        identity = {
            "date": report.report_date,
            "filename": report.path.name,
            "sha256": report.sha256,
        }
        if stored is None:
            if report.cadence != "weekly":
                conflicts.append({**identity, "reason": "new-legacy-report-requires-rebuild"})
            elif latest_existing and report.report_date <= latest_existing:
                conflicts.append({**identity, "reason": "out-of-order-history"})
            else:
                new_reports.append(identity)
        elif stored == {"filename": report.path.name, "sha256": report.sha256}:
            unchanged_reports.append(identity)
        else:
            conflicts.append(
                {
                    **identity,
                    "reason": "existing-report-identity-mismatch",
                    "stored_filename": stored["filename"],
                    "stored_sha256": stored["sha256"],
                }
            )

    pending_migrations = [migration[0] for migration in MIGRATIONS if migration[0] > version]
    return {
        "status": "plan",
        "database_schema_version": version,
        "target_schema_version": LATEST_SCHEMA_VERSION,
        "pending_migrations": pending_migrations,
        "new_reports": new_reports,
        "unchanged_report_count": len(unchanged_reports),
        "conflicts": conflicts,
        "mutation_required": bool(pending_migrations or new_reports),
    }


@contextmanager
def _exclusive_database_lock(database: Path) -> Iterator[None]:
    lock_path = database.with_name(f"{database.name}.lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RegistryLockError(f"registry update is locked: {lock_path.name}") from exc
    except OSError as exc:
        raise RegistryLockError("could not create registry update lock") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _backup_connection(source: sqlite3.Connection, destination: Path) -> None:
    destination_connection = sqlite3.connect(destination)
    try:
        source.backup(destination_connection)
    finally:
        destination_connection.close()


def _backup_name(database: Path) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{database.name}.{stamp}.bak"


def _fsync_parent(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def update_registry(source_dir: Path, database: Path, backup_dir: Path) -> dict:
    """Atomically install a migrated, append-only registry update."""

    source_dir, database = _validate_paths(source_dir, database)
    backup_dir = backup_dir.resolve()
    if backup_dir == source_dir or source_dir in backup_dir.parents:
        raise RegistryInputError("backup directory must be outside the read-only source directory")
    if backup_dir == database or backup_dir in database.parents:
        raise RegistryInputError("backup directory must not contain the live registry database")
    if backup_dir.exists() and not backup_dir.is_dir():
        raise RegistryInputError(f"backup directory path is not a directory: {backup_dir}")

    with _exclusive_database_lock(database):
        sidecars = _sqlite_sidecars(database)
        if sidecars:
            names = ", ".join(path.name for path in sidecars)
            raise RegistryInputError(f"registry has active SQLite sidecar files; reconcile before update: {names}")
        live_fingerprint = _file_sha256(database)
        plan = plan_registry_update(source_dir, database)
        if plan["conflicts"]:
            raise RegistryInputError("registry update plan contains report identity conflicts")
        if not plan["mutation_required"]:
            return {**plan, "status": "no-op", "backup": None, "imported_reports": []}

        reports_by_date = {report.report_date: report for report in _parse_sources(source_dir)}
        for item in plan["new_reports"]:
            report = reports_by_date.get(item["date"])
            if report is None or report.sha256 != item["sha256"] or report.path.name != item["filename"]:
                raise RegistryInputError("source reports changed after the update plan was created")
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RegistryBuildError("could not create the registry backup directory") from exc
        backup_path = backup_dir / _backup_name(database)
        if backup_path.exists():
            raise RegistryBuildError(f"backup destination already exists: {backup_path.name}")

        try:
            descriptor, candidate_name = tempfile.mkstemp(
                prefix=f".{database.name}.", suffix=".candidate", dir=database.parent
            )
        except OSError as exc:
            raise RegistryBuildError("could not create a candidate registry database") from exc
        os.close(descriptor)
        candidate = Path(candidate_name)
        candidate.unlink()
        backup_created = False
        try:
            source_connection = _read_only_connection(database)
            try:
                _backup_connection(source_connection, backup_path)
                backup_created = True
                _backup_connection(source_connection, candidate)
            finally:
                source_connection.close()
            backup_connection = _read_only_connection(backup_path)
            try:
                _validate_database(backup_connection)
            finally:
                backup_connection.close()
            if os.name == "posix":
                backup_path.chmod(0o600)
                shutil.copymode(database, candidate)
            _fsync_parent(backup_path)

            connection = sqlite3.connect(candidate)
            try:
                apply_migrations(connection)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    refresh_article_policy(connection)
                    for item in plan["new_reports"]:
                        _insert_report(connection, reports_by_date[item["date"]])
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                connection.execute("PRAGMA optimize")
                if _validate_database(connection) != LATEST_SCHEMA_VERSION:
                    raise RegistryBuildError("candidate registry did not reach the target schema version")
            finally:
                connection.close()

            if _sqlite_sidecars(database) or _file_sha256(database) != live_fingerprint:
                raise RegistryLockError("live registry changed while the candidate update was being prepared")
            os.replace(candidate, database)
            _fsync_parent(database)
            return {
                **plan,
                "status": "updated",
                "database_schema_version": LATEST_SCHEMA_VERSION,
                "pending_migrations": [],
                "applied_migrations": plan["pending_migrations"],
                "mutation_required": False,
                "backup": str(backup_path),
                "imported_reports": [item["date"] for item in plan["new_reports"]],
            }
        except (RegistryInputError, RegistryBuildError, RegistryLockError):
            raise
        except Exception as exc:
            raise RegistryBuildError(f"persistent registry update failed: {exc}") from exc
        finally:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
            if not backup_created:
                try:
                    backup_path.unlink()
                except FileNotFoundError:
                    pass
