from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from .report_artifact_identity import (
    ArtifactIdentity,
    ArtifactIdentityError,
    validate_report_artifact_identity,
)
from .run_ledger import (
    LedgerError,
    MAX_ATTEMPT_BYTES,
    RunLedgerReader,
    SUCCESS_STATUSES,
    _open_directory_chain,
    append_attempt_repair,
    build_report_identity,
    canonical_report_filename,
    decode_attempt_json,
    read_bounded_file,
    remove_attempt_repair,
    validate_report_identity,
)


MAX_REPORT_BYTES = 8 * 1024 * 1024
DEFAULT_PUBLISHER_LOCK = Path("/tmp/climate-monitor-weekly-publisher.lock")
_REPORT_DATE = re.compile(
    rb"^\s*\*\*Report Date:\*\*\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE
)


class RepairPreflightError(RuntimeError):
    """The exact-date repair cannot be safely planned."""


class RepairValidationError(RuntimeError):
    """Available identities conflict or an atomic apply could not be verified."""


class RepairLockConflict(RuntimeError):
    """The Publisher coordination lock is already held."""


@dataclass(frozen=True)
class _RepairPlan:
    repository_root: Path
    ledger_dir: Path
    attempt: dict[str, Any]
    original_path: Path
    claim_path: Path
    original_raw: bytes
    report: dict[str, str]
    registry_report_sha256: str | None
    artifact: ArtifactIdentity
    already_valid: bool


def _is_link_or_reparse(path: Path) -> bool:
    metadata = os.lstat(path)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse
    )


def _canonical_absolute(path: str | Path, *, label: str, must_exist: bool = True) -> Path:
    raw = Path(path)
    if not raw.is_absolute():
        raise RepairPreflightError(f"{label} must be absolute")
    try:
        resolved = raw.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise RepairPreflightError(f"{label} is unavailable") from exc
    if resolved != raw:
        raise RepairPreflightError(f"{label} is not canonical")
    current = Path(raw.anchor)
    for part in raw.parts[1:]:
        current /= part
        try:
            os.lstat(current)
        except FileNotFoundError:
            if must_exist:
                raise RepairPreflightError(f"{label} is unavailable")
            continue
        except OSError as exc:
            raise RepairPreflightError(f"{label} is unavailable") from exc
        if _is_link_or_reparse(current):
            raise RepairPreflightError(f"{label} contains a link or reparse point")
    return resolved


def _outside_repository(path: Path, repository_root: Path, *, label: str) -> None:
    try:
        path.relative_to(repository_root)
    except ValueError:
        return
    raise RepairPreflightError(f"{label} must be outside the repository")


def _read_regular(path: Path, *, limit: int, label: str) -> bytes:
    try:
        return read_bounded_file(path, max_bytes=limit)
    except LedgerError as exc:
        raise RepairPreflightError(f"{label} is unavailable") from exc


def _git_text(repository_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RepairPreflightError("source checkout could not be verified")
    return result.stdout.strip()


def _git_bytes(repository_root: Path, *args: str, limit: int) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repository_root,
        capture_output=True,
        check=False,
    )
    if result.returncode or len(result.stdout) > limit:
        raise RepairPreflightError("source checkout could not be verified")
    return result.stdout


