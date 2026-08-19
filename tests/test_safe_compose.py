from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from scripts import safe_compose


ROOT = Path(__file__).resolve().parents[1]
REAL_SUBPROCESS_RUN = subprocess.run
PROTECTED_TARGETS = (
    "/registry",
    "/delivery-output",
    "/update-status",
    "/job-status",
)


class FakeProcess:
    def __init__(self, returncodes: int | BaseException | list[int | BaseException]):
        self._returncodes = (
            list(returncodes) if isinstance(returncodes, list) else [returncodes]
        )
        self.returncode: int | None = None
        self.signals: list[int] = []

    def poll(self):
        return self.returncode

    def send_signal(self, signum):
        self.signals.append(signum)

    def wait(self):
        outcome = self._returncodes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        self.returncode = outcome
        return outcome


def _model(*mounts: dict[str, object]) -> dict[str, object]:
    return {"services": {"wiki": {"volumes": list(mounts)}}}


def _bind(source: Path | str, target: str = "/registry") -> dict[str, object]:
    return {
        "type": "bind",
        "source": str(source),
        "target": target,
        "read_only": True,
    }


def _stub_processes(
    monkeypatch,
    *,
    model: dict[str, object] | None = None,
    config_returncode: int = 0,
    config_stdout: str | None = None,
    config_stderr: str = "",
    actual_returncodes: int | BaseException | list[int | BaseException] = 0,
):
    config_calls: list[tuple[list[str], dict[str, object]]] = []
    actual_calls: list[tuple[list[str], dict[str, object], FakeProcess]] = []

    monkeypatch.setattr(safe_compose.shutil, "which", lambda *args, **kwargs: "docker")

    def fake_run(command, **kwargs):
        config_calls.append((command, kwargs))
        stdout = config_stdout
        if stdout is None:
            stdout = json.dumps(_model() if model is None else model)
        return subprocess.CompletedProcess(
            command,
            config_returncode,
            stdout=stdout,
            stderr=config_stderr,
        )

    def fake_popen(command, **kwargs):
        process = FakeProcess(actual_returncodes)
        actual_calls.append((command, kwargs, process))
        return process

    monkeypatch.setattr(safe_compose.subprocess, "run", fake_run)
    monkeypatch.setattr(safe_compose.subprocess, "Popen", fake_popen)
    return config_calls, actual_calls


@pytest.mark.parametrize("command", ["up", "create"])
def test_creating_commands_validate_the_final_compose_model(monkeypatch, command):
    config_calls, actual_calls = _stub_processes(monkeypatch)
    arguments = ["--profile", "up", command, "--example"]

    assert safe_compose.run_compose(arguments) == 0
    assert [call[0] for call in config_calls] == [
        [
            "docker",
            "compose",
            "--profile",
            "up",
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ]
    ]
    assert config_calls[0][1]["shell"] is False
    assert [call[0] for call in actual_calls] == [
        ["docker", "compose", *arguments]
    ]


def test_global_option_values_are_not_mistaken_for_subcommands(monkeypatch):
    config_calls, actual_calls = _stub_processes(monkeypatch)
    arguments = [
        "-f",
        "up",
        "--file=run",
        "--env-file",
        "watch",
        "--project-directory",
        "create",
        "--profile",
        "run",
        "--profile=watch",
        "up",
        "-d",
    ]

    assert safe_compose.run_compose(arguments) == 0
    assert config_calls[0][0] == [
        "docker",
        "compose",
        *arguments[:-2],
        "--profile",
        "*",
        "config",
        "--format",
        "json",
    ]
    assert actual_calls[0][0] == ["docker", "compose", *arguments]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--"],
        ["--", "help", "up"],
        ["--", "up"],
        ["help", "up"],
        ["--", "run", "wiki"],
    ],
)
def test_pre_subcommand_terminator_and_help_fail_closed(
    monkeypatch, capsys, arguments
):
    config_calls, actual_calls = _stub_processes(monkeypatch)

    assert safe_compose.main(arguments) == 2
    assert capsys.readouterr().err == (
        "docker compose wrapper usage error: unknown or missing subcommand\n"
    )
    assert config_calls == []
    assert actual_calls == []


