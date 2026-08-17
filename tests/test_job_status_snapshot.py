from __future__ import annotations

import json
import errno
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from climate_monitor.job_status import (
    DEFAULT_STALE_AFTER_MINUTES,
    JobStatusInvalidSnapshotError,
    JobStatusLocationError,
    JobStatusSnapshotReader,
    JobStatusUnavailableError,
    MAX_SNAPSHOT_BYTES,
    decode_snapshot_json,
    validate_snapshot,
)
from climate_monitor import job_status


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _snapshot() -> dict:
    return {
        "schema_version": "weekly-job-status.v1",
        "generated_at": "2026-08-17T11:55:00Z",
        "jobs": {
            "monitor": {
                "scheduled_for": "2026-08-17T08:00:00Z",
                "state": "completed",
                "claimed_at": "2026-08-17T08:00:01Z",
                "started_at": "2026-08-17T08:00:02Z",
                "finished_at": "2026-08-17T08:40:00Z",
            },
            "email": {
                "scheduled_for": "2026-08-17T09:00:00Z",
                "state": "failed",
                "claimed_at": "2026-08-17T09:00:01Z",
                "started_at": "2026-08-17T09:00:02Z",
                "finished_at": "2026-08-17T09:01:00Z",
                "result_code": "execution_failed",
            },
            "publisher": {
                "scheduled_for": "2026-08-17T10:00:00Z",
                "state": "scheduled",
            },
        },
    }


def _write_snapshot(directory: Path, payload: dict | bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "scheduler-status.json"
    if isinstance(payload, bytes):
        target.write_bytes(payload)
    else:
        target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_contract_accepts_exact_three_public_jobs_and_fixed_public_codes():
    assert validate_snapshot(_snapshot(), now=NOW) == _snapshot()

    payload = _snapshot()
    payload["jobs"]["monitor"] = {
        "scheduled_for": "2026-08-17T08:00:00Z",
        "state": "unknown",
        "claimed_at": "2026-08-17T08:00:01Z",
        "finished_at": "2026-08-17T08:10:00Z",
        "result_code": "execution_unknown",
    }
    payload["jobs"]["publisher"] = {
        "scheduled_for": "2026-08-17T10:00:00Z",
        "state": "not_dispatched",
        "result_code": "not_dispatched",
    }
    assert validate_snapshot(payload, now=NOW) == payload


@pytest.mark.parametrize(
    ("job", "replacement"),
    [
        ("monitor", {"scheduled_for": "2026-08-17T08:00:00Z", "state": "running"}),
        (
            "monitor",
            {
                "scheduled_for": "2026-08-17T08:00:00Z",
                "state": "scheduled",
                "claimed_at": "2026-08-17T08:00:01Z",
            },
        ),
        (
            "email",
            {
                "scheduled_for": "2026-08-17T09:00:00Z",
                "state": "failed",
                "claimed_at": "2026-08-17T09:00:01Z",
                "started_at": "2026-08-17T09:00:02Z",
                "finished_at": "2026-08-17T09:01:00Z",
            },
        ),
        (
            "publisher",
            {
                "scheduled_for": "2026-08-17T10:00:00Z",
                "state": "not_dispatched",
                "result_code": "execution_failed",
            },
        ),
        (
            "monitor",
            {
                "scheduled_for": "2026-08-17T08:00:00Z",
                "state": "completed",
                "claimed_at": "2026-08-17T08:00:01Z",
                "started_at": "2026-08-17T08:00:02Z",
                "finished_at": "2026-08-17T08:40:00Z",
                "result_code": "completed",
            },
        ),
    ],
)
def test_contract_rejects_state_inconsistent_fields(job, replacement):
    payload = _snapshot()
    payload["jobs"][job] = replacement
    with pytest.raises(JobStatusInvalidSnapshotError):
        validate_snapshot(payload, now=NOW)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("generated_at"),
        lambda payload: payload.update(extra="no"),
        lambda payload: payload["jobs"].pop("email"),
        lambda payload: payload["jobs"].update(extra={}),
        lambda payload: payload["jobs"]["monitor"].update(extra="no"),
        lambda payload: payload["jobs"]["monitor"].update(
            scheduled_for="2026-08-17T09:00:00Z"
        ),
        lambda payload: payload["jobs"]["monitor"].update(
            scheduled_for="2026-08-18T08:00:00Z"
        ),
        lambda payload: payload["jobs"]["email"].update(
            scheduled_for="2026-08-10T09:00:00Z"
        ),
        lambda payload: payload.update(generated_at="2026-08-17T12:00:01Z"),
        lambda payload: payload["jobs"]["monitor"].update(
            finished_at="2026-08-17T11:56:00Z"
        ),
        lambda payload: payload["jobs"]["monitor"].update(
            claimed_at="2026-08-17T08:00:03Z",
            started_at="2026-08-17T08:00:02Z",
        ),
        lambda payload: payload["jobs"]["monitor"].update(
            claimed_at="2026-08-17T07:59:59Z"
        ),
        lambda payload: payload["jobs"]["publisher"].update(
            scheduled_for="2026-08-24T10:00:00Z",
            state="not_dispatched",
            result_code="not_dispatched",
        ),
    ],
)
def test_contract_rejects_wrong_shape_schedule_or_time_order(mutate):
    payload = _snapshot()
    mutate(payload)
    with pytest.raises(JobStatusInvalidSnapshotError):
        validate_snapshot(payload, now=NOW)


