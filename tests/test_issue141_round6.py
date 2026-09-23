"""Full-tree ownership, scalable audit receipts and bounded execution."""
import json
import os
from pathlib import Path
import signal
import sys
import time

import pytest

from climate_monitor import managed_runtime as runtime


@pytest.mark.parametrize('double_fork', [False, True])
def test_escaped_descendant_is_gone_before_verified(tmp_path, double_fork):
    ready = tmp_path / 'escaped'
    spawned = tmp_path / 'spawned'
    child = ("import os,signal,time; "
             + ("os.fork() and os._exit(0); os.setsid(); " if double_fork else "")
             + "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
             + f"open({str(ready)!r},'w').write(str(os.getpid())); time.sleep(60)")
    leader = ("import subprocess,sys,time,pathlib; "
              + f"child=subprocess.Popen([sys.executable,'-c',{child!r}], start_new_session=True); "
              + f"pathlib.Path({str(spawned)!r}).write_text(str(child.pid)); "
              + f"p=pathlib.Path({str(ready)!r});\nwhile not p.exists(): time.sleep(.01)\n")
    escaped = None
    try:
        completed = runtime.run_managed([sys.executable, '-c', leader], grace=.1, capture_output=True)
        escaped = int(ready.read_text())
        (tmp_path / 'observed.json').write_text(json.dumps({'escaped_pid': escaped, 'cleanup': completed.cleanup}))
        assert completed.cleanup['verified']
        with pytest.raises(ProcessLookupError):
            os.kill(escaped, 0)
    finally:
        owned = {int(path.read_text()) for path in (ready, spawned) if path.exists()}
        for pid in owned:
            try:
                os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                continue
            fd = runtime._pidfd_open(pid)
            try:
                try:
                    runtime._pidfd_kill(fd)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)
            finally:
                os.close(fd)
        for pid in owned:
            assert not Path(f'/proc/{pid}').exists()


def test_six_hundred_completed_invocations_do_not_exhaust_home(tmp_path):
    for index in range(601):
        try:
            result = runtime.run_managed([sys.executable, '-c', 'pass'], state_dir=tmp_path)
        except runtime.ManagedFailure:
            (tmp_path / 'observed.json').write_text(json.dumps({'successful_invocations': index, 'failed_invocation': index + 1}))
            raise
        assert result.cleanup['verified'], index
    runtime.verify_quiescent(tmp_path)
    receipts = list((tmp_path / 'managed-processes').glob('*.json'))
    assert len(receipts) == 601
    assert all(json.loads(p.read_bytes())['state'] == 'completed' for p in receipts)


def test_completed_receipt_ignores_reused_numeric_group(tmp_path, monkeypatch):
    result = runtime.run_managed([sys.executable, '-c', 'pass'], state_dir=tmp_path)
    # Any future liveness probe for the numeric group sees a different process.
    # Completed evidence must not query it at all.
    checks = []
    def reused(pgid):
        checks.append(pgid)
        return True
    monkeypatch.setattr(runtime, 'group_exists', reused)
    runtime.verify_quiescent(tmp_path)
    assert checks == []
    assert result.cleanup['verified']


def test_sixteen_megabyte_capture_is_terminal_and_bounded(tmp_path):
    with pytest.raises(runtime.ManagedFailure) as caught:
        result = runtime.run_managed([sys.executable, '-c',
                            'import os; b=b"x"*65536; [os.write(1,b) for _ in range(256)]'],
                            capture_output=True, timeout=5)
        (tmp_path / 'observed.json').write_text(json.dumps({'captured_stdout_bytes': len(result.stdout), 'cleanup': result.cleanup}))
    assert caught.value.evidence['category'] == 'internal'
    assert not caught.value.evidence['retryable']
    assert caught.value.evidence['cleanup']['verified']
    assert len(json.dumps(caught.value.evidence)) < 1024


def test_heartbeat_has_independent_one_second_cadence(tmp_path):
    calls = []
    result = runtime.run_managed([sys.executable, '-c', 'import time; time.sleep(1.2)'],
                                 heartbeat=lambda pid: calls.append(time.monotonic()))
    (tmp_path / 'observed.json').write_text(json.dumps({'heartbeat_calls': len(calls), 'span_seconds': calls[-1] - calls[0]}))
    assert result.returncode == 0
    assert 1 <= len(calls) <= 2
    assert all(b - a >= 1 for a, b in zip(calls, calls[1:]))
