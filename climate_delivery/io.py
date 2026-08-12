import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import GenerationError, LockStateError


_POSIX_DIRECTORY_FSYNC = os.name == "posix"


def _fsync_parent(path: Path) -> None:
    if not _POSIX_DIRECTORY_FSYNC:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(Path(path).parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)
    _fsync_parent(destination)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(Path(temporary), path)
    except Exception as exc:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        raise GenerationError(f"could not atomically write {path.name}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    atomic_write_bytes(path, payload)


@contextmanager
def exclusive_lock(state_dir: Path, key: str) -> Iterator[None]:
    lock_dir = Path(state_dir) / "locks"
    lock_path = lock_dir / f"{key}.lock"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise LockStateError(f"report delivery is locked: {key}") from exc
    except OSError as exc:
        raise LockStateError("could not create the delivery lock") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