def test_contract_binds_schedules_to_generated_at_utc_week():
    prior_week = _snapshot()
    prior_week["jobs"]["monitor"]["scheduled_for"] = "2026-08-10T08:00:00Z"
    prior_week["jobs"]["email"]["scheduled_for"] = "2026-08-10T09:00:00Z"
    prior_week["jobs"]["publisher"]["scheduled_for"] = "2026-08-10T10:00:00Z"
    with pytest.raises(JobStatusInvalidSnapshotError):
        validate_snapshot(prior_week, now=NOW)

    future_week = _snapshot()
    future_week["jobs"] = {
        "monitor": {
            "scheduled_for": "2026-08-24T08:00:00Z",
            "state": "scheduled",
        },
        "email": {
            "scheduled_for": "2026-08-24T09:00:00Z",
            "state": "scheduled",
        },
        "publisher": {
            "scheduled_for": "2026-08-24T10:00:00Z",
            "state": "scheduled",
        },
    }
    with pytest.raises(JobStatusInvalidSnapshotError):
        validate_snapshot(future_week, now=NOW)

    monday_morning = _snapshot()
    monday_morning["generated_at"] = "2026-08-17T08:30:00Z"
    monday_morning["jobs"]["monitor"] = {
        "scheduled_for": "2026-08-17T08:00:00Z",
        "state": "running",
        "claimed_at": "2026-08-17T08:00:01Z",
        "started_at": "2026-08-17T08:00:02Z",
    }
    monday_morning["jobs"]["email"] = {
        "scheduled_for": "2026-08-17T09:00:00Z",
        "state": "scheduled",
    }
    assert validate_snapshot(monday_morning, now=NOW) == monday_morning


def test_contract_rejects_duplicate_json_keys_and_pathological_json():
    with pytest.raises(JobStatusInvalidSnapshotError):
        decode_snapshot_json(
            '{"schema_version":"weekly-job-status.v1",'
            '"schema_version":"other","generated_at":"2026-08-17T11:55:00Z",'
            '"jobs":{}}',
            now=NOW,
        )
    with pytest.raises(JobStatusInvalidSnapshotError):
        decode_snapshot_json('{"number":' + ("9" * 5000) + "}", now=NOW)
    with pytest.raises(JobStatusInvalidSnapshotError):
        decode_snapshot_json("[" * 2000 + "]" * 2000, now=NOW)
    with pytest.raises(JobStatusInvalidSnapshotError):
        decode_snapshot_json("\ud800", now=NOW)


def test_reader_reports_observer_staleness_at_strict_boundary(tmp_path):
    directory = tmp_path / "status"
    _write_snapshot(directory, _snapshot())
    reader = JobStatusSnapshotReader(directory, repository_root=ROOT)
    generated = datetime(2026, 8, 17, 11, 55, tzinfo=timezone.utc)

    current = reader.status(
        now=generated + timedelta(minutes=DEFAULT_STALE_AFTER_MINUTES)
    )
    stale = reader.status(
        now=generated + timedelta(minutes=DEFAULT_STALE_AFTER_MINUTES, seconds=1)
    )
    subsecond = reader.status(
        now=generated
        + timedelta(minutes=DEFAULT_STALE_AFTER_MINUTES, milliseconds=500)
    )

    assert current["available"] is True
    assert current["observer"] == {
        "is_stale": False,
        "age_seconds": DEFAULT_STALE_AFTER_MINUTES * 60,
        "max_age_seconds": DEFAULT_STALE_AFTER_MINUTES * 60,
    }
    assert subsecond["observer"] == {
        "is_stale": False,
        "age_seconds": DEFAULT_STALE_AFTER_MINUTES * 60,
        "max_age_seconds": DEFAULT_STALE_AFTER_MINUTES * 60,
    }
    assert stale["observer"]["is_stale"] is True
    assert stale["observer"]["age_seconds"] == 901
    assert stale["jobs"] == _snapshot()["jobs"]


