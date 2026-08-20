from __future__ import annotations

import json
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest
import climate_monitor.run_ledger as run_ledger

from climate_monitor.run_ledger import (
    MAX_ATTEMPT_BYTES,
    MAX_ATTEMPT_COUNT,
    LedgerConflictError,
    LedgerContractError,
    LedgerLocationError,
    LedgerUnavailableError,
    RunLedgerReader,
    _assert_directory_contained,
    append_attempt,
    append_attempt_repair,
    build_report_identity,
    canonical_attempt_bytes,
    decode_attempt_json,
    read_bounded_file,
    validate_report_identity,
)


ROOT = Path(__file__).resolve().parents[1]


def test_public_resource_contract_covers_long_retention_without_weakening_scan_bounds():
    assert MAX_ATTEMPT_BYTES == 128 * 1024
    assert MAX_ATTEMPT_COUNT == 20_000


def _attempt(
    *,
    attempt_id: str = "20260810t080000z-attempt-01",
    stage: str = "monitor",
    status: str = "success",
    result_code: str = "report_written",
    scheduled_for: str = "2026-08-10T08:00:00Z",
    finished_at: str = "2026-08-10T08:30:00Z",
    include_sources: bool = True,
) -> dict:
    payload = {
        "schema_version": "weekly-run-attempt.v1",
        "attempt_id": attempt_id,
        "stage": stage,
        "report_date": "2026-08-10",
        "scheduled_for": scheduled_for,
        "finished_at": finished_at,
        "status": status,
        "result_code": result_code,
        "report": {
            "report_id": "climate-monitor-2026-08-10",
            "report_date": "2026-08-10",
            "sha256": "a" * 64,
        },
        "registry_revision": {
            "namespace": "web-listening:source-registry",
            "revision": "sha256:" + "b" * 64,
        },
    }
    if include_sources:
        source_outcomes = {
            "success": (1, 3, 0, 0, []),
            "no_change": (0, 4, 0, 0, []),
            "partial": (
                1,
                1,
                1,
                1,
                [
                    {"source_id": "imf", "status": "blocked", "error_code": "anti_bot"},
                    {"source_id": "unep", "status": "failed", "error_code": "timeout"},
                ],
            ),
            "failed": (
                0,
                0,
                2,
                2,
                [
                    {"source_id": "imf", "status": "blocked", "error_code": "anti_bot"},
                    {"source_id": "oecd", "status": "blocked", "error_code": "robots"},
                    {"source_id": "unep", "status": "failed", "error_code": "timeout"},
                    {"source_id": "unfccc", "status": "failed", "error_code": "http_500"},
                ],
            ),
        }
        updated, unchanged, failed, blocked, failures = source_outcomes[status]
        payload["sources"] = {
            "total": 4,
            "updated": updated,
            "unchanged": unchanged,
            "failed": failed,
            "blocked": blocked,
            "failures": failures,
        }
    return payload


def test_append_is_atomic_append_only_and_idempotent(tmp_path):
    ledger = tmp_path / "ledger"
    payload = _attempt()

    created = append_attempt(ledger, payload, repository_root=ROOT)
    repeated = append_attempt(ledger, payload, repository_root=ROOT)

    assert created == "created"
    assert repeated == "already_exists"
    files = list(ledger.rglob("*.json"))
    assert len(files) == 1
    assert files[0].read_bytes() == canonical_attempt_bytes(payload)
    assert not list(ledger.rglob("*.tmp"))

    conflict = _attempt(result_code="different_result")
    with pytest.raises(LedgerConflictError, match="attempt identity conflict"):
        append_attempt(ledger, conflict, repository_root=ROOT)
    assert files[0].read_bytes() == canonical_attempt_bytes(payload)


def test_shared_report_identity_binds_date_filename_id_and_raw_sha():
    raw_lf = b"line one\nline two\n"
    raw_crlf = b"line one\r\nline two\r\n"
    identity = build_report_identity(
        report_date="2026-08-10",
        filename="climate-monitor-2026-08-10.md",
        sha256=hashlib.sha256(raw_lf).hexdigest(),
    )
    assert identity.report_id == "climate-monitor-2026-08-10"
    assert identity.sha256 != hashlib.sha256(raw_crlf).hexdigest()
    assert validate_report_identity(identity.as_record()) == identity
    with pytest.raises(LedgerContractError, match="filename"):
        build_report_identity(
            report_date="2026-08-10",
            filename="climate-monitor-2026-08-03.md",
            sha256=identity.sha256,
        )
    with pytest.raises(LedgerContractError, match="filename"):
        validate_report_identity(
            {**identity.as_record(), "report_id": "climate-monitor-2026-08-03"}
        )


def test_repair_overlay_is_raw_bound_append_only_and_publicly_transparent(tmp_path):
    ledger = tmp_path / "ledger"
    attempt = _attempt(
        attempt_id="20260810t100000z-publisher-legacy",
        stage="publisher",
        status="no_change",
        result_code="no-op",
        scheduled_for="2026-08-10T10:00:00Z",
        finished_at="2026-08-10T10:05:00Z",
        include_sources=False,
    )
    attempt.pop("report")
    append_attempt(ledger, attempt, repository_root=ROOT)
    original = ledger / "attempts" / "publisher" / "2026-08-10" / f"{attempt['attempt_id']}.json"
    claim = ledger / ".attempt-identities" / f"{attempt['attempt_id']}.claim"
    original_raw = original.read_bytes()
    original_stat = os.stat(original)
    claim_stat = os.stat(claim)
    repair = {
        "schema_version": "weekly-run-attempt-repair.v1",
        "attempt_id": attempt["attempt_id"],
        "original_sha256": hashlib.sha256(original_raw).hexdigest(),
        "report": {
            "report_id": "climate-monitor-2026-08-10",
            "report_date": "2026-08-10",
            "sha256": "a" * 64,
        },
    }
    assert append_attempt_repair(ledger, repair, repository_root=ROOT) == "created"
    assert append_attempt_repair(ledger, repair, repository_root=ROOT) == "already_exists"
    loaded = RunLedgerReader(ledger, repository_root=ROOT)._load()
    assert loaded[-1]["report"] == repair["report"]
    assert original.read_bytes() == original_raw == claim.read_bytes()
    assert (os.stat(original).st_dev, os.stat(original).st_ino) == (
        original_stat.st_dev,
        original_stat.st_ino,
    )
    assert (os.stat(claim).st_dev, os.stat(claim).st_ino) == (
        claim_stat.st_dev,
        claim_stat.st_ino,
    )
    public = RunLedgerReader(ledger, repository_root=ROOT).status(
        now=datetime(2026, 8, 10, 11, tzinfo=timezone.utc)
    )
    assert public["stages"]["publisher"]["last_success"]["report"] == repair["report"]
    assert ".attempt-repairs" not in json.dumps(public)


