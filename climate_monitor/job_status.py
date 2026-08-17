from __future__ import annotations

import errno
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "weekly-job-status.v1"
SNAPSHOT_FILENAME = "scheduler-status.json"
JOB_SCHEDULE_HOURS = {"monitor": 8, "email": 9, "publisher": 10}
STATES = frozenset(
    {"scheduled", "running", "completed", "failed", "unknown", "not_dispatched"}
)
STATE_RESULT_CODES = {
    "failed": "execution_failed",
    "unknown": "execution_unknown",
    "not_dispatched": "not_dispatched",
}
DEFAULT_STALE_AFTER_MINUTES = 15
MAX_SNAPSHOT_BYTES = 64 * 1024
_SUPPORTS_ANCHORED_DIRECTORY_OPEN = (
    os.name == "posix"
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)

_UTC_TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"
_TOP_LEVEL_FIELDS = frozenset({"schema_version", "generated_at", "jobs"})
_BASE_JOB_FIELDS = frozenset({"scheduled_for", "state"})
_TIME_FIELDS = frozenset({"claimed_at", "started_at", "finished_at"})


class JobStatusError(RuntimeError):
    """Base class for safe scheduler-status snapshot failures."""


class JobStatusLocationError(JobStatusError):
    """The configured snapshot directory violates the external-data boundary."""


class JobStatusUnavailableError(JobStatusError):
    """The configured snapshot cannot currently be read."""


class JobStatusInvalidSnapshotError(JobStatusError):
    """The snapshot or its filesystem representation violates the contract."""


def _exact_object(value: Any, *, expected: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise JobStatusInvalidSnapshotError(f"invalid {label} fields")
    return value


def _strict_utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or len(value) != 20:
        raise JobStatusInvalidSnapshotError(f"invalid {field}")
    try:
        parsed = datetime.strptime(value, _UTC_TIMESTAMP)
    except ValueError as exc:
        raise JobStatusInvalidSnapshotError(f"invalid {field}") from exc
    if parsed.strftime(_UTC_TIMESTAMP) != value:
        raise JobStatusInvalidSnapshotError(f"invalid {field}")
    return parsed.replace(tzinfo=timezone.utc)


def _aware_utc_now(now: datetime | None) -> datetime:
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc)


def _job_fields_for_state(state: str) -> tuple[frozenset[str], frozenset[str]]:
    if state == "scheduled":
        return _BASE_JOB_FIELDS, frozenset()
    if state == "running":
        return _BASE_JOB_FIELDS | frozenset({"claimed_at"}), frozenset({"started_at"})
    if state == "completed":
        return _BASE_JOB_FIELDS | _TIME_FIELDS, frozenset()
    if state == "failed":
        return _BASE_JOB_FIELDS | _TIME_FIELDS | frozenset({"result_code"}), frozenset()
    if state == "unknown":
        required = _BASE_JOB_FIELDS | frozenset(
            {"claimed_at", "finished_at", "result_code"}
        )
        return required, frozenset({"started_at"})
    if state == "not_dispatched":
        return _BASE_JOB_FIELDS | frozenset({"result_code"}), frozenset()
    raise JobStatusInvalidSnapshotError("invalid job state")


def _validate_job(
    alias: str,
    raw: Any,
    *,
    generated_at: datetime,
) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise JobStatusInvalidSnapshotError("job must be an object")
    state = raw.get("state")
    if not isinstance(state, str) or state not in STATES:
        raise JobStatusInvalidSnapshotError("invalid job state")
    required, optional = _job_fields_for_state(state)
    allowed = required | optional
    if set(raw) - allowed or required - set(raw):
        raise JobStatusInvalidSnapshotError("invalid job fields")

    scheduled = _strict_utc(raw["scheduled_for"], field="scheduled_for")
    if scheduled.weekday() != 0 or scheduled.hour != JOB_SCHEDULE_HOURS[alias]:
        raise JobStatusInvalidSnapshotError("scheduled_for does not match the public schedule")
    if scheduled.minute or scheduled.second:
        raise JobStatusInvalidSnapshotError("scheduled_for does not match the public schedule")
    if state == "not_dispatched" and scheduled > generated_at:
        raise JobStatusInvalidSnapshotError("a future occurrence cannot be not_dispatched")

    times: dict[str, datetime] = {}
    for field in _TIME_FIELDS & set(raw):
        timestamp = _strict_utc(raw[field], field=field)
        if timestamp < scheduled or timestamp > generated_at:
            raise JobStatusInvalidSnapshotError("execution timestamp is out of bounds")
        times[field] = timestamp
    ordered = [times[field] for field in ("claimed_at", "started_at", "finished_at") if field in times]
    if ordered != sorted(ordered):
        raise JobStatusInvalidSnapshotError("execution timestamps are out of order")

    required_code = STATE_RESULT_CODES.get(state)
    if required_code is not None and raw.get("result_code") != required_code:
        raise JobStatusInvalidSnapshotError("invalid result_code")
    if required_code is None and "result_code" in raw:
        raise JobStatusInvalidSnapshotError("result_code is not allowed for this state")

    normalized = {
        "scheduled_for": scheduled.strftime(_UTC_TIMESTAMP),
        "state": state,
    }
    for field in ("claimed_at", "started_at", "finished_at"):
        if field in times:
            normalized[field] = times[field].strftime(_UTC_TIMESTAMP)
    if required_code is not None:
        normalized["result_code"] = required_code
    return normalized


