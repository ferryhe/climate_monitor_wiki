from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

import api_server
from api_server import app
from climate_registry.schema import apply_migrations
from report_artifact_fixtures import (
    CANONICAL_REPORT_SHA256,
    canonical_report,
    canonical_report_bytes,
    write_canonical_artifact,
)


REPORT_DATE = "2026-08-17"
REPORT_FILENAME = f"climate-monitor-{REPORT_DATE}.md"
REPORT_SHA256 = CANONICAL_REPORT_SHA256
PDF_FILENAME = f"climate-monitor-{REPORT_DATE}.pdf"


def _registry(directory: Path) -> Path:
    report = canonical_report()
    directory.mkdir()
    database = directory / "article-registry.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection)
    with connection:
        connection.execute(
            "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "report-artifact",
                report.report_date,
                report.filename,
                report.title,
                report.sha256,
                "weekly",
                "weekly-pillars-v1",
                report.checked,
                report.succeeded,
                report.failed,
                "[]",
            ),
        )
    connection.close()
    return database


def _use_canonical_source(directory: Path, monkeypatch) -> None:
    directory.mkdir()
    (directory / REPORT_FILENAME).write_bytes(canonical_report_bytes())
    monkeypatch.setattr(api_server, "SOURCE_DIR", directory)


def test_report_endpoint_adds_null_fields_without_changing_existing_payload(tmp_path, monkeypatch):
    report = canonical_report()
    database = _registry(tmp_path / "registry")
    _use_canonical_source(tmp_path / "sources", monkeypatch)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.delenv("CLIMATE_DELIVERY_OUTPUT_DIR", raising=False)
    client = TestClient(app)

    response = client.get(f"/api/registry/reports/{REPORT_DATE}")

    assert response.status_code == 200
    body = response.json()
    assert body.pop("report_briefing") is None
    assert body.pop("report_pdf") is None
    assert body == {
        "report_date": REPORT_DATE,
        "report_title": report.title,
        "cadence": "weekly",
        "report_format": "weekly-pillars-v1",
        "executive_summary": list(report.monitoring_notes),
        "monitoring": {
            "status": "complete",
            "sites_checked": 57,
            "sites_succeeded": 57,
            "sites_failed": 0,
            "warning_count": 0,
        },
        "articles": [],
    }


def test_report_endpoint_projects_valid_artifact_without_sensitive_manifest_data(tmp_path, monkeypatch):
    database = _registry(tmp_path / "registry")
    _use_canonical_source(tmp_path / "sources", monkeypatch)
    output = tmp_path / "delivery"
    fixture = write_canonical_artifact(output)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setenv("CLIMATE_DELIVERY_OUTPUT_DIR", str(output.resolve()))

    response = TestClient(app).get(f"/api/registry/reports/{REPORT_DATE}")

    assert response.status_code == 200
    body = response.json()
    assert fixture.report.sha256 == CANONICAL_REPORT_SHA256
    assert (fixture.report.checked, fixture.report.succeeded, fixture.report.failed) == (57, 57, 0)
    assert len([item for item in fixture.report.highlights if item.pillar == "A"]) == 9
    assert len([item for item in fixture.report.highlights if item.pillar == "B"]) == 17
    assert len(fixture.summary["executive_summary"]) == 4
    assert len(fixture.summary["monitoring_notes"]) == 3
    assert body["report_title"] == fixture.report.title
    assert body["report_briefing"] == {
        "executive_summary": fixture.summary["executive_summary"],
        "monitoring_snapshot": {
            "sites_checked": 57,
            "sites_succeeded": 57,
            "sites_failed": 0,
            "pillar_a_updates": 9,
            "pillar_b_updates": 17,
            "notes": fixture.summary["monitoring_notes"],
        },
    }
    assert body["report_pdf"] == {
        "filename": PDF_FILENAME,
        "download_url": f"/api/registry/reports/{REPORT_DATE}/pdf",
    }
    assert body["executive_summary"] == list(fixture.report.monitoring_notes)
    assert body["articles"] == []
    assert REPORT_SHA256 not in response.text
    assert "artifact-only" not in response.text
    assert str(fixture.artifact_dir) not in response.text


def test_pdf_download_revalidates_and_sets_safe_headers(tmp_path, monkeypatch):
    database = _registry(tmp_path / "registry")
    output = tmp_path / "delivery"
    fixture = write_canonical_artifact(output)
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    monkeypatch.setenv("CLIMATE_DELIVERY_OUTPUT_DIR", str(output.resolve()))

    response = TestClient(app).get(f"/api/registry/reports/{REPORT_DATE}/pdf")

    assert response.status_code == 200
    assert response.content == fixture.pdf_bytes
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == f'attachment; filename="{PDF_FILENAME}"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert str(fixture.artifact_dir) not in response.text

    (fixture.artifact_dir / PDF_FILENAME).write_bytes(b"%PDF-tampered")
    invalid = TestClient(app).get(f"/api/registry/reports/{REPORT_DATE}/pdf")
    assert invalid.status_code == 404
    assert invalid.json() == {"detail": "Report PDF not found."}
    assert str(fixture.artifact_dir) not in invalid.text
    fallback = TestClient(app).get(f"/api/registry/reports/{REPORT_DATE}")
    assert fallback.status_code == 200
    assert fallback.json()["report_briefing"] is None
    assert fallback.json()["report_pdf"] is None


def test_pdf_download_preserves_registry_query_and_unavailable_semantics(tmp_path, monkeypatch):
    monkeypatch.delenv("CLIMATE_REGISTRY_DB", raising=False)
    client = TestClient(app)
    unavailable = client.get(f"/api/registry/reports/{REPORT_DATE}/pdf")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "Article registry is unavailable."}

    database = _registry(tmp_path / "registry")
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(database))
    assert client.get("/api/registry/reports/not-a-date/pdf").status_code == 400
    assert client.get("/api/registry/reports/2026-08-10/pdf").status_code == 404
