from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api_server
import climate_registry.weekly as weekly
import scripts.weekly_registry_refresh as refresh
from api_server import app
from climate_delivery.artifacts import ARTIFACT_ONLY_DELIVERY_STATUS
from climate_delivery.io import atomic_write_json
from climate_delivery.pdf import render_pdf
from climate_delivery.pipeline import _manifest
from climate_delivery.report import parse_weekly_report
from climate_delivery.summary import build_summary, write_summary
from climate_monitor.run_ledger import append_attempt
from climate_registry.audit import build_audit_registry
from climate_registry.capture import capture_enrich_registry
from climate_registry.errors import RegistryLockError
from climate_registry.fetch import FetchFailure, FetchResponse
from climate_registry.persistent import update_registry


TARGET = "2026-08-17"
PREVIOUS = "2026-08-10"
NOW = datetime(2026, 8, 17, 11, 0, tzinfo=timezone.utc)


def _item(title: str, url: str) -> str:
    return (
        f"- **{title}** (web)\n"
        "  - Climate risk and insurance regulation changed this week.\n"
        f"  🔗 {url}\n"
    )


def _report(report_date: str, *, title: str, url: str) -> str:
    return f"""# Weekly Climate & Actuarial Monitor
**Report Date:** {report_date}
## Executive Summary
- Sites checked: **1**, succeeded: **1**, failed: **0**
## Pillar A — Changes
{_item(title, url)}
## Pillar B — Intelligence
## Original Links
- {url}
"""


def _run(*arguments: str, cwd: Path) -> None:
    subprocess.run(
        arguments,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def _write_artifact(source: Path, artifact_root: Path) -> None:
    report = parse_weekly_report(source)
    summary = build_summary(report)
    destination = artifact_root / report.report_date / report.sha256
    summary_path = destination / "summary.json"
    pdf_name = f"climate-monitor-{report.report_date}.pdf"
    pdf_path = destination / pdf_name
    write_summary(summary, summary_path)
    render_pdf(summary, pdf_path)
    summary_sha = hashlib.sha256(summary_path.read_bytes()).hexdigest()
    pdf_sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    atomic_write_json(
        destination / "manifest.json",
        _manifest(
            summary,
            {"status": ARTIFACT_ONLY_DELIVERY_STATUS, "recipients": []},
            summary_sha256=summary_sha,
            pdf_name=pdf_name,
            pdf_sha256=pdf_sha,
        ),
    )


def _publisher_attempt(report_sha256: str, *, status: str = "success") -> dict:
    return {
        "schema_version": "weekly-run-attempt.v1",
        "attempt_id": f"20260817t100000z-publisher-{status}",
        "stage": "publisher",
        "report_date": TARGET,
        "scheduled_for": "2026-08-17T10:00:00Z",
        "finished_at": "2026-08-17T10:05:00Z",
        "status": status,
        "result_code": "rolling_pr_updated"
        if status == "success"
        else "publisher_error",
        "report": {
            "report_id": f"climate-monitor-{TARGET}",
            "report_date": TARGET,
            "sha256": report_sha256,
        },
    }


class FakeTransport:
    def __init__(self, *, failure: bool = False) -> None:
        self.failure = failure
        self.calls = 0

    def request(self, target, headers, *, timeout, max_bytes):
        self.calls += 1
        if self.failure:
            raise FetchFailure("timeout", "injected timeout")
        body = b"""<html><body><h1>Climate insurance outlook</h1>
        <p>Climate risk and transition risk affect insurance capital, underwriting,
        investment portfolios, disclosure standards, catastrophe models, regulatory
        policy, financial resilience and sustainability reporting this week.</p>
        </body></html>"""
        return FetchResponse(
            200,
            target.url,
            {"content-type": "text/html; charset=utf-8"},
            body,
        )


class SparseTransport:
    def request(self, target, headers, *, timeout, max_bytes):
        return FetchResponse(
            200,
            target.url,
            {"content-type": "text/html; charset=utf-8"},
            b"<html><body><p>Too sparse.</p></body></html>",
        )


@dataclass
class WeeklyFixture:
    repository: Path
    source_dir: Path
    source: Path
    database: Path
    artifact_root: Path
    backup_dir: Path
    ledger_dir: Path
    lock_file: Path

    def arguments(self, **overrides):
        arguments = {
            "target_date": TARGET,
            "source_dir": self.source_dir,
            "database": self.database,
            "artifact_root": self.artifact_root,
            "backup_dir": self.backup_dir,
            "lock_file": self.lock_file,
            "publisher_ledger_dir": self.ledger_dir,
            "clock": lambda: NOW,
            "resolver": lambda _host, _port: ["93.184.216.34"],
            "transport": FakeTransport(),
        }
        arguments.update(overrides)
        return arguments


@pytest.fixture
def weekly_fixture(tmp_path) -> WeeklyFixture:
    repository = tmp_path / "checkout"
    source_dir = repository / "sources"
    source_dir.mkdir(parents=True)
    previous = source_dir / f"climate-monitor-{PREVIOUS}.md"
    previous.write_text(
        _report(PREVIOUS, title="Previous", url="https://example.com/previous"),
        encoding="utf-8",
    )
    _run("git", "init", "-q", cwd=repository)
    _run("git", "config", "user.email", "test@example.com", cwd=repository)
    _run("git", "config", "user.name", "Test", cwd=repository)
    _run("git", "add", "sources", cwd=repository)
    _run("git", "commit", "-qm", "previous report", cwd=repository)

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = runtime / "registry.sqlite3"
    build_audit_registry(source_dir, database, runtime / "audit")

    source = source_dir / f"climate-monitor-{TARGET}.md"
    source.write_text(
        _report(TARGET, title="Target", url="https://example.com/target"),
        encoding="utf-8",
    )
    _run("git", "add", "sources", cwd=repository)
    _run("git", "commit", "-qm", "target report", cwd=repository)

    artifact_root = runtime / "artifacts"
    _write_artifact(source, artifact_root)
    ledger_dir = runtime / "ledger"
    append_attempt(
        ledger_dir,
        _publisher_attempt(hashlib.sha256(source.read_bytes()).hexdigest()),
        repository_root=repository,
    )
    return WeeklyFixture(
        repository=repository,
        source_dir=source_dir,
        source=source,
        database=database,
        artifact_root=artifact_root,
        backup_dir=runtime / "backups",
        ledger_dir=ledger_dir,
        lock_file=database.with_name(f"{database.name}.lock"),
    )


def _counts(database: Path) -> tuple[int, int, int]:
    connection = sqlite3.connect(database)
    try:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("reports", "articles", "article_enrichments")
        )
    finally:
        connection.close()