def test_repair_overlay_conflict_and_wrong_raw_binding_fail_closed(tmp_path):
    ledger = tmp_path / "ledger"
    attempt = _attempt(
        attempt_id="20260810t100000z-publisher-legacy",
        stage="publisher",
        status="success",
        scheduled_for="2026-08-10T10:00:00Z",
        finished_at="2026-08-10T10:05:00Z",
        include_sources=False,
    )
    attempt.pop("report")
    append_attempt(ledger, attempt, repository_root=ROOT)
    repair = {
        "schema_version": "weekly-run-attempt-repair.v1",
        "attempt_id": attempt["attempt_id"],
        "original_sha256": "b" * 64,
        "report": {
            "report_id": "climate-monitor-2026-08-10",
            "report_date": "2026-08-10",
            "sha256": "a" * 64,
        },
    }
    append_attempt_repair(ledger, repair, repository_root=ROOT)
    with pytest.raises(LedgerContractError, match="target is invalid"):
        RunLedgerReader(ledger, repository_root=ROOT)._load()
    with pytest.raises(LedgerConflictError):
        append_attempt_repair(
            ledger,
            {**repair, "original_sha256": "c" * 64},
            repository_root=ROOT,
        )


def test_generic_v1_report_id_remains_backward_compatible(tmp_path):
    ledger = tmp_path / "ledger"
    attempt = _attempt(include_sources=False)
    attempt["report"]["report_id"] = "legacy-monitor-report"
    append_attempt(ledger, attempt, repository_root=ROOT)

    loaded = RunLedgerReader(ledger, repository_root=ROOT)._load()
    assert loaded[0]["report"]["report_id"] == "legacy-monitor-report"


@pytest.mark.parametrize("new_status", ["success", "failed"])
def test_repair_overlay_survives_later_same_date_attempt(tmp_path, new_status):
    ledger = tmp_path / "ledger"
    legacy = _attempt(
        attempt_id="20260810t100000z-publisher-legacy",
        stage="publisher",
        status="no_change",
        result_code="no-op",
        scheduled_for="2026-08-10T10:00:00Z",
        finished_at="2026-08-10T10:05:00Z",
        include_sources=False,
    )
    legacy.pop("report")
    append_attempt(ledger, legacy, repository_root=ROOT)
    original = next((ledger / "attempts" / "publisher" / "2026-08-10").glob("*.json"))
    report = {
        "report_id": "climate-monitor-2026-08-10",
        "report_date": "2026-08-10",
        "sha256": "a" * 64,
    }
    append_attempt_repair(
        ledger,
        {
            "schema_version": "weekly-run-attempt-repair.v1",
            "attempt_id": legacy["attempt_id"],
            "original_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
            "report": report,
        },
        repository_root=ROOT,
    )
    newer = _attempt(
        attempt_id=f"20260810t101000z-publisher-{new_status}",
        stage="publisher",
        status=new_status,
        result_code="retry",
        scheduled_for="2026-08-10T10:00:00Z",
        finished_at="2026-08-10T10:10:00Z",
        include_sources=False,
    )
    if new_status == "failed":
        newer.pop("report")
    append_attempt(ledger, newer, repository_root=ROOT)

    loaded = RunLedgerReader(ledger, repository_root=ROOT)._load()
    assert loaded[0]["report"] == report
    assert loaded[-1]["attempt_id"] == newer["attempt_id"]
    assert loaded[-1]["status"] == new_status


def test_interrupted_repair_temp_is_outside_reader_namespace(tmp_path):
    ledger = tmp_path / "ledger"
    append_attempt(ledger, _attempt(), repository_root=ROOT)
    temp_dir = ledger / ".attempt-repair-tmp"
    temp_dir.mkdir()
    (temp_dir / ".interrupted.tmp").write_bytes(b"partial")

    assert len(RunLedgerReader(ledger, repository_root=ROOT)._load()) == 1


