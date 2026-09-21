from __future__ import annotations

import hashlib
import json
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

    response = tmp_path / "failed.txt"
    response.write_text("provider authentication failed", encoding="utf-8")
    error = runner._hermes_process_error(
        response, 1, phase="acquisition", default_identity_pending=True,
    )
    assert "default configuration is missing or unusable" in error
    content_error = runner._hermes_process_error(
        response, 1, phase="acquisition", default_identity_pending=False,
    )
    assert "default configuration" not in content_error
    assert "Hermes acquisition process exited" in content_error


def test_environment_keeps_all_default_provider_credentials_but_not_app_secrets(monkeypatch):
    from scripts import run_agent_acquisition as runner

    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic")
    monkeypatch.setenv("CUSTOM_DEFAULT_API_KEY", "custom")
    monkeypatch.setenv("RELOAD_TOKEN", "do-not-pass")
    monkeypatch.setenv("DEPLOYMENT_SECRET", "do-not-pass")
    environment = runner._minimal_environment("openai")
    assert environment["OPENAI_API_KEY"] == "openai"
    assert environment["ANTHROPIC_API_KEY"] == "anthropic"
    assert environment["CUSTOM_DEFAULT_API_KEY"] == "custom"
    assert "RELOAD_TOKEN" not in environment
    assert "DEPLOYMENT_SECRET" not in environment


def test_run_home_freezes_ordinary_hermes_config_and_credentials(tmp_path, monkeypatch):
    import yaml
    from climate_monitor.hermes_acquisition_hooks import install_hooks

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

    (source_home / "config.yaml").write_text(
        "model:\n  provider: changed-provider\n  default: changed-model\n",
        encoding="utf-8",
    )
    resumed = dict(binding, attempt=2)
    install_hooks(
        [str(executable), "chat"], binding_path, resumed,
        {"PATH": str(tmp_path), "HERMES_HOME": str(source_home)},
    )
    still_frozen = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert still_frozen["model"] == frozen["model"]


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
    executable = str((tmp_path / "host-bin" / "hermes").resolve())
    monkeypatch.setenv("HERMES_EXECUTABLE", executable)
    monkeypatch.setenv("PATH", str(tmp_path / "decoy-bin"))

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
