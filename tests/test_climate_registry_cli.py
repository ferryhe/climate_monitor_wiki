import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from climate_registry.audit import build_audit_registry
from climate_registry.cli import (
    WEEKLY_NO_OP_EXIT,
    WEEKLY_PREFLIGHT_EXIT,
    WEEKLY_VALIDATION_EXIT,
    main,
)
from climate_registry.errors import RegistryBuildError, RegistryInputError, RegistryLockError
from climate_registry.persistent import _exclusive_database_lock
from climate_registry.weekly import (
    WeeklyPartialError,
    WeeklyPreflightError,
    WeeklyValidationError,
)
import climate_registry.cli as registry_cli


NONCANONICAL_CANDIDATE_URLS = (
    "https://example.com/%2fstory",
    "https://example.com/%41",
    "https://example.com:/story",
    "https://example.com:443/story",
    "https://example.com/a/./b",
    "https://example.com./story",
    "https://empty..example/story",
    "https://under_score.example/story",
    "https://xn--a.example/story",
    "https://[fe80::1%25eth0]/story",
    "https://[2001:DB8::1]/story",
    "https://[v1.]/story",
    "https://127.1/story",
    "https://2130706433/story",
    "https://127.0.0.01/story",
    "https://example.com:08443/story",
    "https://[2001:db8::1]:08443/story",
    "https://xn--ab-0ea.example/story",
    "https://xn--ab-j1t.example/story",
)


def test_cli_reports_input_errors_as_json(tmp_path, capsys):
    code = main(
        [
            "audit-history",
            "--source-dir",
            str(tmp_path / "missing"),
            "--database",
            str(tmp_path / "registry.sqlite3"),
            "--output-dir",
            str(tmp_path / "output"),
        ]
    )

    assert code == 2
    assert json.loads(capsys.readouterr().out)["kind"] == "input"


def _weekly(date: str) -> str:
    return f"""# Weekly Climate Monitor
**Report Date:** {date}
## Executive Summary
- Sites checked: **1**, succeeded: **1**, failed: **0**
## Pillar A
- **Title** (web)
  - Summary.
  🔗 https://example.com/item
## Pillar B
## Original Links
- https://example.com/item
"""


