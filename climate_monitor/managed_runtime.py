"""Bounded managed failures and POSIX execution-group ownership (Issue #141)."""
from contextlib import ExitStack, contextmanager
import ctypes
import json
import locale
import platform
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import tempfile
import time
import uuid

SCHEMA = 'climate-managed-failure.v1'
_MESSAGES = {
    'transient_service': 'Transient service failure; resume after service recovery',
    'timeout_cancelled': 'Execution timed out or was cancelled',
    'identity_drift': 'Hermes effective identity changed; start a fresh run',
    'configuration_auth': 'Hermes configuration or authentication failed; start a fresh run',
    'frozen_input': 'Frozen input contract failed; start a fresh run',
    'internal': 'Internal execution failure; inspect the controlled runtime',
}


class ManagedFailure(RuntimeError):
    def __init__(self, category='internal', *, returncode=None, cleanup=None):
        category = category if isinstance(category, str) and category in _MESSAGES else 'internal'
        receipt = cleanup if isinstance(cleanup, dict) else {}
        pgid = receipt.get('pgid')
        safe_group = _valid_group(pgid)
        verified = cleanup is None or (
            safe_group and receipt.get('verified') is True
            and type(receipt.get('term_sent')) is bool
            and type(receipt.get('kill_sent')) is bool
        )
        self.evidence = {
            'schema_version': SCHEMA, 'category': category,
            'retryable': category == 'transient_service' and verified,
            'recoverable': verified,
            'message': _MESSAGES[category] if verified else 'Process cleanup unverified; recovery blocked',
            'returncode': returncode if type(returncode) is int and -255 <= returncode <= 255 else None,
            'cleanup': None if cleanup is None else {
                'verified': verified,
                'pgid': pgid if safe_group else None,
                'term_sent': receipt.get('term_sent') is True,
                'kill_sent': receipt.get('kill_sent') is True,
            },
        }
        super().__init__(self.evidence['message'])


def failure_for_exit(code, *, cleanup=None):
    # Only our frozen hooks own these exit codes. Unknown provider exits are terminal.
    return ManagedFailure({65: 'frozen_input', 76: 'identity_drift', 78: 'configuration_auth',
                           124: 'timeout_cancelled'}.get(code, 'internal'), returncode=code, cleanup=cleanup)


def failure_from_exception(exc, *, contract=False):
    if isinstance(exc, ManagedFailure):
        return exc
    if isinstance(exc, (KeyboardInterrupt, subprocess.TimeoutExpired)):
        return ManagedFailure('timeout_cancelled')
    # Inspect only our fixed exception messages; never copy diagnostic text.
    text = str(exc)[:256] if isinstance(exc, (ValueError, SystemExit)) else ''
    if isinstance(exc, ValueError) and text.startswith(('Hermes effective identity', 'invalid Hermes identity', 'Hermes session source')):
        return ManagedFailure('identity_drift')
    if isinstance(exc, (ValueError, SystemExit)) and text.startswith((
        'Hermes auth ', 'Hermes snapshot ', 'immutable ', 'unsupported implicit Hermes',
        'unsupported missing Hermes', 'mandatory Hermes', 'Hermes authoring capabilities',
        'Hermes authoring requires', 'Hermes executable unavailable',
    )):
        return ManagedFailure('configuration_auth')
    return ManagedFailure('frozen_input' if contract else 'internal')


def read_failure(value):
    """Rebuild an allowlisted child receipt; never trust serialized retry flags/text."""
    if not isinstance(value, dict) or value.get('schema_version') != SCHEMA:
        return ManagedFailure()
    cleanup = value.get('cleanup')
    # Serialized transient claims must carry actual group cleanup evidence.
    # Only a trusted in-process caller knows that no child was launched.
    if cleanup is None and value.get('category') == 'transient_service':
        cleanup = {'verified': False}
    return ManagedFailure(value.get('category'), returncode=value.get('returncode'), cleanup=cleanup)


