from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "weekly-run-attempt.v1"
REPAIR_SCHEMA_VERSION = "weekly-run-attempt-repair.v1"
STAGES = frozenset({"monitor", "publisher"})
STATUSES = frozenset({"success", "partial", "failed", "no_change"})
SUCCESS_STATUSES = frozenset({"success", "no_change"})
DEFAULT_STALE_AFTER_HOURS = 192
MAX_ATTEMPT_BYTES = 128 * 1024
MAX_ATTEMPT_COUNT = 20_000
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_VISITED_ENTRIES = 20_000
MAX_VISITED_DIRS = 6_000
MAX_REPAIR_BYTES = 32 * 1024

_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_ATTEMPT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_ATTEMPT_FIELDS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "stage",
        "report_date",
        "scheduled_for",
        "finished_at",
        "status",
        "result_code",
        "report",
        "registry_revision",
        "sources",
    }
)
_REQUIRED_ATTEMPT_FIELDS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "stage",
        "report_date",
        "scheduled_for",
        "finished_at",
        "status",
        "result_code",
    }
)


class LedgerError(RuntimeError):
    """Base class for safe weekly-run-ledger failures."""


class LedgerLocationError(LedgerError):
    """The configured ledger violates the external-directory boundary."""


class LedgerUnavailableError(LedgerError):
    """The configured ledger cannot currently be read or written."""


class LedgerContractError(LedgerError):
    """A run attempt or ledger layout violates the public contract."""


class LedgerConflictError(LedgerError):
    """An attempt identity already exists with different content."""


@dataclass(frozen=True)
class ReportIdentity:
    report_id: str
    report_date: str
    filename: str
    sha256: str

    def as_record(self) -> dict[str, str]:
        return {
            "report_id": self.report_id,
            "report_date": self.report_date,
            "sha256": self.sha256,
        }