def test_repair_link_failure_leaves_original_and_claim_unchanged(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger"
    attempt = _attempt(
        attempt_id="20260810t100000z-publisher-legacy",
        stage="publisher",
        status="success",
        scheduled_for="2026-08-10T10:00:00Z",
        finished_at="2026-08-10T10:05:00Z",
        include_sources=False,
    )
    attempt.pop("report")
    append_attempt(ledger, attempt, repository_root=ROOT)
    original = next((ledger / "attempts" / "publisher" / "2026-08-10").glob("*.json"))
    claim = ledger / ".attempt-identities" / f"{attempt['attempt_id']}.claim"
    original_raw = original.read_bytes()
    original_identity = (os.stat(original).st_dev, os.stat(original).st_ino)
    claim_identity = (os.stat(claim).st_dev, os.stat(claim).st_ino)
    monkeypatch.setattr(os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected")))

    with pytest.raises(LedgerUnavailableError):
        append_attempt_repair(
            ledger,
            {
                "schema_version": "weekly-run-attempt-repair.v1",
                "attempt_id": attempt["attempt_id"],
                "original_sha256": hashlib.sha256(original_raw).hexdigest(),
                "report": {
                    "report_id": "climate-monitor-2026-08-10",
                    "report_date": "2026-08-10",
                    "sha256": "a" * 64,
                },
            },
            repository_root=ROOT,
        )
    assert original.read_bytes() == claim.read_bytes() == original_raw
    assert (os.stat(original).st_dev, os.stat(original).st_ino) == original_identity
    assert (os.stat(claim).st_dev, os.stat(claim).st_ino) == claim_identity
    assert not list((ledger / ".attempt-repairs").rglob("*.json"))
    assert not list((ledger / ".attempt-repair-tmp").glob("*.tmp"))


def test_repair_fsync_failure_leaves_no_overlay_or_temp(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger"
    attempt = _attempt(
        attempt_id="20260810t100000z-publisher-legacy",
        stage="publisher",
        status="success",
        scheduled_for="2026-08-10T10:00:00Z",
        finished_at="2026-08-10T10:05:00Z",
        include_sources=False,
    )
    attempt.pop("report")
    append_attempt(ledger, attempt, repository_root=ROOT)
    original = next((ledger / "attempts" / "publisher" / "2026-08-10").glob("*.json"))
    original_raw = original.read_bytes()
    monkeypatch.setattr(
        run_ledger.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError("injected fsync")),
    )

    with pytest.raises(LedgerUnavailableError):
        append_attempt_repair(
            ledger,
            {
                "schema_version": "weekly-run-attempt-repair.v1",
                "attempt_id": attempt["attempt_id"],
                "original_sha256": hashlib.sha256(original_raw).hexdigest(),
                "report": {
                    "report_id": "climate-monitor-2026-08-10",
                    "report_date": "2026-08-10",
                    "sha256": "a" * 64,
                },
            },
            repository_root=ROOT,
        )
    assert original.read_bytes() == original_raw
    assert not list((ledger / ".attempt-repairs").rglob("*.json"))
    assert not list((ledger / ".attempt-repair-tmp").glob("*.tmp"))

def test_concurrent_identical_writers_create_exactly_one_attempt(tmp_path):
    ledger = tmp_path / "ledger"
    payload = _attempt()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _index: append_attempt(ledger, payload, repository_root=ROOT),
                range(16),
            )
        )

    assert results.count("created") == 1
    assert results.count("already_exists") == 15
    assert len(list(ledger.rglob("*.json"))) == 1
    assert not list(ledger.rglob("*.tmp"))


def test_concurrent_identical_writers_stress_creates_exactly_one_attempt(tmp_path):
    """PR-A regression: heavier concurrency maximizes hard-link ctime churn.

    The business invariant is exactly one attempt for identical payloads, even
    under the link-count ctime bumps that previously triggered spurious
    ``LedgerContractError`` failures (root cause (b)). This does not rely on any
    timestamp resolution -- it verifies the idempotent dedupe outcome.
    """
    ledger = tmp_path / "ledger"
    payload = _attempt()

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(
            pool.map(
                lambda _index: append_attempt(ledger, payload, repository_root=ROOT),
                range(64),
            )
        )

    assert results.count("created") == 1
    assert results.count("already_exists") == 63
    assert len(list(ledger.rglob("*.json"))) == 1
    assert not list(ledger.rglob("*.tmp"))


def test_concurrent_different_payloads_create_distinct_attempts(tmp_path):
    """Different payloads (distinct attempt_id) must each persist exactly once.

    This pins the genuine concurrency semantics: dedupe is keyed on the full
    attempt identity, not just arrival order, and distinct writers never
    collide into a single file.
    """

    def _payload(index: int) -> dict:
        return _attempt(
            attempt_id=f"20260810t080000z-attempt-{index:02d}",
            result_code=f"result_{index}",
        )

    ledger = tmp_path / "ledger"
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(
            pool.map(
                lambda index: append_attempt(
                    ledger, _payload(index), repository_root=ROOT
                ),
                range(32),
            )
        )

    assert results.count("created") == 32
    assert results.count("already_exists") == 0
    assert len(list(ledger.rglob("*.json"))) == 32
    assert not list(ledger.rglob("*.tmp"))


