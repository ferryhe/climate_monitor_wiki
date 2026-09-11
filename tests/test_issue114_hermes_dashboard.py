from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi_users.password import PasswordHelper
from starlette.websockets import WebSocketDisconnect


ROOT = Path(__file__).resolve().parents[1]


def _configure_auth(monkeypatch, api_server, *, seconds: int = 1800) -> None:
    monkeypatch.setenv("CLIMATE_CONSOLE_USERNAME", "operator")
    monkeypatch.setenv(
        "CLIMATE_CONSOLE_PASSWORD_HASH", PasswordHelper().hash("correct horse")
    )
    monkeypatch.setenv("CLIMATE_CONSOLE_SESSION_SECRET", "test-secret-with-at-least-32-bytes")
    monkeypatch.setenv("CLIMATE_CONSOLE_SESSION_SECONDS", str(seconds))
    monkeypatch.setenv(
        "HERMES_DASHBOARD_SESSION_TOKEN",
        "default-internal-test-token-with-at-least-32-bytes",
    )
    monkeypatch.setenv(
        "CLIMATE_CONSOLE_SESSION_DB",
        str(Path(tempfile.mkdtemp()) / "console-sessions.sqlite3"),
    )
    api_server._LOGIN_LIMITER.reset()


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/manage/auth/login",
        data={"username": "operator", "password": "correct horse"},
    )
    assert response.status_code == 204
    token = client.cookies.get("climate_console_session")
    assert token is not None
    return token


def _mock_http_upstream(monkeypatch):
    import climate_monitor.hermes_dashboard as dashboard

    real_client = httpx.AsyncClient
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/sessions"})
        return httpx.Response(
            200,
            content=b"official dashboard",
            headers={"content-type": "text/html", "x-upstream": "hermes"},
        )

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(dashboard.httpx, "AsyncClient", client_factory)
    return requests


def test_shared_login_gates_page_http_assets_and_navigation(monkeypatch):
    import api_server

    _configure_auth(monkeypatch, api_server)
    requests = _mock_http_upstream(monkeypatch)
    client = TestClient(api_server.app, base_url="https://testserver")

    assert client.get("/").status_code == 200
    assert client.get("/api/config").status_code == 200
    assert client.get("/api/manage/session").json() == {"authenticated": False}
    denied_page = client.get("/hermes", follow_redirects=False)
    assert denied_page.status_code == 303
    assert denied_page.headers["location"] == "/manage/login?next=/hermes"
    assert client.get("/hermes/", follow_redirects=False).status_code == 303
    assert client.get("/hermes/api/sessions").status_code == 401

    _login(client)
    assert client.get("/api/manage/session").json() == {"authenticated": True}
    manage_page = client.get("/manage")
    assert manage_page.status_code == 200
    assert '<a href="/hermes">Hermes</a>' in manage_page.text
    assert client.get("/hermes").text == "official dashboard"
    assert client.get("/hermes/assets/app.js?build=1").status_code == 200
    redirect = client.get("/hermes/redirect", follow_redirects=False)
    assert redirect.headers["location"] == "/hermes/sessions"
    assert requests[-2].headers["x-forwarded-prefix"] == "/hermes"
    assert requests[-2].url.path == "/assets/app.js"
    assert requests[-2].url.query == b"build=1"
    assert "climate_console_session" not in requests[-2].headers.get("cookie", "")

    index = (ROOT / "showcase" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "showcase" / "app.js").read_text(encoding="utf-8")
    assert re.search(r'id="hermesLink"[^>]+href="/hermes"[^>]+hidden', index)
    assert 'fetch("/api/manage/session"' in script
    assert "authenticated && els.hermesLink" in script