def retry_allowed(result):
    return (result.get('retryable') is True
            and read_failure(result.get('failure')).evidence['retryable'])


def _valid_group(pgid):
    return type(pgid) is int and 1 < pgid <= 2 ** 31 - 1 and pgid != os.getpgrp()


def group_exists(pgid):
    if not _valid_group(pgid):
        raise ValueError('unsafe execution group')
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True  # PermissionError and other errors propagate, never prove absence.


def _subreaper():
    # Linux re-parents orphaned grandchildren to this worker, so we can reap
    # them even on containers whose PID 1 does not reap zombies. Never wait(-1).
    if sys.platform == 'linux':
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise ManagedFailure()


def _pidfd_syscall(number, *arguments):
    # Older Python/glibc builds omit wrappers even on pidfd-capable kernels.
    # Linux x86_64 and asm-generic (aarch64) syscall tables: send=424, open=434.
    if sys.platform != 'linux' or platform.machine() not in ('x86_64', 'aarch64'):
        raise OSError('unsupported managed process runtime')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(ctypes.c_long(number), *arguments)
    if result < 0:
        raise OSError(ctypes.get_errno(), 'managed process operation failed')
    return result


def _pidfd_open(pid):
    if hasattr(os, 'pidfd_open'):
        return os.pidfd_open(pid)
    return _pidfd_syscall(434, ctypes.c_int(pid), ctypes.c_uint(0))


def _pidfd_kill(fd):
    if hasattr(signal, 'pidfd_send_signal'):
        signal.pidfd_send_signal(fd, signal.SIGKILL)
    else:
        _pidfd_syscall(424, ctypes.c_int(fd), ctypes.c_int(signal.SIGKILL),
                       ctypes.c_void_p(), ctypes.c_uint(0))


def _process_info(pid):
    # /proc stat's comm can contain spaces and parentheses. Fields after its
    # final ')' are state, ppid, pgrp, session, ... starttime (field 22).
    with open(f'/proc/{pid}/stat', 'rb') as stream:
        fields = stream.read(4096).rsplit(b') ', 1)[1].split()
    return fields[0], int(fields[2]), int(fields[3]), int(fields[19])


def _leader_status(process):
    if process.returncode is not None or not _valid_group(process.pid):
        raise ValueError()
    status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    info = _process_info(process.pid)
    if (info[1:3] != (process.pid, process.pid)
            or info[3] != process._managed_start_time):
        raise ValueError()
    return status


def _group_members(pgid):
    members = []
    with os.scandir('/proc') as entries:
        for entry in entries:
            if not entry.name.isdecimal() or int(entry.name) == pgid:
                continue
            try:
                info = _process_info(int(entry.name))
            except FileNotFoundError:
                continue
            if info[1] == pgid:
                if info[2] != pgid:
                    raise ValueError()
                members.append((int(entry.name), info))
    return members


def cleanup_group(process, *, grace=1.0):
    pgid = process.pid
    evidence = {'pgid': pgid, 'verified': False, 'term_sent': False, 'kill_sent': False}
    if not _valid_group(pgid) or getattr(process, 'returncode', None) is not None:
        return evidence
    try:
        _leader_status(process)  # ECHILD or changed generation authorizes no signal.

        def reduced_to_leader():
            status = _leader_status(process)
            members = _group_members(pgid)
            for pid, info in members:
                if info[0] == b'Z':
                    try:
                        child = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                        if child is not None and _process_info(pid) == info:
                            os.waitpid(pid, os.WNOHANG)  # Never consume the held leader.
                    except (ChildProcessError, ProcessLookupError, FileNotFoundError):
                        pass
            # A scan that reaped a member cannot prove absence: its children
            # might have been adopted after enumeration. Always scan again.
            return status is not None and not members

        if reduced_to_leader():
            process.wait(timeout=max(grace, .1))
            evidence['verified'] = True
            return evidence
        for sig, key in ((signal.SIGTERM, 'term_sent'), (signal.SIGKILL, 'kill_sent')):
            _leader_status(process)
            os.killpg(pgid, sig)  # Only while the original generation is held.
            evidence[key] = True
            deadline = time.monotonic() + max(0, grace)
            while True:
                if reduced_to_leader():
                    process.wait(timeout=max(grace, .1))  # Reap last; never killpg afterward.
                    evidence['verified'] = True
                    return evidence
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(.02, max(0, deadline - time.monotonic())))
    except (OSError, ValueError, AttributeError, IndexError, subprocess.TimeoutExpired):
        pass
    return evidence