def validate_snapshot(payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
    current = _aware_utc_now(now)
    top = _exact_object(payload, expected=_TOP_LEVEL_FIELDS, label="snapshot")
    if top["schema_version"] != SCHEMA_VERSION:
        raise JobStatusInvalidSnapshotError("unsupported schema_version")
    generated_at = _strict_utc(top["generated_at"], field="generated_at")
    if generated_at > current:
        raise JobStatusInvalidSnapshotError("generated_at must not be in the future")
    jobs = _exact_object(
        top["jobs"],
        expected=frozenset(JOB_SCHEDULE_HOURS),
        label="jobs",
    )
    normalized_jobs = {
        alias: _validate_job(alias, jobs[alias], generated_at=generated_at)
        for alias in JOB_SCHEDULE_HOURS
    }
    schedule_dates = {
        _strict_utc(job["scheduled_for"], field="scheduled_for").date()
        for job in normalized_jobs.values()
    }
    generated_monday = generated_at.date() - timedelta(days=generated_at.weekday())
    if schedule_dates != {generated_monday}:
        raise JobStatusInvalidSnapshotError("jobs must describe the generated week")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.strftime(_UTC_TIMESTAMP),
        "jobs": normalized_jobs,
    }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JobStatusInvalidSnapshotError("duplicate JSON key")
        result[key] = value
    return result


def decode_snapshot_json(
    raw: str | bytes,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    except UnicodeEncodeError as exc:
        raise JobStatusInvalidSnapshotError("snapshot contains invalid text") from exc
    if size > MAX_SNAPSHOT_BYTES:
        raise JobStatusInvalidSnapshotError("snapshot exceeds the file size limit")
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except JobStatusInvalidSnapshotError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise JobStatusInvalidSnapshotError("snapshot contains invalid JSON") from exc
    return validate_snapshot(payload, now=now)


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        (getattr(metadata, "st_file_attributes", 0) or 0) & reparse_flag
    )


def _configured_path_error(exc: OSError) -> JobStatusError:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return JobStatusLocationError("snapshot directory is not a valid location")
    return JobStatusUnavailableError("snapshot is unavailable")


def _existing_components(path: Path) -> list[Path]:
    current = Path(path.anchor)
    components: list[Path] = []
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _configured_path_error(exc) from exc
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise JobStatusLocationError(
                "snapshot directory contains a non-directory component"
            )
        components.append(current)
    return components


def _external_directory(configured: str | Path, *, repository_root: str | Path) -> Path:
    path = Path(configured)
    if not path.is_absolute():
        raise JobStatusLocationError("snapshot directory must be absolute")
    if any(part == os.pardir for part in path.parts):
        raise JobStatusLocationError("snapshot directory must not contain parent traversal")
    for component in _existing_components(path):
        try:
            metadata = os.lstat(component)
        except OSError as exc:
            raise _configured_path_error(exc) from exc
        if _metadata_is_link_or_reparse(metadata):
            raise JobStatusLocationError("snapshot directory must not contain links")
    try:
        resolved = path.resolve(strict=False)
        repository = Path(repository_root).resolve(strict=False)
    except RuntimeError as exc:
        raise JobStatusLocationError("snapshot directory cannot be resolved") from exc
    except OSError as exc:
        raise _configured_path_error(exc) from exc
    try:
        resolved.relative_to(repository)
    except ValueError:
        pass
    else:
        raise JobStatusLocationError("snapshot directory must be outside the repository")
    try:
        metadata = os.lstat(resolved)
    except FileNotFoundError as exc:
        raise JobStatusUnavailableError("snapshot is unavailable") from exc
    except OSError as exc:
        raise _configured_path_error(exc) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise JobStatusLocationError("snapshot location must be a directory")
    return resolved


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _preopen_snapshot_error(exc: OSError) -> JobStatusError:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return JobStatusInvalidSnapshotError("snapshot path is invalid")
    return JobStatusUnavailableError("snapshot is unavailable")


def _snapshot_directory_metadata(
    path: Path,
    *,
    post_open: bool = False,
) -> os.stat_result:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        if post_open:
            raise JobStatusInvalidSnapshotError(
                "snapshot directory changed while reading"
            ) from exc
        raise _preopen_snapshot_error(exc) from exc
    if _metadata_is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise JobStatusInvalidSnapshotError("snapshot directory changed while reading")
    return metadata


def _raw_relative_metadata(directory_descriptor: int, filename: str) -> os.stat_result:
    return os.stat(
        filename,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )


def _relative_metadata(directory_descriptor: int, filename: str) -> os.stat_result:
    try:
        return _raw_relative_metadata(directory_descriptor, filename)
    except OSError as exc:
        raise _preopen_snapshot_error(exc) from exc


