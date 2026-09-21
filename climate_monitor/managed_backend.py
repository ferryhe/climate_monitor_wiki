"""Bounded host-side management relay for external Hermes Dashboard mode."""
from __future__ import annotations

import getpass
import importlib.metadata
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import FastAPI, Header, HTTPException

from climate_monitor.hermes_dashboard_server import SUPPORTED_HERMES_VERSIONS

PROTOCOL_VERSION = "climate-managed-host.v1"
TOKEN_HEADER = "X-Climate-Managed-Token"
_HERMES_VERSION_TITLE = re.compile(
    r"\AHermes Agent v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"
    r"(?: \([0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}\))?\Z"
)


def _token() -> str:
    token_file = os.getenv("HERMES_DASHBOARD_SESSION_TOKEN_FILE", "").strip()
    if token_file:
        try:
            value = Path(token_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("host managed backend token file is unavailable") from exc
    else:
        value = os.getenv("HERMES_DASHBOARD_SESSION_TOKEN", "").strip()
    if not value:
        raise RuntimeError("host managed backend token is unavailable")
    return value


def host_capabilities() -> dict[str, Any]:
    """Validate the exact host runtime before accepting managed operations."""
    try:
        version = importlib.metadata.version("hermes-agent")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("host managed backend has no Hermes installation") from exc
    if version not in SUPPORTED_HERMES_VERSIONS:
        raise RuntimeError(f"host managed backend Hermes {version} is unsupported")
    configured = os.getenv("HERMES_EXECUTABLE") or "hermes"
    executable = shutil.which(configured) if not Path(configured).is_absolute() else configured
    if not executable:
        raise RuntimeError("host managed backend Hermes executable is unavailable")
    executable = str(Path(executable).resolve())
    if not Path(executable).is_file():
        raise RuntimeError("host managed backend Hermes executable is unavailable")
    try:
        version_result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True,
            timeout=15, check=False, env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("host managed backend Hermes CLI is unusable") from exc
    if version_result.returncode:
        raise RuntimeError("host managed backend Hermes CLI version probe failed")
    cli_version = next((
        match.group("version")
        for line in (version_result.stdout + "\n" + version_result.stderr).splitlines()
        if (match := _HERMES_VERSION_TITLE.fullmatch(line)) is not None
    ), None)
    if cli_version is None:
        raise RuntimeError("host managed backend Hermes CLI version is unavailable")
    if cli_version not in SUPPORTED_HERMES_VERSIONS:
        raise RuntimeError(f"host managed backend Hermes CLI {cli_version} is unsupported")
    if cli_version != version:
        raise RuntimeError(
            f"host managed backend Hermes CLI {cli_version} differs from package {version}"
        )
    try:
        help_result = subprocess.run(
            [executable, "chat", "--help"], capture_output=True, text=True,
            timeout=15, check=False, env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("host managed backend Hermes CLI is unusable") from exc
    help_text = help_result.stdout + help_result.stderr
    required_flags = (
        "--ignore-rules", "--max-turns", "--query-file", "--quiet",
        "--reasoning", "--resume", "--run-budget", "--source", "--toolsets",
    )
    if help_result.returncode or not all(flag in help_text for flag in required_flags):
        raise RuntimeError("host managed backend Hermes chat contract is incompatible")
    home = Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).resolve()
    os.environ["HERMES_EXECUTABLE"] = executable
    return {
        "protocol": PROTOCOL_VERSION,
        "backend": "host-dashboard",
        "runtime_root": None,
        "active_task_path": None,
        "host": {
            "user": getpass.getuser(),
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "python": sys.executable,
            "hermes_home": str(home),
            "hermes_version": version,
            "hermes_executable": executable,
        },
    }


def _operations(service: Any) -> dict[str, Callable[..., Any]]:
    def start(*, trigger: str, now: str | None = None) -> Any:
        return service.start(
            trigger=trigger,
            now=datetime.fromisoformat(now) if now is not None else None,
        )

    return {
        "config_load": service.store.load,
        "config_versions": service.store.versions,
        "config_version": service.store.version,
        "config_diff": service.store.diff,
        "config_preview": service.store.preview,
        "config_save": service.store.save,
        "config_restore": service.store.restore,
        "runs_list": service.list_runs,
        "run_start": start,
        "run_resume": service.resume,
        "run_attach_or_resume": service.attach_or_resume,
        "run_binding": service.binding,
        "run_progress": service.progress,
        "run_result": service.run_result,
        "item_detail": service.item_detail,
        "meetings_start": service.start_meetings,
        "meetings_progress": service.meeting_progress,
        "meetings_events": service.meeting_events,
        "meeting_snapshot_freeze": service.freeze_meeting_snapshot,
        "meeting_snapshot_load": service.load_meeting_snapshot,
    }


_READ_OPERATIONS = {
    "config_load", "config_versions", "config_version", "config_diff",
    "runs_list", "run_binding", "run_progress", "run_result", "item_detail",
    "meetings_progress", "meetings_events", "meeting_snapshot_load",
}


def create_managed_backend_app(
    service: Any,
    *, capability_loader: Callable[[], dict[str, Any]] = host_capabilities,
    token_loader: Callable[[], str] = _token,
    read_only: bool = False,
) -> FastAPI:
    """Expose only the fixed management operations needed by this application."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    operations = _operations(service)
    capabilities = capability_loader()
    if capabilities.get("protocol") != PROTOCOL_VERSION:
        raise RuntimeError("host managed backend protocol is incompatible")

    @app.post("/v1/{operation}")
    def dispatch(
        operation: str,
        payload: dict[str, Any],
        x_climate_managed_token: str = Header(default=""),
    ) -> dict[str, Any]:
        if not secrets.compare_digest(x_climate_managed_token, token_loader()):
            raise HTTPException(status_code=401, detail="managed backend authentication failed")
        try:
            if operation == "capabilities":
                result = dict(capabilities)
                result["runtime_root"] = str(service.runtime_root)
                result["active_task_path"] = str(service.store.active_path)
                result["read_only"] = read_only
                archive_error = getattr(service, "archive_error", None)
                if read_only:
                    unavailable = isinstance(archive_error, (FileNotFoundError, ValueError))
                    result["archive_available"] = not unavailable
                    if unavailable:
                        result["archive_error"] = str(archive_error)
                return {"ok": True, "result": result}
            if read_only and operation not in _READ_OPERATIONS:
                raise RuntimeError(
                    f"history backend is read-only; operation {operation} is unavailable"
                )
            archive_error = getattr(service, "archive_error", None)
            if read_only and isinstance(archive_error, (FileNotFoundError, ValueError)):
                raise type(archive_error)(str(archive_error))
            callback = operations.get(operation)
            if callback is None:
                raise KeyError("unsupported managed backend operation")
            return {"ok": True, "result": callback(**payload)}
        except (FileNotFoundError, KeyError, RuntimeError, ValueError) as exc:
            return {
                "ok": False,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }

    return app


class HostTaskDefinitionStore:
    def __init__(self, client: "HostManagementService", active_path: str):
        self._client = client
        self.active_path = Path(active_path)

    def load(self, *, include_raw: bool = False) -> dict[str, Any]:
        return self._client._call("config_load", include_raw=include_raw)

    def versions(self) -> list[dict[str, Any]]:
        return self._client._call("config_versions")

    def version(self, version: int) -> dict[str, Any]:
        return self._client._call("config_version", version=version)

    def diff(self, old_version: int, new_version: int) -> dict[str, Any]:
        return self._client._call("config_diff", old_version=old_version, new_version=new_version)

    def preview(self, definition: dict[str, Any]) -> dict[str, Any]:
        return self._client._call("config_preview", definition=definition)

    def save(self, definition: dict[str, Any], *, expected_version: int | None = None, actor: str) -> dict[str, Any]:
        return self._client._call(
            "config_save", definition=definition,
            expected_version=expected_version, actor=actor,
        )

    def restore(self, version: int, *, expected_version: int, actor: str) -> dict[str, Any]:
        return self._client._call(
            "config_restore", version=version,
            expected_version=expected_version, actor=actor,
        )


class HostManagementService:
    """Synchronous facade whose every operation executes in the host adapter."""
    is_remote = True

    def __init__(
        self, socket_path: str | Path, *, read_only: bool = False,
        timeout: float = 30.0,
    ):
        path = Path(socket_path)
        if not path.is_absolute():
            raise RuntimeError("HERMES_MANAGED_SOCKET must be an absolute path")
        self.socket_path = path
        self.timeout = timeout
        capabilities = self._call("capabilities")
        if (
            capabilities.get("protocol") != PROTOCOL_VERSION
            or capabilities.get("backend") != "host-dashboard"
            or bool(capabilities.get("read_only")) != read_only
        ):
            raise RuntimeError("host managed backend capabilities are incompatible")
        self.capabilities = capabilities
        self.read_only = read_only
        self.runtime_root = Path(capabilities["runtime_root"])
        self.store = HostTaskDefinitionStore(self, capabilities["active_task_path"])

    def _call(self, operation: str, **payload: Any) -> Any:
        token = _token()
        try:
            with httpx.Client(
                transport=httpx.HTTPTransport(uds=str(self.socket_path)),
                base_url="http://managed-host", timeout=self.timeout, trust_env=False,
            ) as client:
                response = client.post(
                    f"/v1/{operation}", json=payload,
                    headers={TOKEN_HEADER: token},
                )
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            raise FileNotFoundError("host managed backend is unavailable") from exc
        if response.status_code == 401:
            raise RuntimeError("host managed backend authentication failed")
        try:
            response.raise_for_status()
            envelope = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FileNotFoundError("host managed backend is unavailable") from exc
        if envelope.get("ok") is True:
            return envelope.get("result")
        error = envelope.get("error") or {}
        message = str(error.get("message") or "host managed backend operation failed")
        exception_type = {
            "FileNotFoundError": FileNotFoundError,
            "KeyError": KeyError,
            "RuntimeError": RuntimeError,
            "ValueError": ValueError,
        }.get(error.get("type"), RuntimeError)
        raise exception_type(message)

    def list_runs(self): return self._call("runs_list")
    def start(self, *, trigger="manual", now=None):
        payload = {"trigger": trigger}
        if now is not None:
            payload["now"] = now.isoformat()
        return self._call("run_start", **payload)
    def resume(self, run_id): return self._call("run_resume", run_id=run_id)
    def attach_or_resume(self, run_id): return self._call("run_attach_or_resume", run_id=run_id)
    def binding(self, run_id): return self._call("run_binding", run_id=run_id)
    def progress(self, run_id): return self._call("run_progress", run_id=run_id)
    def run_result(self, run_id, attempt): return self._call("run_result", run_id=run_id, attempt=attempt)
    def item_detail(self, run_id, item_id): return self._call("item_detail", run_id=run_id, item_id=item_id)
    def start_meetings(self, run_id, *, retry_failed=False):
        return self._call("meetings_start", run_id=run_id, retry_failed=retry_failed)
    def meeting_progress(self, run_id): return self._call("meetings_progress", run_id=run_id)
    def meeting_events(self, **filters): return self._call("meetings_events", **filters)
    def freeze_meeting_snapshot(self, **filters): return self._call("meeting_snapshot_freeze", **filters)
    def load_meeting_snapshot(self, snapshot_id):
        return self._call("meeting_snapshot_load", snapshot_id=snapshot_id)


def management_service_from_environment() -> Any:
    """Select explicit host execution, or the unchanged local backend."""
    dashboard_socket = os.getenv("HERMES_DASHBOARD_SOCKET", "").strip()
    managed_socket = os.getenv("HERMES_MANAGED_SOCKET", "").strip()
    if managed_socket:
        return HostManagementService(managed_socket)
    if dashboard_socket:
        raise RuntimeError("external Hermes Dashboard requires the host managed backend")
    from climate_monitor.management import ManagementService

    return ManagementService.from_environment()


def active_backend_from_environment() -> str:
    managed_socket = os.getenv("HERMES_MANAGED_SOCKET", "").strip()
    if managed_socket:
        return "host-dashboard"
    if os.getenv("HERMES_DASHBOARD_SOCKET", "").strip():
        raise RuntimeError("external Hermes Dashboard requires the host managed backend")
    return "local"


def history_service_from_environment(backend: str, *, active_service: Any | None = None) -> Any:
    """Return one explicit read source without changing the execution backend."""
    if backend not in {"local", "host-dashboard"}:
        raise ValueError("history backend must be local or host-dashboard")
    active_backend = active_backend_from_environment()
    if backend == active_backend:
        return active_service if active_service is not None else management_service_from_environment()
    if backend == "local":
        from climate_monitor.management import ManagementService

        service = ManagementService.archive_from_environment()
        if service.archive_error is not None:
            raise type(service.archive_error)(str(service.archive_error))
        return service
    history_socket = os.getenv("HERMES_MANAGED_HISTORY_SOCKET", "").strip()
    if not history_socket:
        raise FileNotFoundError("host-dashboard archive is not configured")
    service = HostManagementService(history_socket, read_only=True)
    if service.capabilities.get("archive_available") is False:
        raise FileNotFoundError(
            str(service.capabilities.get("archive_error") or "host-dashboard archive is unavailable")
        )
    return service