def test_plan_update_cli_emits_read_only_json(tmp_path, capsys):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "climate-monitor-2026-08-03.md").write_text(_weekly("2026-08-03"), encoding="utf-8")
    database = tmp_path / "registry.sqlite3"
    build_audit_registry(source_dir, database, tmp_path / "audit")
    before = database.read_bytes()

    code = main(
        ["plan-update", "--source-dir", str(source_dir), "--database", str(database)]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "plan"
    assert payload["mutation_required"] is False
    assert database.read_bytes() == before


def test_plan_selection_cli_emits_one_sanitized_json_line(tmp_path, capsys):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "climate-monitor-2026-08-03.md").write_text(_weekly("2026-08-03"), encoding="utf-8")
    database = tmp_path / "registry.sqlite3"
    build_audit_registry(source_dir, database, tmp_path / "audit")
    input_path = tmp_path / "secret-input-name.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": "registry-selection-input.v1",
                "report_date": "2026-08-10",
                "candidates": [
                    {
                        "candidate_id": "candidate-safe",
                        "pillar": "B",
                        "title": "Private candidate title",
                        "summary": "Private summary text",
                        "url": "https://private.example/story?secret=value",
                    },
                    {
                        "candidate_id": "candidate-seen",
                        "pillar": "B",
                        "title": "Previously represented",
                        "summary": "Different summary",
                        "url": "https://example.com/item",
                    },
                    {
                        "candidate_id": "candidate-ipv6",
                        "pillar": "B",
                        "title": "IPv6 candidate",
                        "summary": "Private summary",
                        "url": "https://[2001:db8::1]:8443/story",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "plan-selection",
            "--database",
            str(database),
            "--source-dir",
            str(source_dir),
            "--input",
            str(input_path),
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert code == 0
    assert output.count("\n") == 1
    assert payload["decisions"] == [
        {
            "candidate_id": "candidate-safe",
            "pillar": "B",
            "disposition": "selected",
            "reason": "new_article",
        },
        {
            "candidate_id": "candidate-seen",
            "pillar": "B",
            "disposition": "rejected",
            "reason": "historical_url_seen",
        },
        {
            "candidate_id": "candidate-ipv6",
            "pillar": "B",
            "disposition": "selected",
            "reason": "new_article",
        },
    ]
    for sensitive in ("private.example", "Private candidate", "Private summary", "secret-input-name"):
        assert sensitive not in output
    assert "https://" not in output


def test_plan_selection_cli_uses_stable_input_and_build_error_categories(tmp_path, capsys):
    missing_input = tmp_path / "missing-input.json"
    code = main(
        [
            "plan-selection",
            "--database",
            str(tmp_path / "missing.sqlite3"),
            "--source-dir",
            str(tmp_path / "missing-sources"),
            "--input",
            str(missing_input),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert (code, payload["kind"]) == (2, "input")
    assert str(tmp_path) not in payload["message"]


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://example.com:bad/private",
        "https://example.com\\private",
        "https://example.com|evil/private",
        "https://example.com/private[raw]",
        "https://example.com/private?q=[raw]",
        "https://example.com/private#part[raw]",
        "https://example.com/café",
        *NONCANONICAL_CANDIDATE_URLS,
    ],
)
def test_plan_selection_cli_rejects_malformed_url_without_echo(
    tmp_path, capsys, unsafe_url
):
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": "registry-selection-input.v1",
                "report_date": "2026-08-10",
                "candidates": [
                    {
                        "candidate_id": "unsafe-url",
                        "pillar": "A",
                        "title": "Title",
                        "summary": "Summary",
                        "url": unsafe_url,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "plan-selection", "--database", str(tmp_path / "unused.sqlite3"),
            "--source-dir", str(tmp_path / "unused-sources"), "--input", str(input_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert (code, payload) == (
        2,
        {
            "status": "error",
            "kind": "input",
            "message": "selection candidate URL is invalid",
        },
    )
    assert unsafe_url not in json.dumps(payload)


@pytest.mark.parametrize("unsafe_url", NONCANONICAL_CANDIDATE_URLS)
def test_plan_selection_real_cli_rejects_noncanonical_candidate_without_echo(
    tmp_path, unsafe_url
):
    input_path = tmp_path / "private-input.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": "registry-selection-input.v1",
                "report_date": "2026-08-10",
                "candidates": [
                    {
                        "candidate_id": "unsafe-url",
                        "pillar": "A",
                        "title": "Title",
                        "summary": "Summary",
                        "url": unsafe_url,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    repo = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "climate_registry",
            "plan-selection",
            "--database",
            str(tmp_path / "unused.sqlite3"),
            "--source-dir",
            str(tmp_path / "unused-sources"),
            "--input",
            str(input_path),
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout) == {
        "status": "error",
        "kind": "input",
        "message": "selection candidate URL is invalid",
    }
    assert unsafe_url not in result.stdout
    assert str(tmp_path) not in result.stdout


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://[::1",
        "https://example.com|evil/story",
        "https://example.com\\story",
        "https://example.com/story[raw]",
        "https://example.com/story?q=[raw]",
        *NONCANONICAL_CANDIDATE_URLS,
    ],
)
def test_plan_selection_real_cli_sanitizes_malformed_source_history(
    tmp_path, unsafe_url
):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    report = source_dir / "climate-monitor-2026-08-03.md"
    report.write_text(_weekly("2026-08-03"), encoding="utf-8")
    database = tmp_path / "registry.sqlite3"
    build_audit_registry(source_dir, database, tmp_path / "audit")
    report.write_text(
        _weekly("2026-08-03").replace(
            "https://example.com/item", unsafe_url
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE reports SET report_sha256 = ? WHERE report_date = '2026-08-03'",
        (digest,),
    )
    connection.commit()
    connection.close()
    input_path = tmp_path / "private-input.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": "registry-selection-input.v1",
                "report_date": "2026-08-10",
                "candidates": [],
            }
        ),
        encoding="utf-8",
    )
    repo = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "climate_registry",
            "plan-selection",
            "--database",
            str(database),
            "--source-dir",
            str(source_dir),
            "--input",
            str(input_path),
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 3
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout) == {
        "status": "error",
        "kind": "build",
        "message": "registry source report history is invalid",
    }
    assert unsafe_url not in result.stdout
    assert str(tmp_path) not in result.stdout
    assert "Traceback" not in result.stdout


@pytest.mark.parametrize(
    ("failure", "code", "kind", "message"),
    [
        (RegistryInputError("registry database does not exist"), 2, "input", "registry database does not exist"),
        (RegistryBuildError("registry database is unreadable or corrupt"), 3, "build", "registry database is unreadable or corrupt"),
        (RegistryInputError("registry schema contract is invalid"), 2, "input", "registry schema contract is invalid"),
        (RegistryInputError("registry and source history are not synchronized"), 2, "input", "registry and source history are not synchronized"),
    ],
)
def test_plan_selection_cli_preserves_stable_database_failure_taxonomy(
    tmp_path, capsys, monkeypatch, failure, code, kind, message
):
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": "registry-selection-input.v1",
                "report_date": "2026-08-10",
                "candidates": [],
            }
        ),
        encoding="utf-8",
    )

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(registry_cli, "plan_registry_selection", fail)
    result = main(
        [
            "plan-selection", "--database", "ignored.sqlite3",
            "--source-dir", "ignored-sources", "--input", str(input_path),
        ]
    )

    assert result == code
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "kind": kind,
        "message": message,
    }


