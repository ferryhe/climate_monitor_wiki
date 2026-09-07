"""Write-side of the weekly job-status snapshot (Issue #87 AC-3/AC-4).

The companion read-side lives in :mod:`climate_monitor.job_status`. That
module validates the on-disk snapshot written by ``update_slot`` here.

The writer is the producer counterpart of the read-side:

* Slot names are restricted to ``{monitor, email, publisher, registry}``.
  The canonical three slots are required; ``registry`` is optional and
  surfaces a forward-compatible fourth slot for the Issue #87 workflow.
* States are restricted to the same six as the reader:
  ``scheduled, running, completed, failed, unknown, not_dispatched``.
* Per-state field set is enforced by reusing the read-side validator as
  defence-in-depth, so any drift between the two sides is caught
  immediately by tests.
* The on-disk file is replaced atomically via ``renameat2``-equivalent
  ``os.rename`` on a temporary file written into the same directory; the
  directory descriptor is opened with ``O_NOFOLLOW|O_DIRECTORY`` to
  refuse symlinked parents, mirroring the read-side invariant.
* Free-form ``error_reason`` text is intentionally never serialised —
  the on-disk contract permits only the strict ``result_code`` token.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .job_status import (
    JOB_SCHEDULE_HOURS,
    SNAPSHOT_FILENAME,
    SCHEMA_VERSION,
    STATES,
    JobStatusInvalidSnapshotError,
)


# ---------------------------------------------------------------------------
# Public error hierarchy
# ---------------------------------------------------------------------------


class SchedulerStatusError(RuntimeError):
    """Base class for safe scheduler-status snapshot writer failures."""


class SchedulerStatusLocationError(SchedulerStatusError):
    """The configured snapshot directory is not a writable, external location."""


class SchedulerStatusArgumentError(SchedulerStatusError):
    """A writer argument violated the strict slot contract."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_VALID_SLOTS = frozenset({"monitor", "email", "publisher", "registry"})
_TIME_FIELDS: tuple[str, ...] = ("claimed_at", "started_at", "finished_at")
# Schedule hours for the registry slot (canonical slots reuse JOB_SCHEDULE_HOURS)
_REGISTRY_HOUR = 10
_REGISTRY_MINUTE = 30

