from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from climate_monitor.hermes_identity import (
    IDENTITY_FILE,
    IDENTITY_SCHEMA,
    bind_effective_identity,
    load_effective_identity,
)
from climate_monitor.management import (
    STATE_SCHEMA,
    ManagementService,
    TaskDefinitionStore,
    _sha,
    build_task_binding,
    default_task_definition,
)


def _definition(tmp_path: Path, *, legacy: bool = False) -> dict:
    from climate_registry.persistent import initialize_registry

    value = default_task_definition()
    value["parameters"].update(report_date="2026-09-07", source_keys=["wmo"])
    if legacy:
        value["parameters"].update(provider="openai-codex", model="legacy-model")
    value["runtime"].update(
        registry_database=str(tmp_path / "registry.sqlite3"),
        run_root=str(tmp_path / "runs"),
    )
    initialize_registry(tmp_path / "registry.sqlite3")
    (tmp_path / "runs").mkdir(exist_ok=True)
    return value


def _identity(binding: dict, *, provider="default-provider", model="default-model") -> dict:
    return {
        "schema_version": IDENTITY_SCHEMA,
        "session_id": "20260920_080000_abcdef",
        "source": f"climate-acquisition-{binding['run_id']}",
        "provider": provider,
        "model": model,
        "observed_at": "2026-09-20T08:00:00Z",
    }


def _write_session_route(
    home: Path, *, provider="default-provider", model="default-model",
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(home / "state.db")
    connection.executescript("""
        CREATE TABLE session_model_usage (
            session_id TEXT, model TEXT, billing_provider TEXT,
            task TEXT, api_call_count INTEGER, last_seen INTEGER
        );
    """)
    connection.execute(
        "INSERT INTO session_model_usage VALUES (?, ?, ?, '', 1, 1)",
        ("20260920_080000_abcdef", model, provider),
    )
    connection.commit()
    connection.close()


def test_new_definitions_strip_task_model_fields_while_history_keeps_identity(tmp_path):
    store = TaskDefinitionStore(tmp_path / "task.json", tmp_path / "versions")
    historical = _definition(tmp_path, legacy=True)
    historical_hash = _sha(historical)
    store.active_path.write_text(json.dumps({
        "schema_version": STATE_SCHEMA,
        "version": 1,
        "saved_at": "2026-09-20T08:00:00Z",
        "saved_by": "legacy",
        "definition_sha256": historical_hash,
        "definition": historical,
    }), encoding="utf-8")

    loaded = store.load()
    assert loaded["hashes"]["definition_sha256"] == historical_hash
    assert loaded["definition"]["parameters"]["provider"] == "openai-codex"
    assert "provider" not in store.preview(loaded["definition"])["definition"]["parameters"]

    saved = store.save(loaded["definition"], expected_version=1, actor="operator")
    assert set(saved["definition"]["parameters"]).isdisjoint({"provider", "model"})
    restored = store.restore(1, expected_version=2, actor="operator")
    assert set(restored["definition"]["parameters"]).isdisjoint({"provider", "model"})
    archived = json.loads((store.version_root / "00000001.json").read_text(encoding="utf-8"))
    assert archived["definition_sha256"] == historical_hash
    assert archived["definition"] == historical


def test_new_binding_inherits_defaults_and_legacy_binding_keeps_frozen_override(tmp_path):
    new_binding = build_task_binding(
        _definition(tmp_path), task_version=1, run_id="new", attempt=1,
        created_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )
    assert set(new_binding).isdisjoint({"provider", "model"})
    assert set(new_binding["meeting"]).isdisjoint({"provider", "model"})

    legacy_binding = build_task_binding(
        _definition(tmp_path, legacy=True), task_version=1, run_id="old", attempt=1,
        created_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )
    assert (legacy_binding["provider"], legacy_binding["model"]) == (
        "openai-codex", "legacy-model",
    )


def test_command_defaults_first_turn_then_resumes_observed_session(tmp_path):
    from scripts import run_agent_acquisition as runner

    binding = build_task_binding(
        _definition(tmp_path), task_version=1, run_id="chain", attempt=1,
    )
    prompt = tmp_path / "prompt.md"
    first = runner._hermes_command("hermes", binding, prompt)
    assert not ({"--provider", "--model", "--resume"} & set(first))

    evidence_path = Path(binding["checkpoint_dir"]).parent / IDENTITY_FILE
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(_identity(binding)), encoding="utf-8")
    continued = runner._hermes_command("hermes", binding, prompt)
    assert continued[continued.index("--resume") + 1] == "20260920_080000_abcdef"
    assert "--provider" not in continued and "--model" not in continued

    legacy = build_task_binding(
        _definition(tmp_path, legacy=True), task_version=1, run_id="legacy", attempt=2,
    )
    legacy_command = runner._hermes_command("hermes", legacy, prompt)
    assert legacy_command[legacy_command.index("--provider") + 1] == "openai-codex"
    assert legacy_command[legacy_command.index("--model") + 1] == "legacy-model"
    assert runner._hermes_failure_disposition(
        legacy, "No API key found for provider 'openai-codex'.", 78,
    ) == (True, "retryable_failure", "resume the same frozen run")

    response = tmp_path / "failed.txt"
    response.write_text("provider authentication failed", encoding="utf-8")
    error = runner._hermes_process_error(
        response, 1, phase="acquisition",
    )
    assert "default configuration" not in error
    assert "Hermes acquisition process exited" in error