def _abandon_leader(process):
    # A failed proof never authorizes a numeric group signal. The pidfd remains
    # tied to the original child even if an external waiter has consumed it.
    try:
        _pidfd_kill(process._managed_pidfd)
    except ProcessLookupError:
        pass
    process.wait(timeout=1)


@contextmanager
def _cancellation():
    previous = {}
    cancelled = [False]
    if threading.current_thread() is threading.main_thread():
        def cancel(signum, frame):
            cancelled[0] = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, cancel)
    try:
        yield cancelled
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def verify_quiescent(root):
    """Recovery never relies on a dead manager/leader PID alone."""
    _verify_quiescent(root, acquisition=False)


def verify_acquisition_quiescent(root):
    """Acquisition/report owns the run except its fixed independent meeting home."""
    _verify_quiescent(root, acquisition=True)


MAX_DEPTH = 64
MAX_FDS = 1024
MAX_ENTRIES = 20000
MAX_MARKERS = 512
MAX_RECEIPT = 4096


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid


def _version(info):
    return _identity(info), info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_nlink


def _private_marker(info, *, directory=False):
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError()
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError()
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError()


def _read_receipt(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    data = bytearray()
    while True:
        chunk = os.read(fd, MAX_RECEIPT + 1 - len(data))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > MAX_RECEIPT:
            raise ValueError()


def _receipt_group(data):
    value = json.loads(data)
    if not isinstance(value, dict) or set(value) != {'pgid'} or not _valid_group(value['pgid']):
        raise ValueError()
    return value['pgid']


class _Directories:
    """Retained no-follow ancestry shared by recovery scans and marker writers."""
    def __init__(self, stack):
        self.stack = stack
        self.records = []
        self.missing = []
        self.fds = 0
        self.entries = 0

    def open(self, name, flags, *, parent=None, mode=0o600):
        if self.fds >= MAX_FDS:
            raise ValueError()
        fd = os.open(name, flags, mode, dir_fd=parent)
        self.stack.callback(os.close, fd)
        self.fds += 1
        return fd

    def directory(self, parent, name, *, create=False):
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        fd = self.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, parent=parent)
        if _identity(os.fstat(fd)) != _identity(before):
            raise ValueError()
        self.records.append([parent, name, fd, before, False])
        return fd

    def walk(self, path, *, create=False):
        parts = Path(os.path.abspath(path)).parts[1:]
        if len(parts) > MAX_DEPTH:
            raise ValueError()
        fd = self.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for name in parts:
            before = os.fstat(fd)
            try:
                fd = self.directory(fd, name, create=create)
            except FileNotFoundError:
                if create:
                    raise
                self.missing.append((fd, name, before))
                return None
        return fd

    def names(self, fd):
        names = []
        with os.scandir(fd) as entries:
            for entry in entries:
                self.entries += 1
                if self.entries > MAX_ENTRIES:
                    raise ValueError()
                names.append(entry.name)
        return names

    def check(self):
        for parent, name, fd, before, scanned in self.records:
            signature = _version if scanned else _identity
            if (signature(os.fstat(fd)) != signature(before)
                    or signature(os.stat(name, dir_fd=parent, follow_symlinks=False)) != signature(before)):
                raise ValueError()
        for fd, name, before in self.missing:
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if _version(os.fstat(fd)) != _version(before):
                    raise ValueError()
            else:
                raise ValueError()


