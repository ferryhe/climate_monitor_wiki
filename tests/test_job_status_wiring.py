from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_job_status_override_declares_external_read_only_directory():
    text = (ROOT / "docker-compose.job-status.yml").read_text(encoding="utf-8")
    assert "CLIMATE_JOB_STATUS_DIR: /job-status" in text
    assert "CLIMATE_JOB_STATUS_HOST_DIR" in text
    assert "target: /job-status" in text
    assert "read_only: true" in text
    assert "create_host_path: false" in text
    assert "/home/" not in text


def test_job_status_docs_keep_exporter_and_hermes_database_out_of_scope():
    text = (ROOT / "docs" / "job-status.md").read_text(encoding="utf-8")
    assert "weekly-job-status.v1" in text
    assert "15 minutes" in text
    assert "Do not mount" in text
    assert "Hermes" in text
    assert "exporter" in text
    assert "deferred" in text
    assert "Caddy" in text
    assert "systemd" in text


def test_all_optional_read_only_overrides_render_together(tmp_path):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI is not installed")
    registry = tmp_path / "registry"
    ledger = tmp_path / "ledger"
    job_status = tmp_path / "job-status"
    for directory in (registry, ledger, job_status):
        directory.mkdir()
    environment = os.environ | {
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
    assert mount["source"] == str(job_status.resolve())
    assert mount["read_only"] is True
    assert mount["bind"]["create_host_path"] is False
    assert wiki["environment"]["CLIMATE_JOB_STATUS_DIR"] == "/job-status"