def test_authenticated_http_proxy_owns_upstream_session_auth(monkeypatch):
    import api_server
    import climate_monitor.hermes_dashboard as dashboard

    _configure_auth(monkeypatch, api_server)
    internal_token = "internal-test-token-with-at-least-32-bytes"
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", internal_token)
    real_client = httpx.AsyncClient
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("x-hermes-session-token") != internal_token:
            return httpx.Response(401, json={"detail": "Unauthorized"})
        if request.url.path == "/":
            return httpx.Response(
                200,
                text=(
                    '<script>window.__HERMES_SESSION_TOKEN__="'
                    + internal_token
                    + '";</script>'
                ),
                headers={"X-Upstream-Secret": internal_token},
            )
        return httpx.Response(200, json={"configured": True})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        dashboard.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    client = TestClient(api_server.app, base_url="https://testserver")

    # Even knowing the inner credential doesn't bypass the outer login gate.
    assert client.get(
        "/hermes/api/config",
        headers={"X-Hermes-Session-Token": internal_token},
    ).status_code == 401
    assert requests == []

    _login(client)
    response = client.get(
        "/hermes/api/config",
        headers={
            "Authorization": "Bearer hostile-client-token",
            "Forwarded": "host=attacker.example",
            "Host": "testserver",
            "X-Forwarded-Host": "attacker.example",
            "X-Hermes-Session-Token": "hostile-client-token",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"configured": True}
    assert len(requests) == 1
    upstream_headers = requests[0].headers
    assert upstream_headers["x-hermes-session-token"] == internal_token
    assert "authorization" not in upstream_headers
    assert "forwarded" not in upstream_headers
    assert "x-forwarded-host" not in upstream_headers
    assert upstream_headers["x-forwarded-prefix"] == "/hermes"
    assert upstream_headers["x-forwarded-proto"] == "https"
    assert upstream_headers["accept-encoding"] == "identity"

    page = client.get("/hermes")
    assert page.status_code == 200
    assert internal_token not in page.text
    assert "outer-proxy-authenticated" in page.text
    assert "x-upstream-secret" not in page.headers


def test_loopback_http_proxy_ignores_environment_proxies(monkeypatch):
    import api_server
    import climate_monitor.hermes_dashboard as dashboard

    _configure_auth(monkeypatch, api_server)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    real_client = httpx.AsyncClient
    client_options: list[dict[str, object]] = []
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))

    def client_factory(**kwargs):
        client_options.append(kwargs.copy())
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(dashboard.httpx, "AsyncClient", client_factory)
    client = TestClient(api_server.app, base_url="https://testserver")
    _login(client)

    assert client.get("/hermes/api/config").json() == {"ok": True}
    assert client_options == [
        {"timeout": 30.0, "follow_redirects": False, "trust_env": False}
    ]


def test_logout_and_immediate_relogin_never_restore_replayed_session(monkeypatch):
    import api_server

    _configure_auth(monkeypatch, api_server)
    _mock_http_upstream(monkeypatch)
    client = TestClient(api_server.app, base_url="https://testserver")
    token = _login(client)
    assert client.get("/hermes/api/sessions").status_code == 200
    assert client.post("/api/manage/auth/logout").status_code == 204

    response = client.post(
        "/api/manage/auth/login",
        data={"username": "operator", "password": "correct horse"},
    )
    assert response.status_code == 204
    assert "climate_console_session=" in response.headers["set-cookie"]
    new_token = client.cookies.get("climate_console_session")
    assert new_token is not None
    assert new_token != token

    replay = TestClient(api_server.app, base_url="https://testserver")
    replay.cookies.set("climate_console_session", token)
    assert replay.get("/manage", follow_redirects=False).status_code == 303
    assert replay.get("/api/manage/config").status_code == 401
    assert replay.get("/hermes/api/sessions").status_code == 401

    assert client.get("/manage").status_code == 200
    assert client.get("/api/manage/config").status_code == 200
    assert client.get("/hermes/api/sessions").status_code == 200


