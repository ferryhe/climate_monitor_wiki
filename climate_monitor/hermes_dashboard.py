"""Authenticated same-site proxy for the pinned Hermes Web Dashboard."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, cast
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
import websockets
from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.websockets import WebSocketDisconnect

from climate_monitor.console_auth import (
    ConsoleUserManager,
    _USER_DATABASE,
    get_jwt_strategy,
)

COOKIE_NAME = "climate_console_session"
HERMES_PREFIX = "/hermes"
HERMES_SESSION_HEADER = "X-Hermes-Session-Token"
BROWSER_SESSION_TOKEN_SENTINEL = "outer-proxy-authenticated"
OAUTH_CALLBACK_UPSTREAM_PREFIX = "/api/mcp/oauth/callback/"
_OAUTH_SERVER_NAME = re.compile(r"[A-Za-z0-9._~-]{1,128}\Z")
_WEBSOCKET_LOGGER = logging.getLogger(f"{__name__}.upstream_websocket")
# websockets DEBUG records include the request target, which contains the
# pinned Dashboard's query-string credential. Proxy failures are translated to
# generic client close codes below, so suppress this transport logger entirely.
_WEBSOCKET_LOGGER.disabled = True
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


async def console_session_is_valid(token: str) -> bool:
    if not token:
        return False
    try:
        user = await get_jwt_strategy().read_token(
            token, ConsoleUserManager(_USER_DATABASE)
        )
    except Exception:
        return False
    return user is not None and user.is_active and user.is_verified


def _upstream_base() -> str:
    value = os.getenv("HERMES_DASHBOARD_URL", "http://127.0.0.1:9119").rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("HERMES_DASHBOARD_URL must be a loopback HTTP URL")
    return value


def _upstream_url(path: str, query: str = "", *, websocket: bool = False) -> str:
    parsed = urlsplit(_upstream_base())
    scheme = "ws" if websocket else parsed.scheme
    upstream_path = "/" + path.lstrip("/")
    return urlunsplit((scheme, parsed.netloc, upstream_path, query, ""))


def validate_oauth_server_name(server_name: str) -> str:
    """Require an MCP server identifier that is exactly one safe URL segment."""
    if server_name in {".", ".."} or _OAUTH_SERVER_NAME.fullmatch(server_name) is None:
        raise ValueError("OAuth server name must be one safe URL segment")
    return server_name


def oauth_callback_proxy_path(server_name: str, raw_path: bytes | None = None) -> str:
    """Return the one allowed anonymous upstream path for a safe MCP name."""
    # Starlette and front proxies may decode URL escapes a different number of
    # times. The decoded value must be one RFC 3986 unreserved segment, while a
    # raw percent escape is rejected rather than interpreted by another layer.
    raw_segment = (raw_path or b"").split(b"?", 1)[0].rsplit(b"/", 1)[-1]
    try:
        validate_oauth_server_name(server_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Not found.") from exc
    if raw_segment and (b"%" in raw_segment or b"\\" in raw_segment):
        raise HTTPException(status_code=404, detail="Not found.")
    encoded_name = quote(server_name, safe="")
    return OAUTH_CALLBACK_UPSTREAM_PREFIX.lstrip("/") + encoded_name


def _internal_session_token() -> str:
    token = os.getenv("HERMES_DASHBOARD_SESSION_TOKEN", "")
    if not token:
        raise RuntimeError("Hermes Dashboard internal session token is unavailable")
    return token


def _websocket_query(query: str) -> str:
    # The pinned loopback Dashboard authenticates WebSockets only through its
    # token query parameter. Remove every client-supplied value before adding
    # the server-owned credential so a browser cannot override it.
    values = [
        (name, value)
        for name, value in parse_qsl(query, keep_blank_values=True)
        if name != "token"
    ]
    values.append(("token", _internal_session_token()))
    return urlencode(values)


def _request_headers(request: Request) -> dict[str, str]:
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.lower()
        not in _HOP_BY_HOP
        | {
            "accept-encoding",
            "authorization",
            "host",
            "cookie",
            "content-length",
            "forwarded",
            HERMES_SESSION_HEADER.lower(),
        }
        and not name.lower().startswith("x-forwarded-")
    }
    headers["X-Forwarded-Prefix"] = HERMES_PREFIX
    headers["X-Forwarded-Proto"] = "https"
    headers["Accept-Encoding"] = "identity"
    headers[HERMES_SESSION_HEADER] = _internal_session_token()
    return headers


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    result: dict[str, str] = {}
    token = _internal_session_token()
    for name, value in headers.items():
        lower = name.lower()
        if lower in _HOP_BY_HOP | {"content-encoding", "content-length", "set-cookie"}:
            continue
        if token in value:
            continue
        if lower == "location":
            upstream = _upstream_base()
            if value.startswith(upstream):
                value = value[len(upstream) :] or "/"
            if value.startswith("/") and not value.startswith(HERMES_PREFIX):
                value = HERMES_PREFIX + value
        result[name] = value
    return result


def _response_content(content: bytes) -> bytes:
    # Loopback-mode Hermes deliberately bootstraps its token into index.html.
    # The outer proxy owns authentication instead, so redact that credential
    # (and any accidental echo from another endpoint) before bytes reach the
    # browser.
    # Keep a non-secret truthy value because the pinned chat UI treats a
    # missing token as a direct/unmanaged launch and refuses to open its WS.
    # Both HTTP and WS proxy paths discard this sentinel and inject the real
    # credential only on the loopback hop.
    return content.replace(
        _internal_session_token().encode(),
        BROWSER_SESSION_TOKEN_SENTINEL.encode(),
    )


def _websocket_content(content: str | bytes) -> str | bytes:
    if isinstance(content, bytes):
        return _response_content(content)
    return content.replace(_internal_session_token(), BROWSER_SESSION_TOKEN_SENTINEL)


def unavailable_response(path: str = "") -> Response:
    if path == "api" or path.startswith("api/"):
        return JSONResponse(
            status_code=503,
            content={"available": False, "reason": "hermes_dashboard_unavailable"},
        )
    return HTMLResponse(
        status_code=503,
        headers={"Cache-Control": "no-store"},
        content=(
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='robots' content='noindex,nofollow'>"
            "<title>Hermes unavailable</title></head><body>"
            "<main><h1>Hermes Dashboard unavailable</h1>"
            "<p>The isolated Hermes service is not ready. Its persistent sessions "
            "remain stored and will be available after the service restarts.</p>"
            "<p><a href='/'>Return to Climate Monitor</a></p></main></body></html>"
        ),
    )


async def proxy_http(
    request: Request,
    path: str,
    *,
    expected_upstream_path: str | None = None,
) -> Response:
    try:
        upstream_url = httpx.URL(_upstream_url(path, request.url.query))
        # The anonymous callback caller supplies an exact resolved path. Check
        # HTTPX's final normalized URL immediately before dispatch so it can
        # never move the request outside the callback namespace.
        if (
            expected_upstream_path is not None
            and upstream_url.path != expected_upstream_path
        ):
            raise HTTPException(status_code=404, detail="Not found.")
        # This hop is always loopback and carries the private Dashboard token.
        # Never inherit HTTP(S)_PROXY or other HTTPX transport settings from the
        # container environment, even when NO_PROXY is absent or misconfigured.
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            upstream = await client.request(
                request.method,
                upstream_url,
                headers=_request_headers(request),
                content=await request.body(),
            )
    except (httpx.HTTPError, OSError, RuntimeError):
        return unavailable_response(path)
    return Response(
        content=_response_content(upstream.content),
        status_code=upstream.status_code,
        headers=_response_headers(upstream.headers),
        media_type=None,
    )


def _same_origin(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin", "")
    if not origin:
        return False
    parsed = urlsplit(origin)
    configured_origin = os.getenv("CLIMATE_PUBLIC_ORIGIN", "").strip()
    if configured_origin:
        expected = urlsplit(configured_origin)
        return (
            parsed.scheme == expected.scheme
            and parsed.netloc.lower() == expected.netloc.lower()
            and parsed.path == ""
            and not parsed.query
            and not parsed.fragment
        )
    websocket_scheme = "https" if ws.url.scheme == "wss" else "http"
    return (
        parsed.scheme == websocket_scheme
        and parsed.netloc.lower() == ws.headers.get("host", "").lower()
        and parsed.path == ""
        and not parsed.query
        and not parsed.fragment
    )


async def _relay_browser_to_upstream(ws: WebSocket, upstream: Any, token: str) -> None:
    while True:
        if not await console_session_is_valid(token):
            if ws.application_state.name == "CONNECTED":
                await ws.close(code=4401, reason="session ended")
            return
        message = await ws.receive()
        if not await console_session_is_valid(token):
            if ws.application_state.name == "CONNECTED":
                await ws.close(code=4401, reason="session ended")
            return
        if message["type"] == "websocket.disconnect":
            return
        if message.get("text") is not None:
            await upstream.send(message["text"])
        elif message.get("bytes") is not None:
            await upstream.send(message["bytes"])


async def _relay_upstream_to_browser(ws: WebSocket, upstream: Any, token: str) -> None:
    try:
        async for message in upstream:
            if not await console_session_is_valid(token):
                if ws.application_state.name == "CONNECTED":
                    await ws.close(code=4401, reason="session ended")
                return
            if isinstance(message, bytes):
                await ws.send_bytes(cast(bytes, _websocket_content(message)))
            else:
                await ws.send_text(cast(str, _websocket_content(message)))
        close_code = upstream.close_code if upstream.close_code is not None else 1000
        await ws.close(
            code=int(close_code),
            reason=cast(str, _websocket_content(upstream.close_reason or "")),
        )
    except websockets.exceptions.ConnectionClosed as exc:
        if ws.application_state.name == "CONNECTED":
            await ws.close(
                code=int(exc.code),
                reason=cast(str, _websocket_content(exc.reason)),
            )


async def _watch_session(token: str) -> None:
    while await console_session_is_valid(token):
        await asyncio.sleep(0.25)


async def proxy_websocket(ws: WebSocket, path: str) -> None:
    token = ws.cookies.get(COOKIE_NAME, "")
    if not await console_session_is_valid(token):
        await ws.close(code=4401, reason="authentication required")
        return
    if not _same_origin(ws):
        await ws.close(code=4403, reason="same-origin WebSocket required")
        return

    requested_protocols = [
        value.strip()
        for value in ws.headers.get("sec-websocket-protocol", "").split(",")
        if value.strip()
    ]
    base = urlsplit(_upstream_base())
    headers = {
        "X-Forwarded-Prefix": HERMES_PREFIX,
        "X-Forwarded-Proto": "https" if ws.url.scheme == "wss" else "http",
    }
    try:
        async with websockets.connect(
            _upstream_url(path, _websocket_query(ws.url.query), websocket=True),
            origin=cast(Any, f"http://{base.netloc}"),
            subprotocols=cast(Any, requested_protocols or None),
            additional_headers=headers,
            logger=_WEBSOCKET_LOGGER,
            proxy=None,
        ) as upstream:
            await ws.accept(subprotocol=upstream.subprotocol)
            browser_task = asyncio.create_task(_relay_browser_to_upstream(ws, upstream, token))
            upstream_task = asyncio.create_task(_relay_upstream_to_browser(ws, upstream, token))
            session_task = asyncio.create_task(_watch_session(token))
            tasks = {browser_task, upstream_task, session_task}
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if session_task in done and ws.application_state.name == "CONNECTED":
                await ws.close(code=4401, reason="session ended")
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.cancelled():
                    task.exception()
    except (OSError, websockets.WebSocketException, RuntimeError):
        try:
            await ws.close(code=1013, reason="Hermes Dashboard unavailable")
        except RuntimeError:
            pass
    except WebSocketDisconnect:
        return