def test_dry_run_is_read_only_and_returns_exact_target_plan(weekly_fixture):
    before = weekly_fixture.database.read_bytes()
    result = weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))

    assert result == {
        "status": "ok",
        "date": TARGET,
        "report_sha256": hashlib.sha256(weekly_fixture.source.read_bytes()).hexdigest(),
        "dry_run": True,
        "reports_added": 0,
        "articles_added": 0,
        "articles_captured": 0,
        "articles_failed": 0,
        "target_article_count": 1,
        "target_eligible_article_count": 1,
        "target_article_ids": result["target_article_ids"],
        "would_add_reports": 1,
        "would_add_articles": 1,
        "would_capture_article_ids": result["would_capture_article_ids"],
        "would_capture_count": 1,
        "would_promote": True,
        "promotion": "not-needed",
        "reload_required": False,
        "database_sha256_before": hashlib.sha256(before).hexdigest(),
        "database_sha256_after": hashlib.sha256(before).hexdigest(),
        "backup_name": None,
    }
    assert len(result["would_capture_article_ids"]) == 1
    assert weekly_fixture.database.read_bytes() == before
    assert not weekly_fixture.lock_file.exists()
    assert not weekly_fixture.backup_dir.exists()
    assert not list(weekly_fixture.database.parent.glob("*.weekly-candidate"))


def test_expected_dry_run_sha_blocks_changed_identity_before_writes(weekly_fixture):
    before = weekly_fixture.database.read_bytes()
    with pytest.raises(weekly.WeeklyPreflightError, match="expected dry-run identity"):
        weekly.weekly_sync(
            **weekly_fixture.arguments(
                expected_report_sha256="f" * 64,
                dry_run=False,
            )
        )
    assert weekly_fixture.database.read_bytes() == before
    assert not weekly_fixture.lock_file.exists()
    assert not weekly_fixture.backup_dir.exists()


