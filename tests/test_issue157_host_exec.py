"""Real Linux UDS protocol checks; no runner, credentials or host services."""
import hashlib
import json
import os
import socket
import struct
import subprocess
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from climate_monitor import host_exec as protocol


def credential(path, **changes):
    value = dict(version=1, key_id='test-key', secret='ab' * 32,
                 audience=protocol.AUDIENCE, purpose=protocol.PURPOSE,
                 uid=os.getuid(), expires_at=int(time.time()) + 3600, enabled=True)
    value.update(changes)
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return value


def invocation(**changes):
    value = dict(invocation_id='a' * 32, run_id='run-test', attempt=1,
                 purpose=protocol.PURPOSE, binding_sha256='b' * 64,
                 frozen_identity_sha256='c' * 64, repository_commit_sha='d' * 40,
                 runtime_sha256='e' * 64, timeout_seconds=60, output_bytes=4096)
    value.update(changes)
    return value


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    token = root / 'credential.json'
    credential(token)
    def forbidden(*args, **kwargs):
        pytest.fail('protocol attempted to launch a command')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(os, 'system', forbidden)
    return root, token


@contextmanager
def serving(root, token, **kwargs):
    server = protocol.Server(root, token, owner_uid=os.getuid(), **kwargs)
    server.open()
    try:
        yield server
    finally:
        server.close()


@contextmanager
def exchange_diagnostics(server, *, positive=False):
    """Test-local interception; never patch the shared stdlib socket module."""
    phases = []
    @contextmanager
    def phase(label):
        start = time.monotonic()
        try:
            yield
        finally:
            duration = time.monotonic() - start
            phases.append((label, duration))
            print(f'{label} {duration:.6f}')

    class TimedSocket(socket.socket):
        def connect(self, address):
            with phase('connect'):
                return super().connect(address)

        def sendall(self, data, *args):
            with phase('send'):
                return super().sendall(data, *args)

        def recv(self, size, *args):
            with phase('receive'):
                return super().recv(size, *args)

    receive = protocol._receive
    def timed_receive(sock):
        start = time.monotonic()
        try:
            frame = receive(sock)
        except Exception:
            if isinstance(sock, TimedSocket):
                duration = time.monotonic() - start
                phases.append(('receive-frame-failed', duration))
                print(f'receive-frame-failed {duration:.6f}')
            raise
        if isinstance(sock, TimedSocket):
            duration = time.monotonic() - start
            phases.append(('receive-frame-complete', duration))
            print(f'receive-frame-complete {duration:.6f}')
        return frame

    persist = server._persist
    def timed_persist(state):
        start = time.monotonic()
        label = 'durable-ledger-failed'
        try:
            persist(state)
            label = 'durable-ledger-complete'
        finally:
            # No request, credential, path or exception text enters diagnostics.
            print(f'{label} {time.monotonic() - start:.6f}')

    accept_timeout = server.sock.gettimeout()
    with pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(protocol, 'socket', SimpleNamespace(**{
            **vars(socket), 'socket': TimedSocket}))
        scoped.setattr(protocol, '_receive', timed_receive)
        scoped.setattr(server, '_persist', timed_persist)
        try:
            if positive:
                # Only ordinary positive exchanges opt in; production stays at 1s.
                scoped.setattr(protocol, 'IO_TIMEOUT', 5.0)
                server.sock.settimeout(5.0)
            yield phases
        finally:
            server.sock.settimeout(accept_timeout)


def exchange(server, token, request=None, raw=None, *, positive=False):
    with exchange_diagnostics(server, positive=positive):
        thread = threading.Thread(target=server.serve_once)
        thread.start()
        try:
            if raw is not None:
                with socket.socket(socket.AF_UNIX) as sock:
                    sock.connect(f'/proc/self/fd/{server.fd}/executor.sock')
                    sock.sendall(raw)
                    sock.shutdown(socket.SHUT_WR)
                    return sock.recv(8192)
            return protocol.call(server.directory, token, request, owner_uid=os.getuid())
        finally:
            thread.join(6 if positive else 3)
            assert not thread.is_alive()


def request(token, value=None, operation='submit'):
    return protocol.make_request(token, value or invocation(), operation, owner_uid=os.getuid())


