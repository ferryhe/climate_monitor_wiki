from pathlib import Path

from climate_delivery import io


def test_parent_directory_fsync_runs_on_posix(monkeypatch, tmp_path):
    target = tmp_path / "artifact.json"
    calls = []
    monkeypatch.setattr(io, "_POSIX_DIRECTORY_FSYNC", True)
    monkeypatch.setattr(io.os, "open", lambda path, flags: calls.append((Path(path), flags)) or 99)
    monkeypatch.setattr(io.os, "fsync", lambda descriptor: calls.append(("fsync", descriptor)))
    monkeypatch.setattr(io.os, "close", lambda descriptor: calls.append(("close", descriptor)))

    io._fsync_parent(target)

    assert calls[0][0] == tmp_path
    assert calls[1:] == [("fsync", 99), ("close", 99)]


def test_parent_directory_fsync_is_safely_skipped_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(io, "_POSIX_DIRECTORY_FSYNC", False)
    monkeypatch.setattr(io.os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("open called")))
    io._fsync_parent(tmp_path / "artifact.json")


def test_atomic_write_fsyncs_parent_after_replace(monkeypatch, tmp_path):
    target = tmp_path / "artifact.json"
    synced = []

    def record_sync(path):
        assert Path(path).read_bytes() == b"durable"
        synced.append(Path(path))

    monkeypatch.setattr(io, "_fsync_parent", record_sync)

    io.atomic_write_bytes(target, b"durable")

    assert target.read_bytes() == b"durable"
    assert synced == [target]
