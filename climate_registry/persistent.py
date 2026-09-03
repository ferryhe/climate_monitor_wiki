from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .audit import _insert_report, refresh_article_policy
from .contract import SchemaContractError, validate_registry_contract
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
        try:
            validate_registry_contract(connection)
        except SchemaContractError as exc:
            raise RegistryInputError(str(exc)) from exc
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
    descriptor: int | None = None
    locked = False
    try:
        try:
            path_metadata = os.lstat(lock_path)
        except FileNotFoundError:
            path_metadata = None
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if path_metadata is not None and (
            not stat.S_ISREG(path_metadata.st_mode)
            or stat.S_ISLNK(path_metadata.st_mode)
            or bool(getattr(path_metadata, "st_file_attributes", 0) & reparse)
        ):
            raise OSError("lock path is unsafe")
        descriptor = os.open(
            lock_path,
            os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        metadata = os.fstat(descriptor)
        opened_path_metadata = os.stat(lock_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or bool(getattr(metadata, "st_file_attributes", 0) & reparse)
            or bool(getattr(opened_path_metadata, "st_file_attributes", 0) & reparse)
            or (metadata.st_dev, metadata.st_ino)
            != (opened_path_metadata.st_dev, opened_path_metadata.st_ino)
        ):
            raise OSError("lock is not a regular file")
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif os.name == "nt":
            import msvcrt

            if metadata.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            raise OSError("unsupported lock platform")
        locked = True
        os.ftruncate(descriptor, 0)
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.fsync(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        raise RegistryLockError(f"registry update is locked: {lock_path.name}") from exc
    try:
        yield
    finally:
        if descriptor is not None:
            if locked:
                try:
                    if os.name == "posix":
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    elif os.name == "nt":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            os.close(descriptor)


def _backup_connection(source: sqlite3.Connection, destination: Path) -> None:
    destination_connection = sqlite3.connect(destination)
    try:
        source.backup(destination_connection)
    finally:
        destination_connection.close()


def initialize_registry(database: Path) -> dict:
    """Idempotently create and migrate the registry database.

    First-run bootstrap for the pipeline: plan-update/update refuse a missing
    database (fail-closed), so a fresh deployment must run this once. Existing
    databases are only validated, never modified.
    """
    database = database.resolve()
    if database.is_file():
        connection = _read_only_connection(database)
        try:
            version = _validate_database(connection)
        finally:
            connection.close()
        return {"status": "ok", "created": False, "schema_version": version}
    try:
        database.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RegistryBuildError("could not create the registry database directory") from exc
    with _exclusive_database_lock(database):
        if database.is_file():
            connection = _read_only_connection(database)
            try:
                version = _validate_database(connection)
            finally:
                connection.close()
            return {"status": "ok", "created": False, "schema_version": version}
        connection = sqlite3.connect(database)
        try:
            apply_migrations(connection)
            version = _validate_database(connection)
        finally:
            connection.close()
        if os.name == "posix":
            database.chmod(0o600)
            _fsync_parent(database)
    return {"status": "ok", "created": True, "schema_version": version}


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
