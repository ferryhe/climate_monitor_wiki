"""Private OAuth generations shared by controlled subprocesses of one run.

This detects accidental/uncontrolled drift, not a malicious same-UID writer.
A crash between auth replacement and its seal deliberately requires a fresh run.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

AUTH_LOCK_TIMEOUT = 3600.0

from climate_monitor.hermes_identity import (
    MAX_PRIVATE_BYTES, MAX_TREE_BYTES, _bytes, _check_document, _digest,
    _directory_names, _mkdir_private, _sync_dir, _write, secure_read,
)


def _auth(raw):
    value = json.loads(raw)
    _check_document(value)
    if (not isinstance(value, dict) or
            ('providers' in value and (not isinstance(value['providers'], dict) or
                                      any(not isinstance(v, dict) for v in value['providers'].values()))) or
            ('active_provider' in value and value['active_provider'] is not None and
             not isinstance(value['active_provider'], str))):
        raise ValueError()
    return raw


def _replace(path, raw):
    if len(raw) > MAX_PRIVATE_BYTES:
        raise ValueError()
    fd, temporary = tempfile.mkstemp(prefix='.auth-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_dir(path.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
            _sync_dir(path.parent)


@contextmanager
def _lock(root):
    path = root / 'auth.lock'
    try:
        _write(path, b'')
        _sync_dir(root)
    except FileExistsError:
        pass
    secure_read(path, private=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        deadline = time.monotonic() + AUTH_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError('controlled auth transaction busy') from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def _chain(root, reference):
    # Every published-history reader holds _lock; a remaining marker is abandoned.
    if os.path.lexists(root / 'auth-inflight.json'):
        raise ValueError('unfinished controlled auth transaction')
    manifest = secure_read(root / 'manifest.json', private=True)[0]
    complete = secure_read(root / 'complete', private=True)[0].decode()
    if reference.get('sha256') != complete or _digest(manifest) != complete:
        raise ValueError()
    initial = _bytes(json.loads(manifest)['auth'])
    directory = root / 'auth-generations'
    names = sorted(_directory_names(directory))
    if not names or len(names) > 1024:
        raise ValueError()
    records = []
    previous = None
    total = 0
    for index, name in enumerate(names):
        if name != f'{index:06d}.json':
            raise ValueError()
        raw, _ = secure_read(directory / name, private=True)
        total += len(raw)
        if total > MAX_TREE_BYTES:
            raise ValueError()
        value = json.loads(raw)
        if (set(value) != {'snapshot', 'generation', 'previous', 'sha256', 'auth'} or
                value['snapshot'] != reference or value['generation'] != index or
                value['previous'] != previous or not isinstance(value['auth'], str)):
            raise ValueError()
        auth = _auth(value['auth'].encode())
        if (index == 0 and auth != initial) or _digest(auth) != value['sha256']:
            raise ValueError()
        previous = _digest(raw)
        records.append((value, previous))
    head = json.loads(secure_read(root / 'auth-head.json', private=True)[0])
    if head != {'snapshot': reference, 'generation': len(records) - 1, 'record': previous}:
        raise ValueError()
    return records


def _append(root, reference, records, raw):
    _auth(raw)
    value = {'snapshot': reference, 'generation': len(records),
             'previous': records[-1][1] if records else None,
             'sha256': _digest(raw), 'auth': raw.decode('utf-8')}
    encoded = _bytes(value)
    if len(encoded) > MAX_PRIVATE_BYTES or len(records) >= 1024:
        raise ValueError()
    directory = root / 'auth-generations'
    _write(directory / f'{len(records):06d}.json', encoded)
    _sync_dir(directory)
    _replace(root / 'auth-head.json', _bytes({'snapshot': reference,
             'generation': len(records), 'record': _digest(encoded)}))
    _sync_dir(root)
    return value, _digest(encoded)


def _record(home):
    raw, _ = secure_read(home / 'auth-seal.json', private=True)
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {'snapshot', 'generation', 'record', 'sha256'} or
            not isinstance(value['snapshot'], dict) or
            set(value['snapshot']) != {'schema_version', 'sha256'}):
        raise ValueError()
    return value


def _verify(home, records, reference):
    seal = _record(home)
    index = seal['generation']
    if type(index) is not int or not 0 <= index < len(records):
        raise ValueError()
    record, digest = records[index]
    if seal != {'snapshot': reference, 'generation': index, 'record': digest, 'sha256': record['sha256']}:
        raise ValueError()
    raw = _auth(secure_read(home / 'auth.json', private=True)[0])
    if _digest(raw) != seal['sha256']:
        raise ValueError()
    return raw


def _materialize(home, record):
    value, digest = record
    _replace(home / 'auth.json', value['auth'].encode())
    _replace(home / 'auth-seal.json', _bytes({'snapshot': value['snapshot'],
             'generation': value['generation'], 'record': digest, 'sha256': value['sha256']}))


def prepare_auth(home, reference):
    try:
        with _lock(home.parent):
            records = _chain(home.parent, reference)
            if os.path.lexists(home / 'auth.json') or os.path.lexists(home / 'auth-seal.json'):
                _verify(home, records, reference)
            _materialize(home, records[-1])
    except (ValueError, OSError, KeyError, TypeError, UnicodeError):
        raise ValueError('Hermes auth state inconsistent or busy; start a fresh run') from None


def verify_auth(home):
    """Read-only probes may not rotate auth or create a generation."""
    try:
        with _lock(Path(home).parent):
            seal = _record(Path(home))
            _verify(Path(home), _chain(Path(home).parent, seal['snapshot']), seal['snapshot'])
    except (ValueError, OSError, KeyError, TypeError, UnicodeError):
        raise ValueError('Hermes auth state inconsistent; start a fresh run') from None


@contextmanager
def execution(home, *, enabled=True, require_identity=False):
    result = {'returncode': None}
    if not enabled:
        yield result
        return
    home = Path(home)
    try:
        with _lock(home.parent):
            seal = _record(home)
            reference = seal['snapshot']
            records = _chain(home.parent, reference)
            _verify(home, records, reference)
            _materialize(home, records[-1])
            before = records[-1][0]['sha256']
            inflight = home.parent / 'auth-inflight.json'
            _write(inflight, _bytes({'snapshot': reference, 'generation': len(records) - 1,
                                    'home': home.name, 'sha256': before}))
            _sync_dir(home.parent)
            try:
                yield result
            finally:
                import sys
                from climate_monitor.managed_runtime import ManagedFailure
                failure = sys.exc_info()[1]
                if isinstance(failure, ManagedFailure) and not failure.evidence['recoverable']:
                    raise failure  # Keep inflight ownership while descendants may retain credentials.
                raw = _auth(secure_read(home / 'auth.json', private=True)[0])
                if _digest(raw) != before:
                    if result['returncode'] != 0:
                        raise ValueError('unsealed auth mutation')
                    latest = _append(home.parent, reference, records, raw)
                    _materialize(home, latest)
                # Leave the durable marker on invalid/unsealed changes or a
                # worker crash: no sibling may reuse a possibly stale token.
                inflight.unlink()
                _sync_dir(home.parent)
            if result['returncode'] == 0 and require_identity:
                from climate_monitor.hermes_identity import require_effective_identity
                manifest = json.loads(secure_read(home.parent / 'manifest.json', private=True)[0])
                require_effective_identity(home.parent, manifest['source'])
    except (ValueError, OSError, KeyError, TypeError, UnicodeError):
        raise ValueError('Hermes auth state inconsistent or unsealed; start a fresh run') from None


def initialize_auth(root, reference, initial):
    _mkdir_private(root / 'auth-generations')
    _append(root, reference, [], _bytes(initial))


def verify_history(root, reference):
    try:
        with _lock(root):
            _chain(root, reference)
    except (ValueError, OSError, KeyError, TypeError, UnicodeError):
        raise ValueError('Hermes auth history missing or inconsistent; start a fresh run') from None