@pytest.mark.parametrize("arguments", [["up", "--", "wiki"], ["logs", "--", "wiki"]])
def test_post_subcommand_terminator_is_preserved(monkeypatch, arguments):
    config_calls, actual_calls = _stub_processes(monkeypatch)

    assert safe_compose.run_compose(arguments) == 0
    if arguments[0] == "up":
        assert config_calls[0][0] == [
            "docker",
            "compose",
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ]
    else:
        assert config_calls == []
    assert actual_calls[0][0] == ["docker", "compose", *arguments]


@pytest.mark.parametrize(
    "arguments",
    [["--help"], ["-h"], ["--version"], ["version"]],
)
def test_help_and_version_are_direct_passthrough(monkeypatch, arguments):
    config_calls, actual_calls = _stub_processes(monkeypatch)

    assert safe_compose.run_compose(arguments) == 0
    assert config_calls == []
    assert actual_calls[0][0] == ["docker", "compose", *arguments]


@pytest.mark.parametrize(
    "command",
    [
        "down",
        "stop",
        "rm",
        "ps",
        "logs",
        "config",
        "pull",
        "build",
        "images",
        "ls",
        "version",
        "top",
        "events",
        "kill",
        "pause",
        "unpause",
        "restart",
        "start",
        "exec",
        "cp",
        "port",
        "stats",
        "wait",
    ],
)
def test_noncreating_commands_do_not_require_protected_sources(monkeypatch, command):
    config_calls, actual_calls = _stub_processes(monkeypatch)
    arguments = ["-f", "missing-compose.yml", command]

    assert safe_compose.run_compose(arguments, environment={"PATH": "ignored"}) == 0
    assert config_calls == []
    assert actual_calls[0][0] == ["docker", "compose", *arguments]


@pytest.mark.parametrize(
    "arguments_template",
    [
        ["run", "wiki"],
        ["run", "-v", "{source}:/registry", "wiki"],
        ["run", "--volume", "{source}:/registry", "wiki"],
        ["run", "--volume={source}:/registry", "wiki"],
        ["run", "-v{source}:/registry", "wiki"],
        ["run", "--volume", "registry-data:/registry", "wiki"],
    ],
)
def test_run_and_volume_variants_fail_before_any_docker_command(
    monkeypatch, capsys, tmp_path, arguments_template
):
    secret = tmp_path / "private-source"
    arguments = [argument.format(source=secret) for argument in arguments_template]
    config_calls, actual_calls = _stub_processes(monkeypatch)

    assert safe_compose.main(arguments) == 2
    error = capsys.readouterr().err
    assert error == "docker compose wrapper usage error: unknown or missing subcommand\n"
    assert str(secret) not in error
    assert config_calls == []
    assert actual_calls == []


@pytest.mark.parametrize("command", ["watch", "publish", "bridge", "alpha"])
def test_unsupported_compose_subcommands_fail_closed(monkeypatch, command):
    config_calls, actual_calls = _stub_processes(monkeypatch)

    with pytest.raises(safe_compose.ComposeBindSourceError):
        safe_compose.run_compose([command])
    assert config_calls == []
    assert actual_calls == []


@pytest.mark.parametrize("arguments", [[], ["unknown"], ["--new-option", "up"]])
def test_unknown_or_unrecognizable_commands_fail_closed(
    monkeypatch, capsys, arguments
):
    config_calls, actual_calls = _stub_processes(monkeypatch)

    assert safe_compose.main(arguments) == 2
    assert capsys.readouterr().err == (
        "docker compose wrapper usage error: unknown or missing subcommand\n"
    )
    assert config_calls == []
    assert actual_calls == []


