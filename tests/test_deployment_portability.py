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
@pytest.mark.parametrize("selection_source", ["env-file", "shell", "mixed-env-separator", "shell-separator-override", "env-files-shell-separator", "env-files-shell-interpolation", "file-defined-project", "relative-env-files"])
def test_information_check_uses_complete_compose_selection_and_stdin(tmp_path, selection_source):
    docker = _docker()
    selected_files = (";" if selection_source in {"mixed-env-separator", "env-files-shell-separator"} else ":").join(str(path) for path in FILES)
    selections = {"COMPOSE_FILE": selected_files, "COMPOSE_PROJECT_NAME": "portable-check"}
    local_selections = selections if selection_source in {"env-file", "env-files-shell-separator"} else {}
    if selection_source in {"mixed-env-separator", "shell-separator-override"}:
        local_selections = {"COMPOSE_PATH_SEPARATOR": ";"}
    if selection_source == "env-files-shell-interpolation":
        local_selections = {
            "COMPOSE_FILE": ":".join("${CLIMATE_LOCAL_COMPOSE_ROOT}/" + path.name for path in FILES),
            "COMPOSE_PROJECT_NAME": "${CLIMATE_LOCAL_PROJECT}",
        }
    if selection_source == "file-defined-project":
        override = tmp_path / "named.yml"
        override.write_text("name: portable-check\n")
        local_selections = {"COMPOSE_FILE": ":".join(str(path) for path in [*FILES, override])}
    if selection_source == "relative-env-files":
        local_selections = {"COMPOSE_FILE": ":".join(path.name for path in FILES),
                            "COMPOSE_PROJECT_NAME": "portable-check"}
    env_file = _env_file(tmp_path, "TYPESAFE_API_KEY=stdin-test-only\n" +
        "".join(f"{key}={value}\n" for key, value in local_selections.items()))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    receipt = tmp_path / "receipt.json"
    # The sudo stand-in records dispatch and renders config, never runs a container.
    sudo = bin_dir / "sudo"
    sudo.write_text(f"#!{sys.executable}\n" + '''import json, os, subprocess, sys
args = sys.argv[1:]
assert args[:3] == ["-n", "docker", "compose"]
clean = {key: value for key, value in os.environ.items() if not key.startswith(("COMPOSE_", "CLIMATE_"))}
if "exec" not in args:
    result = subprocess.run([os.environ["TEST_DOCKER"], "compose", *args[3:]], env=clean,
                            input=sys.stdin.read(), capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)
index = args.index("exec")
assert args[index:index+5] == ["exec", "-T", "pdf-intake-writer", "python", "-c"]
credential = sys.stdin.read()
result = subprocess.run([os.environ["TEST_DOCKER"], "compose", *args[3:index], "config", "--format", "json"], env=clean, capture_output=True, text=True)
if result.returncode:
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)
with open(os.environ["TEST_RECEIPT"], "w") as output:
    json.dump({"model": json.loads(result.stdout), "args": args, "stdin": credential}, output)
''')
    sudo.chmod(0o700)
    environment = {**_environment(), "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                   "TEST_DOCKER": docker, "TEST_RECEIPT": str(receipt), "CLIMATE_WIKI_ENV_FILE": str(env_file)}
    if selection_source not in {"env-file", "env-files-shell-interpolation", "file-defined-project", "relative-env-files"}:
        environment["COMPOSE_PROJECT_NAME"] = selections["COMPOSE_PROJECT_NAME"]
        if selection_source != "env-files-shell-separator":
            environment["COMPOSE_FILE"] = selected_files
    if selection_source == "shell-separator-override":
        environment["COMPOSE_PATH_SEPARATOR"] = ":"
    if selection_source == "env-files-shell-separator":
        environment["COMPOSE_PATH_SEPARATOR"] = ";"
    if selection_source == "env-files-shell-interpolation":
        environment.update(CLIMATE_LOCAL_COMPOSE_ROOT=str(ROOT), CLIMATE_LOCAL_PROJECT="portable-check")
    if selection_source == "relative-env-files":
        environment["CLIMATE_WIKI_ENV_FILE"] = env_file.name
    direct = subprocess.run([docker, "compose", "--project-directory", str(ROOT), "--env-file", str(env_file),
                             "config", "--format", "json"], cwd=ROOT if selection_source == "relative-env-files" else tmp_path,
                            env=environment, capture_output=True, text=True)
    assert direct.returncode == 0, direct.stderr
    result = subprocess.run(["bash", str(ROOT / "scripts/hermes_job_information_check.sh"), "--preflight"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(receipt.read_text())
    assert data["model"]["name"] == "portable-check"
    assert "pdf-intake-writer" in data["model"]["services"]
    assert data["model"] == json.loads(direct.stdout)
    assert "TYPESAFE_API_KEY=stdin-test-only" in data["stdin"]
    assert "stdin-test-only" not in " ".join(data["args"]) + result.stdout + result.stderr


@pytest.mark.skipif(os.name == "nt", reason="Hermes wrapper requires Bash")
def test_information_check_missing_env_fails_before_docker(tmp_path):
    result = subprocess.run(["bash", str(ROOT / "scripts/hermes_job_information_check.sh"), "--preflight"],
        env={**_environment(), "CLIMATE_WIKI_ENV_FILE": str(tmp_path / "missing.env")},
        capture_output=True, text=True)
    assert result.returncode == 2
    assert "requires the configured env file" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="The documented sudo Docker entrypoint requires Linux")
def test_documented_sudo_entrypoint_preserves_safe_compose_preflight_and_recreate(tmp_path):
    docker = _docker()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    trace = tmp_path / "trace.jsonl"
    original = bin_dir / "docker"
    original.write_text(f"#!{sys.executable}\n" + '''import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["TEST_TRACE"], "a") as output:
    output.write(json.dumps({"args": args, "sudo": os.getenv("TEST_DOCKER_ACCESS") == "allowed"}) + "\\n")
if "up" in args or "build" in args:
    if os.getenv("TEST_DOCKER_ACCESS") != "allowed":
        sys.stderr.write("daemon permission denied: existing sudo Docker permission required\\n")
        raise SystemExit(13)
    raise SystemExit(0)  # dispatch receipt only; no daemon or containers
raise SystemExit(subprocess.call([os.environ["TEST_DOCKER"], *args]))
''')
    original.chmod(0o700)
    sudo = bin_dir / "sudo"
    sudo.write_text(f"#!{sys.executable}\n" + '''import os, sys
assert sys.argv[1:3] == ["-n", os.environ["TEST_ORIGINAL"]]
for name in list(os.environ):
    if name.startswith(("COMPOSE_", "CLIMATE_")):
        del os.environ[name]
os.environ["TEST_DOCKER_ACCESS"] = "allowed"
os.execv(sys.argv[2], sys.argv[2:])
''')
    sudo.chmod(0o700)
    env_file = _env_file(tmp_path, "COMPOSE_FILE=" + ":".join(str(path) for path in FILES) + "\n")
    selection = ["--project-directory", str(ROOT), "--env-file", str(env_file), "-p", "sudo-portable"]
    environment = {**_environment(), "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                   "TEST_TRACE": str(trace), "TEST_ORIGINAL": str(original), "TEST_DOCKER": docker}
    helper = [sys.executable, "-m", "scripts.safe_compose", *selection]
    config = subprocess.run([*helper, "config", "--quiet"], cwd=ROOT, env=environment, capture_output=True, text=True)
    assert config.returncode == 0, config.stderr  # config itself needs no daemon access
    denied = subprocess.run([*helper, "up", "-d", "--no-build", "--force-recreate", "wiki", "caddy"],
                            cwd=ROOT, env=environment, capture_output=True, text=True)
    assert denied.returncode == 13 and "daemon permission denied" in denied.stderr
    text = (ROOT / "docs/deployment.md").read_text()
    entrypoint = text.split("For operators whose existing permission is only", 1)[1].split("```bash\n", 1)[1].split("```", 1)[0]
    installed = subprocess.run(["bash", "-c", entrypoint + '\ntest "$(command -v docker)" = "$docker_real"\n'],
                               cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert installed.returncode == 0, installed.stderr
    environment["PATH"] = str(tmp_path / "external/deployment/docker-cli") + os.pathsep + environment["PATH"]
    trace.write_text("")
    for command in (["config", "--quiet"], ["build", "--build-arg", "CLIMATE_REPOSITORY_COMMIT_SHA=" + "a" * 40, "wiki"],
                    ["up", "-d", "--no-build", "--force-recreate", "wiki", "caddy"]):
        result = subprocess.run([*helper, *command], cwd=ROOT, env=environment, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    assert all(call["sudo"] for call in calls)
    assert sum("config" in call["args"] for call in calls) == 2  # explicit config plus safe_compose's real model preflight
    assert sum("up" in call["args"] for call in calls) == 1
    assert all(call["args"][:1] == ["compose"] and call["args"][1:1 + len(selection)] == selection for call in calls)