def test_candidate_update_capture_backup_and_promotion_are_atomic_and_idempotent(
    weekly_fixture,
):
    before_sha = hashlib.sha256(weekly_fixture.database.read_bytes()).hexdigest()
    artifact_before = {
        path.relative_to(weekly_fixture.artifact_root): path.read_bytes()
        for path in weekly_fixture.artifact_root.rglob("*")
        if path.is_file()
    }
    ledger_before = {
        path.relative_to(weekly_fixture.ledger_dir): path.read_bytes()
        for path in weekly_fixture.ledger_dir.rglob("*")
        if path.is_file()
    }
    transport = FakeTransport()
    result = weekly.weekly_sync(
        **weekly_fixture.arguments(transport=transport, dry_run=False)
    )

    assert result["status"] == "ok"
    assert result["reports_added"] == 1
    assert result["articles_added"] == 1
    assert result["articles_captured"] == 1
    assert result["articles_failed"] == 0
    assert result["promotion"] == "performed"
    assert result["reload_required"] is True
    assert result["database_sha256_before"] == before_sha
    assert result["database_sha256_after"] != before_sha
    assert transport.calls == 1
    assert _counts(weekly_fixture.database) == (2, 2, 1)
    backups = list(weekly_fixture.backup_dir.iterdir())
    assert len(backups) == 1
    assert hashlib.sha256(backups[0].read_bytes()).hexdigest() == before_sha
    assert not weekly_fixture.lock_file.exists()
    assert artifact_before == {
        path.relative_to(weekly_fixture.artifact_root): path.read_bytes()
        for path in weekly_fixture.artifact_root.rglob("*")
        if path.is_file()
    }
    assert ledger_before == {
        path.relative_to(weekly_fixture.ledger_dir): path.read_bytes()
        for path in weekly_fixture.ledger_dir.rglob("*")
        if path.is_file()
    }
    assert (
        subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=weekly_fixture.repository,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        == ""
    )

    after = weekly_fixture.database.read_bytes()
    repeated = weekly.weekly_sync(**weekly_fixture.arguments())
    assert repeated["status"] == "no-op"
    assert repeated["promotion"] == "not-needed"
    assert repeated["reload_required"] is False
    assert weekly_fixture.database.read_bytes() == after
    assert len(list(weekly_fixture.backup_dir.iterdir())) == 1


def test_dry_and_formal_no_op_still_validate_exact_live_membership(
    weekly_fixture, monkeypatch
):
    weekly.weekly_sync(**weekly_fixture.arguments())
    validated = []
    real_validate = weekly._validate_candidate

    def recording_validate(database, preflight):
        validated.append((database, preflight.target_article_ids))
        return real_validate(database, preflight)

    monkeypatch.setattr(weekly, "_validate_candidate", recording_validate)
    dry = weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))
    formal = weekly.weekly_sync(**weekly_fixture.arguments())

    assert dry["status"] == "no-op"
    assert formal["status"] == "no-op"
    assert validated == [
        (weekly_fixture.database, tuple(dry["target_article_ids"])),
        (weekly_fixture.database, tuple(formal["target_article_ids"])),
    ]