def test_config_failure_preserves_exit_code_and_sanitizes_output(
    monkeypatch, capsys, tmp_path
):
    secret = tmp_path / "private-source"
    config_calls, actual_calls = _stub_processes(
        monkeypatch,
        config_returncode=17,
        config_stderr=f"failed while reading {secret}",
    )

    assert safe_compose.main(["up"]) == 17
    error = capsys.readouterr().err
    assert error == "docker compose preflight failed: Compose config failed\n"
    assert str(secret) not in error
    assert len(config_calls) == 1
    assert actual_calls == []


def test_invalid_config_json_is_sanitized_and_blocks_the_actual_command(
    monkeypatch, tmp_path
):
    secret = tmp_path / "private-source"
    _, actual_calls = _stub_processes(
        monkeypatch,
        config_stdout=f"not JSON: {secret}",
    )

    with pytest.raises(safe_compose.ComposeBindSourceError) as raised:
        safe_compose.run_compose(["up"])
    assert str(secret) not in str(raised.value)
    assert actual_calls == []


def _write_bind_compose(
    path: Path,
    *,
    source: str,
    target: str = "/registry",
    read_only: bool = True,
    profile: str | None = None,
) -> None:
    profile_line = f"    profiles: [{profile}]\n" if profile else ""
    path.write_text(
        "services:\n"
        "  wiki:\n"
        "    image: busybox:latest\n"
        f"{profile_line}"
        "    volumes:\n"
        "      - type: bind\n"
        f"        source: {source}\n"
        f"        target: {target}\n"
        f"        read_only: {str(read_only).lower()}\n"
        "        bind:\n"
        "          create_host_path: false\n",
        encoding="utf-8",
    )


def _run_real_config_preflight(monkeypatch, arguments, environment):
    if not safe_compose.shutil.which("docker"):
        pytest.skip("Docker CLI is not installed")
    actual_calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_actual(command, *, environment):
        actual_calls.append((command, dict(environment)))
        return 0

    monkeypatch.setattr(safe_compose, "_run_actual_compose", fake_actual)
    assert safe_compose.run_compose(arguments, environment=environment) == 0
    assert actual_calls == [
        ([safe_compose.shutil.which("docker"), "compose", *arguments], environment)
    ]


def test_compose_file_and_profile_select_the_final_registry_mount(
    tmp_path, monkeypatch
):
    source = tmp_path / "registry source"
    source.mkdir()
    compose = tmp_path / "registry-only.yml"
    _write_bind_compose(
        compose,
        source="${HOST_SOURCE:?required}",
        profile="guarded",
    )
    environment = os.environ | {
        "COMPOSE_FILE": str(compose),
        "HOST_SOURCE": str(source),
    }

    _run_real_config_preflight(
        monkeypatch,
        ["--profile", "guarded", "up"],
        environment,
    )


@pytest.mark.parametrize("command", ["up", "create"])
def test_explicit_profile_hidden_wiki_cannot_bypass_protected_mount_validation(
    tmp_path, monkeypatch, command
):
    missing = tmp_path / "private-missing-source"
    compose = tmp_path / "profile-hidden.yml"
    _write_bind_compose(
        compose,
        source=str(missing),
        profile="guarded",
    )
    actual_calls: list[list[str]] = []
    monkeypatch.setattr(
        safe_compose,
        "_run_actual_compose",
        lambda compose_command, **kwargs: actual_calls.append(compose_command),
    )

    with pytest.raises(safe_compose.ComposeBindSourceError) as raised:
        safe_compose.run_compose(
            ["-f", str(compose), command, "wiki"],
            environment=dict(os.environ),
        )
    assert str(missing) not in str(raised.value)
    assert actual_calls == []


def test_env_file_is_resolved_by_compose(tmp_path, monkeypatch):
    source = tmp_path / "registry source"
    source.mkdir()
    compose = tmp_path / "compose.yml"
    _write_bind_compose(compose, source="${HOST_SOURCE:?required}")
    env_file = tmp_path / "operator.env"
    env_file.write_text(f"HOST_SOURCE={source.as_posix()}\n", encoding="utf-8")
    environment = dict(os.environ)
    environment.pop("HOST_SOURCE", None)

    _run_real_config_preflight(
        monkeypatch,
        ["--env-file", str(env_file), "-f", str(compose), "up"],
        environment,
    )