@pytest.mark.parametrize(
    ("diagnostic", "exit_code", "expected"),
    [
        (
            "No usable credentials found for provider 'openai-api'.",
            78,
            (False, "terminal_failure", "fix Hermes default configuration and create a new run"),
        ),
        (
            "No usable credentials found for provider 'openai-api'.",
            124,
            (True, "retryable_failure", "resume the same frozen run"),
        ),
        (
            "It looks like Hermes isn't configured yet -- no API keys or providers found.",
            78,
            (False, "terminal_failure", "fix Hermes default configuration and create a new run"),
        ),
        (
            "It looks like Hermes isn't configured yet -- no API keys or providers found.",
            124,
            (True, "retryable_failure", "resume the same frozen run"),
        ),
        (
            "AuthError: provider token refresh failed",
            78,
            (True, "retryable_failure", "resume the same frozen run"),
        ),
        (
            "Hermes acquisition process exited with 70",
            70,
            (True, "retryable_failure", "resume the same frozen run"),
        ),
    ],
)
def test_missing_default_diagnostic_disposition(tmp_path, diagnostic, exit_code, expected):
    from scripts import run_agent_acquisition as runner

    binding = build_task_binding(
        _definition(tmp_path), task_version=1, run_id="classification", attempt=1,
    )
    assert runner._hermes_failure_disposition(binding, diagnostic, exit_code) == expected

    legacy = build_task_binding(
        _definition(tmp_path, legacy=True),
        task_version=1, run_id="legacy-classification", attempt=1,
    )
    assert runner._hermes_failure_disposition(
        legacy, diagnostic, exit_code,
    ) == (True, "retryable_failure", "resume the same frozen run")


def test_environment_keeps_all_default_provider_credentials_but_not_app_secrets(
    tmp_path, monkeypatch,
):
    from scripts import run_agent_acquisition as runner
    import climate_monitor.managed_backend as managed_backend

    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic")
    monkeypatch.setenv("ANTHROPIC_TOKEN", "anthropic-token")
    monkeypatch.setenv("GH_TOKEN", "github-copilot")
    monkeypatch.setenv("HF_TOKEN", "huggingface")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unit-provider.invalid/v1")
    monkeypatch.setenv("HERMES_COPILOT_ACP_COMMAND", "unit-copilot-command")
    monkeypatch.setenv("COPILOT_CLI_PATH", "unit-copilot-cli")
    monkeypatch.setenv("HERMES_COPILOT_ACP_ARGS", "--unit-copilot-arg")
    monkeypatch.setenv("HERMES_INFERENCE_MODEL", "unit-model")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "unit-provider")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "unit-op-token")
    monkeypatch.setenv("API_BASE_URL", "https://application.invalid/v1")
    monkeypatch.setenv("CUSTOM_DEFAULT_API_KEY", "custom")
    monkeypatch.setenv("RELOAD_TOKEN", "do-not-pass")
    monkeypatch.setenv("DEPLOYMENT_SECRET", "do-not-pass")
    environment = runner._minimal_environment("openai")
    assert environment["OPENAI_API_KEY"] == "openai"
    assert environment["ANTHROPIC_API_KEY"] == "anthropic"
    assert environment["ANTHROPIC_TOKEN"] == "anthropic-token"
    assert environment["GH_TOKEN"] == "github-copilot"
    assert environment["HF_TOKEN"] == "huggingface"
    assert environment["OPENAI_BASE_URL"] == "https://unit-provider.invalid/v1"
    assert environment["HERMES_COPILOT_ACP_COMMAND"] == "unit-copilot-command"
    assert environment["COPILOT_CLI_PATH"] == "unit-copilot-cli"
    assert environment["HERMES_COPILOT_ACP_ARGS"] == "--unit-copilot-arg"
    assert environment["HERMES_INFERENCE_MODEL"] == "unit-model"
    assert environment["HERMES_INFERENCE_PROVIDER"] == "unit-provider"
    assert environment["OP_SERVICE_ACCOUNT_TOKEN"] == "unit-op-token"
    assert environment["CUSTOM_DEFAULT_API_KEY"] == "custom"
    assert "API_BASE_URL" not in environment
    assert "RELOAD_TOKEN" not in environment
    assert "DEPLOYMENT_SECRET" not in environment
    assert runner._PINNED_PROVIDER_ROUTE_ENV == {
        "OPENROUTER_BASE_URL", "OPENAI_BASE_URL", "XAI_BASE_URL",
        "HERMES_QWEN_BASE_URL", "LM_BASE_URL", "COPILOT_ACP_BASE_URL",
        "GLM_BASE_URL", "KIMI_BASE_URL", "STEPFUN_BASE_URL",
        "MINIMAX_BASE_URL", "MINIMAX_CN_BASE_URL", "DEEPSEEK_BASE_URL",
        "DASHSCOPE_BASE_URL", "ALIBABA_CODING_PLAN_BASE_URL",
        "OPENCODE_ZEN_BASE_URL", "OPENCODE_GO_BASE_URL",
        "KILOCODE_BASE_URL", "HF_BASE_URL", "NOVITA_BASE_URL",
        "NVIDIA_BASE_URL", "XIAOMI_BASE_URL", "TOKENHUB_BASE_URL",
        "ARCEE_BASE_URL", "GMI_BASE_URL", "ACTUAL_BASE_URL",
        "UPSTAGE_BASE_URL", "OLLAMA_BASE_URL", "AZURE_FOUNDRY_BASE_URL",
        "HERMES_COPILOT_ACP_COMMAND", "COPILOT_CLI_PATH",
        "HERMES_COPILOT_ACP_ARGS", "HERMES_INFERENCE_MODEL",
        "HERMES_INFERENCE_PROVIDER", "OP_SERVICE_ACCOUNT_TOKEN",
    }

    monkeypatch.setattr(managed_backend, "_host_execution_identity", lambda **_kwargs: {
        "user": "host-user", "uid": 1001, "python": "/host/python",
        "hermes_home": "/host/.hermes", "hermes_executable": "/host/hermes",
        "hermes_version": "0.20.5",
    })
    binding = {"run_id": "provider-credential-map"}
    fingerprint = runner._host_execution_fingerprint(
        binding, environment=environment, source_home="/host/.hermes",
    )
    changed = dict(environment, OPENAI_BASE_URL="https://changed-provider.invalid/v1")
    assert runner._host_execution_fingerprint(
        binding, environment=changed, source_home="/host/.hermes",
    )["credentials_sha256"] != fingerprint["credentials_sha256"]
    changed = dict(environment, HERMES_COPILOT_ACP_COMMAND="changed-copilot-command")
    assert runner._host_execution_fingerprint(
        binding, environment=changed, source_home="/host/.hermes",
    )["credentials_sha256"] != fingerprint["credentials_sha256"]
    changed = dict(environment, API_BASE_URL="https://changed-application.invalid/v1")
    assert runner._host_execution_fingerprint(
        binding, environment=changed, source_home="/host/.hermes",
    )["credentials_sha256"] == fingerprint["credentials_sha256"]
    assert not any(
        secret in json.dumps(fingerprint)
        for secret in (
            "anthropic-token", "github-copilot", "huggingface",
            "https://unit-provider.invalid/v1", "unit-copilot-command",
            "unit-copilot-cli", "--unit-copilot-arg",
        )
    )

    response_path = tmp_path / "hermes-response.txt"
    response_path.write_text(
        "anthropic-token github-copilot huggingface "
        "https://unit-provider.invalid/v1 unit-copilot-command "
        "unit-copilot-cli --unit-copilot-arg",
        encoding="utf-8",
    )
    error = runner._hermes_process_error(response_path, 78, phase="acquisition")
    assert "[REDACTED]" in error
    assert not any(
        secret in error
        for secret in (
            "anthropic-token", "github-copilot", "huggingface",
            "https://unit-provider.invalid/v1", "unit-copilot-command",
            "unit-copilot-cli", "--unit-copilot-arg",
        )
    )


