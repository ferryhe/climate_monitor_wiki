"""Standalone stdlib plugin template, copied into each execution snapshot.

ROOT and SOURCE are bound by the snapshot publisher before this source. This
file is never imported by a managed child from the application checkout.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading


def _identity_directory_registry():
    # Closure-local state also keeps independently imported frozen copies safe.
    guard = threading.Lock()
    descriptors = {}

    def after_fork_child():
        # before-fork acquired guard in the forking thread. Never acquire an
        # inherited Python mutex here, and never unlock the parent's flock.
        try:
            for fd in descriptors:
                try:
                    os.close(fd)
                except OSError:
                    # Linux releases the fd even on a delayed close error;
                    # retrying could close a newly reused descriptor.
                    pass
        finally:
            descriptors.clear()
            guard.release()

    os.register_at_fork(before=guard.acquire, after_in_parent=guard.release,
                        after_in_child=after_fork_child)

    @contextmanager
    def opened(root):
        # Fork cannot occur between open/tracking or untracking/close.
        with guard:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            token = object()
            descriptors[fd] = token
        try:
            yield fd
        finally:
            with guard:
                # A fork child may have closed this fd and reused its number.
                if descriptors.get(fd) is token:
                    del descriptors[fd]
                    os.close(fd)

    return opened


_identity_directory = _identity_directory_registry()


@contextmanager
def _identity_lock(root, *, exclusive=False):
    """Separate directory opens coordinate threads/processes without a lock file."""
    with _identity_directory(root) as fd:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError('unsafe identity directory')
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


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
    with _identity_lock(ROOT):
        return _read_locked()


def _read_locked():
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
    with _identity_lock(ROOT, exclusive=True):
        return _publish_locked(value)


def _publish_locked(value):
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
            if _read_locked() != value:
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
        os._exit(76)


def register(ctx):
    def pre(**kwargs):
        _observe(False, **kwargs)
    def post(**kwargs):
        _observe(True, **kwargs)
    ctx.register_hook('pre_api_request', pre)
    ctx.register_hook('post_api_request', post)