def _verify_quiescent(root, *, acquisition):
    excluded = None
    if acquisition:
        from climate_monitor.hermes_identity import SNAPSHOT
        excluded = (SNAPSHOT, 'meetings')
    try:
        with ExitStack() as stack:
            tree = _Directories(stack)
            root_fd = tree.walk(root)
            receipts = []

            def scan(fd, relative, marker_directory=False):
                if len(relative) > MAX_DEPTH:
                    raise ValueError()
                if relative == excluded:
                    return
                if marker_directory:
                    _private_marker(os.fstat(fd), directory=True)
                tree.records[-1][4] = True
                for name in tree.names(fd):
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if marker_directory:
                        _private_marker(info)
                        if (len(receipts) >= MAX_MARKERS or not name.endswith('.json')
                                or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECEIPT):
                            raise ValueError()
                        receipt = tree.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, parent=fd)
                        before = os.fstat(receipt)
                        _private_marker(before)
                        if _version(before) != _version(info) or not stat.S_ISREG(before.st_mode):
                            raise ValueError()
                        data = _read_receipt(receipt)
                        if group_exists(_receipt_group(data)):
                            raise ValueError()
                        tree.records.append([fd, name, receipt, before, True])
                        receipts.append((receipt, data))
                    elif (name == 'managed-processes' or stat.S_ISDIR(info.st_mode)
                          or (excluded and relative + (name,) in (excluded[:1], excluded))):
                        child = tree.directory(fd, name)
                        scan(child, relative + (name,), name == 'managed-processes')
                    elif stat.S_ISLNK(info.st_mode):
                        raise ValueError()
            if root_fd is not None:
                if not tree.records:  # A filesystem root is not a managed run.
                    raise ValueError()
                scan(root_fd, (), Path(root).name == 'managed-processes')
            for fd, initial in receipts:
                current = _read_receipt(fd)
                current_group = _receipt_group(current)
                if (current != initial or group_exists(current_group)
                        or _read_receipt(fd) != current):
                    raise ValueError()
            tree.check()
    except (OSError, ValueError, TypeError, RecursionError):
        raise ManagedFailure(cleanup={'verified': False}) from None


def _write_marker(fd, pgid):
    _private_marker(os.fstat(fd))
    data = json.dumps({'pgid': pgid}).encode('ascii')
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError()
        view = view[written:]
    os.fsync(fd)
    _private_marker(os.fstat(fd))
    return data


def _marker(stack, state_dir):
    tree = _Directories(stack)
    home = tree.walk(state_dir, create=True)
    directory = tree.directory(home, 'managed-processes', create=True)
    info = os.fstat(directory)
    _private_marker(info, directory=True)
    if len(tree.names(directory)) >= MAX_MARKERS:
        raise ValueError()
    name = uuid.uuid4().hex + '.json'
    fd = tree.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, parent=directory)
    record = [directory, name, fd, os.fstat(fd), False]
    tree.records.append(record)
    _write_marker(fd, None)
    tree.check()
    return tree, fd, record