def test_run_home_freezes_ordinary_hermes_config_and_credentials(tmp_path, monkeypatch):
    import yaml
    from climate_monitor.hermes_acquisition_hooks import (
        CREDENTIAL_INPUTS_STATE, install_hooks,
    )

    binding = build_task_binding(
        _definition(tmp_path), task_version=1, run_id="config", attempt=1,
    )
    binding_path = Path(binding["checkpoint_dir"]).parent / "attempt-1.json"
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    source_home = tmp_path / "ordinary-hermes"
    source_home.mkdir()
    (source_home / "config.yaml").write_text(
        "model:\n  provider: configured-default\n  default: configured-model\n",
        encoding="utf-8",
    )
    (source_home / ".env").write_text("DEFAULT_PROVIDER_KEY=secret\n", encoding="utf-8")
    (source_home / "auth.json").write_text('{"token":"secret"}', encoding="utf-8")
    (source_home / ".op.env").write_text("OP_SERVICE_ACCOUNT_TOKEN=secret\n", encoding="utf-8")
    executable = tmp_path / "hermes"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        "climate_monitor.hermes_acquisition_hooks.subprocess.run",
        lambda *args, **kwargs: type(
            "Result", (), {"returncode": 0, "stdout": "climate acquisition hooks verified", "stderr": ""}
        )(),
    )

    _, home = install_hooks(
        [str(executable), "chat"], binding_path, binding,
        {"PATH": str(tmp_path), "HERMES_HOME": str(source_home)},
    )
    frozen = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert frozen["model"] == {
        "provider": "configured-default", "default": "configured-model",
    }
    assert (home / ".env").read_text(encoding="utf-8") == "DEFAULT_PROVIDER_KEY=secret\n"
    assert (home / "auth.json").is_file()
    assert (home / ".op.env").read_text(encoding="utf-8") == (
        "OP_SERVICE_ACCOUNT_TOKEN=secret\n"
    )

    (source_home / "config.yaml").write_text(
        "model:\n  provider: changed-provider\n  default: changed-model\n",
        encoding="utf-8",
    )
    (source_home / ".env").write_text("DEFAULT_PROVIDER_KEY=changed\n", encoding="utf-8")
    (source_home / "auth.json").write_text('{"token":"changed"}', encoding="utf-8")
    (source_home / ".op.env").write_text("OP_SERVICE_ACCOUNT_TOKEN=changed\n", encoding="utf-8")
    (home / "auth.json").write_text('{"token":"private-refresh"}', encoding="utf-8")
    (home / ".op.env").write_text("OP_SERVICE_ACCOUNT_TOKEN=private-refresh\n", encoding="utf-8")
    resumed = dict(binding, attempt=2)
    install_hooks(
        [str(executable), "chat"], binding_path, resumed,
        {"PATH": str(tmp_path), "HERMES_HOME": str(source_home)},
    )
    still_frozen = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert still_frozen["model"] == frozen["model"]
    assert (home / ".env").read_text(encoding="utf-8") == "DEFAULT_PROVIDER_KEY=secret\n"
    assert (home / "auth.json").read_text(encoding="utf-8") == '{"token":"private-refresh"}'
    assert (home / ".op.env").read_text(encoding="utf-8") == (
        "OP_SERVICE_ACCOUNT_TOKEN=private-refresh\n"
    )
    state = json.loads((home / CREDENTIAL_INPUTS_STATE).read_text(encoding="utf-8"))
    assert state["files"] == {
        "config.yaml": True, ".env": True, ".op.env": True, "auth.json": True,
    }
    assert "secret" not in json.dumps(state)


