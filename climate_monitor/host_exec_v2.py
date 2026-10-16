"""Dormant v2 refusal intake. No transport, runner, or application integration.

Only unprivileged contract testing is intended. Never launch this checkout as
root. See docs/host-exec-v2-intake.md. V1 remains unchanged.
"""
from __future__ import annotations

import copy
import base64
import fcntl
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import threading
import time

from climate_monitor import host_exec as v1

ProtocolError = v1.ProtocolError
_require = v1._require
_canonical = v1._canonical
_mac = v1._mac
VERSION = 2
AUDIENCE = v1.AUDIENCE
PURPOSE = v1.PURPOSE
MAX_INPUT = 256 * 1024
MAX_RECORDS = 64
MAX_NONCES = 4096
STATUSES = {'withdrawn', 'not_ready', 'unknown', 'conflict', 'replay', 'capacity'}
OPERATIONS = {'submit', 'readback', 'cancel'}
REQUEST_DOMAIN = b'host-exec-v2/request\0'
RESULT_DOMAIN = b'host-exec-v2/result\0'
INITIALIZED = b'v2-intake-initialized\n'


def encode_message(value):
    """Canonical JSON payload only, no framing or I/O."""
    try:
        raw = _canonical(value)
        _require(type(value) is dict and 0 < len(raw) <= v1.MAX_FRAME)
        return raw
    except Exception:
        raise ProtocolError() from None


def decode_message(raw):
    try:
        _require(type(raw) is bytes and 0 < len(raw) <= v1.MAX_FRAME)
        value = v1._decode(raw)
        _require(type(value) is dict)
        return value
    except Exception:
        raise ProtocolError() from None


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _invocation(value):
    _require(type(value) is dict and set(value) == v1.INVOCATION_FIELDS | {'input_sha256'})
    _require(v1._hex(value['input_sha256'], 64))
    v1._invocation({k: v for k, v in value.items() if k != 'input_sha256'})
    return _digest(value)


def _credential(path, owner_uid):
    path = Path(path)
    fd = v1._directory(path.parent, owner_uid)
    try:
        key = v1._decode(v1._file(fd, path.name, owner_uid, v1.MAX_FRAME), canonical=False)
        _require(type(key) is dict and set(key) == {
            'version', 'key_id', 'secret', 'audience', 'purpose', 'uid', 'expires_at', 'enabled'})
        _require(type(key['version']) is int and key['version'] == VERSION)
        # Reuse the exact v1 lexical key-ID rule without accepting v1 credentials.
        _require(isinstance(key['key_id'], str) and
                 re.fullmatch('[A-Za-z0-9_-]{1,64}', key['key_id']) is not None)
        _require(v1._hex(key['secret'], 64) and type(key['enabled']) is bool and
                 v1._integer(key['uid'], 1, 2**32 - 2) and
                 v1._integer(key['expires_at'], 1, 2**63 - 1) and
                 key['audience'] == AUDIENCE and key['purpose'] == PURPOSE)
        return key
    finally:
        os.close(fd)


def make_request(token_path, invocation, operation='submit', *, owner_uid):
    try:
        digest = _invocation(invocation)
        _require(operation in OPERATIONS)
        key = _credential(token_path, owner_uid)
        now = int(time.time())
        expiry = min(now + 60, key['expires_at'])
        _require(key['enabled'] and expiry > now)
        body = dict(version=VERSION, audience=AUDIENCE, key_id=key['key_id'],
                    operation=operation, invocation=copy.deepcopy(invocation),
                    invocation_sha256=digest, nonce=secrets.token_hex(16), expires_at=expiry)
        return decode_message(encode_message(dict(body, mac=_mac(key, REQUEST_DOMAIN, body))))
    except Exception:
        raise ProtocolError() from None


def verify_reply(token_path, request, reply, *, owner_uid):
    try:
        request = decode_message(encode_message(request))
        reply = decode_message(encode_message(reply))
        key = _credential(token_path, owner_uid)
        _require(key['enabled'] and key['expires_at'] > int(time.time()))
        _require(type(reply) is dict and set(reply) == {'version', 'request_sha256', 'result', 'mac'})
        body = {k: v for k, v in reply.items() if k != 'mac'}
        _require(v1._hex(reply['mac'], 64) and
                 hmac.compare_digest(reply['mac'], _mac(key, RESULT_DOMAIN, body)))
        _require(type(reply['version']) is int and reply['version'] == VERSION and
                 reply['request_sha256'] == _digest(request))
        result = reply['result']
        base = {'invocation_id', 'invocation_sha256', 'status', 'ready', 'retryable', 'cleanup'}
        _require(type(result) is dict and set(result) == base)
        _require(result['invocation_id'] == request['invocation']['invocation_id'] and
                 result['invocation_sha256'] == _invocation(request['invocation']) and
                 result['status'] in STATUSES and result['retryable'] is False)
        _require(result['ready'] is False and result['cleanup'] == {'status': 'unverified'})
        return result
    except Exception:
        raise ProtocolError() from None