def test_random_sessions_are_shared_across_strategy_instances_and_restart(monkeypatch):
    from climate_monitor.console_auth import (
        ConsoleUserManager,
        _USER_DATABASE,
        _configured_user,
        get_jwt_strategy,
    )

    monkeypatch.setenv("CLIMATE_CONSOLE_USERNAME", "operator")
    monkeypatch.setenv(
        "CLIMATE_CONSOLE_PASSWORD_HASH", PasswordHelper().hash("correct horse")
    )
    monkeypatch.setenv("CLIMATE_CONSOLE_SESSION_SECRET", "test-secret-with-at-least-32-bytes")
    monkeypatch.setenv(
        "CLIMATE_CONSOLE_SESSION_DB",
        str(Path(tempfile.mkdtemp()) / "console-sessions.sqlite3"),
    )
    user = _configured_user()
    assert user is not None
    manager = ConsoleUserManager(_USER_DATABASE)

    async def issue() -> tuple[str, str]:
        first_worker = get_jwt_strategy()
        first_token = await first_worker.write_token(user)
        second_token = await first_worker.write_token(user)
        assert first_token != second_token

        restarted_worker = get_jwt_strategy()
        assert await restarted_worker.read_token(first_token, manager) == user
        return first_token, second_token

    first_token, second_token = asyncio.run(issue())
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(ROOT)
    child_env["ISSUE114_TOKEN"] = first_token
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """import asyncio, os
from climate_monitor.console_auth import ConsoleUserManager, _USER_DATABASE, get_jwt_strategy
async def check():
    strategy = get_jwt_strategy()
    manager = ConsoleUserManager(_USER_DATABASE)
    token = os.environ['ISSUE114_TOKEN']
    user = await strategy.read_token(token, manager)
    assert user is not None
    await strategy.destroy_token(token, user)
asyncio.run(check())
""",
        ],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr

    async def verify_other_worker() -> None:
        other_worker = get_jwt_strategy()
        assert await other_worker.read_token(first_token, manager) is None
        assert await other_worker.read_token(second_token, manager) == user

    asyncio.run(verify_other_worker())


def test_legacy_stateless_token_without_random_identity_fails_closed(monkeypatch):
    import api_server
    from fastapi_users.jwt import generate_jwt
    from climate_monitor.console_auth import _configured_user

    _configure_auth(monkeypatch, api_server)
    _mock_http_upstream(monkeypatch)
    user = _configured_user()
    assert user is not None
    legacy = generate_jwt(
        {"sub": str(user.id), "aud": ["fastapi-users:auth"]},
        "test-secret-with-at-least-32-bytes",
        1800,
    )
    replay = TestClient(api_server.app, base_url="https://testserver")
    replay.cookies.set("climate_console_session", legacy)
    assert replay.get("/manage", follow_redirects=False).status_code == 303
    assert replay.get("/api/manage/config").status_code == 401
    assert replay.get("/hermes/api/sessions").status_code == 401


def test_expired_session_denies_page_http_and_websocket(monkeypatch):
    import api_server

    _configure_auth(monkeypatch, api_server, seconds=1)
    _mock_http_upstream(monkeypatch)
    client = TestClient(api_server.app, base_url="https://testserver")
    _login(client)
    time.sleep(2)

    assert client.get("/hermes", follow_redirects=False).status_code == 303
    assert client.get("/hermes/api/sessions").status_code == 401
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/hermes/api/pty?token=upstream", headers={"origin": "https://testserver"}
        ):
            pass
    assert exc.value.code == 4401


class _FakeUpstream:
    subprotocol = None

    def __init__(self, leaked_value: str | None = None) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[str] = []
        self.leaked_value = leaked_value

    async def send(self, message: str) -> None:
        self.sent.append(message)
        if self.leaked_value:
            await self.queue.put(f"credential:{self.leaked_value}")
        await self.queue.put(f"tool-result:{message}")

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        return await self.queue.get()


class _FakeConnect:
    def __init__(self, upstream: _FakeUpstream) -> None:
        self.upstream = upstream

    async def __aenter__(self) -> _FakeUpstream:
        return self.upstream

    async def __aexit__(self, *args) -> None:
        return None


def test_access_validator_supplies_unique_channel_required_by_upstream(monkeypatch):
    from scripts import validate_issue114_access as validator

    values = iter(("first", "second"))
    monkeypatch.setattr(validator.secrets, "token_urlsafe", lambda size: next(values))

    def fake_upstream(path: str) -> dict[str, list[str]]:
        query = parse_qs(urlsplit(path).query)
        if not query.get("channel"):
            raise RuntimeError("Hermes rejected events WebSocket without channel")
        return query

    first = fake_upstream(validator._events_path("dashboard-token"))
    second = fake_upstream(validator._events_path("dashboard-token"))

    assert first == {
        "channel": ["issue114-validation-first"],
        "token": ["dashboard-token"],
    }
    assert second["channel"] == ["issue114-validation-second"]


