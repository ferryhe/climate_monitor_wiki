"""Trusted outer-auth adapter for the pinned Hermes Dashboard.

Hermes 0.20.5 derives MCP OAuth callbacks from ``request.base_url`` unless its
own ``dashboard.public_url`` is set. Setting that option also enables Hermes'
upstream login gate. This deployment already has an outer FastAPI Users gate,
so this narrow adapter supplies only the callback URL from operator-owned
configuration and leaves the loopback Dashboard in session-token mode.
"""
from __future__ import annotations

import importlib.metadata
import os
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from climate_monitor.hermes_dashboard import validate_oauth_server_name


PINNED_HERMES_VERSION = "0.20.5"


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


def install_oauth_callback_adapter(web_server: Any, *, version: str | None = None) -> None:
    actual = version or importlib.metadata.version("hermes-agent")
    if actual != PINNED_HERMES_VERSION:
        raise RuntimeError(
            f"Hermes callback adapter supports {PINNED_HERMES_VERSION}, found {actual}"
        )
    # The router late-binds this web_server symbol at request time in 0.20.5.
    if not callable(getattr(web_server, "_mcp_oauth_callback_url", None)):
        raise RuntimeError("pinned Hermes OAuth callback seam is unavailable")

    def configured_callback(request: Any, server_name: str) -> str:
        del request  # Never trust Host, Forwarded, or X-Forwarded-* for redirects.
        return oauth_callback_url(server_name)

    web_server._mcp_oauth_callback_url = configured_callback


def main() -> None:
    trusted_public_origin()  # Fail before binding if deployment configuration is unsafe.
    from hermes_cli import web_server

    install_oauth_callback_adapter(web_server)
    web_server.start_server(host="127.0.0.1", port=9119, open_browser=False)


if __name__ == "__main__":
    main()