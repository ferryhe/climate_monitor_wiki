from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _docker_config(*files: str, environment: dict[str, str]) -> dict:
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI is not installed")
    command = [docker, "compose"]
    for filename in files:
        command.extend(("-f", filename))
    command.extend(("config", "--format", "json"))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_base_compose_has_no_delivery_artifact_dependency():
    base = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "CLIMATE_DELIVERY_OUTPUT_DIR" not in base
    assert "CLIMATE_DELIVERY_ARTIFACTS_HOST_DIR" not in base
    assert "/delivery-output" not in base


def test_delivery_override_is_required_external_read_only_bind():
    override = (ROOT / "docker-compose.delivery.yml").read_text(encoding="utf-8")
    assert "CLIMATE_DELIVERY_OUTPUT_DIR: /delivery-output" in override
    assert "${CLIMATE_DELIVERY_ARTIFACTS_HOST_DIR:?" in override
    assert "target: /delivery-output" in override
    assert "read_only: true" in override
    assert "create_host_path: false" in override
    assert "/home/" not in override
    assert "config" not in override.casefold()
    assert "state" not in override.casefold()
    assert "recipient" not in override.casefold()


def test_base_and_delivery_override_render_independently(tmp_path):
    declared = yaml.safe_load(
        (ROOT / "docker-compose.delivery.yml").read_text(encoding="utf-8")
    )["services"]["wiki"]["volumes"][0]
    assert declared["type"] == "bind"
    assert declared["source"].startswith("${CLIMATE_DELIVERY_ARTIFACTS_HOST_DIR:?")
    assert declared["target"] == "/delivery-output"
    assert declared["read_only"] is True
    assert declared["bind"] == {"create_host_path": False}

    environment = os.environ | {"OPENAI_API_KEY": "", "RELOAD_TOKEN": ""}
    base = _docker_config("docker-compose.yml", environment=environment)
    assert all(item.get("target") != "/delivery-output" for item in base["services"]["wiki"]["volumes"])

    host_output = tmp_path / "existing-delivery-output"
    host_output.mkdir()
    rendered = _docker_config(
        "docker-compose.yml",
        "docker-compose.delivery.yml",
        environment=environment | {"CLIMATE_DELIVERY_ARTIFACTS_HOST_DIR": str(host_output.resolve())},
    )
    service = rendered["services"]["wiki"]
    assert service["environment"]["CLIMATE_DELIVERY_OUTPUT_DIR"] == "/delivery-output"
    mount = next(item for item in service["volumes"] if item["target"] == "/delivery-output")
    assert mount["type"] == "bind"
    assert mount["source"] == str(host_output.resolve())
    assert mount["read_only"] is True


def test_delivery_override_rejects_missing_host_variable():
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI is not installed")
    environment = os.environ | {"OPENAI_API_KEY": "", "RELOAD_TOKEN": ""}
    environment.pop("CLIMATE_DELIVERY_ARTIFACTS_HOST_DIR", None)
    completed = subprocess.run(
        [
            docker,
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.delivery.yml",
            "config",
            "--quiet",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
