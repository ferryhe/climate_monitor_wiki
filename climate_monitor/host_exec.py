"""Opt-in Linux Hermes invocation protocol. No runner or application integration.

All v1 outcomes are non-retryable and cleanup is unverified. See
``docs/host-exec-protocol.md`` before using this boundary.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import threading
import time

VERSION = 1
AUDIENCE = 'climate-host-hermes-executor'
PURPOSE = 'hermes-cli-invocation'
MAX_FRAME = 8192
MAX_STATE = 4 * 1024 * 1024
MAX_RECORDS = 4096
IO_TIMEOUT = 1.0
STATUSES = {'not_ready', 'conflict', 'unknown', 'replay', 'capacity'}
INVOCATION_FIELDS = {'invocation_id', 'run_id', 'attempt', 'purpose', 'binding_sha256',
                     'frozen_identity_sha256', 'repository_commit_sha', 'runtime_sha256',
                     'timeout_seconds', 'output_bytes'}


class ProtocolError(ValueError):
    """Only this constant, redacted failure crosses the public boundary."""
    retryable = False

    def __init__(self):
        super().__init__('executor rejected; non-retryable')


def _require(condition):
    if not condition:
        raise ProtocolError()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def _decode(raw, *, canonical=True):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result)
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs)
    _require(not canonical or _canonical(value) == raw)
    return value


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % length, value) is not None


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _invocation(value):
    _require(type(value) is dict and set(value) == INVOCATION_FIELDS)
    _require(_hex(value['invocation_id'], 32) and value['purpose'] == PURPOSE)
    _require(isinstance(value['run_id'], str) and
             re.fullmatch('[A-Za-z0-9_-]{1,128}', value['run_id']) is not None)
    for field in ('binding_sha256', 'frozen_identity_sha256', 'runtime_sha256'):
        _require(_hex(value[field], 64))
    _require(_hex(value['repository_commit_sha'], 40))
    for field, high in [('attempt', 1000000), ('timeout_seconds', 3600), ('output_bytes', 1048576)]:
        _require(_integer(value[field], 1, high))
    return hashlib.sha256(_canonical(value)).hexdigest()


def _directory(path, owner_uid):
    # Same descriptor-walk pattern as hermes_identity._open_parent, but no
    # snapshot/YAML imports in this stdlib-only boundary. Never resolve symlinks.
    path = Path(path)
    _require(path.is_absolute() and '..' not in path.parts and path != Path('/'))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, name in enumerate(path.parts[1:]):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            st = os.fstat(fd)
            if index == len(path.parts) - 2:
                _require(st.st_uid == owner_uid and stat.S_IMODE(st.st_mode) == 0o700)
            else:
                _require(st.st_uid in {0, owner_uid} and
                         (not st.st_mode & 0o022 or
                          (st.st_uid == 0 and st.st_mode & stat.S_ISVTX)))
        return fd
    except BaseException:
        os.close(fd)
        raise


def _signature(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
            st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _file(fd, name, owner_uid, limit):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        before = os.fstat(handle)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == owner_uid and
                 stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1 and before.st_size <= limit)
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(handle, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.stat(name, dir_fd=fd, follow_symlinks=False)
        _require(len(raw) <= limit and _signature(before) == _signature(os.fstat(handle)) == _signature(after))
        return bytes(raw)
    finally:
        os.close(handle)


def _credential(path, owner_uid):
    path = Path(path)
    fd = _directory(path.parent, owner_uid)
    try:
        # Operator-authored JSON need not be canonical; wire and ledger must be.
        raw = _file(fd, path.name, owner_uid, MAX_FRAME)
        value = _decode(raw, canonical=False)
        _require(type(value) is dict and set(value) ==
                 {'version', 'key_id', 'secret', 'audience', 'purpose', 'uid', 'expires_at', 'enabled'})
        _require(type(value['version']) is int and value['version'] == VERSION and
                 isinstance(value['key_id'], str) and
                 re.fullmatch('[A-Za-z0-9_-]{1,64}', value['key_id']) is not None and
                 _hex(value['secret'], 64) and type(value['enabled']) is bool and
                 _integer(value['uid'], 0, 2**32-1) and
                 _integer(value['expires_at'], 1, 2**63-1) and
                 isinstance(value['purpose'], str) and isinstance(value['audience'], str))
        return value
    finally:
        os.close(fd)


def _mac(key, domain, value):
    return hmac.new(bytes.fromhex(key['secret']), domain + _canonical(value), hashlib.sha256).hexdigest()


def make_request(token_path, invocation, operation='submit', *, owner_uid):
    """Caller supplies frozen commitments, never executable paths or commands."""
    try:
        digest = _invocation(invocation)
        _require(operation in {'submit', 'readback', 'cancel'})
        key = _credential(token_path, owner_uid)
        now = int(time.time())
        expires_at = min(now + 60, key['expires_at'])
        _require(expires_at > now)
        body = dict(version=VERSION, audience=AUDIENCE, key_id=key['key_id'],
                    operation=operation, invocation=dict(invocation), invocation_sha256=digest,
                    nonce=secrets.token_hex(16), expires_at=expires_at)
        return dict(body, mac=_mac(key, b'request\0', body))
    except Exception:
        raise ProtocolError() from None


def _receive(sock):
    deadline = time.monotonic() + IO_TIMEOUT
    def exact(size):
        raw = bytearray()
        while len(raw) < size:
            remaining = deadline - time.monotonic()
            _require(remaining > 0)
            sock.settimeout(remaining)
            chunk = sock.recv(size - len(raw))
            _require(bool(chunk))
            raw.extend(chunk)
        return bytes(raw)
    length = struct.unpack('!I', exact(4))[0]
    _require(0 < length <= MAX_FRAME)
    value = _decode(exact(length))
    sock.settimeout(max(0.001, deadline - time.monotonic()))
    _require(sock.recv(1) == b'')  # one frame, then half-close; no trailing bytes
    return value


def _send(sock, value):
    raw = _canonical(value)
    _require(len(raw) <= MAX_FRAME)
    sock.settimeout(IO_TIMEOUT)
    sock.sendall(struct.pack('!I', len(raw)) + raw)
    sock.shutdown(socket.SHUT_WR)


def _peer(sock):
    return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def _socket_stat(fd, owner_uid):
    st = os.stat('executor.sock', dir_fd=fd, follow_symlinks=False)
    _require(stat.S_ISSOCK(st.st_mode) and st.st_uid == owner_uid and
             stat.S_IMODE(st.st_mode) == 0o600)
    return st


def call(directory, token_path, request, *, owner_uid):
    """One exchange only: no retry, reconnect, alternate transport or launch."""
    fd = None
    try:
        key = _credential(token_path, owner_uid)
        fd = _directory(directory, owner_uid)
        before = _socket_stat(fd, owner_uid)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(IO_TIMEOUT)
            sock.connect(f'/proc/self/fd/{fd}/executor.sock')
            _require(_peer(sock) == owner_uid and before == _socket_stat(fd, owner_uid))
            _send(sock, request)
            reply = _receive(sock)
        _require(type(reply) is dict and set(reply) == {'version', 'request_sha256', 'result', 'mac'})
        signed = {k: v for k, v in reply.items() if k != 'mac'}
        _require(_hex(reply['mac'], 64) and hmac.compare_digest(reply['mac'], _mac(key, b'result\0', signed)))
        _require(type(reply['version']) is int and reply['version'] == VERSION and
                 reply['request_sha256'] == hashlib.sha256(_canonical(request)).hexdigest())
        result = reply['result']
        _require(type(result) is dict and set(result) ==
                 {'invocation_id', 'invocation_sha256', 'status', 'ready', 'retryable', 'cleanup'})
        _require(result['invocation_id'] == request['invocation']['invocation_id'] and
                 result['invocation_sha256'] == _invocation(request['invocation']) and
                 result['status'] in STATUSES and result['ready'] is False and
                 result['retryable'] is False and result['cleanup'] == {'status': 'unverified'})
        return result
    except Exception:
        raise ProtocolError() from None
    finally:
        if fd is not None:
            os.close(fd)


class Server:
    """Explicit local configuration only. Call open/serve_once/close; no CLI.

    A private directory lock serializes state across server instances. There is
    deliberately no execution callback. Persistent entries record refusals only.
    """

    def __init__(self, directory, token_path, *, owner_uid, max_records=MAX_RECORDS):
        self.directory = Path(directory)
        self.token_path = Path(token_path)
        self.owner_uid = owner_uid
        _require(_integer(max_records, 1, MAX_RECORDS))
        self.max_records = max_records
        self.fd = None
        self.sock = None
        self.socket_identity = None
        self.failed = False
        # ponytail: one lock and bounded whole-ledger writes; #158 owns the runner queue.
        self.lock = threading.Lock()

    def open(self):
        try:
            _require(self.fd is None)
            self.fd = _directory(self.directory, self.owner_uid)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _credential(self.token_path, self.owner_uid)
            try:
                raw = _file(self.fd, 'ledger.json', self.owner_uid, MAX_STATE)
            except FileNotFoundError:
                self.state = {'version': VERSION, 'invocations': {}, 'nonces': []}
                self._persist(self.state)
            else:
                self.state = _decode(raw)
                _require(type(self.state) is dict and set(self.state) == {'version', 'invocations', 'nonces'})
                _require(type(self.state['version']) is int and self.state['version'] == VERSION)
                records, nonces = self.state['invocations'], self.state['nonces']
                _require(type(records) is dict and len(records) <= MAX_RECORDS and
                         all(_hex(k, 32) and _hex(v, 64) for k, v in records.items()))
                _require(type(nonces) is list and len(nonces) <= MAX_RECORDS and
                         all(_hex(n, 64) for n in nonces) and len(set(nonces)) == len(nonces))
            # Existing sockets (including stale ones) are never removed at startup.
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(f'/proc/self/fd/{self.fd}/executor.sock')
            os.chmod('executor.sock', 0o600, dir_fd=self.fd, follow_symlinks=False)
            self.socket_identity = _socket_stat(self.fd, self.owner_uid)
            self.sock.listen(8)
            self.sock.settimeout(IO_TIMEOUT)
        except Exception:
            self.close()
            raise ProtocolError() from None

    def _persist(self, state):
        raw = _canonical(state)
        _require(len(raw) <= MAX_STATE)
        name = '.ledger-' + secrets.token_hex(16)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, 'ledger.json', src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(name, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def _handle(self, request, peer_uid):
        _require(not self.failed)
        key = _credential(self.token_path, self.owner_uid)  # re-read for rotation/revocation
        _require(type(request) is dict and set(request) ==
                 {'version', 'audience', 'key_id', 'operation', 'invocation', 'invocation_sha256', 'nonce', 'expires_at', 'mac'})
        _require(type(request['version']) is int and request['version'] == VERSION and
                 request['audience'] == key['audience'] == AUDIENCE and
                 request['key_id'] == key['key_id'] and key['enabled'] is True and
                 peer_uid == key['uid'] and key['purpose'] == PURPOSE)
        now = int(time.time())
        _require(key['expires_at'] > now and _integer(request['expires_at'], now + 1, now + 60) and
                 request['expires_at'] <= key['expires_at'] and _hex(request['nonce'], 32))
        digest = _invocation(request['invocation'])
        _require(request['invocation_sha256'] == digest and request['operation'] in {'submit', 'readback', 'cancel'})
        signed = {k: v for k, v in request.items() if k != 'mac'}
        _require(_hex(request['mac'], 64) and hmac.compare_digest(request['mac'], _mac(key, b'request\0', signed)))
        identity = request['invocation']['invocation_id']
        nonce = hashlib.sha256(_canonical([key['key_id'], request['nonce']])).hexdigest()
        records = self.state['invocations']
        if nonce in self.state['nonces']:
            status = 'replay'
        elif len(self.state['nonces']) >= self.max_records:
            status = 'capacity'
        else:
            known = records.get(identity)
            status = 'not_ready' if known == digest else 'conflict' if known else 'unknown'
            updated = dict(self.state, nonces=[*self.state['nonces'], nonce], invocations=dict(records))
            if known is None and request['operation'] == 'submit':
                if len(records) >= self.max_records:
                    status = 'capacity'
                else:
                    updated['invocations'][identity] = digest
                    status = 'not_ready'
            try:
                self._persist(updated)  # commit before any response, even not_ready
            except Exception:
                self.failed = True  # uncertain persistence poisons this process
                raise
            self.state = updated
        result = dict(invocation_id=identity, invocation_sha256=digest, status=status,
                      ready=False, retryable=False, cleanup={'status': 'unverified'})
        body = dict(version=VERSION, request_sha256=hashlib.sha256(_canonical(request)).hexdigest(), result=result)
        return dict(body, mac=_mac(key, b'result\0', body))

    def serve_once(self):
        """Serial bounded exchange; caller owns lifecycle. No background workers."""
        try:
            conn, _ = self.sock.accept()
        except (OSError, AttributeError):
            return
        with conn:
            try:
                request = _receive(conn)
                with self.lock:
                    reply = self._handle(request, _peer(conn))
            except Exception:
                reply = {'version': VERSION, 'error': 'rejected'}
            try:
                _send(conn, reply)
            except OSError:
                pass  # disconnected clients cannot authorize another action

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.fd is not None:
            try:
                if self.socket_identity is not None:
                    current = os.stat('executor.sock', dir_fd=self.fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (self.socket_identity.st_dev, self.socket_identity.st_ino):
                        os.unlink('executor.sock', dir_fd=self.fd)
            except FileNotFoundError:
                pass
            except OSError:
                raise ProtocolError() from None
            finally:
                os.close(self.fd)
                self.fd = None
                self.socket_identity = None