class IntakeStore:
    """Bounded, locked refusal commitments; never an execution queue.

    expected_checkpoint MUST come from independent trusted retention, not from
    this ledger. A stale checkpoint stops startup, including after an uncertain
    write. There is deliberately no automatic initialization or anchor reset.
    """

    def __init__(self, directory, *, owner_uid, expected_checkpoint):
        _require(v1._hex(expected_checkpoint, 64))
        self.directory = Path(directory)
        self.owner_uid = owner_uid
        self.expected_checkpoint = expected_checkpoint
        self.fd = None
        self.failed = False
        self.lock = threading.Lock()
        self.max_nonces = MAX_NONCES

    @classmethod
    def initialize(cls, directory, *, owner_uid):
        """Offline host-operator action, only in a new empty private directory."""
        fd = None
        try:
            fd = v1._directory(directory, owner_uid)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _require(not os.listdir(fd))
            # Retain this marker even if initialization crashes. It prevents a
            # deleted ledger from looking like a fresh directory, but is NOT an
            # external rollback anchor or evidence against whole-volume loss.
            marker = os.open('initialized-v2', os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(marker, 'wb') as stream:
                stream.write(INITIALIZED)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(fd)
            store = cls(directory, owner_uid=owner_uid, expected_checkpoint='0' * 64)
            store.fd = fd
            state = dict(version=VERSION, revision=0, inputs={}, invocations={}, nonces=[])
            store._persist(state)
            return store.checkpoint
        except Exception:
            raise ProtocolError() from None
        finally:
            if fd is not None:
                os.close(fd)

    def open(self):
        try:
            _require(self.fd is None and not self.failed)
            self.fd = v1._directory(self.directory, self.owner_uid)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _require(v1._file(self.fd, 'initialized-v2', self.owner_uid, 64) == INITIALIZED)
            raw = v1._file(self.fd, 'ledger-v2.json', self.owner_uid, v1.MAX_STATE)
            _require(hashlib.sha256(raw).hexdigest() == self.expected_checkpoint)
            state = v1._decode(raw)
            self._validate(state)
            self.state = state
            self.checkpoint = self.expected_checkpoint
        except Exception:
            self.close()
            raise ProtocolError() from None

    @staticmethod
    def _validate(state):
        base = {'version', 'revision', 'inputs', 'invocations', 'nonces'}
        _require(type(state) is dict and set(state) == base)
        _require(type(state['version']) is int and state['version'] == VERSION and
                 v1._integer(state['revision'], 0, 2**63 - 1))
        inputs, records, nonces = state['inputs'], state['invocations'], state['nonces']
        _require(type(inputs) is dict and len(inputs) <= MAX_RECORDS and
                 type(records) is dict and len(records) <= MAX_RECORDS and
                 type(nonces) is list and len(nonces) <= MAX_NONCES)
        _require(all(v1._hex(n, 64) for n in nonces) and len(set(nonces)) == len(nonces))
        for digest, encoded in inputs.items():
            _require(v1._hex(digest, 64) and type(encoded) is str and
                     len(encoded) <= 4 * ((MAX_INPUT + 2) // 3))
            raw = base64.b64decode(encoded, validate=True)
            _require(0 < len(raw) <= MAX_INPUT and hashlib.sha256(raw).hexdigest() == digest and
                     base64.b64encode(raw).decode('ascii') == encoded)
        for identity, record in records.items():
            _require(v1._hex(identity, 32) and type(record) is dict and set(record) == {
                'invocation', 'digest', 'phase'})
            _require(_invocation(record['invocation']) == record['digest'] and
                     record['invocation']['invocation_id'] == identity and
                     record['invocation']['input_sha256'] in inputs and
                     record['phase'] in {'refused', 'withdrawn'})

    def _persist(self, state):
        raw = _canonical(state)
        _require(len(raw) <= v1.MAX_STATE)
        name = '.intake-' + secrets.token_hex(16)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.fd)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, 'ledger-v2.json', src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        except Exception:
            self.failed = True
            raise
        finally:
            try:
                os.unlink(name, dir_fd=self.fd)
            except FileNotFoundError:
                pass
            except OSError:
                self.failed = True
                raise
        self.state = state
        self.checkpoint = hashlib.sha256(raw).hexdigest()

    def _update(self, state):
        state['revision'] += 1
        self._validate(state)
        self._persist(state)

    def seal_input(self, raw):
        """Host-local intake only; no input bytes or paths accepted on the wire."""
        try:
            with self.lock:
                _require(self.fd is not None and not self.failed and type(raw) is bytes and
                         0 < len(raw) <= MAX_INPUT)
                digest = hashlib.sha256(raw).hexdigest()
                if digest not in self.state['inputs']:
                    _require(len(self.state['inputs']) < MAX_RECORDS)
                    state = copy.deepcopy(self.state)
                    state['inputs'][digest] = base64.b64encode(raw).decode('ascii')
                    self._update(state)
                return digest
        except Exception:
            raise ProtocolError() from None

    def input_bytes(self, digest):
        try:
            with self.lock:
                _require(self.fd is not None and not self.failed and v1._hex(digest, 64))
                return base64.b64decode(self.state['inputs'][digest], validate=True)
        except Exception:
            raise ProtocolError() from None

    def apply(self, invocation, operation, nonce):
        try:
            with self.lock:
                _require(self.fd is not None and not self.failed)
                digest = _invocation(invocation)
                _require(operation in OPERATIONS and v1._hex(nonce, 64))
                identity = invocation['invocation_id']
                state = copy.deepcopy(self.state)
                records = state['invocations']
                if nonce in state['nonces']:
                    status = 'replay'
                elif len(state['nonces']) >= self.max_nonces:
                    status = 'capacity'
                else:
                    state['nonces'].append(nonce)
                    record = records.get(identity)
                    if record and record['digest'] != digest:
                        status = 'conflict'
                    elif record:
                        if operation == 'cancel':
                            record['phase'] = 'withdrawn'
                        status = 'withdrawn' if record['phase'] == 'withdrawn' else 'not_ready'
                    elif operation != 'submit':
                        status = 'unknown'
                    elif len(records) >= MAX_RECORDS:
                        status = 'capacity'
                    elif invocation['input_sha256'] not in state['inputs']:
                        status = 'not_ready'
                    else:
                        records[identity] = dict(invocation=copy.deepcopy(invocation), digest=digest,
                            phase='refused')
                        status = 'not_ready'
                    self._update(state)  # durable refusal and nonce before replying
                return dict(invocation_id=identity, invocation_sha256=digest, status=status,
                            ready=False, retryable=False, cleanup={'status': 'unverified'})
        except Exception:
            raise ProtocolError() from None

    def close(self):
        # Serialize descriptor retirement with apply/seal_input/input_bytes.
        with self.lock:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None


class IntakeBroker:
    """Pure authenticated intake; peer UID is supplied by the test caller."""

    def __init__(self, store, token_path, *, credential_owner_uid):
        self.store = store
        self.token_path = Path(token_path)
        self.credential_owner_uid = credential_owner_uid

    def handle(self, request, *, peer_uid):
        try:
            request = decode_message(encode_message(request))
            key = _credential(self.token_path, self.credential_owner_uid)
            _require(type(request) is dict and set(request) == {
                'version', 'audience', 'key_id', 'operation', 'invocation',
                'invocation_sha256', 'nonce', 'expires_at', 'mac'})
            now = int(time.time())
            _require(type(request['version']) is int and request['version'] == VERSION and
                     request['audience'] == AUDIENCE and request['key_id'] == key['key_id'] and
                     key['enabled'] and v1._integer(peer_uid, 1, 2**32 - 2) and key['uid'] == peer_uid and
                     key['expires_at'] > now and
                     v1._integer(request['expires_at'], now + 1, min(now + 60, key['expires_at'])))
            digest = _invocation(request['invocation'])
            _require(request['invocation_sha256'] == digest and request['operation'] in OPERATIONS and
                     v1._hex(request['nonce'], 32) and v1._hex(request['mac'], 64))
            body = {k: v for k, v in request.items() if k != 'mac'}
            _require(hmac.compare_digest(request['mac'], _mac(key, REQUEST_DOMAIN, body)))
            nonce = _digest([key['key_id'], request['nonce']])
            result = self.store.apply(request['invocation'], request['operation'], nonce)
            reply = dict(version=VERSION, request_sha256=_digest(request), result=result)
            return dict(reply, mac=_mac(key, RESULT_DOMAIN, reply))
        except Exception:
            raise ProtocolError() from None