def test_authenticated_websocket_relays_realtime_and_logout_closes_it(monkeypatch):
    import api_server
    import climate_monitor.hermes_dashboard as dashboard

    _configure_auth(monkeypatch, api_server)
    internal_token = "internal-test-token-with-at-least-32-bytes"
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", internal_token)
    upstream = _FakeUpstream(leaked_value=internal_token)
    calls = []

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeConnect(upstream)

    monkeypatch.setattr(dashboard.websockets, "connect", connect)
    client = TestClient(api_server.app, base_url="https://testserver")
    _login(client)

    with client.websocket_connect(
        "/hermes/api/pty?token=hostile-client-token&profile=default",
        headers={
            "origin": "https://testserver",
            "x-hermes-session-token": "hostile-client-token",
        },
    ) as socket:
        socket.send_text("continue-session")
        assert socket.receive_text() == "credential:outer-proxy-authenticated"
        assert socket.receive_text() == "tool-result:continue-session"
        assert client.post("/api/manage/auth/logout").status_code == 204
        replacement = _login(client)
        assert replacement
        message = socket.receive()
        assert message["type"] == "websocket.close"
        assert message["code"] == 4401

    assert upstream.sent == ["continue-session"]
    assert calls[0][0] == (
        "ws://127.0.0.1:9119/api/pty?profile=default&token=" + internal_token
    )
    assert calls[0][1]["additional_headers"]["X-Forwarded-Prefix"] == "/hermes"
    assert "X-Hermes-Session-Token" not in calls[0][1]["additional_headers"]
    assert "hostile-client-token" not in calls[0][0]
    assert internal_token not in str(upstream.sent)
    assert calls[0][1]["logger"].disabled is True


def test_open_websocket_closes_when_shared_session_expires(monkeypatch):
    import api_server
    from climate_monitor.console_auth import ConsoleSessionStore
    import climate_monitor.hermes_dashboard as dashboard

    _configure_auth(monkeypatch, api_server, seconds=30)
    upstream = _FakeUpstream()
    monkeypatch.setattr(
        dashboard.websockets, "connect", lambda *args, **kwargs: _FakeConnect(upstream)
    )
    client = TestClient(api_server.app, base_url="https://testserver")
    _login(client)

    with client.websocket_connect(
        "/hermes/api/events?channel=expiry",
        headers={"origin": "https://testserver"},
    ) as socket:
        with ConsoleSessionStore()._connect() as connection:
            connection.execute("UPDATE console_sessions SET expires_at = 0")
        message = socket.receive()
        assert message["type"] == "websocket.close"
        assert message["code"] == 4401


def test_anonymous_and_cross_origin_websockets_are_denied(monkeypatch):
    import api_server
    import climate_monitor.hermes_dashboard as dashboard

    _configure_auth(monkeypatch, api_server)
    client = TestClient(api_server.app, base_url="https://testserver")
    with pytest.raises(WebSocketDisconnect) as anonymous:
        with client.websocket_connect(
            "/hermes/api/ws", headers={"origin": "https://testserver"}
        ):
            pass
    assert anonymous.value.code == 4401

    _login(client)
    with pytest.raises(WebSocketDisconnect) as cross_origin:
        with client.websocket_connect(
            "/hermes/api/ws", headers={"origin": "https://attacker.example"}
        ):
            pass
    assert cross_origin.value.code == 4403

    def unavailable(*args, **kwargs):
        raise OSError("loopback dashboard is down")

    monkeypatch.setattr(dashboard.websockets, "connect", unavailable)
    with pytest.raises(WebSocketDisconnect) as down:
        with client.websocket_connect(
            "/hermes/api/ws", headers={"origin": "https://testserver"}
        ):
            pass
    assert down.value.code == 1013
    assert down.value.reason == "Hermes Dashboard unavailable"


