from __future__ import annotations

import sys
import threading
import os
import time
import json
import shutil
import socket
import subprocess
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from climate_monitor.managed_backend import (
    PROTOCOL_VERSION,
    TOKEN_HEADER,
    create_managed_backend_app,
    history_service_from_environment,
    management_service_from_environment,
)
from climate_monitor.management import (
    ManagementService,
    TaskDefinitionStore,
    build_task_binding,
    default_task_definition,
)


def _capabilities(tmp_path: Path) -> dict:
    return {
        "protocol": PROTOCOL_VERSION,
        "backend": "host-dashboard",
        "runtime_root": str(tmp_path / "host-runs"),
        "active_task_path": str(tmp_path / "host-task.json"),
        "host": {
            "user": "host-user",
            "uid": 1001,
            "python": "/host/venv/bin/python",
            "hermes_home": "/home/host-user/.hermes",
            "hermes_version": "0.20.5",
            "hermes_executable": "/host/venv/bin/hermes",
        },
    }


class _RecordingStore:
    active_path = Path("/host/task.json")

    def __init__(self, calls): self.calls = calls
    def load(self, **kwargs): self.calls.append(("load", kwargs)); return {"definition": {}}
    def versions(self): self.calls.append(("versions", {})); return []
    def version(self, **kwargs): self.calls.append(("version", kwargs)); return kwargs
    def diff(self, **kwargs): self.calls.append(("diff", kwargs)); return kwargs
    def preview(self, **kwargs): self.calls.append(("preview", kwargs)); return kwargs
    def save(self, **kwargs): self.calls.append(("save", kwargs)); return kwargs
    def restore(self, **kwargs): self.calls.append(("restore", kwargs)); return kwargs


class _RecordingService:
    runtime_root = Path("/host/runs")

    def __init__(self):
        self.calls = []
        self.store = _RecordingStore(self.calls)

    def __getattr__(self, name):
        def called(*args, **kwargs):
            if args:
                kwargs = {"_args": args, **kwargs}
            self.calls.append((name, kwargs))
            return {"operation": name, **kwargs}
        return called


def _post(client, operation, **payload):
    return client.post(
        f"/v1/{operation}", json=payload,
        headers={TOKEN_HEADER: "relay-token"},
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    result = {}
    for path in root.rglob("*"):
        key = ("file:" if path.is_file() else "dir:") + str(path.relative_to(root))
        result[key] = path.read_bytes() if path.is_file() else b""
    return result


def _materialized_service(tmp_path: Path) -> ManagementService:
    from climate_registry.persistent import initialize_registry

    run_root = tmp_path / "runs"
    run_root.mkdir()
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    definition = default_task_definition()
    definition["parameters"].update(
        report_date="2026-09-07", source_keys=["wmo"], timezone="UTC",
    )
    definition["runtime"].update(
        registry_database=str(database), run_root=str(run_root),
    )
    store = TaskDefinitionStore(tmp_path / "task.json", tmp_path / "versions")
    store.save(definition, actor="test")
    return ManagementService(
        store=store, runtime_root=run_root, execution_backend="host-dashboard",
    )


@pytest.mark.parametrize("selection", ["configured", "relative", "unset", "empty"])
def test_host_capabilities_selects_configured_or_path_hermes(
    tmp_path, monkeypatch, selection,
):
    import climate_monitor.managed_backend as managed_backend

    executable = tmp_path / "host-bin" / "hermes"
    executable.parent.mkdir()
    executable.write_text("test executable", encoding="utf-8")
    relative = str(executable.relative_to(tmp_path))
    monkeypatch.chdir(tmp_path)
    if selection == "configured":
        monkeypatch.setenv("HERMES_EXECUTABLE", str(executable.resolve()))
    elif selection == "relative":
        monkeypatch.setenv("HERMES_EXECUTABLE", relative)
    elif selection == "empty":
        monkeypatch.setenv("HERMES_EXECUTABLE", "")
    else:
        monkeypatch.delenv("HERMES_EXECUTABLE", raising=False)
    lookups = []

    def lookup(value):
        lookups.append(value)
        return relative if selection == "relative" else str(executable.resolve())

    monkeypatch.setattr(
        managed_backend.shutil, "which", lookup,
    )
    monkeypatch.setattr(
        managed_backend.importlib.metadata, "version", lambda _name: "0.20.5",
    )
    probes = []

    def probe(command, **kwargs):
        probes.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "--ignore-rules --max-turns --query-file --quiet "
                "--reasoning --resume --source --toolsets"
            ),
            stderr="",
        )

    monkeypatch.setattr(managed_backend.subprocess, "run", probe)
    capabilities = managed_backend.host_capabilities()

    assert lookups == {
        "configured": [], "relative": [relative], "unset": ["hermes"],
        "empty": ["hermes"],
    }[selection]
    assert probes[0][0] == [str(executable.resolve()), "chat", "--help"]
    assert capabilities["host"]["hermes_executable"] == str(executable.resolve())
    assert os.environ["HERMES_EXECUTABLE"] == str(executable.resolve())


