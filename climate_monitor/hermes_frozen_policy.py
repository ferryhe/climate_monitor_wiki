"""Standalone stdlib plugin template, copied into each execution snapshot.

ROOT and SOURCE are bound by the snapshot publisher before this source. This
file is never imported by a managed child from the application checkout.
"""
import json
import os
from pathlib import Path
import re
import stat
import tempfile


def _identity(value):
    if (not isinstance(value, dict) or set(value) != {'provider', 'model', 'source'} or
            any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', v)
                or '://' in v for v in value.values())):
        raise ValueError('invalid identity')
    return value


def _sync():
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sig(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _read():
    path = ROOT / 'effective-identity.json'
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o077 or before.st_size > 4096:
        raise ValueError('unsafe identity')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if _sig(opened) != _sig(before):
            raise ValueError('identity changed')
        raw = os.read(fd, 4097)
        os.lseek(fd, 0, os.SEEK_SET)
        if raw != os.read(fd, 4097) or len(raw) != before.st_size or _sig(os.fstat(fd)) != _sig(opened):
            raise ValueError('identity changed')
    finally:
        os.close(fd)
    if _sig(path.lstat()) != _sig(before):
        raise ValueError('identity changed')
    return _identity(json.loads(raw))


def _publish(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    fd, temporary = tempfile.mkstemp(prefix='.identity-', dir=ROOT)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, ROOT / 'effective-identity.json', follow_symlinks=False)
        except FileExistsError:
            if _read() != value:
                raise ValueError('identity mismatch')
        _sync()
    finally:
        os.unlink(temporary)
        _sync()


def _observe(successful, provider=None, model=None, **ignored):
    try:
        st = ROOT.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError('unsafe identity directory')
        value = _identity({'provider': provider, 'model': model, 'source': SOURCE})
        if os.environ.get('HERMES_SESSION_SOURCE') != SOURCE:
            raise ValueError('source mismatch')
        if successful:
            _publish(value)
        else:
            prior = _read()
            if prior is not None and prior != value:
                raise ValueError('identity mismatch')
    except BaseException:
        # Hermes catches plugin exceptions; terminate without printing payloads.
        os._exit(65)


def register(ctx):
    def pre(**kwargs):
        _observe(False, **kwargs)
    def post(**kwargs):
        _observe(True, **kwargs)
    ctx.register_hook('pre_api_request', pre)
    ctx.register_hook('post_api_request', post)
