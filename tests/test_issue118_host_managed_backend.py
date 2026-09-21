from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from climate_monitor.managed_backend import (
    PROTOCOL_VERSION,
    TOKEN_HEADER,
    create_managed_backend_app,
    management_service_from_environment,
)
from climate_monitor.management import ManagementService, TaskDefinitionStore, default_task_definition


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


def test_host_backend_routes_config_crud_run_readback_resume_and_meetings_to_one_service(tmp_path):
    service = _RecordingService()
    app = create_managed_backend_app(
        service, capability_loader=lambda: _capabilities(tmp_path),
        token_loader=lambda: "relay-token",
    )
    operations = [
        ("config_load", {}),
        ("config_versions", {}),
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


def test_external_mode_fails_closed_without_backend_and_local_mode_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_DASHBOARD_SOCKET", str(tmp_path / "dashboard.sock"))
    monkeypatch.delenv("HERMES_MANAGED_SOCKET", raising=False)
    with pytest.raises(RuntimeError, match="requires the host managed backend"):
        management_service_from_environment()

    monkeypatch.delenv("HERMES_DASHBOARD_SOCKET")
    monkeypatch.setattr(
        ManagementService, "from_environment", classmethod(lambda cls: "local-service")
    )
    assert management_service_from_environment() == "local-service"


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
    entrypoint = (root / "scripts" / "docker_entrypoint.sh").read_text(encoding="utf-8")
    assert "HERMES_MANAGED_SOCKET: /run/host-hermes/managed.sock" in override
    assert ":/run/host-hermes:ro" in override
    assert ".hermes" not in override
    assert "host managed backend socket is required in external mode" in entrypoint
    assert 'if [ -z "${HERMES_DASHBOARD_SOCKET:-}" ]; then' in entrypoint


def test_host_dashboard_adapter_starts_managed_service_after_loading_host_environment(
    monkeypatch, tmp_path,
):
    import climate_monitor.hermes_dashboard_server as dashboard_server
    import climate_monitor.managed_backend as managed_backend
    import uvicorn

    events = []
    started = threading.Event()
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
        managed_backend, "create_managed_backend_app",
        lambda selected: events.append(("app", selected)) or object(),
    )
    monkeypatch.setattr(
        uvicorn, "run",
        lambda **kwargs: (events.append(("managed", kwargs)), started.set()),
    )
    monkeypatch.setenv("CLIMATE_PUBLIC_ORIGIN", "https://climate.example")
    monkeypatch.setenv("HERMES_HOME", "/home/host-user/.hermes")
    monkeypatch.setenv("HERMES_MANAGED_SOCKET", str(tmp_path / "managed.sock"))
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "relay-token")
    monkeypatch.setenv("HERMES_DASHBOARD_EXPECTED_VERSION", "0.20.5")
    monkeypatch.setenv("HERMES_DASHBOARD_PORT", "19119")

    dashboard_server.main()
    assert started.wait(2)
    assert events[0] == ("dotenv", {"hermes_home": "/home/host-user/.hermes"})
    assert ("management", {"execution_backend": "host-dashboard"}) in events
    managed = next(value for name, value in events if name == "managed")
    assert managed["uds"] == str(tmp_path / "managed.sock")
    assert next(value for name, value in events if name == "app") is service


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