def test_full_monday_sync_reload_and_read_only_api_contract(
    weekly_fixture, monkeypatch
):
    result = weekly.weekly_sync(**weekly_fixture.arguments())
    assert result["status"] == "ok"
    binding = refresh._verify_sha_binding(
        SimpleNamespace(
            date=TARGET,
            source_dir=weekly_fixture.source_dir,
            database=weekly_fixture.database,
            artifact_root=weekly_fixture.artifact_root,
        ),
        result,
    )
    assert binding == {
        "report_sha256": result["report_sha256"],
        "database_sha256": result["database_sha256_after"],
        "target_article_count": 1,
        "target_article_ids": result["target_article_ids"],
    }
    wrong_membership = {**result, "target_article_ids": ["article-wrong"]}
    with pytest.raises(refresh._JobError, match="sha_verification_failed"):
        refresh._verify_sha_binding(
            SimpleNamespace(
                date=TARGET,
                source_dir=weekly_fixture.source_dir,
                database=weekly_fixture.database,
                artifact_root=weekly_fixture.artifact_root,
            ),
            wrong_membership,
        )

    source_bytes = weekly_fixture.source.read_bytes()
    weekly_fixture.source.write_bytes(source_bytes + b"\n")
    with pytest.raises(refresh._JobError, match="sha_verification_failed"):
        refresh._verify_sha_binding(
            SimpleNamespace(
                date=TARGET,
                source_dir=weekly_fixture.source_dir,
                database=weekly_fixture.database,
                artifact_root=weekly_fixture.artifact_root,
            ),
            result,
        )
    weekly_fixture.source.write_bytes(source_bytes)

    empty_metadata = weekly_fixture.repository / "article_metadata"
    empty_metadata.mkdir()
    monkeypatch.setenv("CLIMATE_REGISTRY_DB", str(weekly_fixture.database))
    monkeypatch.setenv(
        "CLIMATE_DELIVERY_OUTPUT_DIR", str(weekly_fixture.artifact_root)
    )
    monkeypatch.setattr(api_server, "SOURCE_DIR", weekly_fixture.source_dir)
    monkeypatch.setattr(api_server, "ARTICLE_METADATA_DIR", empty_metadata)
    monkeypatch.setattr(api_server, "RELOAD_TOKEN", "test-reload-token")
    client = TestClient(app)

    reload_response = client.post(
        "/api/reload", headers={"x-reload-token": "test-reload-token"}
    )
    assert reload_response.status_code == 200
    status = client.get("/api/registry/status")
    assert status.status_code == 200
    assert status.json()["latest_report_date"] == TARGET

    report = client.get(f"/api/registry/reports/{TARGET}")
    assert report.status_code == 200
    report_payload = report.json()
    assert report_payload["report_date"] == TARGET
    assert len(report_payload["articles"]) == 1
    assert report_payload["report_briefing"]["executive_summary"]
    assert report_payload["report_briefing"]["monitoring_snapshot"] is not None
    assert report_payload["report_pdf"] == {
        "filename": f"climate-monitor-{TARGET}.pdf",
        "download_url": f"/api/registry/reports/{TARGET}/pdf",
    }

    article_id = report_payload["articles"][0]["article_id"]
    detail = client.get(f"/api/registry/articles/{article_id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["summary_provenance"] == "content_enrichment"
    assert payload["metadata_provenance"] == {
        "categories": "content_enrichment",
        "keywords": "content_enrichment",
    }
    assert payload["original_url"] == "https://example.com/target"
    assert payload["appearances"][0]["report_date"] == TARGET
    assert payload["appearances"][0]["pillar"] == "A"
    assert payload["latest_fetch"]["fetch_status"] == "success"
    assert payload["enrichment"]["generator"]["name"] == "climate-registry-rules"
    assert payload["source_annotation"] is None

    pdf = client.get(report_payload["report_pdf"]["download_url"])
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF-")


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_latest_publisher_attempt_must_be_success_or_no_change(weekly_fixture, status):
    append_attempt(
        weekly_fixture.ledger_dir,
        {
            **_publisher_attempt(
                hashlib.sha256(weekly_fixture.source.read_bytes()).hexdigest(),
                status=status,
            ),
            "attempt_id": f"20260817t100100z-publisher-{status}",
            "finished_at": "2026-08-17T10:06:00Z",
        },
        repository_root=weekly_fixture.repository,
    )
    before = weekly_fixture.database.read_bytes()
    with pytest.raises(weekly.WeeklyPreflightError, match="publisher attempt"):
        weekly.weekly_sync(**weekly_fixture.arguments())
    assert weekly_fixture.database.read_bytes() == before
    assert not weekly_fixture.backup_dir.exists()


def test_latest_publisher_success_must_include_exact_report_identity(weekly_fixture):
    attempt = _publisher_attempt(
        hashlib.sha256(weekly_fixture.source.read_bytes()).hexdigest()
    )
    attempt.update(
        attempt_id="20260817t100100z-publisher-success-no-report",
        finished_at="2026-08-17T10:06:00Z",
    )
    attempt.pop("report")
    append_attempt(
        weekly_fixture.ledger_dir,
        attempt,
        repository_root=weekly_fixture.repository,
    )

    with pytest.raises(weekly.WeeklyPreflightError, match="report identity"):
        weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))


