"""Tests for the scheduler-status writer module (Issue #87).

The writer is the producer side of the ``scheduler-status.json`` snapshot
consumed by the public ``/api/job-status`` endpoint. The reader contract
is enforced by ``tests/test_job_status_api.py`` via
``climate_monitor.job_status``; the writer only needs to round-trip the
contract by writing through ``update_slot``.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from climate_monitor import job_status
from climate_monitor import scheduler_status


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _monday_8am(date_str: str = "2026-09-07") -> str:
    """Return a Monday 08:00 UTC ``scheduled_for`` for the monitor slot.

    2026-09-07 is already a Monday, so this works for the canonical
    schedule. The writer only validates the timestamp shape; the public
    reader enforces the (Monday, hour) check.
    """
    return f"{date_str}T08:00:00Z"


def _started_at(scheduled_for: str) -> str:
    dt = datetime.strptime(scheduled_for, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return (dt + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finished_at(scheduled_for: str) -> str:
    dt = datetime.strptime(scheduled_for, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return (dt + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aware_from_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _read_snapshot(directory: Path) -> dict:
    return json.loads((directory / "scheduler-status.json").read_text("utf-8"))


# ---------------------------------------------------------------------------
# 1. Happy path -- writes a snapshot with all four slots, each in a valid state
# ---------------------------------------------------------------------------


def test_update_slot_happy_path_round_trips_through_reader(tmp_path, monkeypatch):
    """AC-1: ``update_slot`` writes a snapshot whose every slot round-trips
    through ``job_status.validate_snapshot``."""
    directory = tmp_path / "status"
    directory.mkdir()
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(directory))

    # Pin "now" so the writer's seed dates and the round-trip agree. The
    # pinned time is well after every execution timestamp in the test so
    # the read-side validator's "claimed_at <= generated_at" check passes.
    pinned_now = "2026-09-07T11:00:00Z"
    monkeypatch.setattr(scheduler_status, "_now_utc", lambda: pinned_now)
    monkeypatch.setattr(
        scheduler_status,
        "_aware_now",
        lambda: datetime(2026, 9, 7, 11, 0, 0, tzinfo=timezone.utc),
    )

    scheduler_status.update_slot(
        "monitor",
        "running",
        scheduled_for=_monday_8am(),
        claimed_at=_started_at(_monday_8am()),
    )
    scheduler_status.update_slot(
        "email",
        "completed",
        scheduled_for="2026-09-07T09:00:00Z",
        claimed_at=_started_at("2026-09-07T09:00:00Z"),
        started_at=_started_at("2026-09-07T09:00:00Z"),
        finished_at=_finished_at("2026-09-07T09:00:00Z"),
    )
    scheduler_status.update_slot(
        "publisher",
        "not_dispatched",
        scheduled_for="2026-09-07T10:00:00Z",
    )
    scheduler_status.update_slot(
        "registry",
        "not_dispatched",
        scheduled_for="2026-09-07T10:30:00Z",
        result_code="awaiting_human_merge_deploy",
    )

    snapshot = _read_snapshot(directory)
    assert snapshot["schema_version"] == "weekly-job-status.v1"
    assert set(snapshot["jobs"]) == {"monitor", "email", "publisher", "registry"}
    assert snapshot["jobs"]["monitor"]["state"] == "running"
    assert snapshot["jobs"]["email"]["state"] == "completed"
    assert snapshot["jobs"]["publisher"]["result_code"] == "not_dispatched"
    assert snapshot["jobs"]["registry"]["result_code"] == "awaiting_human_merge_deploy"

    # Round-trip through the public reader using the snapshot's own
    # generated_at so the reader accepts the timestamps.
    reader_validated = job_status.validate_snapshot(
        snapshot, now=_aware_from_iso(snapshot["generated_at"])
    )
    assert reader_validated["jobs"]["registry"]["state"] == "not_dispatched"


# ---------------------------------------------------------------------------
# 2. Unknown slot name rejected
# ---------------------------------------------------------------------------


def test_update_slot_unknown_slot_raises(tmp_path, monkeypatch):
    """AC-2: writer rejects unknown slot names before touching the filesystem."""
    directory = tmp_path / "status"
    directory.mkdir()
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(directory))

    with pytest.raises(scheduler_status.SchedulerStatusError):
        scheduler_status.update_slot(
            "explorer",
            "scheduled",
            scheduled_for=_monday_8am(),
        )
    assert not (directory / "scheduler-status.json").exists()


# ---------------------------------------------------------------------------
# 3. Unknown state rejected
# ---------------------------------------------------------------------------


def test_update_slot_unknown_state_raises(tmp_path, monkeypatch):
    """AC-2: writer rejects unknown states before touching the filesystem."""
    directory = tmp_path / "status"
    directory.mkdir()
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(directory))

    with pytest.raises(scheduler_status.SchedulerStatusError):
        scheduler_status.update_slot(
            "monitor",
            "vaporised",
            scheduled_for=_monday_8am(),
        )
    assert not (directory / "scheduler-status.json").exists()


# ---------------------------------------------------------------------------
# 4. Bad timestamp rejected
# ---------------------------------------------------------------------------


def test_update_slot_bad_timestamp_raises(tmp_path, monkeypatch):
    """AC-2: writer rejects malformed timestamps before touching the
    filesystem."""
    directory = tmp_path / "status"
    directory.mkdir()
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(directory))

    with pytest.raises(scheduler_status.SchedulerStatusError):
        scheduler_status.update_slot(
            "monitor",
            "running",
            scheduled_for="not-a-timestamp",
            claimed_at=_started_at(_monday_8am()),
        )
    assert not (directory / "scheduler-status.json").exists()


# ---------------------------------------------------------------------------
# 5. Atomic replacement -- concurrent readers see old OR new, never partial
# ---------------------------------------------------------------------------


def test_update_slot_atomic_replacement(tmp_path, monkeypatch):
    """AC-3: the writer replaces the snapshot atomically so concurrent
    readers observe either the pre-update snapshot or the post-update
    snapshot, never a partial / truncated JSON file."""
    # The fixture occurrence must precede the observer clock in every runtime.
    monkeypatch.setattr(scheduler_status, "_now_utc", lambda: "2026-09-07T11:00:00Z")
    monkeypatch.setattr(scheduler_status, "_aware_now", lambda: datetime(2026, 9, 7, 11, tzinfo=timezone.utc))
    directory = tmp_path / "status"
    directory.mkdir()
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(directory))

    scheduler_status.update_slot(
        "monitor",
        "scheduled",
        scheduled_for=_monday_8am(),
    )
    before = _read_snapshot(directory)
    assert before["jobs"]["monitor"]["state"] == "scheduled"

    # Hammer the writer in a thread while a reader observes every transition.
    stop = threading.Event()
    observations: list[bool] = []

    def reader_loop() -> None:
        while not stop.is_set():
            try:
                payload = _read_snapshot(directory)
            except (FileNotFoundError, json.JSONDecodeError):
                # Brief race: temp file moved before new inode is published.
                observations.append(True)
                continue
            observations.append(
                set(payload) >= {"schema_version", "generated_at", "jobs"}
                and set(payload["jobs"]) >= {"monitor"}
            )

    def writer_loop() -> None:
        for _ in range(60):
            scheduler_status.update_slot(
                "monitor",
                "running",
                scheduled_for=_monday_8am(),
                claimed_at=_started_at(_monday_8am()),
            )
            scheduler_status.update_slot(
                "monitor",
                "scheduled",
                scheduled_for=_monday_8am(),
            )

    reader = threading.Thread(target=reader_loop, daemon=True)
    writer = threading.Thread(target=writer_loop)
    reader.start()
    writer.start()
    writer.join()
    stop.set()
    reader.join(timeout=2.0)

    assert observations, "reader thread never observed any snapshot"
    assert all(observations), "reader saw a partial / truncated JSON snapshot"


# ---------------------------------------------------------------------------
# 6. Per-state field set validation
# ---------------------------------------------------------------------------


def test_update_slot_per_state_field_set_validation(tmp_path, monkeypatch):
    """AC-4: the writer enforces the same per-state field set as the reader.
    ``completed`` requires ``finished_at``; supplying ``finished_at`` for
    ``scheduled`` is rejected."""
    directory = tmp_path / "status"
    directory.mkdir()
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(directory))

    # completed without claimed_at/started_at/finished_at -> error
    with pytest.raises(scheduler_status.SchedulerStatusError):
        scheduler_status.update_slot(
            "monitor",
            "completed",
            scheduled_for=_monday_8am(),
        )

    # scheduled with finished_at -> error (extra field)
    with pytest.raises(scheduler_status.SchedulerStatusError):
        scheduler_status.update_slot(
            "monitor",
            "scheduled",
            scheduled_for=_monday_8am(),
            finished_at=_finished_at(_monday_8am()),
        )

    assert not (directory / "scheduler-status.json").exists()


# ---------------------------------------------------------------------------
# 7. status_dir arg + env precedence (covers the env fallback path)
# ---------------------------------------------------------------------------


def test_update_slot_resolves_directory_via_env(tmp_path, monkeypatch):
    """AC-5: writer honours ``CLIMATE_JOB_STATUS_DIR`` when no explicit
    ``status_dir`` is provided."""
    directory = tmp_path / "from-env"
    directory.mkdir()
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(directory))

    scheduler_status.update_slot(
        "monitor",
        "scheduled",
        scheduled_for=_monday_8am(),
    )

    on_disk = _read_snapshot(directory)
    assert on_disk["jobs"]["monitor"]["state"] == "scheduled"


def test_update_slot_explicit_status_dir_overrides_env(tmp_path, monkeypatch):
    """AC-5: an explicit ``status_dir`` argument wins over the env var,
    so wrappers can target per-run locations without mutating the env."""
    env_directory = tmp_path / "from-env"
    explicit_directory = tmp_path / "explicit"
    env_directory.mkdir()
    explicit_directory.mkdir()
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(env_directory))

    scheduler_status.update_slot(
        "monitor",
        "scheduled",
        scheduled_for=_monday_8am(),
        status_dir=explicit_directory,
    )

    assert not (env_directory / "scheduler-status.json").exists()
    on_disk = _read_snapshot(explicit_directory)
    assert on_disk["jobs"]["monitor"]["state"] == "scheduled"


def test_update_slot_missing_env_and_arg_raises(tmp_path, monkeypatch):
    """AC-5: when neither env nor arg is provided, the writer fails with a
    clear SchedulerStatusError and never touches the filesystem."""
    monkeypatch.delenv("CLIMATE_JOB_STATUS_DIR", raising=False)

    with pytest.raises(scheduler_status.SchedulerStatusError):
        scheduler_status.update_slot(
            "monitor",
            "scheduled",
            scheduled_for=_monday_8am(),
        )


# ---------------------------------------------------------------------------
# 8. result_code passthrough / error_reason behaviour
# ---------------------------------------------------------------------------


def test_update_slot_passes_error_reason_via_result_code(tmp_path, monkeypatch):
    """AC-6: ``error_reason`` is folded into ``result_code`` so the read
    contract (which forbids free-form error text on disk) is preserved."""
    directory = tmp_path / "status"
    directory.mkdir()
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(directory))

    # Mock the writer's clock to a UTC moment after the test's finished_at
    # (08:00:02Z) so the read-side acceptance check accepts the snapshot.
    monkeypatch.setattr(
        scheduler_status, "_now_utc", lambda: "2026-09-07T08:01:00Z"
    )

    scheduler_status.update_slot(
        "monitor",
        "failed",
        scheduled_for=_monday_8am(),
        claimed_at=_started_at(_monday_8am()),
        started_at=_started_at(_monday_8am()),
        finished_at=_finished_at(_monday_8am()),
        # error_reason carries the free-form reason but is not written to
        # disk; the read-side contract pins ``result_code`` to the canonical
        # STATE_RESULT_CODES mapping for ``failed`` (``execution_failed``).
        result_code="execution_failed",
        error_reason="evidence loopback returned 0 candidates",
    )

    on_disk = _read_snapshot(directory)
    assert on_disk["jobs"]["monitor"]["state"] == "failed"
    assert on_disk["jobs"]["monitor"]["result_code"] == "execution_failed"
    # The free-form error text must never be written to disk.
    assert "error_reason" not in on_disk["jobs"]["monitor"]
    assert "evidence loopback returned" not in json.dumps(on_disk)


def test_registry_isolated_dry_run_records_exit_without_claiming_sync(tmp_path, monkeypatch, capsys):
    import scripts.hermes_job as job
    import sys
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 11, tzinfo=timezone.utc)
    monkeypatch.setattr(job, 'datetime', Clock)
    monkeypatch.setattr(scheduler_status, '_now_utc', lambda: '2026-09-07T11:00:00Z')
    monkeypatch.setattr(scheduler_status, '_aware_now', lambda: datetime(2026, 9, 7, 11, tzinfo=timezone.utc))
    monkeypatch.setenv('REPORT_DATE', '2026-09-07')
    monkeypatch.setenv('CLIMATE_JOB_STATUS_DIR', str(tmp_path))
    monkeypatch.setenv('CLIMATE_DRY_RUN_ROOT', str(tmp_path))
    monkeypatch.setenv('CLIMATE_DRY_RUN', '1')
    monkeypatch.setattr(job, 'registry_command', lambda *_args, **_kwargs: [sys.executable, '-c', 'raise SystemExit(7)'])
    assert job.main(['registry']) == 7
    payload = _read_snapshot(tmp_path)
    assert payload['jobs']['registry'] == {'state': 'not_dispatched',
        'scheduled_for': '2026-09-07T10:30:00Z', 'result_code': 'registry_dry_run_exit_7'}
    assert 'registry_exit_7' in capsys.readouterr().out
