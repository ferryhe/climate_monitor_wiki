"""Negative-path coverage for the Caddy PUBLIC_HOST/SITE_HOST env var wiring.

Both PUBLIC_HOST and SITE_HOST drive a host match in the Caddyfile
(public site block and internal-IP health-check block, respectively).
Leaving either unset or empty must never degrade into a catch-all route
that answers for an arbitrary Host header — that would let a client with
a matching DNS/SNI serve traffic through this deployment under any
hostname, including one it never should (e.g. issuing a same-origin
redirect to an attacker-controlled domain). See PR #145 review for the
reproduction this guards against.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GUARDED_VARS = ("PUBLIC_HOST", "SITE_HOST")


def _compose_env() -> dict[str, str]:
    environment = os.environ | {"OPENAI_API_KEY": "", "RELOAD_TOKEN": "x" * 32}
    for var in GUARDED_VARS:
        environment.pop(var, None)
    return environment


def _env_file(tmp_path: Path, **values: str | None) -> Path:
    """Build a standalone .env file docker compose reads instead of the repo's.

    docker compose auto-loads ./.env from the project directory regardless of
    what is passed via subprocess env=; a real production .env on this host
    always has both guarded vars set, so relying on env= alone silently
    no-ops these tests. --env-file must point at a file that actually omits
    (or empties) the variable under test.
    """
    lines = ["OPENAI_API_KEY=", "RELOAD_TOKEN=" + "x" * 32]
    for key, value in values.items():
        if value is not None:
            lines.append(f"{key}={value}")
    env_file = tmp_path / ".env.test"
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_file


def _run_compose_config(env_file: Path):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI is not installed")
    return subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            "docker-compose.yml",
            "config",
            "--quiet",
        ],
        cwd=ROOT,
        env=_compose_env(),
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("guarded_var", GUARDED_VARS)
def test_compose_rejects_missing_var(tmp_path, guarded_var):
    other_var = next(v for v in GUARDED_VARS if v != guarded_var)
    env_file = _env_file(tmp_path, **{other_var: "placeholder.example"})
    completed = _run_compose_config(env_file)
    assert completed.returncode != 0
    assert guarded_var in completed.stderr


@pytest.mark.parametrize("guarded_var", GUARDED_VARS)
def test_compose_rejects_empty_var(tmp_path, guarded_var):
    other_var = next(v for v in GUARDED_VARS if v != guarded_var)
    env_file = _env_file(tmp_path, **{guarded_var: "", other_var: "placeholder.example"})
    completed = _run_compose_config(env_file)
    assert completed.returncode != 0
    assert guarded_var in completed.stderr


@pytest.mark.parametrize("guarded_var", GUARDED_VARS)
def test_docker_compose_yml_requires_var(guarded_var):
    base = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert f"${{{guarded_var}:?" in base


@pytest.mark.parametrize("guarded_var", GUARDED_VARS)
def test_empty_var_fails_closed_at_the_raw_caddyfile_level(guarded_var):
    """Confirm the failure mode for each var when the compose guard is bypassed.

    This directly adapts the Caddyfile (bypassing docker-compose's own
    required-variable check, tested above) to characterize what happens if
    that guard is ever accidentally removed or worked around:

    - PUBLIC_HOST empty: the host-match condition is dropped entirely,
      producing an unconditional (catch-all) route that answers for ANY
      Host header. This is the original PR #145 review finding.
    - SITE_HOST empty: `default_sni {$SITE_HOST}` itself fails to parse
      (the directive requires a non-empty argument), so the whole
      Caddyfile is rejected outright — a stricter, safer failure mode than
      PUBLIC_HOST's, but still exercised here so a future refactor that
      changes this behavior is caught.

    Either way, the docker-compose.yml `${VAR:?...}` guard (see
    test_docker_compose_yml_requires_var) is the actual defense in
    production and must never be removed, regardless of how the
    raw-Caddyfile failure mode behaves.
    """
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI is not installed")
    other_var = next(v for v in GUARDED_VARS if v != guarded_var)
    completed = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "-e",
            f"{guarded_var}=",
            "-e",
            f"{other_var}=placeholder.example",
            "-v",
            f"{ROOT / 'Caddyfile'}:/etc/caddy/Caddyfile:ro",
            "caddy:2-alpine",
            "caddy",
            "adapt",
            "--config",
            "/etc/caddy/Caddyfile",
            "--adapter",
            "caddyfile",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if guarded_var == "SITE_HOST":
        # default_sni requires a non-empty argument, so the whole config is
        # rejected — fails closed, no catch-all is even reachable.
        assert completed.returncode != 0
        assert "default_sni" in completed.stderr
        return

    assert completed.returncode == 0, completed.stderr
    adapted = json.loads(completed.stdout)
    servers = adapted["apps"]["http"]["servers"]
    unmatched_routes = [
        route
        for server in servers.values()
        for route in server.get("routes", ())
        if not route.get("match")
    ]
    assert unmatched_routes, (
        f"expected an unconditional (no-match) route with {guarded_var} "
        "empty at the raw Caddyfile level — if this assertion now fails, "
        "Caddy's behavior changed for the better, but the compose-level "
        "guard above is still the real defense and must stay in place "
        "regardless"
    )


@pytest.mark.parametrize("guarded_var", GUARDED_VARS)
def test_required_host_is_documented_in_env_example(guarded_var):
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert f"{guarded_var}=" in lines
