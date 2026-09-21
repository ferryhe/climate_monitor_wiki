"""Trusted outer-auth adapter for the supported Hermes Dashboards.

Hermes 0.20.0 and 0.20.5 derive MCP OAuth callbacks from ``request.base_url``
unless their own ``dashboard.public_url`` is set. Setting that option also
enables Hermes' upstream login gate. This deployment already has an outer
FastAPI Users gate, so this narrow adapter supplies only the callback URL from
operator-owned configuration and leaves the Dashboard in session-token mode.
"""
from __future__ import annotations

import _thread
import importlib.metadata
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

PINNED_HERMES_VERSION = "0.20.5"
HOST_HERMES_VERSION = "0.20.0"
SUPPORTED_HERMES_VERSIONS = {HOST_HERMES_VERSION, PINNED_HERMES_VERSION}
_OAUTH_SERVER_NAME = re.compile(r"[A-Za-z0-9._~-]{1,128}\Z")
_RELAY_START_TIMEOUT = 5.0
_RELAY_STOP_TIMEOUT = 2.0
_RELAY_WATCH_INTERVAL = 0.02
_DASHBOARD_STOP_TIMEOUT = 5.0


def validate_oauth_server_name(server_name: str) -> str:
    """Require an MCP server identifier that is exactly one safe URL segment."""
    if server_name in {".", ".."} or _OAUTH_SERVER_NAME.fullmatch(server_name) is None:
        raise ValueError("OAuth server name must be one safe URL segment")
    return server_name


def trusted_public_origin(raw: str | None = None) -> str:
    value = (raw if raw is not None else os.getenv("CLIMATE_PUBLIC_ORIGIN", "")).strip()
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Force validation of a configured authority port.
    except ValueError as exc:
        raise ValueError("CLIMATE_PUBLIC_ORIGIN must be a valid HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or any(character.isspace() for character in parsed.netloc)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "CLIMATE_PUBLIC_ORIGIN must be an HTTPS origin without credentials, path, query, or fragment"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def oauth_callback_url(server_name: str, *, origin: str | None = None) -> str:
    base = trusted_public_origin(origin)
    validate_oauth_server_name(server_name)
    return f"{base}/hermes/api/mcp/oauth/callback/{quote(server_name, safe='')}"


def install_oauth_callback_adapter(
    web_server: Any,
    *,
    version: str | None = None,
    expected_version: str | None = None,
) -> None:
    actual = version or importlib.metadata.version("hermes-agent")
    expected = expected_version or os.getenv(
        "HERMES_DASHBOARD_EXPECTED_VERSION", PINNED_HERMES_VERSION
    )
    if expected not in SUPPORTED_HERMES_VERSIONS:
        raise RuntimeError(
            "unsupported HERMES_DASHBOARD_EXPECTED_VERSION; use "
            + " or ".join(sorted(SUPPORTED_HERMES_VERSIONS))
        )
    if actual != expected:
        raise RuntimeError(
            f"Hermes callback adapter expected {expected}, found {actual}"
        )
    # Both supported routers late-bind this web_server symbol at request time.
    if not callable(getattr(web_server, "_mcp_oauth_callback_url", None)):
        raise RuntimeError("supported Hermes OAuth callback seam is unavailable")

    def configured_callback(request: Any, server_name: str) -> str:
        del request  # Never trust Host, Forwarded, or X-Forwarded-* for redirects.
        return oauth_callback_url(server_name)

    web_server._mcp_oauth_callback_url = configured_callback


def _run_relay(server: Any, failures: list[BaseException]) -> None:
    try:
        server.run()
    except BaseException as exc:
        failures.append(exc)


def _stop_relays(relays: list[tuple[Any, threading.Thread, list[BaseException], str]]) -> None:
    for server, _thread, _failures, _name in relays:
        server.should_exit = True
    deadline = time.monotonic() + _RELAY_STOP_TIMEOUT
    for _server, thread, _failures, _name in relays:
        thread.join(max(0.0, deadline - time.monotonic()))


def _start_relays(
    service: Any, specs: list[tuple[str, bool, str]],
) -> list[tuple[Any, threading.Thread, list[BaseException], str]]:
    from climate_monitor.managed_backend import (
        HostManagementService,
        create_managed_backend_app,
    )
    import uvicorn

    relays: list[tuple[Any, threading.Thread, list[BaseException], str]] = []
    try:
        for socket_value, read_only, name in specs:
            socket_path = Path(socket_value)
            if not socket_path.is_absolute():
                raise ValueError(f"{name} socket must be an absolute path")
            if not socket_path.parent.is_dir():
                raise RuntimeError(f"{name} relay directory is unavailable")
            try:
                if socket_path.exists() or socket_path.is_symlink():
                    socket_path.unlink()
            except OSError:
                raise RuntimeError(f"{name} relay socket is unavailable") from None
            app = create_managed_backend_app(service, read_only=read_only)
            server = uvicorn.Server(uvicorn.Config(
                app=app, uds=str(socket_path), log_level="warning", access_log=False,
            ))
            failures: list[BaseException] = []
            thread = threading.Thread(
                target=_run_relay, args=(server, failures), name=name, daemon=True,
            )
            relay = (server, thread, failures, name)
            relays.append(relay)
            thread.start()
            deadline = time.monotonic() + _RELAY_START_TIMEOUT
            while True:
                if failures or not thread.is_alive():
                    raise RuntimeError(f"{name} relay failed during startup")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"{name} relay readiness timed out")
                try:
                    HostManagementService(
                        socket_path, read_only=read_only,
                        timeout=min(0.2, remaining),
                    )
                    break
                except FileNotFoundError:
                    time.sleep(min(0.02, remaining))
                except RuntimeError:
                    raise RuntimeError(
                        f"{name} relay capability check failed"
                    ) from None
        return relays
    except BaseException:
        _stop_relays(relays)
        raise