def test_link_failure_rollbacks_residual_temp(tmp_path, monkeypatch):
    """A hard-link failure on the write path must roll back and leave no .tmp.

    This exercises the failure branch of ``append_attempt`` and asserts the
    residual-temporary cleanup contract survives a fault (no broad retry, no
    silent leak).
    """
    ledger = tmp_path / "ledger"
    payload = _attempt()

    state = {"calls": 0}
    real_link = os.link

    def faulty_link(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise OSError("injected link failure")
        return real_link(*args, **kwargs)

    monkeypatch.setattr(os, "link", faulty_link)

    with pytest.raises(LedgerUnavailableError):
        append_attempt(ledger, payload, repository_root=ROOT)
    assert not list(ledger.rglob("*.tmp"))

    # Once the fault clears, a clean write succeeds and still leaves no .tmp.
    assert append_attempt(ledger, payload, repository_root=ROOT) == "created"
    assert not list(ledger.rglob("*.tmp"))


class _StatProxy:
    """Wrap an ``os.stat_result``, overriding selected fields for deterministic
    races. Used to simulate the ctime-on-link-count condition without needing a
    real concurrent process or a mounted overlayfs (which requires root)."""

    def __init__(self, st, **overrides):
        object.__setattr__(self, "_st", st)
        object.__setattr__(self, "_over", overrides)

    def __getattr__(self, name):
        over = object.__getattribute__(self, "_over")
        if name in over:
            return over[name]
        return getattr(object.__getattribute__(self, "_st"), name)


def test_read_bounded_file_ignores_benign_ctime_bump_from_concurrent_link(
    tmp_path, monkeypatch
):
    """PR-A root-cause regression (deterministic).

    A concurrent ``os.link()`` bumps ``st_ctime`` on the *shared* inode between
    the pre-read and post-read fstat of the same descriptor. The reader must NOT
    treat that benign link-count change as "attempt changed while reading".
    """
    path = tmp_path / "attempt.json"
    data = b'{"attempt_id":"x","status":"success"}\n'
    path.write_bytes(data)

    data_fd: dict = {}
    real_open = os.open

    def fake_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        # POSIX opens the data file via path.name + dir_fd; non-POSIX opens the
        # full Path. Match both so the patch is not silently skipped off Linux.
        if args and args[0] in (path.name, path):
            data_fd["fd"] = fd
        return fd

    monkeypatch.setattr(os, "open", fake_open)

    real_fstat = os.fstat
    calls = {"data": 0}

    def fake_fstat(fd):
        st = real_fstat(fd)
        if data_fd and fd == data_fd.get("fd"):
            calls["data"] += 1
            if calls["data"] == 2:  # post-read fstat (the "after" snapshot)
                return _StatProxy(st, st_ctime_ns=st.st_ctime_ns + 1)
        return st

    monkeypatch.setattr(os, "fstat", fake_fstat)

    # Must not raise; must return the consistent bytes.
    assert read_bounded_file(path, max_bytes=4096) == data
    # Guard: the ctime bump simulation must have actually fired on the data
    # descriptor, otherwise this test would pass without exercising anything.
    assert calls["data"] >= 2


def test_read_bounded_file_still_detects_replacement(tmp_path, monkeypatch):
    """Preserved contract: a rename-swap (path resolves to a different inode)
    must STILL raise ``LedgerContractError("attempt changed while reading")``.

    This guards against the fix accidentally weakening TOCTOU / replacement
    detection. Identity (dev, ino) of the open descriptor and the post-read
    path lookup must agree; a swap changes the path's inode.
    """
    path = tmp_path / "attempt.json"
    data = b'{"attempt_id":"x","status":"success"}\n'
    path.write_bytes(data)

    # POSIX resolves the post-read path via os.stat(name, dir_fd=...); non-POSIX
    # uses os.lstat(path). Patch both so the simulated rename-swap reaches the
    # branch actually taken on the current platform (no silent no-op off Linux).
    real_stat = os.stat
    real_lstat = os.lstat
    stat_calls = {"n": 0}
    lstat_calls = {"n": 0}

    def fake_stat(target, *args, **kwargs):
        st = real_stat(target, *args, **kwargs)
        stat_calls["n"] += 1
        if stat_calls["n"] == 2:  # post-read path stat -> simulate rename-swap inode
            return _StatProxy(st, st_ino=st.st_ino + 1, st_dev=st.st_dev)
        return st

    def fake_lstat(target, *args, **kwargs):
        st = real_lstat(target, *args, **kwargs)
        lstat_calls["n"] += 1
        if lstat_calls["n"] == 2:  # post-read path lstat -> simulate rename-swap inode
            return _StatProxy(st, st_ino=st.st_ino + 1, st_dev=st.st_dev)
        return st

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "lstat", fake_lstat)
    with pytest.raises(LedgerContractError, match="attempt changed while reading"):
        read_bounded_file(path, max_bytes=4096)
    # Guard: the swap simulation must have fired on the platform's lookup call.
    assert stat_calls["n"] >= 2 or lstat_calls["n"] >= 2


def test_read_bounded_file_rejects_size_growth(tmp_path, monkeypatch):
    """Preserved contract: concurrent size growth must still be rejected.

    The descriptor snapshot size must match the bytes read, and the path's
    current size must match the descriptor size.
    """
    path = tmp_path / "attempt.json"
    data = b'{"attempt_id":"x","status":"success"}\n'
    path.write_bytes(data)

    data_fd: dict = {}
    real_open = os.open

    def fake_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        # POSIX opens the data file via path.name + dir_fd; non-POSIX opens the
        # full Path. Match both so the patch is not silently skipped off Linux.
        if args and args[0] in (path.name, path):
            data_fd["fd"] = fd
        return fd

    monkeypatch.setattr(os, "open", fake_open)

    real_fstat = os.fstat
    calls = {"data": 0}

    def fake_fstat(fd):
        st = real_fstat(fd)
        if data_fd and fd == data_fd.get("fd"):
            calls["data"] += 1
            if calls["data"] == 2:  # post-read fstat (the "after" snapshot)
                return _StatProxy(st, st_size=st.st_size + 1)
        return st

    monkeypatch.setattr(os, "fstat", fake_fstat)
    with pytest.raises(LedgerContractError, match="attempt changed while reading"):
        read_bounded_file(path, max_bytes=4096)
    # Guard: the size-growth simulation must have actually fired on the data
    # descriptor, otherwise this test would pass without exercising anything.
    assert calls["data"] >= 2