def test_unavailable_state_and_pinned_isolated_runtime(monkeypatch):
    import api_server
    import climate_monitor.hermes_dashboard as dashboard

    _configure_auth(monkeypatch, api_server)

    class BrokenClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, *args, **kwargs):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(dashboard.httpx, "AsyncClient", BrokenClient)
    client = TestClient(api_server.app, base_url="https://testserver")
    _login(client)
    page = client.get("/hermes")
    assert page.status_code == 503
    assert "Hermes Dashboard unavailable" in page.text
    assert "persistent sessions remain stored" in page.text
    assert client.get("/hermes/api/sessions").json() == {
        "available": False,
        "reason": "hermes_dashboard_unavailable",
    }

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    entrypoint = (ROOT / "scripts" / "docker_entrypoint.sh").read_text(encoding="utf-8")
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")
    assert "node:22.22.0-bookworm-slim" in dockerfile
    assert "checkout 5538bd1f933be2e94aca9755deca5cc59cccc553" in dockerfile
    assert "npm run build --workspace web" in dockerfile
    assert "npm run build --workspace ui-tui" in dockerfile
    assert "HERMES_HOME: /app/output/hermes" in compose
    assert "climate_runtime:/app/output" in compose
    assert "HERMES_DASHBOARD_URL: http://127.0.0.1:9119" in compose
    assert "python -m climate_monitor.hermes_dashboard_server" in entrypoint
    assert "HERMES_DASHBOARD_SESSION_TOKEN" in entrypoint
    assert "secrets.token_urlsafe(32)" in entrypoint
    assert "CLIMATE_CONSOLE_SESSION_DB: /app/output/console-sessions.sqlite3" in compose
    assert "CLIMATE_PUBLIC_ORIGIN: ${CLIMATE_PUBLIC_ORIGIN:-}" in compose
    assert "9119" not in caddy
    assert 'ports:\n      - "80:80"\n      - "443:443"' in compose


def test_caddy_suppresses_oauth_callback_uri_access_logs():
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert "@hermes_oauth_callback path /hermes/api/mcp/oauth/callback/*" in caddy
    assert "log_skip @hermes_oauth_callback" in caddy
    assert "output file /var/log/caddy/access.log" in caddy


