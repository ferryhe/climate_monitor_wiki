import json

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