def test_read_bounded_file_concurrent_hardlink_is_portable_and_benign(
    tmp_path,
):
    """Platform-agnostic regression for PR-A root cause (b).

    Unlike the deterministic monkeypatch tests above, this exercises the real
    bug: a concurrent ``os.link()`` (the ledger's hard-link dedupe token) bumps
    ``st_ctime`` on the *shared* inode while ``read_bounded_file`` is reading.
    The reader must return consistent bytes with no spurious
    ``LedgerContractError``. Because it uses a genuine hard link rather than a
    monkeypatched ``os.*`` call, it cannot silently no-op on non-POSIX
    platforms.
    """
    path = tmp_path / "attempt.json"
    data = b'{"attempt_id":"x","status":"success"}\n'
    path.write_bytes(data)
    link_path = tmp_path / "attempt.link"

    stop = threading.Event()

    def hammer_links():
        while not stop.is_set():
            try:
                os.link(path, link_path)
                os.unlink(link_path)
            except OSError:
                # Lost a race with the reader's own operations; harmless.
                pass

    thread = threading.Thread(target=hammer_links, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            assert read_bounded_file(path, max_bytes=4096) == data
    finally:
        stop.set()
        thread.join(timeout=5)


def test_retries_are_preserved_and_ordered_by_finished_time_then_attempt_id(tmp_path):
    ledger = tmp_path / "ledger"
    later_id = _attempt(
        attempt_id="20260810t080000z-attempt-02",
        status="failed",
        result_code="publisher_error",
        include_sources=False,
    )
    later_id["stage"] = "publisher"
    earlier_id = _attempt(
        attempt_id="20260810t080000z-attempt-01",
        stage="publisher",
        status="no_change",
        result_code="nothing_to_publish",
        include_sources=False,
    )
    append_attempt(ledger, later_id, repository_root=ROOT)
    append_attempt(ledger, earlier_id, repository_root=ROOT)

    status = RunLedgerReader(ledger, repository_root=ROOT).status(
        now=datetime(2026, 8, 10, 9, tzinfo=timezone.utc)
    )

    publisher = status["stages"]["publisher"]
    assert status["attempt_count"] == 2
    assert publisher["last_attempt"]["attempt_id"].endswith("02")
    assert publisher["last_success"]["attempt_id"].endswith("01")
    assert publisher["has_newer_unsuccessful_attempt"] is True
    assert publisher["stale"] == {
        "is_stale": True,
        "reason": "latest_attempt_failed",
        "max_age_hours": 192,
    }


@pytest.mark.parametrize(
    ("attempt_status", "expected"),
    [
        ("success", False),
        ("no_change", False),
        ("partial", True),
        ("failed", True),
    ],
)
def test_unsuccessful_attempt_flag_public_contract(tmp_path, attempt_status, expected):
    ledger = tmp_path / "ledger"
    append_attempt(
        ledger,
        _attempt(status=attempt_status, include_sources=False),
        repository_root=ROOT,
    )

    monitor = RunLedgerReader(ledger, repository_root=ROOT).status(
        now=datetime(2026, 8, 10, 9, tzinfo=timezone.utc)
    )["stages"]["monitor"]

    assert monitor["has_newer_unsuccessful_attempt"] is expected
    legacy_key = "has_newer_" + "incomplete_attempt"
    assert legacy_key not in monitor


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda item: item.update(schema_version="other"), "schema_version"),
        (lambda item: item.update(extra="not allowed"), "unexpected attempt fields"),
        (lambda item: item.update(attempt_id="../escape"), "attempt_id"),
        (lambda item: item.update(report_date="2026-08-11"), "Monday"),
        (lambda item: item.update(scheduled_for="2026-08-10T08:00:00+00:00"), "UTC Z"),
        (
            lambda item: item.update(
                scheduled_for="2026-08-10T09:00:00Z",
                finished_at="2026-08-10T08:00:00Z",
            ),
            "finished_at",
        ),
        (lambda item: item["report"].update(report_date="2026-08-03"), "report_date"),
        (lambda item: item["registry_revision"].update(revision="/host/private"), "revision"),
    ],
)
def test_contract_rejects_unknown_paths_and_noncanonical_values(mutate, match):
    payload = _attempt()
    mutate(payload)
    with pytest.raises(LedgerContractError, match=match):
        canonical_attempt_bytes(payload)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("stage", [], "stage"),
        ("status", {}, "status"),
        ("result_code", 7, "result_code"),
    ],
)
def test_contract_type_checks_membership_fields(field, value, match):
    payload = _attempt()
    payload[field] = value
    with pytest.raises(LedgerContractError, match=match):
        canonical_attempt_bytes(payload)


def test_decoder_wraps_integer_limit_recursion_and_malformed_types():
    huge_integer = '{"number":' + ("9" * 5000) + "}"
    deeply_nested = "[" * 2000 + "]" * 2000
    for raw in (huge_integer, deeply_nested, '["not-an-object"]'):
        with pytest.raises(LedgerContractError):
            decode_attempt_json(raw)

    with pytest.raises(LedgerContractError, match="file size limit"):
        decode_attempt_json(b" " * (128 * 1024 + 1))


def test_source_block_is_complete_exact_and_contains_no_free_text():
    payload = _attempt(status="partial")
    payload["sources"]["unchanged"] = 0
    with pytest.raises(LedgerContractError, match="sum exactly"):
        canonical_attempt_bytes(payload)

    payload = _attempt(status="partial")
    payload["sources"]["failures"].pop()
    with pytest.raises(LedgerContractError, match="failure counts"):
        canonical_attempt_bytes(payload)

    payload = _attempt(status="partial")
    payload["sources"]["failures"][0]["message"] = "raw upstream exception"
    with pytest.raises(LedgerContractError, match="unexpected source failure fields"):
        canonical_attempt_bytes(payload)

    payload = _attempt(status="partial")
    payload["sources"]["failures"][0]["status"] = []
    with pytest.raises(LedgerContractError, match="source failure status"):
        canonical_attempt_bytes(payload)