def _watch_relays(
    relays: list[tuple[Any, threading.Thread, list[BaseException], str]],
    stop: threading.Event,
    dashboard_done: threading.Event,
    relay_failure: list[str],
) -> None:
    while not stop.wait(_RELAY_WATCH_INTERVAL):
        for _server, thread, failures, name in relays:
            if failures or not thread.is_alive():
                outcome = "failed" if failures else "exited"
                relay_failure.append(f"{name} relay {outcome} after readiness")
                _stop_relays(relays)
                _thread.interrupt_main()
                if not dashboard_done.wait(_DASHBOARD_STOP_TIMEOUT):
                    os._exit(1)
                return


def main() -> None:
    from hermes_cli import env_loader

    env_loader.load_hermes_dotenv(hermes_home=os.getenv("HERMES_HOME") or None)
    trusted_public_origin()  # Validate the effective post-bootstrap configuration.
    from hermes_cli import web_server

    install_oauth_callback_adapter(web_server)
    managed_socket = os.getenv("HERMES_MANAGED_SOCKET", "").strip()
    history_socket = os.getenv("HERMES_MANAGED_HISTORY_SOCKET", "").strip()
    try:
        port = int(os.getenv("HERMES_DASHBOARD_PORT", "9119"))
    except ValueError as exc:
        raise ValueError("HERMES_DASHBOARD_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("HERMES_DASHBOARD_PORT must be between 1 and 65535")
    relays: list[tuple[Any, threading.Thread, list[BaseException], str]] = []
    if managed_socket or history_socket:
        if managed_socket and managed_socket == history_socket:
            raise ValueError("managed execution and history sockets must be different")
        from climate_monitor.management import ManagementService

        service = (
            ManagementService.from_environment(execution_backend="host-dashboard")
            if managed_socket else
            ManagementService.archive_from_environment(execution_backend="host-dashboard")
        )
        specs = [spec for spec in (
            (managed_socket, False, "climate-managed-host"),
            (history_socket, True, "climate-managed-history"),
        ) if spec[0]]
        relays = _start_relays(service, specs)
    watcher_stop = threading.Event()
    dashboard_done = threading.Event()
    relay_failure: list[str] = []
    watcher = None
    if relays:
        watcher = threading.Thread(
            target=_watch_relays,
            args=(relays, watcher_stop, dashboard_done, relay_failure),
            name="climate-relay-watch",
            daemon=True,
        )
        watcher.start()
    try:
        try:
            web_server.start_server(host="127.0.0.1", port=port, open_browser=False)
        except KeyboardInterrupt:
            if not relay_failure:
                raise
    finally:
        dashboard_done.set()
        watcher_stop.set()
        if watcher is not None:
            watcher.join(_RELAY_STOP_TIMEOUT)
        _stop_relays(relays)
    if relay_failure:
        raise RuntimeError(relay_failure[0])


if __name__ == "__main__":
    main()
