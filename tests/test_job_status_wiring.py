from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_job_status_override_declares_external_read_only_directory():
    text = (ROOT / "docker-compose.job-status.yml").read_text(encoding="utf-8")
    assert "CLIMATE_JOB_STATUS_DIR: /job-status" in text
    assert "CLIMATE_JOB_STATUS_HOST_DIR" in text
    assert "target: /job-status" in text
    assert "read_only: true" in text
    assert "create_host_path: false" in text
    assert "/home/" not in text


def test_job_status_docs_keep_exporter_and_hermes_database_out_of_public_runtime():
    from climate_monitor.job_status import validate_snapshot

    text = (ROOT / "docs" / "job-status.md").read_text(encoding="utf-8")
    contract = text.split("## Contract", 1)[1]
    example = json.loads(contract.split("```json", 1)[1].split("```", 1)[0])
    assert example["schema_version"] == "biweekly-job-status.v1"
    assert set(example["jobs"]) == {"monitor", "email", "publisher", "registry"}
    assert validate_snapshot(
        example, now=datetime(2026, 9, 14, 12, 5, tzinfo=timezone.utc),
    )["jobs"] == example["jobs"]
    assert "weekly-job-status.v1" in contract.split("compatibility input", 1)[0]
    assert "15 minutes" in text
    assert "Do not mount" in text
    assert "Hermes" in text
    assert "exporter" in text
    assert "installation" in text.lower()
    assert "unperformed" in text
    assert "Caddy" in text
    assert "systemd" in text


def test_all_optional_read_only_overrides_render_together(tmp_path):
    declared = yaml.safe_load(
        (ROOT / "docker-compose.job-status.yml").read_text(encoding="utf-8")
    )["services"]["wiki"]["volumes"][0]
    assert declared["type"] == "bind"
    assert declared["source"].startswith("${CLIMATE_JOB_STATUS_HOST_DIR:?")
    assert declared["target"] == "/job-status"
    assert declared["read_only"] is True
    assert declared["bind"] == {"create_host_path": False}

    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI is not installed")
    registry = tmp_path / "registry"
    ledger = tmp_path / "ledger"
    job_status = tmp_path / "job-status"
    for directory in (registry, ledger, job_status):
        directory.mkdir()
    environment = os.environ | {
        "SITE_HOST": "127.0.0.1", "PUBLIC_HOST": "public.example.test",
        "CLIMATE_REGISTRY_HOST_DIR": str(registry.resolve()),
        "CLIMATE_UPDATE_STATUS_HOST_DIR": str(ledger.resolve()),
        "CLIMATE_JOB_STATUS_HOST_DIR": str(job_status.resolve()),
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
            "-f",
            "docker-compose.job-status.yml",
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
    mount = next(item for item in wiki["volumes"] if item["target"] == "/job-status")
    assert mount["type"] == "bind"
    assert mount["source"] == str(job_status.resolve())
    assert mount["read_only"] is True
    assert wiki["environment"]["CLIMATE_JOB_STATUS_DIR"] == "/job-status"
