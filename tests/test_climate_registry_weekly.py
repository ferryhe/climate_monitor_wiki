from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api_server
import climate_registry.weekly as weekly
import climate_monitor.ledger_repair as ledger_repair
import scripts.weekly_registry_refresh as refresh
from api_server import app
from climate_delivery.artifacts import ARTIFACT_ONLY_DELIVERY_STATUS
from climate_delivery.io import atomic_write_json
from climate_delivery.pdf import render_pdf
from climate_delivery.pipeline import _manifest
from climate_delivery.report import parse_weekly_report
from climate_delivery.summary import build_summary, write_summary
from climate_monitor.run_ledger import (
    LedgerContractError,
    RunLedgerReader,
    append_attempt,
    remove_attempt_repair,
)
from climate_monitor.ledger_repair import publisher_lock, repair_publisher_ledger
from climate_registry.audit import build_audit_registry
from climate_registry.capture import capture_enrich_registry
from climate_registry.errors import RegistryLockError
from climate_registry.fetch import FetchFailure, FetchResponse
from climate_registry.persistent import update_registry


TARGET = "2026-08-17"
PREVIOUS = "2026-08-10"
NOW = datetime(2026, 8, 17, 11, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
REPAIR_SCRIPT = ROOT / "scripts" / "repair_publisher_ledger.py"


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


class ForbiddenTransport:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, target, headers, *, timeout, max_bytes):
        self.calls += 1
        return FetchResponse(
            403,
            target.url,
            {"content-type": "text/html; charset=utf-8"},
            b"<html><body>Publisher bot wall.</body></html>",
        )


