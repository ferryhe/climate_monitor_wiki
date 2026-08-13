import json

import pytest

from climate_registry.audit import build_audit_registry
from climate_registry.cli import main
import climate_registry.cli as registry_cli


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


def test_update_cli_reports_existing_lock(tmp_path, capsys):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "climate-monitor-2026-08-03.md").write_text(_weekly("2026-08-03"), encoding="utf-8")
    database = tmp_path / "registry.sqlite3"
    build_audit_registry(source_dir, database, tmp_path / "audit")
    database.with_name(f"{database.name}.lock").write_text("existing", encoding="ascii")

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