def _open_prevalidated(
    path: str | Path,
    flags: int,
    *,
    directory_descriptor: int | None = None,
) -> int:
    try:
        if directory_descriptor is None:
            return os.open(path, flags)
        return os.open(path, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise _preopen_snapshot_error(exc) from exc


def _read_snapshot_file(
    directory: Path,
    *,
    filename: str = SNAPSHOT_FILENAME,
) -> bytes:
    before_directory = _snapshot_directory_metadata(directory)
    anchored = _SUPPORTS_ANCHORED_DIRECTORY_OPEN
    directory_descriptor: int | None = None
    descriptor: int | None = None

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if os.name == "posix":
        flags |= os.O_NONBLOCK
    try:
        if anchored:
            directory_descriptor = _open_prevalidated(directory, directory_flags)
            opened_directory = os.fstat(directory_descriptor)
            if not stat.S_ISDIR(opened_directory.st_mode) or _identity(
                opened_directory
            ) != _identity(before_directory):
                raise JobStatusInvalidSnapshotError(
                    "snapshot directory changed while reading"
                )
            if _identity(
                _snapshot_directory_metadata(directory, post_open=True)
            ) != _identity(
                opened_directory
            ):
                raise JobStatusInvalidSnapshotError(
                    "snapshot directory changed while reading"
                )
            before_path = _relative_metadata(directory_descriptor, filename)
            if _metadata_is_link_or_reparse(before_path) or not stat.S_ISREG(
                before_path.st_mode
            ):
                raise JobStatusInvalidSnapshotError("snapshot must be a regular file")
            descriptor = _open_prevalidated(
                filename,
                flags,
                directory_descriptor=directory_descriptor,
            )
        else:
            snapshot_path = directory / filename
            try:
                before_path = os.lstat(snapshot_path)
            except OSError as exc:
                raise _preopen_snapshot_error(exc) from exc
            if _metadata_is_link_or_reparse(before_path) or not stat.S_ISREG(
                before_path.st_mode
            ):
                raise JobStatusInvalidSnapshotError("snapshot must be a regular file")
            descriptor = _open_prevalidated(snapshot_path, flags)

        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise JobStatusInvalidSnapshotError("snapshot must be a regular file")
        if _identity(before) != _identity(before_path):
            raise JobStatusInvalidSnapshotError("snapshot changed while reading")

        opened_parent = (
            os.fstat(directory_descriptor) if directory_descriptor is not None else before_directory
        )
        if _identity(
            _snapshot_directory_metadata(directory, post_open=True)
        ) != _identity(opened_parent):
            raise JobStatusInvalidSnapshotError("snapshot directory changed while reading")

        chunks: list[bytes] = []
        remaining = MAX_SNAPSHOT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise JobStatusInvalidSnapshotError("snapshot exceeds the file size limit")

        after = os.fstat(descriptor)
        if directory_descriptor is not None:
            try:
                after_path = os.stat(
                    filename,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise JobStatusInvalidSnapshotError("snapshot changed while reading") from exc
        else:
            try:
                after_path = os.lstat(directory / filename)
            except OSError as exc:
                raise JobStatusInvalidSnapshotError("snapshot changed while reading") from exc
        identities = {
            _identity(before),
            _identity(after),
            _identity(after_path),
        }
        before_change = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        after_change = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if len(identities) != 1 or before_change != after_change:
            raise JobStatusInvalidSnapshotError("snapshot changed while reading")
        if after.st_size != len(raw) or _metadata_is_link_or_reparse(after_path):
            raise JobStatusInvalidSnapshotError("snapshot changed while reading")
        final_parent = (
            os.fstat(directory_descriptor) if directory_descriptor is not None else before_directory
        )
        if _identity(
            _snapshot_directory_metadata(directory, post_open=True)
        ) != _identity(final_parent):
            raise JobStatusInvalidSnapshotError("snapshot directory changed while reading")
        return raw
    except JobStatusError:
        raise
    except OSError as exc:
        raise JobStatusUnavailableError("snapshot is unavailable") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass


class JobStatusSnapshotReader:
    def __init__(self, directory: str | Path, *, repository_root: str | Path):
        self.repository_root = Path(repository_root)
        self.directory = _external_directory(
            directory,
            repository_root=self.repository_root,
        )

    def status(
        self,
        *,
        now: datetime | None = None,
        stale_after: timedelta = timedelta(minutes=DEFAULT_STALE_AFTER_MINUTES),
    ) -> dict[str, Any]:
        if stale_after < timedelta(0):
            raise ValueError("stale_after must not be negative")
        current = _aware_utc_now(now)
        directory = _external_directory(
            self.directory,
            repository_root=self.repository_root,
        )
        snapshot = decode_snapshot_json(
            _read_snapshot_file(directory),
            now=current,
        )
        generated_at = _strict_utc(snapshot["generated_at"], field="generated_at")
        age = current - generated_at
        age_seconds = int(age.total_seconds())
        max_age_seconds = int(stale_after.total_seconds())
        return {
            "available": True,
            **snapshot,
            "observer": {
                "is_stale": age_seconds > max_age_seconds,
                "age_seconds": age_seconds,
                "max_age_seconds": max_age_seconds,
            },
        }