def _write_target_annotation(directory: Path) -> None:
    directory.mkdir()
    (directory / "articles-001-001.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "annotation_method": "subagent-original-content-v1",
                "source_scope": "linked-original-content-with-report-fallback",
                "generated_on": TARGET,
                "articles": [
                    {
                        "canonical_url": "https://example.com/target",
                        "source_url": "https://example.com/target",
                        "title": "Target",
                        "source_basis": "publisher_excerpt",
                        "summary": "Climate risk and insurance regulation changed this week.",
                        "categories": ["Climate Risk"],
                        "keywords": ["climate", "insurance", "regulation"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_annotations(directory: Path, urls: list[str]) -> None:
    directory.mkdir(exist_ok=True)
    (directory / "articles-001-025.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "annotation_method": "subagent-original-content-v1",
                "source_scope": "linked-original-content-with-report-fallback",
                "generated_on": TARGET,
                "articles": [
                    {
                        "canonical_url": url,
                        "source_url": url,
                        "title": f"Target {index}",
                        "source_basis": "publisher_excerpt",
                        "summary": f"Validated publisher fallback for article {index}.",
                        "categories": ["Climate Risk"],
                        "keywords": ["climate", "insurance", f"risk-{index}"],
                    }
                    for index, url in enumerate(urls, start=1)
                ],
            }
        ),
        encoding="utf-8",
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
    _run("git", "config", "core.autocrlf", "false", cwd=repository)
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


def test_legacy_publisher_repair_dry_apply_and_idempotent(
    weekly_fixture, tmp_path, monkeypatch
):
    ledger = tmp_path / "legacy-ledger"
    legacy = _publisher_attempt(
        hashlib.sha256(weekly_fixture.source.read_bytes()).hexdigest()
    )
    legacy.pop("report")
    append_attempt(ledger, legacy, repository_root=weekly_fixture.repository)
    original = next((ledger / "attempts" / "publisher" / TARGET).glob("*.json"))
    claim = ledger / ".attempt-identities" / f"{legacy['attempt_id']}.claim"
    original_raw = original.read_bytes()
    original_mode = stat.S_IMODE(os.stat(original).st_mode)
    original_inode = (os.stat(original).st_dev, os.stat(original).st_ino)
    claim_inode = (os.stat(claim).st_dev, os.stat(claim).st_ino)
    original_owner = (os.stat(original).st_uid, os.stat(original).st_gid)
    claim_owner = (os.stat(claim).st_uid, os.stat(claim).st_gid)
    publisher_lock = tmp_path / "publisher.lock"
    publisher_lock.write_bytes(b"publisher-lock\n")
    lock_raw = publisher_lock.read_bytes()
    monkeypatch.setenv("CLIMATE_PUBLISH_LOCK", str(publisher_lock))
    arguments = {
        "target_date": TARGET,
        "source_dir": weekly_fixture.source_dir,
        "database": weekly_fixture.database,
        "artifact_root": weekly_fixture.artifact_root,
        "ledger_dir": ledger,
        "lock_file": publisher_lock,
    }

    dry = repair_publisher_ledger(**arguments, apply=False)
    assert dry["status"] == "would_repair"
    assert publisher_lock.read_bytes() == lock_raw
    assert not (ledger / ".attempt-repairs").exists()
    applied = repair_publisher_ledger(**arguments, apply=True)
    assert applied["status"] == "repaired"
    assert repair_publisher_ledger(**arguments, apply=True)["status"] == "already_valid"
    assert original.read_bytes() == claim.read_bytes() == original_raw
    assert stat.S_IMODE(os.stat(original).st_mode) == original_mode
    assert (os.stat(original).st_dev, os.stat(original).st_ino) == original_inode
    assert (os.stat(claim).st_dev, os.stat(claim).st_ino) == claim_inode
    assert (os.stat(original).st_uid, os.stat(original).st_gid) == original_owner
    assert (os.stat(claim).st_uid, os.stat(claim).st_gid) == claim_owner
    assert os.stat(original).st_nlink == os.stat(claim).st_nlink == 2

    result = weekly.weekly_sync(
        **weekly_fixture.arguments(publisher_ledger_dir=ledger, dry_run=True)
    )
    assert result["status"] == "ok"

    overlay = next((ledger / ".attempt-repairs").rglob("*.json"))
    overlay_payload = json.loads(overlay.read_text(encoding="ascii"))
    remove_attempt_repair(
        ledger,
        overlay_payload,
        repository_root=weekly_fixture.repository,
    )
    assert not overlay.exists()
    assert original.read_bytes() == claim.read_bytes() == original_raw
    restored = RunLedgerReader(
        ledger, repository_root=weekly_fixture.repository
    )._load()
    assert "report" not in restored[-1]


def _legacy_cli_arguments(weekly_fixture, tmp_path):
    ledger = tmp_path / "cli-ledger"
    legacy = _publisher_attempt(
        hashlib.sha256(weekly_fixture.source.read_bytes()).hexdigest()
    )
    legacy.pop("report")
    append_attempt(ledger, legacy, repository_root=weekly_fixture.repository)
    lock_file = tmp_path / "publisher.lock"
    lock_file.write_bytes(b"publisher-lock\n")
    command = [
        sys.executable,
        str(REPAIR_SCRIPT),
        "--date",
        TARGET,
        "--ledger-dir",
        str(ledger),
        "--source-dir",
        str(weekly_fixture.source_dir),
        "--registry-database",
        str(weekly_fixture.database),
        "--artifact-root",
        str(weekly_fixture.artifact_root),
        "--lock-file",
        str(lock_file),
    ]
    environment = {**os.environ, "CLIMATE_PUBLISH_LOCK": str(lock_file)}
    return ledger, lock_file, command, environment


def _run_repair_cli(command, environment):
    return subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_repair_cli_help_and_exact_date_surface(tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPAIR_SCRIPT), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--date" in result.stdout
    assert "--apply" in result.stdout
    assert "dry-run" in result.stdout
    assert "--all" not in result.stdout
    assert "--latest" not in result.stdout
    assert "--sha" not in result.stdout


def test_repair_cli_dry_apply_and_repeat_statuses(weekly_fixture, tmp_path):
    ledger, lock_file, command, environment = _legacy_cli_arguments(
        weekly_fixture, tmp_path
    )
    lock_raw = lock_file.read_bytes()

    dry = _run_repair_cli(command, environment)
    assert dry.returncode == 0
    dry_payload = json.loads(dry.stdout)
    assert dry_payload["status"] == "would_repair"
    expected_sha = hashlib.sha256(weekly_fixture.source.read_bytes()).hexdigest()
    assert dry_payload["source_report_sha256"] == expected_sha
    assert dry_payload["artifact_report_sha256"] == expected_sha
    assert dry_payload["registry_report_sha256"] is None
    assert Path(dry_payload["artifact_directory"]).name == expected_sha
    assert lock_file.read_bytes() == lock_raw
    assert not (ledger / ".attempt-repairs").exists()
    assert not (ledger / ".attempt-repair-tmp").exists()

    applied = _run_repair_cli([*command, "--apply"], environment)
    assert applied.returncode == 0
    assert json.loads(applied.stdout)["status"] == "repaired"
    repeated = _run_repair_cli([*command, "--apply"], environment)
    assert repeated.returncode == 0
    assert json.loads(repeated.stdout)["status"] == "already_valid"
    assert len(list((ledger / ".attempt-repairs").rglob("*.json"))) == 1
    assert not list((ledger / ".attempt-repair-tmp").glob("*.tmp"))


def test_repair_cli_stable_failure_exits(weekly_fixture, tmp_path, monkeypatch):
    _ledger, lock_file, command, environment = _legacy_cli_arguments(
        weekly_fixture, tmp_path
    )
    monkeypatch.setenv("CLIMATE_PUBLISH_LOCK", str(lock_file))
    offcycle = command.copy()
    offcycle[offcycle.index(TARGET)] = "2026-08-18"
    preflight = _run_repair_cli(offcycle, environment)
    assert preflight.returncode == 7
    assert json.loads(preflight.stdout)["status"] == "preflight_failed"

    manifest_path = next(weekly_fixture.artifact_root.rglob("manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["report"]["sha256"] = "c" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    invalid = _run_repair_cli(command, environment)
    assert invalid.returncode == 8
    assert json.loads(invalid.stdout)["status"] == "validation_failed"

    manifest["report"]["sha256"] = hashlib.sha256(
        weekly_fixture.source.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with publisher_lock(lock_file):
        conflict = _run_repair_cli(command, environment)
    assert conflict.returncode == 4
    assert json.loads(conflict.stdout)["status"] == "lock_conflict"


def test_repair_cli_rejects_missing_or_mistyped_publisher_lock(
    weekly_fixture, tmp_path
):
    _ledger, lock_file, command, environment = _legacy_cli_arguments(
        weekly_fixture, tmp_path
    )
    lock_file.unlink()
    missing = _run_repair_cli(command, environment)
    assert missing.returncode == 7
    assert json.loads(missing.stdout)["status"] == "preflight_failed"
    assert not lock_file.exists()
    missing_apply = _run_repair_cli([*command, "--apply"], environment)
    assert missing_apply.returncode == 7
    assert not lock_file.exists()

    configured = tmp_path / "configured.lock"
    configured.write_bytes(b"configured\n")
    supplied = tmp_path / "mistyped.lock"
    supplied.write_bytes(b"mistyped\n")
    environment["CLIMATE_PUBLISH_LOCK"] = str(configured)
    mistyped = command.copy()
    mistyped[mistyped.index(str(lock_file))] = str(supplied)
    wrong = _run_repair_cli([*mistyped, "--apply"], environment)
    assert wrong.returncode == 7
    assert supplied.read_bytes() == b"mistyped\n"


@pytest.mark.skipif(os.name != "posix", reason="util-linux flock is POSIX-only")
def test_repair_cli_interoperates_with_wrapper_flock(weekly_fixture, tmp_path):
    flock_binary = shutil.which("flock")
    if flock_binary is None:
        pytest.skip("util-linux flock is unavailable")
    _ledger, lock_file, command, environment = _legacy_cli_arguments(
        weekly_fixture, tmp_path
    )
    holder = subprocess.Popen(
        [
            flock_binary,
            "-n",
            str(lock_file),
            sys.executable,
            "-c",
            "import time; print('locked', flush=True); time.sleep(30)",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        result = _run_repair_cli(command, environment)
        assert result.returncode == 4
        assert json.loads(result.stdout)["status"] == "lock_conflict"
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_repair_does_not_depend_on_delivery_or_mail_configuration():
    source = Path(ledger_repair.__file__).read_text(encoding="utf-8")
    assert "climate_delivery.email" not in source
    assert "smtplib" not in source
    assert "recipient" not in source.lower()
    assert "delivery_state" not in source


def _library_repair_arguments(weekly_fixture, tmp_path, monkeypatch):
    ledger, lock_file, _command, _environment = _legacy_cli_arguments(
        weekly_fixture, tmp_path
    )
    monkeypatch.setenv("CLIMATE_PUBLISH_LOCK", str(lock_file))
    return ledger, {
        "target_date": TARGET,
        "source_dir": weekly_fixture.source_dir,
        "database": weekly_fixture.database,
        "artifact_root": weekly_fixture.artifact_root,
        "ledger_dir": ledger,
        "lock_file": lock_file,
    }


def test_repair_rejects_registry_identity_mismatch(
    weekly_fixture, tmp_path, monkeypatch
):
    ledger, arguments = _library_repair_arguments(
        weekly_fixture, tmp_path, monkeypatch
    )
    update_registry(
        weekly_fixture.source_dir,
        weekly_fixture.database,
        tmp_path / "registry-update-backups",
    )
    connection = sqlite3.connect(weekly_fixture.database)
    with connection:
        connection.execute(
            "UPDATE reports SET report_sha256 = ? WHERE report_date = ?",
            ("d" * 64, TARGET),
        )
    connection.close()

    with pytest.raises(ledger_repair.RepairValidationError, match="Registry"):
        repair_publisher_ledger(**arguments, apply=False)
    assert not (ledger / ".attempt-repairs").exists()


def test_repair_rejects_source_race_against_head(
    weekly_fixture, tmp_path, monkeypatch
):
    ledger, arguments = _library_repair_arguments(
        weekly_fixture, tmp_path, monkeypatch
    )
    real_git_text = ledger_repair._git_text
    raced = False

    def race_after_clean(repository_root, *args):
        nonlocal raced
        result = real_git_text(repository_root, *args)
        if not raced and args[:2] == ("status", "--porcelain=v1"):
            raced = True
            raw = weekly_fixture.source.read_bytes()
            weekly_fixture.source.write_bytes(raw.replace(b"Target", b"Raced!", 1))
        return result

    monkeypatch.setattr(ledger_repair, "_git_text", race_after_clean)
    with pytest.raises(
        ledger_repair.RepairPreflightError, match="does not match HEAD raw bytes"
    ):
        repair_publisher_ledger(**arguments, apply=False)
    assert not (ledger / ".attempt-repairs").exists()


def test_repair_rejects_symlinked_artifact_root(
    weekly_fixture, tmp_path, monkeypatch
):
    ledger, arguments = _library_repair_arguments(
        weekly_fixture, tmp_path, monkeypatch
    )
    linked = tmp_path / "linked-artifacts"
    try:
        linked.symlink_to(weekly_fixture.artifact_root, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    arguments["artifact_root"] = linked

    with pytest.raises(ledger_repair.RepairPreflightError, match="canonical"):
        repair_publisher_ledger(**arguments, apply=False)
    assert not (ledger / ".attempt-repairs").exists()


def test_repair_rejects_extra_hardlink_to_private_attempt(
    weekly_fixture, tmp_path, monkeypatch
):
    ledger, arguments = _library_repair_arguments(
        weekly_fixture, tmp_path, monkeypatch
    )
    original = next((ledger / "attempts" / "publisher" / TARGET).glob("*.json"))
    extra = tmp_path / "extra-attempt-link"
    try:
        os.link(original, extra)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(
        ledger_repair.RepairValidationError, match="link topology"
    ):
        repair_publisher_ledger(**arguments, apply=False)
    assert not (ledger / ".attempt-repairs").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_repair_rejects_group_writable_private_attempt(
    weekly_fixture, tmp_path, monkeypatch
):
    ledger, arguments = _library_repair_arguments(
        weekly_fixture, tmp_path, monkeypatch
    )
    original = next((ledger / "attempts" / "publisher" / TARGET).glob("*.json"))
    os.chmod(original, 0o660)

    with pytest.raises(
        ledger_repair.RepairValidationError, match="permissions are unsafe"
    ):
        repair_publisher_ledger(**arguments, apply=False)
    assert not (ledger / ".attempt-repairs").exists()


def test_repair_rolls_back_overlay_when_post_commit_validation_fails(
    weekly_fixture, tmp_path, monkeypatch
):
    ledger, arguments = _library_repair_arguments(
        weekly_fixture, tmp_path, monkeypatch
    )
    original = next((ledger / "attempts" / "publisher" / TARGET).glob("*.json"))
    original_raw = original.read_bytes()
    real_load = RunLedgerReader._load
    calls = 0

    def fail_third_load(reader):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise LedgerContractError("injected post-commit validation failure")
        return real_load(reader)

    monkeypatch.setattr(RunLedgerReader, "_load", fail_third_load)
    with pytest.raises(ledger_repair.RepairValidationError, match="repair failed"):
        repair_publisher_ledger(**arguments, apply=True)
    assert original.read_bytes() == original_raw
    assert not list((ledger / ".attempt-repairs").rglob("*.json"))


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
        "articles_fallback": 0,
        "fallback_article_ids": [],
        "articles_unresolved": 0,
        "fallback_failure_classes": {},
        "promotion_with_fallback": False,
        "coverage_status": "ok",
        "target_article_count": 1,
        "target_eligible_article_count": 1,
        "target_article_ids": result["target_article_ids"],
        "would_add_reports": 1,
        "would_add_articles": 1,
        "would_capture_article_ids": result["would_capture_article_ids"],
        "would_capture_count": 1,
        "would_fallback_count": 0,
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


def test_live_v3_remains_readable_until_candidate_v4_promotion(weekly_fixture):
    connection = sqlite3.connect(weekly_fixture.database)
    with connection:
        connection.execute("DROP TRIGGER article_capture_resolutions_validate_insert")
        connection.execute("DROP TRIGGER article_capture_resolutions_are_append_only_update")
        connection.execute("DROP TRIGGER article_capture_resolutions_are_append_only_delete")
        connection.execute("DROP TABLE article_capture_resolutions")
        connection.execute("DELETE FROM schema_migrations WHERE version = 4")
        connection.execute("PRAGMA user_version = 3")
    connection.close()
    assert api_server.RegistryReader(
        weekly_fixture.database, repository_root=weekly_fixture.repository
    ).status()["schema_version"] == 3

    result = weekly.weekly_sync(**weekly_fixture.arguments())
    assert result["promotion"] == "performed"
    assert api_server.RegistryReader(
        weekly_fixture.database, repository_root=weekly_fixture.repository
    ).status()["schema_version"] == 4
    backup = weekly_fixture.backup_dir / result["backup_name"]
    assert api_server.RegistryReader(
        backup, repository_root=weekly_fixture.repository
    ).status()["schema_version"] == 3


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
    assert weekly_fixture.lock_file.is_file()
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


def test_weekly_sync_loads_annotation_catalog_once(weekly_fixture, monkeypatch):
    real_loader = weekly.load_article_annotations_catalog
    calls = 0

    def counted_loader(path):
        nonlocal calls
        calls += 1
        return real_loader(path)

    monkeypatch.setattr(weekly, "load_article_annotations_catalog", counted_loader)
    result = weekly.weekly_sync(
        **weekly_fixture.arguments(transport=FakeTransport())
    )
    assert result["status"] == "ok"
    assert calls == 1


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


def test_sha_binding_rejects_noncanonical_registry_filename_before_read(
    weekly_fixture, monkeypatch
):
    result = weekly.weekly_sync(**weekly_fixture.arguments())
    connection = sqlite3.connect(weekly_fixture.database)
    with connection:
        connection.execute(
            "UPDATE reports SET filename = '../outside.md' WHERE report_date = ?",
            (TARGET,),
        )
    connection.close()
    altered_result = {
        **result,
        "database_sha256_after": refresh._stream_sha256(weekly_fixture.database),
    }

    def unexpected_read(_path):
        pytest.fail("noncanonical Registry filename reached the source reader")

    monkeypatch.setattr(refresh, "parse_historical_report", unexpected_read)
    with pytest.raises(refresh._JobError, match="sha_verification_failed"):
        refresh._verify_sha_binding(
            SimpleNamespace(
                date=TARGET,
                source_dir=weekly_fixture.source_dir,
                database=weekly_fixture.database,
                artifact_root=weekly_fixture.artifact_root,
            ),
            altered_result,
        )


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


@pytest.mark.parametrize(
    ("case", "expected_message"),
    [
        ("wrong_report_id", "report identity"),
        ("wrong_date", "ledger is invalid"),
        ("wrong_sha", "report identity"),
        ("malformed_sha", "ledger is invalid"),
    ],
)
def test_publisher_identity_mismatch_fails_before_network_or_writes(
    weekly_fixture, case, expected_message
):
    attempt = _publisher_attempt(
        hashlib.sha256(weekly_fixture.source.read_bytes()).hexdigest()
    )
    attempt.update(
        attempt_id=f"20260817t100100z-publisher-{case}",
        finished_at="2026-08-17T10:06:00Z",
    )
    if case == "wrong_report_id":
        attempt["report"]["report_id"] = f"other-{TARGET}"
    elif case == "wrong_date":
        attempt["report"]["report_date"] = PREVIOUS
    elif case == "wrong_sha":
        attempt["report"]["sha256"] = "b" * 64
    else:
        attempt["report"]["sha256"] = "NOT-A-SHA"
    destination = (
        weekly_fixture.ledger_dir
        / "attempts"
        / "publisher"
        / TARGET
        / f"{attempt['attempt_id']}.json"
    )
    destination.write_text(
        json.dumps(attempt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )

    class UnexpectedTransport:
        def request(self, *_args, **_kwargs):
            pytest.fail("network capture started before identity preflight")

    def unexpected_resolver(*_args):
        pytest.fail("DNS resolution started before identity preflight")

    before = weekly_fixture.database.read_bytes()
    with pytest.raises(weekly.WeeklyPreflightError, match=expected_message):
        weekly.weekly_sync(
            **weekly_fixture.arguments(
                dry_run=False,
                transport=UnexpectedTransport(),
                resolver=unexpected_resolver,
            )
        )
    assert weekly_fixture.database.read_bytes() == before
    assert not weekly_fixture.backup_dir.exists()
    assert not weekly_fixture.lock_file.exists()
    assert not list(weekly_fixture.database.parent.glob("*.weekly-candidate"))


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


def test_artifact_root_inside_checkout_fails_closed(weekly_fixture):
    (weekly_fixture.repository / ".git" / "info" / "exclude").write_text(
        "ignored-artifacts/\n", encoding="utf-8"
    )
    checkout_artifacts = weekly_fixture.repository / "ignored-artifacts"
    shutil.copytree(weekly_fixture.artifact_root, checkout_artifacts)

    with pytest.raises(
        weekly.WeeklyPreflightError,
        match="artifact root must be outside the repository",
    ):
        weekly.weekly_sync(
            **weekly_fixture.arguments(
                artifact_root=checkout_artifacts,
                dry_run=True,
            )
        )


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


def test_stale_standard_lock_file_does_not_permanently_block(weekly_fixture):
    weekly_fixture.lock_file.write_text("other", encoding="ascii")
    result = weekly.weekly_sync(**weekly_fixture.arguments(dry_run=True))
    assert result["status"] == "ok"
    assert weekly_fixture.lock_file.read_text(encoding="ascii").strip().isdigit()


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
    assert weekly_fixture.lock_file.is_file()


def test_enrichment_failure_counts_as_legacy_failed_and_unresolved(
    weekly_fixture, monkeypatch
):
    def enrichment_failed(_database, _backup_dir, *, article_ids, **_kwargs):
        return {
            "status": "partial",
            "selected": 1,
            "counts": {"enrichment_failed": 1},
            "articles": [
                {
                    "article_id": article_ids[0],
                    "status": "enrichment_failed",
                    "error_code": "enrichment_invalid",
                }
            ],
        }

    monkeypatch.setattr(weekly, "capture_enrich_registry", enrichment_failed)
    with pytest.raises(weekly.WeeklyPartialError) as raised:
        weekly.weekly_sync(**weekly_fixture.arguments())
    result = raised.value.result
    assert result["articles_failed"] == 1
    assert result["articles_fallback"] == 0
    assert result["fallback_article_ids"] == []
    assert result["articles_unresolved"] == 1
    assert result["coverage_status"] == "blocked_unresolved"


def test_exact_403_uses_whole_annotation_bundle_and_then_is_no_op(weekly_fixture):
    metadata_dir = weekly_fixture.repository / "article_metadata"
    _write_target_annotation(metadata_dir)
    _run("git", "add", "article_metadata", cwd=weekly_fixture.repository)
    _run("git", "commit", "-qm", "validated article metadata", cwd=weekly_fixture.repository)
    transport = ForbiddenTransport()

    result = weekly.weekly_sync(
        **weekly_fixture.arguments(transport=transport, metadata_dir=metadata_dir)
    )

    assert result["status"] == "ok"
    assert result["coverage_status"] == "partial_with_validated_fallback"
    assert result["articles_captured"] == 0
    assert result["articles_failed"] == 1
    assert result["articles_fallback"] == 1
    assert result["fallback_article_ids"] == result["would_capture_article_ids"]
    assert result["articles_unresolved"] == 0
    assert result["fallback_failure_classes"] == {
        "http_403_publisher_bot_wall": 1
    }
    assert result["promotion_with_fallback"] is True
    connection = sqlite3.connect(weekly_fixture.database)
    connection.row_factory = sqlite3.Row
    resolution = connection.execute(
        "SELECT * FROM article_capture_resolutions"
    ).fetchone()
    latest = connection.execute(
        "SELECT * FROM article_fetches ORDER BY fetched_at DESC, fetch_id DESC LIMIT 1"
    ).fetchone()
    assert resolution["fetch_id"] == latest["fetch_id"]
    assert resolution["failure_class"] == "http_403_publisher_bot_wall"
    assert resolution["http_status"] == 403
    assert resolution["attempt_at"] == latest["fetched_at"]
    assert resolution["fallback_source"] == "json_annotation"
    assert resolution["fallback_provenance"] == "publisher_excerpt_annotation"
    assert resolution["bundle_sha256"] == hashlib.sha256(
        resolution["bundle_json"].encode("utf-8")
    ).hexdigest()
    bundle = json.loads(resolution["bundle_json"])
    assert bundle["canonical_url"] == "https://example.com/target"
    assert bundle["summary"] and bundle["categories"] and bundle["keywords"]
    assert bundle["provenance"] == "publisher_excerpt_annotation"
    assert bundle["identity"] == {
        "annotation_method": "subagent-original-content-v1",
        "generated_on": TARGET,
        "schema_version": 1,
        "source_basis": "publisher_excerpt",
        "source_scope": "linked-original-content-with-report-fallback",
        "source_url": "https://example.com/target",
        "title": "Target",
    }
    assert resolution["validated_at"].endswith("Z")
    assert latest["fetch_status"] == "failed"
    assert latest["error_code"] == "http_error"
    assert latest["http_status"] == 403
    assert latest["content_version_id"] is None
    assert connection.execute("SELECT COUNT(*) FROM article_content_versions").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM article_enrichments").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "UPDATE article_capture_resolutions SET validated_at = 'changed'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="identity already exists"):
        connection.execute(
            """
            INSERT OR REPLACE INTO article_capture_resolutions
            SELECT * FROM article_capture_resolutions WHERE resolution_id = ?
            """,
            (resolution["resolution_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError, match="identity already exists"):
        connection.execute(
            """
            INSERT OR REPLACE INTO article_capture_resolutions
            SELECT ?, report_id, report_date, report_sha256, article_id,
                   canonical_url, fetch_id, failure_class, http_status,
                   attempt_at, fallback_source, fallback_provenance,
                   bundle_json, bundle_sha256, validated_at
            FROM article_capture_resolutions WHERE resolution_id = ?
            """,
            ("resolution-" + "e" * 64, resolution["resolution_id"]),
        )
    connection.close()

    class NoNetwork:
        def request(self, *args, **kwargs):
            raise AssertionError("no-op must not use the network")

    before = weekly_fixture.database.read_bytes()
    dry = weekly.weekly_sync(
        **weekly_fixture.arguments(
            transport=NoNetwork(), metadata_dir=metadata_dir, dry_run=True
        )
    )
    assert dry["status"] == "no-op"
    assert dry["would_capture_count"] == 0
    assert dry["would_promote"] is False
    repeated = weekly.weekly_sync(
        **weekly_fixture.arguments(transport=NoNetwork(), metadata_dir=metadata_dir)
    )
    assert repeated["status"] == "no-op"
    assert repeated["would_capture_count"] == 0
    assert repeated["would_promote"] is False
    assert weekly_fixture.database.read_bytes() == before

    connection = sqlite3.connect(weekly_fixture.database)
    article_id = result["fallback_article_ids"][0]
    later_at = (NOW + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    connection.execute(
        """
        INSERT INTO article_fetches(
            fetch_id, article_id, requested_url, final_url, fetched_at,
            fetch_status, http_status, error_code, error_message
        ) VALUES ('fetch-later-same-bundle', ?, 'https://example.com/target',
                  'https://redirect.example/target', ?, 'failed', 403,
                  'http_error', 'publisher bot wall')
        """,
        (article_id, later_at),
    )
    connection.commit()
    connection.close()
    same_bundle_plan = weekly.weekly_sync(
        **weekly_fixture.arguments(
            transport=NoNetwork(), metadata_dir=metadata_dir, dry_run=True
        )
    )
    assert same_bundle_plan["would_capture_count"] == 1
    same_bundle_refresh = weekly.weekly_sync(
        **weekly_fixture.arguments(
            transport=ForbiddenTransport(),
            metadata_dir=metadata_dir,
            clock=lambda: NOW + timedelta(hours=1),
        )
    )
    assert same_bundle_refresh["fallback_article_ids"] == [article_id]
    connection = sqlite3.connect(weekly_fixture.database)
    assert connection.execute(
        "SELECT COUNT(*) FROM article_capture_resolutions"
    ).fetchone()[0] == 2
    connection.close()

    annotation_path = metadata_dir / "articles-001-001.json"
    annotation_payload = json.loads(annotation_path.read_text(encoding="utf-8"))
    annotation_payload["articles"][0]["summary"] += " Revised bundle."
    annotation_path.write_text(json.dumps(annotation_payload), encoding="utf-8")
    _run("git", "add", "article_metadata", cwd=weekly_fixture.repository)
    _run("git", "commit", "-qm", "revise fallback bundle", cwd=weekly_fixture.repository)
    changed_plan = weekly.weekly_sync(
        **weekly_fixture.arguments(
            transport=NoNetwork(), metadata_dir=metadata_dir, dry_run=True
        )
    )
    assert changed_plan["status"] == "ok"
    assert changed_plan["would_capture_count"] == 1
    refreshed = weekly.weekly_sync(
        **weekly_fixture.arguments(
            transport=ForbiddenTransport(),
            metadata_dir=metadata_dir,
            clock=lambda: NOW + timedelta(hours=2),
        )
    )
    assert refreshed["coverage_status"] == "partial_with_validated_fallback"
    connection = sqlite3.connect(weekly_fixture.database)
    assert connection.execute(
        "SELECT COUNT(*) FROM article_capture_resolutions"
    ).fetchone()[0] == 3
    connection.close()
    final_no_op = weekly.weekly_sync(
        **weekly_fixture.arguments(
            transport=NoNetwork(), metadata_dir=metadata_dir, dry_run=True
        )
    )
    assert final_no_op["status"] == "no-op"
    assert final_no_op["would_capture_count"] == 0

    connection = sqlite3.connect(weekly_fixture.database)
    wrong_at = (NOW + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    connection.execute(
        """
        INSERT INTO article_fetches(
            fetch_id, article_id, requested_url, final_url, fetched_at,
            fetch_status, http_status, error_code, error_message
        ) VALUES ('fetch-wrong-requested-url', ?, 'https://evil.example/target',
                  'https://example.com/target', ?, 'failed', 403,
                  'http_error', 'publisher bot wall')
        """,
        (article_id, wrong_at),
    )
    current = connection.execute(
        "SELECT * FROM article_capture_resolutions ORDER BY validated_at DESC LIMIT 1"
    ).fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="invalid capture fallback"):
        connection.execute(
            """
            INSERT INTO article_capture_resolutions(
                resolution_id, report_id, report_date, report_sha256, article_id,
                canonical_url, fetch_id, failure_class, http_status, attempt_at,
                fallback_source, fallback_provenance, bundle_json, bundle_sha256,
                validated_at
            ) VALUES (?, ?, ?, ?, ?, 'https://example.com/target',
                      'fetch-wrong-requested-url', 'http_403_publisher_bot_wall',
                      403, ?, ?, ?, ?, ?, ?)
            """,
            (
                "resolution-" + "f" * 64,
                current[1], current[2], current[3], article_id, wrong_at,
                current[10], current[11], current[12], current[13], wrong_at,
            ),
        )
    connection.close()


@pytest.mark.parametrize("fallback_count", (0, 4))
def test_twenty_five_article_coverage_promotes_only_when_fully_resolved(
    weekly_fixture, fallback_count
):
    urls = [f"https://example.com/target-{index:02d}" for index in range(1, 26)]
    item_text = "".join(
        _item(f"Target {index}", url) for index, url in enumerate(urls, start=1)
    )
    link_text = "".join(f"- {url}\n" for url in urls)
    weekly_fixture.source.write_text(
        f"""# Weekly Climate & Actuarial Monitor
**Report Date:** {TARGET}
## Executive Summary
- Sites checked: **25**, succeeded: **21**, failed: **4**
## Pillar A — Changes
{item_text}
## Pillar B — Intelligence
## Original Links
{link_text}
""",
        encoding="utf-8",
    )
    metadata_dir = weekly_fixture.repository / "article_metadata"
    fallback_urls = urls[-fallback_count:] if fallback_count else []
    if fallback_urls:
        _write_annotations(metadata_dir, fallback_urls)
    _run("git", "add", ".", cwd=weekly_fixture.repository)
    _run("git", "commit", "-qm", "25 article target", cwd=weekly_fixture.repository)
    _write_artifact(weekly_fixture.source, weekly_fixture.artifact_root)
    ledger = weekly_fixture.database.parent / f"ledger-{fallback_count}"
    append_attempt(
        ledger,
        _publisher_attempt(hashlib.sha256(weekly_fixture.source.read_bytes()).hexdigest()),
        repository_root=weekly_fixture.repository,
    )

    class MixedTransport(FakeTransport):
        def request(self, target, headers, *, timeout, max_bytes):
            self.calls += 1
            if target.url in fallback_urls:
                return FetchResponse(
                    403, target.url, {"content-type": "text/html"}, b"publisher bot wall"
                )
            body = b"""<html><body><h1>Climate insurance outlook</h1>
            <p>Climate risk and transition risk affect insurance capital, underwriting,
            investment portfolios, disclosure standards, catastrophe models, regulatory
            policy, financial resilience and sustainability reporting this week.</p>
            </body></html>"""
            return FetchResponse(
                200, target.url, {"content-type": "text/html; charset=utf-8"}, body
            )

    result = weekly.weekly_sync(
        **weekly_fixture.arguments(
            publisher_ledger_dir=ledger,
            metadata_dir=metadata_dir,
            transport=MixedTransport(),
        )
    )
    assert result["target_eligible_article_count"] == 25
    assert result["articles_captured"] == 25 - fallback_count
    assert result["articles_failed"] == fallback_count
    assert result["articles_fallback"] == fallback_count
    assert result["fallback_article_ids"] == sorted(result["fallback_article_ids"])
    assert len(result["fallback_article_ids"]) == fallback_count
    assert set(result["fallback_article_ids"]) <= set(result["target_article_ids"])
    assert result["articles_unresolved"] == 0
    assert result["promotion"] == "performed"
    assert result["coverage_status"] == (
        "partial_with_validated_fallback" if fallback_count else "ok"
    )

    reader = api_server.RegistryReader(
        weekly_fixture.database,
        repository_root=weekly_fixture.repository,
        source_dir=weekly_fixture.source_dir,
        metadata_dir=metadata_dir,
    )
    report = reader.report(TARGET)
    provenances = []
    for article in report["articles"]:
        detail = reader.article(article["article_id"])
        assert detail["summary"] and detail["categories"] and detail["keywords"]
        provenances.append(detail["summary_provenance"])
        if detail["summary_provenance"] != "content_enrichment":
            assert detail["latest_fetch"] == {
                "fetched_at": NOW.isoformat().replace("+00:00", "Z"),
                "fetch_status": "failed",
                "http_status": 403,
                "content_type": "text/html",
                "error_code": "http_error",
            }
    assert provenances.count("content_enrichment") == 25 - fallback_count
    assert provenances.count("publisher_excerpt_annotation") == fallback_count


@pytest.mark.parametrize("failure_kind", ("http_503", "dns", "blocked_response"))
def test_non_bot_wall_capture_failure_remains_unresolved(
    weekly_fixture, failure_kind
):
    class FailureTransport:
        def request(self, target, headers, *, timeout, max_bytes):
            if failure_kind == "http_503":
                return FetchResponse(503, target.url, {"content-type": "text/html"}, b"down")
            if failure_kind == "dns":
                raise FetchFailure("dns_error", "name lookup failed")
            return FetchResponse(
                200,
                target.url,
                {"content-type": "text/html"},
                b"<html><body>access denied captcha challenge</body></html>",
            )

    before = weekly_fixture.database.read_bytes()
    with pytest.raises(weekly.WeeklyPartialError) as raised:
        weekly.weekly_sync(
            **weekly_fixture.arguments(transport=FailureTransport())
        )
    result = raised.value.result
    assert result["coverage_status"] == "blocked_unresolved"
    assert result["articles_failed"] == 1
    assert result["articles_fallback"] == 0
    assert result["articles_unresolved"] == 1
    assert weekly_fixture.database.read_bytes() == before
    assert not weekly_fixture.backup_dir.exists()


@pytest.mark.parametrize("fallback_problem", ("missing", "identity_mismatch", "incomplete"))
def test_403_with_invalid_or_missing_fallback_blocks_before_backup(
    weekly_fixture, fallback_problem
):
    metadata_dir = weekly_fixture.repository / "article_metadata"
    if fallback_problem != "missing":
        _write_target_annotation(metadata_dir)
        path = metadata_dir / "articles-001-001.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if fallback_problem == "identity_mismatch":
            payload["articles"][0]["canonical_url"] = "https://other.example/item"
        else:
            payload["articles"][0]["categories"] = []
        path.write_text(json.dumps(payload), encoding="utf-8")
        _run("git", "add", "article_metadata", cwd=weekly_fixture.repository)
        _run("git", "commit", "-qm", fallback_problem, cwd=weekly_fixture.repository)

    before = weekly_fixture.database.read_bytes()
    with pytest.raises(weekly.WeeklyPartialError) as raised:
        weekly.weekly_sync(
            **weekly_fixture.arguments(
                transport=ForbiddenTransport(), metadata_dir=metadata_dir
            )
        )
    result = raised.value.result
    assert result["coverage_status"] == "blocked_unresolved"
    assert result["articles_failed"] == 1
    assert result["articles_fallback"] == 0
    assert result["articles_unresolved"] == 1
    assert result["promotion"] == "blocked"
    assert weekly_fixture.database.read_bytes() == before
    assert not weekly_fixture.backup_dir.exists()


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
    assert weekly_fixture.lock_file.is_file()


def test_external_annotation_catalog_change_blocks_before_backup(
    weekly_fixture, tmp_path
):
    metadata_dir = tmp_path / "external-article-metadata"
    _write_target_annotation(metadata_dir)
    annotation_path = metadata_dir / "articles-001-001.json"
    before = weekly_fixture.database.read_bytes()

    class ChangingForbiddenTransport(ForbiddenTransport):
        def request(self, *args, **kwargs):
            response = super().request(*args, **kwargs)
            payload = json.loads(annotation_path.read_text(encoding="utf-8"))
            payload["articles"][0]["summary"] += " Changed during capture."
            annotation_path.write_text(json.dumps(payload), encoding="utf-8")
            return response

    with pytest.raises(RegistryLockError, match="metadata changed"):
        weekly.weekly_sync(
            **weekly_fixture.arguments(
                metadata_dir=metadata_dir,
                transport=ChangingForbiddenTransport(),
            )
        )
    assert weekly_fixture.database.read_bytes() == before
    assert not weekly_fixture.backup_dir.exists()


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


def test_post_promotion_full_validation_failure_restores_exact_live_database(
    weekly_fixture, monkeypatch
):
    before = weekly_fixture.database.read_bytes()
    before_sha = hashlib.sha256(before).hexdigest()
    real_validate = weekly._validate_candidate

    def tamper_promoted_live(path, preflight):
        if Path(path) == weekly_fixture.database:
            Path(path).write_bytes(b"tampered-after-promotion")
        return real_validate(path, preflight)

    monkeypatch.setattr(weekly, "_validate_candidate", tamper_promoted_live)
    with pytest.raises(weekly.WeeklyValidationError, match="candidate is invalid"):
        weekly.weekly_sync(**weekly_fixture.arguments(transport=FakeTransport()))

    assert weekly_fixture.database.read_bytes() == before
    backups = list(weekly_fixture.backup_dir.iterdir())
    assert len(backups) == 1
    assert hashlib.sha256(backups[0].read_bytes()).hexdigest() == before_sha
    assert not [
        path
        for path in weekly_fixture.database.parent.glob(
            f".{weekly_fixture.database.name}.*"
        )
        if path.is_dir()
    ]

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