@pytest.mark.parametrize(
    ("status", "counts", "match"),
    [
        ("success", {"updated": 1, "unchanged": 1, "failed": 1, "blocked": 1}, "success"),
        ("no_change", {"updated": 1, "unchanged": 3, "failed": 0, "blocked": 0}, "no_change"),
        ("partial", {"updated": 2, "unchanged": 2, "failed": 0, "blocked": 0}, "partial"),
        ("failed", {"updated": 1, "unchanged": 1, "failed": 1, "blocked": 1}, "failed"),
    ],
)
def test_source_evidence_must_agree_with_attempt_status(status, counts, match):
    payload = _attempt(status=status)
    payload["sources"].update(counts)
    payload["sources"]["failures"] = []
    if counts["failed"]:
        payload["sources"]["failures"].append(
            {"source_id": "unep", "status": "failed", "error_code": "timeout"}
        )
    if counts["blocked"]:
        payload["sources"]["failures"].append(
            {"source_id": "imf", "status": "blocked", "error_code": "anti_bot"}
        )
    with pytest.raises(LedgerContractError, match=match):
        canonical_attempt_bytes(payload)


def test_status_without_source_evidence_is_not_overconstrained():
    for status in ("success", "no_change", "partial", "failed"):
        canonical_attempt_bytes(_attempt(status=status, include_sources=False))


def test_failed_source_evidence_cannot_claim_an_empty_run():
    payload = _attempt(status="failed")
    payload["sources"].update(
        total=0, updated=0, unchanged=0, failed=0, blocked=0, failures=[]
    )
    with pytest.raises(LedgerContractError, match="failed"):
        canonical_attempt_bytes(payload)


def test_scheduled_date_must_match_report_monday():
    payload = _attempt(scheduled_for="2026-08-11T08:00:00Z", finished_at="2026-08-11T08:30:00Z")
    with pytest.raises(LedgerContractError, match="scheduled_for date"):
        canonical_attempt_bytes(payload)


def test_absent_source_evidence_stays_absent(tmp_path):
    ledger = tmp_path / "ledger"
    payload = _attempt(include_sources=False)
    append_attempt(ledger, payload, repository_root=ROOT)

    result = RunLedgerReader(ledger, repository_root=ROOT).status(
        now=datetime(2026, 8, 10, 9, tzinfo=timezone.utc)
    )

    assert "sources" not in result
    assert "sources" not in result["stages"]["monitor"]["last_attempt"]


def test_staleness_is_stage_specific_and_no_change_is_a_success(tmp_path):
    ledger = tmp_path / "ledger"
    monitor = _attempt(status="no_change", result_code="no_matching_updates", include_sources=False)
    monitor.pop("report")
    publisher = _attempt(
        attempt_id="20260810t100000z-attempt-01",
        stage="publisher",
        status="success",
        result_code="rolling_pr_updated",
        scheduled_for="2026-08-10T10:00:00Z",
        finished_at="2026-08-10T10:05:00Z",
        include_sources=False,
    )
    append_attempt(ledger, monitor, repository_root=ROOT)
    append_attempt(ledger, publisher, repository_root=ROOT)

    current = RunLedgerReader(ledger, repository_root=ROOT).status(
        now=datetime(2026, 8, 18, 8, 29, 59, tzinfo=timezone.utc)
    )
    expired = RunLedgerReader(ledger, repository_root=ROOT).status(
        now=datetime(2026, 8, 18, 8, 30, 1, tzinfo=timezone.utc)
    )

    assert current["stages"]["monitor"]["last_success"]["status"] == "no_change"
    assert current["stages"]["monitor"]["has_newer_unsuccessful_attempt"] is False
    assert current["stages"]["monitor"]["stale"]["is_stale"] is False
    assert expired["stages"]["monitor"]["stale"]["reason"] == "last_success_expired"
    assert expired["stale"] == expired["stages"]["monitor"]["stale"]
    assert expired["stale_source"] == "monitor"
    assert expired["stages"]["publisher"]["stale"]["is_stale"] is False


def test_future_finished_attempt_fails_closed_at_exact_clock_boundary(tmp_path):
    ledger = tmp_path / "ledger"
    append_attempt(ledger, _attempt(), repository_root=ROOT)
    reader = RunLedgerReader(ledger, repository_root=ROOT)

    with pytest.raises(LedgerContractError, match="future_attempt"):
        reader.status(now=datetime(2026, 8, 10, 8, 29, 59, tzinfo=timezone.utc))
    assert reader.status(
        now=datetime(2026, 8, 10, 8, 30, tzinfo=timezone.utc)
    )["state"] == "current"


def test_partial_report_is_not_mislabeled_as_latest_successful_report(tmp_path):
    ledger = tmp_path / "ledger"
    append_attempt(
        ledger,
        _attempt(status="partial", result_code="report_written_with_failures"),
        repository_root=ROOT,
    )

    status = RunLedgerReader(ledger, repository_root=ROOT).status(
        now=datetime(2026, 8, 10, 9, tzinfo=timezone.utc)
    )

    assert status["state"] == "degraded"
    assert "report" in status["stages"]["monitor"]["last_attempt"]
    assert "latest_successful_report" not in status
    assert "latest_report" not in status


def test_reader_sees_new_attempt_without_a_cached_pointer(tmp_path):
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    reader = RunLedgerReader(ledger, repository_root=ROOT)
    assert reader.status()["state"] == "empty"

    append_attempt(ledger, _attempt(), repository_root=ROOT)
    assert reader.status()["attempt_count"] == 1