def test_shipped_application_logging_does_not_record_oauth_callback_query(tmp_path):
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    command_line = next(
        line.removeprefix("CMD ")
        for line in dockerfile.splitlines()
        if line.startswith("CMD ")
    )
    command = json.loads(command_line)
    assert command[:2] == ["uvicorn", "api_server:app"]

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command[command.index("--host") + 1] = "127.0.0.1"
    command[command.index("--port") + 1] = str(port)

    marker_code = "ISSUE114_CALLBACK_CODE_DO_NOT_LOG_7f93"
    marker_state = "ISSUE114_CALLBACK_STATE_DO_NOT_LOG_b281"
    env = os.environ.copy()
    env.update(
        {
            "CLIMATE_CONSOLE_PASSWORD_HASH": PasswordHelper().hash("unused"),
            "CLIMATE_CONSOLE_SESSION_DB": str(tmp_path / "console-sessions.sqlite3"),
            "CLIMATE_CONSOLE_SESSION_SECRET": "runtime-test-secret-with-at-least-32-bytes",
            "CLIMATE_CONSOLE_SECURE_COOKIE": "true",
            "CLIMATE_CONSOLE_USERNAME": "runtime-test-operator",
            "CLIMATE_REQUIRE_CONSOLE_AUTH": "1",
            "HERMES_DASHBOARD_ENABLED": "0",
            "HERMES_DASHBOARD_URL": "http://127.0.0.1:9119",
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{env['PATH']}",
        }
    )
    process = subprocess.Popen(
        ["sh", str(ROOT / "scripts" / "docker_entrypoint.sh"), *command],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    pytest.fail(f"shipped application exited before startup: {process.stdout.read()}")
                try:
                    if client.get("/api/config").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() >= deadline:
                    pytest.fail("shipped application did not start within 30 seconds")
                time.sleep(0.1)

            response = client.get(
                "/hermes/api/mcp/oauth/callback/calendar",
                params={"code": marker_code, "state": marker_state},
            )
            assert response.status_code == 503
    finally:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate(timeout=10)

    assert marker_code not in output
    assert marker_state not in output
    assert "Application startup complete" in output


def test_dashboard_is_opt_in_and_disabled_mode_starts_wiki_without_origin(tmp_path):
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "HERMES_DASHBOARD_ENABLED: ${HERMES_DASHBOARD_ENABLED:-0}" in compose

    marker = tmp_path / "application-started"
    command = (
        "from pathlib import Path; import sys; "
        "Path(sys.argv[1]).write_text('wiki-chat-started', encoding='utf-8')"
    )
    result = subprocess.run(
        [
            "sh",
            str(ROOT / "scripts" / "docker_entrypoint.sh"),
            sys.executable,
            "-c",
            command,
            str(marker),
        ],
        cwd=ROOT,
        env={
            "PATH": os.environ["PATH"],
            "HERMES_DASHBOARD_ENABLED": "0",
            "CLIMATE_PUBLIC_ORIGIN": "",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "wiki-chat-started"


def test_trusted_public_origin_pins_oauth_callback_and_rejects_header_input(monkeypatch):
    from types import SimpleNamespace

    from climate_monitor.hermes_dashboard_server import (
        install_oauth_callback_adapter,
        oauth_callback_url,
        trusted_public_origin,
    )

    monkeypatch.setenv("CLIMATE_PUBLIC_ORIGIN", "https://climate.example")
    fake = SimpleNamespace(_mcp_oauth_callback_url=lambda request, name: "unsafe")
    install_oauth_callback_adapter(fake, version="0.20.5")
    hostile_request = SimpleNamespace(
        headers={"host": "attacker.example", "x-forwarded-host": "attacker.example"}
    )
    assert fake._mcp_oauth_callback_url(hostile_request, "calendar-primary") == (
        "https://climate.example/hermes/api/mcp/oauth/callback/calendar-primary"
    )
    assert oauth_callback_url("mail") == (
        "https://climate.example/hermes/api/mcp/oauth/callback/mail"
    )
    assert trusted_public_origin("https://climate.example/") == "https://climate.example"
    for invalid in (
        "http://climate.example",
        "https://user:pass@climate.example",
        "https://climate.example/path",
        "https://climate.example?next=evil",
        "https://climate.example#fragment",
        "https://climate.example:invalid",
        "",
    ):
        with pytest.raises(ValueError):
            trusted_public_origin(invalid)
    for invalid_name in ("", ".", "..", "calendar/primary", r"calendar\primary", "%2e%2e"):
        with pytest.raises(ValueError):
            oauth_callback_url(invalid_name)


def test_oauth_callback_uses_narrow_cookie_free_proxy_route(monkeypatch):
    import api_server

    _configure_auth(monkeypatch, api_server)
    requests = _mock_http_upstream(monkeypatch)
    client = TestClient(api_server.app, base_url="https://testserver")

    callback = client.get(
        "/hermes/api/mcp/oauth/callback/calendar?code=safe&state=opaque",
        headers={"host": "attacker.example", "x-forwarded-host": "attacker.example"},
    )
    assert callback.status_code == 200
    assert requests[-1].url.path == "/api/mcp/oauth/callback/calendar"
    assert requests[-1].url.query == b"code=safe&state=opaque"
    assert "climate_console_session" not in requests[-1].headers.get("cookie", "")
    assert "x-forwarded-host" not in requests[-1].headers
    assert requests[-1].headers["x-forwarded-proto"] == "https"
    assert requests[-1].headers["x-forwarded-prefix"] == "/hermes"
    assert client.post("/hermes/api/mcp/oauth/callback/calendar").status_code == 401


@pytest.mark.parametrize(
    "attack",
    [
        "%2e%2e/%2e%2e/%2e%2e/%2e%2e/api/config",
        "%252e%252e/%252e%252e/%252e%252e/%252e%252e/api/config/raw",
        "..%5c..%5c..%5c..%5capi%5csessions",
        "..%255c..%255c..%255c..%255capi%255csessions",
        "calendar%2f..%2f..%2f..%2f..%2fapi%2fsessions/session-1/messages",
        "calendar%252f..%252f..%252f..%252f..%252fapi%252fsessions/session-1/export",
        "%ef%bc%8e%ef%bc%8e/%ef%bc%8fapi/sessions",
        "%2563alendar",
    ],
)
def test_anonymous_oauth_callback_rejects_traversal_and_alternate_segments(
    monkeypatch, attack
):
    import api_server

    _configure_auth(monkeypatch, api_server)
    requests = _mock_http_upstream(monkeypatch)
    client = TestClient(api_server.app, base_url="https://testserver")

    response = client.get(
        f"/hermes/api/mcp/oauth/callback/{attack}",
        params={"code": "hostile", "state": "hostile"},
    )

    assert response.status_code in {401, 404}
    assert requests == []
