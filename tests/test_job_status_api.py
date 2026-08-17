from __future__ import annotations

import errno
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import api_server
from climate_monitor import job_status


def _fresh_snapshot() -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    monday = now.date()
    monday = monday.fromordinal(monday.toordinal() - monday.weekday())
    date_text = monday.isoformat()
    return {
        "schema_version": "weekly-job-status.v1",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jobs": {
            "monitor": {
                "scheduled_for": f"{date_text}T08:00:00Z",
                "state": "scheduled",
            },
            "email": {
                "scheduled_for": f"{date_text}T09:00:00Z",
                "state": "scheduled",
            },
            "publisher": {
                "scheduled_for": f"{date_text}T10:00:00Z",
                "state": "scheduled",
            },
        },
    }


def _write(directory: Path, content: dict | str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "scheduler-status.json"
    if isinstance(content, str):
        target.write_text(content, encoding="utf-8")
    else:
        target.write_text(json.dumps(content), encoding="utf-8")


def test_job_status_has_stable_success_and_unavailable_responses(tmp_path, monkeypatch):
    client = TestClient(api_server.app)

    monkeypatch.delenv("CLIMATE_JOB_STATUS_DIR", raising=False)
    response = client.get("/api/job-status")
    assert response.status_code == 503
    assert response.json() == {
        "available": False,
        "reason": "not_configured",
    }

    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", "relative")
    response = client.get("/api/job-status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_location"}

    monkeypatch.setenv(
        "CLIMATE_JOB_STATUS_DIR",
        str(tmp_path / "child" / ".." / "status"),
    )
    response = client.get("/api/job-status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_location"}

    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(tmp_path / "missing"))
    response = client.get("/api/job-status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "snapshot_unavailable"}

    parent_file = tmp_path / "parent-file"
    parent_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(parent_file / "nested"))
    response = client.get("/api/job-status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_location"}

    original_lstat = job_status.os.lstat

    def looped_parent(path, *args, **kwargs):
        if Path(path) == tmp_path:
            raise OSError(errno.ELOOP, "loop")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(job_status.os, "lstat", looped_parent)
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(tmp_path / "looped"))
    response = client.get("/api/job-status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_location"}
    monkeypatch.setattr(job_status.os, "lstat", original_lstat)

    configured = tmp_path / "configured"
    configured.mkdir()
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(configured))
    response = client.get("/api/job-status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "snapshot_unavailable"}

    _write(configured, "not json")
    response = client.get("/api/job-status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_snapshot"}

    original_open = job_status.os.open

    def parent_changed(*_args, **_kwargs):
        raise NotADirectoryError(errno.ENOTDIR, "parent changed")

    monkeypatch.setattr(job_status.os, "open", parent_changed)
    response = client.get("/api/job-status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_snapshot"}
    monkeypatch.setattr(job_status.os, "open", original_open)

    _write(configured, _fresh_snapshot())
    response = client.get("/api/job-status")
    assert response.status_code == 200
    assert response.json()["available"] is True
    assert set(response.json()["jobs"]) == {"monitor", "email", "publisher"}

    def nofollow_race(*_args, **_kwargs):
        raise OSError(errno.ELOOP, "symlink race")

    monkeypatch.setattr(job_status.os, "open", nofollow_race)
    response = client.get("/api/job-status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_snapshot"}


def test_invalid_job_snapshot_does_not_break_core_or_other_status_routes(
    tmp_path, monkeypatch
):
    directory = tmp_path / "status"
    _write(directory, "{")
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(directory))
    monkeypatch.setattr(api_server.responder, "config", lambda: {"agent_mode": "offline"})
    monkeypatch.setattr(
        api_server.responder,
        "answer",
        lambda *args, **kwargs: {"text": "offline", "sources": []},
    )
    client = TestClient(api_server.app)

    assert client.get("/api/job-status").status_code == 503
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/config").json() == {"agent_mode": "offline"}
    assert client.get("/").status_code == 200
    assert client.post("/api/chat", json={"message": "latest"}).status_code == 200

    monkeypatch.delenv("CLIMATE_UPDATE_STATUS_DIR", raising=False)
    assert client.get("/api/update-status").json()["reason"] == "not_configured"
    monkeypatch.delenv("CLIMATE_REGISTRY_DB", raising=False)
    assert client.get("/api/registry/status").json()["reason"] == "not_configured"

    body = json.dumps(client.get("/api/job-status").json())
    assert str(directory) not in body


def test_job_status_api_classifies_read_phase_parent_errors(tmp_path, monkeypatch):
    directory = tmp_path / "status"
    _write(directory, _fresh_snapshot())
    monkeypatch.setenv("CLIMATE_JOB_STATUS_DIR", str(directory))
    monkeypatch.setattr(
        job_status,
        "_external_directory",
        lambda *_args, **_kwargs: directory,
    )
    original_lstat = job_status.os.lstat
    client = TestClient(api_server.app)

    for path_error, reason in (
        (OSError(errno.ELOOP, "loop"), "invalid_snapshot"),
        (NotADirectoryError(errno.ENOTDIR, "not a directory"), "invalid_snapshot"),
        (FileNotFoundError(errno.ENOENT, "missing"), "snapshot_unavailable"),
        (PermissionError(errno.EACCES, "unreadable"), "snapshot_unavailable"),
    ):
        def fail_parent(path, *args, _error=path_error, **kwargs):
            if Path(path) == directory:
                raise _error
            return original_lstat(path, *args, **kwargs)

        monkeypatch.setattr(job_status.os, "lstat", fail_parent)
        response = client.get("/api/job-status")
        assert response.status_code == 503
        assert response.json() == {"available": False, "reason": reason}