@pytest.mark.parametrize('fail_persistence', [False, True])
def test_positive_exchange_deadline_is_scoped(setup, monkeypatch, capsys, fail_persistence):
    root, token = setup
    assert protocol.IO_TIMEOUT == 1.0
    with serving(root, token) as server:
        persist = server._persist
        def checked_persist(state):
            assert protocol.IO_TIMEOUT == 5.0
            if fail_persistence:
                raise OSError('test persistence failure')
            persist(state)
        monkeypatch.setattr(server, '_persist', checked_persist)
        if fail_persistence:
            with pytest.raises(protocol.ProtocolError) as caught:
                exchange(server, token, request(token), positive=True)
            assert caught.value.retryable is False
        else:
            assert exchange(server, token, request(token), positive=True)['status'] == 'not_ready'
        assert server.sock.gettimeout() == 1.0
    assert protocol.IO_TIMEOUT == 1.0
    assert protocol.socket is socket
    phases = [line.split() for line in capsys.readouterr().out.splitlines()]
    assert {phase for phase, duration in phases} == {
        'connect', 'send', 'receive', 'receive-frame-complete',
        'durable-ledger-failed' if fail_persistence else 'durable-ledger-complete'}
    assert all(float(duration) >= 0 for phase, duration in phases)


def test_default_receive_deadline_is_nonretryable_after_late_persistence(setup, monkeypatch):
    root, token = setup
    entered = threading.Event()
    receive_ready = threading.Event()
    allow_receive = threading.Event()
    client_done = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    outcomes, receive_started_at, client_done_at = [], [], []
    req = request(token)
    assert protocol.IO_TIMEOUT == 1.0
    with serving(root, token) as server:
        persist = server._persist
        def delayed_persist(state):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('test release deadline')
            persist(state)
            completed.set()
        monkeypatch.setattr(server, '_persist', delayed_persist)
        with exchange_diagnostics(server) as phases:
            # Accept may wait longer on a loaded runner; both protocol endpoints
            # still use the unchanged production IO_TIMEOUT=1.0.
            assert server.sock is not None
            server.sock.settimeout(5.0)
            original_receive = protocol._receive
            client_thread = None
            def gated_receive(sock):
                if threading.current_thread() is client_thread:
                    assert entered.wait(5)
                    receive_ready.set()
                    assert allow_receive.wait(5)
                    receive_started_at.append(time.monotonic())
                return original_receive(sock)
            def call_once():
                try:
                    outcomes.append(protocol.call(root, token, req, owner_uid=os.getuid()))
                except BaseException as error:
                    outcomes.append(error)
                finally:
                    client_done_at.append(time.monotonic())
                    client_done.set()
            with pytest.MonkeyPatch.context() as scoped:
                scoped.setattr(protocol, '_receive', gated_receive)
                server_thread = threading.Thread(target=server.serve_once)
                client_thread = threading.Thread(target=call_once)
                server_thread.start()
                client_thread.start()
                try:
                    assert entered.wait(5)
                    assert receive_ready.wait(5)
                    assert not client_done.is_set()
                    assert not completed.is_set()
                    allow_receive.set()
                    client_thread.join(4)
                    assert not client_thread.is_alive()
                    assert len(outcomes) == 1 and isinstance(outcomes[0], protocol.ProtocolError)
                    assert str(outcomes[0]) == 'executor rejected; non-retryable'
                    assert outcomes[0].retryable is False
                    assert client_done_at[0] - receive_started_at[0] >= protocol.IO_TIMEOUT
                    assert not completed.is_set() and not release.is_set()
                    # No reconnect, resend or additional frame after the timeout.
                    assert [phase for phase, _ in phases] == [
                        'connect', 'send', 'receive', 'receive-frame-failed']
                finally:
                    allow_receive.set()
                    release.set()
                    client_thread.join(5)
                    server_thread.join(5)
                    assert not client_thread.is_alive() and not server_thread.is_alive()
            assert completed.is_set()
            assert protocol.IO_TIMEOUT == 1.0
    # The late durable commit survives, but never changes the caller's failure.
    with serving(root, token) as server:
        assert server.state['invocations'] == {
            req['invocation']['invocation_id']: req['invocation_sha256']}
        assert server.state['nonces'] == [hashlib.sha256(
            protocol._canonical([req['key_id'], req['nonce']])).hexdigest()]


@pytest.mark.parametrize('operation', ['submit', 'readback', 'cancel'])
def test_still_valid_near_expiry_key_over_real_uds(setup, monkeypatch, operation):
    root, token = setup
    now = 2_000_000_000
    monkeypatch.setattr(protocol.time, 'time', lambda: now)
    credential(token)
    with serving(root, token) as server:
        initial = request(token)
        assert initial['expires_at'] == now + 60
        assert exchange(server, token, initial, positive=True)['status'] == 'not_ready'

        credential(token, expires_at=now + 30)
        req = request(token, operation=operation)
        result = exchange(server, token, req, positive=True)
        assert req['expires_at'] == now + 30
        assert result['status'] == 'not_ready'
        assert result['ready'] is result['retryable'] is False
        assert result['cleanup'] == {'status': 'unverified'}


