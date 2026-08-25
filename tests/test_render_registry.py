from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

import api_server
from scripts import run_render_web


ROOT = Path(__file__).resolve().parents[1]


def test_render_blueprint_uses_registry_bootstrap_runner():
    blueprint = (ROOT / "render.yaml").read_text(encoding="utf-8")

    assert "startCommand: python -m scripts.run_render_web" in blueprint


def test_render_runner_builds_ephemeral_registry_before_starting(monkeypatch):
    observed: dict[str, object] = {}
    expected_latest = max(
        path.stem.removeprefix("climate-monitor-")
        for path in (ROOT / "sources").glob("climate-monitor-*.md")
    )
    monkeypatch.delenv("CLIMATE_REGISTRY_DB", raising=False)
    monkeypatch.setenv("PORT", "9876")

    def run_app(app: str, *, host: str, port: int) -> None:
        database = Path(os.environ["CLIMATE_REGISTRY_DB"])
        observed.update(app=app, host=host, port=port, database=database)
        assert database.is_file()

        client = TestClient(api_server.app)
        status = client.get("/api/registry/status")
        reports = client.get("/api/registry/reports?page=1&page_size=1")
        articles = client.get("/api/registry/articles?page=1&page_size=1")

        assert status.status_code == 200
        assert status.json()["latest_report_date"] == expected_latest
        assert reports.status_code == 200
        assert reports.json()["items"][0]["report_date"] == expected_latest
        assert articles.status_code == 200
        assert articles.json()["pagination"]["total"] > 0

    monkeypatch.setattr(run_render_web.uvicorn, "run", run_app)

    run_render_web.main()

    assert observed["app"] == "api_server:app"
    assert observed["host"] == "0.0.0.0"
    assert observed["port"] == 9876
    assert not Path(observed["database"]).exists()
    assert "CLIMATE_REGISTRY_DB" not in os.environ


def test_render_runner_preserves_explicit_registry(monkeypatch, tmp_path):
    database = tmp_path / "external.sqlite3"
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setenv("PORT", "8501")
    observed: dict[str, object] = {}

    def fail_build(*_args, **_kwargs):
        raise AssertionError("an explicitly configured registry must not be rebuilt")

    def run_app(app: str, *, host: str, port: int) -> None:
        observed.update(app=app, host=host, port=port)

    monkeypatch.setattr(run_render_web, "build_audit_registry", fail_build)
    monkeypatch.setattr(run_render_web.uvicorn, "run", run_app)

    run_render_web.main()

    assert os.environ["CLIMATE_REGISTRY_DB"] == str(database)
    assert observed == {"app": "api_server:app", "host": "0.0.0.0", "port": 8501}