def _source_identity(source_dir: Path, target_date: str) -> tuple[Path, Path, bytes, dict[str, str]]:
    source_dir = _canonical_absolute(source_dir, label="source directory")
    if not source_dir.is_dir():
        raise RepairPreflightError("source directory is invalid")
    repository_text = _git_text(source_dir, "rev-parse", "--show-toplevel")
    repository_root = _canonical_absolute(
        Path(repository_text), label="source repository"
    )
    try:
        source_dir.relative_to(repository_root)
    except ValueError as exc:
        raise RepairPreflightError("source directory escapes its checkout") from exc
    if _git_text(repository_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RepairPreflightError("source checkout is not clean")

    filename = canonical_report_filename(target_date)
    source_path = _canonical_absolute(
        source_dir / filename, label="canonical source"
    )
    relative = source_path.relative_to(repository_root).as_posix()
    if _git_text(repository_root, "ls-files", "--error-unmatch", "--", relative) != relative:
        raise RepairPreflightError("canonical source is not tracked at HEAD")
    raw = _read_regular(source_path, limit=MAX_REPORT_BYTES, label="canonical source")
    tracked_raw = _git_bytes(
        repository_root, "show", f"HEAD:{relative}", limit=MAX_REPORT_BYTES
    )
    if tracked_raw != raw:
        raise RepairPreflightError("canonical source does not match HEAD raw bytes")
    if _git_text(repository_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RepairPreflightError("source checkout changed during verification")
    matches = _REPORT_DATE.findall(raw)
    if len(matches) != 1 or matches[0].decode("ascii") != target_date:
        raise RepairValidationError("canonical source report date is invalid")
    identity = build_report_identity(
        report_date=target_date,
        filename=filename,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    return repository_root, source_path, raw, identity.as_record()


def _validate_registry(
    database: Path,
    *,
    repository_root: Path,
    target_date: str,
    expected_report: dict[str, str],
) -> str | None:
    database = _canonical_absolute(database, label="Registry database")
    _outside_repository(database, repository_root, label="Registry database")
    if not stat.S_ISREG(os.lstat(database).st_mode):
        raise RepairPreflightError("Registry database is not a regular file")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{database}{suffix}")
        try:
            os.lstat(sidecar)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RepairPreflightError("Registry database sidecar is unsafe") from exc
        raise RepairPreflightError("Registry database has an active sidecar")
    parent_descriptor: int | None = None
    database_descriptor: int | None = None
    before: os.stat_result | None = None
    try:
        database_uri = f"{database.as_uri()}?mode=ro&immutable=1"
        if os.name == "posix":
            parent_descriptor = _open_directory_chain(database.parent)
            path_metadata = os.stat(
                database.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            database_descriptor = os.open(
                database.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_descriptor,
            )
            before = os.fstat(database_descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or (before.st_dev, before.st_ino)
                != (path_metadata.st_dev, path_metadata.st_ino)
            ):
                raise RepairPreflightError("Registry database changed while opening")
            descriptor_path = Path(f"/proc/self/fd/{database_descriptor}")
            if not descriptor_path.exists():
                descriptor_path = Path(f"/dev/fd/{database_descriptor}")
            database_uri = f"{descriptor_path.as_uri()}?mode=ro&immutable=1"
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {2, 3, 4}:
            raise RepairPreflightError("Registry database schema is unsupported")
        rows = connection.execute(
            "SELECT report_id, report_date, filename, report_sha256 "
            "FROM reports WHERE report_date = ?",
            (target_date,),
        ).fetchall()
        if before is not None and parent_descriptor is not None and database_descriptor is not None:
            after = os.fstat(database_descriptor)
            after_path = os.stat(
                database.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            identities = {
                (before.st_dev, before.st_ino),
                (after.st_dev, after.st_ino),
                (after_path.st_dev, after_path.st_ino),
            }
            before_change = (
                before.st_size,
                getattr(before, "st_mtime_ns", None),
                getattr(before, "st_ctime_ns", None),
            )
            after_change = (
                after.st_size,
                getattr(after, "st_mtime_ns", None),
                getattr(after, "st_ctime_ns", None),
            )
            if len(identities) != 1 or before_change != after_change:
                raise RepairPreflightError("Registry database changed while reading")
    except RepairPreflightError:
        raise
    except (LedgerError, sqlite3.DatabaseError, OSError) as exc:
        raise RepairPreflightError("Registry report identity is unavailable") from exc
    finally:
        if "connection" in locals():
            connection.close()
        if database_descriptor is not None:
            os.close(database_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)
    if not rows:
        return None
    if len(rows) != 1:
        raise RepairValidationError("Registry report identity is ambiguous")
    row = rows[0]
    if (
        row["report_id"] != f"report-{target_date}"
        or row["report_date"] != target_date
        or row["filename"] != canonical_report_filename(target_date)
        or row["report_sha256"] != expected_report["sha256"]
    ):
        raise RepairValidationError("Registry report identity does not match source")
    return row["report_sha256"]


def _original_snapshot(path: Path) -> tuple[int, ...]:
    metadata = os.lstat(path)
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        getattr(metadata, "st_mtime_ns", 0),
        getattr(metadata, "st_ctime_ns", 0),
        metadata.st_nlink,
    )


def _validate_private_provenance(
    *,
    reader: RunLedgerReader,
    original_path: Path,
    claim_path: Path,
) -> None:
    root_metadata = os.lstat(reader.root)
    directory_paths = (
        reader.root,
        reader.root / "attempts",
        reader.root / "attempts" / "publisher",
        original_path.parent,
        reader.root / ".attempt-identities",
    )
    for directory in directory_paths:
        metadata = os.lstat(directory)
        if _is_link_or_reparse(directory) or not stat.S_ISDIR(metadata.st_mode):
            raise RepairValidationError("Publisher ledger provenance is unsafe")
        if os.name == "posix" and (
            metadata.st_uid != root_metadata.st_uid
            or metadata.st_gid != root_metadata.st_gid
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise RepairValidationError("Publisher ledger permissions are unsafe")
    original = os.lstat(original_path)
    claim = os.lstat(claim_path)
    for metadata in (original, claim):
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 2:
            raise RepairValidationError("Publisher attempt link topology is unsafe")
        if os.name == "posix" and (
            metadata.st_uid != root_metadata.st_uid
            or metadata.st_gid != root_metadata.st_gid
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise RepairValidationError("Publisher attempt permissions are unsafe")
    if (
        original.st_uid,
        original.st_gid,
        stat.S_IMODE(original.st_mode),
    ) != (
        claim.st_uid,
        claim.st_gid,
        stat.S_IMODE(claim.st_mode),
    ):
        raise RepairValidationError("Publisher attempt metadata does not match claim")


def _plan(
    *,
    target_date: str,
    source_dir: Path,
    database: Path,
    artifact_root: Path,
    ledger_dir: Path,
) -> _RepairPlan:
    try:
        parsed = date.fromisoformat(target_date)
    except (TypeError, ValueError) as exc:
        raise RepairPreflightError("target date is invalid") from exc
    if parsed.isoformat() != target_date or parsed.weekday() != 0:
        raise RepairPreflightError("target date must be an exact Monday")
    repository_root, _source_path, _raw, report = _source_identity(
        source_dir, target_date
    )
    registry_report_sha256 = _validate_registry(
        database,
        repository_root=repository_root,
        target_date=target_date,
        expected_report=report,
    )
    artifact_root = _canonical_absolute(artifact_root, label="artifact root")
    _outside_repository(artifact_root, repository_root, label="artifact root")
    try:
        artifact = validate_report_artifact_identity(
            artifact_root,
            report_date=target_date,
            report_filename=canonical_report_filename(target_date),
            report_sha256=report["sha256"],
        )
    except (ArtifactIdentityError, OSError) as exc:
        raise RepairValidationError("delivery artifact identity does not match source") from exc

    try:
        reader = RunLedgerReader(ledger_dir, repository_root=repository_root)
        attempts = reader._load()
    except LedgerError as exc:
        raise RepairValidationError("Publisher ledger is invalid") from exc
    matching = [
        attempt
        for attempt in attempts
        if attempt["stage"] == "publisher" and attempt["report_date"] == target_date
    ]
    if not matching:
        raise RepairPreflightError("Publisher attempt is missing")
    attempt = matching[-1]
    if attempt["status"] not in SUCCESS_STATUSES:
        raise RepairPreflightError("latest Publisher attempt is not successful")
    existing_report = attempt.get("report")
    if existing_report is not None:
        try:
            validate_report_identity(
                existing_report,
                expected_report_date=target_date,
                expected_filename=canonical_report_filename(target_date),
                expected_sha256=report["sha256"],
            )
        except LedgerError as exc:
            raise RepairValidationError(
                "Publisher report identity does not match source"
            ) from exc
        return _RepairPlan(
            repository_root=repository_root,
            ledger_dir=reader.root,
            attempt=attempt,
            original_path=Path(),
            claim_path=Path(),
            original_raw=b"",
            report=report,
            registry_report_sha256=registry_report_sha256,
            artifact=artifact,
            already_valid=True,
        )

    original_path = (
        reader.root
        / "attempts"
        / "publisher"
        / target_date
        / f"{attempt['attempt_id']}.json"
    )
    claim_path = reader.root / ".attempt-identities" / f"{attempt['attempt_id']}.claim"
    try:
        original_raw = read_bounded_file(
            original_path, max_bytes=MAX_ATTEMPT_BYTES
        )
        claim_raw = read_bounded_file(claim_path, max_bytes=MAX_ATTEMPT_BYTES)
        original_record = decode_attempt_json(original_raw)
    except LedgerError as exc:
        raise RepairValidationError("original Publisher attempt is invalid") from exc
    original_stat = os.lstat(original_path)
    claim_stat = os.lstat(claim_path)
    _validate_private_provenance(
        reader=reader,
        original_path=original_path,
        claim_path=claim_path,
    )
    if (
        (original_stat.st_dev, original_stat.st_ino)
        != (claim_stat.st_dev, claim_stat.st_ino)
        or original_raw != claim_raw
        or original_record != attempt
        or "report" in original_record
    ):
        raise RepairValidationError("original Publisher attempt claim does not match")
    return _RepairPlan(
        repository_root=repository_root,
        ledger_dir=reader.root,
        attempt=attempt,
        original_path=original_path,
        claim_path=claim_path,
        original_raw=original_raw,
        report=report,
        registry_report_sha256=registry_report_sha256,
        artifact=artifact,
        already_valid=False,
    )


@contextmanager
def publisher_lock(lock_file: Path) -> Iterator[None]:
    expected_raw = Path(
        os.environ.get("CLIMATE_PUBLISH_LOCK", str(DEFAULT_PUBLISHER_LOCK))
    )
    expected = _canonical_absolute(expected_raw, label="configured Publisher lock")
    lock_file = _canonical_absolute(lock_file, label="Publisher lock")
    if lock_file != expected:
        raise RepairPreflightError("Publisher lock does not match configured lock")
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(lock_file, flags)
        opened = os.fstat(descriptor)
        path_metadata = os.lstat(lock_file)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_link_or_reparse(lock_file)
            or (opened.st_dev, opened.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            raise RepairPreflightError("Publisher lock changed while opening")
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RepairLockConflict("Publisher lock is held") from exc
        else:
            import msvcrt

            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RepairLockConflict("Publisher lock is held") from exc
        locked_path = os.lstat(lock_file)
        if (opened.st_dev, opened.st_ino) != (
            locked_path.st_dev,
            locked_path.st_ino,
        ) or _is_link_or_reparse(lock_file):
            raise RepairPreflightError("Publisher lock changed while locking")
        yield
    except RepairLockConflict:
        raise
    except OSError as exc:
        raise RepairPreflightError("Publisher lock is unavailable") from exc
    finally:
        if descriptor is not None:
            if os.name == "posix":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
            else:
                import msvcrt

                try:
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            os.close(descriptor)


def repair_publisher_ledger(
    *,
    target_date: str,
    source_dir: Path,
    database: Path,
    artifact_root: Path,
    ledger_dir: Path,
    lock_file: Path,
    apply: bool,
) -> dict[str, Any]:
    with publisher_lock(lock_file):
        plan = _plan(
            target_date=target_date,
            source_dir=source_dir,
            database=database,
            artifact_root=artifact_root,
            ledger_dir=ledger_dir,
        )
        evidence = {
            "report_date": target_date,
            "report_sha256": plan.report["sha256"],
            "source_report_sha256": plan.report["sha256"],
            "registry_report_sha256": plan.registry_report_sha256,
            "artifact_report_sha256": plan.report["sha256"],
            "artifact_directory": str(plan.artifact.artifact_dir),
            "attempt_id": plan.attempt["attempt_id"],
            "artifact_manifest_sha256": plan.artifact.manifest_sha256,
            "artifact_summary_sha256": plan.artifact.summary_sha256,
            "artifact_pdf_sha256": plan.artifact.pdf_sha256,
        }
        if plan.already_valid:
            return {"status": "already_valid", **evidence}
        if not apply:
            return {"status": "would_repair", **evidence}

        revalidated = _plan(
            target_date=target_date,
            source_dir=source_dir,
            database=database,
            artifact_root=artifact_root,
            ledger_dir=ledger_dir,
        )
        if (
            revalidated.already_valid
            or revalidated.attempt != plan.attempt
            or revalidated.original_raw != plan.original_raw
            or revalidated.report != plan.report
            or revalidated.registry_report_sha256 != plan.registry_report_sha256
            or revalidated.artifact != plan.artifact
            or _original_snapshot(revalidated.original_path)
            != _original_snapshot(plan.original_path)
            or _original_snapshot(revalidated.claim_path)
            != _original_snapshot(plan.claim_path)
        ):
            raise RepairValidationError("repair evidence changed before apply")
        plan = revalidated

        original_before = _original_snapshot(plan.original_path)
        claim_before = _original_snapshot(plan.claim_path)
        payload = {
            "schema_version": "weekly-run-attempt-repair.v1",
            "attempt_id": plan.attempt["attempt_id"],
            "original_sha256": hashlib.sha256(plan.original_raw).hexdigest(),
            "report": plan.report,
        }
        created = ""

        def rollback_created_overlay() -> None:
            if created != "created":
                return
            try:
                remove_attempt_repair(
                    plan.ledger_dir,
                    payload,
                    repository_root=plan.repository_root,
                )
            except (LedgerError, OSError) as rollback_error:
                raise RepairValidationError(
                    "repair was applied but rollback requires manual recovery"
                ) from rollback_error

        try:
            created = append_attempt_repair(
                plan.ledger_dir,
                payload,
                repository_root=plan.repository_root,
            )
            attempts = RunLedgerReader(
                plan.ledger_dir, repository_root=plan.repository_root
            )._load()
        except LedgerError as exc:
            rollback_created_overlay()
            raise RepairValidationError("Publisher ledger repair failed") from exc
        try:
            repaired = next(
                (
                    attempt
                    for attempt in attempts
                    if attempt["attempt_id"] == plan.attempt["attempt_id"]
                ),
                None,
            )
            verification_failed = (
                created not in {"created", "already_exists"}
                or repaired is None
                or repaired.get("report") != plan.report
                or _original_snapshot(plan.original_path) != original_before
                or _original_snapshot(plan.claim_path) != claim_before
                or read_bounded_file(
                    plan.original_path, max_bytes=MAX_ATTEMPT_BYTES
                )
                != plan.original_raw
                or read_bounded_file(
                    plan.claim_path, max_bytes=MAX_ATTEMPT_BYTES
                )
                != plan.original_raw
            )
        except (LedgerError, OSError) as exc:
            rollback_created_overlay()
            raise RepairValidationError(
                "Publisher ledger repair verification failed"
            ) from exc
        if verification_failed:
            rollback_created_overlay()
            raise RepairValidationError("Publisher ledger repair verification failed")
        return {"status": "repaired", **evidence}