def test_completed_input_snapshot_survives_governed_config_interruption(
    tmp_path, monkeypatch,
):
    import yaml
    import climate_monitor.hermes_acquisition_hooks as hooks
    from scripts import run_agent_acquisition as runner

    binding = build_task_binding(
        _definition(tmp_path), task_version=1, run_id="config-interruption", attempt=1,
    )
    binding_path = Path(binding["checkpoint_dir"]).parent / "attempt-1.json"
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    source_home = tmp_path / "ordinary-hermes"
    source_home.mkdir()
    original_config = (
        "model:\n  provider: custom:unit\n  default: first-model\n"
        "providers:\n  unit:\n    key_env: CUSTOM_ROUTE_TOKEN\n"
    )
    (source_home / "config.yaml").write_text(original_config, encoding="utf-8")
    executable = tmp_path / "hermes"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        hooks.subprocess, "run",
        lambda *args, **kwargs: type(
            "Result", (), {
                "returncode": 0,
                "stdout": "climate acquisition hooks verified",
                "stderr": "",
            },
        )(),
    )
    original_write = hooks._write_immutable

    def interrupt_governed_config(path, raw, message):
        if path.name == "config.yaml":
            raise OSError("interrupted before governed config")
        original_write(path, raw, message)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(hooks, "_write_immutable", interrupt_governed_config)
        with pytest.raises(OSError, match="governed config"):
            hooks.install_hooks(
                [str(executable), "chat"], binding_path, binding,
                {
                    "PATH": str(tmp_path), "HERMES_HOME": str(source_home),
                    "CUSTOM_ROUTE_TOKEN": "first-token",
                },
                managed_environment_names={"CUSTOM_ROUTE_TOKEN"},
            )

    home = hooks.attempt_home(binding)
    complete_path = home / ".managed-inputs-complete.json"
    source_snapshot = home / ".managed-source" / "config.yaml"
    assert complete_path.is_file()
    assert "first-token" not in complete_path.read_text(encoding="utf-8")
    assert source_snapshot.read_text(
        encoding="utf-8"
    ) == original_config
    if os.name != "nt":
        import stat
        assert stat.S_IMODE(complete_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(source_snapshot.stat().st_mode) == 0o600
    (source_home / "config.yaml").write_text("providers: [\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(source_home))
    monkeypatch.setenv("CUSTOM_ROUTE_TOKEN", "changed-token")

    class Process:
        pid = 123

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            return 0

    launched = {}
    monkeypatch.setattr(
        runner, "RequestBudget",
        lambda *args, **kwargs: SimpleNamespace(remaining_seconds=lambda: 60),
    )
    monkeypatch.setattr(runner, "bind_effective_identity", lambda *args: None)
    monkeypatch.setattr(
        runner.subprocess, "Popen",
        lambda command, **kwargs: launched.update(command=command, **kwargs) or Process(),
    )
    assert runner._invoke_hermes(
        [str(executable), "chat"], home.parent / "retry-response.txt",
        binding_path, dict(binding, attempt=2), runner.time.monotonic() + 60,
    ) == 0
    resumed_environment = launched["env"]
    governed = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert governed["model"] == {
        "provider": "custom:unit", "default": "first-model",
    }
    assert resumed_environment["CUSTOM_ROUTE_TOKEN"] == "first-token"


def test_nonempty_private_home_without_complete_snapshot_never_resamples(
    tmp_path, monkeypatch,
):
    import climate_monitor.hermes_acquisition_hooks as hooks

    binding = build_task_binding(
        _definition(tmp_path), task_version=1, run_id="incomplete-private", attempt=1,
    )
    binding_path = Path(binding["checkpoint_dir"]).parent / "attempt-1.json"
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    home = hooks.attempt_home(binding)
    home.mkdir()
    (home / "config.yaml").write_text("model: private-old\n", encoding="utf-8")
    (home / ".env").write_text("CUSTOM_ROUTE_TOKEN=private-old\n", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in home.iterdir()}
    source_home = tmp_path / "ordinary-hermes"
    source_home.mkdir()
    (source_home / "config.yaml").write_text(
        "providers:\n  changed:\n    key_env: CUSTOM_ROUTE_TOKEN\n",
        encoding="utf-8",
    )
    executable = tmp_path / "hermes"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)

    with pytest.raises(ValueError, match="initialization is incomplete"):
        hooks.install_hooks(
            [str(executable), "chat"], binding_path, binding,
            {
                "PATH": str(tmp_path), "HERMES_HOME": str(source_home),
                "CUSTOM_ROUTE_TOKEN": "changed-token",
            },
            managed_environment_names={"CUSTOM_ROUTE_TOKEN"},
        )
    assert {path.name: path.read_bytes() for path in home.iterdir()} == before


@pytest.mark.parametrize("artifacts", [
    (".managed-inputs-complete.json",),
    (".managed-credential-inputs.json",),
    (".managed-provider-environment.json",),
    (".managed-credential-inputs.json", ".managed-provider-environment.json"),
    (".managed-inputs-complete.json", ".managed-credential-inputs.json"),
    (".managed-inputs-complete.json", ".managed-provider-environment.json"),
])
def test_partial_managed_input_snapshot_is_rejected_without_writes(tmp_path, artifacts):
    import climate_monitor.hermes_acquisition_hooks as hooks

    home = tmp_path / "partial" / str(len(artifacts))
    home.mkdir(parents=True)
    for name in artifacts:
        (home / name).write_text("{}", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in home.iterdir()}
    with pytest.raises(ValueError, match="managed Hermes input snapshot"):
        hooks._freeze_credential_inputs(tmp_path / "ordinary-hermes", home)
    assert {path.name: path.read_bytes() for path in home.iterdir()} == before


def test_managed_provider_environment_freezes_first_local_values(tmp_path, monkeypatch):
    import stat
    import climate_monitor.hermes_acquisition_hooks as hooks
    from scripts import run_agent_acquisition as runner

    binding = build_task_binding(
        _definition(tmp_path), task_version=1, run_id="environment-freeze", attempt=1,
    )
    binding_path = Path(binding["checkpoint_dir"]).parent / "attempt-1.json"
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    source_home = tmp_path / "ordinary-hermes"
    source_home.mkdir()
    (source_home / "config.yaml").write_text(
        "model:\n  provider: custom:unit\n  default: unit-model\n"
        "providers:\n  unit:\n    name: Unit\n"
        "    base_url: https://unit.invalid/v1\n"
        "    key_env: CUSTOM_ROUTE_TOKEN\n"
        "custom_providers:\n  - name: Late\n"
        "    base_url: https://late.invalid/v1\n"
        "    key_env: LATE_CUSTOM_TOKEN\n",
        encoding="utf-8",
    )
    executable = tmp_path / "hermes"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(
        hooks.subprocess, "run",
        lambda *args, **kwargs: type(
            "Result", (), {
                "returncode": 0,
                "stdout": "climate acquisition hooks verified",
                "stderr": "",
            },
        )(),
    )
    monkeypatch.setenv("HERMES_HOME", str(source_home))
    monkeypatch.setenv("CUSTOM_ROUTE_TOKEN", "custom-first")
    monkeypatch.delenv("LATE_CUSTOM_TOKEN", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider-first.invalid/v1")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "provider-first")
    monkeypatch.setenv("HERMES_INFERENCE_MODEL", "model-first")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "op-first")
    monkeypatch.setenv("API_BASE_URL", "https://application-first.invalid/v1")

    names = runner._provider_credential_names(source_home)
    first, home = hooks.install_hooks(
        [str(executable), "chat"], binding_path, binding,
        runner._minimal_environment(source_home=source_home),
        managed_environment_names=names,
    )
    assert first["CUSTOM_ROUTE_TOKEN"] == "custom-first"
    assert first["OPENAI_BASE_URL"] == "https://provider-first.invalid/v1"
    assert first["HERMES_INFERENCE_PROVIDER"] == "provider-first"
    assert first["HERMES_INFERENCE_MODEL"] == "model-first"
    assert first["OP_SERVICE_ACCOUNT_TOKEN"] == "op-first"
    assert "LATE_CUSTOM_TOKEN" not in first
    assert "API_BASE_URL" not in first

    monkeypatch.setenv("CUSTOM_ROUTE_TOKEN", "custom-changed")
    monkeypatch.setenv("LATE_CUSTOM_TOKEN", "late-added")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider-changed.invalid/v1")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "provider-changed")
    monkeypatch.setenv("HERMES_INFERENCE_MODEL", "model-changed")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "op-changed")
    (source_home / "config.yaml").write_text("providers: [\n", encoding="utf-8")

    class Process:
        pid = 123

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            return 0

    launched = {}
    monkeypatch.setattr(
        runner, "RequestBudget",
        lambda *args, **kwargs: SimpleNamespace(remaining_seconds=lambda: 60),
    )
    monkeypatch.setattr(runner, "bind_effective_identity", lambda *args: None)
    monkeypatch.setattr(
        runner.subprocess, "Popen",
        lambda command, **kwargs: launched.update(command=command, **kwargs) or Process(),
    )
    assert runner._invoke_hermes(
        [str(executable), "chat"], home.parent / "resume-response.txt",
        binding_path, dict(binding, attempt=2), runner.time.monotonic() + 60,
    ) == 0
    resumed = launched["env"]
    assert launched["cwd"] == home
    assert resumed["HERMES_HOME"] == str(home)
    assert resumed[runner._SOURCE_HERMES_HOME_ENV] == str(source_home)
    for name in (
        "CUSTOM_ROUTE_TOKEN", "OPENAI_BASE_URL", "HERMES_INFERENCE_PROVIDER",
        "HERMES_INFERENCE_MODEL", "OP_SERVICE_ACCOUNT_TOKEN",
    ):
        assert resumed[name] == first[name]
    assert "LATE_CUSTOM_TOKEN" not in resumed
    assert "API_BASE_URL" not in resumed

    downstream = runner._managed_child_environment(
        binding, source_home=source_home,
    )
    assert downstream["CUSTOM_ROUTE_TOKEN"] == "custom-first"
    assert downstream["OPENAI_BASE_URL"] == "https://provider-first.invalid/v1"
    assert "LATE_CUSTOM_TOKEN" not in downstream
    assert "API_BASE_URL" not in downstream
    state_path = home / hooks.MANAGED_ENVIRONMENT_STATE
    if os.name != "nt":
        assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert "application-first" not in state_path.read_text(encoding="utf-8")
    response_path = home.parent / "frozen-environment-error.txt"
    response_path.write_text(
        "custom-first op-first https://provider-first.invalid/v1",
        encoding="utf-8",
    )
    error = runner._hermes_process_error(
        response_path, 78, phase="acquisition", binding=binding,
    )
    assert "[REDACTED]" in error
    assert not any(value in error for value in (
        "custom-first", "op-first", "https://provider-first.invalid/v1",
    ))


