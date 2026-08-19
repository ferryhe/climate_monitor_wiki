from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


_BIND_SOURCE_VARIABLES = {
    "docker-compose.delivery.yml": "CLIMATE_DELIVERY_ARTIFACTS_HOST_DIR",
    "docker-compose.job-status.yml": "CLIMATE_JOB_STATUS_HOST_DIR",
    "docker-compose.registry.yml": "CLIMATE_REGISTRY_HOST_DIR",
    "docker-compose.update-status.yml": "CLIMATE_UPDATE_STATUS_HOST_DIR",
}


class ComposeBindSourceError(RuntimeError):
    """Raised before Compose can create an invalid bind source directory."""


def _compose_files(arguments: Sequence[str]) -> tuple[str, ...]:
    files: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-f", "--file"}:
            index += 1
            if index >= len(arguments):
                raise ComposeBindSourceError(
                    "docker compose preflight failed: a compose file argument is missing"
                )
            files.append(arguments[index])
        elif argument.startswith("--file=") or argument.startswith("-f="):
            files.append(argument.split("=", 1)[1])
        index += 1
    return tuple(files)


def _compose_basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def validate_bind_sources(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Path]:
    active_environment = os.environ if environment is None else environment
    variables = {
        _BIND_SOURCE_VARIABLES[name]
        for value in _compose_files(arguments)
        if (name := _compose_basename(value)) in _BIND_SOURCE_VARIABLES
    }
    validated: dict[str, Path] = {}
    for variable in sorted(variables):
        raw = active_environment.get(variable, "")
        try:
            path = Path(raw).expanduser()
        except (OSError, RuntimeError, TypeError) as exc:
            raise ComposeBindSourceError(
                f"docker compose preflight failed: {variable} is invalid"
            ) from exc
        if not raw or not path.is_absolute():
            raise ComposeBindSourceError(
                f"docker compose preflight failed: {variable} must be an absolute existing directory"
            )
        try:
            is_directory = path.is_dir()
        except OSError as exc:
            raise ComposeBindSourceError(
                f"docker compose preflight failed: {variable} is unavailable"
            ) from exc
        if not is_directory:
            raise ComposeBindSourceError(
                f"docker compose preflight failed: {variable} must be an absolute existing directory"
            )
        validated[variable] = path
    return validated


def run_compose(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    active_environment = dict(os.environ if environment is None else environment)
    validate_bind_sources(arguments, environment=active_environment)
    docker = shutil.which("docker", path=active_environment.get("PATH"))
    if not docker:
        raise ComposeBindSourceError(
            "docker compose preflight failed: Docker CLI is unavailable"
        )
    completed = subprocess.run(
        [docker, "compose", *arguments],
        env=active_environment,
        check=False,
    )
    return completed.returncode


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        return run_compose(sys.argv[1:] if arguments is None else arguments)
    except ComposeBindSourceError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