def test_reader_reopens_snapshot_and_observes_atomic_replacement(tmp_path):
    directory = tmp_path / "status"
    target = _write_snapshot(directory, _snapshot())
    reader = JobStatusSnapshotReader(directory, repository_root=ROOT)
    assert reader.status(now=NOW)["jobs"]["publisher"]["state"] == "scheduled"

    replacement = _snapshot()
    replacement["jobs"]["publisher"] = {
        "scheduled_for": "2026-08-17T10:00:00Z",
        "state": "running",
        "claimed_at": "2026-08-17T10:00:01Z",
        "started_at": "2026-08-17T10:00:02Z",
    }
    temporary = directory / "replacement.json"
    temporary.write_text(json.dumps(replacement), encoding="utf-8")
    os.replace(temporary, target)

    assert reader.status(now=NOW)["jobs"]["publisher"]["state"] == "running"
    assert sorted(path.name for path in directory.iterdir()) == ["scheduler-status.json"]


def test_reader_rejects_relative_inside_repo_missing_and_nondirectory_locations(tmp_path):
    with pytest.raises(JobStatusLocationError):
        JobStatusSnapshotReader("relative", repository_root=ROOT)
    with pytest.raises(JobStatusLocationError):
        JobStatusSnapshotReader(ROOT / "status", repository_root=ROOT)
    traversal = tmp_path / "child" / ".." / "status"
    with pytest.raises(JobStatusLocationError):
        JobStatusSnapshotReader(traversal, repository_root=ROOT)
    with pytest.raises(JobStatusUnavailableError):
        JobStatusSnapshotReader(tmp_path / "missing", repository_root=ROOT)
    regular_file = tmp_path / "file"
    regular_file.write_text("x", encoding="utf-8")
    with pytest.raises(JobStatusLocationError):
        JobStatusSnapshotReader(regular_file, repository_root=ROOT)
    with pytest.raises(JobStatusLocationError):
        JobStatusSnapshotReader(regular_file / "nested", repository_root=ROOT)


@pytest.mark.parametrize(
    ("path_error", "expected_error"),
    [
        (OSError(errno.ELOOP, "loop"), JobStatusLocationError),
        (NotADirectoryError(errno.ENOTDIR, "not a directory"), JobStatusLocationError),
        (PermissionError(errno.EACCES, "unreadable"), JobStatusUnavailableError),
    ],
)
def test_configured_parent_error_taxonomy(
    tmp_path, monkeypatch, path_error, expected_error
):
    configured = tmp_path / "status"

    def fail_lstat(*_args, **_kwargs):
        raise path_error

    monkeypatch.setattr(job_status.os, "lstat", fail_lstat)
    with pytest.raises(expected_error):
        JobStatusSnapshotReader(configured, repository_root=ROOT)


