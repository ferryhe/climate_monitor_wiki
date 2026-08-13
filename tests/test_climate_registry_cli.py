import json

from climate_registry.audit import build_audit_registry
from climate_registry.cli import main


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