def test_reader_fails_closed_for_corrupt_misplaced_duplicate_and_resource_excess(tmp_path):
    ledger = tmp_path / "ledger"
    append_attempt(ledger, _attempt(), repository_root=ROOT)
    valid = next(ledger.rglob("*.json"))

    valid.write_text("{broken", encoding="utf-8")
    with pytest.raises(LedgerContractError, match="invalid JSON"):
        RunLedgerReader(ledger, repository_root=ROOT).status()

    valid.write_bytes(canonical_attempt_bytes(_attempt()))
    misplaced = ledger / "attempts" / "monitor" / "2026-08-03" / valid.name
    misplaced.parent.mkdir(parents=True)
    misplaced.write_bytes(valid.read_bytes())
    with pytest.raises(LedgerContractError, match="path does not match"):
        RunLedgerReader(ledger, repository_root=ROOT).status()
    misplaced.unlink()

    with pytest.raises(LedgerContractError, match="attempt count limit"):
        RunLedgerReader(ledger, repository_root=ROOT, max_attempt_count=0).status()
    with pytest.raises(LedgerContractError, match="file size limit"):
        RunLedgerReader(ledger, repository_root=ROOT, max_file_bytes=1).status()


def test_reader_bounds_aggregate_bytes_and_all_visited_entries(tmp_path):
    ledger = tmp_path / "ledger"
    first = _attempt(include_sources=False)
    second = _attempt(
        attempt_id="20260817t080000z-attempt-01",
        scheduled_for="2026-08-17T08:00:00Z",
        finished_at="2026-08-17T08:30:00Z",
        include_sources=False,
    )
    second["report_date"] = "2026-08-17"
    second["report"]["report_date"] = "2026-08-17"
    second["report"]["report_id"] = "climate-monitor-2026-08-17"
    append_attempt(ledger, first, repository_root=ROOT)
    append_attempt(ledger, second, repository_root=ROOT)
    one_size = len(canonical_attempt_bytes(first))

    with pytest.raises(LedgerContractError, match="total byte limit"):
        RunLedgerReader(
            ledger, repository_root=ROOT, max_total_bytes=one_size
        ).status(now=datetime(2026, 8, 17, 9, tzinfo=timezone.utc))

    junk = ledger / "attempts" / "junk"
    junk.mkdir()
    for index in range(5):
        (junk / f"ignored-{index}.txt").write_text("x", encoding="utf-8")
    with pytest.raises(LedgerContractError, match="visited entry limit"):
        RunLedgerReader(
            ledger, repository_root=ROOT, max_visited_entries=3
        ).status(now=datetime(2026, 8, 17, 9, tzinfo=timezone.utc))

    with pytest.raises(LedgerContractError, match="visited directory limit"):
        RunLedgerReader(
            ledger, repository_root=ROOT, max_visited_dirs=2
        ).status(now=datetime(2026, 8, 17, 9, tzinfo=timezone.utc))


def test_scandir_entry_budget_stops_at_limit_plus_one_without_processing_sentinel(
    tmp_path, monkeypatch
):
    ledger = tmp_path / "ledger"
    attempts = ledger / "attempts"
    attempts.mkdir(parents=True)
    for index in range(6):
        (attempts / f"junk-{index}.txt").write_text("x", encoding="utf-8")

    real_scandir = os.scandir
    consumed = 0
    processed = 0

    class CountingEntry:
        def __init__(self, entry, ordinal):
            self._entry = entry
            self._ordinal = ordinal
            self.name = entry.name
            self.path = entry.path

        def stat(self, *, follow_symlinks):
            nonlocal processed
            assert self._ordinal <= 3, "entry beyond the budget was processed"
            processed += 1
            return self._entry.stat(follow_symlinks=follow_symlinks)

    class CountingScan:
        def __init__(self, path):
            self._scan = real_scandir(path)
            self._iterator = iter(self._scan)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._scan.close()

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal consumed
            entry = next(self._iterator)
            consumed += 1
            return CountingEntry(entry, consumed)

    monkeypatch.setattr("climate_monitor.run_ledger.os.scandir", CountingScan)

    def forbidden_walk(*_args, **_kwargs):
        raise AssertionError("os.walk must not be used")

    monkeypatch.setattr("climate_monitor.run_ledger.os.walk", forbidden_walk)
    with pytest.raises(LedgerContractError, match="visited entry limit"):
        RunLedgerReader(
            ledger, repository_root=ROOT, max_visited_entries=3
        ).status()
    assert consumed == 4
    assert processed == 3


def test_scandir_permission_errors_never_look_like_an_empty_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger"
    (ledger / "attempts").mkdir(parents=True)

    def denied_scandir(_root):
        raise PermissionError("denied")

    monkeypatch.setattr("climate_monitor.run_ledger.os.scandir", denied_scandir)
    with pytest.raises(LedgerUnavailableError, match="unavailable"):
        RunLedgerReader(ledger, repository_root=ROOT).status()


