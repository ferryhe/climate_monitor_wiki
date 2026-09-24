"""V2 intake tests only. No system manager or containment claims."""
import ast
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

def _execution_ast_violations(tree):
    """Defense-in-depth guard, not a proof about arbitrary Python programs."""
    allowed_imports = {'copy', 'base64', 'fcntl', 'hashlib', 'hmac', 'os', 're',
                       'secrets', 'threading', 'time'}
    allowed_from = {'__future__': {'annotations'}, 'pathlib': {'Path'},
                    'climate_monitor': {'host_exec'}}
    allowed_attrs = {
        'v1': {'AUDIENCE', 'INVOCATION_FIELDS', 'MAX_FRAME', 'MAX_STATE', 'PURPOSE',
               'ProtocolError', '_canonical', '_decode', '_directory', '_file',
               '_hex', '_integer', '_invocation', '_mac', '_require'},
        'os': {'O_CREAT', 'O_EXCL', 'O_NOFOLLOW', 'O_WRONLY', 'close', 'fdopen',
               'fsync', 'listdir', 'open', 'replace', 'unlink'},
    }
    dangerous_names = {'Popen', 'run', 'call', 'check_call', 'check_output',
                       'system', 'popen', 'exec', 'eval', 'compile', '__import__', 'getattr'}
    def dangerous(name):
        return name in dangerous_names or name.startswith(('exec', 'spawn', 'fork', 'posix_spawn'))
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(node for alias in node.names if alias.name not in allowed_imports)
        elif isinstance(node, ast.ImportFrom):
            if (node.level != 0 or node.module not in allowed_from or
                    any(alias.name not in allowed_from[node.module] for alias in node.names)):
                violations.append(node)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in allowed_attrs and node.attr not in allowed_attrs[node.value.id]:
                violations.append(node)
        elif isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, (ast.Name, ast.Attribute)) and dangerous(
                    target.id if isinstance(target, ast.Name) else target.attr):
                violations.append(node)
    return violations


# Scan the candidate *before* importing it; this never grants root source trust.
_module_path = Path(__file__).resolve().parents[1] / 'climate_monitor/host_exec_v2.py'
assert not _execution_ast_violations(ast.parse(_module_path.read_text()))
from climate_monitor import host_exec_v2 as v2


def invocation(**changes):
    value = dict(invocation_id='a' * 32, run_id='run-test', attempt=1,
                 purpose='hermes-cli-invocation', binding_sha256='b' * 64,
                 frozen_identity_sha256='c' * 64, repository_commit_sha='d' * 40,
                 runtime_sha256='e' * 64, timeout_seconds=60, output_bytes=4096,
                 input_sha256=hashlib.sha256(b'finite prompt').hexdigest())
    return dict(value, **changes)


