"""Real verified cleanup evidence for tests exercising persisted retry paths."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import threading
import time

from climate_monitor.managed_runtime import run_managed


def verified_cleanup():
    result = run_managed([sys.executable, '-c', 'pass'], timeout=5)
    assert result.cleanup['verified']
    return result.cleanup


@contextmanager
def live_managed_group(state_dir):
    """Own a real independent group until the caller releases its ready child."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    ready, release = state_dir / 'test-ready', state_dir / 'test-release'
    code = ('import os,pathlib,time; '
            f'ready=pathlib.Path({str(ready)!r}); '
            'ready.with_suffix(".tmp").write_text(str(os.getpid())); '
            'ready.with_suffix(".tmp").replace(ready); '
            f'release=pathlib.Path({str(release)!r});\n'
            'while not release.exists(): time.sleep(.01)\n')
    results, errors = [], []
    owned = threading.Event()
    def execute():
        try:
            results.append(run_managed([sys.executable, '-c', code], timeout=60,
                                       grace=.2, state_dir=state_dir, heartbeat=lambda pid: owned.set()))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=execute)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not (ready.exists() and owned.is_set()) and not errors and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists() and owned.is_set() and not errors
        pid = int(ready.read_text())
        markers = list((state_dir / 'managed-processes').glob('*.json'))
        assert len(markers) == 1 and json.loads(markers[0].read_text())['pgid'] == pid
        os.kill(pid, 0)
        yield pid
    finally:
        release.touch()
        thread.join(timeout=65)  # The managed timeout also bounds a broken readiness path.
        assert not thread.is_alive() and not errors
        assert results[0].cleanup['verified']
        pid = results[0].cleanup['pgid']
        for check in (os.kill, os.killpg):
            try:
                check(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError('managed fixture process/group survived')