def test_host_capability_probe_failure_does_not_freeze_executable(tmp_path, monkeypatch):
    import climate_monitor.managed_backend as managed_backend

    executable = tmp_path / "host-bin" / "hermes"
    executable.parent.mkdir()
    executable.write_text("test executable", encoding="utf-8")
    relative = str(executable.relative_to(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_EXECUTABLE", relative)
    monkeypatch.setattr(managed_backend.shutil, "which", lambda _value: relative)
    monkeypatch.setattr(
        managed_backend.importlib.metadata, "version", lambda _name: "0.20.5",
    )
    monkeypatch.setattr(
        managed_backend.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="failed"),
    )

    with pytest.raises(RuntimeError, match="chat contract is incompatible"):
        managed_backend.host_capabilities()
    assert os.environ["HERMES_EXECUTABLE"] == relative


def test_host_readiness_freezes_relative_executable_for_acquisition_child(
    tmp_path, monkeypatch,
):
    import climate_monitor.managed_backend as managed_backend
    from climate_registry.persistent import initialize_registry
    from scripts import run_agent_acquisition as runner

    executable = tmp_path / "host-bin" / "hermes"
    executable.parent.mkdir()
    executable.write_text("test executable", encoding="utf-8")
    relative = str(executable.relative_to(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_EXECUTABLE", relative)
    monkeypatch.setattr(managed_backend.shutil, "which", lambda _value: relative)
    monkeypatch.setattr(
        managed_backend.importlib.metadata, "version", lambda _name: "0.20.5",
    )
    monkeypatch.setattr(
        managed_backend.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "--ignore-rules --max-turns --query-file --quiet "
                "--reasoning --resume --source --toolsets"
            ),
            stderr="",
        ),
    )
    managed_backend.host_capabilities()

    monkeypatch.setenv("CLIMATE_REPOSITORY_COMMIT_SHA", "a" * 40)
    database = tmp_path / "registry.sqlite3"
    run_root = tmp_path / "runs"
    initialize_registry(database)
    run_root.mkdir()
    definition = default_task_definition()
    definition["parameters"].update(
        report_date="2026-09-07", source_keys=["wmo"], timezone="UTC",
    )
    definition["runtime"].update(
        registry_database=str(database), run_root=str(run_root),
    )
    binding = build_task_binding(
        definition, task_version=1, run_id="relative-executable", attempt=1,
    )
    binding_path = Path(binding["checkpoint_dir"]).parent / "attempt-1.json"
    binding_path.parent.mkdir(parents=True)
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    prompt = binding_path.parent / "prompt.md"
    prompt.write_text("test", encoding="utf-8")
    run_home = binding_path.parent / "hermes-runtime"
    run_home.mkdir()
    launched = {}

    def install(command, supplied_path, supplied_binding, environment):
        assert supplied_path == binding_path
        assert supplied_binding == binding
        launched["hook_command"] = command
        return environment, run_home

    class Process:
        pid = 123

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait():
            return 0

    monkeypatch.setattr(runner, "install_hooks", install)
    monkeypatch.setattr(
        runner, "RequestBudget",
        lambda *args, **kwargs: SimpleNamespace(remaining_seconds=lambda: 60),
    )
    monkeypatch.setattr(runner, "bind_effective_identity", lambda *args: None)
    monkeypatch.setattr(
        runner.subprocess, "Popen",
        lambda command, **kwargs: launched.update(command=command, **kwargs) or Process(),
    )
    command = runner._hermes_command(
        os.environ["HERMES_EXECUTABLE"], binding, prompt,
    )
    assert runner._invoke_hermes(
        command, binding_path.parent / "response.txt", binding_path, binding,
        runner.time.monotonic() + 60,
    ) == 0
    frozen = str(executable.resolve())
    assert launched["command"][0] == frozen
    assert launched["hook_command"][0] == frozen
    assert launched["cwd"] == run_home
    assert launched["env"]["HERMES_EXECUTABLE"] == frozen


def test_bounded_host_backend_exposes_only_fixed_operations_and_host_identity(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", "/container/hermes")
    monkeypatch.setenv("OPENAI_API_KEY", "container-secret-must-not-cross-relay")
    service = _RecordingService()
    app = create_managed_backend_app(
        service, capability_loader=lambda: _capabilities(tmp_path),
        token_loader=lambda: "relay-token",
    )
    with TestClient(app) as client:
        capabilities = _post(client, "capabilities").json()["result"]
        assert capabilities["host"] == _capabilities(tmp_path)["host"]
        assert capabilities["host"]["hermes_home"] != "/container/hermes"
        assert capabilities["host"]["python"] != "C:/container/python.exe"
        assert "container-secret-must-not-cross-relay" not in str(capabilities)
        assert capabilities["runtime_root"] == str(service.runtime_root)
        assert _post(client, "run_start", trigger="manual").json()["result"]["operation"] == "start"
        assert _post(client, "run_progress", run_id="run-1").json()["result"]["operation"] == "progress"
        assert _post(client, "meetings_start", run_id="run-1", retry_failed=False).json()["result"]["operation"] == "start_meetings"
        assert _post(client, "meeting_snapshot_load", snapshot_id="snap-1").json()["result"]["operation"] == "load_meeting_snapshot"
        rejected = _post(client, "shell", command="id").json()
    assert rejected == {
        "ok": False,
        "error": {"type": "KeyError", "message": "'unsupported managed backend operation'"},
    }
    assert all("provider" not in kwargs and "model" not in kwargs for _, kwargs in service.calls)


def test_host_backend_rejects_requests_without_the_relay_token(tmp_path):
    service = _RecordingService()
    app = create_managed_backend_app(
        service, capability_loader=lambda: _capabilities(tmp_path),
        token_loader=lambda: "relay-token",
    )
    with TestClient(app) as client:
        response = client.post("/v1/run_start", json={"trigger": "manual"})
    assert response.status_code == 401
    assert service.calls == []


def test_history_socket_allows_reads_and_rejects_every_mutation(tmp_path):
    service = _RecordingService()
    app = create_managed_backend_app(
        service, capability_loader=lambda: _capabilities(tmp_path),
        token_loader=lambda: "relay-token", read_only=True,
    )
    mutations = {
        "config_preview": {"definition": {}},
        "config_save": {"definition": {}, "actor": "x"},
        "config_restore": {"version": 1, "expected_version": 1, "actor": "x"},
        "run_start": {"trigger": "manual"},
        "run_resume": {"run_id": "same"},
        "run_attach_or_resume": {"run_id": "same"},
        "meetings_start": {"run_id": "same", "retry_failed": True},
        "meeting_snapshot_freeze": {},
    }
    with TestClient(app) as client:
        capabilities = _post(client, "capabilities").json()["result"]
        assert capabilities["read_only"] is True
        assert _post(client, "config_version", version=1).json()["ok"] is True
        calls_after_read = list(service.calls)
        for operation, payload in mutations.items():
            rejected = _post(client, operation, **payload).json()
            assert rejected["ok"] is False
            assert "history backend is read-only" in rejected["error"]["message"]
    assert service.calls == calls_after_read


def test_host_backend_routes_config_crud_run_readback_resume_and_meetings_to_one_service(tmp_path):
    service = _RecordingService()
    app = create_managed_backend_app(
        service, capability_loader=lambda: _capabilities(tmp_path),
        token_loader=lambda: "relay-token",
    )
    operations = [
        ("config_load", {}),
        ("config_versions", {}),
        ("config_version", {"version": 1}),
        ("config_preview", {"definition": {"task_id": "weekly"}}),
        ("config_save", {"definition": {}, "expected_version": 1, "actor": "operator"}),
        ("config_restore", {"version": 1, "expected_version": 2, "actor": "operator"}),
        ("runs_list", {}),
        ("run_binding", {"run_id": "run-1"}),
        ("run_attach_or_resume", {"run_id": "run-1"}),
        ("run_result", {"run_id": "run-1", "attempt": 1}),
        ("item_detail", {"run_id": "run-1", "item_id": "article"}),
        ("meetings_progress", {"run_id": "run-1"}),
        ("meetings_events", {"organizer": "A"}),
        ("meeting_snapshot_freeze", {"organizer": "A"}),
    ]
    with TestClient(app) as client:
        for operation, payload in operations:
            response = _post(client, operation, **payload)
            assert response.status_code == 200
            assert response.json()["ok"] is True
    assert len(service.calls) == len(operations)


def test_manage_routes_use_only_the_selected_host_service(monkeypatch):
    import api_server

    service = _RecordingService()
    monkeypatch.setattr(api_server, "management_service", service)
    principal = object()
    assert api_server.console_config(principal) == {"definition": {}}
    assert api_server.console_start({}, principal)["operation"] == "start"
    assert api_server.console_run_progress("run-1", principal)["operation"] == "progress"
    assert api_server.console_start_meetings(
        "run-1", {"retry_failed": False}, principal,
    )["operation"] == "start_meetings"
    assert api_server.console_meeting_snapshot(
        "snapshot-1", principal,
    )["operation"] == "load_meeting_snapshot"
    assert all("provider" not in kwargs and "model" not in kwargs for _, kwargs in service.calls)


def test_manage_routes_report_unavailable_and_incompatible_backend_without_fallback(monkeypatch):
    import api_server

    monkeypatch.setattr(api_server, "management_service", None)
    monkeypatch.setattr(
        api_server, "management_service_from_environment",
        lambda: (_ for _ in ()).throw(FileNotFoundError("host managed backend is unavailable")),
    )
    with pytest.raises(api_server.HTTPException) as unavailable:
        api_server.console_config(object())
    assert unavailable.value.status_code == 503
    assert unavailable.value.detail == "host managed backend is unavailable"

    monkeypatch.setattr(
        api_server, "management_service_from_environment",
        lambda: (_ for _ in ()).throw(RuntimeError("host managed backend protocol is incompatible")),
    )
    with pytest.raises(api_server.HTTPException) as incompatible:
        api_server.console_versions(object())
    assert incompatible.value.status_code == 409
    assert incompatible.value.detail == "host managed backend protocol is incompatible"


def test_history_api_keeps_overlapping_backend_identities_and_is_read_only(monkeypatch):
    import api_server

    class Store:
        def __init__(self, backend): self.backend = backend
        def versions(self): return [{"version": 1, "definition_sha256": self.backend}]
        def version(self, version): return {"version": version, "hashes": {"definition_sha256": self.backend}}
        def diff(self, old_version, new_version): return {"old_version": old_version, "new_version": new_version}

    class History:
        def __init__(self, backend): self.backend, self.store = backend, Store(backend)
        def list_runs(self): return [{"run_id": "same-run", "stage": "completed"}]
        def binding(self, run_id): return {"run_id": run_id, "execution_backend": self.backend}
        def progress(self, run_id): return {"run_id": run_id, "items": [{"item_id": "same-item"}]}
        def meeting_progress(self, run_id): return {"acquisition_run_id": run_id, "runs": []}
        def item_detail(self, run_id, item_id): return {"run_id": run_id, "item_id": item_id}
        def meeting_events(self, **filters): return {"filters": filters, "events": []}
        def load_meeting_snapshot(self, snapshot_id): return {"snapshot_id": snapshot_id}

    services = {backend: History(backend) for backend in ("local", "host-dashboard")}
    monkeypatch.setattr(api_server, "active_backend_from_environment", lambda: "host-dashboard")
    monkeypatch.setattr(api_server, "_history_service", services.__getitem__)
    principal = object()

    sources = api_server.console_history_sources(principal)
    assert sources == [
        {"backend": "local", "active": False, "available": True},
        {"backend": "host-dashboard", "active": True, "available": True},
    ]
    assert api_server.console_history_versions("local", principal)[0]["backend"] == "local"
    assert api_server.console_history_versions("host-dashboard", principal)[0]["backend"] == "host-dashboard"
    assert api_server.console_history_version("local", 1, principal)["version"]["hashes"]["definition_sha256"] == "local"
    assert api_server.console_history_version("host-dashboard", 1, principal)["version"]["hashes"]["definition_sha256"] == "host-dashboard"
    assert api_server.console_history_diff("local", 1, 1, principal)["backend"] == "local"
    assert api_server.console_history_runs("local", principal)[0] == {
        "run_id": "same-run", "stage": "completed", "backend": "local",
    }
    assert api_server.console_history_runs("host-dashboard", principal)[0]["backend"] == "host-dashboard"
    detail = api_server.console_history_run("host-dashboard", "same-run", principal)
    assert (detail["backend"], detail["run_id"]) == ("host-dashboard", "same-run")
    assert detail["meetings"] == {"acquisition_run_id": "same-run", "runs": []}
    assert api_server.console_history_item(
        "local", "same-run", "same-item", principal,
    )["backend"] == "local"
    assert api_server.console_history_meetings(
        "host-dashboard", principal,
    )["backend"] == "host-dashboard"
    assert api_server.console_history_snapshot(
        "local", "same-snapshot", principal,
    )["snapshot"]["snapshot_id"] == "same-snapshot"
    with pytest.raises(api_server.HTTPException) as rejected:
        api_server.reject_history_mutation("local", "runs/same-run/resume", principal)
    assert rejected.value.status_code == 409


def test_history_source_reports_disconnected_archive_without_hiding_local(monkeypatch):
    import api_server

    local = object()
    monkeypatch.setattr(api_server, "active_backend_from_environment", lambda: "local")
    monkeypatch.setattr(
        api_server, "_history_service",
        lambda backend: local if backend == "local" else (_ for _ in ()).throw(
            FileNotFoundError("host-dashboard archive is unavailable")
        ),
    )
    assert api_server.console_history_sources(object()) == [
        {"backend": "local", "active": True, "available": True},
        {
            "backend": "host-dashboard", "active": False,
            "available": False, "reason": "host-dashboard archive is unavailable",
        },
    ]


def test_history_direct_read_rejects_binding_from_another_backend(monkeypatch):
    import api_server

    service = SimpleNamespace(
        binding=lambda run_id: {"run_id": run_id, "execution_backend": "local"},
        progress=lambda run_id: pytest.fail("mismatched binding must stop before read"),
        meeting_progress=lambda run_id: pytest.fail("mismatched binding must stop before read"),
    )
    monkeypatch.setattr(api_server, "_history_service", lambda backend: service)
    with pytest.raises(api_server.HTTPException) as rejected:
        api_server.console_history_run("host-dashboard", "same-run", object())
    assert rejected.value.status_code == 409
    assert "frozen to local" in rejected.value.detail


def test_history_run_core_failures_and_unknown_meeting_errors_are_not_hidden(monkeypatch):
    import api_server

    missing = SimpleNamespace(
        binding=lambda run_id: (_ for _ in ()).throw(FileNotFoundError("missing run")),
        progress=lambda run_id: pytest.fail("missing binding must stop before progress"),
        meeting_progress=lambda run_id: pytest.fail("missing binding must stop before meetings"),
    )
    monkeypatch.setattr(api_server, "_history_service", lambda backend: missing)
    with pytest.raises(api_server.HTTPException) as not_found:
        api_server.console_history_run("local", "missing-run", object())
    assert not_found.value.status_code == 503

    broken_meeting = SimpleNamespace(
        binding=lambda run_id: {"run_id": run_id, "execution_backend": "local"},
        progress=lambda run_id: {"run_id": run_id, "stage": "retained"},
        meeting_progress=lambda run_id: (_ for _ in ()).throw(
            RuntimeError("unexpected meeting failure")
        ),
    )
    monkeypatch.setattr(api_server, "_history_service", lambda backend: broken_meeting)
    with pytest.raises(api_server.HTTPException) as unknown:
        api_server.console_history_run("local", "retained-run", object())
    assert unknown.value.status_code == 409
    assert unknown.value.detail == "unexpected meeting failure"


def test_history_run_marks_incompatible_meeting_schema_unavailable(monkeypatch):
    import api_server

    service = SimpleNamespace(
        binding=lambda run_id: {"run_id": run_id, "execution_backend": "local"},
        progress=lambda run_id: {"run_id": run_id, "stage": "retained"},
        meeting_progress=lambda run_id: (_ for _ in ()).throw(
            ValueError("registry schema is incompatible")
        ),
    )
    monkeypatch.setattr(api_server, "_history_service", lambda backend: service)
    detail = api_server.console_history_run("local", "retained-run", object())
    assert detail["progress"]["stage"] == "retained"
    assert detail["meetings"] == {
        "status": "unavailable",
        "reason": "Meeting evidence is unavailable for this archived run.",
    }


@pytest.mark.parametrize("state", ["missing", "bootstrap"])
def test_inactive_local_archive_never_bootstraps_or_changes_files(
    monkeypatch, tmp_path, state,
):
    import api_server
    import climate_monitor.management as management
    import climate_registry.persistent as persistent

    task_path = tmp_path / "task.json"
    if state == "bootstrap":
        task_path.write_text(json.dumps({
            "schema_version": "climate-acquisition-task-bootstrap.v1",
            "created_at": "2026-09-10T00:00:00Z",
        }), encoding="utf-8")
    before = _tree_bytes(tmp_path)
    monkeypatch.setenv("CLIMATE_TASK_CONFIG", str(task_path))
    monkeypatch.setenv("CLIMATE_TASK_VERSION_DIR", str(tmp_path / "versions"))
    monkeypatch.setenv("CLIMATE_ACQUISITION_RUN_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("HERMES_MANAGED_SOCKET", str(tmp_path / "host.sock"))
    monkeypatch.delenv("HERMES_DASHBOARD_SOCKET", raising=False)

    def side_effect(*_args, **_kwargs):
        pytest.fail("archive discovery attempted a write/bootstrap/lock/launch seam")

    monkeypatch.setattr(management, "default_task_definition", side_effect)
    monkeypatch.setattr(management, "_atomic_write", side_effect)
    monkeypatch.setattr(management, "_exclusive_lock", side_effect)
    monkeypatch.setattr(persistent, "initialize_registry", side_effect)
    monkeypatch.setattr(ManagementService, "_launch_process", side_effect)
    monkeypatch.setattr(Path, "mkdir", side_effect)

    with pytest.raises(FileNotFoundError, match="not configured|not materialized"):
        history_service_from_environment("local")

    real_history = api_server._history_service
    monkeypatch.setattr(api_server, "active_backend_from_environment", lambda: "host-dashboard")
    monkeypatch.setattr(
        api_server, "_history_service",
        lambda backend: real_history(backend) if backend == "local" else object(),
    )
    sources = api_server.console_history_sources(object())
    assert sources[0]["backend"] == "local"
    assert sources[0]["available"] is False
    with pytest.raises(api_server.HTTPException) as unavailable:
        api_server.console_history_versions("local", object())
    assert unavailable.value.status_code == 503
    assert _tree_bytes(tmp_path) == before


def test_materialized_local_archive_reads_without_any_file_change(monkeypatch, tmp_path):
    import climate_monitor.management as management
    import climate_registry.persistent as persistent

    run_root = tmp_path / "runs"
    run_root.mkdir()
    database = tmp_path / "registry.sqlite3"
    persistent.initialize_registry(database)
    definition = default_task_definition()
    definition["parameters"].update(
        report_date="2026-09-07", source_keys=["wmo"], timezone="UTC",
    )
    definition["runtime"].update(
        registry_database=str(database), run_root=str(run_root),
    )
    task_path = tmp_path / "task.json"
    store = TaskDefinitionStore(task_path, tmp_path / "versions")
    saved = store.save(definition, actor="test")
    before = _tree_bytes(tmp_path)
    monkeypatch.setenv("CLIMATE_TASK_CONFIG", str(task_path))
    monkeypatch.setenv("CLIMATE_TASK_VERSION_DIR", str(tmp_path / "versions"))
    monkeypatch.setenv("HERMES_MANAGED_SOCKET", str(tmp_path / "host.sock"))
    monkeypatch.delenv("HERMES_DASHBOARD_SOCKET", raising=False)

    def side_effect(*_args, **_kwargs):
        pytest.fail("materialized archive read attempted a mutation seam")

    monkeypatch.setattr(management, "default_task_definition", side_effect)
    monkeypatch.setattr(management, "_atomic_write", side_effect)
    monkeypatch.setattr(management, "_exclusive_lock", side_effect)
    monkeypatch.setattr(persistent, "initialize_registry", side_effect)
    monkeypatch.setattr(ManagementService, "_launch_process", side_effect)
    monkeypatch.setattr(Path, "mkdir", side_effect)

    archive = history_service_from_environment("local")
    assert archive.store.load()["hashes"]["definition_sha256"] == saved["hashes"]["definition_sha256"]
    assert archive.store.versions()[0]["version"] == 1
    assert archive.list_runs() == []
    with pytest.raises(RuntimeError, match="read-only"):
        archive.store.save(definition, actor="forbidden")
    assert _tree_bytes(tmp_path) == before


def test_history_run_detail_keeps_real_progress_when_registry_is_missing(monkeypatch, tmp_path):
    import api_server
    import climate_monitor.management as management
    import climate_registry.persistent as persistent

    materialized = _materialized_service(tmp_path)
    writer = ManagementService(
        store=materialized.store, runtime_root=materialized.runtime_root,
        execution_backend="local", launcher=lambda binding: 123,
    )
    started = writer.start(
        trigger="manual", now=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )
    binding = writer.binding(started["run_id"])
    Path(binding["registry_database"]).unlink()
    before = _tree_bytes(tmp_path)
    monkeypatch.setenv("CLIMATE_TASK_CONFIG", str(materialized.store.active_path))
    monkeypatch.setenv("CLIMATE_TASK_VERSION_DIR", str(materialized.store.version_root))
    monkeypatch.setenv("HERMES_MANAGED_SOCKET", str(tmp_path / "host.sock"))
    monkeypatch.delenv("HERMES_DASHBOARD_SOCKET", raising=False)

    def side_effect(*_args, **_kwargs):
        pytest.fail("history run detail attempted a write/bootstrap/lock/launch seam")

    monkeypatch.setattr(management, "default_task_definition", side_effect)
    monkeypatch.setattr(management, "_atomic_write", side_effect)
    monkeypatch.setattr(management, "_exclusive_lock", side_effect)
    monkeypatch.setattr(persistent, "initialize_registry", side_effect)
    monkeypatch.setattr(ManagementService, "_launch_process", side_effect)
    monkeypatch.setattr(Path, "mkdir", side_effect)
    api_server.app.dependency_overrides[api_server.current_console_user] = lambda: object()
    try:
        with TestClient(api_server.app) as client:
            response = client.get(
                f"/api/manage/history/local/runs/{started['run_id']}"
            )
    finally:
        api_server.app.dependency_overrides.pop(api_server.current_console_user, None)

    assert response.status_code == 200
    detail = response.json()
    assert (detail["backend"], detail["run_id"]) == ("local", started["run_id"])
    assert detail["binding"]["execution_backend"] == "local"
    assert detail["progress"]["run_id"] == started["run_id"]
    assert detail["progress"]["stage"] == "running"
    assert detail["meetings"] == {
        "status": "unavailable",
        "reason": "Meeting evidence is unavailable for this archived run.",
    }
    assert _tree_bytes(tmp_path) == before


def test_history_only_service_reports_unmaterialized_content_without_bootstrap(
    monkeypatch, tmp_path,
):
    import climate_monitor.management as management

    task_path = tmp_path / "task.json"
    task_path.write_text(
        '{"schema_version":"climate-acquisition-task-bootstrap.v1"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLIMATE_TASK_CONFIG", str(task_path))
    monkeypatch.setenv("CLIMATE_TASK_VERSION_DIR", str(tmp_path / "versions"))
    monkeypatch.setattr(
        management, "default_task_definition",
        lambda: pytest.fail("history-only service bootstrapped a definition"),
    )
    before = _tree_bytes(tmp_path)
    service = ManagementService.archive_from_environment(
        execution_backend="host-dashboard",
    )
    app = create_managed_backend_app(
        service, capability_loader=lambda: _capabilities(tmp_path),
        token_loader=lambda: "relay-token", read_only=True,
    )
    with TestClient(app) as client:
        capabilities = _post(client, "capabilities").json()["result"]
        assert capabilities["read_only"] is True
        assert capabilities["archive_available"] is False
        unavailable = _post(client, "config_load").json()
        assert unavailable["ok"] is False
        assert "not materialized" in unavailable["error"]["message"]
        runs = _post(client, "runs_list").json()
        assert runs["ok"] is False
        assert "not materialized" in runs["error"]["message"]
    assert _tree_bytes(tmp_path) == before


def test_external_mode_fails_closed_without_backend_and_local_mode_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_DASHBOARD_SOCKET", str(tmp_path / "dashboard.sock"))
    monkeypatch.setenv(
        "HERMES_MANAGED_HISTORY_SOCKET", str(tmp_path / "history.sock"),
    )
    monkeypatch.delenv("HERMES_MANAGED_SOCKET", raising=False)
    with pytest.raises(RuntimeError, match="requires the host managed backend"):
        management_service_from_environment()

    monkeypatch.delenv("HERMES_DASHBOARD_SOCKET")
    monkeypatch.setattr(
        ManagementService, "from_environment", classmethod(lambda cls: "local-service")
    )
    assert management_service_from_environment() == "local-service"


def test_managed_socket_alone_selects_host_backend_for_host_cli(monkeypatch, tmp_path):
    import climate_monitor.managed_backend as backend

    selected = object()
    monkeypatch.setenv("HERMES_MANAGED_SOCKET", str(tmp_path / "managed.sock"))
    monkeypatch.delenv("HERMES_DASHBOARD_SOCKET", raising=False)
    monkeypatch.setattr(backend, "HostManagementService", lambda path: selected)
    monkeypatch.setattr(
        ManagementService, "from_environment",
        classmethod(lambda cls: pytest.fail("managed socket must not fall back locally")),
    )
    assert management_service_from_environment() is selected


@pytest.mark.skipif(os.name == "nt", reason="requires a real Unix socket and fcntl")
def test_managed_socket_only_factory_and_scheduler_preflight_use_host_binding(
    monkeypatch, tmp_path,
):
    import uvicorn
    from climate_registry.persistent import initialize_registry
    from scripts import hermes_job

    run_root = tmp_path / "runs"
    run_root.mkdir()
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    definition = default_task_definition()
    definition["parameters"].update(
        report_date="2026-09-07", source_keys=["wmo"], timezone="UTC",
    )
    definition["runtime"].update(
        registry_database=str(database), run_root=str(run_root),
    )
    store = TaskDefinitionStore(tmp_path / "task.json", tmp_path / "versions")
    store.save(definition, actor="test")
    launched = []
    host_service = ManagementService(
        store=store, runtime_root=run_root, execution_backend="host-dashboard",
        launcher=lambda binding: launched.append(binding) or 101,
    )
    app = create_managed_backend_app(
        host_service, capability_loader=lambda: _capabilities(tmp_path),
        token_loader=lambda: "relay-token",
    )
    socket_path = tmp_path / "managed.sock"
    server = uvicorn.Server(uvicorn.Config(
        app, uds=str(socket_path), log_level="warning", access_log=False,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert socket_path.exists()

    for name in ("sources", "wiki", "state", "ledger"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("HERMES_MANAGED_SOCKET", str(socket_path))
    monkeypatch.delenv("HERMES_DASHBOARD_SOCKET", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN_FILE", raising=False)
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "relay-token")
    monkeypatch.setenv("CLIMATE_MANAGED_SOURCE_DIR", str(tmp_path / "sources"))
    monkeypatch.setenv("CLIMATE_MANAGED_WIKI_DIR", str(tmp_path / "wiki"))
    monkeypatch.setenv("CLIMATE_MANAGED_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLIMATE_SOURCE_DIR", str(tmp_path / "sources"))
    monkeypatch.setenv("CLIMATE_RUN_LEDGER_DIR", str(tmp_path / "ledger"))
    try:
        selected = hermes_job.managed_monitor_preflight("2026-09-07")
        started = selected.start(
            trigger="scheduled", now=datetime(2026, 9, 7, tzinfo=timezone.utc),
        )
        binding = selected.binding(started["run_id"])
        assert binding["execution_backend"] == "host-dashboard"
        assert launched[0]["execution_backend"] == "host-dashboard"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_history_socket_never_selects_host_execution(monkeypatch, tmp_path):
    import climate_monitor.managed_backend as backend

    local = object()
    host_history = SimpleNamespace(
        store=SimpleNamespace(load=lambda: {"version": 1}),
        capabilities={"archive_available": True},
    )
    monkeypatch.delenv("HERMES_MANAGED_SOCKET", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_SOCKET", raising=False)
    monkeypatch.setenv(
        "HERMES_MANAGED_HISTORY_SOCKET", str(tmp_path / "history.sock"),
    )
    monkeypatch.setattr(
        ManagementService, "from_environment", classmethod(lambda cls: local),
    )
    monkeypatch.setattr(
        backend, "HostManagementService",
        lambda path, read_only=False: host_history if read_only else pytest.fail(
            "history connection must not select host execution"
        ),
    )
    assert management_service_from_environment() is local
    assert history_service_from_environment("host-dashboard") is host_history


def test_history_socket_reports_unmaterialized_host_archive(monkeypatch, tmp_path):
    import climate_monitor.managed_backend as backend

    unavailable = SimpleNamespace(capabilities={
        "archive_available": False,
        "archive_error": "task history is not materialized",
    })
    monkeypatch.delenv("HERMES_MANAGED_SOCKET", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_SOCKET", raising=False)
    monkeypatch.setenv("HERMES_MANAGED_HISTORY_SOCKET", str(tmp_path / "history.sock"))
    monkeypatch.setattr(
        backend, "HostManagementService",
        lambda path, read_only=False: unavailable,
    )
    with pytest.raises(FileNotFoundError, match="not materialized"):
        history_service_from_environment("host-dashboard")


def test_host_active_reads_local_archive_without_using_host_paths(monkeypatch, tmp_path):
    import climate_monitor.managed_backend as backend

    active_host = object()
    local_archive = SimpleNamespace(archive_error=None)
    monkeypatch.setenv("HERMES_MANAGED_SOCKET", str(tmp_path / "managed.sock"))
    monkeypatch.delenv("HERMES_DASHBOARD_SOCKET", raising=False)
    monkeypatch.setattr(backend, "HostManagementService", lambda path: active_host)
    monkeypatch.setattr(
        ManagementService, "archive_from_environment", classmethod(lambda cls: local_archive),
    )
    assert management_service_from_environment() is active_host
    assert history_service_from_environment("local") is local_archive


def test_history_relay_disconnect_does_not_change_active_local_backend(monkeypatch, tmp_path):
    local = object()
    monkeypatch.delenv("HERMES_MANAGED_SOCKET", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_SOCKET", raising=False)
    monkeypatch.setenv(
        "HERMES_MANAGED_HISTORY_SOCKET", str(tmp_path / "missing-history.sock"),
    )
    monkeypatch.setenv(
        "HERMES_DASHBOARD_SESSION_TOKEN_FILE", str(tmp_path / "missing-token"),
    )
    monkeypatch.setattr(
        ManagementService, "from_environment", classmethod(lambda cls: local),
    )
    assert management_service_from_environment() is local
    with pytest.raises((FileNotFoundError, RuntimeError), match="unavailable|token file"):
        history_service_from_environment("host-dashboard")


def test_incompatible_host_protocol_is_rejected_before_accepting_operations():
    with pytest.raises(RuntimeError, match="protocol is incompatible"):
        create_managed_backend_app(
            _RecordingService(),
            capability_loader=lambda: {"protocol": "future-protocol"},
            token_loader=lambda: "relay-token",
        )


def test_external_mode_never_falls_back_when_host_backend_is_unavailable(monkeypatch, tmp_path):
    token_file = tmp_path / "session-token"
    token_file.write_text("relay-token", encoding="utf-8")
    monkeypatch.setenv("HERMES_DASHBOARD_SOCKET", str(tmp_path / "dashboard.sock"))
    monkeypatch.setenv("HERMES_MANAGED_SOCKET", str(tmp_path / "missing-managed.sock"))
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(
        ManagementService, "from_environment",
        classmethod(lambda cls: pytest.fail("local fallback must not run")),
    )
    with pytest.raises(FileNotFoundError, match="unavailable"):
        management_service_from_environment()


def test_host_compose_mounts_only_bounded_relay_and_requires_managed_socket():
    root = Path(__file__).resolve().parents[1]
    override = (root / "docker-compose.host-hermes.yml").read_text(encoding="utf-8")
    history_override = (root / "docker-compose.host-hermes-history.yml").read_text(
        encoding="utf-8"
    )
    entrypoint = (root / "scripts" / "docker_entrypoint.sh").read_text(encoding="utf-8")
    management_page = (root / "management_ui" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "HERMES_MANAGED_SOCKET: /run/host-hermes/managed.sock" in override
    assert ":/run/host-hermes:ro" in override
    assert ".hermes" not in override
    assert "HERMES_MANAGED_HISTORY_SOCKET: /run/host-hermes/history.sock" in history_override
    assert "HERMES_MANAGED_SOCKET:" not in history_override
    assert ".hermes" not in history_override
    assert "host managed backend socket is required in external mode" in entrypoint
    assert (
        'if [ -z "${HERMES_DASHBOARD_SOCKET:-}" ] '
        '&& [ -z "${HERMES_MANAGED_SOCKET:-}" ]; then'
    ) in entrypoint
    assert 'href="/api/manage/history"' in management_page


@pytest.mark.parametrize(
    ("mode", "selectors", "existing_task", "expected_copy", "expected_mkdirs"),
    [
        ("managed", {"HERMES_MANAGED_SOCKET": "/relay/managed.sock"}, False, 0, 0),
        ("dashboard", {"HERMES_DASHBOARD_SOCKET": "/relay/dashboard.sock"}, False, 0, 0),
        ("local", {}, False, 1, 3),
        ("history", {"HERMES_MANAGED_HISTORY_SOCKET": "/relay/history.sock"}, False, 1, 3),
        ("existing", {}, True, 0, 2),
    ],
)
def test_entrypoint_seeds_only_the_local_execution_backend(
    tmp_path, mode, selectors, existing_task, expected_copy, expected_mkdirs,
):
    shell = shutil.which("sh")
    if not shell and os.name == "nt":
        git_shell = Path(r"C:\Program Files\Git\usr\bin\sh.exe")
        shell = str(git_shell) if git_shell.is_file() else None
    if not shell:
        pytest.skip("requires a POSIX shell")
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    log = tmp_path / "operations.log"
    for command in ("cp", "mkdir"):
        shim = shim_dir / command
        shim.write_text(
            f'#!/bin/sh\nprintf "%s %s\\n" "{command}" "$*" >> "$ENTRYPOINT_LOG"\n',
            encoding="utf-8",
        )
        shim.chmod(0o755)
    task = tmp_path / "config" / "task.json"
    if existing_task:
        task.parent.mkdir()
        task.write_text("retained-task", encoding="utf-8")
    env = os.environ | selectors | {
        "CLIMATE_TASK_CONFIG": task.as_posix(),
        "CLIMATE_TASK_VERSION_DIR": (tmp_path / "versions").as_posix(),
        "CLIMATE_ACQUISITION_RUN_DIR": (tmp_path / "runs").as_posix(),
        "ENTRYPOINT_LOG": log.as_posix(),
        "HERMES_DASHBOARD_ENABLED": "0",
        "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
    }
    for name in (
        "HERMES_DASHBOARD_SOCKET", "HERMES_MANAGED_SOCKET",
        "HERMES_MANAGED_HISTORY_SOCKET",
    ):
        if name not in selectors:
            env.pop(name, None)
    result = subprocess.run(
        [shell, str(Path(__file__).resolve().parents[1] / "scripts" / "docker_entrypoint.sh"), "/usr/bin/true"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    operations = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert sum(line.startswith("cp ") for line in operations) == expected_copy, (mode, operations)
    assert sum(line.startswith("mkdir ") for line in operations) == expected_mkdirs, (mode, operations)
    if existing_task:
        assert task.read_text(encoding="utf-8") == "retained-task"


def test_host_dashboard_adapter_starts_managed_service_after_loading_host_environment(
    monkeypatch, tmp_path,
):
    import climate_monitor.hermes_dashboard_server as dashboard_server
    events = []
    service = _RecordingService()
    env_loader = SimpleNamespace(load_hermes_dotenv=lambda **kwargs: events.append(("dotenv", kwargs)))
    web_server = SimpleNamespace(
        _mcp_oauth_callback_url=lambda request, name: "unsafe",
        start_server=lambda **kwargs: events.append(("dashboard", kwargs)),
    )
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.env_loader = env_loader
    hermes_cli.web_server = web_server
    management_module = ModuleType("climate_monitor.management")
    management_module.ManagementService = SimpleNamespace(
        from_environment=lambda **kwargs: events.append(("management", kwargs)) or service,
    )

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "climate_monitor.management", management_module)
    monkeypatch.setattr(dashboard_server.importlib.metadata, "version", lambda _name: "0.20.5")
    monkeypatch.setattr(
        dashboard_server, "_start_relays",
        lambda selected, specs: events.append(("relays", selected, specs)) or [],
    )
    monkeypatch.setenv("CLIMATE_PUBLIC_ORIGIN", "https://climate.example")
    monkeypatch.setenv("HERMES_HOME", "/home/host-user/.hermes")
    monkeypatch.setenv("HERMES_MANAGED_SOCKET", str(tmp_path / "managed.sock"))
    monkeypatch.setenv(
        "HERMES_MANAGED_HISTORY_SOCKET", str(tmp_path / "history.sock"),
    )
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "relay-token")
    monkeypatch.setenv("HERMES_DASHBOARD_EXPECTED_VERSION", "0.20.5")
    monkeypatch.setenv("HERMES_DASHBOARD_PORT", "19119")

    dashboard_server.main()
    assert events[0] == ("dotenv", {"hermes_home": "/home/host-user/.hermes"})
    assert ("management", {"execution_backend": "host-dashboard"}) in events
    relay_event = next(event for event in events if event[0] == "relays")
    assert relay_event[1] is service
    assert relay_event[2] == [
        (str(tmp_path / "managed.sock"), False, "climate-managed-host"),
        (str(tmp_path / "history.sock"), True, "climate-managed-history"),
    ]
    assert events[-1][0] == "dashboard"


def test_history_only_adapter_uses_archive_constructor(monkeypatch, tmp_path):
    import climate_monitor.hermes_dashboard_server as dashboard_server

    events = []
    archive = object()
    web_server = SimpleNamespace(
        _mcp_oauth_callback_url=lambda request, name: "unsafe",
        start_server=lambda **kwargs: events.append(("dashboard", kwargs)),
    )
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.env_loader = SimpleNamespace(load_hermes_dotenv=lambda **kwargs: None)
    hermes_cli.web_server = web_server
    management_module = ModuleType("climate_monitor.management")
    management_module.ManagementService = SimpleNamespace(
        from_environment=lambda **kwargs: pytest.fail("history-only adapter used active constructor"),
        archive_from_environment=lambda **kwargs: events.append(("archive", kwargs)) or archive,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "climate_monitor.management", management_module)
    monkeypatch.setattr(dashboard_server.importlib.metadata, "version", lambda _name: "0.20.5")
    monkeypatch.setattr(
        dashboard_server, "_start_relays",
        lambda service, specs: events.append(("relays", service, specs)) or [],
    )
    monkeypatch.setenv("CLIMATE_PUBLIC_ORIGIN", "https://climate.example")
    monkeypatch.delenv("HERMES_MANAGED_SOCKET", raising=False)
    monkeypatch.setenv("HERMES_MANAGED_HISTORY_SOCKET", str(tmp_path / "history.sock"))

    dashboard_server.main()
    assert ("archive", {"execution_backend": "host-dashboard"}) in events
    relay = next(event for event in events if event[0] == "relays")
    assert relay[1] is archive
    assert relay[2] == [
        (str(tmp_path / "history.sock"), True, "climate-managed-history"),
    ]


@pytest.mark.skipif(os.name == "nt", reason="requires real Unix sockets")
def test_host_adapter_waits_for_real_managed_and_history_capabilities(
    monkeypatch, tmp_path,
):
    import climate_monitor.hermes_dashboard_server as dashboard_server
    import climate_monitor.managed_backend as managed_backend

    service = _materialized_service(tmp_path)
    events = []
    env_loader = SimpleNamespace(load_hermes_dotenv=lambda **kwargs: None)

    def start_dashboard(**_kwargs):
        active = managed_backend.HostManagementService(tmp_path / "managed.sock")
        history = managed_backend.HostManagementService(
            tmp_path / "history.sock", read_only=True,
        )
        events.append((active.capabilities["read_only"], history.capabilities["read_only"]))

    web_server = SimpleNamespace(
        _mcp_oauth_callback_url=lambda request, name: "unsafe",
        start_server=start_dashboard,
    )
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.env_loader = env_loader
    hermes_cli.web_server = web_server
    management_module = ModuleType("climate_monitor.management")
    management_module.ManagementService = SimpleNamespace(
        from_environment=lambda **kwargs: service,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "climate_monitor.management", management_module)
    monkeypatch.setattr(dashboard_server.importlib.metadata, "version", lambda _name: "0.20.5")
    monkeypatch.setattr(managed_backend, "host_capabilities", lambda: _capabilities(tmp_path))
    monkeypatch.setenv("CLIMATE_PUBLIC_ORIGIN", "https://climate.example")
    monkeypatch.setenv("HERMES_MANAGED_SOCKET", str(tmp_path / "managed.sock"))
    monkeypatch.setenv("HERMES_MANAGED_HISTORY_SOCKET", str(tmp_path / "history.sock"))
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "relay-token")
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN_FILE", raising=False)

    dashboard_server.main()
    assert events == [(False, True)]


@pytest.mark.skipif(os.name == "nt", reason="requires real Unix sockets")
@pytest.mark.parametrize("failed_socket", ["managed", "history"])
def test_host_adapter_bind_failure_stops_other_relay_and_never_starts_dashboard(
    monkeypatch, tmp_path, failed_socket,
):
    import climate_monitor.hermes_dashboard_server as dashboard_server
    import climate_monitor.managed_backend as managed_backend

    service = _materialized_service(tmp_path)
    events = []
    web_server = SimpleNamespace(
        _mcp_oauth_callback_url=lambda request, name: "unsafe",
        start_server=lambda **kwargs: events.append("dashboard"),
    )
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.env_loader = SimpleNamespace(load_hermes_dotenv=lambda **kwargs: None)
    hermes_cli.web_server = web_server
    management_module = ModuleType("climate_monitor.management")
    management_module.ManagementService = SimpleNamespace(
        from_environment=lambda **kwargs: service,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "climate_monitor.management", management_module)
    monkeypatch.setattr(dashboard_server.importlib.metadata, "version", lambda _name: "0.20.5")
    monkeypatch.setattr(managed_backend, "host_capabilities", lambda: _capabilities(tmp_path))
    monkeypatch.setenv("CLIMATE_PUBLIC_ORIGIN", "https://climate.example")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "relay-token")
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN_FILE", raising=False)
    long_socket = tmp_path / (("x" * 150) + ".sock")
    managed_socket = long_socket if failed_socket == "managed" else tmp_path / "managed.sock"
    history_socket = long_socket if failed_socket == "history" else ""
    monkeypatch.setenv("HERMES_MANAGED_SOCKET", str(managed_socket))
    if history_socket:
        monkeypatch.setenv("HERMES_MANAGED_HISTORY_SOCKET", str(history_socket))
    else:
        monkeypatch.delenv("HERMES_MANAGED_HISTORY_SOCKET", raising=False)

    with pytest.raises(RuntimeError, match="relay failed during startup"):
        dashboard_server.main()
    assert events == []
    if failed_socket == "history":
        with pytest.raises(FileNotFoundError, match="unavailable"):
            managed_backend.HostManagementService(tmp_path / "managed.sock")


def test_relay_readiness_timeout_is_bounded_and_stops_thread(monkeypatch, tmp_path):
    import climate_monitor.hermes_dashboard_server as dashboard_server
    import climate_monitor.managed_backend as managed_backend
    import uvicorn

    servers = []

    class Server:
        def __init__(self, _config):
            self.should_exit = False
            servers.append(self)

        def run(self):
            while not self.should_exit:
                time.sleep(0.001)

    monkeypatch.setattr(uvicorn, "Config", lambda **kwargs: kwargs)
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(managed_backend, "create_managed_backend_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        managed_backend, "HostManagementService",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            FileNotFoundError("host managed backend is unavailable")
        ),
    )
    monkeypatch.setattr(dashboard_server, "_RELAY_START_TIMEOUT", 0.05)
    monkeypatch.setattr(dashboard_server, "_RELAY_STOP_TIMEOUT", 0.2)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="readiness timed out"):
        dashboard_server._start_relays(
            _RecordingService(),
            [(str(tmp_path / "managed.sock"), False, "climate-managed-host")],
        )
    assert time.monotonic() - started < 1
    assert servers and servers[0].should_exit is True


@pytest.mark.parametrize("failed_index", [0, 1])
@pytest.mark.parametrize("raised", [False, True])
def test_ready_relay_exit_fails_adapter_and_stops_peer(
    monkeypatch, tmp_path, failed_index, raised,
):
    import climate_monitor.hermes_dashboard_server as dashboard_server

    dashboard_entered = threading.Event()
    events = []

    class RelayServer:
        def __init__(self, fails=False):
            self.should_exit = False
            self.fails = fails

        def run(self):
            if self.fails:
                assert dashboard_entered.wait(1)
                if raised:
                    raise RuntimeError("relay boom")
                return
            while not self.should_exit:
                time.sleep(0.001)

    relays = []
    for index, name in enumerate(("climate-managed-host", "climate-managed-history")):
        server = RelayServer(fails=index == failed_index)
        failures = []
        thread = threading.Thread(
            target=dashboard_server._run_relay, args=(server, failures), daemon=True,
        )
        thread.start()
        relays.append((server, thread, failures, name))

    def start_dashboard(**_kwargs):
        dashboard_entered.set()
        try:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                time.sleep(0.01)
        except KeyboardInterrupt:
            events.append("dashboard-interrupted")
            return
        raise AssertionError("relay death did not interrupt the Dashboard")

    web_server = SimpleNamespace(
        _mcp_oauth_callback_url=lambda request, name: "unsafe",
        start_server=start_dashboard,
    )
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.env_loader = SimpleNamespace(load_hermes_dotenv=lambda **kwargs: None)
    hermes_cli.web_server = web_server
    management_module = ModuleType("climate_monitor.management")
    management_module.ManagementService = SimpleNamespace(
        from_environment=lambda **kwargs: object(),
    )
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "climate_monitor.management", management_module)
    monkeypatch.setattr(dashboard_server.importlib.metadata, "version", lambda _name: "0.20.5")
    monkeypatch.setattr(dashboard_server, "_start_relays", lambda service, specs: relays)
    monkeypatch.setenv("CLIMATE_PUBLIC_ORIGIN", "https://climate.example")
    monkeypatch.setenv("HERMES_MANAGED_SOCKET", str(tmp_path / "managed.sock"))
    monkeypatch.setenv("HERMES_MANAGED_HISTORY_SOCKET", str(tmp_path / "history.sock"))

    failed_name = relays[failed_index][3]
    with pytest.raises(RuntimeError, match=failed_name):
        dashboard_server.main()
    assert events == ["dashboard-interrupted"]
    assert all(server.should_exit for server, *_rest in relays)
    assert all(not thread.is_alive() for _server, thread, _failures, _name in relays)


def test_ordinary_dashboard_exit_stops_relays_without_false_failure(monkeypatch, tmp_path):
    import climate_monitor.hermes_dashboard_server as dashboard_server

    server = SimpleNamespace(should_exit=False)

    def relay():
        while not server.should_exit:
            time.sleep(0.001)

    thread = threading.Thread(target=relay, daemon=True)
    thread.start()
    relays = [(server, thread, [], "climate-managed-host")]
    web_server = SimpleNamespace(
        _mcp_oauth_callback_url=lambda request, name: "unsafe",
        start_server=lambda **kwargs: None,
    )
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.env_loader = SimpleNamespace(load_hermes_dotenv=lambda **kwargs: None)
    hermes_cli.web_server = web_server
    management_module = ModuleType("climate_monitor.management")
    management_module.ManagementService = SimpleNamespace(
        from_environment=lambda **kwargs: object(),
    )
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "climate_monitor.management", management_module)
    monkeypatch.setattr(dashboard_server.importlib.metadata, "version", lambda _name: "0.20.5")
    monkeypatch.setattr(dashboard_server, "_start_relays", lambda service, specs: relays)
    monkeypatch.setenv("CLIMATE_PUBLIC_ORIGIN", "https://climate.example")
    monkeypatch.setenv("HERMES_MANAGED_SOCKET", str(tmp_path / "managed.sock"))
    monkeypatch.delenv("HERMES_MANAGED_HISTORY_SOCKET", raising=False)

    dashboard_server.main()
    assert server.should_exit is True
    assert not thread.is_alive()


def test_noncooperating_dashboard_is_bounded_in_disposable_subprocess(tmp_path):
    code = textwrap.dedent("""
        import os, sys, threading, time
        from pathlib import Path
        from types import ModuleType, SimpleNamespace
        import climate_monitor.hermes_dashboard_server as dashboard_server

        entered = threading.Event()
        server = SimpleNamespace(should_exit=False)
        failures = []
        def relay():
            entered.wait()
        thread = threading.Thread(target=relay, daemon=True)
        thread.start()
        relays = [(server, thread, failures, "climate-managed-host")]
        dashboard_server._start_relays = lambda service, specs: relays
        dashboard_server._DASHBOARD_STOP_TIMEOUT = 0.1
        dashboard_server.importlib.metadata.version = lambda name: "0.20.5"

        def start_server(**kwargs):
            entered.set()
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                while True:
                    time.sleep(1)

        hermes_cli = ModuleType("hermes_cli")
        hermes_cli.env_loader = SimpleNamespace(load_hermes_dotenv=lambda **kwargs: None)
        hermes_cli.web_server = SimpleNamespace(
            _mcp_oauth_callback_url=lambda request, name: "unsafe",
            start_server=start_server,
        )
        sys.modules["hermes_cli"] = hermes_cli
        management = ModuleType("climate_monitor.management")
        management.ManagementService = SimpleNamespace(from_environment=lambda **kwargs: object())
        sys.modules["climate_monitor.management"] = management
        os.environ.update(
            CLIMATE_PUBLIC_ORIGIN="https://climate.example",
            HERMES_MANAGED_SOCKET=str(Path.cwd() / "managed.sock"),
        )
        dashboard_server.main()
    """)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode != 0


@pytest.mark.skipif(os.name == "nt", reason="requires real Unix sockets")
def test_ready_real_uds_relay_exit_closes_peer_and_fails_process(tmp_path):
    managed_socket = tmp_path / "managed.sock"
    history_socket = tmp_path / "history.sock"
    code = textwrap.dedent("""
        import os, sys, threading, time
        from pathlib import Path
        from types import ModuleType, SimpleNamespace
        import climate_monitor.hermes_dashboard_server as dashboard_server
        import climate_monitor.managed_backend as managed_backend

        root = Path(sys.argv[1])
        managed_socket = Path(sys.argv[2])
        history_socket = Path(sys.argv[3])
        class Surface:
            def __getattr__(self, name):
                return lambda *args, **kwargs: {}
        store = Surface()
        store.active_path = root / "task.json"
        service = Surface()
        service.store = store
        service.runtime_root = root / "runs"
        service.archive_error = None
        capabilities = {
            "protocol": managed_backend.PROTOCOL_VERSION,
            "backend": "host-dashboard",
            "runtime_root": str(service.runtime_root),
            "active_task_path": str(store.active_path),
            "host": {},
        }
        create_app = managed_backend.create_managed_backend_app
        managed_backend.create_managed_backend_app = lambda target, read_only=False: create_app(
            target, read_only=read_only,
            capability_loader=lambda: capabilities,
            token_loader=lambda: "relay-token",
        )
        dashboard_server.importlib.metadata.version = lambda name: "0.20.5"
        original_start_relays = dashboard_server._start_relays
        dashboard_entered = threading.Event()

        def start_relays(target, specs):
            relays = original_start_relays(target, specs)
            def stop_managed():
                dashboard_entered.wait()
                relays[0][0].should_exit = True
            threading.Thread(target=stop_managed, daemon=True).start()
            return relays

        dashboard_server._start_relays = start_relays
        def start_server(**kwargs):
            dashboard_entered.set()
            try:
                while True:
                    time.sleep(0.1)
            except KeyboardInterrupt:
                return

        hermes_cli = ModuleType("hermes_cli")
        hermes_cli.env_loader = SimpleNamespace(load_hermes_dotenv=lambda **kwargs: None)
        hermes_cli.web_server = SimpleNamespace(
            _mcp_oauth_callback_url=lambda request, name: "unsafe",
            start_server=start_server,
        )
        sys.modules["hermes_cli"] = hermes_cli
        management = ModuleType("climate_monitor.management")
        management.ManagementService = SimpleNamespace(
            from_environment=lambda **kwargs: service,
        )
        sys.modules["climate_monitor.management"] = management
        os.environ.pop("HERMES_DASHBOARD_SESSION_TOKEN_FILE", None)
        os.environ.update(
            CLIMATE_PUBLIC_ORIGIN="https://climate.example",
            HERMES_DASHBOARD_SESSION_TOKEN="relay-token",
            HERMES_MANAGED_SOCKET=str(managed_socket),
            HERMES_MANAGED_HISTORY_SOCKET=str(history_socket),
        )
        dashboard_server.main()
    """)
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), str(managed_socket), str(history_socket)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "climate-managed-host relay exited after readiness" in result.stderr
    for path in (managed_socket, history_socket):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            with pytest.raises(OSError):
                client.connect(str(path))


def test_new_host_binding_freezes_backend_and_resume_does_not_rebind(tmp_path, monkeypatch):
    from climate_registry.persistent import initialize_registry

    definition = default_task_definition()
    definition["parameters"].update(
        report_date="2026-09-07", source_keys=["wmo"], timezone="UTC",
    )
    definition["runtime"].update(
        registry_database=str(tmp_path / "registry.sqlite3"),
        run_root=str(tmp_path / "runs"),
    )
    (tmp_path / "runs").mkdir()
    initialize_registry(tmp_path / "registry.sqlite3")
    store = TaskDefinitionStore(tmp_path / "task.json", tmp_path / "versions")
    store.save(definition, actor="test")
    launched = []
    service = ManagementService(
        store=store, runtime_root=tmp_path / "runs",
        execution_backend="host-dashboard", launcher=lambda binding: launched.append(binding) or 123,
    )
    result = service.start()
    binding = service.binding(result["run_id"])
    assert binding["execution_backend"] == "host-dashboard"
    result_path = tmp_path / "runs" / result["run_id"] / "attempt-1-result.json"
    result_path.write_text('{"exit_code": 75, "retryable": true}', encoding="utf-8")
    service.resume(result["run_id"])
    assert launched[-1]["execution_backend"] == "host-dashboard"


def test_version_read_preserves_stored_identity(tmp_path):
    from climate_registry.persistent import initialize_registry

    run_root = tmp_path / "runs"
    run_root.mkdir()
    database = tmp_path / "registry.sqlite3"
    initialize_registry(database)
    definition = default_task_definition()
    definition["parameters"].update(
        report_date="2026-09-07", source_keys=["wmo"], timezone="UTC",
    )
    definition["runtime"].update(
        registry_database=str(database), run_root=str(run_root),
    )
    store = TaskDefinitionStore(tmp_path / "task.json", tmp_path / "versions")
    saved = store.save(definition, actor="one")
    original_hash = saved["hashes"]["definition_sha256"]
    changed = saved["definition"]
    changed["parameters"]["report_date"] = "2026-09-14"
    store.save(changed, expected_version=1, actor="two")
    historical = store.version(1)
    assert historical["version"] == 1
    assert historical["hashes"]["definition_sha256"] == original_hash


def test_legacy_binding_without_backend_remains_readable(tmp_path):
    service = object.__new__(ManagementService)
    service.runtime_root = tmp_path / "runs"
    run_dir = service.runtime_root / "legacy-run"
    run_dir.mkdir(parents=True)
    (run_dir / "binding.json").write_text(
        '{"schema_version":"climate-acquisition-run-binding.v1","attempt":1}',
        encoding="utf-8",
    )
    assert "execution_backend" not in service.binding("legacy-run")


def test_backend_mismatch_allows_readback_but_rejects_resume(tmp_path):
    service = object.__new__(ManagementService)
    service.runtime_root = tmp_path / "runs"
    service.execution_backend = "host-dashboard"
    run_dir = service.runtime_root / "legacy-local"
    run_dir.mkdir(parents=True)
    (run_dir / "binding.json").write_text(
        '{"schema_version":"climate-acquisition-run-binding.v1","attempt":1}',
        encoding="utf-8",
    )
    assert service.binding("legacy-local")["attempt"] == 1
    with pytest.raises(RuntimeError, match="frozen to the local backend"):
        service.resume("legacy-local")
