"""Bound site-packages bytes without importing packages or processing .pth.

A streaming tree commitment includes membership, ownership, identity and content.
RECORD is checked as data, never trusted as the complete list of importable files.
Bytecode caches are inactive: the frozen launcher redirects Python's cache prefix.
"""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import zlib
import time

# Measured installed Hermes: 84,897 files / 1.69 GB, largest file 221 MB.
# Declared Playwright tree: 568,368,835 bytes; Chromium alone is 290,614,600 bytes.
# Separate from the much smaller private configuration/policy limits.
MAX_FILES = 150000
MAX_BYTES = 3 * 1024 ** 3
MAX_FILE = 320 * 1024 ** 2
MAX_RECORD = 16 * 1024 ** 2
# Measured full installed runtime verification: 53.26 seconds / 1.40 GB.
VERIFY_TIMEOUT = 180


def signature(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
            st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def check(st, directory=False):
    if (not (stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode))
            or st.st_uid not in {0, os.getuid()} or st.st_mode & 0o022
            or (not directory and st.st_size > MAX_FILE)):
        raise ValueError('unsafe Hermes runtime file')


def open_file(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('unsafe Hermes runtime path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in path.parts[1:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
            st = os.fstat(fd)
            if st.st_uid not in {0, os.getuid()} or (st.st_mode & 0o022 and not (st.st_uid == 0 and st.st_mode & stat.S_ISVTX)):
                raise ValueError('unsafe Hermes runtime parent')
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_DIRECTORY if directory else 0), dir_fd=fd)
    finally:
        os.close(fd)


_DIGEST_CACHE = {}


def file_digest(path, expected, parent_fd=None):
    # Cache only complete Linux stat identity (including ctime), never path alone.
    key = (path, expected)
    stable = max(expected[-2:]) < time.time_ns() - 1_000_000_000
    if stable and key in _DIGEST_CACHE:
        return _DIGEST_CACHE[key]
    fd = (open_file(path) if parent_fd is None else
          os.open(Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd))
    try:
        before = os.fstat(fd); check(before)
        if signature(before) != expected:
            raise ValueError('Hermes runtime changed')
        digest = hashlib.sha256(); size = 0
        while True:
            raw = os.read(fd, 1024 * 1024)
            if not raw: break
            size += len(raw)
            if size > MAX_FILE: raise ValueError('Hermes runtime size limit')
            digest.update(raw)
        if size != before.st_size or signature(os.fstat(fd)) != expected:
            raise ValueError('Hermes runtime changed')
        value = digest.hexdigest()
        if len(_DIGEST_CACHE) >= MAX_FILES:
            _DIGEST_CACHE.pop(next(iter(_DIGEST_CACHE)))
        if stable:
            _DIGEST_CACHE[key] = value
        return value
    finally:
        os.close(fd)


MAX_COMMITMENT_BYTES = 64 * 1024 ** 2


def encode_commitments(values):
    raw = json.dumps(values, separators=(',', ':'), sort_keys=True).encode()
    if len(raw) > MAX_COMMITMENT_BYTES:
        raise ValueError('Hermes runtime commitment limit')
    return zlib.compress(raw)


def decode_commitments(raw):
    decoder = zlib.decompressobj()
    content = decoder.decompress(raw, MAX_COMMITMENT_BYTES + 1)
    if (len(content) > MAX_COMMITMENT_BYTES or not decoder.eof
            or decoder.unused_data or decoder.unconsumed_tail):
        raise ValueError('invalid Hermes runtime commitments')
    values = json.loads(content)
    if not isinstance(values, dict) or len(values) > MAX_FILES:
        raise ValueError('invalid Hermes runtime commitments')
    for path, value in values.items():
        if (not Path(path).is_absolute() or '..' in Path(path).parts
                or not isinstance(value, list) or len(value) != 2
                or not isinstance(value[0], list) or len(value[0]) != 8
                or any(type(n) is not int for n in value[0])
                or not isinstance(value[1], str) or len(value[1]) != 64
                or any(c not in '0123456789abcdef' for c in value[1])):
            raise ValueError('invalid Hermes runtime commitments')
    return values


