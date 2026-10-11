"""Render real Compose models without a Docker daemon or production credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / name for name in (
    "docker-compose.yml", "docker-compose.registry.yml", "docker-compose.registry-import.yml")]


def _environment():
    return {key: value for key, value in os.environ.items()
            if not key.startswith(("COMPOSE_", "CLIMATE_"))
            and key not in {"TZ", "HERMES_TIMEZONE", "SITE_HOST", "PUBLIC_HOST", "RELOAD_TOKEN"}}


def _env_file(tmp_path, extra=""):
    path = tmp_path / "local.env"
    path.write_text(
        "SITE_HOST=internal.example\nPUBLIC_HOST=public.example\nRELOAD_TOKEN=test-only\n"
        f"CLIMATE_REGISTRY_HOST_DIR={tmp_path.as_posix()}\n"
        f"CLIMATE_PDF_QUEUE_HOST_DIR={tmp_path.as_posix()}\n"
        f"CLIMATE_RUNTIME_WIKI_HOST_DIR={tmp_path.as_posix()}\n" + extra)
    return path


def _docker():
    executable = shutil.which("docker")
    if not executable:
        pytest.skip("Docker Compose CLI is not installed")
    return executable


def _model(env_file, project, *extra_files):
    arguments = [_docker(), "compose", "--env-file", str(env_file), "-p", project]
    for path in [*FILES, *extra_files]:
        arguments += ["-f", str(path)]
    result = subprocess.run(arguments + ["config", "--format", "json"],
                            env=_environment(), capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_compose_projects_isolate_resources_and_forward_local_timezones(tmp_path):
    env_file = _env_file(tmp_path, "TZ=UTC\nHERMES_TIMEZONE=UTC\nCLIMATE_SCHEDULE=weekly-utc\n")
    models = [_model(env_file, name) for name in ("portable-a", "portable-b")]
    for name, model in zip(("portable-a", "portable-b"), models):
        for service in ("wiki", "caddy", "pdf-intake-writer"):
            assert "container_name" not in model["services"][service]
        assert model["services"]["wiki"]["image"] == f"{name}-wiki:local"
        assert model["services"]["pdf-intake-writer"]["image"] == f"{name}-wiki:local"
        assert model["volumes"]["web_listening_runtime"]["name"] == f"{name}_web_listening_runtime"
        environment = model["services"]["wiki"]["environment"]
        assert (environment["TZ"], environment["HERMES_TIMEZONE"], environment["CLIMATE_SCHEDULE"]) == (
            "UTC", "UTC", "weekly-utc")
        mounts = {entry["target"]: entry for entry in model["services"]["wiki"]["volumes"]}
        assert all(mounts[target]["read_only"] for target in ("/app/wiki", "/app/sources", "/app/article_metadata"))
    assert models[0]["volumes"]["climate_runtime"]["name"] != models[1]["volumes"]["climate_runtime"]["name"]


def test_existing_external_volume_and_image_are_retained_by_local_override(tmp_path):
    env_file = _env_file(tmp_path, "CLIMATE_WIKI_IMAGE=retained-deployment:local\n")
    override = tmp_path / "retained.yml"
    override.write_text("volumes:\n  web_listening_runtime:\n    external: true\n    name: retained-acquisition-data\n")
    model = _model(env_file, "portable-new", override)
    assert model["volumes"]["web_listening_runtime"] == {
        "name": "retained-acquisition-data", "external": True}
    assert model["services"]["wiki"]["image"] == model["services"]["pdf-intake-writer"]["image"] == "retained-deployment:local"


@pytest.mark.skipif(os.name == "nt", reason="Hermes wrapper requires Bash on its Linux host")
@pytest.mark.parametrize("selection_source", ["env-file", "shell"])
def test_information_check_uses_complete_compose_selection_and_stdin(tmp_path, selection_source):
    docker = _docker()
    selected_files = ":".join(str(path) for path in FILES)
    selections = {"COMPOSE_FILE": selected_files, "COMPOSE_PROJECT_NAME": "portable-check"}
    env_file = _env_file(tmp_path, "TYPESAFE_API_KEY=stdin-test-only\n" + (
        "" if selection_source == "shell" else "".join(f"{key}={value}\n" for key, value in selections.items())))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    receipt = tmp_path / "receipt.json"
    # The sudo stand-in records dispatch and renders config, never runs a container.
    sudo = bin_dir / "sudo"
    sudo.write_text(f"#!{sys.executable}\n" + '''import json, os, subprocess, sys
args = sys.argv[1:]
assert args[:3] == ["-n", "docker", "compose"]
index = args.index("exec")
assert args[index:index+5] == ["exec", "-T", "pdf-intake-writer", "python", "-c"]
credential = sys.stdin.read()
result = subprocess.run([os.environ["TEST_DOCKER"], "compose", *args[3:index], "config", "--format", "json"], capture_output=True, text=True)
if result.returncode:
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)
with open(os.environ["TEST_RECEIPT"], "w") as output:
    json.dump({"model": json.loads(result.stdout), "args": args, "stdin": credential}, output)
''')
    sudo.chmod(0o700)
    environment = {**_environment(), "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                   "TEST_DOCKER": docker, "TEST_RECEIPT": str(receipt), "CLIMATE_WIKI_ENV_FILE": str(env_file)}
    if selection_source == "shell":
        environment.update(selections)
    result = subprocess.run(["bash", str(ROOT / "scripts/hermes_job_information_check.sh"), "--preflight"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(receipt.read_text())
    assert data["model"]["name"] == "portable-check"
    assert "pdf-intake-writer" in data["model"]["services"]
    assert "TYPESAFE_API_KEY=stdin-test-only" in data["stdin"]
    assert "stdin-test-only" not in " ".join(data["args"]) + result.stdout + result.stderr


@pytest.mark.skipif(os.name == "nt", reason="Hermes wrapper requires Bash")
def test_information_check_missing_env_fails_before_docker(tmp_path):
    result = subprocess.run(["bash", str(ROOT / "scripts/hermes_job_information_check.sh"), "--preflight"],
        env={**_environment(), "CLIMATE_WIKI_ENV_FILE": str(tmp_path / "missing.env")},
        capture_output=True, text=True)
    assert result.returncode == 2
    assert "requires the configured env file" in result.stderr
