from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any


_CREATING_COMMANDS = {"create", "up"}
_PASSTHROUGH_COMMANDS = {
    "build",
    "config",
    "cp",
    "down",
    "events",
    "exec",
    "images",
    "kill",
    "logs",
    "ls",
    "pause",
    "port",
    "ps",
    "pull",
    "restart",
    "rm",
    "start",
    "stats",
    "stop",
    "top",
    "unpause",
    "version",
    "wait",
}
_GLOBAL_VALUE_OPTIONS = {
    "--ansi",
    "--env-file",
    "--file",
    "--parallel",
    "--profile",
    "--progress",
    "--project-directory",
    "--project-name",
    "-f",
    "-p",
}
_GLOBAL_FLAG_OPTIONS = {"--all-resources", "--compatibility", "--dry-run"}
_TERMINAL_GLOBAL_OPTIONS = {"--help", "--version", "-h"}
_PROTECTED_TARGETS = {
    "/delivery-output",
    "/job-status",
    "/registry",
    "/update-status",
}
_USAGE_ERROR = "docker compose wrapper usage error: unknown or missing subcommand"


class ComposeBindSourceError(RuntimeError):
    """Raised before Compose can create an invalid protected bind source."""

    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class _Command:
    name: str | None
    index: int | None
    requires_preflight: bool


def _usage_error() -> ComposeBindSourceError:
    return ComposeBindSourceError(_USAGE_ERROR)


def _classify_command(arguments: Sequence[str]) -> _Command:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            raise _usage_error()
        if argument in _TERMINAL_GLOBAL_OPTIONS:
            return _Command(name=None, index=None, requires_preflight=False)
        if argument in _GLOBAL_FLAG_OPTIONS:
            index += 1
            continue
        if argument in _GLOBAL_VALUE_OPTIONS:
            index += 2
            if index > len(arguments):
                raise _usage_error()
            continue
        if any(
            argument.startswith(f"{option}=")
            for option in _GLOBAL_VALUE_OPTIONS
            if option.startswith("--")
        ):
            index += 1
            continue
        if (
            (argument.startswith("-f") and argument != "-f")
            or (argument.startswith("-p") and argument != "-p")
        ):
            index += 1
            continue
        if argument.startswith("-"):
            raise _usage_error()
        break

    if index >= len(arguments):
        raise _usage_error()
    command = arguments[index]
    if command in _CREATING_COMMANDS:
        return _Command(name=command, index=index, requires_preflight=True)
    if command in _PASSTHROUGH_COMMANDS:
        return _Command(name=command, index=index, requires_preflight=False)
    raise _usage_error()


def _normalize_exit_code(returncode: int) -> int:
    if returncode < 0:
        return 128 + abs(returncode)
    return returncode


def _load_final_model(
    docker: str,
    arguments: Sequence[str],
    command_index: int,
    *,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    config_arguments = [
        docker,
        "compose",
        *arguments[:command_index],
        "--profile",
        "*",
        "config",
        "--format",
        "json",
    ]
    completed = subprocess.run(
        config_arguments,
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode:
        raise ComposeBindSourceError(
            "docker compose preflight failed: Compose config failed",
            exit_code=_normalize_exit_code(completed.returncode),
        )
    try:
        model = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ComposeBindSourceError(
            "docker compose preflight failed: Compose config returned invalid JSON"
        ) from exc
    if not isinstance(model, dict):
        raise ComposeBindSourceError(
            "docker compose preflight failed: Compose config returned an invalid model"
        )
    return model


def _path_components(path: Path):
    current = Path(path.anchor)
    yield current
    for part in path.parts[1:]:
        current = current / part
        yield current


def _is_link_or_reparse(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(file_attributes & reparse_attribute)


def _validate_source_directory(path: Path, *, target: str) -> None:
    if not path.is_absolute():
        raise ComposeBindSourceError(
            f"docker compose preflight failed: protected mount {target} "
            "must use an absolute existing directory"
        )
    try:
        components = tuple(_path_components(path))
        metadata = [os.lstat(component) for component in components]
    except (OSError, ValueError) as exc:
        raise ComposeBindSourceError(
            f"docker compose preflight failed: protected mount {target} "
            "must use an absolute existing directory"
        ) from exc
    if any(_is_link_or_reparse(item) for item in metadata):
        raise ComposeBindSourceError(
            f"docker compose preflight failed: protected mount {target} "
            "contains a link or reparse point"
        )
    if not all(stat.S_ISDIR(item.st_mode) for item in metadata):
        raise ComposeBindSourceError(
            f"docker compose preflight failed: protected mount {target} "
            "must use an absolute existing directory"
        )


def _validate_final_model(model: Mapping[str, Any]) -> None:
    services = model.get("services")
    if not isinstance(services, Mapping):
        raise ComposeBindSourceError(
            "docker compose preflight failed: Compose config returned an invalid model"
        )
    wiki = services.get("wiki")
    if wiki is None:
        return
    if not isinstance(wiki, Mapping):
        raise ComposeBindSourceError(
            "docker compose preflight failed: Compose config returned an invalid model"
        )
    volumes = wiki.get("volumes", [])
    if not isinstance(volumes, list) or not all(
        isinstance(mount, Mapping) for mount in volumes
    ):
        raise ComposeBindSourceError(
            "docker compose preflight failed: Compose config returned an invalid model"
        )

    for target in sorted(_PROTECTED_TARGETS):
        matches = [mount for mount in volumes if mount.get("target") == target]
        if len(matches) > 1:
            raise ComposeBindSourceError(
                f"docker compose preflight failed: protected mount {target} must be unique"
            )
        if not matches:
            continue
        mount = matches[0]
        if mount.get("type") != "bind" or mount.get("read_only") is not True:
            raise ComposeBindSourceError(
                f"docker compose preflight failed: protected mount {target} "
                "must be a read-only bind"
            )
        source = mount.get("source")
        if not isinstance(source, str) or not source:
            raise ComposeBindSourceError(
                f"docker compose preflight failed: protected mount {target} "
                "must use an absolute existing directory"
            )
        _validate_source_directory(Path(source), target=target)


def _run_actual_compose(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
) -> int:
    process = subprocess.Popen(
        list(command),
        env=dict(environment),
        shell=False,
    )
    previous_handlers: dict[int, Any] = {}

    def forward_signal(signum: int, _frame: FrameType | None) -> None:
        if process.poll() is None:
            process.send_signal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.signal(signum, forward_signal)
        except (OSError, ValueError):
            continue
    try:
        try:
            returncode = process.wait()
        except KeyboardInterrupt:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                returncode = process.wait()
            except KeyboardInterrupt:
                return 128 + signal.SIGINT
        return _normalize_exit_code(returncode)
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass


def run_compose(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    command = _classify_command(arguments)
    active_environment = dict(os.environ if environment is None else environment)
    docker = shutil.which("docker", path=active_environment.get("PATH"))
    if not docker:
        raise ComposeBindSourceError(
            "docker compose preflight failed: Docker CLI is unavailable"
        )

    if command.requires_preflight:
        assert command.index is not None
        model = _load_final_model(
            docker,
            arguments,
            command.index,
            environment=active_environment,
        )
        _validate_final_model(model)

    return _run_actual_compose(
        [docker, "compose", *arguments],
        environment=active_environment,
    )


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        return run_compose(sys.argv[1:] if arguments is None else arguments)
    except ComposeBindSourceError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 128 + signal.SIGINT


if __name__ == "__main__":
    raise SystemExit(main())