def test_dotenv_and_project_directory_are_resolved_by_compose(
    tmp_path, monkeypatch
):
    source = tmp_path / "registry source"
    source.mkdir()
    compose = tmp_path / "compose.yml"
    _write_bind_compose(compose, source="${HOST_SOURCE:?required}")
    (tmp_path / ".env").write_text(
        f"HOST_SOURCE={source.as_posix()}\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.pop("HOST_SOURCE", None)

    _run_real_config_preflight(
        monkeypatch,
        ["--project-directory", str(tmp_path), "-f", str(compose), "up"],
        environment,
    )


def test_later_compose_file_replaces_the_source_before_validation(
    tmp_path, monkeypatch
):
    missing = tmp_path / "missing-first-source"
    final_source = tmp_path / "final source"
    final_source.mkdir()
    base = tmp_path / "base.yml"
    override = tmp_path / "override.yml"
    _write_bind_compose(base, source="${FIRST_SOURCE:?required}")
    _write_bind_compose(override, source="${FINAL_SOURCE:?required}")
    environment = os.environ | {
        "FIRST_SOURCE": str(missing),
        "FINAL_SOURCE": str(final_source),
    }

    _run_real_config_preflight(
        monkeypatch,
        ["-f", str(base), "-f", str(override), "up"],
        environment,
    )


@pytest.mark.parametrize("unsafe_override", ["volume", "writable"])
def test_later_compose_file_cannot_change_protected_mount_safety(
    tmp_path, monkeypatch, unsafe_override
):
    source = tmp_path / "source"
    source.mkdir()
    base = tmp_path / "base.yml"
    override = tmp_path / "override.yml"
    _write_bind_compose(base, source=str(source))
    if unsafe_override == "volume":
        override.write_text(
            "services:\n"
            "  wiki:\n"
            "    volumes:\n"
            "      - type: volume\n"
            "        source: registry-data\n"
            "        target: /registry\n"
            "        read_only: true\n"
            "volumes:\n"
            "  registry-data:\n",
            encoding="utf-8",
        )
    else:
        _write_bind_compose(override, source=str(source), read_only=False)
    actual_calls: list[list[str]] = []
    monkeypatch.setattr(
        safe_compose,
        "_run_actual_compose",
        lambda command, **kwargs: actual_calls.append(command),
    )

    with pytest.raises(
        safe_compose.ComposeBindSourceError,
        match="must be a read-only bind",
    ):
        safe_compose.run_compose(
            ["-f", str(base), "-f", str(override), "up"]
        )
    assert actual_calls == []


def test_project_directory_sets_the_relative_source_base(tmp_path, monkeypatch):
    source = tmp_path / "relative source"
    source.mkdir()
    compose = tmp_path / "compose.yml"
    _write_bind_compose(compose, source="./relative source")

    _run_real_config_preflight(
        monkeypatch,
        ["--project-directory", str(tmp_path), "-f", str(compose), "up"],
        dict(os.environ),
    )


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_required_variable_stops_at_compose_config(
    tmp_path, monkeypatch, value
):
    compose = tmp_path / "compose.yml"
    _write_bind_compose(compose, source="${HOST_SOURCE:?required}")
    environment = dict(os.environ)
    if value is None:
        environment.pop("HOST_SOURCE", None)
    else:
        environment["HOST_SOURCE"] = value
    actual_calls: list[list[str]] = []
    monkeypatch.setattr(
        safe_compose,
        "_run_actual_compose",
        lambda command, **kwargs: actual_calls.append(command),
    )

    with pytest.raises(safe_compose.ComposeBindSourceError) as raised:
        safe_compose.run_compose(
            ["-f", str(compose), "up"],
            environment=environment,
        )
    assert raised.value.exit_code != 0
    assert str(tmp_path) not in str(raised.value)
    assert actual_calls == []


@pytest.mark.parametrize(
    ("mount_update", "message"),
    [
        ({"type": "volume"}, "must be a read-only bind"),
        ({"read_only": False}, "must be a read-only bind"),
        ({"source": "relative/source"}, "must use an absolute existing directory"),
        ({"source": ""}, "must use an absolute existing directory"),
    ],
)
def test_invalid_final_mount_contract_blocks_the_actual_command(
    tmp_path, monkeypatch, mount_update, message
):
    source = tmp_path / "source"
    source.mkdir()
    mount = _bind(source)
    mount.update(mount_update)
    _, actual_calls = _stub_processes(monkeypatch, model=_model(mount))

    with pytest.raises(safe_compose.ComposeBindSourceError, match=message):
        safe_compose.run_compose(["up"])
    assert actual_calls == []


def test_each_protected_target_must_be_unique(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _, actual_calls = _stub_processes(
        monkeypatch,
        model=_model(_bind(source), _bind(source)),
    )

    with pytest.raises(
        safe_compose.ComposeBindSourceError,
        match="protected mount /registry must be unique",
    ):
        safe_compose.run_compose(["up"])
    assert actual_calls == []


@pytest.mark.parametrize("target", PROTECTED_TARGETS)
def test_all_protected_targets_accept_existing_directories(
    tmp_path, monkeypatch, target
):
    source = tmp_path / "source with spaces"
    source.mkdir()
    _, actual_calls = _stub_processes(
        monkeypatch,
        model=_model(_bind(source, target)),
    )

    assert safe_compose.run_compose(["up"]) == 0
    assert len(actual_calls) == 1


def test_missing_source_and_regular_file_are_rejected_without_path_leak(
    tmp_path, monkeypatch
):
    candidates = [tmp_path / "missing", tmp_path / "private-file"]
    candidates[1].write_text("private", encoding="utf-8")

    for candidate in candidates:
        _, actual_calls = _stub_processes(
            monkeypatch,
            model=_model(_bind(candidate)),
        )
        with pytest.raises(safe_compose.ComposeBindSourceError) as raised:
            safe_compose.run_compose(["up"])
        assert str(candidate) not in str(raised.value)
        assert actual_calls == []


def test_fifo_and_socket_sources_are_rejected(tmp_path, monkeypatch):
    if os.name != "posix":
        assert not hasattr(os, "mkfifo")
        return
    fifo = tmp_path / "private-fifo"
    os.mkfifo(fifo)
    socket_path = tmp_path / "private-socket"
    unix_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix_socket.bind(str(socket_path))
    try:
        for candidate in (fifo, socket_path):
            _, actual_calls = _stub_processes(
                monkeypatch,
                model=_model(_bind(candidate)),
            )
            with pytest.raises(safe_compose.ComposeBindSourceError) as raised:
                safe_compose.run_compose(["up"])
            assert str(candidate) not in str(raised.value)
            assert actual_calls == []
    finally:
        unix_socket.close()


def _make_directory_symlink(link: Path, target: Path) -> None:
    link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("parent_link", [False, True])
def test_direct_and_parent_symlinks_are_rejected(
    tmp_path, monkeypatch, parent_link
):
    target = tmp_path / "target"
    target.mkdir()
    (target / "child").mkdir()
    link = tmp_path / "private-link"
    try:
        _make_directory_symlink(link, target)
    except OSError:
        if os.name != "nt":
            raise
        metadata = type(
            "SymlinkMetadata",
            (),
            {"st_mode": stat.S_IFLNK, "st_file_attributes": 0},
        )()
        assert safe_compose._is_link_or_reparse(metadata)
        return
    source = link / "child" if parent_link else link
    _, actual_calls = _stub_processes(
        monkeypatch,
        model=_model(_bind(source)),
    )

    with pytest.raises(
        safe_compose.ComposeBindSourceError,
        match="contains a link or reparse point",
    ) as raised:
        safe_compose.run_compose(["up"])
    assert str(source) not in str(raised.value)
    assert actual_calls == []


def _make_junction(link: Path, target: Path) -> None:
    completed = REAL_SUBPROCESS_RUN(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip("NTFS junction creation is unavailable")


@pytest.mark.parametrize("parent_link", [False, True])
def test_direct_and_parent_junctions_are_rejected(
    tmp_path, monkeypatch, parent_link
):
    target = tmp_path / "target"
    target.mkdir()
    (target / "child").mkdir()
    link = tmp_path / "private-junction"
    if os.name == "nt":
        _make_junction(link, target)
    else:
        _make_directory_symlink(link, target)
    source = link / "child" if parent_link else link
    _, actual_calls = _stub_processes(
        monkeypatch,
        model=_model(_bind(source)),
    )

    with pytest.raises(
        safe_compose.ComposeBindSourceError,
        match="contains a link or reparse point",
    ) as raised:
        safe_compose.run_compose(["up"])
    assert str(source) not in str(raised.value)
    assert actual_calls == []


def test_actual_command_uses_argv_inherits_streams_and_preserves_exit_code(
    monkeypatch,
):
    config_calls, actual_calls = _stub_processes(
        monkeypatch,
        actual_returncodes=23,
    )
    environment = {"PATH": "private-path", "PRIVATE_VALUE": "not-printed"}
    arguments = ["logs", "--follow"]

    assert safe_compose.run_compose(arguments, environment=environment) == 23
    assert config_calls == []
    command, kwargs, _ = actual_calls[0]
    assert command == ["docker", "compose", *arguments]
    assert kwargs["env"] == environment
    assert kwargs["shell"] is False
    assert "stdout" not in kwargs
    assert "stderr" not in kwargs


def test_keyboard_interrupt_is_forwarded_with_stable_exit_code(monkeypatch):
    _, actual_calls = _stub_processes(
        monkeypatch,
        actual_returncodes=[KeyboardInterrupt(), -signal.SIGINT],
    )

    assert safe_compose.run_compose(["logs", "--follow"]) == 128 + signal.SIGINT
    process = actual_calls[0][2]
    assert process.signals == [signal.SIGINT]


def test_sigterm_handler_forwards_to_compose(monkeypatch):
    installed: dict[int, object] = {}

    def recording_signal(signum, handler):
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return previous

    class SignalledProcess(FakeProcess):
        def wait(self):
            handler = installed[signal.SIGTERM]
            assert callable(handler)
            handler(signal.SIGTERM, None)
            self.returncode = -signal.SIGTERM
            return self.returncode

    actual_calls: list[SignalledProcess] = []
    monkeypatch.setattr(safe_compose.shutil, "which", lambda *args, **kwargs: "docker")
    monkeypatch.setattr(safe_compose.signal, "signal", recording_signal)

    def fake_popen(command, **kwargs):
        process = SignalledProcess(0)
        actual_calls.append(process)
        return process

    monkeypatch.setattr(safe_compose.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        safe_compose.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("safe command must not run config"),
    )

    assert safe_compose.run_compose(["logs", "--follow"]) == 128 + signal.SIGTERM
    assert actual_calls[0].signals == [signal.SIGTERM]


def test_post_validation_replacement_probe_documents_the_toctou_boundary(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    replacement_link = tmp_path / "replacement-link"
    if os.name == "nt":
        _make_junction(replacement_link, replacement)
    else:
        _make_directory_symlink(replacement_link, replacement)
    _, actual_calls = _stub_processes(
        monkeypatch,
        model=_model(_bind(source)),
    )
    real_validate = safe_compose._validate_source_directory

    def validate_then_replace(path, *, target):
        real_validate(path, target=target)
        source.rmdir()
        replacement_link.rename(source)

    monkeypatch.setattr(
        safe_compose,
        "_validate_source_directory",
        validate_then_replace,
    )

    assert safe_compose.run_compose(["up"]) == 0
    assert source.exists()
    assert len(actual_calls) == 1
    deployment = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")
    assert "preflight hardening" in deployment
    assert "TOCTOU" in deployment
    assert "not an atomic filesystem guarantee" in deployment
