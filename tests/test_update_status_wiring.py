from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import record_weekly_run


ROOT = Path(__file__).resolve().parents[1]


def _attempt() -> dict:
    return {
        "schema_version": "weekly-run-attempt.v1",
        "attempt_id": "20260810t100000z-attempt-01",
        "stage": "publisher",
        "report_date": "2026-08-10",
        "scheduled_for": "2026-08-10T10:00:00Z",
        "finished_at": "2026-08-10T10:05:00Z",
        "status": "no_change",
        "result_code": "nothing_to_publish",
    }


def test_record_cli_is_idempotent_and_prints_no_paths(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "ledger"
    payload = tmp_path / "attempt.json"
    payload.write_text(json.dumps(_attempt()), encoding="utf-8")
    arguments = [
        "record_weekly_run.py",
        "--ledger-dir",
        str(ledger),
        "--input",
        str(payload),
    ]

    monkeypatch.setattr(sys, "argv", arguments)
    assert record_weekly_run.main() == 0
    created = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(sys, "argv", arguments)
    assert record_weekly_run.main() == 0
    repeated = json.loads(capsys.readouterr().out)

    assert created == {"attempt_id": _attempt()["attempt_id"], "status": "created"}
    assert repeated == {"attempt_id": _attempt()["attempt_id"], "status": "already_exists"}
    assert str(ledger) not in json.dumps(created)
    assert len(list(ledger.rglob("*.json"))) == 1


def test_record_cli_failure_is_sanitized(tmp_path, monkeypatch, capsys):
    payload = tmp_path / "private-name.json"
    payload.write_text('{"secret": "do not echo"}', encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "record_weekly_run.py",
            "--ledger-dir",
            str(tmp_path / "ledger"),
            "--input",
            str(payload),
        ],
    )

    assert record_weekly_run.main() == 2
    error = capsys.readouterr().err
    assert json.loads(error) == {"status": "error", "reason": "invalid_attempt"}
    assert "private-name" not in error
    assert "do not echo" not in error


def test_record_cli_rejects_duplicate_json_keys(tmp_path, monkeypatch, capsys):
    payload = tmp_path / "attempt.json"
    payload.write_text('{"schema_version":"weekly-run-attempt.v1","schema_version":"other"}', encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "record_weekly_run.py",
            "--ledger-dir",
            str(tmp_path / "ledger"),
            "--input",
            str(payload),
        ],
    )

    assert record_weekly_run.main() == 2
    assert json.loads(capsys.readouterr().err) == {"status": "error", "reason": "invalid_attempt"}


def test_record_cli_bounds_raw_input_and_pathological_json(tmp_path, monkeypatch, capsys):
    payload = tmp_path / "attempt.json"
    ledger = tmp_path / "ledger"
    for content in (
        b"{" + b" " * (128 * 1024) + b"}",
        ('{"number":' + ("9" * 5000) + "}").encode(),
        ("[" * 2000 + "]" * 2000).encode(),
    ):
        payload.write_bytes(content)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "record_weekly_run.py",
                "--ledger-dir",
                str(ledger),
                "--input",
                str(payload),
            ],
        )
        assert record_weekly_run.main() == 2
        assert json.loads(capsys.readouterr().err) == {
            "status": "error",
            "reason": "invalid_attempt",
        }


def test_compose_override_declares_external_read_only_directory():
    text = (ROOT / "docker-compose.update-status.yml").read_text(encoding="utf-8")
    assert "CLIMATE_UPDATE_STATUS_DIR: /update-status" in text
    assert "CLIMATE_UPDATE_STATUS_HOST_DIR" in text
    assert "target: /update-status" in text
    assert "read_only: true" in text
    assert "create_host_path: false" in text
    assert "/home/" not in text


def test_contract_does_not_claim_token_validation_can_detect_secrets():
    text = (ROOT / "docs" / "update-status.md").read_text(encoding="utf-8")
    assert "cannot detect secrets" in text
    assert "approved public classification" in text


def test_base_registry_and_update_status_compose_overrides_render_together(tmp_path):
    declared = yaml.safe_load(
        (ROOT / "docker-compose.update-status.yml").read_text(encoding="utf-8")
    )["services"]["wiki"]["volumes"][0]
    assert declared["type"] == "bind"
    assert declared["source"].startswith("${CLIMATE_UPDATE_STATUS_HOST_DIR:?")
    assert declared["target"] == "/update-status"
    assert declared["read_only"] is True
    assert declared["bind"] == {"create_host_path": False}

    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI is not installed")
    registry = tmp_path / "registry"
    ledger = tmp_path / "ledger"
    registry.mkdir()
    ledger.mkdir()
    environment = os.environ | {
        "CLIMATE_REGISTRY_HOST_DIR": str(registry.resolve()),
        "CLIMATE_UPDATE_STATUS_HOST_DIR": str(ledger.resolve()),
        "OPENAI_API_KEY": "",
        "RELOAD_TOKEN": "",
    }
    completed = subprocess.run(
        [
            docker,
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.registry.yml",
            "-f",
            "docker-compose.update-status.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    rendered = json.loads(completed.stdout)
    wiki = rendered["services"]["wiki"]
    mount = next(item for item in wiki["volumes"] if item["target"] == "/update-status")
    assert mount["source"] == str(ledger.resolve())
    assert mount["read_only"] is True
    assert wiki["environment"]["CLIMATE_UPDATE_STATUS_DIR"] == "/update-status"