def test_scandir_nested_permission_errors_are_not_silently_ignored(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger"
    attempts = ledger / "attempts"
    blocked = attempts / "monitor"
    blocked.mkdir(parents=True)
    real_scandir = os.scandir

    def partly_denied_scandir(path):
        if Path(path) == blocked:
            raise PermissionError("nested denied")
        return real_scandir(path)

    monkeypatch.setattr("climate_monitor.run_ledger.os.scandir", partly_denied_scandir)
    with pytest.raises(LedgerUnavailableError, match="unavailable"):
        RunLedgerReader(ledger, repository_root=ROOT).status()


def test_scandir_entry_stat_errors_are_sanitized(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger"
    attempts = ledger / "attempts"
    attempts.mkdir(parents=True)

    class DeniedEntry:
        name = "attempt.json"
        path = str(attempts / name)

        def stat(self, *, follow_symlinks):
            assert follow_symlinks is False
            raise PermissionError("entry metadata denied")

    class DeniedScan:
        def __enter__(self):
            return iter([DeniedEntry()])

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("climate_monitor.run_ledger.os.scandir", lambda _path: DeniedScan())
    with pytest.raises(LedgerUnavailableError, match="unavailable"):
        RunLedgerReader(ledger, repository_root=ROOT).status()


def test_bounded_reader_rejects_nonregular_and_detects_open_file_change(tmp_path, monkeypatch):
    regular = tmp_path / "attempt.json"
    regular.write_bytes(b"{}")
    real_fstat = os.fstat
    calls = 0

    def changing_fstat(fd):
        nonlocal calls
        calls += 1
        result = real_fstat(fd)
        if calls == 2:
            return type("ChangedStat", (), {
                "st_mode": result.st_mode,
                "st_dev": result.st_dev,
                "st_ino": result.st_ino + 1,
                "st_size": result.st_size,
            })()
        return result

    monkeypatch.setattr("climate_monitor.run_ledger.os.fstat", changing_fstat)
    with pytest.raises(LedgerContractError, match="changed while reading"):
        read_bounded_file(regular, max_bytes=MAX_ATTEMPT_BYTES)


def test_bounded_reader_rejects_oversize_and_growth(tmp_path, monkeypatch):
    oversize = tmp_path / "oversize.json"
    oversize.write_bytes(b"x" * 5)
    with pytest.raises(LedgerContractError, match="file size limit"):
        read_bounded_file(oversize, max_bytes=4)

    growing = tmp_path / "growing.json"
    growing.write_bytes(b"{}")
    real_fstat = os.fstat
    calls = 0

    def growing_fstat(fd):
        nonlocal calls
        calls += 1
        result = real_fstat(fd)
        if calls == 2:
            return type(
                "GrownStat",
                (),
                {
                    "st_mode": result.st_mode,
                    "st_dev": result.st_dev,
                    "st_ino": result.st_ino,
                    "st_size": result.st_size + 1,
                },
            )()
        return result

    monkeypatch.setattr("climate_monitor.run_ledger.os.fstat", growing_fstat)
    with pytest.raises(LedgerContractError, match="changed while reading"):
        read_bounded_file(growing, max_bytes=MAX_ATTEMPT_BYTES)


@pytest.mark.skipif(os.name != "posix", reason="O_NONBLOCK safety is POSIX-specific")
def test_bounded_reader_opens_with_nonblocking_flag_on_posix(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt.json"
    attempt.write_bytes(b"{}")
    real_open = os.open
    observed_calls: list[tuple[object, int, int, int | None]] = []

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        observed_calls.append((path, flags, mode, dir_fd))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("climate_monitor.run_ledger.os.open", recording_open)
    assert read_bounded_file(attempt, max_bytes=MAX_ATTEMPT_BYTES) == b"{}"

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    assert observed_calls[0] == (attempt.anchor, directory_flags, 0o777, None)
    assert all(isinstance(call[3], int) for call in observed_calls[1:])

    file_path, file_flags, file_mode, file_dir_fd = observed_calls[-1]
    assert file_path == attempt.name
    assert file_flags == (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | os.O_NONBLOCK
    )
    assert file_mode == 0o777
    assert isinstance(file_dir_fd, int)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_bounded_reader_rejects_fifo_without_blocking(tmp_path):
    fifo = tmp_path / "attempt.json"
    os.mkfifo(fifo)
    with pytest.raises(LedgerContractError, match="regular file"):
        read_bounded_file(fifo, max_bytes=MAX_ATTEMPT_BYTES)


def test_writer_rejects_intermediate_reparse_probe_and_containment_escape(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger"
    real_probe = __import__("climate_monitor.run_ledger", fromlist=["_is_link_or_reparse"])._is_link_or_reparse

    def flagged(path):
        return path.name == "attempts" or real_probe(path)

    monkeypatch.setattr("climate_monitor.run_ledger._is_link_or_reparse", flagged)
    with pytest.raises(LedgerContractError, match="symbolic link or reparse"):
        append_attempt(ledger, _attempt(), repository_root=ROOT)

    with pytest.raises(LedgerLocationError, match="escapes"):
        _assert_directory_contained(tmp_path.parent, root=ledger)


def test_writer_rejects_real_intermediate_directory_symlink_when_supported(tmp_path):
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = ledger / "attempts"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(LedgerContractError, match="symbolic link or reparse"):
        append_attempt(ledger, _attempt(), repository_root=ROOT)


def test_reader_rejects_duplicate_attempt_identity_across_stages(tmp_path):
    ledger = tmp_path / "ledger"
    first = _attempt(include_sources=False)
    second = _attempt(stage="publisher", include_sources=False)
    append_attempt(ledger, first, repository_root=ROOT)
    with pytest.raises(LedgerConflictError, match="attempt identity conflict"):
        append_attempt(ledger, second, repository_root=ROOT)
    assert RunLedgerReader(ledger, repository_root=ROOT).status()["attempt_count"] == 1


def test_configured_path_must_be_absolute_external_and_not_a_symlink(tmp_path):
    with pytest.raises(LedgerLocationError, match="absolute"):
        RunLedgerReader(Path("relative-ledger"), repository_root=ROOT)

    inside = ROOT / "tmp" / "ledger-do-not-create"
    with pytest.raises(LedgerLocationError, match="outside"):
        RunLedgerReader(inside, repository_root=ROOT)

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(LedgerLocationError, match="symbolic link"):
        RunLedgerReader(link, repository_root=ROOT)


def test_public_projection_contains_record_identity_not_filesystem_paths(tmp_path):
    ledger = tmp_path / "private-host-path" / "ledger"
    append_attempt(ledger, _attempt(), repository_root=ROOT)

    payload = RunLedgerReader(ledger, repository_root=ROOT).status(
        now=datetime(2026, 8, 10, 9, tzinfo=timezone.utc)
    )
    encoded = json.dumps(payload)

    assert str(ledger) not in encoded
    assert "private-host-path" not in encoded
    assert payload["latest_successful_report"]["report_id"] == "climate-monitor-2026-08-10"
    assert payload["latest_successful_registry_revision"]["namespace"] == "web-listening:source-registry"
