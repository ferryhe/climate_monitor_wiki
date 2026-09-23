"""Managed failures and real process-tree lifecycle regressions."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from tests.managed_runtime_fixtures import verified_cleanup


def test_acquisition_diagnostic_never_returns_arbitrary_stderr(tmp_path):
    from scripts.run_agent_acquisition import _hermes_process_error
    response = tmp_path / 'response'
    response.write_text('private-provider-payload Bearer opaque-credential\n')
    message = _hermes_process_error(response, 1, phase='acquisition')
    assert 'private-provider-payload' not in message
    assert 'opaque-credential' not in message


@pytest.mark.parametrize('category,retryable,fresh', [
    ('transient_service', True, False), ('timeout_cancelled', False, False),
    ('identity_drift', False, True), ('configuration_auth', False, True),
    ('frozen_input', False, True), ('internal', False, False),
])
def test_taxonomy(category, retryable, fresh):
    from climate_monitor.managed_runtime import ManagedFailure
    failure = ManagedFailure(category)
    assert failure.evidence['retryable'] is retryable
    assert ('fresh run' in str(failure)) is fresh
    assert len(json.dumps(failure.evidence)) < 1024


def test_unknown_exit_is_terminal():
    from climate_monitor.managed_runtime import failure_for_exit
    failure = failure_for_exit(1)
    assert failure.evidence['category'] == 'internal'
    assert failure.evidence['retryable'] is False


def test_failed_cleanup_overrides_transient():
    from climate_monitor.managed_runtime import ManagedFailure
    failure = ManagedFailure('transient_service', cleanup={'verified': False})
    assert not failure.evidence['retryable']
    assert not failure.evidence['recoverable']


@pytest.mark.parametrize('cancel', [False, True])
def test_real_tree_reaped(tmp_path, cancel):
    from climate_monitor.managed_runtime import ManagedFailure, run_managed
    pids = tmp_path / 'pids'
    # Both descendants ignore TERM. Leader exits on TERM; leader wait is insufficient.
    grandchild = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
    child = (
        "import os,signal,subprocess,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
        f"open({str(pids)!r},'w').write(str(os.getpid())+' '+str(p.pid)); time.sleep(60)"
    )
    leader = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(60)"
    def heartbeat(pid):
        if cancel and pids.exists():
            raise KeyboardInterrupt()
    with pytest.raises(ManagedFailure) as caught:
        run_managed([sys.executable, '-c', leader], timeout=0.5,
                    grace=0.1, heartbeat=heartbeat, capture_output=True)
    evidence = caught.value.evidence
    assert evidence['category'] == 'timeout_cancelled'
    assert evidence['cleanup']['verified'] is True
    assert evidence['cleanup']['kill_sent'] is True
    assert pids.exists()
    for pid in [*map(int, pids.read_text().split()), evidence['cleanup']['pgid']]:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    with pytest.raises(ProcessLookupError):
        os.killpg(evidence['cleanup']['pgid'], 0)


def test_meeting_configuration_failure_stops_batch(tmp_path):
    from climate_monitor.managed_runtime import ManagedFailure
    from climate_monitor.meetings import process_batch, meeting_retry_run
    from test_issue136_meetings import _database
    database = _database(tmp_path, ['one', 'two'])
    calls = []
    def extract(request):
        calls.append(request)
        raise ManagedFailure('configuration_auth')
    result = process_batch(database, 'batch', prompt_text='extract', prompt_version='1',
                           provider='', model='', extractor=extract)
    assert len(calls) == 1
    assert result['failure']['category'] == 'configuration_auth'
    assert not result['failure']['retryable']
    assert meeting_retry_run(database, batch_id='batch') is None
    with pytest.raises(ValueError, match='fresh run|terminal'):
        process_batch(database, 'batch', prompt_text='extract', prompt_version='1',
                      provider='', model='', extractor=extract, retry_failed=True)


@pytest.mark.parametrize('pgid', [0, -1, 1, '123', True, os.getpgrp()])
def test_cleanup_rejects_unsafe_groups(monkeypatch, pgid):
    from types import SimpleNamespace
    from climate_monitor import managed_runtime as runtime
    signals = []
    monkeypatch.setattr(runtime.os, 'killpg', lambda *args: signals.append(args))
    assert runtime.cleanup_group(SimpleNamespace(pid=pgid))['verified'] is False
    assert signals == []


@pytest.mark.parametrize('fault', ['signal', 'probe', 'wait', 'wrong_group'])
def test_cleanup_failure_never_claims_verified(monkeypatch, fault):
    from types import SimpleNamespace
    from climate_monitor import managed_runtime as runtime
    pgid = 99999999
    process = SimpleNamespace(pid=pgid, returncode=None, poll=lambda: None)
    monkeypatch.setattr(runtime.os, 'getpgid', lambda pid: pgid + (fault == 'wrong_group'))
    monkeypatch.setattr(runtime.os, 'getsid', lambda pid: pgid)
    def killpg(pid, sig):
        if (fault == 'signal' and sig) or (fault == 'probe' and not sig):
            raise PermissionError('private diagnostic')
    monkeypatch.setattr(runtime.os, 'killpg', killpg)
    if fault == 'wait':
        def poll():
            raise OSError('private diagnostic')
        process.poll = poll
    evidence = runtime.cleanup_group(process, grace=0.001)
    assert evidence['verified'] is False
    assert 'private' not in json.dumps(evidence)


def test_real_leader_exit_still_reaps_descendant(tmp_path):
    from climate_monitor.managed_runtime import run_managed
    pids = tmp_path / 'child'
    child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
    leader = (
        'import subprocess,sys,time; '
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"open({str(pids)!r},'w').write(str(p.pid)); time.sleep(.1)"
    )
    completed = run_managed([sys.executable, '-c', leader], timeout=2, grace=0.1, capture_output=True)
    assert completed.returncode == 0
    assert completed.cleanup['verified'] is True
    assert completed.cleanup['kill_sent'] is True
    with pytest.raises(ProcessLookupError):
        os.kill(int(pids.read_text()), 0)


def test_recovery_blocks_unverified_markers(tmp_path):
    from climate_monitor.managed_runtime import ManagedFailure, verify_quiescent
    directory = tmp_path / 'hermes' / 'report' / 'managed-processes'
    directory.mkdir(parents=True, mode=0o700)
    path = directory / 'inflight.json'
    path.write_text(json.dumps({'pgid': os.getpgrp()}))
    path.chmod(0o600)
    with pytest.raises(ManagedFailure) as caught:
        verify_quiescent(tmp_path)
    assert not caught.value.evidence['recoverable']
    assert path.exists()
    path.write_text(json.dumps({'pgid': 99999999}))
    path.chmod(0o600)
    verify_quiescent(tmp_path)
    assert json.loads(path.read_text()) == {'pgid': 99999999}


@pytest.mark.parametrize('category', ['identity_drift', 'configuration_auth', 'frozen_input', 'internal', 'timeout_cancelled', 'transient_service'])
def test_meeting_failure_retry_and_redaction(tmp_path, category):
    from climate_monitor.managed_runtime import ManagedFailure
    from climate_monitor.meetings import process_batch, meeting_retry_run
    from test_issue136_meetings import _database
    database = _database(tmp_path, ['one', 'two'])
    def extract(request):
        raise ManagedFailure(category, cleanup=verified_cleanup())
    options = dict(prompt_text='extract', prompt_version='1', provider='', model='')
    result = process_batch(database, 'batch', extractor=extract, **options)
    assert result['failure']['category'] == category
    assert bool(meeting_retry_run(database, batch_id='batch')) == (category == 'transient_service')
    if category == 'transient_service':
        result = process_batch(database, 'batch', extractor=lambda request: {'events': []},
                               retry_failed=True, **options)
        assert result['status'] == 'succeeded'


def test_unknown_meeting_error_is_redacted_terminal(tmp_path):
    from climate_monitor.meetings import process_batch, meeting_retry_run
    from test_issue136_meetings import _database
    database = _database(tmp_path, ['one'])
    def extract(request):
        raise RuntimeError('arbitrary provider diagnostic Bearer opaque-secret')
    result = process_batch(database, 'batch', prompt_text='extract', prompt_version='1',
                           provider='', model='', extractor=extract)
    assert 'opaque-secret' not in json.dumps(result)
    assert 'arbitrary provider' not in json.dumps(result)
    assert result['failure']['category'] == 'internal'
    assert meeting_retry_run(database, batch_id='batch') is None


@pytest.mark.parametrize('code,category', [(1, 'internal'), (65, 'frozen_input'), (76, 'identity_drift'), (78, 'configuration_auth')])
def test_report_checkpoint_retains_only_bounded_evidence(tmp_path, monkeypatch, code, category):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from scripts import run_climate_monitor as report
    from climate_monitor import hermes_identity
    from climate_monitor.managed_runtime import ManagedFailure
    from tests.test_issue87_post_pr106 import _help_with_query_file
    path = tmp_path / 'checkpoint.json'
    monkeypatch.setattr(report, '_managed_inference_runtime', lambda args: ('hermes', {}, tmp_path))
    monkeypatch.setattr(hermes_identity, 'auth_execution', lambda *a, **k: nullcontext({}))
    monkeypatch.setattr(report, 'run_managed', lambda *a, **k: SimpleNamespace(
        returncode=code, stdout='private-output', stderr='Authorization: Bearer opaque-secret',
        cleanup=verified_cleanup()))
    args = SimpleNamespace(task_binding='bound', model='', model_provider='', authoring_timeout=1)
    with pytest.raises(ManagedFailure):
        report._checkpointed_authoring(path, 'instruction', args=args,
                                       help_stdout=_help_with_query_file(), validate=lambda value: value)
    checkpoint = json.loads(path.read_text())
    assert checkpoint['failure']['category'] == category
    diagnostic = path.with_suffix('.attempt-1.json').read_text()
    assert 'opaque-secret' not in diagnostic
    assert 'private-output' not in diagnostic
    with pytest.raises(ManagedFailure):
        report._checkpointed_authoring(path, 'instruction', args=args,
                                       help_stdout=_help_with_query_file(), validate=lambda value: value)
    assert json.loads(path.read_text())['attempt'] == 1


def test_acquisition_result_cannot_make_unknown_exit_retryable(tmp_path):
    from scripts import run_agent_acquisition as acquisition
    binding = tmp_path / 'attempt-1.json'
    binding.write_text(json.dumps({'run_id': 'run', 'attempt': 1}))
    acquisition._write_result(binding, exit_code=1, retryable=True, error='secret diagnostic')
    result = json.loads((tmp_path / 'attempt-1-result.json').read_text())
    assert result['retryable'] is False
    assert result['failure']['category'] == 'internal'
    assert 'secret diagnostic' not in json.dumps(result)


def test_acquisition_failed_cleanup_never_finishes_recoverable(tmp_path):
    from scripts import run_agent_acquisition as acquisition
    from climate_monitor.managed_runtime import ManagedFailure
    binding = tmp_path / 'attempt-1.json'
    binding.write_text(json.dumps({'run_id': 'run', 'attempt': 1}))
    acquisition._write_result(binding, exit_code=1, retryable=True, error=None,
                              failure=ManagedFailure('transient_service', cleanup={'verified': False}))
    result = json.loads((tmp_path / 'attempt-1-result.json').read_text())
    runtime = json.loads((tmp_path / 'runtime.json').read_text())
    assert result['retryable'] is False
    assert runtime['state'] == 'cleanup_failed'


def test_malformed_cleanup_receipt_cannot_authorize_retry():
    from climate_monitor.managed_runtime import SCHEMA, read_failure
    failure = read_failure({'schema_version': SCHEMA, 'category': 'transient_service', 'cleanup': 'verified'})
    assert not failure.evidence['retryable']
    assert not failure.evidence['recoverable']


def test_real_sigterm_cancels_nested_execution_groups(tmp_path):
    pids = tmp_path / 'pids'
    receipt = tmp_path / 'receipt'
    ready = tmp_path / 'ready'
    grandchild = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'
    child = (
        'import os,signal,subprocess,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
        f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
        f"open({str(pids)!r},'w').write(str(os.getpid())+' '+str(p.pid)); time.sleep(60)"
    )
    worker = (
        'import json,sys; from climate_monitor.managed_runtime import ManagedFailure,run_managed\n'
        'try:\n'
        f" run_managed([sys.executable,'-c',{child!r}],timeout=30,grace=.1,capture_output=True)\n"
        'except ManagedFailure as failure:\n'
        f" open({str(receipt)!r},'w').write(json.dumps(failure.evidence))\n"
    )
    # Outer owner also owns a distinct session. TERM must reach the nested group's owner.
    supervisor = (
        'import sys; from climate_monitor.managed_runtime import run_managed,ManagedFailure\n'
        'try:\n'
        f" run_managed([sys.executable,'-c',{worker!r}],timeout=30,grace=1,capture_output=True)\n"
        'except ManagedFailure: pass\n'
    )
    process = subprocess.Popen([sys.executable, '-c', supervisor], start_new_session=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not pids.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert pids.exists()
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
        assert receipt.exists()
        evidence = json.loads(receipt.read_text())
        assert evidence['category'] == 'timeout_cancelled'
        assert evidence['cleanup']['verified'] is True
        for pid in map(int, pids.read_text().split()):
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_meeting_inference_identity_failure_is_batch_wide(tmp_path, monkeypatch):
    import hashlib
    from climate_monitor import hermes_identity
    from climate_monitor.meetings import process_batch
    from scripts.run_meeting_extraction import _extractor
    from test_issue136_meetings import _database
    database = _database(tmp_path, ['one', 'two'])
    path = tmp_path / 'binding.json'
    path.write_text(json.dumps({'run_id': 'run', 'hermes_snapshot': {}}))
    calls = []
    def inference(*args, **kwargs):
        calls.append(True)
        raise ValueError('Hermes effective identity changed; start a fresh run')
    monkeypatch.setattr(hermes_identity, 'inference_runtime', inference)
    result = process_batch(database, 'batch', prompt_text='extract', prompt_version='1',
                           provider='', model='', extractor=_extractor('', '', path))
    assert len(calls) == 1
    assert result['failure']['category'] == 'identity_drift'


def test_legacy_retry_flag_is_not_evidence():
    from climate_monitor.managed_runtime import retry_allowed, ManagedFailure
    assert not retry_allowed({'retryable': True, 'exit_code': 75})
    assert not retry_allowed({'retryable': True, 'failure': ManagedFailure('internal').evidence})
    assert retry_allowed({'retryable': True, 'failure': ManagedFailure('transient_service', cleanup=verified_cleanup()).evidence})
    assert not retry_allowed({'retryable': True, 'failure': ManagedFailure(
        'transient_service', cleanup={'verified': False}).evidence})


def test_failed_cleanup_retains_auth_inflight_marker(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from climate_monitor import hermes_auth_state as auth
    from climate_monitor.managed_runtime import ManagedFailure
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(auth, '_lock', lambda root: nullcontext())
    monkeypatch.setattr(auth, '_record', lambda home: {'snapshot': {}})
    monkeypatch.setattr(auth, '_chain', lambda *args: [({'sha256': 'a' * 64}, {})])
    monkeypatch.setattr(auth, '_verify', lambda *args: None)
    monkeypatch.setattr(auth, '_materialize', lambda *args: None)
    monkeypatch.setattr(auth, 'secure_read', lambda *args, **kwargs: pytest.fail('unverified child cannot seal auth'))
    with pytest.raises(ManagedFailure):
        with auth.execution(home):
            raise ManagedFailure('timeout_cancelled', cleanup={'verified': False})
    assert (tmp_path / 'auth-inflight.json').is_file()


@pytest.mark.parametrize('message,category', [
    ('Hermes effective identity changed; start a fresh run', 'identity_drift'),
    ('immutable Hermes inference configuration changed', 'configuration_auth'),
    ('Hermes auth state inconsistent or unsealed; start a fresh run', 'configuration_auth'),
    ('Hermes snapshot missing, incomplete or runtime drifted; start a fresh run', 'configuration_auth'),
    ('private unknown payload', 'internal'),
])
def test_exception_classification_never_retains_text(message, category):
    from climate_monitor.managed_runtime import failure_from_exception
    failure = failure_from_exception(ValueError(message))
    assert failure.evidence['category'] == category
    assert not failure.evidence['retryable']
    assert 'private unknown payload' not in json.dumps(failure.evidence)


def test_meeting_missing_cli_capability_is_bounded_batch_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from climate_monitor import hermes_identity, hermes_auth_state
    from climate_monitor.meetings import process_batch
    from scripts import run_meeting_extraction as worker
    from test_issue136_meetings import _database
    database = _database(tmp_path, ['one', 'two'])
    binding = tmp_path / 'binding.json'
    binding.write_text(json.dumps({'run_id': 'run', 'hermes_snapshot': {}}))
    monkeypatch.setattr(hermes_identity, 'inference_runtime', lambda *a, **k: ('hermes', {}, tmp_path))
    monkeypatch.setattr(hermes_auth_state, 'verify_auth', lambda *a: None)
    monkeypatch.setattr(worker, 'run_managed', lambda *a, **k: SimpleNamespace(returncode=0, stdout='unsupported CLI'))
    result = process_batch(database, 'batch', prompt_text='extract', prompt_version='1',
                           provider='', model='', extractor=worker._extractor('', '', binding))
    assert result['failure']['category'] == 'configuration_auth'
    assert sum(item['status'] == 'failed' for item in result['items']) == 1


@pytest.mark.parametrize('category', ['transient_service', 'timeout_cancelled', 'identity_drift',
                                     'configuration_auth', 'frozen_input', 'internal', 'unknown'])
def test_real_report_worker_receipt_is_bounded(tmp_path, monkeypatch, category):
    from scripts import run_agent_acquisition as acquisition
    from climate_monitor.managed_runtime import SCHEMA
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    payload = {'failure': {'schema_version': SCHEMA, 'category': category,
                          'message': 'opaque-secret', 'arbitrary': 'private-output', 'cleanup': verified_cleanup()}}
    (scripts / 'run_climate_monitor.py').write_text(
        'import sys\n'
        'print("Authorization: Bearer opaque-secret", file=sys.stderr)\n'
        f'print({json.dumps(payload)!r})\n'
        'sys.exit(1)\n'
    )
    monkeypatch.setattr(acquisition, 'ROOT', tmp_path)
    monkeypatch.setattr(acquisition, '_report_environment', lambda provider: {})
    binding = {'attempt': 1, 'repository_commit_sha': 'a' * 40,
               'report_inputs': {name: str(tmp_path / name) for name in (
                   'acquisition_batch', 'web_listening_manifest', 'pillar_b_artifact',
                   'staging_dir', 'state_dir', 'source_dir', 'wiki_dir')}}
    assert acquisition._run_report(tmp_path / 'binding.json', binding) == 1
    result = (tmp_path / 'attempt-1-report-result.json').read_text()
    assert 'opaque-secret' not in result and 'private-output' not in result
    failure = json.loads(result)['failure']
    assert failure['category'] == ('internal' if category == 'unknown' else category)
    assert failure['retryable'] is (category == 'transient_service')
    assert len(result) < 1024


def test_real_acquisition_exit_keeps_group_cleanup_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from contextlib import nullcontext
    from climate_monitor import hermes_identity
    from climate_monitor.managed_runtime import ManagedFailure
    from scripts import run_agent_acquisition as acquisition
    binding = {'run_id': 'run', 'attempt': 1, 'checkpoint_dir': str(tmp_path / 'checkpoints')}
    monkeypatch.setattr(acquisition, 'RequestBudget', lambda *a: SimpleNamespace(remaining_seconds=lambda: 30))
    monkeypatch.setattr(acquisition, 'install_hooks', lambda *a: ({}, tmp_path))
    monkeypatch.setattr(acquisition, '_trusted_tool_events', lambda *a: [])
    monkeypatch.setattr(hermes_identity, 'auth_execution', lambda *a, **k: nullcontext({}))
    with pytest.raises(ManagedFailure) as caught:
        acquisition._invoke_hermes([sys.executable, '-c', 'raise SystemExit(76)'],
                                  tmp_path / 'response', tmp_path / 'binding', binding,
                                  time.monotonic() + 5)
    assert caught.value.evidence['category'] == 'identity_drift'
    assert caught.value.evidence['cleanup']['verified'] is True


def test_real_meeting_auth_exit_stops_batch_and_owns_session(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from climate_monitor import hermes_identity, hermes_auth_state
    from climate_monitor.meetings import process_batch
    from scripts import run_meeting_extraction as worker
    from test_issue136_meetings import _database
    from tests.test_issue87_post_pr106 import _help_with_query_file
    database = _database(tmp_path, ['one', 'two'])
    script, pids = tmp_path / 'fake.py', tmp_path / 'pids'
    script.write_text(
        'import os,sys\n'
        f'with open({str(pids)!r},"a") as out: out.write(str(os.getpid())+" "+str(os.getpgrp())+" "+str(os.getsid(0))+"\\n")\n'
        f'if "--help" in sys.argv: print({_help_with_query_file()!r}); sys.exit(0)\n'
        'print("Authorization: Bearer opaque-secret", file=sys.stderr)\n'
        'sys.exit(78)\n'
    )
    path = tmp_path / 'binding.json'
    path.write_text(json.dumps({'run_id': 'run', 'hermes_snapshot': {}}))
    monkeypatch.setattr(hermes_identity, 'inference_runtime', lambda *a, **k: ([sys.executable, str(script)], {}, tmp_path))
    monkeypatch.setattr(hermes_identity, 'auth_execution', lambda *a, **k: nullcontext({}))
    monkeypatch.setattr(hermes_auth_state, 'verify_auth', lambda *a: None)
    result = process_batch(database, 'batch', prompt_text='extract', prompt_version='1',
                           provider='', model='', extractor=worker._extractor('', '', path))
    assert result['failure']['category'] == 'configuration_auth'
    assert result['failure']['cleanup']['verified'] is True
    assert 'opaque-secret' not in json.dumps(result)
    assert sum(item['status'] == 'failed' for item in result['items']) == 1
    rows = pids.read_text().splitlines()
    assert len(rows) == 2
    for row in rows:
        pid, pgid, sid = map(int, row.split())
        assert pid == pgid == sid
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)


def test_failed_execution_cleanup_keeps_durable_owner(tmp_path, monkeypatch):
    from climate_monitor import managed_runtime as runtime
    cleanup = runtime.cleanup_group
    def cannot_verify(process, **kwargs):
        evidence = cleanup(process, **kwargs)
        assert evidence['verified']  # Do not leave a real process behind in this fixture.
        return {**evidence, 'verified': False}
    monkeypatch.setattr(runtime, 'cleanup_group', cannot_verify)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.run_managed([sys.executable, '-c', 'pass'], capture_output=True, state_dir=tmp_path)
    assert not caught.value.evidence['recoverable']
    assert not caught.value.evidence['retryable']
    markers = list((tmp_path / 'managed-processes').glob('*.json'))
    assert len(markers) == 1
    assert markers[0].stat().st_mode & 0o777 == 0o600


def test_terminal_report_checkpoint_cannot_be_retried_without_binding(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from scripts import run_climate_monitor as report
    from climate_monitor.managed_runtime import ManagedFailure
    checkpoint = tmp_path / 'checkpoint.json'
    checkpoint.write_text(json.dumps({
        'input_sha256': report._canonical_digest({'instruction': 'input', 'model': '', 'provider': ''}),
        'status': 'failed', 'attempt': 1, 'failure': ManagedFailure('internal').evidence,
    }))
    monkeypatch.setattr(report, '_hermes_authoring_invocation', lambda *a, **k: pytest.fail('terminal checkpoint retried'))
    with pytest.raises(ManagedFailure):
        report._checkpointed_authoring(checkpoint, 'input', args=SimpleNamespace(model='', model_provider=''),
                                       help_stdout='', validate=lambda value: value)


@pytest.mark.parametrize('category', [None, {}, [], 42, 'unknown'])
def test_malformed_failure_category_is_bounded_terminal(category):
    from climate_monitor.managed_runtime import SCHEMA, read_failure
    failure = read_failure({'schema_version': SCHEMA, 'category': category,
                            'cleanup': {'verified': True, 'pgid': 10 ** 2048}})
    assert failure.evidence['category'] == 'internal'
    assert not failure.evidence['retryable']
    assert len(json.dumps(failure.evidence)) < 1024


def test_unreadable_owner_directory_blocks_recovery(tmp_path):
    from climate_monitor.managed_runtime import ManagedFailure, verify_quiescent
    directory = tmp_path / 'managed-processes'
    directory.mkdir(mode=0o700)
    directory.chmod(0)
    try:
        with pytest.raises(ManagedFailure):
            verify_quiescent(tmp_path)
    finally:
        directory.chmod(0o700)


def test_cancellation_at_successful_exit_observation_is_not_success(monkeypatch):
    from climate_monitor import managed_runtime as runtime
    original = runtime._leader_status
    sent = []
    def observe(process):
        result = original(process)
        if result is not None and not sent:
            sent.append(True)
            os.kill(os.getpid(), signal.SIGTERM)
        return result
    monkeypatch.setattr(runtime, '_leader_status', observe)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.run_managed([sys.executable, '-c', 'pass'])
    assert sent and caught.value.evidence['category'] == 'timeout_cancelled'
    assert caught.value.evidence['cleanup']['verified']


def test_marker_retention_never_unlinks_by_name(tmp_path, monkeypatch):
    from climate_monitor import managed_runtime as runtime
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path.parent.name == 'managed-processes':
            pytest.fail('immutable process receipt must not be unlinked')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)
    completed = runtime.run_managed([sys.executable, '-c', 'pass'], state_dir=tmp_path)
    assert completed.cleanup['verified']
    markers = list((tmp_path / 'managed-processes').glob('*.json'))
    assert len(markers) == 1
    assert json.loads(markers[0].read_bytes()) == {'pgid': completed.cleanup['pgid']}


@pytest.mark.parametrize('cancel_signal', [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize('verified', [True, False])
def test_first_cancellation_during_cleanup_is_preserved(tmp_path, monkeypatch, cancel_signal, verified):
    from climate_monitor import managed_runtime as runtime
    ready = tmp_path / 'descendant'
    child = (
        'import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
        f'open({str(ready)!r},"w").write(str(os.getpid())); time.sleep(60)'
    )
    # The readiness handshake proves TERM resistance before the leader exits 0.
    leader = (
        'import pathlib,subprocess,sys,time; '
        f'subprocess.Popen([sys.executable,"-c",{child!r}],'
        'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
        f'ready=pathlib.Path({str(ready)!r}); '
        '\nwhile not ready.exists(): time.sleep(.01)\n'
    )
    original_cleanup = runtime.cleanup_group
    original_sleep = time.sleep
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    state = {'cleaning': False, 'signals': 0}

    def cleanup(process, **kwargs):
        assert process.returncode is None  # Exit observed without consuming ownership.
        status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        assert status is not None and status.si_status == 0
        state['cleaning'] = True
        try:
            evidence = original_cleanup(process, **kwargs)
        finally:
            state['cleaning'] = False
        assert evidence['verified'] and evidence['term_sent'] and evidence['kill_sent']
        state['cleanup'] = evidence
        return {**evidence, 'verified': verified}

    def sleep(delay):
        if state['cleaning'] and state['signals'] < 2:
            # cleanup_group sleeps only after TERM and a failed disappearance check.
            os.kill(int(ready.read_text()), 0)
            os.kill(os.getpid(), cancel_signal)
            state['signals'] += 1
        original_sleep(delay)

    monkeypatch.setattr(runtime, 'cleanup_group', cleanup)
    monkeypatch.setattr(runtime.time, 'sleep', sleep)
    failure = None
    try:
        runtime.run_managed([sys.executable, '-c', leader], timeout=10, grace=.15,
                            capture_output=True, state_dir=tmp_path)
    except runtime.ManagedFailure as exc:
        failure = exc

    assert state['signals'] == 2  # Repeated requests must not interrupt cleanup.
    assert {sig: signal.getsignal(sig) for sig in handlers} == handlers
    for pid in (int(ready.read_text()), state['cleanup']['pgid']):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    with pytest.raises(ProcessLookupError):
        os.killpg(state['cleanup']['pgid'], 0)
    assert len(list((tmp_path / 'managed-processes').glob('*.json'))) == 1
    assert failure is not None, 'cancellation during cleanup was discarded as success'
    assert failure.evidence['category'] == 'timeout_cancelled'
    assert failure.evidence['cleanup']['verified'] is verified
    assert failure.evidence['recoverable'] is verified
    assert failure.evidence['retryable'] is False
    assert len(json.dumps(failure.evidence)) < 1024


@pytest.mark.parametrize('bad_pgid', [None, '123', True, 0, 1, -1, 2 ** 31, 'own', 'missing'])
def test_round2_malformed_cleanup_pgid_cannot_authorize_retry(bad_pgid):
    from climate_monitor.managed_runtime import ManagedFailure, read_failure, retry_allowed
    cleanup = {'verified': True, 'term_sent': False, 'kill_sent': False,
               'pgid': os.getpgrp() if bad_pgid == 'own' else bad_pgid,
               'private': 'opaque-secret'}
    if bad_pgid == 'missing':
        del cleanup['pgid']
    failure = ManagedFailure('transient_service', cleanup=cleanup)
    for evidence in (failure.evidence, read_failure({
        'schema_version': 'climate-managed-failure.v1', 'category': 'transient_service',
        'cleanup': cleanup}).evidence):
        assert evidence['recoverable'] is False
        assert evidence['retryable'] is False
        assert evidence['cleanup']['verified'] is False
        assert evidence['cleanup']['pgid'] is None
        assert not retry_allowed({'retryable': True, 'failure': evidence})
        assert 'opaque-secret' not in json.dumps(evidence)
        assert len(json.dumps(evidence)) < 1024


@pytest.mark.parametrize('field,value', [('term_sent', None), ('kill_sent', 'yes'), ('verified', 1)])
def test_round2_malformed_cleanup_flags_fail_closed(field, value):
    from climate_monitor.managed_runtime import ManagedFailure, run_managed
    cleanup = run_managed([sys.executable, '-c', 'pass']).cleanup
    cleanup[field] = value
    failure = ManagedFailure('transient_service', cleanup=cleanup)
    assert not failure.evidence['recoverable'] and not failure.evidence['retryable']


@pytest.mark.parametrize('omitted', [False, True])
def test_round2_serialized_no_cleanup_does_not_prove_quiescence(omitted):
    from climate_monitor.managed_runtime import ManagedFailure, read_failure, retry_allowed
    # Trusted in-process failure before launching any child remains distinct.
    failure = ManagedFailure('transient_service')
    assert failure.evidence['recoverable'] and failure.evidence['retryable']
    evidence = dict(failure.evidence)
    if omitted:
        del evidence['cleanup']
    restored = read_failure(evidence)
    assert not restored.evidence['recoverable']
    assert not restored.evidence['retryable']
    assert not retry_allowed({'retryable': True, 'failure': evidence})


def test_round2_generated_cleanup_receipt_still_authorizes_explicit_transient():
    from climate_monitor.managed_runtime import ManagedFailure, run_managed, read_failure, retry_allowed
    cleanup = run_managed([sys.executable, '-c', 'pass']).cleanup
    assert cleanup['verified'] and cleanup['pgid'] != os.getpgrp()
    with pytest.raises(ProcessLookupError):
        os.killpg(cleanup['pgid'], 0)
    evidence = ManagedFailure('transient_service', cleanup=cleanup).evidence
    assert read_failure(evidence).evidence == evidence
    assert retry_allowed({'retryable': True, 'failure': evidence})


@pytest.mark.parametrize('message,category', [
    ('Hermes auth state inconsistent or unsealed; start a fresh run', 'configuration_auth'),
    ('Hermes effective identity changed; start a fresh run', 'identity_drift'),
])
def test_round2_classified_raw_meeting_failure_stops_batch(tmp_path, message, category):
    from climate_monitor.meetings import process_batch, meeting_retry_run
    from test_issue136_meetings import _database
    database = _database(tmp_path, ['one', 'two'])
    calls = []
    def extract(request):
        calls.append(request)
        raise ValueError(message + ' opaque-secret')
    options = dict(prompt_text='extract', prompt_version='1', provider='', model='', extractor=extract)
    result = process_batch(database, 'batch', **options)
    assert len(calls) == 1
    assert result['failure']['category'] == category
    assert [item['status'] for item in result['items']] == ['failed', 'pending']
    assert not result['failure']['retryable']
    assert 'fresh run' in result['error_message']
    assert 'opaque-secret' not in json.dumps(result)
    assert meeting_retry_run(database, batch_id='batch') is None
    with pytest.raises(ValueError, match='terminal.*fresh run'):
        process_batch(database, 'batch', retry_failed=True, **options)
    assert len(calls) == 1


@pytest.mark.parametrize('managed', [False, True])
def test_round2_meeting_internal_content_failure_scope(tmp_path, managed):
    from climate_monitor.managed_runtime import ManagedFailure
    from climate_monitor.meetings import process_batch, meeting_retry_run
    from test_issue136_meetings import _database
    database = _database(tmp_path, ['one', 'two'])
    calls = []
    def extract(request):
        calls.append(request)
        if len(calls) == 1:
            if managed:
                raise ManagedFailure('internal')
            raise ValueError('content-specific opaque-secret')
        return {'events': []}
    result = process_batch(database, 'batch', prompt_text='extract', prompt_version='1',
                           provider='', model='', extractor=extract)
    assert len(calls) == (1 if managed else 2)
    assert [item['status'] for item in result['items']] == ['failed', 'pending' if managed else 'succeeded']
    assert result['failure']['category'] == 'internal'
    assert not result['failure']['retryable']
    assert 'opaque-secret' not in json.dumps(result)
    assert meeting_retry_run(database, batch_id='batch') is None


@pytest.mark.parametrize('owner', ['meetings', 'report', 'acquisition', 'outer-report'])
def test_round3_independent_meeting_does_not_poison_report_failure(tmp_path, owner):
    from climate_monitor import managed_runtime as runtime
    from climate_monitor.hermes_identity import SNAPSHOT
    from scripts import run_agent_acquisition as acquisition
    from tests.managed_runtime_fixtures import live_managed_group
    binding = tmp_path / 'attempt-1.json'
    binding.write_text(json.dumps({'run_id': 'run', 'attempt': 1}))
    home = tmp_path if owner == 'outer-report' else tmp_path / SNAPSHOT / (
        'attempt-1' if owner == 'acquisition' else owner)
    with live_managed_group(home) as pid:
        # Automatic meeting work has already started when report authoring fails.
        report = runtime.run_managed([sys.executable, '-c', 'raise SystemExit(75)'],
                                     state_dir=tmp_path / SNAPSHOT / 'report-child')
        failure = runtime.ManagedFailure('transient_service', returncode=75, cleanup=report.cleanup)
        acquisition._write_result(binding, exit_code=75, retryable=True, error=None,
                                  resume_phase='report', failure=failure)
        os.kill(pid, 0)
        os.killpg(pid, 0)
        result = json.loads((tmp_path / 'attempt-1-result.json').read_text())
        state = json.loads((tmp_path / 'runtime.json').read_text())
        if owner == 'meetings':
            assert result['failure'] == failure.evidence
            assert result['retryable'] is True
            assert state['state'] == 'finished'
        else:
            assert not result['failure']['recoverable'] and not result['retryable']
            assert state['state'] == 'cleanup_failed'
        # The meeting/phase owner must still reject its own live group.
        with pytest.raises(runtime.ManagedFailure) as caught:
            runtime.verify_quiescent(home)
        assert not caught.value.evidence['recoverable']
        os.kill(pid, 0)


@pytest.mark.parametrize('operation', ['report-resume', 'acquisition-resume', 'startup', 'execute'])
def test_round3_acquisition_entrypoints_ignore_only_independent_meeting(tmp_path, monkeypatch, operation):
    from climate_monitor.management import ManagementService
    from climate_monitor.hermes_identity import SNAPSHOT
    from climate_monitor.managed_runtime import ManagedFailure
    from scripts import run_agent_acquisition as acquisition
    from test_issue94_management_console import _store, _definition
    from tests.managed_runtime_fixtures import live_managed_group
    monkeypatch.setenv('CLIMATE_MANAGED_STATE_DIR', str(tmp_path / 'state'))
    store = _store(tmp_path)
    store.save(_definition(tmp_path), actor='operator')
    launches = []
    service = ManagementService(store=store, runtime_root=tmp_path / 'runs',
                                launcher=lambda b: launches.append(b) or 4321)
    started = service.start()
    run = service._run_dir(started['run_id'])
    binding = service.binding(started['run_id'])
    if operation == 'report-resume':
        Path(binding['frozen_report_input']).write_text('{}')
    # Isolate resume/start checks from the separately covered result writer.
    (run / 'attempt-1-result.json').write_text(json.dumps({
        'exit_code': 75, 'retryable': True, 'resume_phase': 'report',
        'failure': ManagedFailure('transient_service', cleanup=verified_cleanup()).evidence,
    }))
    with live_managed_group(run / SNAPSHOT / 'meetings') as pid:
        if operation.endswith('resume'):
            resumed = service.attach_or_resume(started['run_id'])
            assert resumed['attempt'] == (1 if operation == 'report-resume' else 2)
            assert len(launches) == 2
        elif operation == 'startup':
            service._assert_no_startup_owner(binding)
        else:
            entered = []
            monkeypatch.setattr(acquisition, '_execute_locked', lambda *a, **k: entered.append(True) or 0)
            assert acquisition.execute(run / 'attempt-1.json') == 0
            assert entered == [True]
        os.kill(pid, 0)
        os.killpg(pid, 0)


@pytest.mark.parametrize('relative', ['managed-processes', 'hermes-private/report/managed-processes',
                                     'hermes-private/attempt-1/managed-processes',
                                     'other/meetings/managed-processes',
                                     'hermes-private/meetings-other/managed-processes'])
@pytest.mark.parametrize('fault', ['ambiguous', 'malformed', 'symlink', 'unreadable'])
def test_round3_same_owner_bad_markers_still_block(tmp_path, relative, fault):
    from scripts import run_agent_acquisition as acquisition
    from climate_monitor.managed_runtime import ManagedFailure
    marker_root = tmp_path / relative
    marker_root.mkdir(parents=True, mode=0o700)
    marker = marker_root / 'owner.json'
    marker.write_text('not-json' if fault == 'malformed' else '{"pgid":null}')
    marker.chmod(0o600)
    if fault == 'symlink':
        moved = marker_root.with_name('real-markers')
        marker_root.rename(moved)
        marker_root.symlink_to(moved, target_is_directory=True)
    if fault == 'unreadable':
        marker_root.chmod(0)
    binding = tmp_path / 'attempt-1.json'
    binding.write_text(json.dumps({'run_id': 'run', 'attempt': 1}))
    try:
        acquisition._write_result(binding, exit_code=75, retryable=True, error=None,
                                  failure=ManagedFailure('transient_service', cleanup=verified_cleanup()))
    finally:
        if fault == 'unreadable':
            marker_root.chmod(0o700)
    result = json.loads((tmp_path / 'attempt-1-result.json').read_text())
    assert not result['failure']['recoverable'] and not result['retryable']
    assert json.loads((tmp_path / 'runtime.json').read_text())['state'] == 'cleanup_failed'


@pytest.mark.parametrize('component', ['hermes-private', 'hermes-private/meetings'])
@pytest.mark.parametrize('fault', ['symlink', 'file', 'unreadable'])
def test_round3_exclusion_boundary_must_be_a_real_readable_directory(tmp_path, component, fault):
    from scripts import run_agent_acquisition as acquisition
    from climate_monitor.managed_runtime import ManagedFailure
    path = tmp_path / component
    path.parent.mkdir(parents=True, exist_ok=True)
    if fault == 'symlink':
        target = tmp_path / 'outside-exclusion'
        target.mkdir()
        path.symlink_to(target, target_is_directory=True)
    elif fault == 'file':
        path.write_text('not a directory')
    else:
        path.mkdir(mode=0)
    binding = tmp_path / 'attempt-1.json'
    binding.write_text(json.dumps({'run_id': 'run', 'attempt': 1}))
    try:
        acquisition._write_result(binding, exit_code=75, retryable=True, error=None,
                                  failure=ManagedFailure('transient_service', cleanup=verified_cleanup()))
    finally:
        if fault == 'unreadable':
            path.chmod(0o700)
    result = json.loads((tmp_path / 'attempt-1-result.json').read_text())
    assert not result['failure']['recoverable'] and not result['retryable']


@pytest.mark.parametrize('component', ['hermes-private', 'hermes-private/meetings'])
@pytest.mark.parametrize('replacement', ['symlink', 'directory'])
def test_round4_excluded_binding_swap(tmp_path, monkeypatch, component, replacement):
    from climate_monitor import managed_runtime as runtime
    (tmp_path / 'hermes-private/meetings').mkdir(parents=True)
    target = tmp_path / component
    outside = tmp_path / 'outside'
    outside.mkdir()
    swapped = []
    original_stat, original_lstat = os.stat, os.lstat
    def observe(original, path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if isinstance(path, int):
            return result
        parent = kwargs.get('dir_fd')
        observed = Path(os.readlink('/proc/self/fd/' + str(parent))) / path if parent is not None else Path(path)
        if observed == target and not swapped:
            swapped.append(True)
            target.rename(target.with_name(target.name + '-old'))
            if replacement == 'symlink':
                target.symlink_to(outside, target_is_directory=True)
            else:
                target.mkdir()
        return result
    monkeypatch.setattr(os, 'stat', lambda *a, **k: observe(original_stat, *a, **k))
    monkeypatch.setattr(os, 'lstat', lambda *a, **k: observe(original_lstat, *a, **k))
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.verify_acquisition_quiescent(tmp_path)
    assert swapped
    assert not caught.value.evidence['recoverable']


@pytest.mark.parametrize('creation', ['managed-processes', 'hermes-private', 'hermes-private/meetings'])
def test_round4_directory_creation_after_scan_fails_closed(tmp_path, monkeypatch, creation):
    from climate_monitor import managed_runtime as runtime
    target = tmp_path / creation
    target.parent.mkdir(parents=True, exist_ok=True)
    original = os.scandir
    created = []
    class Scan:
        def __init__(self, path):
            self.scan = original(path)
            self.path = Path(os.readlink('/proc/self/fd/' + str(path))) if isinstance(path, int) else Path(path)
        def __iter__(self):
            return self
        def __next__(self):
            return next(self.scan)
        def __enter__(self):
            self.scan.__enter__()
            return self
        def __exit__(self, *args):
            result = self.scan.__exit__(*args)
            if self.path == target.parent and not created:
                created.append(True)
                target.mkdir()
                if creation == 'managed-processes':
                    (target / 'owner.json').write_text('{"pgid":null}')
            return result
    monkeypatch.setattr(os, 'scandir', Scan)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.verify_acquisition_quiescent(tmp_path)
    assert created
    assert not caught.value.evidence['recoverable']


@pytest.mark.parametrize('owner', ['acquisition', 'strict', 'excluded-meeting'])
@pytest.mark.parametrize('fault', ['file', 'dangling', 'symlink', 'fifo', 'socket', 'unreadable'])
def test_round4_corrupt_marker_boundary(tmp_path, owner, fault):
    import socket
    from climate_monitor import managed_runtime as runtime
    home = tmp_path / 'hermes-private/meetings' if owner == 'excluded-meeting' else tmp_path / 'report'
    home.mkdir(parents=True)
    boundary = home / 'managed-processes'
    sock = None
    if fault == 'file':
        boundary.write_text('opaque-secret')
    elif fault in ('dangling', 'symlink'):
        target = tmp_path / 'outside'
        if fault == 'symlink':
            target.mkdir()
        boundary.symlink_to(target, target_is_directory=True)
    elif fault == 'fifo':
        os.mkfifo(boundary)
    elif fault == 'socket':
        sock = socket.socket(socket.AF_UNIX)
        descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            sock.bind(f'/proc/self/fd/{descriptor}/{boundary.name}')
        finally:
            os.close(descriptor)
    else:
        boundary.mkdir(mode=0)
    try:
        if owner == 'excluded-meeting':
            runtime.verify_acquisition_quiescent(tmp_path)
        verify = runtime.verify_acquisition_quiescent if owner == 'acquisition' else runtime.verify_quiescent
        with pytest.raises(runtime.ManagedFailure) as caught:
            verify(home if owner == 'excluded-meeting' else tmp_path)
        assert not caught.value.evidence['recoverable']
        assert 'opaque-secret' not in json.dumps(caught.value.evidence)
    finally:
        if sock is not None:
            sock.close()
        if fault == 'unreadable':
            boundary.chmod(0o700)


@pytest.mark.parametrize('fault', ['symlink', 'directory', 'fifo', 'socket', 'oversized',
                                 'malformed', 'list', 'unknown-name', 'unreadable'])
def test_round4_corrupt_marker_receipt(tmp_path, fault):
    import socket
    from climate_monitor import managed_runtime as runtime
    directory = tmp_path / 'managed-processes'
    directory.mkdir(mode=0o700)
    marker = directory / 'owner.json'
    sock = None
    if fault == 'symlink':
        target = tmp_path / 'outside.json'
        target.write_text(json.dumps({'pgid': verified_cleanup()['pgid']}))
        marker.symlink_to(target)
    elif fault == 'directory':
        marker.mkdir()
    elif fault == 'fifo':
        os.mkfifo(marker)
    elif fault == 'socket':
        sock = socket.socket(socket.AF_UNIX)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            sock.bind(f'/proc/self/fd/{descriptor}/{marker.name}')
        finally:
            os.close(descriptor)
    elif fault == 'oversized':
        marker.write_text(json.dumps({'pgid': verified_cleanup()['pgid']}) + ' ' * 8192)
        marker.chmod(0o600)
    elif fault == 'list':
        marker.write_text('[]')
        marker.chmod(0o600)
    elif fault == 'unknown-name':
        marker.with_suffix('.unexpected').write_text('opaque-secret')
        marker.with_suffix('.unexpected').chmod(0o600)
    else:
        marker.write_text('opaque-secret')
        marker.chmod(0o600)
        if fault == 'unreadable':
            marker.chmod(0)
    def blocked_read(signum, frame):
        raise AssertionError('verification blocked on a non-regular receipt')
    previous = signal.signal(signal.SIGALRM, blocked_read)
    signal.setitimer(signal.ITIMER_REAL, 2)
    try:
        with pytest.raises(runtime.ManagedFailure) as caught:
            runtime.verify_quiescent(tmp_path)
        assert not caught.value.evidence['recoverable']
        assert 'opaque-secret' not in json.dumps(caught.value.evidence)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if sock is not None:
            sock.close()
        if fault == 'unreadable':
            marker.chmod(0o600)


def test_round4_real_stale_receipt_is_retained_read_only(tmp_path):
    from climate_monitor import managed_runtime as runtime
    directory = tmp_path / 'managed-processes'
    directory.mkdir(mode=0o700)
    marker = directory / 'owner.json'
    marker.write_text(json.dumps({'pgid': verified_cleanup()['pgid']}))
    marker.chmod(0o600)
    before = marker.read_bytes()
    runtime.verify_quiescent(tmp_path)
    assert marker.read_bytes() == before


@pytest.mark.parametrize('component', ['report', 'report/managed-processes'])
@pytest.mark.parametrize('replacement', ['symlink', 'directory'])
def test_round4_same_owner_directory_swap_during_scan(tmp_path, monkeypatch, component, replacement):
    from climate_monitor import managed_runtime as runtime
    target = tmp_path / component
    target.mkdir(parents=True, mode=0o700)
    outside = tmp_path / 'outside'
    outside.mkdir()
    original = os.scandir
    swapped = []
    class Scan:
        def __init__(self, path):
            self.scan = original(path)
            self.path = Path(os.readlink('/proc/self/fd/' + str(path))) if isinstance(path, int) else Path(path)
        def __iter__(self):
            return self
        def __next__(self):
            return next(self.scan)
        def __enter__(self):
            self.scan.__enter__()
            return self
        def __exit__(self, *args):
            result = self.scan.__exit__(*args)
            if self.path == target and not swapped:
                swapped.append(True)
                target.rename(target.with_name(target.name + '-old'))
                if replacement == 'symlink':
                    target.symlink_to(outside, target_is_directory=True)
                else:
                    target.mkdir()
            return result
    monkeypatch.setattr(os, 'scandir', Scan)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.verify_acquisition_quiescent(tmp_path)
    assert swapped
    assert not caught.value.evidence['recoverable']


@pytest.mark.parametrize('replacement', ['symlink', 'fifo', 'receipt'])
def test_round4_receipt_replacement_before_open(tmp_path, monkeypatch, replacement):
    from climate_monitor import managed_runtime as runtime
    directory = tmp_path / 'managed-processes'
    directory.mkdir(mode=0o700)
    marker = directory / 'owner.json'
    marker.write_text(json.dumps({'pgid': verified_cleanup()['pgid']}))
    marker.chmod(0o600)
    original = os.open
    swapped = []
    def open_receipt(path, flags, *args, **kwargs):
        if path == marker.name and not swapped:
            swapped.append(True)
            marker.unlink()
            if replacement == 'symlink':
                marker.symlink_to(tmp_path / 'missing')
            elif replacement == 'fifo':
                os.mkfifo(marker)
            else:
                marker.write_text('{"pgid":null}')
                marker.chmod(0o600)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', open_receipt)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.verify_quiescent(tmp_path)
    assert swapped and os.path.lexists(marker)
    assert not caught.value.evidence['recoverable']


@pytest.mark.parametrize('fault', ['none', 'scan', 'read', 'malformed'])
def test_round4_descriptors_close_on_success_and_errors(tmp_path, monkeypatch, fault):
    from climate_monitor import managed_runtime as runtime
    directory = tmp_path / 'hermes-private/report/managed-processes'
    directory.mkdir(parents=True, mode=0o700)
    (tmp_path / 'hermes-private/meetings').mkdir()
    marker = directory / 'owner.json'
    marker.write_text('invalid' if fault == 'malformed' else json.dumps({'pgid': verified_cleanup()['pgid']}))
    marker.chmod(0o600)
    before = set(os.listdir('/proc/self/fd'))
    def fail(*args, **kwargs):
        raise OSError('opaque-secret')
    if fault in ('scan', 'read'):
        monkeypatch.setattr(os, {'scan': 'scandir', 'read': 'read'}[fault], fail)
    if fault == 'none':
        runtime.verify_acquisition_quiescent(tmp_path)
    else:
        with pytest.raises(runtime.ManagedFailure) as caught:
            runtime.verify_acquisition_quiescent(tmp_path)
        assert not caught.value.evidence['recoverable']
        assert 'opaque-secret' not in json.dumps(caught.value.evidence)
    assert set(os.listdir('/proc/self/fd')) == before


@pytest.mark.parametrize('component', ['hermes-private', 'hermes-private/meetings'])
@pytest.mark.parametrize('replacement', ['symlink', 'directory'])
def test_round4_excluded_binding_rechecked_after_visit(tmp_path, monkeypatch, component, replacement):
    from climate_monitor import managed_runtime as runtime
    (tmp_path / 'hermes-private/meetings').mkdir(parents=True)
    late = tmp_path / 'zz-last'
    late.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = tmp_path / component
    original = os.scandir
    swapped = []
    class Scan:
        def __init__(self, path):
            self.scan = original(path)
            self.path = Path(os.readlink('/proc/self/fd/' + str(path))) if isinstance(path, int) else Path(path)
        def __iter__(self):
            return self
        def __next__(self):
            return next(self.entries)
        def __enter__(self):
            # Explicit ordering makes the swap happen after the exclusion visit.
            self.entries = iter(sorted(self.scan.__enter__(), key=lambda entry: entry.name))
            return self
        def __exit__(self, *args):
            result = self.scan.__exit__(*args)
            if self.path == late and not swapped:
                swapped.append(True)
                target.rename(target.with_name(target.name + '-old'))
                if replacement == 'symlink':
                    target.symlink_to(outside, target_is_directory=True)
                else:
                    target.mkdir()
            return result
    monkeypatch.setattr(os, 'scandir', Scan)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.verify_acquisition_quiescent(tmp_path)
    assert swapped
    assert not caught.value.evidence['recoverable']


def test_round4_verification_cannot_unlink_a_replacement_receipt(tmp_path, monkeypatch):
    from climate_monitor import managed_runtime as runtime
    directory = tmp_path / 'managed-processes'
    directory.mkdir(mode=0o700)
    marker = directory / 'owner.json'
    marker.write_text(json.dumps({'pgid': verified_cleanup()['pgid']}))
    marker.chmod(0o600)
    original = os.unlink
    def replace_before_unlink(path, *args, **kwargs):
        if path == marker.name and kwargs.get('dir_fd') is not None:
            # The name changes after the verifier's last check but before unlink.
            marker.rename(directory / 'old.json')
            marker.write_text('{"pgid":null}')
            marker.chmod(0o600)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'unlink', replace_before_unlink)
    try:
        runtime.verify_quiescent(tmp_path)
    except runtime.ManagedFailure:
        pass
    assert marker.exists(), 'verification removed a receipt it had not verified'


@pytest.mark.parametrize('replacement', ['in-place', 'new-inode'])
def test_round4_receipt_change_after_read_fails_closed(tmp_path, monkeypatch, replacement):
    from climate_monitor import managed_runtime as runtime
    directory = tmp_path / 'managed-processes'
    directory.mkdir(mode=0o700)
    marker = directory / 'owner.json'
    marker.write_text(json.dumps({'pgid': verified_cleanup()['pgid']}))
    marker.chmod(0o600)
    original = runtime.group_exists
    checks = []
    def exists(pgid):
        result = original(pgid)
        checks.append(pgid)
        if len(checks) == 2:
            if replacement == 'new-inode':
                marker.rename(directory / 'old.json')
            marker.write_text('{"pgid":null}')
            marker.chmod(0o600)
        return result
    monkeypatch.setattr(runtime, 'group_exists', exists)
    with pytest.raises(runtime.ManagedFailure) as caught:
        runtime.verify_quiescent(tmp_path)
    assert len(checks) == 2
    assert marker.exists()
    assert not caught.value.evidence['recoverable']