def test_reader_rejects_looped_parent_link_where_supported(tmp_path):
    loop = tmp_path / "loop"
    try:
        loop.symlink_to(loop, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(JobStatusLocationError):
        JobStatusSnapshotReader(loop / "nested", repository_root=ROOT)


def test_reader_rejects_symlink_components_and_snapshot_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(JobStatusLocationError):
        JobStatusSnapshotReader(linked, repository_root=ROOT)

    target = tmp_path / "payload.json"
    target.write_text(json.dumps(_snapshot()), encoding="utf-8")
    (real / "scheduler-status.json").symlink_to(target)
    reader = JobStatusSnapshotReader(real, repository_root=ROOT)
    with pytest.raises(JobStatusInvalidSnapshotError):
        reader.status(now=NOW)


def test_windows_reparse_metadata_is_rejected_platform_independently():
    metadata = SimpleNamespace(
        st_mode=stat.S_IFDIR,
        st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
    )
    assert job_status._metadata_is_link_or_reparse(metadata) is True


def test_reader_rejects_nonregular_oversize_and_invalid_content(tmp_path):
    directory = tmp_path / "status"
    directory.mkdir()
    target = directory / "scheduler-status.json"
    target.mkdir()
    reader = JobStatusSnapshotReader(directory, repository_root=ROOT)
    with pytest.raises(JobStatusInvalidSnapshotError):
        reader.status(now=NOW)

    target.rmdir()
    target.write_bytes(b"{" + b" " * MAX_SNAPSHOT_BYTES + b"}")
    with pytest.raises(JobStatusInvalidSnapshotError):
        reader.status(now=NOW)

    target.write_text("not json", encoding="utf-8")
    with pytest.raises(JobStatusInvalidSnapshotError):
        reader.status(now=NOW)


@pytest.mark.parametrize(
    ("open_error", "expected_error"),
    [
        (OSError(errno.ELOOP, "symlink race"), JobStatusInvalidSnapshotError),
        (
            NotADirectoryError(errno.ENOTDIR, "parent changed"),
            JobStatusInvalidSnapshotError,
        ),
        (FileNotFoundError(errno.ENOENT, "missing"), JobStatusUnavailableError),
        (PermissionError(errno.EACCES, "unreadable"), JobStatusUnavailableError),
    ],
)
def test_reader_classifies_nofollow_race_separately_from_unreadable(
    tmp_path, monkeypatch, open_error, expected_error
):
    directory = tmp_path / "status"
    _write_snapshot(directory, _snapshot())

    def fail_open(*_args, **_kwargs):
        raise open_error

    monkeypatch.setattr(job_status.os, "open", fail_open)
    with pytest.raises(expected_error):
        JobStatusSnapshotReader(directory, repository_root=ROOT).status(now=NOW)


_PREOPEN_ERROR_CASES = [
    ("eloop", lambda: OSError(errno.ELOOP, "loop"), JobStatusInvalidSnapshotError),
    (
        "enotdir",
        lambda: NotADirectoryError(errno.ENOTDIR, "not a directory"),
        JobStatusInvalidSnapshotError,
    ),
    (
        "enoent",
        lambda: FileNotFoundError(errno.ENOENT, "missing"),
        JobStatusUnavailableError,
    ),
    (
        "eacces",
        lambda: PermissionError(errno.EACCES, "unreadable"),
        JobStatusUnavailableError,
    ),
]


@pytest.mark.parametrize(
    ("_case", "error_factory", "expected_error"),
    _PREOPEN_ERROR_CASES,
)
def test_read_phase_initial_parent_metadata_error_taxonomy(
    tmp_path, monkeypatch, _case, error_factory, expected_error
):
    directory = tmp_path / "status"
    _write_snapshot(directory, _snapshot())
    original_lstat = os.lstat

    def fail_parent(path, *args, **kwargs):
        if Path(path) == directory:
            raise error_factory()
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(job_status.os, "lstat", fail_parent)
    with pytest.raises(expected_error):
        job_status._read_snapshot_file(directory)


@pytest.mark.parametrize(
    ("_case", "error_factory", "expected_error"),
    _PREOPEN_ERROR_CASES,
)
def test_anchored_relative_metadata_error_taxonomy(
    monkeypatch, _case, error_factory, expected_error
):
    def fail_stat(*_args, **_kwargs):
        raise error_factory()

    monkeypatch.setattr(job_status, "_raw_relative_metadata", fail_stat)
    with pytest.raises(expected_error):
        job_status._relative_metadata(123, "scheduler-status.json")


@pytest.mark.parametrize(
    ("_case", "error_factory", "expected_error"),
    _PREOPEN_ERROR_CASES,
)
def test_nonanchored_leaf_metadata_error_taxonomy(
    tmp_path, monkeypatch, _case, error_factory, expected_error
):
    directory = tmp_path / "status"
    target = _write_snapshot(directory, _snapshot())
    original_lstat = os.lstat

    def fail_leaf(path, *args, **kwargs):
        if Path(path) == target:
            raise error_factory()
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(job_status, "_SUPPORTS_ANCHORED_DIRECTORY_OPEN", False)
    monkeypatch.setattr(job_status.os, "lstat", fail_leaf)
    with pytest.raises(expected_error):
        job_status._read_snapshot_file(directory)


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO semantics")
def test_reader_rejects_fifo_without_blocking(tmp_path):
    directory = tmp_path / "status"
    directory.mkdir()
    os.mkfifo(directory / "scheduler-status.json")
    with pytest.raises(JobStatusInvalidSnapshotError):
        JobStatusSnapshotReader(directory, repository_root=ROOT).status(now=NOW)


@pytest.mark.skipif(os.name != "posix", reason="POSIX device file")
def test_bounded_reader_rejects_device_file():
    metadata = os.lstat("/dev/null")
    assert not stat.S_ISREG(metadata.st_mode)
    with pytest.raises(JobStatusInvalidSnapshotError):
        job_status._read_snapshot_file(Path("/dev"), filename="null")


@pytest.mark.parametrize("mutation", ["grow", "truncate", "replace"])
def test_reader_rejects_file_mutation_during_read(tmp_path, monkeypatch, mutation):
    directory = tmp_path / "status"
    target = _write_snapshot(directory, _snapshot())
    original_read = os.read
    changed = False

    def mutate_after_read(descriptor, count):
        nonlocal changed
        chunk = original_read(descriptor, count)
        if chunk and not changed:
            changed = True
            try:
                if mutation == "grow":
                    with target.open("ab") as stream:
                        stream.write(b" ")
                elif mutation == "truncate":
                    target.write_bytes(b"")
                else:
                    replacement = directory / "replacement.json"
                    replacement.write_text(json.dumps(_snapshot()), encoding="utf-8")
                    os.replace(replacement, target)
            except PermissionError:
                pytest.skip("platform does not allow mutating an open file")
        return chunk

    monkeypatch.setattr(job_status.os, "read", mutate_after_read)
    with pytest.raises(JobStatusInvalidSnapshotError):
        JobStatusSnapshotReader(directory, repository_root=ROOT).status(now=NOW)


def test_reader_rejects_parent_identity_swap(tmp_path, monkeypatch):
    directory = tmp_path / "status"
    _write_snapshot(directory, _snapshot())
    original_metadata = job_status._snapshot_directory_metadata
    directory_calls = 0

    class ChangedIdentity:
        def __init__(self, metadata):
            self._metadata = metadata
            self.st_ino = metadata.st_ino + 1
            self.st_dev = metadata.st_dev

        def __getattr__(self, name):
            return getattr(self._metadata, name)

    def changed_parent_identity(path, *args, **kwargs):
        nonlocal directory_calls
        metadata = original_metadata(path, *args, **kwargs)
        directory_calls += 1
        if directory_calls >= 2:
            return ChangedIdentity(metadata)
        return metadata

    monkeypatch.setattr(job_status, "_snapshot_directory_metadata", changed_parent_identity)
    with pytest.raises(JobStatusInvalidSnapshotError):
        JobStatusSnapshotReader(directory, repository_root=ROOT).status(now=NOW)


def test_reader_closes_parent_and_snapshot_descriptors(tmp_path, monkeypatch):
    directory = tmp_path / "status"
    _write_snapshot(directory, _snapshot())
    original_open = os.open
    descriptors: list[int] = []

    def capture_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(job_status.os, "open", capture_open)
    JobStatusSnapshotReader(directory, repository_root=ROOT).status(now=NOW)
    anchored = job_status._SUPPORTS_ANCHORED_DIRECTORY_OPEN
    assert len(descriptors) >= (2 if anchored else 1)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_reader_closes_descriptors_after_read_error(tmp_path, monkeypatch):
    directory = tmp_path / "status"
    _write_snapshot(directory, _snapshot())
    original_open = os.open
    descriptors: list[int] = []

    def capture_open(*args, **kwargs):
        descriptor = original_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(job_status.os, "open", capture_open)
    monkeypatch.setattr(
        job_status.os,
        "read",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read failed")),
    )
    with pytest.raises(JobStatusUnavailableError):
        JobStatusSnapshotReader(directory, repository_root=ROOT).status(now=NOW)
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_reader_does_not_write_or_cache_snapshot(tmp_path):
    directory = tmp_path / "status"
    target = _write_snapshot(directory, _snapshot())
    before = (target.stat().st_size, target.stat().st_mtime_ns, target.read_bytes())

    JobStatusSnapshotReader(directory, repository_root=ROOT).status(now=NOW)

    after = (target.stat().st_size, target.stat().st_mtime_ns, target.read_bytes())
    assert after == before
    assert list(directory.iterdir()) == [target]