def _exact_fields(
    payload: Mapping[str, Any],
    *,
    expected: frozenset[str],
    required: frozenset[str] | None = None,
    label: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise LedgerContractError(f"{label} must be an object")
    unexpected = set(payload) - expected
    if unexpected:
        raise LedgerContractError(f"unexpected {label} fields")
    missing = (required if required is not None else expected) - set(payload)
    if missing:
        raise LedgerContractError(f"missing {label} fields")


def _safe_token(value: Any, *, field: str, attempt_id: bool = False) -> str:
    pattern = _ATTEMPT_ID if attempt_id else _TOKEN
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise LedgerContractError(f"invalid {field}")
    return value


def _strict_date(value: Any, *, field: str) -> date:
    if not isinstance(value, str):
        raise LedgerContractError(f"invalid {field}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise LedgerContractError(f"invalid {field}") from exc
    if parsed.isoformat() != value:
        raise LedgerContractError(f"invalid {field}")
    return parsed


def _strict_utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise LedgerContractError(f"{field} must be strict UTC Z")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise LedgerContractError(f"{field} must be strict UTC Z") from exc
    return parsed.replace(tzinfo=timezone.utc)


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LedgerContractError(f"invalid {field}")
    return value


def canonical_report_filename(report_date: str) -> str:
    parsed = _strict_date(report_date, field="report report_date")
    if parsed.weekday() != 0:
        raise LedgerContractError("report report_date must be Monday")
    return f"climate-monitor-{report_date}.md"


def build_report_identity(
    *, report_date: str, filename: str, sha256: str
) -> ReportIdentity:
    expected_filename = canonical_report_filename(report_date)
    if filename != expected_filename:
        raise LedgerContractError("report filename does not match report_date")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise LedgerContractError("invalid report sha256")
    return ReportIdentity(
        report_id=expected_filename.removesuffix(".md"),
        report_date=report_date,
        filename=expected_filename,
        sha256=sha256,
    )


def validate_report_identity(
    raw: Any,
    *,
    expected_report_date: str | None = None,
    expected_filename: str | None = None,
    expected_sha256: str | None = None,
) -> ReportIdentity:
    expected = frozenset({"report_id", "report_date", "sha256"})
    _exact_fields(raw, expected=expected, label="report")
    report_id = _safe_token(raw["report_id"], field="report_id", attempt_id=True)
    identity = build_report_identity(
        report_date=raw["report_date"],
        filename=f"{report_id}.md",
        sha256=raw["sha256"],
    )
    if expected_report_date is not None and identity.report_date != expected_report_date:
        raise LedgerContractError("report report_date does not match attempt report_date")
    if expected_filename is not None and identity.filename != expected_filename:
        raise LedgerContractError("report filename does not match expected filename")
    if expected_sha256 is not None and identity.sha256 != expected_sha256:
        raise LedgerContractError("report sha256 does not match expected sha256")
    return identity


def _validate_report(raw: Any, *, report_date: str) -> dict[str, str]:
    # Keep the published v1 wire contract backward compatible. Historically,
    # report_id was any safe token; Publisher-specific consumers opt into the
    # stricter canonical filename relationship via validate_report_identity().
    expected = frozenset({"report_id", "report_date", "sha256"})
    _exact_fields(raw, expected=expected, label="report")
    report_id = _safe_token(raw["report_id"], field="report_id", attempt_id=True)
    nested_date = _strict_date(raw["report_date"], field="report report_date")
    if nested_date.isoformat() != report_date:
        raise LedgerContractError("report report_date does not match attempt report_date")
    sha256 = raw["sha256"]
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise LedgerContractError("invalid report sha256")
    return {
        "report_id": report_id,
        "report_date": nested_date.isoformat(),
        "sha256": sha256,
    }


def _validate_revision(raw: Any) -> dict[str, str]:
    expected = frozenset({"namespace", "revision"})
    _exact_fields(raw, expected=expected, label="registry revision")
    return {
        "namespace": _safe_token(raw["namespace"], field="registry namespace"),
        "revision": _safe_token(raw["revision"], field="registry revision"),
    }


def _validate_sources(raw: Any) -> dict[str, Any]:
    expected = frozenset(
        {"total", "updated", "unchanged", "failed", "blocked", "failures"}
    )
    _exact_fields(raw, expected=expected, label="sources")
    counts = {
        name: _nonnegative_int(raw[name], field=f"sources {name}")
        for name in ("total", "updated", "unchanged", "failed", "blocked")
    }
    if sum(counts[name] for name in ("updated", "unchanged", "failed", "blocked")) != counts[
        "total"
    ]:
        raise LedgerContractError("source counts must sum exactly to total")

    failures = raw["failures"]
    if not isinstance(failures, list):
        raise LedgerContractError("sources failures must be a list")
    normalized_failures: list[dict[str, str]] = []
    seen_sources: set[str] = set()
    failure_status_counts = {"failed": 0, "blocked": 0}
    expected_failure_fields = frozenset({"source_id", "status", "error_code"})
    for failure in failures:
        _exact_fields(
            failure,
            expected=expected_failure_fields,
            label="source failure",
        )
        source_id = _safe_token(failure["source_id"], field="source_id")
        if source_id in seen_sources:
            raise LedgerContractError("duplicate source failure")
        seen_sources.add(source_id)
        status = failure["status"]
        if not isinstance(status, str) or status not in failure_status_counts:
            raise LedgerContractError("invalid source failure status")
        error_code = _safe_token(failure["error_code"], field="source error_code")
        failure_status_counts[status] += 1
        normalized_failures.append(
            {"source_id": source_id, "status": status, "error_code": error_code}
        )
    if (
        failure_status_counts["failed"] != counts["failed"]
        or failure_status_counts["blocked"] != counts["blocked"]
    ):
        raise LedgerContractError("source failure counts do not match failures")
    normalized_failures.sort(key=lambda item: (item["status"], item["source_id"], item["error_code"]))
    return {**counts, "failures": normalized_failures}


def validate_attempt(payload: Any) -> dict[str, Any]:
    _exact_fields(
        payload,
        expected=_ATTEMPT_FIELDS,
        required=_REQUIRED_ATTEMPT_FIELDS,
        label="attempt",
    )
    if payload["schema_version"] != SCHEMA_VERSION:
        raise LedgerContractError("unsupported schema_version")
    attempt_id = _safe_token(payload["attempt_id"], field="attempt_id", attempt_id=True)
    stage = payload["stage"]
    if not isinstance(stage, str) or stage not in STAGES:
        raise LedgerContractError("invalid stage")
    report_day = _strict_date(payload["report_date"], field="report_date")
    if report_day.weekday() != 0:
        raise LedgerContractError("report_date must be Monday")
    scheduled = _strict_utc(payload["scheduled_for"], field="scheduled_for")
    finished = _strict_utc(payload["finished_at"], field="finished_at")
    if scheduled.date() != report_day:
        raise LedgerContractError("scheduled_for date must match report_date")
    if finished < scheduled:
        raise LedgerContractError("finished_at must not precede scheduled_for")
    status = payload["status"]
    if not isinstance(status, str) or status not in STATUSES:
        raise LedgerContractError("invalid status")
    result_code = _safe_token(payload["result_code"], field="result_code")

    normalized: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "stage": stage,
        "report_date": report_day.isoformat(),
        "scheduled_for": scheduled.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "result_code": result_code,
    }
    if "report" in payload:
        normalized["report"] = _validate_report(
            payload["report"], report_date=report_day.isoformat()
        )
    if "registry_revision" in payload:
        normalized["registry_revision"] = _validate_revision(payload["registry_revision"])
    if "sources" in payload:
        sources = _validate_sources(payload["sources"])
        if status == "success" and (sources["failed"] or sources["blocked"]):
            raise LedgerContractError("success source evidence cannot contain failures")
        if status == "no_change" and (
            sources["updated"] or sources["failed"] or sources["blocked"]
        ):
            raise LedgerContractError("no_change source evidence must all be unchanged")
        if status == "partial" and not (
            sources["updated"] + sources["unchanged"] > 0
            and sources["failed"] + sources["blocked"] > 0
        ):
            raise LedgerContractError("partial source evidence must contain mixed outcomes")
        if status == "failed" and (
            sources["total"] == 0
            or sources["updated"]
            or sources["unchanged"]
            or sources["failed"] + sources["blocked"] != sources["total"]
        ):
            raise LedgerContractError("failed source evidence must contain no successes")
        normalized["sources"] = sources
    return normalized


def canonical_attempt_bytes(payload: Any) -> bytes:
    normalized = validate_attempt(payload)
    return (
        json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def validate_attempt_repair(payload: Any) -> dict[str, Any]:
    expected = frozenset(
        {"schema_version", "attempt_id", "original_sha256", "report"}
    )
    _exact_fields(payload, expected=expected, label="attempt repair")
    if payload["schema_version"] != REPAIR_SCHEMA_VERSION:
        raise LedgerContractError("unsupported repair schema_version")
    attempt_id = _safe_token(
        payload["attempt_id"], field="attempt_id", attempt_id=True
    )
    original_sha256 = payload["original_sha256"]
    if not isinstance(original_sha256, str) or not _SHA256.fullmatch(original_sha256):
        raise LedgerContractError("invalid original attempt sha256")
    report = validate_report_identity(payload["report"])
    return {
        "schema_version": REPAIR_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "original_sha256": original_sha256,
        "report": report.as_record(),
    }


def canonical_attempt_repair_bytes(payload: Any) -> bytes:
    normalized = validate_attempt_repair(payload)
    return (
        json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def decode_attempt_repair_json(raw: str | bytes) -> dict[str, Any]:
    raw_size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    if raw_size > MAX_REPAIR_BYTES:
        raise LedgerContractError("attempt repair exceeds file size limit")
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except LedgerContractError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise LedgerContractError("attempt repair contains invalid JSON") from exc
    return validate_attempt_repair(payload)


def decode_attempt_json(raw: str | bytes) -> dict[str, Any]:
    raw_size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    if raw_size > MAX_ATTEMPT_BYTES:
        raise LedgerContractError("attempt exceeds file size limit")
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except LedgerContractError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise LedgerContractError("attempt contains invalid JSON") from exc
    return validate_attempt(payload)


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LedgerUnavailableError("ledger is unavailable") from exc
    return _metadata_is_link_or_reparse(metadata)


def _existing_components(path: Path) -> list[Path]:
    current = Path(path.anchor)
    components: list[Path] = []
    for part in path.parts[1:]:
        current /= part
        try:
            os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise LedgerUnavailableError("ledger is unavailable") from exc
        components.append(current)
    return components


def _assert_no_link_components(path: Path, *, contract_error: bool) -> None:
    for component in _existing_components(path):
        if _is_link_or_reparse(component):
            message = "ledger contains a symbolic link or reparse point"
            if contract_error:
                raise LedgerContractError(message)
            raise LedgerLocationError("ledger path must not contain a symbolic link or reparse point")


def _assert_directory_contained(candidate: Path, *, root: Path) -> None:
    try:
        resolved_root = root.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(resolved_root)
    except (RuntimeError, ValueError) as exc:
        raise LedgerLocationError("ledger directory escapes configured root") from exc
    except OSError as exc:
        raise LedgerUnavailableError("ledger is unavailable") from exc


def _external_directory(
    configured: str | Path,
    *,
    repository_root: str | Path,
) -> Path:
    try:
        raw = Path(configured).expanduser()
    except RuntimeError as exc:
        raise LedgerLocationError("ledger path cannot be resolved safely") from exc
    if not raw.is_absolute():
        raise LedgerLocationError("ledger path must be absolute")
    _assert_no_link_components(raw, contract_error=False)
    try:
        root = Path(repository_root).resolve(strict=False)
        resolved = raw.resolve(strict=False)
    except RuntimeError as exc:
        raise LedgerLocationError("ledger path cannot be resolved safely") from exc
    except OSError as exc:
        raise LedgerUnavailableError("ledger is unavailable") from exc
    try:
        resolved.relative_to(root)
    except ValueError:
        pass
    else:
        raise LedgerLocationError("ledger path must be outside the repository")
    return resolved


def read_bounded_file(path: Path, *, max_bytes: int) -> bytes:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise LedgerContractError("invalid file size limit")
    parent_descriptor: int | None = None
    try:
        if os.name == "posix":
            parent_descriptor = _open_directory_chain(path.parent)
            before_path = os.stat(
                path.name, dir_fd=parent_descriptor, follow_symlinks=False
            )
        else:
            before_path = os.lstat(path)
    except OSError as exc:
        raise LedgerUnavailableError("ledger is unavailable") from exc
    if _metadata_is_link_or_reparse(before_path) or not stat.S_ISREG(before_path.st_mode):
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        raise LedgerContractError("attempt must be a regular file")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if os.name == "posix":
        flags |= os.O_NONBLOCK
    descriptor: int | None = None
    try:
        if parent_descriptor is not None:
            descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        else:
            descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise LedgerContractError("attempt must be a regular file")
        if (before.st_dev, before.st_ino) != (before_path.st_dev, before_path.st_ino):
            raise LedgerContractError("attempt changed while reading")

        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise LedgerContractError("attempt exceeds file size limit")

        after = os.fstat(descriptor)
        try:
            if parent_descriptor is not None:
                after_path = os.stat(
                    path.name, dir_fd=parent_descriptor, follow_symlinks=False
                )
            else:
                after_path = os.lstat(path)
        except OSError as exc:
            raise LedgerContractError("attempt changed while reading") from exc
        identities = (
            (before.st_dev, before.st_ino),
            (after.st_dev, after.st_ino),
            (after_path.st_dev, after_path.st_ino),
        )
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
        if len(set(identities)) != 1 or before_change != after_change:
            raise LedgerContractError("attempt changed while reading")
        if after.st_size != len(raw) or _metadata_is_link_or_reparse(after_path):
            raise LedgerContractError("attempt changed while reading")
        return raw
    except LedgerError:
        raise
    except OSError as exc:
        raise LedgerUnavailableError("ledger is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _read_existing(path: Path, expected: bytes) -> str:
    try:
        existing = read_bounded_file(path, max_bytes=MAX_ATTEMPT_BYTES)
    except LedgerContractError as exc:
        raise LedgerConflictError("attempt identity conflict") from exc
    if existing == expected:
        return "already_exists"
    raise LedgerConflictError("attempt identity conflict")


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _ensure_ledger_directory(path: Path, *, root: Path) -> None:
    _assert_no_link_components(path, contract_error=True)
    try:
        _assert_directory_contained(path, root=root)
    except LedgerLocationError as exc:
        raise LedgerContractError("ledger directory escapes configured root") from exc
    try:
        path.mkdir(mode=0o750, exist_ok=True)
    except OSError as exc:
        raise LedgerUnavailableError("ledger is unavailable") from exc
    _assert_no_link_components(path, contract_error=True)
    try:
        if not path.is_dir():
            raise LedgerContractError("ledger path must be a directory")
    except OSError as exc:
        raise LedgerUnavailableError("ledger is unavailable") from exc


def append_attempt(
    ledger_dir: str | Path,
    payload: Any,
    *,
    repository_root: str | Path,
) -> str:
    normalized = validate_attempt(payload)
    encoded = canonical_attempt_bytes(normalized)
    if len(encoded) > MAX_ATTEMPT_BYTES:
        raise LedgerContractError("attempt exceeds file size limit")
    root = _external_directory(ledger_dir, repository_root=repository_root)
    try:
        root.mkdir(parents=True, mode=0o750, exist_ok=True)
    except OSError as exc:
        raise LedgerUnavailableError("ledger is unavailable") from exc
    _assert_no_link_components(root, contract_error=False)

    attempts_dir = root / "attempts"
    stage_dir = attempts_dir / normalized["stage"]
    parent = stage_dir / normalized["report_date"]
    destination = parent / f"{normalized['attempt_id']}.json"
    identity_dir = root / ".attempt-identities"
    identity = identity_dir / f"{normalized['attempt_id']}.claim"
    for directory in (attempts_dir, stage_dir, parent, identity_dir):
        _ensure_ledger_directory(directory, root=root)
    destination_preexisting = destination.exists() or destination.is_symlink()
    if destination_preexisting:
        _read_existing(destination, encoded)
    if identity.exists() or identity.is_symlink():
        _read_existing(identity, encoded)
        try:
            if not destination_preexisting:
                os.link(identity, destination)
                _fsync_directory(parent)
        except FileExistsError:
            _read_existing(destination, encoded)
        except OSError as exc:
            raise LedgerUnavailableError("ledger is unavailable") from exc
        return "already_exists"

    temporary = identity_dir / f".{normalized['attempt_id']}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o640,
        )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        identity_created = False
        try:
            os.link(temporary, identity)
        except FileExistsError:
            _read_existing(identity, encoded)
        else:
            identity_created = True
            _fsync_directory(identity_dir)
        try:
            if not destination_preexisting:
                os.link(identity, destination)
        except FileExistsError:
            _read_existing(destination, encoded)
        _fsync_directory(parent)
        return "created" if identity_created and not destination_preexisting else "already_exists"
    except LedgerError:
        raise
    except OSError as exc:
        raise LedgerUnavailableError("ledger is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _open_directory_chain(path: Path) -> int:
    """Open an absolute directory without following descendant links."""
    if os.name != "posix":
        raise LedgerUnavailableError("directory handles are unavailable")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(path.anchor, flags)
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise LedgerContractError("ledger directory is unsafe")
        return descriptor
    except LedgerError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise LedgerUnavailableError("ledger is unavailable") from exc


def _strict_fsync(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise LedgerUnavailableError("ledger repair durability failed") from exc


def _secure_directory_metadata(
    metadata: os.stat_result, *, root_metadata: os.stat_result
) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or _metadata_is_link_or_reparse(metadata):
        raise LedgerContractError("ledger repair directory is unsafe")
    if os.name == "posix" and (
        metadata.st_uid != root_metadata.st_uid
        or metadata.st_gid != root_metadata.st_gid
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise LedgerContractError("ledger repair directory permissions are unsafe")


def _open_repair_child(
    parent_descriptor: int,
    name: str,
    *,
    root_metadata: os.stat_result,
    create: bool,
) -> int:
    if create:
        try:
            os.mkdir(name, 0o750, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        else:
            _strict_fsync(parent_descriptor)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        _secure_directory_metadata(
            os.fstat(descriptor), root_metadata=root_metadata
        )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _append_attempt_repair_posix(
    root: Path,
    normalized: dict[str, Any],
    encoded: bytes,
) -> str:
    root_descriptor = _open_directory_chain(root)
    descriptors = [root_descriptor]
    temporary_descriptor: int | None = None
    temporary_name = f".{normalized['attempt_id']}.{secrets.token_hex(8)}.tmp"
    destination_name = f"{normalized['attempt_id']}.json"
    destination_created = False
    try:
        root_metadata = os.fstat(root_descriptor)
        _secure_directory_metadata(root_metadata, root_metadata=root_metadata)
        repair_descriptor = _open_repair_child(
            root_descriptor,
            ".attempt-repairs",
            root_metadata=root_metadata,
            create=True,
        )
        descriptors.append(repair_descriptor)
        stage_descriptor = _open_repair_child(
            repair_descriptor,
            "publisher",
            root_metadata=root_metadata,
            create=True,
        )
        descriptors.append(stage_descriptor)
        date_descriptor = _open_repair_child(
            stage_descriptor,
            normalized["report"]["report_date"],
            root_metadata=root_metadata,
            create=True,
        )
        descriptors.append(date_descriptor)
        temp_directory = _open_repair_child(
            root_descriptor,
            ".attempt-repair-tmp",
            root_metadata=root_metadata,
            create=True,
        )
        descriptors.append(temp_directory)

        destination = (
            root
            / ".attempt-repairs"
            / "publisher"
            / normalized["report"]["report_date"]
            / destination_name
        )
        try:
            os.stat(destination_name, dir_fd=date_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            return _read_existing(destination, encoded)

        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o640,
            dir_fd=temp_directory,
        )
        os.fchmod(temporary_descriptor, 0o640)
        view = memoryview(encoded)
        while view:
            written = os.write(temporary_descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        _strict_fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = None
        try:
            os.link(
                temporary_name,
                destination_name,
                src_dir_fd=temp_directory,
                dst_dir_fd=date_descriptor,
                follow_symlinks=False,
            )
            destination_created = True
        except FileExistsError:
            return _read_existing(destination, encoded)
        _strict_fsync(date_descriptor)
        os.unlink(temporary_name, dir_fd=temp_directory)
        _strict_fsync(temp_directory)
        return "created"
    except LedgerError:
        if destination_created:
            try:
                os.unlink(destination_name, dir_fd=descriptors[-2])
                _strict_fsync(descriptors[-2])
            except (OSError, LedgerError) as rollback_error:
                raise LedgerUnavailableError(
                    "ledger repair rollback requires manual recovery"
                ) from rollback_error
        raise
    except OSError as exc:
        if destination_created:
            try:
                os.unlink(destination_name, dir_fd=descriptors[-2])
                _strict_fsync(descriptors[-2])
            except (OSError, LedgerError) as rollback_error:
                raise LedgerUnavailableError(
                    "ledger repair rollback requires manual recovery"
                ) from rollback_error
        raise LedgerUnavailableError("ledger is unavailable") from exc
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if len(descriptors) == 5:
            try:
                os.unlink(temporary_name, dir_fd=descriptors[-1])
                _strict_fsync(descriptors[-1])
            except FileNotFoundError:
                pass
            except OSError:
                pass
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _append_attempt_repair_portable(
    root: Path,
    normalized: dict[str, Any],
    encoded: bytes,
) -> str:
    repair_root = root / ".attempt-repairs"
    stage_dir = repair_root / "publisher"
    parent = stage_dir / normalized["report"]["report_date"]
    temp_directory = root / ".attempt-repair-tmp"
    destination = parent / f"{normalized['attempt_id']}.json"
    for directory in (repair_root, stage_dir, parent, temp_directory):
        _ensure_ledger_directory(directory, root=root)
    if destination.exists() or destination.is_symlink():
        return _read_existing(destination, encoded)
    temporary = temp_directory / f".{normalized['attempt_id']}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    destination_created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o640,
        )
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(temporary, destination)
        destination_created = True
        _assert_no_link_components(parent, contract_error=True)
        return "created"
    except FileExistsError:
        return _read_existing(destination, encoded)
    except LedgerError:
        if destination_created:
            destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        if destination_created:
            destination.unlink(missing_ok=True)
        raise LedgerUnavailableError("ledger is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def append_attempt_repair(
    ledger_dir: str | Path,
    payload: Any,
    *,
    repository_root: str | Path,
) -> str:
    normalized = validate_attempt_repair(payload)
    encoded = canonical_attempt_repair_bytes(normalized)
    root = _external_directory(ledger_dir, repository_root=repository_root)
    _assert_no_link_components(root, contract_error=False)
    try:
        root_metadata = os.lstat(root)
    except OSError as exc:
        raise LedgerUnavailableError("ledger is unavailable") from exc
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise LedgerContractError("ledger path must be a directory")
    if os.name == "posix":
        return _append_attempt_repair_posix(root, normalized, encoded)
    return _append_attempt_repair_portable(root, normalized, encoded)


def remove_attempt_repair(
    ledger_dir: str | Path,
    payload: Any,
    *,
    repository_root: str | Path,
) -> None:
    """Remove only an exact overlay created by the current repair operation."""
    normalized = validate_attempt_repair(payload)
    encoded = canonical_attempt_repair_bytes(normalized)
    root = _external_directory(ledger_dir, repository_root=repository_root)
    destination = (
        root
        / ".attempt-repairs"
        / "publisher"
        / normalized["report"]["report_date"]
        / f"{normalized['attempt_id']}.json"
    )
    if _read_existing(destination, encoded) != "already_exists":
        raise LedgerConflictError("attempt repair identity conflict")
    if os.name == "posix":
        root_descriptor = _open_directory_chain(root)
        descriptors = [root_descriptor]
        try:
            root_metadata = os.fstat(root_descriptor)
            _secure_directory_metadata(root_metadata, root_metadata=root_metadata)
            current = root_descriptor
            for name in (
                ".attempt-repairs",
                "publisher",
                normalized["report"]["report_date"],
            ):
                current = _open_repair_child(
                    current,
                    name,
                    root_metadata=root_metadata,
                    create=False,
                )
                descriptors.append(current)
            metadata = os.stat(
                destination.name,
                dir_fd=current,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(metadata.st_mode) or _metadata_is_link_or_reparse(metadata):
                raise LedgerConflictError("attempt repair identity conflict")
            os.unlink(destination.name, dir_fd=current)
            _strict_fsync(current)
            cleanup = (
                (descriptors[2], normalized["report"]["report_date"]),
                (descriptors[1], "publisher"),
                (descriptors[0], ".attempt-repairs"),
                (descriptors[0], ".attempt-repair-tmp"),
            )
            for parent_descriptor, name in cleanup:
                try:
                    os.rmdir(name, dir_fd=parent_descriptor)
                    _strict_fsync(parent_descriptor)
                except OSError as exc:
                    if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                        raise
        except LedgerError:
            raise
        except OSError as exc:
            raise LedgerUnavailableError("ledger repair rollback failed") from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        return
    _assert_no_link_components(destination.parent, contract_error=True)
    destination.unlink()
    for directory in (
        destination.parent,
        destination.parent.parent,
        destination.parent.parent.parent,
        root / ".attempt-repair-tmp",
    ):
        try:
            directory.rmdir()
        except OSError:
            pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LedgerContractError("attempt JSON contains duplicate keys")
        result[key] = value
    return result


def _public_attempt(attempt: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "attempt_id",
        "report_date",
        "scheduled_for",
        "finished_at",
        "status",
        "result_code",
        "report",
        "registry_revision",
        "sources",
    )
    return {field: attempt[field] for field in fields if field in attempt}


class RunLedgerReader:
    def __init__(
        self,
        ledger_dir: str | Path,
        *,
        repository_root: str | Path,
        stale_after_hours: int = DEFAULT_STALE_AFTER_HOURS,
        max_file_bytes: int = MAX_ATTEMPT_BYTES,
        max_attempt_count: int = MAX_ATTEMPT_COUNT,
        max_total_bytes: int = MAX_TOTAL_BYTES,
        max_visited_entries: int = MAX_VISITED_ENTRIES,
        max_visited_dirs: int = MAX_VISITED_DIRS,
    ):
        self.root = _external_directory(ledger_dir, repository_root=repository_root)
        if (
            isinstance(stale_after_hours, bool)
            or not isinstance(stale_after_hours, int)
            or stale_after_hours <= 0
        ):
            raise LedgerContractError("invalid stale threshold")
        self.stale_after_hours = stale_after_hours
        limits = {
            "file size": max_file_bytes,
            "attempt count": max_attempt_count,
            "total byte": max_total_bytes,
            "visited entry": max_visited_entries,
            "visited directory": max_visited_dirs,
        }
        for label, value in limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise LedgerContractError(f"invalid {label} limit")
        self.max_file_bytes = max_file_bytes
        self.max_attempt_count = max_attempt_count
        self.max_total_bytes = max_total_bytes
        self.max_visited_entries = max_visited_entries
        self.max_visited_dirs = max_visited_dirs

    def _files(self) -> Iterator[Path]:
        try:
            root_metadata = os.lstat(self.root)
        except OSError as exc:
            raise LedgerUnavailableError("ledger is unavailable") from exc
        if _is_link_or_reparse(self.root) or not stat.S_ISDIR(root_metadata.st_mode):
            raise LedgerUnavailableError("ledger is unavailable")
        attempts_root = self.root / "attempts"
        if _is_link_or_reparse(attempts_root):
            raise LedgerContractError("ledger contains a symbolic link or reparse point")
        try:
            attempts_metadata = os.lstat(attempts_root)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LedgerUnavailableError("ledger is unavailable") from exc
        if not stat.S_ISDIR(attempts_metadata.st_mode):
            raise LedgerContractError("attempts path must be a directory")
        visited_entries = 0
        visited_dirs = 0
        attempt_count = 0
        pending = [attempts_root]
        while pending:
            current_path = pending.pop()
            visited_dirs += 1
            if visited_dirs > self.max_visited_dirs:
                raise LedgerContractError("ledger exceeds visited directory limit")
            try:
                _assert_directory_contained(current_path, root=attempts_root)
            except LedgerLocationError as exc:
                raise LedgerContractError(
                    "ledger directory escapes configured root"
                ) from exc
            _assert_no_link_components(current_path, contract_error=True)
            try:
                with os.scandir(current_path) as entries:
                    for entry in entries:
                        visited_entries += 1
                        if visited_entries > self.max_visited_entries:
                            raise LedgerContractError(
                                "ledger exceeds visited entry limit"
                            )
                        path = Path(entry.path)
                        metadata = entry.stat(follow_symlinks=False)
                        if _metadata_is_link_or_reparse(metadata):
                            raise LedgerContractError(
                                "ledger contains a symbolic link or reparse point"
                            )
                        try:
                            _assert_directory_contained(path, root=attempts_root)
                        except LedgerLocationError as exc:
                            raise LedgerContractError(
                                "ledger entry escapes configured root"
                            ) from exc
                        if stat.S_ISDIR(metadata.st_mode):
                            pending.append(path)
                            continue
                        if not entry.name.endswith(".json"):
                            continue
                        attempt_count += 1
                        if attempt_count > self.max_attempt_count:
                            raise LedgerContractError(
                                "ledger exceeds attempt count limit"
                            )
                        yield path
            except LedgerError:
                raise
            except OSError as exc:
                raise LedgerUnavailableError("ledger is unavailable") from exc

    def _repair_files(self) -> Iterator[Path]:
        repair_root = self.root / ".attempt-repairs"
        if _is_link_or_reparse(repair_root):
            raise LedgerContractError("ledger contains a symbolic link or reparse point")
        try:
            root_metadata = os.lstat(repair_root)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LedgerUnavailableError("ledger is unavailable") from exc
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise LedgerContractError("attempt repairs path must be a directory")

        visited_entries = 0
        repair_count = 0
        try:
            with os.scandir(repair_root) as stages:
                for stage_entry in stages:
                    visited_entries += 1
                    if visited_entries > self.max_visited_entries:
                        raise LedgerContractError("ledger exceeds visited entry limit")
                    metadata = stage_entry.stat(follow_symlinks=False)
                    if (
                        stage_entry.name != "publisher"
                        or _metadata_is_link_or_reparse(metadata)
                        or not stat.S_ISDIR(metadata.st_mode)
                    ):
                        raise LedgerContractError("invalid attempt repair layout")
                    stage_path = Path(stage_entry.path)
                    _assert_directory_contained(stage_path, root=repair_root)
                    with os.scandir(stage_path) as dates:
                        for date_entry in dates:
                            visited_entries += 1
                            if visited_entries > self.max_visited_entries:
                                raise LedgerContractError(
                                    "ledger exceeds visited entry limit"
                                )
                            metadata = date_entry.stat(follow_symlinks=False)
                            try:
                                canonical_report_filename(date_entry.name)
                            except LedgerContractError as exc:
                                raise LedgerContractError(
                                    "invalid attempt repair layout"
                                ) from exc
                            if _metadata_is_link_or_reparse(
                                metadata
                            ) or not stat.S_ISDIR(metadata.st_mode):
                                raise LedgerContractError(
                                    "invalid attempt repair layout"
                                )
                            date_path = Path(date_entry.path)
                            _assert_directory_contained(date_path, root=repair_root)
                            with os.scandir(date_path) as repairs:
                                for repair_entry in repairs:
                                    visited_entries += 1
                                    if visited_entries > self.max_visited_entries:
                                        raise LedgerContractError(
                                            "ledger exceeds visited entry limit"
                                        )
                                    metadata = repair_entry.stat(
                                        follow_symlinks=False
                                    )
                                    if (
                                        _metadata_is_link_or_reparse(metadata)
                                        or not stat.S_ISREG(metadata.st_mode)
                                        or not repair_entry.name.endswith(".json")
                                    ):
                                        raise LedgerContractError(
                                            "invalid attempt repair layout"
                                        )
                                    repair_count += 1
                                    if repair_count > self.max_attempt_count:
                                        raise LedgerContractError(
                                            "ledger exceeds attempt repair count limit"
                                        )
                                    path = Path(repair_entry.path)
                                    _assert_directory_contained(
                                        path, root=repair_root
                                    )
                                    yield path
        except LedgerError:
            raise
        except OSError as exc:
            raise LedgerUnavailableError("ledger is unavailable") from exc

    def _load(self) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        raw_sha256_by_id: dict[str, str] = {}
        total_bytes = 0
        for path in self._files():
            raw = read_bounded_file(path, max_bytes=self.max_file_bytes)
            total_bytes += len(raw)
            if total_bytes > self.max_total_bytes:
                raise LedgerContractError("ledger exceeds total byte limit")
            attempt = decode_attempt_json(raw)
            relative = path.relative_to(self.root)
            expected = Path(
                "attempts",
                attempt["stage"],
                attempt["report_date"],
                f"{attempt['attempt_id']}.json",
            )
            if relative != expected:
                raise LedgerContractError("attempt path does not match record identity")
            if attempt["attempt_id"] in seen_ids:
                raise LedgerContractError("duplicate attempt identity")
            seen_ids.add(attempt["attempt_id"])
            raw_sha256_by_id[attempt["attempt_id"]] = hashlib.sha256(raw).hexdigest()
            attempts.append(attempt)
        attempts.sort(key=lambda item: (item["finished_at"], item["attempt_id"]))

        repairs: list[dict[str, Any]] = []
        seen_repair_ids: set[str] = set()
        for path in self._repair_files():
            raw = read_bounded_file(path, max_bytes=MAX_REPAIR_BYTES)
            total_bytes += len(raw)
            if total_bytes > self.max_total_bytes:
                raise LedgerContractError("ledger exceeds total byte limit")
            repair = decode_attempt_repair_json(raw)
            expected = Path(
                ".attempt-repairs",
                "publisher",
                repair["report"]["report_date"],
                f"{repair['attempt_id']}.json",
            )
            if path.relative_to(self.root) != expected:
                raise LedgerContractError("attempt repair path does not match identity")
            if repair["attempt_id"] in seen_repair_ids:
                raise LedgerContractError("duplicate attempt repair identity")
            seen_repair_ids.add(repair["attempt_id"])
            repairs.append(repair)

        attempts_by_id = {attempt["attempt_id"]: attempt for attempt in attempts}
        for repair in repairs:
            original = attempts_by_id.get(repair["attempt_id"])
            if original is None:
                raise LedgerContractError("attempt repair target is missing")
            if (
                original["stage"] != "publisher"
                or original["status"] not in SUCCESS_STATUSES
                or "report" in original
                or repair["report"]["report_date"] != original["report_date"]
                or raw_sha256_by_id[repair["attempt_id"]]
                != repair["original_sha256"]
            ):
                raise LedgerContractError("attempt repair target is invalid")
            repaired = validate_attempt({**original, "report": repair["report"]})
            if {key: value for key, value in repaired.items() if key != "report"} != original:
                raise LedgerContractError("attempt repair changes original fields")
            original.clear()
            original.update(repaired)
        return attempts

    def _stage_status(
        self,
        attempts: list[dict[str, Any]],
        *,
        now: datetime,
    ) -> dict[str, Any]:
        latest = attempts[-1] if attempts else None
        successful = [item for item in attempts if item["status"] in SUCCESS_STATUSES]
        last_success = successful[-1] if successful else None
        if latest and latest["status"] == "failed":
            stale_reason = "latest_attempt_failed"
        elif latest and latest["status"] == "partial":
            stale_reason = "latest_attempt_partial"
        elif last_success is None:
            stale_reason = "no_successful_attempt"
        else:
            success_time = _strict_utc(last_success["finished_at"], field="finished_at")
            stale_reason = (
                "last_success_expired"
                if now - success_time > timedelta(hours=self.stale_after_hours)
                else None
            )
        result: dict[str, Any] = {
            "attempt_count": len(attempts),
            "last_attempt": _public_attempt(latest) if latest else None,
            "last_success": _public_attempt(last_success) if last_success else None,
            "has_newer_unsuccessful_attempt": bool(
                latest and latest["status"] in {"failed", "partial"}
            ),
            "stale": {
                "is_stale": stale_reason is not None,
                "reason": stale_reason or "current",
                "max_age_hours": self.stale_after_hours,
            },
        }
        return result

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        as_of = now or datetime.now(timezone.utc)
        if as_of.tzinfo is None or as_of.utcoffset() != timedelta(0):
            raise LedgerContractError("status clock must be UTC")
        attempts = self._load()
        if any(
            _strict_utc(attempt["finished_at"], field="finished_at") > as_of
            for attempt in attempts
        ):
            raise LedgerContractError("future_attempt")
        by_stage = {
            stage: [attempt for attempt in attempts if attempt["stage"] == stage]
            for stage in sorted(STAGES)
        }
        stages = {
            stage: self._stage_status(stage_attempts, now=as_of)
            for stage, stage_attempts in by_stage.items()
        }
        monitor_stale = stages["monitor"]["stale"]
        if not attempts:
            state = "empty"
        elif monitor_stale["reason"] in {"latest_attempt_failed", "latest_attempt_partial"}:
            state = "degraded"
        elif monitor_stale["is_stale"]:
            state = "stale"
        else:
            state = "current"
        result: dict[str, Any] = {
            "available": True,
            "schema_version": SCHEMA_VERSION,
            "state": state,
            "attempt_count": len(attempts),
            "stages": stages,
            "stale": monitor_stale,
            "stale_source": "monitor",
        }
        evidenced = [
            attempt
            for attempt in attempts
            if attempt["status"] in SUCCESS_STATUSES
        ]
        report_attempts = [attempt for attempt in evidenced if "report" in attempt]
        if report_attempts:
            report_attempt = report_attempts[-1]
            result["latest_successful_report"] = {
                "stage": report_attempt["stage"],
                **report_attempt["report"],
            }
        revision_attempts = [
            attempt for attempt in evidenced if "registry_revision" in attempt
        ]
        if revision_attempts:
            revision_attempt = revision_attempts[-1]
            result["latest_successful_registry_revision"] = {
                "stage": revision_attempt["stage"],
                **revision_attempt["registry_revision"],
            }
        latest_monitor = by_stage["monitor"][-1] if by_stage["monitor"] else None
        if latest_monitor and "sources" in latest_monitor:
            result["sources"] = latest_monitor["sources"]
        return result