def test_source_must_be_committed_clean_and_artifact_sha_must_match(weekly_fixture):
    weekly_fixture.source.write_text(
        _report(TARGET, title="Changed", url="https://example.com/target"),
        encoding="utf-8",
    )
    with pytest.raises(weekly.WeeklyPreflightError, match="not clean"):
        weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))

    _run("git", "add", "sources", cwd=weekly_fixture.repository)
    _run("git", "commit", "-qm", "change target", cwd=weekly_fixture.repository)
    updated_attempt = _publisher_attempt(
        hashlib.sha256(weekly_fixture.source.read_bytes()).hexdigest()
    )
    updated_attempt.update(
        attempt_id="20260817t100100z-publisher-success",
        finished_at="2026-08-17T10:06:00Z",
    )
    append_attempt(
        weekly_fixture.ledger_dir,
        updated_attempt,
        repository_root=weekly_fixture.repository,
    )
    with pytest.raises(weekly.WeeklyPreflightError, match="artifact"):
        weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))


def test_missing_committed_target_source_fails_closed(weekly_fixture):
    weekly_fixture.source.unlink()
    _run("git", "add", "sources", cwd=weekly_fixture.repository)
    _run("git", "commit", "-qm", "remove target", cwd=weekly_fixture.repository)
    before = weekly_fixture.database.read_bytes()
    with pytest.raises(weekly.WeeklyPreflightError, match="source is unavailable"):
        weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))
    assert weekly_fixture.database.read_bytes() == before
    assert not weekly_fixture.lock_file.exists()


def test_source_date_and_non_target_update_plan_fail_closed(weekly_fixture):
    with pytest.raises(weekly.WeeklyPreflightError, match="must be a Monday"):
        weekly.weekly_sync(
            **weekly_fixture.arguments(target_date="2026-08-18", dry_run=True)
        )

    extra = weekly_fixture.source_dir / "climate-monitor-2026-08-24.md"
    extra.write_text(
        _report("2026-08-24", title="Extra", url="https://example.com/extra"),
        encoding="utf-8",
    )
    _run("git", "add", "sources", cwd=weekly_fixture.repository)
    _run("git", "commit", "-qm", "extra report", cwd=weekly_fixture.repository)
    with pytest.raises(weekly.WeeklyPreflightError, match="another report date"):
        weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))


def test_database_symlink_sidecars_and_nonstandard_lock_fail_before_writes(
    weekly_fixture,
):
    with pytest.raises(weekly.WeeklyPreflightError, match="coordinate"):
        weekly.weekly_sync(
            **weekly_fixture.arguments(
                lock_file=weekly_fixture.database.parent / "different.lock",
                dry_run=True,
            )
        )

    sidecar = Path(f"{weekly_fixture.database}-wal")
    sidecar.write_bytes(b"active")
    with pytest.raises(weekly.WeeklyPreflightError, match="sidecars"):
        weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))
    sidecar.unlink()

    dangling_sidecar = Path(f"{weekly_fixture.database}-wal")
    try:
        dangling_sidecar.symlink_to(weekly_fixture.database.parent / "missing-wal")
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(weekly.WeeklyPreflightError, match="sidecars"):
        weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))
    dangling_sidecar.unlink()

    linked = weekly_fixture.database.parent / "linked.sqlite3"
    try:
        linked.symlink_to(weekly_fixture.database)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(weekly.WeeklyPreflightError, match="database is unsafe"):
        weekly.weekly_sync(
            **weekly_fixture.arguments(
                database=linked,
                lock_file=linked.with_name(f"{linked.name}.lock"),
                dry_run=True,
            )
        )


def test_existing_standard_lock_uses_registry_lock_error(weekly_fixture):
    weekly_fixture.lock_file.write_text("other", encoding="ascii")
    with pytest.raises(RegistryLockError, match="already locked"):
        weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))
    assert weekly_fixture.lock_file.read_text(encoding="ascii") == "other"


