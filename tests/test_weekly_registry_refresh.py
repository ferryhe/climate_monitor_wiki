from __future__ import annotations

import json
import subprocess

import pytest

import scripts.weekly_registry_refresh as refresh


TARGET = "2026-08-17"
REPORT_SHA = "a" * 64


@pytest.fixture(autouse=True)
def _stub_sha_binding(monkeypatch):
    monkeypatch.setattr(
        refresh,
        "_verify_sha_binding",
        lambda _args, _result: {
            "report_sha256": REPORT_SHA,
            "database_sha256": "c" * 64,
            "target_article_count": 1,
            "target_article_ids": ["article-safe"],
        },
    )


def _argv(tmp_path):
    database = tmp_path / "registry.sqlite3"
    return [
        "--date",
        TARGET,
        "--source-dir",
        str(tmp_path / "sources"),
        "--database",
        str(database),
        "--artifact-root",
        str(tmp_path / "artifacts"),
        "--backup-dir",
        str(tmp_path / "backups"),
        "--lock-file",
        str(database.with_name(f"{database.name}.lock")),
        "--publisher-ledger-dir",
        str(tmp_path / "ledger"),
        "--base-url",
        "https://climate.example",
    ]


def _sync_payload(*, dry_run: bool, status: str = "ok"):
    performed = not dry_run and status == "ok"
    return {
        "status": status,
        "date": TARGET,
        "report_sha256": REPORT_SHA,
        "dry_run": dry_run,
        "reports_added": 0 if dry_run else 1,
        "articles_added": 0 if dry_run else 1,
        "articles_captured": 0 if dry_run else 1,
        "articles_failed": 0,
        "target_article_count": 1,
        "target_eligible_article_count": 1,
        "target_article_ids": ["article-safe"],
        "would_add_reports": 1,
        "would_add_articles": 1,
        "would_capture_article_ids": ["article-safe"],
        "would_capture_count": 1,
        "would_promote": True,
        "promotion": "performed" if performed else "not-needed",
        "reload_required": performed,
        "database_sha256_before": "b" * 64,
        "database_sha256_after": "c" * 64 if performed else "b" * 64,
        "backup_name": "registry.sqlite3.20260817.bak" if performed else None,
    }


def _api_payload(url, *, method="GET", headers=None, timeout):
    if url.endswith("/api/reload"):
        assert method == "POST"
        assert headers == {"x-reload-token": "reload-secret"}
        return {"status": "ok"}
    if url.endswith("/api/registry/status"):
        return {"available": True, "latest_report_date": TARGET}
    if url.endswith(f"/api/registry/reports/{TARGET}"):
        return {
            "report_date": TARGET,
            "report_briefing": {
                "executive_summary": ["Weekly narrative."],
                "monitoring_snapshot": {
                    "sites_checked": 1,
                    "sites_succeeded": 1,
                    "sites_failed": 0,
                    "pillar_a_updates": 1,
                    "pillar_b_updates": 0,
                    "notes": [],
                },
            },
            "report_pdf": {
                "filename": f"climate-monitor-{TARGET}.pdf",
                "download_url": f"/api/registry/reports/{TARGET}/pdf",
            },
            "articles": [{"article_id": "article-safe"}],
        }
    if url.endswith("/api/registry/articles/article-safe"):
        return {
            "article_id": "article-safe",
            "title": "Climate outlook",
            "summary": "Climate risk affects insurance markets.",
            "summary_provenance": "content_enrichment",
            "canonical_url": "https://example.com/climate",
            "original_url": "https://example.com/climate?source=report",
            "source": "example.com",
            "publisher": "Example",
            "publication_eligible": True,
            "categories": ["climate-risk"],
            "keywords": ["climate", "insurance"],
            "metadata_provenance": {
                "categories": "content_enrichment",
                "keywords": "content_enrichment",
            },
            "latest_fetch": {"fetch_status": "success"},
            "appearances": [{"report_date": TARGET}],
            "enrichment": {
                "summary": "Climate risk affects insurance markets.",
                "categories": ["climate-risk"],
                "keywords": ["climate", "insurance"],
                "language": "en",
                "generator": {
                    "kind": "deterministic",
                    "name": "climate-registry-rules",
                    "version": "1",
                    "generated_at": "2026-08-17T10:30:00Z",
                },
            },
        }
    raise AssertionError(f"unexpected request: {url}")


def test_draft_job_runs_dry_live_reload_and_complete_api_verification(
    tmp_path, capsys, monkeypatch
):
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        dry_run = "--dry-run" in command
        payload = _sync_payload(dry_run=dry_run)
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload) + "\n", stderr=""
        )

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)
    monkeypatch.setattr(refresh, "_request_json", _api_payload)
    monkeypatch.setenv("RELOAD_TOKEN", "reload-secret")

    code = refresh.main(_argv(tmp_path))
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 0
    assert len(commands) == 2
    assert "--dry-run" in commands[0]
    assert "--dry-run" not in commands[1]
    assert commands[1][-2:] == ["--expected-report-sha256", REPORT_SHA]
    assert payload == {
        "status": "ok",
        "date": TARGET,
        "report_sha256": REPORT_SHA,
        "dry_run_status": "ok",
        "sync_status": "ok",
        "promotion": "performed",
        "backup_name": "registry.sqlite3.20260817.bak",
        "database_sha256_before": "b" * 64,
        "database_sha256_after": "c" * 64,
        "reload": "performed",
        "verification": {
            "latest_report_date": TARGET,
            "source_registry_artifact_sha_match": True,
            "briefing": True,
            "monitoring_snapshot": True,
            "pdf": True,
            "article_count": 1,
            "sample_article_id": "article-safe",
        },
    }
    assert output.count("\n") == 1
    assert "reload-secret" not in output
    assert str(tmp_path) not in output
    assert "recipient" not in output.lower()
    assert "email" not in output.lower()