@pytest.mark.parametrize('remaining', [0, -1])
def test_request_rejects_expired_key_locally(setup, monkeypatch, remaining):
    _, token = setup
    now = 2_000_000_000
    monkeypatch.setattr(protocol.time, 'time', lambda: now)
    credential(token, expires_at=now + remaining)
    with pytest.raises(protocol.ProtocolError, match='executor rejected') as caught:
        request(token)
    assert caught.value.retryable is False


def test_not_ready_duplicate_conflict_readback_cancel_and_restart(setup):
    root, token = setup
    with serving(root, token) as server:
        for operation in ('submit', 'submit', 'readback', 'cancel'):
            result = exchange(server, token, request(token, operation=operation), positive=True)
            assert result['status'] == 'not_ready'
            assert result['ready'] is result['retryable'] is False
            assert result['cleanup'] == {'status': 'unverified'}
        result = exchange(server, token, request(token, invocation(attempt=2)), positive=True)
        assert result['status'] == 'conflict'
    with serving(root, token) as server:
        assert exchange(server, token, request(token), positive=True)['status'] == 'not_ready'
        assert exchange(server, token, request(token, invocation(attempt=3)), positive=True)['status'] == 'conflict'
        assert exchange(server, token, request(token, invocation(invocation_id='f' * 32), 'readback'), positive=True)['status'] == 'unknown'


def test_nonce_replay_survives_restart(setup):
    root, token = setup
    req = request(token)
    with serving(root, token) as server:
        assert exchange(server, token, req, positive=True)['status'] == 'not_ready'
        assert exchange(server, token, req, positive=True)['status'] == 'replay'
    with serving(root, token) as server:
        assert exchange(server, token, req, positive=True)['status'] == 'replay'


@pytest.mark.parametrize('changes', [dict(purpose='dashboard'), dict(audience='dashboard'),
                                    dict(expires_at=1), dict(enabled=False), dict(uid=os.getuid()+1)])
def test_auth_policy_rejects(setup, changes):
    root, token = setup
    req = request(token)
    credential(token, **changes)
    with serving(root, token) as server:
        with pytest.raises(protocol.ProtocolError, match='rejected'):
            exchange(server, token, req)


def test_rotation_revokes_old_signature_without_resetting_invocation(setup):
    root, token = setup
    old = request(token)
    with serving(root, token) as server:
        exchange(server, token, request(token), positive=True)
        credential(token, key_id='replacement', secret='cd' * 32)
        with pytest.raises(protocol.ProtocolError):
            exchange(server, token, old)
        assert exchange(server, token, request(token, invocation(attempt=2)), positive=True)['status'] == 'conflict'
        token.unlink()
        with pytest.raises(protocol.ProtocolError):
            exchange(server, token, old)


@pytest.mark.parametrize('field,value', [('argv', ['echo', 'secret']), ('attempt', True),
    ('timeout_seconds', 0), ('output_bytes', 1048577), ('runtime_sha256', 'x'),
    ('invocation_id', '../bad'), ('run_id', 'x'*129), ('purpose', 'dashboard')])
def test_strict_invocation(setup, field, value):
    _, token = setup
    with pytest.raises(protocol.ProtocolError):
        request(token, invocation(**{field: value}))


@pytest.mark.parametrize('raw', [b'\x00\x00\x20\x01', b'\x00\x00\x00\x05{}',
    struct.pack('!I', 17)+b'{"a":1,"a":2}    ', struct.pack('!I', 3)+b'NaN',
    struct.pack('!I', 6)+b'[[[[[['])
def test_bad_wire_is_bounded_and_redacted(setup, raw, caplog):
    root, token = setup
    with serving(root, token) as server:
        output = exchange(server, token, raw=raw)
        assert b'rejected' in output
        assert b'ababab' not in output
        assert not caplog.text


@pytest.mark.parametrize('target', ['parent', 'socket', 'token'])
@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'group', 'world'])
def test_unsafe_paths(setup, target, kind):
    root, token = setup
    path = {'parent': root, 'socket': root / 'executor.sock', 'token': token}[target]
    if kind in ('group', 'world'):
        if target == 'socket':
            path.touch(mode=0o600)
        path.chmod(0o770 if kind == 'group' else 0o777)
    elif target == 'parent':
        root.rename(root.with_name('real'))
        if kind == 'symlink':
            root.symlink_to(root.with_name('real'), target_is_directory=True)
        else:
            os.mkfifo(root, 0o600)
    else:
        path.unlink(missing_ok=True)
        if kind == 'symlink':
            path.symlink_to(root / 'missing')
        else:
            os.mkfifo(path, 0o600)
    with pytest.raises(protocol.ProtocolError):
        with serving(root, token):
            pass