def run_managed(command, *, timeout=None, grace=1.0, heartbeat=None, state_dir=None,
                input=None, capture_output=False, **options):
    """Hold the session leader until descendant cleanup is proven; retain evidence."""
    _subreaper()
    process = None
    error = None
    cleanup = None
    output = stderr = None
    marker = None
    with ExitStack() as stack, _cancellation() as cancelled:
        try:
            stack.callback(os.close, _pidfd_open(os.getpid()))  # Capability check before launch.
            if state_dir is not None:
                try:
                    marker = _marker(stack, state_dir)
                except (OSError, ValueError, TypeError):
                    raise ManagedFailure(cleanup={'verified': False}) from None
            # Private regular files prevent pipe backpressure without a waiter
            # that could reap the leader. Preserve Popen's text/encoding defaults.
            text_mode = bool(options.get('text') or options.get('universal_newlines')
                             or options.get('encoding') or options.get('errors'))
            encoding = options.get('encoding') or ('utf-8' if sys.flags.utf8_mode else locale.getencoding())
            errors = options.get('errors') or 'strict'
            captures = {}
            if capture_output:
                if 'stdout' in options or 'stderr' in options:
                    raise ValueError()
                options['stdout'] = options['stderr'] = subprocess.PIPE
            options.setdefault('stderr', subprocess.DEVNULL)
            for name in ('stdout', 'stderr'):
                if options.get(name) == subprocess.PIPE:
                    stream = stack.enter_context(tempfile.TemporaryFile())
                    captures[name] = stream
                    options[name] = stream
            if input is not None:
                if 'stdin' in options:
                    raise ValueError()
                stream = stack.enter_context(tempfile.TemporaryFile())
                stream.write(input.encode(encoding, errors) if text_mode else input)
                stream.seek(0)
                options['stdin'] = stream
            else:
                options.setdefault('stdin', subprocess.DEVNULL)
                if options['stdin'] == subprocess.PIPE:
                    options['stdin'] = subprocess.DEVNULL  # communicate(None) closes it.
            options.update(start_new_session=True, close_fds=True)
            if cancelled[0]:
                raise KeyboardInterrupt()
            process = subprocess.Popen(command, **options)
            # No poll/wait/communicate is permitted before cleanup. WNOWAIT
            # and a held pidfd establish the original child generation.
            try:
                process._managed_start_time = _process_info(process.pid)[3]
            finally:
                process._managed_pidfd = _pidfd_open(process.pid)
                stack.callback(os.close, process._managed_pidfd)
            _leader_status(process)
            if marker is not None:
                tree, fd, record = marker
                expected = _write_marker(fd, process.pid)
                record[3], record[4] = os.fstat(fd), True
                tree.check()
            deadline = None if timeout is None else time.monotonic() + max(0, timeout)
            while True:
                if cancelled[0]:
                    raise KeyboardInterrupt()
                if heartbeat:
                    heartbeat(process.pid)
                if _leader_status(process) is not None:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(.02 if deadline is None else min(.02, max(0, deadline - time.monotonic())))
        except BaseException as exc:
            error = failure_from_exception(exc)
        finally:
            if process is not None:
                cleanup = cleanup_group(process, grace=grace)
                if not cleanup['verified']:
                    try:
                        _abandon_leader(process)
                    except (OSError, AttributeError, subprocess.TimeoutExpired):
                        pass  # Never turn a failed cleanup proof into recovery.
            if marker is not None:
                try:
                    tree, fd, record = marker
                    if process is None:
                        raise ValueError()
                    current = _read_receipt(fd)
                    if current != expected or _receipt_group(current) != process.pid:
                        raise ValueError()
                    tree.check()
                    if _read_receipt(fd) != expected:
                        raise ValueError()
                except (OSError, ValueError, UnboundLocalError):
                    cleanup = {**(cleanup or {}), 'verified': False}
                    error = error or ManagedFailure()
        if cancelled[0]:
            error = ManagedFailure('timeout_cancelled')
        if cleanup is not None and not cleanup['verified']:
            raise ManagedFailure(error.evidence['category'] if error else 'internal', cleanup=cleanup) from None
        if error:
            raise ManagedFailure(error.evidence['category'], cleanup=error.evidence['cleanup'] or cleanup) from None
        try:
            for name, stream in captures.items():
                stream.seek(0)
                value = stream.read()
                if text_mode:
                    value = value.decode(encoding, errors).replace('\r\n', '\n').replace('\r', '\n')
                if name == 'stdout':
                    output = value
                else:
                    stderr = value
        except (OSError, ValueError, LookupError):
            raise ManagedFailure(cleanup=cleanup) from None
    if cancelled[0]:
        raise ManagedFailure('timeout_cancelled', cleanup=cleanup) from None
    result = subprocess.CompletedProcess(command, process.returncode, output, stderr)
    result.cleanup = cleanup
    return result