def test_partial_capture_never_promotes_or_creates_live_backup(weekly_fixture):
    before = weekly_fixture.database.read_bytes()
    with pytest.raises(weekly.WeeklyPartialError, match="partial") as raised:
        weekly.weekly_sync(
            **weekly_fixture.arguments(transport=FakeTransport(failure=True))
        )
    result = raised.value.result
    assert result["status"] == "partial"
    assert result["promotion"] == "blocked"
    assert result["reload_required"] is False
    assert result["articles_failed"] == 1
    assert result["capture"]["succeeded_article_ids"] == []
    assert result["capture"]["failures"] == [
        {
            "article_id": result["would_capture_article_ids"][0],
            "status": "failed",
            "error_code": "timeout",
        }
    ]
    assert result["capture"]["skipped_article_ids"] == []
    assert weekly_fixture.database.read_bytes() == before
    assert not weekly_fixture.backup_dir.exists()
    assert not weekly_fixture.lock_file.exists()


def test_upstream_is_revalidated_after_network_before_backup_and_promotion(
    weekly_fixture,
):
    before = weekly_fixture.database.read_bytes()

    class DirtyingTransport(FakeTransport):
        def request(self, target, headers, *, timeout, max_bytes):
            response = super().request(
                target, headers, timeout=timeout, max_bytes=max_bytes
            )
            weekly_fixture.source.write_text(
                _report(
                    TARGET,
                    title="Changed during capture",
                    url="https://example.com/target",
                ),
                encoding="utf-8",
            )
            return response

    with pytest.raises(weekly.WeeklyPreflightError, match="not clean"):
        weekly.weekly_sync(
            **weekly_fixture.arguments(transport=DirtyingTransport())
        )

    assert weekly_fixture.database.read_bytes() == before
    assert not weekly_fixture.backup_dir.exists()
    assert not weekly_fixture.lock_file.exists()


def test_new_failed_publisher_attempt_during_capture_blocks_promotion(
    weekly_fixture,
):
    before = weekly_fixture.database.read_bytes()

    class FailingLedgerTransport(FakeTransport):
        def request(self, target, headers, *, timeout, max_bytes):
            response = super().request(
                target, headers, timeout=timeout, max_bytes=max_bytes
            )
            failed = _publisher_attempt(
                hashlib.sha256(weekly_fixture.source.read_bytes()).hexdigest(),
                status="failed",
            )
            failed.update(
                attempt_id="20260817t100700z-publisher-failed-during-capture",
                finished_at="2026-08-17T10:07:00Z",
            )
            append_attempt(
                weekly_fixture.ledger_dir,
                failed,
                repository_root=weekly_fixture.repository,
            )
            return response

    with pytest.raises(weekly.WeeklyPreflightError, match="publisher attempt"):
        weekly.weekly_sync(
            **weekly_fixture.arguments(transport=FailingLedgerTransport())
        )

    assert weekly_fixture.database.read_bytes() == before
    assert not weekly_fixture.backup_dir.exists()


def test_existing_content_with_failed_enrichment_is_refreshed(weekly_fixture):
    update_registry(
        weekly_fixture.source_dir,
        weekly_fixture.database,
        weekly_fixture.database.parent / "seed-update-backups",
    )
    connection = sqlite3.connect(weekly_fixture.database)
    try:
        article_id = connection.execute(
            """
            SELECT ra.article_id
            FROM report_appearances ra
            JOIN reports r ON r.report_id = ra.report_id
            WHERE r.report_date = ?
            """,
            (TARGET,),
        ).fetchone()[0]
    finally:
        connection.close()
    seeded = capture_enrich_registry(
        weekly_fixture.database,
        weekly_fixture.database.parent / "seed-capture-backups",
        article_ids=(article_id,),
        resolver=lambda _host, _port: ["93.184.216.34"],
        transport=SparseTransport(),
        clock=lambda: "2026-08-17T10:15:00Z",
    )
    assert seeded["status"] == "partial"

    transport = FakeTransport()
    result = weekly.weekly_sync(
        **weekly_fixture.arguments(transport=transport, dry_run=False)
    )

    assert result["status"] == "ok"
    assert result["reports_added"] == 0
    assert result["articles_added"] == 0
    assert result["articles_captured"] == 1
    assert result["promotion"] == "performed"
    assert transport.calls == 1


