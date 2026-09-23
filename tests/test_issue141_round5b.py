"""Private owned audit evidence and final writer-content verification."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from climate_monitor import managed_runtime as runtime
from tests.managed_runtime_fixtures import verified_cleanup


def _private_receipt(home):
    boundary = home / 'managed-processes'
    boundary.mkdir(parents=True, mode=0o700)
    receipt = boundary / 'owner.json'
    receipt.write_text(json.dumps({'pgid': verified_cleanup()['pgid']}))
    receipt.chmod(0o600)
    return boundary, receipt


def _modeled_stat(monkeypatch, inode, **changes):
    original_stat, original_fstat = os.stat, os.fstat
    def changed(info):
        if info.st_ino != inode:
            return info
        attributes = {name: getattr(info, name) for name in (
            'st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_size',
            'st_mtime_ns', 'st_ctime_ns', 'st_nlink')}
        return SimpleNamespace(**{**attributes, **changes})
    monkeypatch.setattr(os, 'stat', lambda *a, **k: changed(original_stat(*a, **k)))
    monkeypatch.setattr(os, 'fstat', lambda *a, **k: changed(original_fstat(*a, **k)))


@pytest.mark.parametrize('scope', ['strict', 'acquisition', 'boundary-root'])
@pytest.mark.parametrize('fault', ['directory-world', 'directory-group', 'directory-read',
                                  'receipt-world', 'receipt-group', 'receipt-read',
                                  'directory-owner', 'receipt-owner', 'receipt-hardlink'])
def test_untrusted_audit_permissions_block_recovery(tmp_path, monkeypatch, scope, fault):
    home = tmp_path / 'run'
    boundary, receipt = _private_receipt(home)
    before = receipt.read_bytes()
    target = boundary if fault.startswith('directory') else receipt
    if fault.endswith('owner'):
        _modeled_stat(monkeypatch, target.stat().st_ino, st_uid=os.geteuid() + 1)
    elif fault.endswith('hardlink'):
        os.link(receipt, tmp_path / 'outside-link')
    else:
        modes = {'directory-world': 0o777, 'directory-group': 0o770, 'directory-read': 0o750,
                 'receipt-world': 0o666, 'receipt-group': 0o660, 'receipt-read': 0o640}
        target.chmod(modes[fault])
    verify = runtime.verify_acquisition_quiescent if scope == 'acquisition' else runtime.verify_quiescent
    with pytest.raises(runtime.ManagedFailure) as caught:
        verify(boundary if scope == 'boundary-root' else home)
    assert not caught.value.evidence['recoverable'] and not caught.value.evidence['retryable']
    assert receipt.read_bytes() == before
    assert len(json.dumps(caught.value.evidence)) < 1024


@pytest.mark.parametrize('scope', ['strict', 'acquisition', 'boundary-root'])
def test_private_single_link_receipt_is_accepted(tmp_path, scope):
    boundary, receipt = _private_receipt(tmp_path)
    verify = runtime.verify_acquisition_quiescent if scope == 'acquisition' else runtime.verify_quiescent
    verify(boundary if scope == 'boundary-root' else tmp_path)
    assert receipt.exists()


@pytest.mark.parametrize('fault', ['directory-world', 'receipt-world', 'receipt-hardlink'])
def test_excluded_meeting_evidence_remains_independently_owned(tmp_path, fault):
    from climate_monitor.hermes_identity import SNAPSHOT
    home = tmp_path / SNAPSHOT / 'meetings'
    boundary, receipt = _private_receipt(home)
    if fault == 'directory-world':
        boundary.chmod(0o777)
    elif fault == 'receipt-world':
        receipt.chmod(0o666)
    else:
        os.link(receipt, tmp_path / 'outside-link')
    runtime.verify_acquisition_quiescent(tmp_path)
    with pytest.raises(runtime.ManagedFailure):
        runtime.verify_quiescent(home)


@pytest.mark.parametrize('window', ['after-read', 'during-binding-check'])
@pytest.mark.parametrize('rewrite', ['malformed', 'wrong-pgid'])
def test_writer_final_content_rewrite_blocks_recovery(tmp_path, monkeypatch, window, rewrite):
    original_cleanup, original_read, original_check = runtime.cleanup_group, runtime._read_receipt, runtime._Directories.check
    state = {}
    def cleanup(process, **kwargs):
        result = original_cleanup(process, **kwargs)
        assert result['verified']
        path = next((tmp_path / 'managed-processes').glob('*.json'))
        info = path.stat()
        _modeled_stat(monkeypatch, info.st_ino, st_size=info.st_size,
                      st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns)
        state.update(path=path, pid=process.pid)
        return result
    def replace():
        if 'rewritten' in state:
            return
        data = state['path'].read_bytes()
        value = b'{"pgid":null}' if rewrite == 'malformed' else b'{"pgid":2}'
        assert len(value) <= len(data)
        fd = os.open(state['path'], os.O_WRONLY)
        try:
            assert os.pwrite(fd, value.ljust(len(data)), 0) == len(data)
        finally:
            os.close(fd)
        state['rewritten'] = True
    def read(fd):
        result = original_read(fd)
        if state and window == 'after-read':
            replace()
        return result
    def check(tree):
        result = original_check(tree)
        if state and window == 'during-binding-check':
            replace()
        return result
    monkeypatch.setattr(runtime, 'cleanup_group', cleanup)
    monkeypatch.setattr(runtime, '_read_receipt', read)
    monkeypatch.setattr(runtime._Directories, 'check', check)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.run_managed([sys.executable, '-c', 'pass'], state_dir=tmp_path)
    assert state['rewritten']
    assert not caught.value.evidence['recoverable'] and not caught.value.evidence['retryable']
    assert state['path'].exists()
    with pytest.raises(ProcessLookupError):
        os.kill(state['pid'], 0)


@pytest.mark.parametrize('fault', ['owner', 'permissions', 'hardlink'])
def test_writer_explicitly_validates_receipt_properties(tmp_path, monkeypatch, fault):
    original = runtime._write_marker
    changed = []
    def write(fd, pgid):
        if pgid is None:
            info = os.fstat(fd)
            if fault == 'hardlink':
                path = next((tmp_path / 'managed-processes').glob('*.json'))
                os.link(path, tmp_path / 'outside-link')
            elif fault == 'permissions':
                os.fchmod(fd, 0o666)
            else:
                _modeled_stat(monkeypatch, info.st_ino, st_uid=os.geteuid() + 1)
            changed.append(True)
        return original(fd, pgid)
    monkeypatch.setattr(runtime, '_write_marker', write)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.run_managed([sys.executable, '-c', 'pass'], state_dir=tmp_path)
    assert changed and not caught.value.evidence['recoverable']
    assert list((tmp_path / 'managed-processes').glob('*.json'))


def test_writer_creates_private_owned_single_link_evidence(tmp_path):
    result = runtime.run_managed([sys.executable, '-c', 'pass'], state_dir=tmp_path)
    boundary = tmp_path / 'managed-processes'
    receipt = next(boundary.glob('*.json'))
    assert boundary.stat().st_mode & 0o777 == 0o700
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert boundary.stat().st_uid == receipt.stat().st_uid == os.geteuid()
    assert receipt.stat().st_nlink == 1
    assert json.loads(receipt.read_bytes()) == {'pgid': result.cleanup['pgid']}
    runtime.verify_quiescent(tmp_path)
