#!/usr/bin/env python3
"""Exercise Hermes 0.20.5 OAuth start + callback through FastAPI.

The real pinned Hermes route and a real loopback HTTP socket are used. Provider
network I/O is replaced immediately before discovery; no credential or config is
written and no OAuth provider is contacted.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from importlib import import_module
from pathlib import Path

import httpx
import uvicorn
from fastapi.testclient import TestClient
from fastapi_users.password import PasswordHelper

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from climate_monitor.hermes_dashboard_server import install_oauth_callback_adapter  # noqa: E402


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def main() -> None:
    port = _free_loopback_port()
    os.environ.update(
        {
            "CLIMATE_PUBLIC_ORIGIN": "https://climate.example",
            "CLIMATE_CONSOLE_USERNAME": "operator",
            "CLIMATE_CONSOLE_PASSWORD_HASH": PasswordHelper().hash("validation-password"),
            "CLIMATE_CONSOLE_SESSION_SECRET": "validation-session-secret-with-at-least-32-bytes",
            "CLIMATE_CONSOLE_SESSION_DB": str(
                Path(tempfile.mkdtemp()) / "console-sessions.sqlite3"
            ),
            "CLIMATE_CONSOLE_SECURE_COOKIE": "true",
            "HERMES_DASHBOARD_URL": f"http://127.0.0.1:{port}",
            "HERMES_DASHBOARD_SESSION_TOKEN": (
                "issue114-validation-internal-token-with-at-least-32-bytes"
            ),
        }
    )

    mcp_config = import_module("hermes_cli.mcp_config")
    web_server = import_module("hermes_cli.web_server")
    install_oauth_callback_adapter(web_server)
    web_server.app.state.auth_required = False
    web_server._mcp_oauth_flows.clear()
    captured: list[str] = []
    upstream_paths: list[str] = []

    @web_server.app.middleware("http")
    async def record_upstream_path(request, call_next):
        upstream_paths.append(request.url.path)
        return await call_next(request)

    original_get_servers = getattr(mcp_config, "_get_mcp_servers")
    original_worker = getattr(web_server, "_run_dashboard_mcp_oauth")

    def fake_servers() -> dict[str, dict[str, str]]:
        return {"calendar": {"url": "https://mcp.invalid", "auth": "oauth"}}

    def stop_before_network(flow, cfg) -> None:
        captured.append(flow.redirect_uri)
        flow.mark_error("validation stopped before provider network call")

    setattr(mcp_config, "_get_mcp_servers", fake_servers)
    setattr(web_server, "_run_dashboard_mcp_oauth", stop_before_network)
    server = uvicorn.Server(
        uvicorn.Config(
            web_server.app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            lifespan="off",
        )
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    outer_server = None
    outer_thread = None
    anonymous = None
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("pinned Hermes test server did not start")

        api_server = import_module("api_server")
        outer_port = _free_loopback_port()
        outer_server = uvicorn.Server(
            uvicorn.Config(
                api_server.app,
                host="127.0.0.1",
                port=outer_port,
                log_level="error",
                lifespan="off",
            )
        )
        outer_thread = threading.Thread(target=outer_server.run, daemon=True)
        outer_thread.start()
        for _ in range(100):
            if outer_server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("outer FastAPI test server did not start")

        client = TestClient(api_server.app, base_url="https://climate.example")
        login = client.post(
            "/api/manage/auth/login",
            data={"username": "operator", "password": "validation-password"},
        )
        if login.status_code != 204:
            raise RuntimeError(f"shared login returned {login.status_code}: {login.text}")

        response = client.post(
            "/hermes/api/mcp/servers/calendar/auth",
            headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        )
        if response.status_code != 200:
            raise RuntimeError(f"proxied OAuth start returned {response.status_code}: {response.text}")
        expected = "https://climate.example/hermes/api/mcp/oauth/callback/calendar"
        if captured != [expected]:
            raise RuntimeError(f"unexpected redirect URI: {captured!r}")

        # Use a real outer HTTP socket for callback security checks so URL
        # parsing matches the final container rather than TestClient alone.
        anonymous = httpx.Client(
            base_url=f"http://127.0.0.1:{outer_port}",
            follow_redirects=False,
            timeout=10,
        )
        callback = anonymous.get(
            "/hermes/api/mcp/oauth/callback/calendar",
            params={"state": "deliberately-invalid"},
            headers={"X-Forwarded-Host": "attacker.example"},
        )
        if callback.status_code != 404 or "OAuth flow expired" not in callback.text:
            raise RuntimeError("cookie-free invalid-state callback did not reach Hermes and fail closed")

        paths_before_attacks = list(upstream_paths)
        traversal_paths = [
            "%2e%2e/%2e%2e/%2e%2e/%2e%2e/api/config",
            "%252e%252e/%252e%252e/%252e%252e/%252e%252e/api/config/raw",
            "..%5c..%5c..%5c..%5capi%5csessions",
            "calendar%2f..%2f..%2f..%2f..%2fapi%2fsessions/session-1/messages",
            "calendar%252f..%252f..%252f..%252f..%252fapi%252fsessions/session-1/export",
        ]
        for attack in traversal_paths:
            rejected = anonymous.get(
                f"/hermes/api/mcp/oauth/callback/{attack}",
                params={"code": "hostile", "state": "hostile"},
            )
            if rejected.status_code not in {401, 404}:
                raise RuntimeError(
                    f"anonymous callback traversal was not rejected: {attack!r} "
                    f"returned {rejected.status_code}"
                )
        if upstream_paths != paths_before_attacks:
            raise RuntimeError(
                "anonymous callback traversal reached the loopback Hermes server: "
                f"{upstream_paths[len(paths_before_attacks):]!r}"
            )

        # Exercise the exact public resource with a real pinned flow object:
        # wrong state was rejected above; matching state and code are delivered.
        from tools.mcp_dashboard_oauth import DashboardOAuthFlow

        valid_flow = DashboardOAuthFlow(
            flow_id="issue114-valid-callback",
            server_name="issue114-state-check",
            profile=None,
            hermes_home=os.environ.get("HERMES_HOME", "/tmp/hermes"),
            redirect_uri=(
                "https://climate.example/hermes/api/mcp/oauth/callback/"
                "issue114-state-check"
            ),
        )
        asyncio.run(
            valid_flow.publish_authorization_url(
                "https://provider.invalid/authorize?state=issue114-expected-state"
            )
        )
        web_server._mcp_oauth_flows[valid_flow.flow_id] = valid_flow
        accepted = anonymous.get(
            "/hermes/api/mcp/oauth/callback/issue114-state-check",
            params={"code": "safe-code", "state": "issue114-expected-state"},
        )
        if accepted.status_code != 200 or "Authorization received" not in accepted.text:
            raise RuntimeError("exact state-protected callback was not accepted by Hermes")
        if asyncio.run(valid_flow.wait_for_callback(timeout=1)) != (
            "safe-code",
            "issue114-expected-state",
        ):
            raise RuntimeError("exact callback values were not delivered to the pinned flow")
    finally:
        if anonymous is not None:
            anonymous.close()
        if outer_server is not None:
            outer_server.should_exit = True
        if outer_thread is not None:
            outer_thread.join(timeout=10)
        server.should_exit = True
        server_thread.join(timeout=10)
        setattr(mcp_config, "_get_mcp_servers", original_get_servers)
        setattr(web_server, "_run_dashboard_mcp_oauth", original_worker)
        web_server._mcp_oauth_flows.clear()

    print(
        json.dumps(
            {
                "hermes_version": "0.20.5",
                "real_oauth_start_through_fastapi": "passed",
                "redirect_uri": captured[0],
                "provider_network_called": False,
                "cookie_free_invalid_state_callback_through_fastapi": "rejected",
                "anonymous_callback_traversal_upstream_requests": 0,
                "anonymous_callback_traversal_variants_rejected": len(traversal_paths),
                "exact_state_protected_callback": "accepted",
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