def test_run_home_never_adds_credentials_missing_at_first_install(tmp_path, monkeypatch):
    from climate_monitor.hermes_acquisition_hooks import attempt_home, install_hooks

    binding = build_task_binding(
        _definition(tmp_path), task_version=1, run_id="missing-credentials", attempt=1,
    )
    binding_path = Path(binding["checkpoint_dir"]).parent / "attempt-1.json"
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    source_home = tmp_path / "ordinary-hermes"
    source_home.mkdir()
    executable = tmp_path / "hermes"
    executable.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    executable.chmod(0o755)
    verification_results = iter([
        type("Result", (), {
            "returncode": 1, "stdout": "", "stderr": "interrupted initialization",
        })(),
        type("Result", (), {
            "returncode": 0,
            "stdout": "climate acquisition hooks verified",
            "stderr": "",
        })(),
    ])
    monkeypatch.setattr(
        "climate_monitor.hermes_acquisition_hooks.subprocess.run",
        lambda *args, **kwargs: next(verification_results),
    )
    with pytest.raises(ValueError, match="installation incompatible"):
        install_hooks(
            [str(executable), "chat"], binding_path, binding,
            {"PATH": str(tmp_path), "HERMES_HOME": str(source_home)},
        )
    home = attempt_home(binding)
    (source_home / "config.yaml").write_text(
        "model:\n  provider: late-provider\n  default: late-model\n",
        encoding="utf-8",
    )
    (source_home / ".env").write_text("LATE_KEY=late-secret\n", encoding="utf-8")
    (source_home / "auth.json").write_text('{"token":"late-secret"}', encoding="utf-8")
    (source_home / ".op.env").write_text("OP_SERVICE_ACCOUNT_TOKEN=late-secret\n", encoding="utf-8")

    install_hooks(
        [str(executable), "chat"], binding_path, dict(binding, attempt=2),
        {"PATH": str(tmp_path), "HERMES_HOME": str(source_home)},
    )
    assert not (home / ".env").exists()
    assert not (home / "auth.json").exists()
    assert not (home / ".op.env").exists()
    assert "model" not in json.loads((home / "config.yaml").read_text(encoding="utf-8"))
    state_path = home / ".managed-credential-inputs.json"
    state_before = state_path.read_bytes()
    assert json.loads(state_before)["files"] == {
        "config.yaml": False, ".env": False, ".op.env": False,
        "auth.json": False,
    }
    assert "late-secret" not in state_path.read_text(encoding="utf-8")