def test_plan_selection_cli_reports_real_corrupt_database_as_build_error(tmp_path, capsys):
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "schema_version": "registry-selection-input.v1",
                "report_date": "2026-08-10",
                "candidates": [],
            }
        ),
        encoding="utf-8",
    )
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "climate-monitor-2026-08-03.md").write_text(_weekly("2026-08-03"), encoding="utf-8")
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not sqlite")
    code = main(
        [
            "plan-selection", "--database", str(corrupt), "--source-dir", str(sources),
            "--input", str(input_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert (code, payload["kind"]) == (3, "build")
    assert str(tmp_path) not in payload["message"]


def test_update_cli_reports_actively_held_lock(tmp_path, capsys):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "climate-monitor-2026-08-03.md").write_text(_weekly("2026-08-03"), encoding="utf-8")
    database = tmp_path / "registry.sqlite3"
    build_audit_registry(source_dir, database, tmp_path / "audit")
    with _exclusive_database_lock(database):
        code = main(
            [
                "update",
                "--source-dir",
                str(source_dir),
                "--database",
                str(database),
                "--backup-dir",
                str(tmp_path / "backups"),
            ]
        )

    assert code == 4
    assert json.loads(capsys.readouterr().out)["kind"] == "lock"


def test_capture_cli_emits_one_json_line_and_partial_exit_without_sensitive_values(
    tmp_path, capsys, monkeypatch
):
    def fake_capture(database, backup_dir, **kwargs):
        assert kwargs == {"article_ids": ["article-safe"], "limit": 4, "refresh": True}
        return {
            "status": "partial",
            "selected": 1,
            "counts": {"failed": 1},
            "articles": [{"article_id": "article-safe", "status": "failed", "error_code": "timeout"}],
            "backup": str(backup_dir / "registry.bak"),
        }

    monkeypatch.setattr(registry_cli, "capture_enrich_registry", fake_capture)
    code = main(
        [
            "capture-enrich", "--database", str(tmp_path / "registry.sqlite3"),
            "--backup-dir", str(tmp_path / "backups"), "--article-id", "article-safe",
            "--limit", "4", "--refresh",
        ]
    )
    output = capsys.readouterr().out
    assert code == 5
    assert output.count("\n") == 1
    assert json.loads(output)["articles"][0]["error_code"] == "timeout"
    assert "https://" not in output and "body" not in output and "secret" not in output


def test_capture_cli_reports_invalid_limit_as_one_json_line(tmp_path, capsys):
    code = main(
        [
            "capture-enrich",
            "--database",
            str(tmp_path / "registry.sqlite3"),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--limit",
            "0",
        ]
    )
    output = capsys.readouterr().out
    assert code == 2
    assert output.count("\n") == 1
    assert json.loads(output)["kind"] == "input"


def _weekly_argv(tmp_path):
    database = tmp_path / "registry.sqlite3"
    return [
        "weekly-sync",
        "--date",
        "2026-08-17",
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
        "--timeout",
        "12.5",
        "--dry-run",
    ]


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [("ok", 0), ("no-op", WEEKLY_NO_OP_EXIT)],
)
def test_weekly_sync_cli_passes_explicit_inputs_and_distinguishes_no_op(
    tmp_path, capsys, monkeypatch, status, expected_code
):
    def fake_weekly_sync(**kwargs):
        assert kwargs == {
            "target_date": "2026-08-17",
            "source_dir": tmp_path / "sources",
            "database": tmp_path / "registry.sqlite3",
            "artifact_root": tmp_path / "artifacts",
            "backup_dir": tmp_path / "backups",
            "lock_file": tmp_path / "registry.sqlite3.lock",
            "publisher_ledger_dir": tmp_path / "ledger",
            "metadata_dir": None,
            "expected_report_sha256": None,
            "timeout": 12.5,
            "dry_run": True,
            "allow_offcycle": False,
        }
        return {"status": status, "date": "2026-08-17"}

    monkeypatch.setattr(registry_cli, "weekly_sync", fake_weekly_sync)
    code = main(_weekly_argv(tmp_path))

    assert code == expected_code
    assert json.loads(capsys.readouterr().out)["status"] == status


@pytest.mark.parametrize(
    ("failure", "expected_code", "kind", "status"),
    [
        (
            WeeklyPreflightError("weekly preflight blocked"),
            WEEKLY_PREFLIGHT_EXIT,
            "preflight",
            "failed",
        ),
        (
            WeeklyValidationError("weekly validation failed"),
            WEEKLY_VALIDATION_EXIT,
            "validation",
            "failed",
        ),
        (RegistryLockError("weekly lock conflict"), 4, "lock", "failed"),
    ],
)
def test_weekly_sync_cli_has_distinct_structured_failure_codes(
    tmp_path, capsys, monkeypatch, failure, expected_code, kind, status
):
    def fail(**_kwargs):
        raise failure

    monkeypatch.setattr(registry_cli, "weekly_sync", fail)
    code = main(_weekly_argv(tmp_path))
    payload = json.loads(capsys.readouterr().out)

    assert code == expected_code
    assert payload["status"] == status
    assert payload["kind"] == kind
    assert payload["date"] == "2026-08-17"
    assert payload["promotion"] == "blocked"
    assert payload["reload_required"] is False


def test_weekly_sync_cli_preserves_sanitized_partial_result(
    tmp_path, capsys, monkeypatch
):
    partial = {
        "status": "partial",
        "date": "2026-08-17",
        "report_sha256": "a" * 64,
        "articles_failed": 1,
        "promotion": "blocked",
        "reload_required": False,
        "capture": {
            "succeeded_article_ids": [],
            "failures": [
                {
                    "article_id": "article-safe",
                    "status": "failed",
                    "error_code": "timeout",
                }
            ],
            "skipped_article_ids": [],
        },
    }

    def fail(**_kwargs):
        raise WeeklyPartialError("weekly capture partial", result=partial)

    monkeypatch.setattr(registry_cli, "weekly_sync", fail)
    code = main(_weekly_argv(tmp_path))
    payload = json.loads(capsys.readouterr().out)

    assert code == 5
    assert payload["status"] == "partial"
    assert payload["kind"] == "partial"
    assert payload["capture"]["failures"][0]["error_code"] == "timeout"


def test_restore_backup_cli_uses_explicit_verified_inputs(tmp_path, capsys, monkeypatch):
    database = tmp_path / "registry.sqlite3"
    backup = tmp_path / "registry-old.bak"

    def fake_restore(**kwargs):
        assert kwargs == {
            "database": database,
            "backup": backup,
            "expected_sha256": "a" * 64,
            "backup_dir": tmp_path / "restore-backups",
            "lock_file": tmp_path / "registry.sqlite3.lock",
        }
        return {
            "status": "ok",
            "promotion": "performed",
            "reload_required": True,
            "restored_database_sha256": "a" * 64,
        }

    monkeypatch.setattr(registry_cli, "restore_registry_backup", fake_restore)
    code = main(
        [
            "restore-backup",
            "--database",
            str(database),
            "--backup",
            str(backup),
            "--expected-sha256",
            "a" * 64,
            "--backup-dir",
            str(tmp_path / "restore-backups"),
            "--lock-file",
            str(tmp_path / "registry.sqlite3.lock"),
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["reload_required"] is True


@pytest.mark.parametrize(
    "argv",
    [
        ["capture-enrich"],
        ["not-a-command"],
        ["capture-enrich", "--database", "db", "--backup-dir", "backups", "--unknown"],
    ],
)
def test_cli_argument_errors_are_one_json_line_without_usage(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    assert code == 2
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload["kind"] == "input"
    assert "usage:" not in captured.out.lower()
    assert "not-a-command" not in captured.out
    assert "--unknown" not in captured.out