@pytest.fixture(autouse=True)
def shared_umask():
    previous = os.umask(0o002)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def store(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('intake boundary attempted execution')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(os, 'system', forbidden)
    tmp_path.chmod(0o700)
    path = tmp_path / 'state'
    path.mkdir(mode=0o700)
    checkpoint = v2.IntakeStore.initialize(path, owner_uid=os.getuid())
    state = v2.IntakeStore(path, owner_uid=os.getuid(), expected_checkpoint=checkpoint)
    state.open()
    yield state
    state.close()


def test_seal_refuse_duplicate_conflict_and_restart(store):
    digest = store.seal_input(b'finite prompt')
    assert digest == invocation()['input_sha256']
    result = store.apply(invocation(), 'submit', '1' * 64)
    assert result['status'] == 'not_ready'
    assert result['ready'] is result['retryable'] is False
    assert result['cleanup'] == {'status': 'unverified'}
    record = store.state['invocations']['a' * 32]
    assert set(record) == {'invocation', 'digest', 'phase'}
    assert record['phase'] == 'refused'
    assert store.apply(invocation(), 'submit', '2' * 64)['status'] == 'not_ready'
    assert store.state['invocations']['a' * 32] == record
    assert store.apply(invocation(attempt=2), 'submit', '3' * 64)['status'] == 'conflict'
    checkpoint = store.checkpoint
    store.close()
    restarted = v2.IntakeStore(store.directory, owner_uid=os.getuid(), expected_checkpoint=checkpoint)
    restarted.open()
    try:
        assert restarted.input_bytes(digest) == b'finite prompt'
        assert restarted.apply(invocation(), 'readback', '4' * 64)['status'] == 'not_ready'
        assert restarted.state['invocations']['a' * 32] == record
        assert restarted.apply(invocation(), 'cancel', '5' * 64)['status'] == 'withdrawn'
        assert restarted.apply(invocation(), 'submit', '6' * 64)['status'] == 'withdrawn'
        assert restarted.apply(invocation(), 'submit', '1' * 64)['status'] == 'replay'
    finally:
        restarted.close()


def test_unregistered_input_and_unknown_cancel_do_not_create_intent(store):
    assert store.apply(invocation(), 'submit', '1' * 64)['status'] == 'not_ready'
    assert store.apply(invocation(), 'cancel', '2' * 64)['status'] == 'unknown'
    assert not store.state['invocations']


@pytest.mark.parametrize('changes', [dict(command='id'), dict(argv=['id']),
    dict(env={}), dict(path='/tmp/x'), dict(uid=0), dict(provider='auto'),
    dict(model='x'), dict(input_sha256='z' * 64), dict(attempt=True),
    dict(invocation_id='../etc'), dict(output_bytes=1048577)])
def test_rejects_unbounded_or_caller_selected_execution(store, changes):
    with pytest.raises(v2.ProtocolError):
        store.apply(invocation(**changes), 'submit', '1' * 64)


def test_explicit_initialization_missing_corrupt_and_rollback_fail_closed(store):
    initial = store.checkpoint
    old = (store.directory / 'ledger-v2.json').read_bytes()
    store.seal_input(b'finite prompt')
    newer = store.checkpoint
    assert initial != newer
    store.close()
    with pytest.raises(v2.ProtocolError):
        v2.IntakeStore.initialize(store.directory, owner_uid=os.getuid())
    for raw in (old, b'{}', b'invalid'):
        (store.directory / 'ledger-v2.json').write_bytes(raw)
        with pytest.raises(v2.ProtocolError):
            v2.IntakeStore(store.directory, owner_uid=os.getuid(), expected_checkpoint=newer).open()
    (store.directory / 'ledger-v2.json').unlink()
    with pytest.raises(v2.ProtocolError):
        v2.IntakeStore(store.directory, owner_uid=os.getuid(), expected_checkpoint=newer).open()
    with pytest.raises(v2.ProtocolError):
        v2.IntakeStore.initialize(store.directory, owner_uid=os.getuid())


def test_parallel_duplicate_refusals_are_one_record(store):
    store.seal_input(b'finite prompt')
    results = []
    threads = [threading.Thread(target=lambda n=n: results.append(
        store.apply(invocation(), 'submit', f'{n:064x}'))) for n in range(1, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert len(results) == 8 and {r['status'] for r in results} == {'not_ready'}
    assert len(store.state['invocations']) == 1
    assert len(store.state['nonces']) == 8


def test_second_store_cannot_open_locked_directory(store):
    with pytest.raises(v2.ProtocolError):
        v2.IntakeStore(store.directory, owner_uid=os.getuid(),
                      expected_checkpoint=store.checkpoint).open()


def test_close_serializes_with_active_persistence(store, monkeypatch):
    store.seal_input(b'finite prompt')
    entered = threading.Event()
    release = threading.Event()
    close_attempted = threading.Event()
    close_finished = threading.Event()
    failures = []
    closer = None
    class ObservedLock:
        """Signal only when close actually attempts the shared lock."""
        def __init__(self):
            self.raw = threading.Lock()
        def __enter__(self):
            if threading.current_thread() is closer:
                close_attempted.set()
            self.raw.acquire()
            return self
        def __exit__(self, *_):
            self.raw.release()
    store.lock = ObservedLock()
    persist = store._persist
    def waiting_persist(state):
        entered.set()
        assert release.wait(5)
        persist(state)
    monkeypatch.setattr(store, '_persist', waiting_persist)
    def submit():
        try:
            assert store.apply(invocation(), 'submit', '1' * 64)['status'] == 'not_ready'
        except BaseException as error:
            failures.append(error)
    def close():
        try:
            store.close()
        except BaseException as error:
            failures.append(error)
        finally:
            close_finished.set()
    writer = threading.Thread(target=submit)
    closer = threading.Thread(target=close)
    writer.start()
    try:
        assert entered.wait(5)
        closer.start()
        assert close_attempted.wait(5)
        assert not close_finished.is_set()
    finally:
        release.set()
        writer.join(5)
        if closer.ident is not None:
            closer.join(5)
    assert not writer.is_alive() and not closer.is_alive() and close_finished.is_set()
    assert not failures
    assert store.fd is None
    reopened = v2.IntakeStore(store.directory, owner_uid=os.getuid(),
                              expected_checkpoint=store.checkpoint)
    reopened.open()
    try:
        assert reopened.state['invocations']['a' * 32]['phase'] == 'refused'
    finally:
        reopened.close()


def test_tampered_input_rejected_even_with_matching_checkpoint(store):
    store.seal_input(b'finite prompt')
    path = store.directory / 'ledger-v2.json'
    state = json.loads(path.read_bytes())
    state['inputs'][invocation()['input_sha256']] = 'c3Vic3RpdHV0ZWQ='
    raw = v2._canonical(state)
    store.close()
    path.write_bytes(raw)
    with pytest.raises(v2.ProtocolError):
        v2.IntakeStore(store.directory, owner_uid=os.getuid(),
                      expected_checkpoint=hashlib.sha256(raw).hexdigest()).open()


@pytest.mark.parametrize('fault', ['file_fsync', 'replace', 'directory_fsync'])
def test_crash_boundaries_require_independent_new_checkpoint(store, monkeypatch, fault):
    store.seal_input(b'finite prompt')
    checkpoint = store.checkpoint
    original_fsync = os.fsync
    count = 0
    def fail_fsync(fd):
        nonlocal count
        count += 1
        if count == (1 if fault == 'file_fsync' else 2):
            raise OSError('injected persistence uncertainty')
        original_fsync(fd)
    with monkeypatch.context() as patch:
        if fault == 'replace':
            patch.setattr(os, 'replace', lambda *a, **kw: (_ for _ in ()).throw(OSError('injected')))
        else:
            patch.setattr(os, 'fsync', fail_fsync)
        with pytest.raises(v2.ProtocolError):
            store.apply(invocation(), 'submit', '1' * 64)
    assert store.failed
    store.close()
    restart = v2.IntakeStore(store.directory, owner_uid=os.getuid(), expected_checkpoint=checkpoint)
    if fault == 'directory_fsync':
        with pytest.raises(v2.ProtocolError):
            restart.open()
    else:
        restart.open()
        try:
            assert not restart.state['invocations']
        finally:
            restart.close()


def test_persistence_failure_poisons_intake(store, monkeypatch):
    store.seal_input(b'finite prompt')
    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', lambda *_: (_ for _ in ()).throw(OSError('private diagnostic')))
        with pytest.raises(v2.ProtocolError, match='non-retryable'):
            store.apply(invocation(), 'submit', '1' * 64)
    with pytest.raises(v2.ProtocolError):
        store.apply(invocation(), 'submit', '2' * 64)


def test_capacity_preserves_tombstones(store):
    store.seal_input(b'finite prompt')
    store.apply(invocation(), 'submit', '1' * 64)
    store.apply(invocation(), 'cancel', '2' * 64)
    store.max_nonces = 2
    assert store.apply(invocation(), 'readback', '3' * 64)['status'] == 'capacity'
    assert store.state['invocations']['a' * 32]['phase'] == 'withdrawn'


def test_finite_input_limit(store):
    assert store.seal_input(b'x' * v2.MAX_INPUT)
    with pytest.raises(v2.ProtocolError):
        store.seal_input(b'x' * (v2.MAX_INPUT + 1))


def credential(path, uid):
    value = dict(version=2, key_id='test-key', secret='ab' * 32,
                 audience=v2.AUDIENCE, purpose=v2.PURPOSE, uid=uid,
                 expires_at=int(time.time()) + 3600, enabled=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)


def test_authenticated_intake_distinct_peer_and_no_execution(store, tmp_path):
    token_dir = tmp_path / 'keys'
    token_dir.mkdir(mode=0o700)
    token = token_dir / 'credential.json'
    credential(token, 12345)
    store.seal_input(b'finite prompt')
    broker = v2.IntakeBroker(store, token, credential_owner_uid=os.getuid())
    request = v2.make_request(token, invocation(), owner_uid=os.getuid())
    reply = broker.handle(request, peer_uid=12345)
    assert v2.verify_reply(token, request, reply, owner_uid=os.getuid())['status'] == 'not_ready'
    with pytest.raises(v2.ProtocolError):
        broker.handle(request, peer_uid=0)
    with pytest.raises(v2.ProtocolError):
        broker.handle(request, peer_uid=12346)
    request['version'] = 1
    with pytest.raises(v2.ProtocolError):
        broker.handle(request, peer_uid=12345)


@pytest.mark.parametrize('changes', [dict(cleanup={'status': 'verified'}),
    dict(status='completed'), dict(status='accepted'), dict(execution={}), dict(ready=True), dict(retryable=True)])
def test_forged_success_and_cleanup_are_rejected_even_when_signed(store, tmp_path, changes):
    token_dir = tmp_path / 'keys'
    token_dir.mkdir(mode=0o700)
    token = token_dir / 'credential.json'
    credential(token, 12345)
    broker = v2.IntakeBroker(store, token, credential_owner_uid=os.getuid())
    request = v2.make_request(token, invocation(), owner_uid=os.getuid())
    reply = broker.handle(request, peer_uid=12345)
    reply['result'].update(changes)
    key = v2._credential(token, os.getuid())
    reply['mac'] = v2._mac(key, b'host-exec-v2/result\0', {k: v for k, v in reply.items() if k != 'mac'})
    with pytest.raises(v2.ProtocolError):
        v2.verify_reply(token, request, reply, owner_uid=os.getuid())


def test_rotation_revocation_and_request_binding(store, tmp_path):
    token_dir = tmp_path / 'keys'
    token_dir.mkdir(mode=0o700)
    token = token_dir / 'credential.json'
    credential(token, 12345)
    broker = v2.IntakeBroker(store, token, credential_owner_uid=os.getuid())
    request = v2.make_request(token, invocation(), owner_uid=os.getuid())
    reply = broker.handle(request, peer_uid=12345)
    other = v2.make_request(token, invocation(), owner_uid=os.getuid())
    with pytest.raises(v2.ProtocolError):
        v2.verify_reply(token, other, reply, owner_uid=os.getuid())
    key = json.loads(token.read_text())
    key.update(key_id='rotated', secret='cd' * 32)
    token.write_text(json.dumps(key))
    with pytest.raises(v2.ProtocolError):
        broker.handle(request, peer_uid=12345)
    new = v2.make_request(token, invocation(), owner_uid=os.getuid())
    key['enabled'] = False
    token.write_text(json.dumps(key))
    with pytest.raises(v2.ProtocolError):
        broker.handle(new, peer_uid=12345)


def test_no_runner_or_application_integration():
    module = Path(v2.__file__)
    tree = ast.parse(module.read_text())
    assert not _execution_ast_violations(tree)
    for name in ('api_server.py', 'Caddyfile', 'docker-compose.yml', 'docker-compose.host-hermes.yml'):
        content = (module.parent.parent / name).read_text()
        assert 'host_exec_v2' not in content and 'executor-v2.sock' not in content


def test_execution_ast_guard_catches_import_alias_and_direct_calls():
    for source in ("from subprocess import run\nrun(['id'])",
                   "from os import execve as launch\nlaunch('id', [], {})",
                   "import os\nlaunch = os.execve\nlaunch('id', [], {})",
                   "from climate_monitor import host_exec as v1\nv1.Server(None, None, owner_uid=0)",
                   "from climate_monitor import host_exec as v1\nv1.socket.socket()",
                   "from asyncio import create_subprocess_exec as launch\nlaunch('id')",
                   "from concurrent.futures import ProcessPoolExecutor as launch\nlaunch()",
                   "import asyncio\nasyncio.create_subprocess_shell('id')",
                   "import subprocess as sp\nsp.run(['id'])",
                   "getattr(os, 'system')('id')",
                   "__import__('subprocess').run(['id'])"):
        assert _execution_ast_violations(ast.parse(source)), source


@pytest.fixture
def authenticated(store, tmp_path):
    directory = tmp_path / 'auth'
    directory.mkdir(mode=0o700)
    token = directory / 'key.json'
    credential(token, 12345)
    broker = v2.IntakeBroker(store, token, credential_owner_uid=os.getuid())
    request = v2.make_request(token, invocation(), owner_uid=os.getuid())
    return token, broker, request


@pytest.mark.parametrize('changes', [dict(enabled=False), dict(expires_at=1),
    dict(uid=0), dict(version=1), dict(enabled=1), dict(extra=True)])
def test_invalid_expired_revoked_credentials(authenticated, changes):
    token, broker, request = authenticated
    reply = broker.handle(request, peer_uid=12345)
    key = json.loads(token.read_text())
    key.update(changes)
    token.write_text(json.dumps(key))  # existing 0600 inode, even under umask 0002
    for action in (lambda: broker.handle(request, peer_uid=12345),
                   lambda: v2.make_request(token, invocation(), owner_uid=os.getuid()),
                   lambda: v2.verify_reply(token, request, reply, owner_uid=os.getuid())):
        with pytest.raises(v2.ProtocolError):
            action()


@pytest.mark.parametrize('changes', [dict(extra=True), dict(version=True),
    dict(audience='other'), dict(operation='launch'), dict(nonce='bad'),
    dict(expires_at=1), dict(expires_at=2**63), dict(invocation_sha256='0' * 64)])
def test_invalid_signed_requests_do_not_mutate(authenticated, store, changes):
    token, broker, request = authenticated
    request.update(changes)
    key = v2._credential(token, os.getuid())
    request['mac'] = v2._mac(key, v2.REQUEST_DOMAIN,
                            {k: v for k, v in request.items() if k != 'mac'})
    checkpoint = store.checkpoint
    with pytest.raises(v2.ProtocolError):
        broker.handle(request, peer_uid=12345)
    assert store.checkpoint == checkpoint


@pytest.mark.parametrize('domain', [b'request\0', v2.RESULT_DOMAIN])
def test_request_mac_domain_separation(authenticated, domain):
    token, broker, request = authenticated
    key = v2._credential(token, os.getuid())
    request['mac'] = v2._mac(key, domain, {k: v for k, v in request.items() if k != 'mac'})
    with pytest.raises(v2.ProtocolError):
        broker.handle(request, peer_uid=12345)


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'mac', 'version', 'binding', 'domain'])
def test_malformed_or_forged_reply(authenticated, mutation):
    token, broker, request = authenticated
    reply = broker.handle(request, peer_uid=12345)
    if mutation == 'missing':
        del reply['result']
    elif mutation == 'extra':
        reply['extra'] = True
    elif mutation == 'mac':
        reply['mac'] = '0' * 64
    else:
        if mutation == 'version':
            reply['version'] = True
        elif mutation == 'binding':
            reply['request_sha256'] = '0' * 64
        key = v2._credential(token, os.getuid())
        domain = b'result\0' if mutation == 'domain' else v2.RESULT_DOMAIN
        reply['mac'] = v2._mac(key, domain, {k: v for k, v in reply.items() if k != 'mac'})
    with pytest.raises(v2.ProtocolError):
        v2.verify_reply(token, request, reply, owner_uid=os.getuid())


@pytest.mark.parametrize('raw', [b'', b'{', b'{} ', b'{"x":1,"x":1}',
    b'{"x":NaN}', b'{"x":1.0e0}', b'[]', b'{}{}', b' ' * 8193])
def test_noncanonical_or_unbounded_payload(raw):
    with pytest.raises(v2.ProtocolError):
        v2.decode_message(raw)


def test_wire_roundtrip_bounds_and_authenticated_nonce(authenticated):
    token, broker, request = authenticated
    assert v2.decode_message(v2.encode_message(request)) == request
    with pytest.raises(v2.ProtocolError):
        v2.encode_message({'x': 'x' * 8192})
    broker.handle(request, peer_uid=12345)
    reply = broker.handle(request, peer_uid=12345)
    assert v2.verify_reply(token, request, reply, owner_uid=os.getuid())['status'] == 'replay'
    with pytest.raises(v2.ProtocolError):
        broker.handle(request, peer_uid=True)


@pytest.mark.parametrize('raw', [b'', 'text', bytearray(b'mutable')])
def test_input_rejects_empty_or_mutable_data(store, raw):
    checkpoint = store.checkpoint
    with pytest.raises(v2.ProtocolError):
        store.seal_input(raw)
    assert store.checkpoint == checkpoint


def test_record_and_input_capacity_without_eviction(store, monkeypatch):
    monkeypatch.setattr(v2, 'MAX_RECORDS', 1)
    store.seal_input(b'finite prompt')
    store.apply(invocation(), 'submit', '1' * 64)
    assert store.apply(invocation(invocation_id='f' * 32), 'submit', '2' * 64)['status'] == 'capacity'
    with pytest.raises(v2.ProtocolError):
        store.seal_input(b'other')
    assert len(store.state['inputs']) == len(store.state['invocations']) == 1


@pytest.mark.parametrize('unsafe', ['mode', 'symlink', 'hardlink', 'missing_marker'])
def test_unsafe_storage_refused(store, unsafe):
    checkpoint = store.checkpoint
    path = store.directory / 'ledger-v2.json'
    store.close()
    if unsafe == 'mode':
        path.chmod(0o660)
    elif unsafe == 'symlink':
        target = store.directory / 'other'
        path.rename(target)
        path.symlink_to(target)
    elif unsafe == 'hardlink':
        os.link(path, store.directory / 'other')
    else:
        (store.directory / 'initialized-v2').unlink()
    with pytest.raises(v2.ProtocolError):
        v2.IntakeStore(store.directory, owner_uid=os.getuid(), expected_checkpoint=checkpoint).open()


def test_checkpoint_does_not_bypass_strict_state_schema(store):
    path = store.directory / 'ledger-v2.json'
    value = json.loads(path.read_bytes())
    value['extra'] = {}
    raw = v2._canonical(value)
    store.close()
    path.write_bytes(raw)
    with pytest.raises(v2.ProtocolError):
        v2.IntakeStore(store.directory, owner_uid=os.getuid(),
                      expected_checkpoint=hashlib.sha256(raw).hexdigest()).open()