def test_dry_run_failure_stops_before_live_sync_and_reload(tmp_path, capsys, monkeypatch):
    calls = 0

    def blocked(command, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            command,
            7,
            stdout='{"status":"failed","kind":"preflight"}\n',
            stderr="",
        )

    def no_request(*_args, **_kwargs):
        raise AssertionError("reload must not run after a blocked dry-run")

    monkeypatch.setattr(refresh.subprocess, "run", blocked)
    monkeypatch.setattr(refresh, "_request_json", no_request)

    code = refresh.main(_argv(tmp_path))
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert calls == 1
    assert payload["kind"] == "dry_run_blocked"
    assert payload["reload"] == "not-performed"


def test_no_op_is_safe_and_still_reloads_and_verifies(tmp_path, capsys, monkeypatch):
    def fake_run(command, **kwargs):
        payload = _sync_payload(dry_run="--dry-run" in command, status="no-op")
        payload["would_promote"] = False
        payload["would_add_reports"] = 0
        payload["would_add_articles"] = 0
        payload["would_capture_article_ids"] = []
        payload["would_capture_count"] = 0
        return subprocess.CompletedProcess(
            command, 6, stdout=json.dumps(payload) + "\n", stderr=""
        )

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)
    monkeypatch.setattr(refresh, "_request_json", _api_payload)
    monkeypatch.setenv("RELOAD_TOKEN", "reload-secret")

    code = refresh.main(_argv(tmp_path))
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["dry_run_status"] == "no-op"
    assert payload["sync_status"] == "no-op"
    assert payload["promotion"] == "not-needed"


def test_incomplete_report_artifact_verification_fails_without_sensitive_output(
    tmp_path, capsys, monkeypatch
):
    def fake_run(command, **kwargs):
        payload = _sync_payload(dry_run="--dry-run" in command)
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload) + "\n", stderr=""
        )

    def incomplete(url, **kwargs):
        if url.endswith("/api/reload"):
            return {"status": "ok"}
        if url.endswith("/api/registry/status"):
            return {"available": True, "latest_report_date": TARGET}
        return {
            "report_date": TARGET,
            "report_briefing": {"executive_summary": ["ok"], "monitoring_snapshot": {}},
            "report_pdf": {
                "filename": f"climate-monitor-{TARGET}.pdf",
                "download_url": f"/api/registry/reports/{TARGET}/pdf",
            },
            "articles": [{"article_id": "article-safe"}],
        }

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)
    monkeypatch.setattr(refresh, "_request_json", incomplete)

    code = refresh.main(_argv(tmp_path))
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 1
    assert payload["kind"] == "report_verification_failed"
    assert payload["backup_name"] == "registry.sqlite3.20260817.bak"
    assert payload["database_sha256_before"] == "b" * 64
    assert payload["database_sha256_after"] == "c" * 64
    assert str(tmp_path) not in output
    assert "https://" not in output


def test_cross_origin_or_inexact_pdf_url_is_rejected(tmp_path, capsys, monkeypatch):
    def fake_run(command, **kwargs):
        payload = _sync_payload(dry_run="--dry-run" in command)
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload) + "\n", stderr=""
        )

    def unsafe_pdf(url, **kwargs):
        payload = _api_payload(url, **kwargs)
        if url.endswith(f"/api/registry/reports/{TARGET}"):
            payload["report_pdf"]["download_url"] = (
                "https://untrusted.example/climate-monitor.pdf"
            )
        return payload

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)
    monkeypatch.setattr(refresh, "_request_json", unsafe_pdf)
    monkeypatch.setenv("RELOAD_TOKEN", "reload-secret")

    code = refresh.main(_argv(tmp_path))
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["kind"] == "report_verification_failed"


def test_same_count_wrong_api_membership_is_rejected(tmp_path, capsys, monkeypatch):
    def fake_run(command, **kwargs):
        payload = _sync_payload(dry_run="--dry-run" in command)
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload) + "\n", stderr=""
        )

    def wrong_membership(url, **kwargs):
        payload = _api_payload(url, **kwargs)
        if url.endswith(f"/api/registry/reports/{TARGET}"):
            payload["articles"] = [{"article_id": "article-wrong"}]
        return payload

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)
    monkeypatch.setattr(refresh, "_request_json", wrong_membership)
    monkeypatch.setenv("RELOAD_TOKEN", "reload-secret")

    code = refresh.main(_argv(tmp_path))
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["kind"] == "report_verification_failed"