def test_complete_content_with_a_later_failed_fetch_is_stale(weekly_fixture):
    update_registry(
        weekly_fixture.source_dir,
        weekly_fixture.database,
        weekly_fixture.database.parent / "seed-update-backups",
    )
    connection = sqlite3.connect(weekly_fixture.database)
    try:
        article_id, canonical = connection.execute(
            """
            SELECT a.article_id, a.canonical_url
            FROM report_appearances ra
            JOIN reports r ON r.report_id = ra.report_id
            JOIN articles a ON a.article_id = ra.article_id
            WHERE r.report_date = ?
            """,
            (TARGET,),
        ).fetchone()
    finally:
        connection.close()
    captured = capture_enrich_registry(
        weekly_fixture.database,
        weekly_fixture.database.parent / "seed-capture-backups",
        article_ids=(article_id,),
        resolver=lambda _host, _port: ["93.184.216.34"],
        transport=FakeTransport(),
        clock=lambda: "2026-08-17T10:15:00Z",
    )
    assert captured["status"] == "updated"

    connection = sqlite3.connect(weekly_fixture.database)
    with connection:
        connection.execute(
            """
            INSERT INTO article_fetches(
                fetch_id, article_id, requested_url, fetched_at, fetch_status,
                error_code, error_message
            ) VALUES (
                'fetch-later-failure', ?, ?, '2026-08-17T10:20:00Z',
                'failed', 'timeout', 'bounded request timed out'
            )
            """,
            (article_id, canonical),
        )
    connection.close()

    dry_run = weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))
    assert dry_run["would_capture_article_ids"] == [article_id]

    transport = FakeTransport()
    result = weekly.weekly_sync(**weekly_fixture.arguments(transport=transport))
    assert result["articles_captured"] == 1
    assert result["promotion"] == "performed"
    assert transport.calls == 1


def test_candidate_validation_failure_never_reaches_backup_or_live(
    weekly_fixture, monkeypatch
):
    before = weekly_fixture.database.read_bytes()

    def reject(_candidate, _preflight):
        raise weekly.WeeklyValidationError("injected candidate validation failure")

    monkeypatch.setattr(weekly, "_validate_candidate", reject)
    with pytest.raises(weekly.WeeklyValidationError, match="injected"):
        weekly.weekly_sync(**weekly_fixture.arguments())
    assert weekly_fixture.database.read_bytes() == before
    assert not weekly_fixture.backup_dir.exists()


def test_backup_and_promotion_failure_keep_live_database_unchanged(
    weekly_fixture, monkeypatch
):
    before = weekly_fixture.database.read_bytes()
    real_backup = weekly._create_exact_backup

    def fail_backup(_source, _destination):
        raise OSError("injected backup failure")

    monkeypatch.setattr(weekly, "_create_exact_backup", fail_backup)
    with pytest.raises(weekly.WeeklyValidationError, match="backup"):
        weekly.weekly_sync(**weekly_fixture.arguments())
    assert weekly_fixture.database.read_bytes() == before

    monkeypatch.setattr(weekly, "_create_exact_backup", real_backup)

    def fail_replace(_source, _destination):
        raise OSError("injected promotion failure")

    monkeypatch.setattr(weekly, "_atomic_replace", fail_replace)
    with pytest.raises(weekly.WeeklyValidationError, match="promotion"):
        weekly.weekly_sync(**weekly_fixture.arguments())
    assert weekly_fixture.database.read_bytes() == before
    assert len(list(weekly_fixture.backup_dir.glob("*.bak"))) == 1


