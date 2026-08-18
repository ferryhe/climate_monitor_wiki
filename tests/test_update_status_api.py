from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

import api_server
from climate_monitor.run_ledger import append_attempt


ROOT = Path(__file__).resolve().parents[1]


def _attempt() -> dict:
    now = datetime.now(timezone.utc)
    report_day = now.date() - timedelta(days=now.weekday())
    scheduled = datetime.combine(report_day, time(hour=8), tzinfo=timezone.utc)
    finished = scheduled + timedelta(minutes=30)
    if finished > now:
        scheduled -= timedelta(days=7)
        finished -= timedelta(days=7)
        report_day -= timedelta(days=7)
    report_date = report_day.isoformat()
    return {
        "schema_version": "weekly-run-attempt.v1",
        "attempt_id": f"{scheduled:%Y%m%dt%H%M%Sz}-attempt-01",
        "stage": "monitor",
        "report_date": report_date,
        "scheduled_for": scheduled.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "success",
        "result_code": "report_written",
        "report": {
            "report_id": f"climate-monitor-{report_date}",
            "report_date": report_date,
            "sha256": "a" * 64,
        },
    }


def test_update_status_has_stable_unavailable_reasons(tmp_path, monkeypatch):
    client = TestClient(api_server.app)

    monkeypatch.delenv("CLIMATE_UPDATE_STATUS_DIR", raising=False)
    response = client.get("/api/update-status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "not_configured"}

    monkeypatch.setenv("CLIMATE_UPDATE_STATUS_DIR", "relative-ledger")
    response = client.get("/api/update-status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_location"}

    missing = tmp_path / "missing"
    monkeypatch.setenv("CLIMATE_UPDATE_STATUS_DIR", str(missing))
    response = client.get("/api/update-status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "ledger_unavailable"}

    corrupt = tmp_path / "corrupt"
    path = corrupt / "attempts" / "monitor" / "2026-08-10"
    path.mkdir(parents=True)
    (path / "bad.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("CLIMATE_UPDATE_STATUS_DIR", str(corrupt))
    response = client.get("/api/update-status")
    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_ledger"}


def test_valid_empty_and_populated_ledgers_are_distinct(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    monkeypatch.setenv("CLIMATE_UPDATE_STATUS_DIR", str(ledger))
    client = TestClient(api_server.app)
    attempt = _attempt()

    empty = client.get("/api/update-status")
    assert empty.status_code == 200
    assert empty.json()["state"] == "empty"
    assert empty.json()["attempt_count"] == 0

    append_attempt(ledger, attempt, repository_root=ROOT)
    populated = client.get("/api/update-status")
    assert populated.status_code == 200
    assert populated.json()["state"] == "current"
    assert populated.json()["stages"]["monitor"]["last_attempt"]["attempt_id"] == attempt[
        "attempt_id"
    ]


def test_invalid_ledger_does_not_break_health_home_config_or_chat(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger"
    path = ledger / "attempts" / "monitor" / "2026-08-10"
    path.mkdir(parents=True)
    (path / "bad.json").write_text("not json", encoding="utf-8")
    monkeypatch.setenv("CLIMATE_UPDATE_STATUS_DIR", str(ledger))
    monkeypatch.setattr(api_server.responder, "config", lambda: {"agent_mode": "offline"})
    monkeypatch.setattr(
        api_server.responder,
        "answer",
        lambda *args, **kwargs: {"text": "offline answer", "sources": [{"date": "2026-08-10"}]},
    )
    client = TestClient(api_server.app)

    assert client.get("/api/update-status").status_code == 503
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/config").json() == {"agent_mode": "offline"}
    assert client.get("/").status_code == 200
    chat = client.post("/api/chat", json={"message": "latest"})
    assert chat.status_code == 200
    assert chat.json()["text"] == "offline answer"
    assert str(ledger) not in json.dumps(client.get("/api/update-status").json())


def test_pathological_json_is_sanitized_as_invalid_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger"
    path = ledger / "attempts" / "monitor" / "2026-08-10"
    path.mkdir(parents=True)
    (path / "bad.json").write_text('{"number":' + ("9" * 5000) + "}", encoding="utf-8")
    monkeypatch.setenv("CLIMATE_UPDATE_STATUS_DIR", str(ledger))

    response = TestClient(api_server.app).get("/api/update-status")

    assert response.status_code == 503
    assert response.json() == {"available": False, "reason": "invalid_ledger"}