def test_credential_snapshot_survives_interruption_and_concurrent_initialization(
    tmp_path, monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor
    import climate_monitor.hermes_acquisition_hooks as hooks

    source_home = tmp_path / "ordinary-hermes"
    source_home.mkdir()
    (source_home / "auth.json").write_text('{"token":"first"}', encoding="utf-8")
    captured_home = tmp_path / "captured-hermes"
    captured_home.mkdir()
    original_copy = hooks._copy_frozen_credential

    def change_source_after_read(raw, destination):
        (source_home / "auth.json").write_text(
            '{"token":"changed-during-copy"}', encoding="utf-8",
        )
        original_copy(raw, destination)

    with monkeypatch.context() as changing:
        changing.setattr(hooks, "_copy_frozen_credential", change_source_after_read)
        hooks._freeze_credential_inputs(source_home, captured_home)
    assert (captured_home / "auth.json").read_text(encoding="utf-8") == '{"token":"first"}'

    (source_home / "auth.json").write_text('{"token":"first"}', encoding="utf-8")
    home = tmp_path / "private-hermes"
    home.mkdir()

    def stop_after_snapshot(_raw, _destination):
        (source_home / "auth.json").write_text(
            '{"token":"changed-after-snapshot"}', encoding="utf-8",
        )
        raise OSError("interrupted after snapshot")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            hooks, "_copy_frozen_credential", stop_after_snapshot,
        )
        with pytest.raises(OSError, match="interrupted after snapshot"):
            hooks._freeze_credential_inputs(source_home, home)

    state_path = home / hooks.CREDENTIAL_INPUTS_STATE
    initializing = home / f"{hooks.CREDENTIAL_INPUTS_STATE}.initializing"
    assert not state_path.exists()
    assert initializing.read_bytes() == b""
    (source_home / ".env").write_text("LATE_KEY=late-secret\n", encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        hooks._freeze_credential_inputs(source_home, home)
    assert not state_path.exists()
    assert not (home / "auth.json").exists()
    assert not (home / ".env").exists()
    assert not (home / ".op.env").exists()

    environment_home = tmp_path / "interrupted-environment"
    environment_home.mkdir()
    original_create_once = hooks._create_once

    def stop_before_environment_state(path, raw):
        if path.name == hooks.MANAGED_ENVIRONMENT_STATE:
            raise OSError("interrupted before environment state")
        original_create_once(path, raw)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(hooks, "_create_once", stop_before_environment_state)
        with pytest.raises(OSError, match="environment state"):
            hooks._freeze_credential_inputs(
                source_home, environment_home,
                {"CUSTOM_ROUTE_TOKEN": "first"}, {"CUSTOM_ROUTE_TOKEN"},
            )
    assert not (environment_home / hooks.MANAGED_ENVIRONMENT_STATE).exists()
    assert (
        environment_home / f"{hooks.CREDENTIAL_INPUTS_STATE}.initializing"
    ).read_bytes() == b""
    with pytest.raises(ValueError, match="incomplete"):
        hooks._freeze_credential_inputs(
            source_home, environment_home,
            {"CUSTOM_ROUTE_TOKEN": "changed"}, {"CUSTOM_ROUTE_TOKEN"},
        )

    (source_home / "auth.json").write_text('{"token":"first"}', encoding="utf-8")
    concurrent_home = tmp_path / "concurrent-hermes"
    concurrent_home.mkdir()

    def concurrent_freeze(_index):
        try:
            hooks._freeze_credential_inputs(source_home, concurrent_home)
        except ValueError as exc:
            return str(exc)
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent_results = list(pool.map(concurrent_freeze, range(2)))
    assert None in concurrent_results
    assert all(
        result is None or "initialization is incomplete" in result
        for result in concurrent_results
    )
    hooks._freeze_credential_inputs(source_home, concurrent_home)
    concurrent_state = json.loads(
        (concurrent_home / hooks.CREDENTIAL_INPUTS_STATE).read_text(encoding="utf-8")
    )
    assert concurrent_state["files"] == {
        "config.yaml": False, ".env": True, ".op.env": False,
        "auth.json": True,
    }
    assert (concurrent_home / ".env").read_text(encoding="utf-8") == "LATE_KEY=late-secret\n"
    assert (concurrent_home / "auth.json").read_text(encoding="utf-8") == '{"token":"first"}'


def test_actual_session_usage_is_persisted_and_mismatch_rejected(tmp_path):
    binding = build_task_binding(
        _definition(tmp_path), task_version=1, run_id="evidence", attempt=1,
    )
    home = Path(binding["checkpoint_dir"]).parent / "hermes-runtime"
    home.mkdir(parents=True)
    connection = sqlite3.connect(home / "state.db")
    connection.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at INTEGER NOT NULL
        );
        CREATE TABLE session_model_usage (
            session_id TEXT, model TEXT, billing_provider TEXT,
            task TEXT, api_call_count INTEGER, last_seen INTEGER
        );
        INSERT INTO sessions VALUES
            ('older-session', 'climate-acquisition-evidence', 1),
            ('session-1', 'climate-acquisition-evidence', 2);
        INSERT INTO session_model_usage VALUES
            ('older-session', 'irrelevant-model', 'irrelevant-provider', '', 100, 999),
            ('session-1', 'old-model', 'old-provider', '', 50, 100),
            ('session-1', 'actual-model', 'actual-provider', '', 1, 200);
    """)
    connection.commit()
    connection.close()

    evidence = bind_effective_identity(binding, home, "climate-acquisition-evidence")
    assert (evidence["provider"], evidence["model"]) == (
        "actual-provider", "actual-model",
    )
    assert load_effective_identity(binding) == evidence
    assert "credential" not in json.dumps(evidence).lower()

    connection = sqlite3.connect(home / "state.db")
    connection.execute(
        "UPDATE session_model_usage SET api_call_count = 500 "
        "WHERE session_id = 'session-1' AND model = 'old-model'"
    )
    connection.commit()
    connection.close()
    assert bind_effective_identity(
        binding, home, "climate-acquisition-evidence"
    ) == evidence

    connection = sqlite3.connect(home / "state.db")
    connection.execute(
        "UPDATE session_model_usage SET last_seen = 300 "
        "WHERE session_id = 'session-1' AND model = 'old-model'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="start a fresh managed run"):
        bind_effective_identity(binding, home, "climate-acquisition-evidence")


def test_report_and_manual_meeting_use_observed_run_identity(tmp_path, monkeypatch):
    from climate_monitor import management
    from climate_monitor import meetings
    from scripts import run_agent_acquisition as runner

    definition = _definition(tmp_path)
    definition["meeting"]["enabled"] = True
    store = TaskDefinitionStore(tmp_path / "task.json", tmp_path / "versions")
    saved = store.save(definition, actor="operator")
    binding = build_task_binding(saved["definition"], task_version=1, run_id="handoff", attempt=1)
    run_dir = Path(binding["checkpoint_dir"]).parent
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "binding.json").write_text(json.dumps(binding), encoding="utf-8")
    (run_dir / IDENTITY_FILE).write_text(json.dumps(_identity(binding)), encoding="utf-8")
    from climate_monitor import hermes_acquisition_hooks as hooks

    source_home = tmp_path / "ordinary-hermes"
    source_home.mkdir()
    hooks.attempt_home(binding).mkdir()
    hooks._freeze_credential_inputs(
        source_home, hooks.attempt_home(binding),
        {"OPENAI_BASE_URL": "https://first-route.invalid/v1"},
        {"OPENAI_BASE_URL"},
    )
    executable = str((tmp_path / "host-bin" / "hermes").resolve())
    monkeypatch.setenv("HERMES_HOME", str(source_home))
    monkeypatch.setenv("HERMES_EXECUTABLE", executable)
    monkeypatch.setenv("PATH", str(tmp_path / "decoy-bin"))
    monkeypatch.setenv("OPENAI_BASE_URL", "https://changed-route.invalid/v1")

    observed = {}
    monkeypatch.setattr(runner.subprocess, "run", lambda command, **kwargs: (
        observed.update(command=command, env=kwargs["env"])
        or type("Result", (), {"returncode": 0})()
    ))
    binding_path = run_dir / "attempt-1.json"
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    assert runner._run_report(binding_path, binding) == 0
    assert "--model-provider" not in observed["command"]
    assert "--model" not in observed["command"]
    from climate_monitor.hermes_acquisition_hooks import attempt_home

    stable_home = str(attempt_home(binding))
    assert observed["env"]["HERMES_HOME"] == stable_home
    assert observed["env"]["HERMES_EXECUTABLE"] == executable
    assert observed["env"]["OPENAI_BASE_URL"] == "https://first-route.invalid/v1"

    class FakeProcess:
        pid = 456

        @staticmethod
        def wait():
            return 0

    automatic_launch = {}
    monkeypatch.setattr(
        runner.subprocess, "Popen",
        lambda command, **kwargs: (
            automatic_launch.update(command=command, **kwargs) or FakeProcess()
        ),
    )
    monkeypatch.setattr(runner.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(meetings, "active_meeting_run", lambda *args: None)
    automatic = runner._launch_meeting_worker(binding_path, binding)
    automatic_binding = json.loads(Path(automatic["binding"]).read_text(encoding="utf-8"))
    assert (automatic_binding["provider"], automatic_binding["model"]) == (
        "default-provider", "default-model",
    )
    assert automatic_binding["hermes_home"] == stable_home
    assert automatic_launch["env"]["HERMES_HOME"] == stable_home
    assert automatic_launch["env"]["HERMES_EXECUTABLE"] == executable
    assert automatic_launch["env"]["OPENAI_BASE_URL"] == (
        "https://first-route.invalid/v1"
    )

    launched = []
    monkeypatch.setattr(management, "load_acquisition_batch", lambda *args: {})
    monkeypatch.setattr(meetings, "active_meeting_run", lambda *args: None)
    service = ManagementService(
        store=store, runtime_root=tmp_path / "runs",
        meeting_launcher=lambda value: launched.append(value) or 123,
    )
    service.start_meetings("handoff")
    assert (launched[0]["provider"], launched[0]["model"]) == (
        "default-provider", "default-model",
    )
    assert launched[0]["hermes_home"] == stable_home
    manual_launch = {}
    monkeypatch.setenv("RELOAD_TOKEN", "do-not-pass")
    monkeypatch.setenv("DEPLOYMENT_SECRET", "do-not-pass")
    monkeypatch.setattr(
        management.subprocess, "Popen",
        lambda command, **kwargs: (
            manual_launch.update(command=command, **kwargs) or FakeProcess()
        ),
    )
    service._launch_meeting_process(launched[0])
    assert manual_launch["env"]["HERMES_HOME"] == stable_home
    assert manual_launch["env"]["HERMES_EXECUTABLE"] == executable
    assert manual_launch["env"]["OPENAI_BASE_URL"] == "https://first-route.invalid/v1"
    assert "RELOAD_TOKEN" not in manual_launch["env"]
    assert "DEPLOYMENT_SECRET" not in manual_launch["env"]


def test_managed_authoring_reuses_home_without_task_override(tmp_path, monkeypatch):
    from scripts import run_climate_monitor as monitor

    binding = build_task_binding(
        _definition(tmp_path), task_version=1, run_id="authoring", attempt=1,
    )
    run_dir = Path(binding["checkpoint_dir"]).parent
    run_dir.mkdir(parents=True, exist_ok=True)
    binding_path = run_dir / "attempt-1.json"
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    loaded, _ = monitor._load_task_binding(str(binding_path))
    assert set(loaded).isdisjoint({"provider", "model"})
    (run_dir / IDENTITY_FILE).write_text(json.dumps(_identity(binding)), encoding="utf-8")

    help_text = "--query-file --max-turns --reasoning --ignore-rules"
    args = SimpleNamespace(
        model="ignored-model", model_provider="ignored-provider", authoring_timeout=30,
    )
    monitor._configure_managed_authoring(args, loaded)
    assert (args.model, args.model_provider) == ("", "")
    assert monitor._effective_authoring_identity(args) == (
        "default-model", "default-provider",
    )

    from climate_monitor.hermes_acquisition_hooks import attempt_home

    stable_home = str(attempt_home(binding))
    _write_session_route(Path(stable_home))
    executable = str((tmp_path / "host-bin" / "hermes").resolve())
    monkeypatch.setenv("HERMES_EXECUTABLE", executable)
    monkeypatch.setenv("PATH", str(tmp_path / "decoy-bin"))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"result": len(calls)}),
            stderr="session_id: 20260920_080000_abcdef",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    for name in ("url_authoring", "executive_authoring"):
        monitor._checkpointed_authoring(
            run_dir / f"{name}.json", name, args=args, help_stdout=help_text,
            validate=lambda raw: raw,
        )

    assert len(calls) == 2
    for command, kwargs in calls:
        assert command[0] == executable
        assert "--provider" not in command and "--model" not in command
        assert kwargs["env"]["HERMES_HOME"] == stable_home

    connection = sqlite3.connect(Path(stable_home) / "state.db")
    connection.execute(
        "INSERT INTO session_model_usage VALUES (?, ?, ?, '', 1, 2)",
        ("20260920_080000_abcdef", "drifted-model", "drifted-provider"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(SystemExit, match="effective identity changed.*fresh managed run"):
        monitor._checkpointed_authoring(
            run_dir / "drifted_authoring.json", "drifted", args=args,
            help_stdout=help_text, validate=lambda raw: raw,
        )


def test_managed_meeting_reuses_home_without_task_override(tmp_path, monkeypatch):
    from scripts import run_meeting_extraction as meeting_worker

    executable = str((tmp_path / "host-bin" / "hermes").resolve())
    monkeypatch.setenv("HERMES_EXECUTABLE", executable)
    monkeypatch.setenv("PATH", str(tmp_path / "decoy-bin"))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if "--help" in command:
            return SimpleNamespace(
                returncode=0,
                stdout="--query-file --max-turns --reasoning --ignore-rules",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"meetings": []}),
            stderr="session_id: 20260920_080000_abcdef",
        )

    monkeypatch.setattr(meeting_worker.subprocess, "run", fake_run)
    hermes_home = str((tmp_path / "hermes-runtime").resolve())
    _write_session_route(Path(hermes_home))
    body = "meeting evidence"
    result = meeting_worker._extractor(
        "default-provider", "default-model", hermes_home=hermes_home,
    )({
        "prompt": "Extract meetings.",
        "content_version_id": "content-1",
        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "source_url": "https://example.com/report",
        "article_body": body,
    })

    assert result == {"meetings": []}
    assert len(calls) == 2
    for command, kwargs in calls:
        assert command[0] == executable
        assert kwargs["env"]["HERMES_HOME"] == hermes_home
        if "--help" not in command:
            assert "--provider" not in command and "--model" not in command

    connection = sqlite3.connect(Path(hermes_home) / "state.db")
    connection.execute(
        "INSERT INTO session_model_usage VALUES (?, ?, ?, '', 1, 2)",
        ("20260920_080000_abcdef", "drifted-model", "drifted-provider"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="effective identity changed.*fresh managed run"):
        meeting_worker._extractor(
            "default-provider", "default-model", hermes_home=hermes_home,
        )({
            "prompt": "Extract meetings.",
            "content_version_id": "content-1",
            "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "source_url": "https://example.com/report",
            "article_body": body,
        })

    calls.clear()
    meeting_worker._extractor("legacy-provider", "legacy-model")({
        "prompt": "Extract meetings.",
        "content_version_id": "content-1",
        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "source_url": "https://example.com/report",
        "article_body": body,
    })
    legacy_command = calls[-1][0]
    assert legacy_command[legacy_command.index("--provider") + 1] == "legacy-provider"
    assert legacy_command[legacy_command.index("--model") + 1] == "legacy-model"


@pytest.mark.parametrize("selection", ["configured", "unset", "empty"])
def test_report_help_probe_uses_selected_hermes_executable(
    tmp_path, monkeypatch, selection,
):
    from scripts import run_climate_monitor as monitor

    executable = str((tmp_path / "host-bin" / "hermes").resolve())
    if selection == "configured":
        monkeypatch.setenv("HERMES_EXECUTABLE", executable)
        expected = executable
    elif selection == "empty":
        monkeypatch.setenv("HERMES_EXECUTABLE", "")
        expected = "hermes"
    else:
        monkeypatch.delenv("HERMES_EXECUTABLE", raising=False)
        expected = "hermes"
    monkeypatch.setenv("PATH", str(tmp_path / "decoy-bin"))
    staging = tmp_path / "staging"
    staging.mkdir()
    for name, payload in (
        ("bundle.json", {}),
        ("v2_authoring_request.json", {"articles": []}),
        ("article_evidence.json", {"records": []}),
    ):
        (staging / name).write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(monitor, "_read_staging_bundle", lambda path: {})
    monkeypatch.setattr(monitor, "_verify_authoring_resume", lambda *args: None)
    monkeypatch.setattr(monitor, "_verify_candidate_selection", lambda *args: None)
    monkeypatch.setattr(monitor, "_candidate_items_from_evidence", lambda *args: [])

    class ProbeSeen(Exception):
        pass

    def probe(command, **kwargs):
        assert command == [expected, "chat", "--help"]
        assert kwargs["env"].get("HERMES_EXECUTABLE") == {
            "configured": executable, "empty": "", "unset": None,
        }[selection]
        raise ProbeSeen

    monkeypatch.setattr("subprocess.run", probe)
    args = SimpleNamespace(
        staging_dir=str(staging), task_binding="", model="legacy-model",
        model_provider="legacy-provider",
    )
    with pytest.raises(ProbeSeen):
        monitor._run_authoring_sequence(args, None)


@pytest.mark.parametrize("selection", ["unset", "empty"])
def test_authoring_and_meeting_fall_back_to_path_hermes(monkeypatch, selection):
    from scripts import run_climate_monitor as monitor
    from scripts import run_meeting_extraction as meeting_worker

    if selection == "empty":
        monkeypatch.setenv("HERMES_EXECUTABLE", "")
    else:
        monkeypatch.delenv("HERMES_EXECUTABLE", raising=False)
    help_text = "--query-file --max-turns --reasoning --ignore-rules"
    command, _stdin = monitor._hermes_authoring_invocation(help_text, "author")
    assert command[0] == "hermes"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "--help" in command:
            return SimpleNamespace(returncode=0, stdout=help_text, stderr="")
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"meetings": []}),
            stderr="session_id: 20260920_080000_abcdef",
        )

    monkeypatch.setattr(meeting_worker.subprocess, "run", fake_run)
    body = "meeting evidence"
    meeting_worker._extractor("legacy-provider", "legacy-model")({
        "prompt": "Extract meetings.",
        "content_version_id": "content-1",
        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "source_url": "https://example.com/report",
        "article_body": body,
    })
    assert [call[0] for call in calls] == ["hermes", "hermes"]
