"""Generation-bound cleanup and descriptor/content-bound ownership regressions."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from climate_monitor import managed_runtime as runtime
from tests.managed_runtime_fixtures import verified_cleanup, live_managed_group


def test_reaped_reused_identity_never_signals(monkeypatch):
    pgid = 424242 if os.getpgrp() != 424242 else 424243
    process = SimpleNamespace(pid=pgid, returncode=0, poll=lambda: 0)
    signals = []
    monkeypatch.setattr(os, 'getpgid', lambda pid: pgid)
    monkeypatch.setattr(os, 'getsid', lambda pid: pgid)
    monkeypatch.setattr(os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    def no_child(*args):
        raise ChildProcessError()
    monkeypatch.setattr(os, 'waitpid', no_child)
    monkeypatch.setattr(os, 'waitid', no_child)
    evidence = runtime.cleanup_group(process, grace=0)
    assert signals == []
    assert not evidence['verified']


def test_real_leader_is_held_at_cleanup(monkeypatch):
    original = runtime.cleanup_group
    held = []
    def cleanup(process, **kwargs):
        try:
            status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            held.append(process.returncode is None and status is not None and status.si_status == 0)
        except ChildProcessError:
            held.append(False)
        return original(process, **kwargs)
    monkeypatch.setattr(runtime, 'cleanup_group', cleanup)
    completed = runtime.run_managed([sys.executable, '-c', 'pass'])
    assert completed.cleanup['verified']
    assert held == [True]


@pytest.mark.parametrize('rewrite', ['malformed', 'live'])
def test_receipt_same_metadata_rewrite_rejected(tmp_path, monkeypatch, rewrite):
    root = tmp_path / 'run'
    markers = root / 'managed-processes'
    markers.mkdir(parents=True, mode=0o700)
    path = markers / 'owner.json'
    absent = verified_cleanup()['pgid']
    with live_managed_group(tmp_path / 'independent') as live:
        data = json.dumps({'pgid': absent}).encode().ljust(64)
        path.write_bytes(data)
        path.chmod(0o600)
        before = path.stat()
        original_stat, original_fstat = os.stat, os.fstat
        # Deterministically model a filesystem clock tick in which metadata is equal.
        def stable(info):
            return before if (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino) else info
        monkeypatch.setattr(os, 'stat', lambda *a, **k: stable(original_stat(*a, **k)))
        monkeypatch.setattr(os, 'fstat', lambda *a, **k: stable(original_fstat(*a, **k)))
        original = runtime.group_exists
        rewritten = []
        checks = []
        def exists(pgid):
            result = original(pgid)
            checks.append(pgid)
            if pgid == absent and len(checks) == 2 and not rewritten:
                rewritten.append(True)
                replacement = (b'{"pgid":null}' if rewrite == 'malformed' else json.dumps({'pgid': live}).encode()).ljust(64)
                fd = os.open(path, os.O_WRONLY)
                try:
                    assert os.pwrite(fd, replacement, 0) == len(data)
                finally:
                    os.close(fd)
            return result
        monkeypatch.setattr(runtime, 'group_exists', exists)
        with pytest.raises(runtime.ManagedFailure) as caught:
            runtime.verify_quiescent(root)
        assert rewritten and not caught.value.evidence['recoverable']
        os.killpg(live, 0)
        assert path.exists()


def test_receipt_reads_until_eof(tmp_path, monkeypatch):
    markers = tmp_path / 'managed-processes'
    markers.mkdir(mode=0o700)
    path = markers / 'owner.json'
    path.write_text(json.dumps({'pgid': verified_cleanup()['pgid']}))
    path.chmod(0o600)
    before = path.read_bytes()
    original = os.read
    monkeypatch.setattr(os, 'read', lambda fd, size: original(fd, min(size, 2)))
    runtime.verify_quiescent(tmp_path)
    assert path.read_bytes() == before


def test_ancestor_replacement_cannot_verify_detached_tree(tmp_path, monkeypatch):
    parent = tmp_path / 'parent'
    root = parent / 'run'
    root.mkdir(parents=True)
    inode = root.stat().st_ino
    original = os.scandir
    swapped = []
    def scan(fd):
        if isinstance(fd, int) and os.fstat(fd).st_ino == inode and not swapped:
            swapped.append(True)
            parent.rename(tmp_path / 'detached')
            marker_root = root / 'managed-processes'
            marker_root.mkdir(parents=True, mode=0o700)
            (marker_root / 'live.json').write_text(json.dumps({'pgid': os.getpgrp()}))
        return original(fd)
    monkeypatch.setattr(os, 'scandir', scan)
    with pytest.raises(runtime.ManagedFailure):
        runtime.verify_quiescent(root)
    assert swapped


def test_symlink_ancestor_rejected(tmp_path):
    real = tmp_path / 'real'
    (real / 'nested/run').mkdir(parents=True)
    (tmp_path / 'link').symlink_to(real, target_is_directory=True)
    with pytest.raises(runtime.ManagedFailure):
        runtime.verify_quiescent(tmp_path / 'link/nested/run')


@pytest.mark.parametrize('budget', ['depth', 'entries', 'fds'])
def test_explicit_tree_budgets_fail_closed(tmp_path, monkeypatch, budget):
    if budget == 'depth':
        monkeypatch.setattr(runtime, 'MAX_DEPTH', 8, raising=False)
        (tmp_path / ('child/' * 16)).mkdir(parents=True)
    elif budget == 'entries':
        monkeypatch.setattr(runtime, 'MAX_ENTRIES', 8, raising=False)
        for i in range(16):
            (tmp_path / str(i)).write_text('')
    else:
        monkeypatch.setattr(runtime, 'MAX_FDS', 8, raising=False)
        for i in range(16):
            (tmp_path / str(i)).mkdir()
    with pytest.raises(runtime.ManagedFailure):
        runtime.verify_quiescent(tmp_path)


@pytest.mark.parametrize('fault', ['symlink', 'file'])
def test_marker_boundary_corruption_prevents_launch(tmp_path, fault):
    outside = tmp_path / 'outside'
    outside.mkdir()
    state = tmp_path / 'state'
    state.mkdir()
    boundary = state / 'managed-processes'
    if fault == 'symlink':
        boundary.symlink_to(outside, target_is_directory=True)
    else:
        boundary.write_text('opaque-secret')
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.run_managed([sys.executable, '-c', 'pass'], state_dir=state)
    assert not caught.value.evidence['recoverable']
    assert list(outside.iterdir()) == []


def test_completed_marker_is_retained(tmp_path):
    completed = runtime.run_managed([sys.executable, '-c', 'pass'], state_dir=tmp_path)
    markers = list((tmp_path / 'managed-processes').glob('*.json'))
    assert len(markers) == 1
    assert json.loads(markers[0].read_bytes()) == {'pgid': completed.cleanup['pgid']}
    runtime.verify_quiescent(tmp_path)
    assert markers[0].exists()


def test_launch_failure_retains_ambiguous_evidence(tmp_path):
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.run_managed(['/nonexistent-issue141-executable'], state_dir=tmp_path)
    markers = list((tmp_path / 'managed-processes').glob('*.json'))
    assert len(markers) == 1
    assert json.loads(markers[0].read_bytes()) == {'pgid': None}
    assert not caught.value.evidence['recoverable']


def test_marker_replacement_during_launch_is_not_overwritten_or_deleted(tmp_path, monkeypatch):
    original = subprocess.Popen
    swapped = []
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        path = next((tmp_path / 'managed-processes').glob('*.json'))
        path.rename(path.with_suffix('.moved'))
        path.write_text('{"pgid":null}')
        swapped.append(path)
        return process
    monkeypatch.setattr(subprocess, 'Popen', launch)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.run_managed([sys.executable, '-c', 'pass'], state_dir=tmp_path)
    assert not caught.value.evidence['recoverable']
    assert swapped[0].read_text() == '{"pgid":null}'
    assert swapped[0].with_suffix('.moved').exists()


@pytest.mark.parametrize('text', [False, True])
def test_large_input_capture_contract(text):
    value = ('é\n' * 100000) if text else (b'\x00\xff\n' * 100000)
    command = [sys.executable, '-c', 'import sys; b=sys.stdin.buffer.read(); sys.stdout.buffer.write(b); sys.stderr.buffer.write(b)']
    options = {'text': True, 'encoding': 'utf-8'} if text else {}
    completed = runtime.run_managed(command, input=value, capture_output=True, timeout=10, **options)
    assert completed.stdout == value and completed.stderr == value
    assert completed.returncode == 0 and completed.cleanup['verified']


def test_missing_ancestor_appearance_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / 'new/parent/run'
    appeared = []
    original_open, original_stat = os.open, os.stat
    def operation(original, path, *args, **kwargs):
        try:
            return original(path, *args, **kwargs)
        except FileNotFoundError:
            if not appeared:
                appeared.append(True)
                root.mkdir(parents=True)
            raise
    monkeypatch.setattr(os, 'open', lambda *a, **k: operation(original_open, *a, **k))
    monkeypatch.setattr(os, 'stat', lambda *a, **k: operation(original_stat, *a, **k))
    with pytest.raises(runtime.ManagedFailure):
        runtime.verify_quiescent(root)
    assert appeared


def test_marker_ancestor_swap_blocks_recovery(tmp_path, monkeypatch):
    parent = tmp_path / 'parent'
    home = parent / 'home'
    home.mkdir(parents=True)
    original = subprocess.Popen
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        parent.rename(tmp_path / 'detached')
        home.mkdir(parents=True)
        return process
    monkeypatch.setattr(subprocess, 'Popen', launch)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.run_managed([sys.executable, '-c', 'pass'], state_dir=home)
    assert not caught.value.evidence['recoverable']
    assert list((tmp_path / 'detached/home/managed-processes').glob('*.json'))


def test_marker_never_unlinks_a_replaced_name(tmp_path, monkeypatch):
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path.parent.name == 'managed-processes':
            path.rename(path.with_suffix('.moved'))
            path.write_text('{"pgid":null}')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)
    completed = runtime.run_managed([sys.executable, '-c', 'pass'], state_dir=tmp_path)
    assert completed.cleanup['verified']
    assert len(list((tmp_path / 'managed-processes').glob('*.json'))) == 1


def test_no_reaping_api_before_cleanup(monkeypatch):
    cleanup = runtime.cleanup_group
    cleaning = []
    def enter(process, **kwargs):
        cleaning.append(True)
        return cleanup(process, **kwargs)
    monkeypatch.setattr(runtime, 'cleanup_group', enter)
    for name in ('communicate', 'poll', 'wait'):
        original = getattr(subprocess.Popen, name)
        def guard(process, *args, _original=original, **kwargs):
            assert cleaning, 'leader was consumed before cleanup'
            return _original(process, *args, **kwargs)
        monkeypatch.setattr(subprocess.Popen, name, guard)
    assert runtime.run_managed([sys.executable, '-c', 'pass']).cleanup['verified']


@pytest.mark.parametrize('fault', ['none', 'launch', 'encoding', 'unknown-encoding', 'marker', 'cleanup'])
def test_runtime_fd_and_child_cleanup(tmp_path, monkeypatch, fault):
    before = set(os.listdir('/proc/self/fd'))
    command = [sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"\\xff")']
    options = dict(state_dir=tmp_path, capture_output=True)
    children = []
    original = subprocess.Popen
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process.pid)
        return process
    monkeypatch.setattr(subprocess, 'Popen', launch)
    if fault == 'launch':
        command = ['/nonexistent-issue141-executable']
    elif fault in ('encoding', 'unknown-encoding'):
        options.update(text=True, encoding='ascii' if fault == 'encoding' else 'opaque-unknown-codec')
    elif fault == 'marker':
        (tmp_path / 'managed-processes').write_text('invalid')
    elif fault == 'cleanup':
        def fail(*args):
            raise PermissionError('opaque-secret')
        monkeypatch.setattr(runtime, '_group_members', fail, raising=False)
    if fault == 'none':
        assert runtime.run_managed(command, **options).stdout == b'\xff'
    else:
        with pytest.raises(runtime.ManagedFailure):
            runtime.run_managed(command, **options)
    assert set(os.listdir('/proc/self/fd')) == before
    for pid in children:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_text_encoding_newlines_and_explicit_stdout(tmp_path):
    result = runtime.run_managed([sys.executable, '-c', 'import sys;sys.stdout.buffer.write(b"a\\r\\nb\\rc\\xff")'],
                                 capture_output=True, encoding='ascii', errors='replace')
    assert result.stdout == 'a\nb\nc�' and result.stderr == ''
    with (tmp_path / 'out').open('wb') as output:
        result = runtime.run_managed([sys.executable, '-c', 'print("hello")'], stdout=output)
    assert result.stdout is None and result.stderr is None
    assert (tmp_path / 'out').read_bytes() == b'hello\n'


def test_marker_count_limit_prevents_launch(tmp_path, monkeypatch):
    directory = tmp_path / 'managed-processes'
    directory.mkdir(mode=0o700)
    for i in range(2):
        (directory / f'{i}.json').write_text('{"pgid":null}')
    monkeypatch.setattr(runtime, 'MAX_MARKERS', 2, raising=False)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.run_managed([sys.executable, '-c', 'pass'], state_dir=tmp_path)
    assert not caught.value.evidence['recoverable']
    assert len(list(directory.iterdir())) == 2


def test_cancellation_while_reading_capture_cannot_return_success(monkeypatch):
    original = runtime.tempfile.TemporaryFile
    sent = []
    class Capture:
        def __init__(self, *args, **kwargs):
            self.file = original(*args, **kwargs)
        def __getattr__(self, name):
            return getattr(self.file, name)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.file.close()
        def read(self, *args):
            if not sent:
                sent.append(True)
                os.kill(os.getpid(), signal.SIGTERM)
            return self.file.read(*args)
    monkeypatch.setattr(runtime.tempfile, 'TemporaryFile', Capture)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.run_managed([sys.executable, '-c', 'print("ok")'], capture_output=True)
    assert caught.value.evidence['category'] == 'timeout_cancelled'
    assert caught.value.evidence['cleanup']['verified']


def test_pidfd_unavailable_rejects_before_launch(monkeypatch):
    children = []
    original = subprocess.Popen
    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    def unavailable(*args):
        raise OSError('unsupported private operation')
    monkeypatch.setattr(subprocess, 'Popen', launch)
    monkeypatch.setattr(runtime, '_pidfd_open', unavailable)
    try:
        with pytest.raises(runtime.ManagedFailure):
            runtime.run_managed([sys.executable, '-c', 'import time;time.sleep(60)'])
        assert not children
    finally:
        for child in children:
            child.kill()
            child.wait(timeout=5)


def test_local_timestamp_resolution_rewrite_is_rejected(tmp_path, monkeypatch):
    markers = tmp_path / 'managed-processes'
    markers.mkdir(mode=0o700)
    path = markers / 'owner.json'
    pgid = verified_cleanup()['pgid']
    data = json.dumps({'pgid': pgid}).encode().ljust(64)
    replacement = b'{"pgid":null}'.ljust(64)
    original = runtime.group_exists
    same_tick = []
    for attempt in range(200):
        path.write_bytes(data)
        path.chmod(0o600)
        before = path.stat()
        checks = []
        def exists(group):
            result = original(group)
            checks.append(group)
            if len(checks) == 2:
                fd = os.open(path, os.O_WRONLY)
                try:
                    os.pwrite(fd, replacement, 0)
                finally:
                    os.close(fd)
                after = path.stat()
                same_tick.append((before.st_mtime_ns, before.st_ctime_ns, before.st_size)
                                 == (after.st_mtime_ns, after.st_ctime_ns, after.st_size))
            return result
        monkeypatch.setattr(runtime, 'group_exists', exists)
        with pytest.raises(runtime.ManagedFailure):
            runtime.verify_quiescent(tmp_path)
        if any(same_tick):
            break
    # Equal-metadata rewrites are deterministic in the two modeled tests above;
    # this additionally verifies every actual local rewrite, whatever its clock tick.
    assert same_tick and path.read_bytes() == replacement


def test_pin_failure_after_spawn_still_reaps(tmp_path, monkeypatch):
    original = runtime._pidfd_open
    calls = []
    def open_fd(pid):
        calls.append(pid)
        if len(calls) == 2:
            raise OSError('private FD exhaustion')
        return original(pid)
    monkeypatch.setattr(runtime, '_pidfd_open', open_fd)
    with pytest.raises(runtime.ManagedFailure):
        runtime.run_managed([sys.executable, '-c', 'import time;time.sleep(60)'], state_dir=tmp_path)
    assert len(calls) == 2
    with pytest.raises(ProcessLookupError):
        os.kill(calls[-1], 0)


def test_pidfd_failure_abort_targets_only_original_leader(monkeypatch):
    pid = []
    original = subprocess.Popen
    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        pid.append(child.pid)
        return child
    def no_scan(*args):
        raise PermissionError('private proc scan diagnostic')
    monkeypatch.setattr(subprocess, 'Popen', launch)
    monkeypatch.setattr(runtime, '_group_members', no_scan)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.run_managed([sys.executable, '-c', 'import time;time.sleep(60)'], timeout=.05)
    assert not caught.value.evidence['recoverable']
    with pytest.raises(ProcessLookupError):
        os.kill(pid[0], 0)
