from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, Sequence
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

USER_AGENT = "ClimateMonitorRegistry/1.0 (+https://climate.aiinforsearch.com/)"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_REDIRECTS = 3


class FetchFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        final_url: str | None = None,
        content_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.final_url = final_url
        self.content_type = content_type


@dataclass(frozen=True)
class FetchResponse:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class ApprovedTarget:
    url: str
    scheme: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


Resolver = Callable[[str, int], Sequence[str]]


class Transport(Protocol):
    def request(
        self,
        target: ApprovedTarget,
        headers: Mapping[str, str],
        *,
        timeout: float,
        max_bytes: int,
    ) -> FetchResponse: ...


def _default_resolver(hostname: str, port: int) -> Sequence[str]:
    return [
        item[4][0]
        for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    ]


def _is_public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped is not None
        or address.sixtofour is not None
        or address.teredo is not None
    ):
        return False
    return (
        address.is_global
        and not address.is_reserved
        and not address.is_multicast
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_unspecified
    )


def _approve_url(url: str, resolver: Resolver) -> ApprovedTarget:
    if any(ord(character) <= 32 or ord(character) == 127 for character in url):
        raise FetchFailure("unsafe_url", "URL contains prohibited whitespace or controls")
    if len(url) > 4096:
        raise FetchFailure("unsafe_url", "URL exceeds the supported length")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise FetchFailure("unsafe_url", "URL contains an invalid port") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise FetchFailure("unsafe_url", "URL scheme is not allowed")
    if parsed.username is not None or parsed.password is not None:
        raise FetchFailure("unsafe_url", "URL user information is not allowed")
    if not parsed.hostname:
        raise FetchFailure("unsafe_url", "URL hostname is missing")
    default_port = 443 if scheme == "https" else 80
    if port is not None and port != default_port:
        raise FetchFailure("unsafe_url", "URL uses a non-default port")
    hostname = parsed.hostname.rstrip(".").lower()
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise FetchFailure("unsafe_url", "URL hostname is invalid") from exc
    try:
        resolved = tuple(
            dict.fromkeys(
                str(ipaddress.ip_address(item))
                for item in resolver(hostname, default_port)
            )
        )
    except (OSError, ValueError) as exc:
        raise FetchFailure("dns_error", "hostname resolution failed") from exc
    if not resolved:
        raise FetchFailure("dns_error", "hostname did not resolve")
    if any(not _is_public_address(item) for item in resolved):
        raise FetchFailure("unsafe_address", "hostname resolved to a prohibited address")
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="%=&?/:@!$'()*+,;-._~")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    clean_url = urlunsplit((scheme, netloc, path, query, ""))
    return ApprovedTarget(clean_url, scheme, hostname, default_port, resolved)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: float) -> None:
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._pinned_address = address

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class PinnedTransport:
    """Small stdlib transport that connects only to pre-approved DNS results."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise FetchFailure("timeout", "request timed out")
        return remaining

    @staticmethod
    def _active_socket(
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse | None,
    ) -> object | None:
        if response is None:
            candidates = [connection.sock]
        else:
            candidates = []
        fp = getattr(response, "fp", None)
        if response is not None and fp is None:
            return None
        candidates.append(fp)
        raw = getattr(fp, "raw", None)
        candidates.extend(
            (raw, getattr(fp, "_sock", None), getattr(raw, "_sock", None))
        )
        for candidate in candidates:
            if callable(getattr(candidate, "settimeout", None)):
                return candidate
        return None

    def request(
        self,
        target: ApprovedTarget,
        headers: Mapping[str, str],
        *,
        timeout: float,
        max_bytes: int,
    ) -> FetchResponse:
        parsed = urlsplit(target.url)
        request_path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        deadline = self._monotonic() + timeout
        last_error: Exception | None = None
        for address in target.addresses:
            remaining = self._remaining(deadline)
            connection: http.client.HTTPConnection
            if target.scheme == "https":
                connection = _PinnedHTTPSConnection(
                    target.hostname, address, target.port, remaining
                )
            else:
                connection = http.client.HTTPConnection(
                    address, target.port, timeout=remaining
                )
            response: http.client.HTTPResponse | None = None
            watchdog: threading.Timer | None = None
            state_lock = threading.Lock()
            state: dict[str, object] = {
                "done": False,
                "timed_out": False,
                "response": None,
            }

            def abort_at_deadline() -> None:
                with state_lock:
                    if state["done"]:
                        return
                    state["timed_out"] = True
                    active = self._active_socket(
                        connection,
                        state["response"],  # type: ignore[arg-type]
                    )
                    if active is None:
                        return
                    try:
                        shutdown = getattr(active, "shutdown", None)
                        if callable(shutdown):
                            shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        close = getattr(active, "close", None)
                        if callable(close):
                            close()
                    except OSError:
                        pass

            try:
                connection.request("GET", request_path, headers=dict(headers))
                if connection.sock is not None:
                    connection.sock.settimeout(self._remaining(deadline))
                watchdog = threading.Timer(
                    self._remaining(deadline), abort_at_deadline
                )
                watchdog.name = "climate-registry-fetch-watchdog"
                watchdog.daemon = True
                watchdog.start()
                response = connection.getresponse()
                with state_lock:
                    state["response"] = response
                response_headers = {
                    key.lower(): value for key, value in response.getheaders()
                }
                chunks: list[bytes] = []
                received = 0
                read_once = getattr(response, "read1", response.read)
                while True:
                    remaining = self._remaining(deadline)
                    active_socket = self._active_socket(connection, response)
                    if active_socket is not None:
                        active_socket.settimeout(remaining)
                    chunk = read_once(min(64 * 1024, max_bytes + 1 - received))
                    self._remaining(deadline)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                    if received > max_bytes:
                        raise FetchFailure(
                            "body_too_large",
                            "response exceeded the configured byte limit",
                            http_status=response.status,
                            final_url=target.url,
                            content_type=response.getheader("Content-Type"),
                        )
                return FetchResponse(
                    response.status, target.url, response_headers, b"".join(chunks)
                )
            except FetchFailure:
                raise
            except (TimeoutError, socket.timeout) as exc:
                last_error = exc
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                with state_lock:
                    timed_out = bool(state["timed_out"])
                last_error = socket.timeout() if timed_out else exc
            finally:
                with state_lock:
                    state["done"] = True
                try:
                    if response is not None:
                        response.close()
                finally:
                    try:
                        connection.close()
                    finally:
                        if watchdog is not None:
                            watchdog.cancel()
                            watchdog.join()
        if isinstance(last_error, (TimeoutError, socket.timeout)):
            raise FetchFailure("timeout", "request timed out") from last_error
        raise FetchFailure("network_error", "request failed") from last_error


def fetch_document(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    resolver: Resolver = _default_resolver,
    transport: Transport | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    monotonic: Callable[[], float] = time.monotonic,
) -> FetchResponse:
    transport = transport or PinnedTransport(monotonic=monotonic)
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/pdf",
    }
    canonical_request_headers = {
        "if-none-match": "If-None-Match",
        "if-modified-since": "If-Modified-Since",
    }
    for key, value in (headers or {}).items():
        request_headers[canonical_request_headers.get(key.lower(), key)] = value
    current = url
    deadline = monotonic() + timeout
    original_origin: tuple[str, str, int] | None = None
    secure_seen = False
    visited: set[str] = set()
    for redirect_count in range(max_redirects + 1):
        if urlsplit(current).scheme.lower() == "http" and secure_seen:
            raise FetchFailure(
                "tls_downgrade", "redirect chain attempted to downgrade HTTPS"
            )
        target = _approve_url(current, resolver)
        secure_seen = secure_seen or target.scheme == "https"
        if target.url in visited:
            raise FetchFailure(
                "redirect_loop", "redirect loop detected", final_url=target.url
            )
        visited.add(target.url)
        hop_headers = dict(request_headers)
        origin = (target.scheme, target.hostname, target.port)
        if original_origin is None:
            original_origin = origin
        elif origin != original_origin:
            hop_headers.pop("If-None-Match", None)
            hop_headers.pop("If-Modified-Since", None)
        hop_headers["Host"] = (
            f"[{target.hostname}]" if ":" in target.hostname else target.hostname
        )
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise FetchFailure("timeout", "request timed out")
        response = transport.request(target, hop_headers, timeout=remaining, max_bytes=max_bytes)
        if monotonic() >= deadline:
            raise FetchFailure("timeout", "request timed out")
        if not isinstance(response.status, int) or not 100 <= response.status <= 599:
            raise FetchFailure(
                "invalid_response", "transport returned an invalid HTTP status"
            )
        if not isinstance(response.body, bytes):
            raise FetchFailure(
                "invalid_response", "transport returned a non-byte response body"
            )
        response = FetchResponse(
            response.status,
            target.url,
            {str(key).lower(): str(value) for key, value in response.headers.items()},
            response.body,
        )
        if response.status == 304 and not (
            hop_headers.get("If-None-Match")
            or hop_headers.get("If-Modified-Since")
        ):
            raise FetchFailure(
                "invalid_not_modified",
                "304 response followed an unconditional request",
                http_status=304,
                final_url=target.url,
            )
        if response.status not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        if not location:
            raise FetchFailure(
                "invalid_redirect",
                "redirect response omitted Location",
                http_status=response.status,
                final_url=target.url,
            )
        if redirect_count == max_redirects:
            raise FetchFailure(
                "too_many_redirects", "redirect limit exceeded", final_url=target.url
            )
        current = urljoin(target.url, location)
    raise AssertionError("unreachable")