def inventory(sites, *, bound_files=None, capture=None, commitments=None, scoped=False):
    digest = hashlib.sha256(); count = 0; total = 0; records = []; members = set(); directories = 0
    def emit(value):
        digest.update(json.dumps(value, separators=(',', ':'), sort_keys=True).encode() + b'\n')
    def add(path, parent_fd=None):
        nonlocal count, total
        if str(path) in members: return
        st = path.lstat()
        linked = scoped and stat.S_ISLNK(st.st_mode)
        if linked:
            if not any(Path(site).name == 'browser-runtimes' and path.is_relative_to(Path(site)) for site in sites):
                raise ValueError('unsafe installed tool link')
            if st.st_uid not in {0, os.getuid()}: raise ValueError('unsafe runtime link')
            target = path.resolve(strict=True)
            internal = any(target.is_relative_to(Path(site)) for site in sites)
            if not internal:
                if path.parent.name != 'bin' or not path.name.startswith('python'):
                    raise ValueError('external runtime link')
                add(target)
        else:
            check(st)
        count += 1; total += st.st_size
        if count + directories > MAX_FILES or total > MAX_BYTES:
            raise ValueError('Hermes runtime inventory limit exceeded')
        ident = signature(st)
        if capture is not None:
            # Seal only after the source's timestamp tick has passed. Otherwise
            # a same-tick write could look unchanged to a much later child.
            remaining = (st.st_ctime_ns + 1_000_000_000 - time.time_ns()) / 1e9
            if remaining > 1.1:
                raise ValueError('unsupported Hermes runtime timestamp')
            if remaining > 0:
                time.sleep(remaining + 0.01)
        if linked:
            value = hashlib.sha256(os.readlink(path).encode()).hexdigest()
            if commitments is not None and commitments.get(str(path)) != [list(ident), value]:
                raise ValueError('Hermes runtime link changed')
        elif commitments is None:
            value = file_digest(str(path), ident, parent_fd)
        else:
            bound = commitments.get(str(path))
            if bound is None or tuple(bound[0]) != ident:
                raise ValueError('Hermes runtime identity changed')
            value = bound[1]
            # Linux may coalesce multiple writes within a clock tick (tmpfs
            # included). Never reuse a commitment while that tick is recent.
            if max(ident[-2:]) >= time.time_ns() - 1_000_000_000:
                if file_digest(str(path), ident, parent_fd) != value:
                    raise ValueError('Hermes runtime content changed')
        if capture is not None:
            capture[str(path)] = [list(ident), value]
        if signature(path.lstat()) != ident:
            raise ValueError('Hermes runtime changed')
        emit([str(path), list(ident), value]); members.add(str(path))
    def walk(path, depth=0):
        nonlocal directories
        directories += 1
        if directories + count > MAX_FILES: raise ValueError("Hermes runtime entry limit")
        if depth > 32: raise ValueError('Hermes runtime depth limit')
        before = path.lstat(); check(before, directory=True)
        fd = open_file(path, directory=True)
        try:
            if signature(os.fstat(fd)) != signature(before): raise ValueError('Hermes runtime changed')
            names = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    if len(names) >= MAX_FILES: raise ValueError('Hermes runtime entry limit')
                    if entry.name != '__pycache__': names.append(entry.name)
            # Do not bind ignored cache creation timestamps; do bind membership/mode.
            emit([str(path), before.st_dev, before.st_ino, before.st_mode, before.st_uid, before.st_gid, sorted(names)])
            for name in sorted(names):
                child = path / name
                if stat.S_ISDIR(child.lstat().st_mode): walk(child, depth + 1)
                else:
                    add(child, fd)
                    if not scoped and depth == 1 and name == 'RECORD' and path.name.endswith('.dist-info'):
                        # Nested vendored dist-info RECORDs are package data,
                        # not an installation manifest rooted at this site.
                        records.append(child)
            if signature(os.fstat(fd)) != signature(before) or signature(path.lstat()) != signature(before):
                raise ValueError('Hermes runtime changed')
        finally: os.close(fd)
    for site in sorted(set(sites)):
        walk(Path(site))
    for record in records:
        site = record.parent.parent
        # Wheel scripts may use ../../../bin; they must stay within this prefix.
        prefix = site.parent.parent.parent
        fd = open_file(record)
        try:
            st = os.fstat(fd)
            if st.st_size > MAX_RECORD: raise ValueError('Hermes RECORD limit')
            raw = bytearray()
            while len(raw) <= MAX_RECORD:
                part = os.read(fd, min(65536, MAX_RECORD + 1 - len(raw)))
                if not part: break
                raw.extend(part)
            if len(raw) > MAX_RECORD or signature(os.fstat(fd)) != signature(st): raise ValueError('Hermes RECORD changed')
        finally: os.close(fd)
        seen = set()
        for row in csv.reader(io.StringIO(raw.decode('utf-8')), strict=True):
            if len(row) != 3 or not row[0] or '\\' in row[0] or Path(row[0]).is_absolute():
                raise ValueError('unsupported Hermes RECORD path')
            target = Path(os.path.abspath(site / row[0]))
            if target in seen or not target.is_relative_to(prefix):
                raise ValueError('ambiguous or escaping Hermes RECORD path')
            seen.add(target)
            if '__pycache__' in target.parts: continue
            add(target)
    if commitments is not None and members != set(commitments):
        raise ValueError('Hermes runtime membership changed')
    if bound_files is not None:
        bound_files.update(members)
    return {'sha256': digest.hexdigest(), 'files': count, 'bytes': total}


def install_origin_guard(files, stdlib_paths):
    """No loader may execute an unbound file from editable/runtime roots.

    The interpreter's standard library is the -I -S bootstrap trust base. Private
    policy and verified third-party files are enumerated explicitly, not by an
    ambient finder. Audit also covers direct source-loader execution.
    """
    import importlib.machinery
    import sys
    paths = tuple(os.path.abspath(p) + os.sep for p in stdlib_paths)
    def allowed(origin):
        name = os.path.abspath(origin)
        return name in files or ('site-packages' not in Path(name).parts and name.startswith(paths))
    original = importlib.machinery.PathFinder
    class BoundFinder:
        @classmethod
        def find_spec(cls, fullname, path=None, target=None):
            spec = original.find_spec(fullname, path, target)
            if spec is not None and spec.origin not in (None, 'built-in', 'frozen') and not allowed(spec.origin):
                raise ImportError('unbound Hermes runtime origin')
            return spec
        @classmethod
        def find_distributions(cls, *args, **kwargs):
            return original.find_distributions(*args, **kwargs)
        @classmethod
        def invalidate_caches(cls):
            original.invalidate_caches()
    sys.meta_path[:] = [BoundFinder if finder is original else finder for finder in sys.meta_path]
    def audit(event, arguments):
        origin = None
        if event == 'exec':
            origin = arguments[0].co_filename
            if origin.startswith('<'):
                return
        elif event == 'import' and len(arguments) > 1:
            origin = arguments[1]
        if origin and not allowed(origin):
            raise ImportError('unbound Hermes runtime origin')
    sys.addaudithook(audit)
