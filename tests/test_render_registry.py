from __future__ import annotations

import os
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

import api_server
from scripts import run_render_web


ROOT = Path(__file__).resolve().parents[1]


def test_actual_render_startup_keeps_approved_report_fields_and_pdf_public_metadata(tmp_path,monkeypatch):
    from climate_registry.audit import build_audit_registry
    from climate_registry.annotations import ArticleAnnotation
    from climate_registry.read_api import RegistryReader,RegistryNotFoundError
    from climate_registry.publication import stage_entities,snapshot_entity,stage_snapshot,_approve,export_public_snapshot,_pdf_report_dto
    from climate_registry.pdf_intake import persist_pdf_intake
    from climate_monitor.pdf_intake import import_pdf_reports
    from test_pdf_intake import _report_pdf_with_two_articles
    import pytest
    application=tmp_path/"application";sources=application/"sources";sources.mkdir(parents=True)
    original=ROOT/"sources/climate-monitor-2026-09-14.md";report=sources/original.name;shutil.copyfile(original,report)
    source_bytes=report.read_bytes();source_sha=hashlib.sha256(source_bytes).hexdigest()
    database=tmp_path/"live.sqlite3";build_audit_registry(sources,database,tmp_path/"audit")
    live=RegistryReader(database,repository_root=application,source_dir=sources)
    article=live.articles()["items"][0];identity=article["article_id"];url=article["canonical_url"]
    annotation=ArticleAnnotation(url,url,"Approved curated title","original_content","Approved curated summary",("Climate Risk",),("insurance",),"2026-09-14")
    pdf=tmp_path/"source.pdf";_report_pdf_with_two_articles(pdf);monkeypatch.delenv("TYPESAFE_API_KEY",raising=False)
    bundle=import_pdf_reports([pdf]);persist_pdf_intake(database,tmp_path/"pdf-backups",bundle)
    with sqlite3.connect(database) as db:
        for sha in stage_entities(db):_approve(db,sha,{"basis":"explicit exact PDF/report fixture approval"}, status="accepted_legacy")
        sha=stage_snapshot(db,snapshot_entity(db,"article",identity,annotations={url:annotation},source_dir=sources))
        _approve(db,sha,{"basis":"explicit approved curated report fixture"}, status="accepted_legacy")
    expected,expected_identity=live.report_with_identity("2026-09-14")
    display=next(item for item in expected["articles"] if item["article_id"]==identity)
    fields=("title","summary","summary_provenance","categories","keywords","metadata_provenance","source_annotation")
    assert {key:display[key] for key in fields}=={key:live.article(identity)[key] for key in fields}
    expected_pdf=live.pdf_reports_all();assert len(expected_pdf)==1
    document=expected_pdf[0]["document_sha256"];expected_detail=_pdf_report_dto(live.pdf_report(document))
    wiki=application/"wiki";wiki.mkdir();snapshot=wiki/"public-registry.json";export_public_snapshot(database,snapshot,reader=live)
    artifact=json.loads(snapshot.read_text())
    assert artifact["pdf_reports"]==expected_pdf and artifact["pdf_report_details"][document]==expected_detail
    assert expected_detail["report_pdf"] is None and expected_detail["pages"]==[] and expected_detail["executive_summary"]==[]
    text=snapshot.read_text()
    for private in ("original_pdf_base64","pdf_bytes",'"checks"','"latest_fetch"',str(tmp_path)):assert private not in text
    monkeypatch.setattr(run_render_web,"ROOT",application);monkeypatch.setattr(run_render_web,"load_dotenv",lambda *_:False)
    monkeypatch.delenv("CLIMATE_REGISTRY_DB",raising=False);monkeypatch.delenv("SOURCE_DIR",raising=False)
    observed=[]
    def run_app():
        rendered=RegistryReader(os.environ["CLIMATE_REGISTRY_DB"],repository_root=application,source_dir=sources)
        payload,actual_identity=rendered.report_with_identity("2026-09-14")
        assert actual_identity==expected_identity and actual_identity.report_sha256==source_sha
        assert payload["executive_summary"]==expected["executive_summary"]
        current=next(item for item in payload["articles"] if item["article_id"]==identity)
        assert {key:current[key] for key in fields}=={key:display[key] for key in fields}
        assert rendered.pdf_reports_all()==expected_pdf and rendered.pdf_report(document)==expected_detail
        with pytest.raises(RegistryNotFoundError):rendered.pdf_report(document,include_bytes=True)
        monkeypatch.setattr(api_server,"_registry_reader",lambda:rendered)
        monkeypatch.setattr(api_server,"_range_report_overlay",lambda:(None,None,None))
        client=TestClient(api_server.app)
        assert any(item.get("document_sha256")==document for item in client.get("/api/registry/reports?include_pdf=true").json()["items"])
        assert client.get("/api/registry/pdf-intake/reports/"+document).json()==expected_detail
        assert client.get("/api/registry/pdf-intake/reports/"+document+"/pdf").status_code==404
        observed.append(True)
    monkeypatch.setattr(run_render_web,"_run_app",run_app);run_render_web.main()
    assert observed==[True] and report.read_bytes()==source_bytes


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
        from climate_registry.persistent import initialize_registry
        initialize_registry(database)

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