def test_wrong_server_peer_and_socket_permissions(setup):
    root, token = setup
    with serving(root, token) as server:
        with pytest.raises(protocol.ProtocolError):
            protocol.call(root, token, request(token), owner_uid=os.getuid()+1)
        (root / 'executor.sock').chmod(0o666)
        with pytest.raises(protocol.ProtocolError):
            protocol.call(root, token, request(token), owner_uid=os.getuid())


def test_outage_is_nonretryable(setup):
    root, token = setup
    with pytest.raises(protocol.ProtocolError) as caught:
        protocol.call(root, token, request(token), owner_uid=os.getuid())
    assert caught.value.retryable is False


def test_failed_persistence_cannot_acknowledge(setup, monkeypatch):
    root, token = setup
    with serving(root, token) as server:
        def failed(*args):
            raise OSError('secret internal path')
        monkeypatch.setattr(os, 'fsync', failed)
        with pytest.raises(protocol.ProtocolError, match='rejected'):
            exchange(server, token, request(token))


def test_capacity_does_not_evict_replay_records(setup):
    root, token = setup
    with serving(root, token, max_records=1) as server:
        first = request(token)
        exchange(server, token, first, positive=True)
        assert exchange(server, token, request(token, invocation(invocation_id='f'*32)), positive=True)['status'] == 'capacity'
        assert exchange(server, token, first, positive=True)['status'] == 'replay'


@pytest.mark.parametrize('change', [dict(version=True), dict(audience='dashboard'),
    dict(expires_at=1), dict(expires_at=2**62), dict(nonce='x'), dict(mac='0'*64),
    dict(operation='execute'), dict(env={'SECRET': 'never-echo'}), dict(invocation_sha256='0'*64)])
def test_server_checks_signed_envelope(setup, change):
    root, token = setup
    req = request(token)
    req.update(change)
    # Deliberately use the real test key: validation must hold for a credential holder.
    if 'mac' not in change:
        key = credential(token)
        req['mac'] = protocol._mac(key, b'request\0', {k:v for k,v in req.items() if k != 'mac'})
    with serving(root, token) as server:
        with pytest.raises(protocol.ProtocolError):
            exchange(server, token, req)
        assert server.state['invocations'] == {}


@pytest.mark.parametrize('change', [dict(ready=True), dict(retryable=True), dict(status='completed'),
    dict(cleanup={'status': 'verified'}), dict(cleanup=None), dict(invocation_sha256='0'*64),
    dict(stderr='private stderr'), dict(invocation_id='0'*32)])
def test_client_rejects_invalid_signed_results(setup, monkeypatch, change):
    root, token = setup
    with serving(root, token) as server:
        real_handle = server._handle
        def corrupted(req, peer):
            reply = real_handle(req, peer)
            reply['result'].update(change)
            key = credential(token)
            reply['mac'] = protocol._mac(key, b'result\0', {k:v for k,v in reply.items() if k != 'mac'})
            return reply
        monkeypatch.setattr(server, '_handle', corrupted)
        with pytest.raises(protocol.ProtocolError):
            exchange(server, token, request(token))


@pytest.mark.parametrize('wire', [b'', b'\x00\x00', struct.pack('!I', 12)+b'{}',
                                  struct.pack('!I', 8193), struct.pack('!I', 2)+b'{}'])
def test_disconnect_truncated_unsigned_response_is_nonretryable(setup, monkeypatch, wire):
    root, token = setup
    with serving(root, token) as server:
        real_send = protocol._send
        def damaged_send(sock, value):
            if 'result' not in value:
                return real_send(sock, value)
            sock.sendall(wire)
            sock.shutdown(socket.SHUT_WR)
        monkeypatch.setattr(protocol, '_send', damaged_send)
        with pytest.raises(protocol.ProtocolError) as caught:
            exchange(server, token, request(token))
        assert not caught.value.retryable
    with serving(root, token) as server:
        assert len(server.state['invocations']) == 1


def test_peer_check_uses_kernel_uid(setup, monkeypatch):
    root, token = setup
    # An allowed application UID is policy, not a UID supplied in a request.
    credential(token, uid=os.getuid()+1)
    with serving(root, token) as server:
        with pytest.raises(protocol.ProtocolError):
            exchange(server, token, request(token))
        assert server.state['invocations'] == {}


