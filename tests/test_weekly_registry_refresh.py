from __future__ import annotations

import json
import subprocess
from urllib import error

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
        "--expected-api-host",
        "climate.example",
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
        "articles_fallback": 0,
        "fallback_article_ids": [],
        "articles_unresolved": 0,
        "fallback_failure_classes": {},
        "promotion_with_fallback": False,
        "coverage_status": "ok",
        "target_article_count": 1,
        "target_eligible_article_count": 1,
        "target_article_ids": ["article-safe"],
        "would_add_reports": 1,
        "would_add_articles": 1,
        "would_capture_article_ids": ["article-safe"],
        "would_capture_count": 1,
        "would_fallback_count": 0,
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
                "verified_eligible_article_count": 1,
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


def test_no_op_is_local_only_and_does_not_reload_or_call_api(tmp_path, capsys, monkeypatch):
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
    monkeypatch.setattr(
        refresh,
        "_request_json",
        lambda *_args, **_kwargs: pytest.fail("no-op must not call the API"),
    )
    monkeypatch.setenv("RELOAD_TOKEN", "reload-secret")

    code = refresh.main(_argv(tmp_path))
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["dry_run_status"] == "no-op"
    assert payload["sync_status"] == "no-op"
    assert payload["promotion"] == "not-needed"
    assert payload["reload"] == "not-needed"
    assert payload["verification"]["local_only"] is True


def test_runner_accepts_complete_api_fallback_with_real_failed_403():
    detail = _api_payload(
        "https://climate.example/api/registry/articles/article-safe", timeout=1
    )
    detail["summary_provenance"] = "publisher_excerpt_annotation"
    detail["metadata_provenance"] = {
        "categories": "publisher_excerpt_annotation",
        "keywords": "publisher_excerpt_annotation",
    }
    detail["latest_fetch"] = {
        "fetch_status": "failed",
        "http_status": 403,
        "error_code": "http_error",
    }
    detail["enrichment"] = {
        "summary": None,
        "categories": [],
        "keywords": [],
        "language": None,
        "generator": None,
    }
    refresh._verify_article(
        detail,
        article_id="article-safe",
        target_date=TARGET,
        fallback_article_ids=frozenset({"article-safe"}),
    )


def test_structured_fallback_result_preserves_real_failure_count():
    payload = _sync_payload(dry_run=False)
    payload.update(
        {
            "coverage_status": "partial_with_validated_fallback",
            "articles_captured": 21,
            "articles_failed": 4,
            "articles_fallback": 4,
            "fallback_article_ids": [f"article-{index}" for index in range(4)],
            "articles_unresolved": 0,
            "fallback_failure_classes": {"http_403_publisher_bot_wall": 4},
            "promotion_with_fallback": True,
            "target_article_count": 5,
            "target_article_ids": [
                "article-0", "article-1", "article-2", "article-3", "article-safe"
            ],
        }
    )
    assert refresh._validate_sync_result(
        payload, target_date=TARGET, dry_run=False, returncode=0
    )["articles_failed"] == 4


@pytest.mark.parametrize(
    "fallback_ids",
    (["article-safe", "article-safe"], ["article-unbound"], []),
)
def test_structured_fallback_ids_fail_closed_when_not_exact(fallback_ids):
    payload = _sync_payload(dry_run=False)
    payload.update(
        {
            "coverage_status": "partial_with_validated_fallback",
            "articles_failed": 1,
            "articles_fallback": 1,
            "fallback_article_ids": fallback_ids,
            "fallback_failure_classes": {"http_403_publisher_bot_wall": 1},
            "promotion_with_fallback": True,
        }
    )
    with pytest.raises(refresh._JobError, match="invalid_sync_result"):
        refresh._validate_sync_result(
            payload, target_date=TARGET, dry_run=False, returncode=0
        )


def test_runner_accepts_complete_db_enrichment_with_empty_metadata_lists():
    detail = _api_payload(
        "https://climate.example/api/registry/articles/article-safe", timeout=1
    )
    detail["categories"] = []
    detail["keywords"] = []
    detail["enrichment"]["categories"] = []
    detail["enrichment"]["keywords"] = []
    refresh._verify_article(
        detail,
        article_id="article-safe",
        target_date=TARGET,
        fallback_article_ids=frozenset(),
    )


def test_db_first_enrichment_with_later_403_requires_bound_fallback_id():
    detail = _api_payload(
        "https://climate.example/api/registry/articles/article-safe", timeout=1
    )
    detail["latest_fetch"] = {
        "fetch_status": "failed",
        "http_status": 403,
        "error_code": "http_error",
    }
    with pytest.raises(refresh._JobError, match="article_detail_incomplete"):
        refresh._verify_article(
            detail,
            article_id="article-safe",
            target_date=TARGET,
            fallback_article_ids=frozenset(),
        )
    refresh._verify_article(
        detail,
        article_id="article-safe",
        target_date=TARGET,
        fallback_article_ids=frozenset({"article-safe"}),
    )


@pytest.mark.parametrize(
    ("value", "expected_host"),
    (
        ("https://user:secret@climate.example", "climate.example"),
        ("http://climate.example", "climate.example"),
        ("https://climate.example", None),
        ("https://climate.example", "other.example"),
        ("http://localhost.example", None),
    ),
)
def test_runner_rejects_userinfo_remote_http_and_unbound_hosts(value, expected_host):
    with pytest.raises(refresh._JobError, match="invalid_base_url"):
        refresh._base_url(value, expected_host)


def test_runner_allows_literal_loopback_and_exact_https_host():
    assert refresh._base_url("http://127.0.0.1:8501") == "http://127.0.0.1:8501"
    assert refresh._base_url("http://localhost:8501") == "http://localhost:8501"
    assert (
        refresh._base_url("https://climate.example", "climate.example")
        == "https://climate.example"
    )


@pytest.mark.parametrize("redirect", ("https://climate.example/next", "https://evil.example/next"))
def test_runner_rejects_redirect_without_forwarding_reload_token(monkeypatch, redirect):
    opened = []

    class RedirectingOpener:
        def open(self, outbound, *, timeout):
            opened.append((outbound.full_url, dict(outbound.header_items()), timeout))
            raise error.HTTPError(outbound.full_url, 302, redirect, {}, None)

    monkeypatch.setattr(refresh.request, "build_opener", lambda *_handlers: RedirectingOpener())
    with pytest.raises(refresh._JobError, match="api_request_failed"):
        refresh._request_json(
            "https://climate.example/api/reload",
            method="POST",
            headers={"x-reload-token": "top-secret"},
            timeout=1,
        )
    assert len(opened) == 1
    assert opened[0][0] == "https://climate.example/api/reload"
    assert opened[0][1] == {"X-reload-token": "top-secret"}


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
