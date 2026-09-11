#!/usr/bin/env python3
"""Side-effect-free deployed access check for Issue #114.

Credentials are read from environment variables so they do not appear in shell
history or process arguments. This probe does not send a model prompt or alter a
Hermes session.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import ssl
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect
from websockets.typing import Origin


_TOKEN_RE = re.compile(r'window\.__HERMES_SESSION_TOKEN__="([^"]+)"')
_COOKIE = "climate_console_session"


def _ws_url(base_url: str, path: str) -> str:
    parsed = urlsplit(base_url)
    return urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc, path, "", ""))


def _events_path(dashboard_token: str) -> str:
    query = urlencode(
        {
            "channel": f"issue114-validation-{secrets.token_urlsafe(12)}",
            "token": dashboard_token,
        }
    )
    return f"/hermes/api/events?{query}"


def _assert_status(response: httpx.Response, expected: int, label: str) -> None:
    if response.status_code != expected:
        raise RuntimeError(f"{label}: expected HTTP {expected}, got {response.status_code}")


def validate(base_url: str, *, verify_tls: bool) -> dict[str, object]:
    username = os.getenv("CLIMATE_VALIDATION_USERNAME", "")
    password = os.getenv("CLIMATE_VALIDATION_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("set CLIMATE_VALIDATION_USERNAME and CLIMATE_VALIDATION_PASSWORD")

    base_url = base_url.rstrip("/")
    origin = urlunsplit((*urlsplit(base_url)[:2], "", "", ""))
    ssl_context = None
    if base_url.startswith("https://") and not verify_tls:
        ssl_context = ssl._create_unverified_context()
    result: dict[str, object] = {"base_url": base_url, "model_prompt_sent": False}
    with httpx.Client(base_url=base_url, verify=verify_tls, follow_redirects=False, timeout=30) as client:
        anonymous_page = client.get("/hermes")
        _assert_status(anonymous_page, 303, "anonymous page gate")
        anonymous_api = client.get("/hermes/api/sessions")
        _assert_status(anonymous_api, 401, "anonymous API gate")

        try:
            connect(
                _ws_url(base_url, "/hermes/api/events"),
                origin=Origin(origin),
                ssl=ssl_context,
                open_timeout=5,
            )
        except InvalidStatus:
            pass
        else:
            raise RuntimeError("anonymous WebSocket unexpectedly connected")

        login = client.post(
            "/api/manage/auth/login",
            data={"username": username, "password": password},
        )
        _assert_status(login, 204, "shared login")
        console_token = client.cookies.get(_COOKIE)
        if not console_token:
            raise RuntimeError("shared login did not issue the console cookie")

        dashboard = client.get("/hermes")
        _assert_status(dashboard, 200, "authenticated Dashboard page")
        match = _TOKEN_RE.search(dashboard.text)
        if match is None:
            raise RuntimeError("Dashboard did not inject its loopback session token")
        dashboard_token = match.group(1)
        sessions = client.get(
            "/hermes/api/sessions?limit=1",
            headers={"X-Hermes-Session-Token": dashboard_token},
        )
        _assert_status(sessions, 200, "authenticated Dashboard sessions API")

        ws_headers = {"Cookie": f"{_COOKIE}={console_token}"}
        with connect(
            _ws_url(base_url, _events_path(dashboard_token)),
            origin=Origin(origin),
            additional_headers=ws_headers,
            ssl=ssl_context,
            open_timeout=10,
            close_timeout=3,
        ):
            pass

        # An invalid state cannot mutate credentials. A 404 containing Hermes'
        # expiry page proves the public, cookie-free callback reached upstream.
        callback = httpx.get(
            f"{base_url}/hermes/api/mcp/oauth/callback/issue114-validation",
            params={"state": "deliberately-invalid"},
            verify=verify_tls,
            follow_redirects=False,
            timeout=30,
        )
        _assert_status(callback, 404, "public OAuth callback route")
        if "OAuth flow expired" not in callback.text:
            raise RuntimeError("OAuth callback response did not come from Hermes")

        logout = client.post("/api/manage/auth/logout")
        _assert_status(logout, 204, "shared logout")
        replay = httpx.get(
            f"{base_url}/hermes/api/sessions",
            headers={"Cookie": f"{_COOKIE}={console_token}"},
            verify=verify_tls,
            follow_redirects=False,
            timeout=30,
        )
        _assert_status(replay, 401, "logged-out token replay")

    result.update(
        {
            "anonymous_page": "redirected",
            "anonymous_http_api": "denied",
            "anonymous_websocket": "denied",
            "authenticated_dashboard": "loaded",
            "authenticated_sessions_api": "loaded",
            "authenticated_websocket": "connected",
            "oauth_callback_route": "reached_upstream_with_invalid_state_rejected",
            "logout_replay": "denied",
        }
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", help="trusted deployment origin, e.g. https://climate.example")
    parser.add_argument("--insecure-tls", action="store_true", help="test-only: accept a self-signed certificate")
    args = parser.parse_args()
    print(json.dumps(validate(args.base_url, verify_tls=not args.insecure_tls), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