@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'permissions', 'corrupt', 'hardlink'])
def test_bad_ledger_fails_closed(setup, kind):
    root, token = setup
    path = root / 'ledger.json'
    if kind == 'symlink':
        path.symlink_to(token)
    elif kind == 'fifo':
        os.mkfifo(path, 0o600)
    elif kind == 'hardlink':
        os.link(token, path)
    else:
        path.write_text('{}')
        path.chmod(0o666 if kind == 'permissions' else 0o600)
    with pytest.raises(protocol.ProtocolError):
        with serving(root, token):
            pass


def test_exclusive_server_and_replacement_safe_cleanup(setup):
    root, token = setup
    with serving(root, token):
        with pytest.raises(protocol.ProtocolError):
            with serving(root, token):
                pass
        path = root / 'executor.sock'
        path.unlink()
        path.write_text('replacement must survive')
    assert path.read_text() == 'replacement must survive'


def test_stalled_request_has_deadline(setup):
    root, token = setup
    with serving(root, token) as server:
        thread = threading.Thread(target=server.serve_once)
        thread.start()
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(f'/proc/self/fd/{server.fd}/executor.sock')
            sock.sendall(b'\x00')
            thread.join(2)
            assert not thread.is_alive()
            assert b'rejected' in sock.recv(8192)


def test_request_detaches_caller_owned_identity(setup):
    _, token = setup
    value = invocation()
    req = request(token, value)
    value['attempt'] = 2
    assert req['invocation']['attempt'] == 1


def test_concurrent_conflicting_submits_only_bind_once(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    root, token = setup
    with serving(root, token) as server:
        persist = server._persist
        def slow_persist(state):
            time.sleep(0.05)
            persist(state)
        monkeypatch.setattr(server, '_persist', slow_persist)
        workers = [threading.Thread(target=server.serve_once) for _ in range(2)]
        for worker in workers:
            worker.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(protocol.call, root, token, request(token, invocation(attempt=n)),
                                   owner_uid=os.getuid()) for n in (1, 2)]
            statuses = sorted(f.result()['status'] for f in futures)
        for worker in workers:
            worker.join(3)
        assert statuses == ['conflict', 'not_ready']


@pytest.mark.parametrize('field,value', [('version', True), ('request_sha256', '0'*64), ('mac', '0'*64)])
def test_reply_auth_and_request_binding(setup, monkeypatch, field, value):
    root, token = setup
    with serving(root, token) as server:
        real_handle = server._handle
        def corrupted(req, peer):
            reply = real_handle(req, peer)
            reply[field] = value
            if field != 'mac':
                key = credential(token)
                reply['mac'] = protocol._mac(key, b'result\0', {k:v for k,v in reply.items() if k != 'mac'})
            return reply
        monkeypatch.setattr(server, '_handle', corrupted)
        with pytest.raises(protocol.ProtocolError):
            exchange(server, token, request(token))


def test_duplicate_token_fields_rejected(setup):
    root, token = setup
    token.write_text(token.read_text().replace('"enabled": true', '"enabled": false, "enabled": true'))
    with pytest.raises(protocol.ProtocolError):
        with serving(root, token):
            pass


def test_ancestor_symlink_rejected(setup):
    root, token = setup
    alias = root.parent / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    nested = root / 'nested'
    nested.mkdir(mode=0o700)
    with pytest.raises(protocol.ProtocolError):
        with serving(alias / 'nested', token):
            pass


def test_module_has_no_launch_or_app_integration():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(protocol.__file__).read_text())
    imports = {node.names[0].name for node in ast.walk(tree) if isinstance(node, ast.Import)}
    assert imports <= {'fcntl', 'hashlib', 'hmac', 'json', 'os', 're', 'secrets', 'socket',
                       'stat', 'struct', 'time', 'threading'}
    forbidden = {'Popen', 'run', 'system', 'popen', 'fork', 'forkpty', 'posix_spawn', 'execv', 'execve', 'execvp', 'execvpe'}
    assert not any(isinstance(node, ast.Attribute) and node.attr in forbidden for node in ast.walk(tree))
    root = Path(__file__).resolve().parents[1]
    for name in ('api_server.py', 'Caddyfile', 'docker-compose.yml', 'docker-compose.host-hermes.yml'):
        assert 'host_exec' not in (root / name).read_text()
        assert 'executor.sock' not in (root / name).read_text()


def test_cleanup_error_is_redacted(setup, monkeypatch):
    root, token = setup
    with serving(root, token) as server:
        with monkeypatch.context() as scoped:
            def denied(*args, **kwargs):
                raise PermissionError('secret path must not escape')
            scoped.setattr(os, 'stat', denied)
            with pytest.raises(protocol.ProtocolError, match='executor rejected'):
                server.close()
        assert server.fd is None
