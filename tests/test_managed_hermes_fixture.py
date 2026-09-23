"""The synthetic managed runtime must not inherit writable CI interpreter modes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def test_managed_interpreter_copy_is_private_and_executable(safe_managed_interpreter):
    source = Path(sys.executable).resolve()
    target = safe_managed_interpreter
    assert target != source
    assert target.stat().st_uid == os.getuid()
    assert target.stat().st_mode & 0o777 == 0o700
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert target.parent.parent.stat().st_mode & 0o777 == 0o700
    assert hashlib.sha256(target.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()
    child = subprocess.run([str(target), '-I', '-S', '-c',
                            'import json,sys; print(json.dumps(list(sys.version_info[:2])))'],
                           check=True, capture_output=True, text=True)
    assert json.loads(child.stdout) == list(sys.version_info[:2])


def test_shared_hermes_launcher_uses_safe_copy(safe_managed_interpreter):
    from climate_monitor.hermes_identity import secure_read
    launcher = Path(os.environ['HERMES_EXECUTABLE'])
    assert launcher.read_text().splitlines()[0] == '#!' + str(safe_managed_interpreter)
    secure_read(safe_managed_interpreter)
