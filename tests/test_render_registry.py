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
    monkeypatch.setattr(run_render_web, "load_dotenv", lambda *_args: False)

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
    monkeypatch.setattr(run_render_web, "load_dotenv", lambda *_args: False)
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


def test_render_runner_honors_dotenv_registry(monkeypatch, tmp_path):
    database = tmp_path / "dotenv-registry.sqlite3"
    (tmp_path / ".env").write_text(
        f"CLIMATE_REGISTRY_DB={database}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run_render_web, "ROOT", tmp_path)
    monkeypatch.delenv("CLIMATE_REGISTRY_DB", raising=False)
    monkeypatch.setenv("PORT", "8501")
    observed: dict[str, object] = {}

    def fail_build(*_args, **_kwargs):
        raise AssertionError("the dotenv registry must remain authoritative")

    def run_app(app: str, *, host: str, port: int) -> None:
        observed.update(
            app=app,
            host=host,
            port=port,
            database=os.environ["CLIMATE_REGISTRY_DB"],
        )

    monkeypatch.setattr(run_render_web, "build_audit_registry", fail_build)
    monkeypatch.setattr(run_render_web.uvicorn, "run", run_app)

    try:
        run_render_web.main()
    finally:
        os.environ.pop("CLIMATE_REGISTRY_DB", None)

    assert observed == {
        "app": "api_server:app",
        "host": "0.0.0.0",
        "port": 8501,
        "database": str(database),
    }


def test_render_runner_honors_dotenv_source_directory(monkeypatch, tmp_path):
    source_dir = tmp_path / "custom-sources"
    source_dir.mkdir()
    (tmp_path / ".env").write_text("SOURCE_DIR=custom-sources\n", encoding="utf-8")
    monkeypatch.setattr(run_render_web, "ROOT", tmp_path)
    monkeypatch.delenv("CLIMATE_REGISTRY_DB", raising=False)
    monkeypatch.delenv("SOURCE_DIR", raising=False)
    monkeypatch.setenv("PORT", "8501")
    observed: dict[str, object] = {}

    def build_registry(source: Path, database: Path, output: Path) -> None:
        observed.update(source=source, database=database, output=output)
        database.write_bytes(b"registry")

    def run_app(app: str, *, host: str, port: int) -> None:
        observed.update(app=app, host=host, port=port)

    monkeypatch.setattr(run_render_web, "build_audit_registry", build_registry)
    monkeypatch.setattr(run_render_web.uvicorn, "run", run_app)

    try:
        run_render_web.main()
    finally:
        os.environ.pop("SOURCE_DIR", None)

    assert observed["source"] == source_dir.resolve()
    assert observed["app"] == "api_server:app"