# Atomic-replacement support: dirfd-relative open with O_NOFOLLOW|O_DIRECTORY
# is the same trick the reader uses. The writer falls back to a plain
# ``os.rename`` when the host doesn't expose dir_fd in os.open.
_SUPPORTS_ANCHORED_DIRECTORY_OPEN = (
    os.name == "posix"
    and "dir_fd" in getattr(os.open, "__doc__", "")
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strict_utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or len(value) != 20:
        raise SchedulerStatusArgumentError(
            f"{field} must be a 20-character strict UTC timestamp"
        )
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SchedulerStatusArgumentError(
            f"{field} must be a strict UTC timestamp with zero seconds"
        ) from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise SchedulerStatusArgumentError(
            f"{field} must be a strict UTC timestamp with zero seconds"
        ) from exc
    return parsed.replace(tzinfo=timezone.utc)


def _coerce_time(value: str | datetime | None, *, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise SchedulerStatusArgumentError(
                f"{field} must be timezone-aware"
            )
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(value, str):
        raise SchedulerStatusArgumentError(
            f"{field} must be a UTC timestamp string or datetime, got {type(value).__name__}"
        )
    _strict_utc(value, field=field)
    return value


def _target_for_slot(name: str) -> tuple[int, int]:
    if name in JOB_SCHEDULE_HOURS:
        return JOB_SCHEDULE_HOURS[name], 0
    if name == "registry":
        return _REGISTRY_HOUR, _REGISTRY_MINUTE
    raise SchedulerStatusArgumentError(f"unknown slot {name!r}")


def _validate_state(state: Any) -> None:
    if not isinstance(state, str) or state not in STATES:
        raise SchedulerStatusArgumentError(
            f"state must be one of {sorted(STATES)}, got {state!r}"
        )


def _validate_result_code(result_code: Any) -> None:
    if result_code is None:
        return
    if not (
        isinstance(result_code, str)
        and result_code.isascii()
        and 1 <= len(result_code) <= 64
        and all(ch.isalnum() or ch in ("_", "-") for ch in result_code)
    ):
        raise SchedulerStatusArgumentError(
            "result_code must be a safe token (alnum / _ -) up to 64 chars"
        )


def _result_code_for_state(state: str, requested: str | None) -> str | None:
    if requested is not None:
        return requested
    defaults = {
        "failed": "execution_failed",
        "unknown": "execution_unknown",
        "not_dispatched": "not_dispatched",
    }
    return defaults.get(state)


def _resolve_status_dir(
    *,
    status_dir: str | Path | None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the snapshot directory from arg then env, validating the path."""
    chosen: str | Path | None = status_dir
    if chosen is None:
        env_value = (env or os.environ).get("CLIMATE_JOB_STATUS_DIR", "")
        chosen = env_value or None
    if chosen is None or str(chosen) == "":
        raise SchedulerStatusArgumentError(
            "status_dir is required (pass it explicitly or set "
            "CLIMATE_JOB_STATUS_DIR in the environment)"
        )
    path = Path(chosen)
    if not path.is_absolute():
        raise SchedulerStatusLocationError(
            "status_dir must be an absolute path"
        )
    if any(part == os.pardir for part in path.parts):
        raise SchedulerStatusLocationError(
            "status_dir must not contain parent traversal"
        )
    return path


def _seed_for_monday() -> dict[str, dict[str, str]]:
    """Build a seed payload for ``JOB_SCHEDULE_HOURS`` slots on the next Monday.

    The seed uses the most recent Monday relative to ``generated_at`` so
    freshly-created snapshots still satisfy the read-side validator's
    "jobs must describe the generated week" invariant.
    """
    generated = _aware_now()
    generated_monday = generated.date() - timedelta(days=generated.weekday())
    return {
        alias: {
            "scheduled_for": f"{generated_monday.isoformat()}T{hour:02d}:00:00Z",
            "state": "scheduled",
        }
        for alias, hour in JOB_SCHEDULE_HOURS.items()
    }


def _aware_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _load_existing_payload(target: Path) -> dict[str, Any]:
    """Return the existing snapshot, seeded with the canonical three slots.

    A new snapshot is always serialised with all three canonical slots
    (``monitor``, ``email``, ``publisher``) present in their default
    ``scheduled`` state, so the read-side exact-shape validator accepts
    the payload even when only one slot has been written by a wrapper.
    The optional ``registry`` slot is added lazily by ``update_slot``.
    """
    seeded_jobs = _seed_for_monday()
    if not target.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now_utc(),
            "jobs": seeded_jobs,
        }
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise SchedulerStatusLocationError(
            "snapshot is not readable"
        ) from exc
    try:
        loaded = json.loads(raw)
    except ValueError as exc:
        raise SchedulerStatusLocationError(
            "snapshot is not valid JSON"
        ) from exc
    if not isinstance(loaded, dict):
        raise SchedulerStatusLocationError("snapshot root must be an object")
    loaded.setdefault("schema_version", SCHEMA_VERSION)
    jobs = loaded.get("jobs")
    if not isinstance(jobs, dict):
        jobs = {}
    for alias, default in seeded_jobs.items():
        jobs.setdefault(alias, dict(default))
    loaded["jobs"] = jobs
    return loaded


def _build_job_block(
    *,
    state: str,
    scheduled_for: datetime,
    claimed_at: str | datetime | None,
    started_at: str | datetime | None,
    finished_at: str | datetime | None,
    result_code: str | None,
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "scheduled_for": scheduled_for.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "state": state,
    }
    claimed_str = _coerce_time(claimed_at, field="claimed_at")
    started_str = _coerce_time(started_at, field="started_at")
    finished_str = _coerce_time(finished_at, field="finished_at")
    if claimed_str is not None:
        block["claimed_at"] = claimed_str
    if started_str is not None:
        block["started_at"] = started_str
    if finished_str is not None:
        block["finished_at"] = finished_str
    final_code = _result_code_for_state(state, result_code)
    if final_code is not None:
        block["result_code"] = final_code
    return block


def _validate_payload_against_read_contract(payload: Mapping[str, Any]) -> None:
    """Round-trip through the read-side validator to catch contract drift.

    Pass the snapshot's own ``generated_at`` as ``now`` so the read-side
    acceptance check (generated_at must not be in the future) does not reject
    a freshly-written snapshot. Callers using the writer to seed a snapshot
    for a specific moment (e.g. a fixed test clock) get back-validated
    against that same moment.
    """
    from . import job_status

    now_arg: datetime | None = None
    raw_generated = payload.get("generated_at")
    if isinstance(raw_generated, str):
        try:
            now_arg = datetime.strptime(raw_generated, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            now_arg = None
    try:
        job_status.validate_snapshot(payload, now=now_arg)
    except JobStatusInvalidSnapshotError as exc:
        raise SchedulerStatusArgumentError(
            f"resulting snapshot fails read-side validation: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------


def _open_directory_for_write(directory: Path) -> int | None:
    """Open ``directory`` with O_NOFOLLOW|O_DIRECTORY for dirfd-relative ops.

    Returns ``None`` when the host doesn't support the relevant POSIX
    flags; callers fall back to ``os.rename``.
    """
    if not _SUPPORTS_ANCHORED_DIRECTORY_OPEN:
        return None
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(str(directory), flags)
    except OSError as exc:
        raise SchedulerStatusLocationError(
            f"status_dir is not openable as a directory: {exc}"
        ) from exc


def _open_temp_file(directory_descriptor: int | None, target: Path) -> tuple[int, str]:
    """Open a temp file in :py:obj:`target`'s parent directory.

    Returns ``(fd, basename)``. The basename is relative to the directory
    when ``directory_descriptor`` is provided, otherwise an absolute path
    suitable for direct ``os.rename``.
    """
    if directory_descriptor is None:
        # Fallback: open via tempfile; still in the same directory so the
        # subsequent rename is atomic on POSIX.
        fd, path = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
        )
        return fd, path
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    basename = f".{os.sep}{target.name}.{os.getpid()}.tmp"
    fd = os.open(basename, flags, dir_fd=directory_descriptor)
    return fd, basename


def _close_quietly(fd: int | None) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _atomic_write(target: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    directory_descriptor: int | None = None
    temp_path_str: str | None = None
    fd: int | None = None
    cleanup_temp: list[str] = []
    try:
        directory_descriptor = _open_directory_for_write(target.parent)
        fd, temp_path_str = _open_temp_file(directory_descriptor, target)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        fd = None
        if directory_descriptor is None:
            os.rename(temp_path_str, target)
            temp_path_str = None
        else:
            try:
                os.rename(
                    temp_path_str,
                    f".{os.sep}{target.name}",
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
                temp_path_str = None
            except (OSError, ValueError):
                # The host kernel may accept dir_fd in open but not in rename.
                # Resolve the basename to an absolute path and retry.
                cleanup_temp.append(temp_path_str)
                resolved = (
                    Path("/proc/self/fd")
                    / str(os.open(temp_path_str, os.O_RDONLY, dir_fd=directory_descriptor))
                ).resolve()
                os.rename(str(resolved), target)
                temp_path_str = None
    finally:
        _close_quietly(fd)
        _close_quietly(directory_descriptor)
        for leftover in cleanup_temp:
            try:
                Path(leftover).unlink(missing_ok=True)
            except OSError:
                pass
        if temp_path_str is not None:
            try:
                Path(temp_path_str).unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def update_slot(
    name: str,
    state: str,
    *,
    scheduled_for: str | datetime,
    claimed_at: str | datetime | None = None,
    started_at: str | datetime | None = None,
    finished_at: str | datetime | None = None,
    result_code: str | None = None,
    error_reason: str | None = None,
    status_dir: str | Path | None = None,
) -> Path:
    """Atomically replace ``scheduler-status.json`` with one slot updated.

    Parameters
    ----------
    name:
        One of ``{monitor, email, publisher, registry}``.
    state:
        One of ``{scheduled, running, completed, failed, unknown,
        not_dispatched}``.
    scheduled_for:
        Strict UTC ``YYYY-MM-DDTHH:MM:SSZ`` Monday timestamp matching the
        slot's public schedule (08:00, 09:00, 10:00, or 10:30 UTC).
    claimed_at / started_at / finished_at:
        Optional execution timestamps (strict UTC). Per-state field set
        rules mirror the reader; ``running`` requires ``claimed_at``,
        ``completed``/``failed`` require all three.
    result_code:
        Optional explicit ``result_code`` token (alnum/_-, up to 64 chars).
        Defaults are inferred from the state for ``failed``/``unknown``/
        ``not_dispatched``.
    error_reason:
        Free-form failure description that is intentionally NOT written to
        disk. Accepted for API ergonomics only; passing it requires an
        explicit ``result_code`` so callers don't accidentally lose the
        structured failure token.
    status_dir:
        Absolute path to the directory containing ``scheduler-status.json``.
        Falls back to the ``CLIMATE_JOB_STATUS_DIR`` environment variable
        when omitted.

    Returns
    -------
    pathlib.Path
        The path of the written ``scheduler-status.json`` snapshot.
    """
    if name not in _VALID_SLOTS:
        raise SchedulerStatusArgumentError(
            f"name must be one of {sorted(_VALID_SLOTS)}, got {name!r}"
        )
    _validate_state(state)

    scheduled_str = _coerce_time(scheduled_for, field="scheduled_for")
    if scheduled_str is None:
        raise SchedulerStatusArgumentError("scheduled_for is required")
    scheduled_dt = _strict_utc(scheduled_str, field="scheduled_for")

    expected_hour, expected_minute = _target_for_slot(name)
    if (
        scheduled_dt.weekday() != 0
        or scheduled_dt.hour != expected_hour
        or scheduled_dt.minute != expected_minute
    ):
        raise SchedulerStatusArgumentError(
            f"scheduled_for does not match the public schedule for slot "
            f"{name!r} (expected Monday {expected_hour:02d}:{expected_minute:02d}Z)"
        )

    _validate_result_code(result_code)
    if error_reason is not None and result_code is None:
        raise SchedulerStatusArgumentError(
            "error_reason requires an explicit result_code token"
        )

    resolved_dir = _resolve_status_dir(status_dir=status_dir)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    target = resolved_dir / SNAPSHOT_FILENAME

    payload = _load_existing_payload(target)
    job_block = _build_job_block(
        state=state,
        scheduled_for=scheduled_dt,
        claimed_at=claimed_at,
        started_at=started_at,
        finished_at=finished_at,
        result_code=result_code,
    )
    payload["jobs"][name] = job_block
    payload["schema_version"] = SCHEMA_VERSION
    payload["generated_at"] = _now_utc()
    _validate_payload_against_read_contract(payload)

    _atomic_write(target, payload)
    return target


__all__ = [
    "SchedulerStatusError",
    "SchedulerStatusLocationError",
    "SchedulerStatusArgumentError",
    "update_slot",
]