def test_upstream_is_revalidated_again_after_backup_before_promotion(
    weekly_fixture, monkeypatch
):
    before = weekly_fixture.database.read_bytes()
    real_backup = weekly._create_exact_backup

    def backup_then_dirty(source, destination):
        real_backup(source, destination)
        weekly_fixture.source.write_text(
            _report(
                TARGET,
                title="Changed after backup",
                url="https://example.com/target",
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(weekly, "_create_exact_backup", backup_then_dirty)
    with pytest.raises(weekly.WeeklyPreflightError, match="not clean"):
        weekly.weekly_sync(**weekly_fixture.arguments())

    assert weekly_fixture.database.read_bytes() == before
    assert len(list(weekly_fixture.backup_dir.glob("*.bak"))) == 1


def test_post_replace_failure_restores_exact_live_database(weekly_fixture, monkeypatch):
    if os.name == "posix":
        weekly_fixture.database.chmod(0o640)
    before_metadata = weekly_fixture.database.stat()
    before_mode = stat.S_IMODE(before_metadata.st_mode)
    before_owner = (
        getattr(before_metadata, "st_uid", None),
        getattr(before_metadata, "st_gid", None),
    )
    before = weekly_fixture.database.read_bytes()
    failed_once = False

    def fail_first_live_fsync(path):
        nonlocal failed_once
        if path == weekly_fixture.database and not failed_once:
            failed_once = True
            raise OSError("injected post-replace failure")

    monkeypatch.setattr(weekly, "_fsync_parent", fail_first_live_fsync)
    with pytest.raises(weekly.WeeklyValidationError, match="promotion"):
        weekly.weekly_sync(**weekly_fixture.arguments())

    assert failed_once is True
    assert weekly_fixture.database.read_bytes() == before
    assert stat.S_IMODE(weekly_fixture.database.stat().st_mode) == before_mode
    if os.name == "posix":
        restored_metadata = weekly_fixture.database.stat()
        assert (restored_metadata.st_uid, restored_metadata.st_gid) == before_owner
    backups = list(weekly_fixture.backup_dir.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before


def test_successful_sync_backup_has_supported_exact_restore(weekly_fixture):
    original = weekly_fixture.database.read_bytes()
    original_sha = hashlib.sha256(original).hexdigest()
    sync_result = weekly.weekly_sync(**weekly_fixture.arguments())
    promoted = weekly_fixture.database.read_bytes()
    promoted_sha = hashlib.sha256(promoted).hexdigest()
    promoted_metadata = weekly_fixture.database.stat()
    promoted_mode = stat.S_IMODE(promoted_metadata.st_mode)
    promoted_owner = (
        getattr(promoted_metadata, "st_uid", None),
        getattr(promoted_metadata, "st_gid", None),
    )
    selected_backup = weekly_fixture.backup_dir / sync_result["backup_name"]
    assert selected_backup.read_bytes() == original

    restore_backups = weekly_fixture.database.parent / "restore-backups"
    restored = weekly.restore_registry_backup(
        database=weekly_fixture.database,
        backup=selected_backup,
        expected_sha256=sync_result["database_sha256_before"],
        backup_dir=restore_backups,
        lock_file=weekly_fixture.lock_file,
        clock=lambda: NOW,
    )

    assert restored["status"] == "ok"
    assert restored["promotion"] == "performed"
    assert restored["reload_required"] is True
    assert restored["restored_database_sha256"] == original_sha
    assert restored["replaced_database_sha256"] == promoted_sha
    assert weekly_fixture.database.read_bytes() == original
    assert stat.S_IMODE(weekly_fixture.database.stat().st_mode) == promoted_mode
    if os.name == "posix":
        restored_metadata = weekly_fixture.database.stat()
        assert (restored_metadata.st_uid, restored_metadata.st_gid) == promoted_owner
    rollback = restore_backups / restored["rollback_backup_name"]
    assert rollback.read_bytes() == promoted

    repeated = weekly.restore_registry_backup(
        database=weekly_fixture.database,
        backup=selected_backup,
        expected_sha256=original_sha,
        backup_dir=restore_backups,
        lock_file=weekly_fixture.lock_file,
        clock=lambda: NOW,
    )
    assert repeated["status"] == "no-op"
    assert repeated["reload_required"] is False
    assert len(list(restore_backups.glob("*.bak"))) == 1


def test_errors_and_results_do_not_disclose_paths_urls_or_delivery_state(
    weekly_fixture,
):
    result = weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))
    serialized = json.dumps(result, sort_keys=True)
    assert str(weekly_fixture.database) not in serialized
    assert "https://" not in serialized
    assert "recipient" not in serialized
    assert "delivery" not in serialized

    shutil.rmtree(weekly_fixture.artifact_root)
    with pytest.raises(weekly.WeeklyPreflightError) as error:
        weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))
    message = str(error.value)
    assert str(weekly_fixture.artifact_root) not in message
    assert "https://" not in message
    assert "recipient" not in message
