from __future__ import annotations

import hashlib
import json
import re
import socket
import sqlite3
import threading
import time
from io import BytesIO

import pytest
from reportlab.pdfgen import canvas

import climate_registry.capture as capture
import climate_registry.fetch as registry_fetch
from climate_registry.errors import RegistryBuildError, RegistryInputError, RegistryLockError
from climate_registry.schema import apply_migrations


PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def _registry(tmp_path, *, articles=("article-a",), eligible=True):
    database = tmp_path / "registry.sqlite3"
    connection = sqlite3.connect(database)
    apply_migrations(connection)
    with connection:
        connection.execute(
            "INSERT INTO sources VALUES ('source', 'example.com', 'Example', '2026-08-01', '2026-08-01')"
        )
        for index, article_id in enumerate(articles):
            connection.execute(
                """
                INSERT INTO articles(
                    article_id, canonical_url, source_id, first_seen, last_seen,
                    document_kind, publication_eligible, exclusion_reason
                ) VALUES (?, ?, 'source', '2026-08-01', '2026-08-01', ?, ?, ?)
                """,
                (
                    article_id,
                    f"https://example.com/{index}",
                    "article" if eligible else "landing_page",
                    int(eligible),
                    None if eligible else "root-url",
                ),
            )
    connection.close()
    return database


def _html(version="one"):
    return f"""<html><head><title>Climate risk report {version}</title><style>hide</style></head>
    <body><h1>Climate insurance outlook</h1><p>Climate risk and transition risk affect
    insurance capital, underwriting, investment portfolios, disclosure standards, flood
    catastrophe modelling and regulatory policy this week.</p><ul><li><a href="/source">Original evidence</a></li></ul>
    <script>secret()</script></body></html>""".encode()


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, target, headers, *, timeout, max_bytes):
        self.calls.append((target, dict(headers), timeout, max_bytes))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if len(response.body) > max_bytes:
            raise registry_fetch.FetchFailure("body_too_large", "too large", final_url=target.url)
        return registry_fetch.FetchResponse(response.status, target.url, response.headers, response.body)


def _response(status=200, body=None, headers=None):
    return registry_fetch.FetchResponse(
        status,
        "unused",
        headers or {"content-type": "text/html; charset=utf-8", "etag": '"v1"'},
        _html() if body is None else body,
    )


def _run(database, tmp_path, transport, **kwargs):
    return capture.capture_enrich_registry(
        database,
        tmp_path / "backups",
        resolver=lambda host, port: [PUBLIC_V4],
        transport=transport,
        clock=lambda: "2026-08-13T12:00:00Z",
        **kwargs,
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:pass@example.com/a",
        "https://example.com:444/a",
        "http://example.com:81/a",
    ],
)
def test_url_policy_rejects_unsafe_shapes(url):
    with pytest.raises(registry_fetch.FetchFailure, match="URL"):
        registry_fetch._approve_url(url, lambda host, port: [PUBLIC_V4])


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "100.64.0.1",
        "169.254.169.254",
        "192.0.0.1",
        "192.0.2.1",
        "198.18.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "fe80::1",
        "2001:db8::1",
        "64:ff9b:1::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "2002:7f00:1::1",
        "2002:0a00:1::1",
        "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
        "ff02::1",
    ],
)
def test_url_policy_rejects_non_public_ipv4_and_ipv6(address):
    with pytest.raises(registry_fetch.FetchFailure) as error:
        registry_fetch._approve_url("https://example.com/a", lambda host, port: [address])
    assert error.value.code == "unsafe_address"


def test_url_policy_rejects_mixed_dns_and_pins_all_public_results():
    with pytest.raises(registry_fetch.FetchFailure) as error:
        registry_fetch._approve_url("https://example.com/a", lambda host, port: [PUBLIC_V4, "10.0.0.1"])
    assert error.value.code == "unsafe_address"

    target = registry_fetch._approve_url(
        "https://Example.COM/a#fragment", lambda host, port: [PUBLIC_V4, PUBLIC_V6]
    )
    assert target.hostname == "example.com"
    assert target.addresses == (PUBLIC_V4, PUBLIC_V6)
    assert target.url == "https://example.com/a"


def test_url_policy_percent_encodes_unicode_path_and_query_without_double_encoding():
    target = registry_fetch._approve_url(
        "https://example.com/气候/😀?q=气候%20risk",
        lambda host, port: [PUBLIC_V4],
    )
    assert target.url == (
        "https://example.com/%E6%B0%94%E5%80%99/%F0%9F%98%80"
        "?q=%E6%B0%94%E5%80%99%20risk"
    )


def test_redirects_are_revalidated_and_loops_and_limits_fail():
    transport = FakeTransport(
        _response(302, b"", {"location": "https://safe.example/next"}),
        _response(200),
    )
    response = registry_fetch.fetch_document(
        "https://example.com/start",
        headers={"If-None-Match": '"private-validator"'},
        resolver=lambda host, port: [PUBLIC_V4],
        transport=transport,
    )
    assert response.status == 200
    assert [call[0].hostname for call in transport.calls] == ["example.com", "safe.example"]
    assert [call[1]["Host"] for call in transport.calls] == ["example.com", "safe.example"]
    assert transport.calls[0][1]["If-None-Match"] == '"private-validator"'
    assert "If-None-Match" not in transport.calls[1][1]

    transport = FakeTransport(_response(302, b"", {"location": "http://127.0.0.1/"}))
    with pytest.raises(registry_fetch.FetchFailure) as error:
        registry_fetch.fetch_document(
            "https://example.com/start",
            resolver=lambda host, port: ["127.0.0.1"] if host == "127.0.0.1" else [PUBLIC_V4],
            transport=transport,
        )
    assert error.value.code == "tls_downgrade"

    transport = FakeTransport(_response(302, b"", {"location": "/start"}))
    with pytest.raises(registry_fetch.FetchFailure) as error:
        registry_fetch.fetch_document(
            "https://example.com/start",
            resolver=lambda h, p: [PUBLIC_V4],
            transport=transport,
        )
    assert error.value.code == "redirect_loop"

    transport = FakeTransport(
        _response(302, b"", {"location": "/two"}),
        _response(302, b"", {"location": "/three"}),
    )
    with pytest.raises(registry_fetch.FetchFailure) as error:
        registry_fetch.fetch_document(
            "https://example.com/one", resolver=lambda h, p: [PUBLIC_V4],
            transport=transport, max_redirects=1,
        )
    assert error.value.code == "too_many_redirects"


@pytest.mark.parametrize(
    ("start", "responses"),
    [
        (
            "https://example.com/one",
            (_response(302, b"", {"location": "http://safe.example/two"}),),
        ),
        (
            "http://example.com/one",
            (
                _response(302, b"", {"location": "https://safe.example/two"}),
                _response(302, b"", {"location": "http://third.example/three"}),
            ),
        ),
    ],
)
def test_redirect_chain_never_downgrades_after_https(start, responses):
    with pytest.raises(registry_fetch.FetchFailure) as error:
        registry_fetch.fetch_document(
            start,
            resolver=lambda host, port: [PUBLIC_V4],
            transport=FakeTransport(*responses),
        )
    assert error.value.code == "tls_downgrade"


def test_304_requires_validators_on_the_actual_redirect_hop():
    with pytest.raises(registry_fetch.FetchFailure) as error:
        registry_fetch.fetch_document(
            "https://example.com/article",
            resolver=lambda host, port: [PUBLIC_V4],
            transport=FakeTransport(_response(304, b"", {})),
        )
    assert error.value.code == "invalid_not_modified"

    cross_origin = FakeTransport(
        _response(302, b"", {"location": "https://other.example/article"}),
        _response(304, b"", {}),
    )
    with pytest.raises(registry_fetch.FetchFailure) as error:
        registry_fetch.fetch_document(
            "https://example.com/article",
            headers={"If-None-Match": '"v1"'},
            resolver=lambda host, port: [PUBLIC_V4],
            transport=cross_origin,
        )
    assert error.value.code == "invalid_not_modified"

    same_origin = FakeTransport(
        _response(302, b"", {"location": "/canonical"}),
        _response(304, b"", {}),
    )
    response = registry_fetch.fetch_document(
        "https://example.com/article",
        headers={"If-Modified-Since": "Wed, 12 Aug 2026 12:00:00 GMT"},
        resolver=lambda host, port: [PUBLIC_V4],
        transport=same_origin,
    )
    assert response.status == 304
    assert same_origin.calls[1][1]["If-Modified-Since"]


def test_pinned_https_connects_to_ip_but_wraps_with_hostname(monkeypatch):
    calls = {}

    class Socket:
        pass

    class Context:
        def wrap_socket(self, sock, *, server_hostname):
            calls["sni"] = server_hostname
            return sock

    def connect(address, timeout, source):
        calls["connect"] = address
        return Socket()

    monkeypatch.setattr(registry_fetch.socket, "create_connection", connect)
    connection = registry_fetch._PinnedHTTPSConnection("example.com", PUBLIC_V4, 443, 2)
    connection._context = Context()
    connection.connect()
    assert calls["connect"] == (PUBLIC_V4, 443)
    assert calls["sni"] == "example.com"


def test_pinned_transport_maps_socket_timeout_without_resolving_again(monkeypatch):
    class Connection:
        def __init__(self, host, port, timeout):
            assert host == PUBLIC_V4

        def request(self, *args, **kwargs):
            raise socket.timeout()

        def close(self):
            pass

    monkeypatch.setattr(registry_fetch.http.client, "HTTPConnection", Connection)
    target = registry_fetch.ApprovedTarget(
        "http://example.com/a", "http", "example.com", 80, (PUBLIC_V4,)
    )
    with pytest.raises(registry_fetch.FetchFailure) as error:
        registry_fetch.PinnedTransport().request(target, {"Host": "example.com"}, timeout=1, max_bytes=10)
    assert error.value.code == "timeout"


def test_pinned_transport_enforces_total_deadline_during_slow_body(monkeypatch):
    now = [0.0]

    class Socket:
        def settimeout(self, timeout):
            assert timeout > 0

    class Response:
        status = 200

        def getheaders(self):
            return [("Content-Type", "text/html")]

        def getheader(self, name):
            return "text/html"

        def read1(self, amount):
            now[0] += 0.2
            return b"x"

        read = read1

        def close(self):
            pass

    class Connection:
        def __init__(self, host, port, timeout):
            self.sock = Socket()

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(registry_fetch.http.client, "HTTPConnection", Connection)
    target = registry_fetch.ApprovedTarget(
        "http://example.com/a", "http", "example.com", 80, (PUBLIC_V4,)
    )
    transport = registry_fetch.PinnedTransport(monotonic=lambda: now[0])
    with pytest.raises(registry_fetch.FetchFailure) as error:
        transport.request(target, {"Host": "example.com"}, timeout=1, max_bytes=10)
    assert error.value.code == "timeout"
    assert now[0] == pytest.approx(1.0)


def test_close_delimited_http10_body_obeys_deadline_with_response_socket():
    stop = threading.Event()
    server_errors = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.recv(4096)
                    connection.sendall(
                        b"HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n"
                    )
                    while not stop.wait(0.05):
                        connection.sendall(b"x")
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            except Exception as exc:  # pragma: no cover - diagnostic guard
                server_errors.append(exc)

        thread = threading.Thread(target=serve)
        thread.start()
        target = registry_fetch.ApprovedTarget(
            "http://example.com/slow", "http", "example.com", port, ("127.0.0.1",)
        )
        started = time.monotonic()
        try:
            with pytest.raises(registry_fetch.FetchFailure) as error:
                registry_fetch.PinnedTransport().request(
                    target, {"Host": "example.com"}, timeout=0.2, max_bytes=1024
                )
            elapsed = time.monotonic() - started
            assert error.value.code == "timeout"
            assert 0.15 <= elapsed <= 0.6
        finally:
            stop.set()
            thread.join(timeout=1)
        assert not thread.is_alive()
        assert server_errors == []


@pytest.mark.parametrize(
    "response_head",
    [
        b"HTTP/1.0 200 OK\r\nContent-Length: 5\r\n\r\n",
        (
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n"
            b"Connection: close\r\n\r\n"
        ),
    ],
)
def test_content_length_responses_close_cleanly_without_watchdog_leaks(response_head):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve():
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(response_head + b"hello")

        thread = threading.Thread(target=serve)
        thread.start()
        target = registry_fetch.ApprovedTarget(
            "http://example.com/content", "http", "example.com", port, ("127.0.0.1",)
        )
        response = registry_fetch.PinnedTransport().request(
            target, {"Host": "example.com"}, timeout=1, max_bytes=32
        )
        thread.join(timeout=1)
        assert response.body == b"hello"
        assert not thread.is_alive()
        assert not any(
            item.name == "climate-registry-fetch-watchdog"
            for item in threading.enumerate()
        )


def test_slow_chunk_extension_is_interrupted_by_absolute_watchdog_deadline():
    stop = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.recv(4096)
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                        b"Connection: close\r\n\r\n"
                    )
                    for value in b"1;extension=slow\r\n":
                        if stop.wait(0.05):
                            break
                        connection.sendall(bytes([value]))
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        thread = threading.Thread(target=serve)
        thread.start()
        target = registry_fetch.ApprovedTarget(
            "http://example.com/chunked", "http", "example.com", port, ("127.0.0.1",)
        )
        started = time.monotonic()
        try:
            with pytest.raises(registry_fetch.FetchFailure) as error:
                registry_fetch.PinnedTransport().request(
                    target, {"Host": "example.com"}, timeout=0.2, max_bytes=32
                )
            elapsed = time.monotonic() - started
            assert error.value.code == "timeout"
            assert 0.15 <= elapsed <= 0.6
        finally:
            stop.set()
            thread.join(timeout=1)
        assert not thread.is_alive()
        assert not any(
            item.name == "climate-registry-fetch-watchdog"
            for item in threading.enumerate()
        )


def test_chunk_read_error_closes_peer_and_cancels_watchdog():
    peer_closed = threading.Event()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve():
            connection, _ = listener.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                    b"Connection: close\r\n\r\nnot-a-chunk\r\n"
                )
                connection.settimeout(1)
                try:
                    if connection.recv(1) == b"":
                        peer_closed.set()
                except (ConnectionResetError, OSError):
                    peer_closed.set()

        thread = threading.Thread(target=serve)
        thread.start()
        target = registry_fetch.ApprovedTarget(
            "http://example.com/error", "http", "example.com", port, ("127.0.0.1",)
        )
        with pytest.raises(registry_fetch.FetchFailure) as error:
            registry_fetch.PinnedTransport().request(
                target, {"Host": "example.com"}, timeout=1, max_bytes=32
            )
        thread.join(timeout=1)
        assert error.value.code == "network_error"
        assert peer_closed.is_set()
        assert not thread.is_alive()
        assert not any(
            item.name == "climate-registry-fetch-watchdog"
            for item in threading.enumerate()
        )


def test_html_and_pdf_extraction_and_invalid_content():
    markdown = capture.extract_html(
        _html(), base_url="https://example.com/article", charset="utf-8"
    )
    assert "# Climate risk report one" in markdown
    assert "[Original evidence](https://example.com/source)" in markdown
    assert "secret" not in markdown

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, "Climate insurance disclosure and financial risk report")
    pdf.save()
    pdf_markdown = capture.extract_pdf(buffer.getvalue())
    assert "## Page 1" in pdf_markdown
    assert "Climate insurance" in pdf_markdown

    for body, content_type, code in [
        (b"not html", "text/html", "invalid_html"),
        (b"%PDF-bad", "application/pdf", "invalid_pdf"),
        (b"words", "text/plain", "unsupported_mime"),
        (b"<html><script>x</script></html>", "text/html", "empty_content"),
    ]:
        with pytest.raises(registry_fetch.FetchFailure) as error:
            capture.extract_markdown(body, content_type)
        assert error.value.code == code


def test_pdf_extraction_has_page_and_text_limits(monkeypatch):
    class Page:
        def __init__(self, text="text"):
            self.text = text

        def extract_text(self):
            return self.text

    class TooManyPages:
        is_encrypted = False
        pages = [Page()] * (capture.MAX_PDF_PAGES + 1)

    monkeypatch.setattr(capture, "PdfReader", lambda stream: TooManyPages())
    with pytest.raises(registry_fetch.FetchFailure) as error:
        capture.extract_pdf(b"%PDF-fixture")
    assert error.value.code == "pdf_too_many_pages"

    class TooMuchText:
        is_encrypted = False
        pages = [Page("x" * (capture.MAX_EXTRACTED_TEXT_CHARS + 1))]

    monkeypatch.setattr(capture, "PdfReader", lambda stream: TooMuchText())
    with pytest.raises(registry_fetch.FetchFailure) as error:
        capture.extract_pdf(b"%PDF-fixture")
    assert error.value.code == "extracted_text_too_large"


def test_html_extraction_honors_explicit_charset_and_fails_unknown_charset():
    body = "<html><p>Risque climatique à Montréal.</p></html>".encode("iso-8859-1")
    markdown, method = capture.extract_markdown(
        body,
        "text/html; charset=iso-8859-1",
        base_url="https://example.com/article",
    )
    assert markdown == "Risque climatique à Montréal."
    assert method == "html-stdlib"
    with pytest.raises(registry_fetch.FetchFailure) as error:
        capture.extract_markdown(
            body,
            "text/html; charset=not-a-real-charset",
            base_url="https://example.com/article",
        )
    assert error.value.code == "invalid_charset"
    with pytest.raises(registry_fetch.FetchFailure) as error:
        capture.extract_markdown(
            body,
            "text/html; charset=utf-8",
            base_url="https://example.com/article",
        )
    assert error.value.code == "invalid_charset"


def test_html_extraction_resolves_path_relative_links_from_final_url():
    markdown, _ = capture.extract_markdown(
        b'<html><p><a href="evidence/item.pdf">Evidence</a></p></html>',
        "text/html; charset=utf-8",
        base_url="https://publisher.example/reports/current/index.html",
    )
    assert markdown == (
        "[Evidence](https://publisher.example/reports/current/evidence/item.pdf)"
    )


def test_html_markdown_limit_records_failed_fetch_without_content_version(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(capture, "MAX_EXTRACTED_TEXT_CHARS", 120)
    links = "".join(
        f'<a href="relative/evidence-{index}.html">Evidence {index}</a>'
        for index in range(20)
    )
    body = f"<html><p>{links}</p></html>".encode()
    database = _registry(tmp_path)
    result = _run(database, tmp_path, FakeTransport(_response(body=body)))
    assert result["status"] == "partial"
    assert result["articles"][0]["error_code"] == "extracted_text_too_large"
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT COUNT(*) FROM article_content_versions").fetchone() == (0,)
    assert connection.execute(
        "SELECT fetch_status, error_code FROM article_fetches"
    ).fetchone() == ("failed", "extracted_text_too_large")
    connection.close()


def test_capture_creates_content_fetch_and_deterministic_enrichment(tmp_path):
    database = _registry(tmp_path)
    result = _run(database, tmp_path, FakeTransport(_response()))
    assert result["status"] == "updated"
    assert result["counts"] == {"captured": 1}
    connection = sqlite3.connect(database)
    content = connection.execute(
        "SELECT content_sha256, markdown_sha256, source_bytes, extraction_method FROM article_content_versions"
    ).fetchone()
    assert content[0] == hashlib.sha256(_html()).hexdigest()
    assert len(content[1]) == 64 and content[2] == len(_html()) and content[3] == "html-stdlib"
    enrichment = connection.execute(
        "SELECT summary, categories_json, keywords_json, language, generator_name, "
        "generator_version FROM article_enrichments"
    ).fetchone()
    assert "Climate risk" in enrichment[0]
    assert "insurance" in json.loads(enrichment[1])
    assert 8 <= len(json.loads(enrichment[2])) <= 12
    assert enrichment[3:] == ("en", capture.GENERATOR_NAME, capture.GENERATOR_VERSION)
    assert connection.execute("SELECT current_content_version_id FROM articles").fetchone()[0]
    connection.close()
    backup = sqlite3.connect(result["backup"])
    assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert backup.execute("SELECT COUNT(*) FROM article_fetches").fetchone() == (0,)
    backup.close()


def test_conditional_304_same_hash_and_new_hash_are_audited_without_duplicate_enrichment(tmp_path):
    database = _registry(tmp_path)
    first_transport = FakeTransport(_response())
    _run(database, tmp_path, first_transport)

    second_transport = FakeTransport(
        _response(
            304,
            b"",
            {"etag": '"v2"', "last-modified": "Wed, 12 Aug 2026 12:00:00 GMT"},
        )
    )
    result = _run(database, tmp_path, second_transport, refresh=True)
    assert result["counts"] == {"not_modified": 1}
    assert second_transport.calls[0][1]["If-None-Match"] == '"v1"'

    third_transport = FakeTransport(_response())
    result = _run(database, tmp_path, third_transport, refresh=True)
    assert third_transport.calls[0][1]["If-None-Match"] == '"v2"'
    assert third_transport.calls[0][1]["If-Modified-Since"] == (
        "Wed, 12 Aug 2026 12:00:00 GMT"
    )
    assert result["articles"][0]["content_version"] == "reused"

    changed = _html("two")
    result = _run(database, tmp_path, FakeTransport(_response(body=changed)), refresh=True)
    assert result["articles"][0]["content_version"] == "new"
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT COUNT(*) FROM article_content_versions").fetchone() == (2,)
    assert connection.execute("SELECT COUNT(*) FROM article_fetches").fetchone() == (4,)
    assert connection.execute("SELECT COUNT(*) FROM article_enrichments").fetchone() == (2,)
    connection.close()


def test_failed_enrichment_remains_partial_when_followed_by_304(tmp_path):
    database = _registry(tmp_path)
    short = b"<html><p>Climate risk.</p></html>"
    first = _run(database, tmp_path, FakeTransport(_response(body=short)))
    assert first["status"] == "partial"
    assert first["counts"] == {"enrichment_failed": 1}
    second = _run(
        database,
        tmp_path,
        FakeTransport(_response(304, b"", {"etag": '"short"'})),
        refresh=True,
    )
    assert second["status"] == "partial"
    assert second["counts"] == {"enrichment_failed": 1}


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (registry_fetch.FetchFailure("timeout", "timed out"), "timeout"),
        (registry_fetch.FetchFailure("network_error", "network"), "network_error"),
        (registry_fetch.FetchFailure("body_too_large", "large"), "body_too_large"),
    ],
)
def test_failed_attempt_is_append_only_sanitized_and_partial(failure, code, tmp_path):
    database = _registry(tmp_path)
    result = _run(database, tmp_path, FakeTransport(failure))
    assert result["status"] == "partial"
    assert result["articles"] == [{"article_id": "article-a", "status": "failed", "error_code": code}]
    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT fetch_status, error_code, content_version_id FROM article_fetches"
    ).fetchone() == ("failed", code, None)
    connection.close()


def test_selection_defaults_to_uncaptured_eligible_and_explicit_ids_are_exact(tmp_path):
    database = _registry(tmp_path, articles=("a", "b"))
    connection = sqlite3.connect(database)
    with connection:
        connection.execute(
            "UPDATE articles SET publication_eligible = 0, document_kind = 'landing_page', "
            "exclusion_reason = 'root-url' WHERE article_id = 'b'"
        )
    connection.close()
    result = _run(database, tmp_path, FakeTransport(_response()), limit=1)
    assert [item["article_id"] for item in result["articles"]] == ["a"]
    assert _run(database, tmp_path, FakeTransport(), limit=1)["status"] == "no-op"
    with pytest.raises(RegistryInputError, match="publication-eligible"):
        _run(database, tmp_path, FakeTransport(), article_ids=["b"])
    with pytest.raises(RegistryInputError, match="do not exist"):
        _run(database, tmp_path, FakeTransport(), article_ids=["missing"])


def test_selection_has_a_hard_one_hundred_article_batch_limit(tmp_path):
    article_ids = tuple(f"article-{index:03d}" for index in range(101))
    database = _registry(tmp_path, articles=article_ids)
    connection = sqlite3.connect(database)
    rows = capture._select_articles(connection, (), refresh=False, limit=None)
    assert len(rows) == capture.MAX_BATCH == 100
    with pytest.raises(RegistryInputError, match="at most 100"):
        capture._select_articles(
            connection, article_ids, refresh=False, limit=None
        )
    connection.close()


def test_refresh_batches_advance_to_never_fetched_then_oldest(tmp_path):
    database = _registry(tmp_path, articles=("a", "b", "c"))
    first = _run(
        database,
        tmp_path,
        FakeTransport(_response(), _response()),
        refresh=True,
        limit=2,
    )
    assert [item["article_id"] for item in first["articles"]] == ["a", "b"]

    second = _run(
        database,
        tmp_path,
        FakeTransport(_response(), _response()),
        refresh=True,
        limit=2,
    )
    assert [item["article_id"] for item in second["articles"]] == ["c", "a"]


def test_unicode_enrichment_is_factual_and_idempotent():
    markdown = """# 气候风险与保险监管更新

气候风险报告讨论保险资本、洪水灾害、投资披露和监管政策。该报告说明精算分析如何支持风险管理。
Climate risk disclosure supports insurance capital investment and actuarial catastrophe modelling standards."""
    first = capture.deterministic_enrichment(markdown)
    second = capture.deterministic_enrichment(markdown)
    assert first == second
    assert first["language"] == "mixed"
    source_text = re.sub(r"\s+", " ", re.sub(r"(?m)^#\s+", "", markdown)).strip()
    assert all(part.strip() in source_text for part in re.split(r"[。.]", first["summary"]) if part.strip())
    assert 8 <= len(first["keywords"]) <= 12


def test_keyword_extraction_is_deterministic_for_many_unique_terms():
    def letters(value):
        result = ""
        while value:
            value, remainder = divmod(value - 1, 26)
            result = chr(97 + remainder) + result
        return result or "a"

    words = [f"term{letters(index)}" for index in range(1, 5001)]
    markdown = "Climate insurance regulation analysis. " + " ".join(words)
    first = capture.deterministic_enrichment(markdown)
    assert first == capture.deterministic_enrichment(markdown)
    assert first["keywords"][:3] == ["climate", "insurance", "regulation"]


def test_live_database_is_unchanged_for_lock_sidecar_and_candidate_failure(tmp_path, monkeypatch):
    database = _registry(tmp_path)
    before = database.read_bytes()
    lock = database.with_name(f"{database.name}.lock")
    lock.write_text("stale", encoding="ascii")
    with pytest.raises(RegistryLockError):
        _run(database, tmp_path, FakeTransport(_response()))
    assert database.read_bytes() == before
    lock.unlink()

    sidecar = database.with_name(f"{database.name}-wal")
    sidecar.write_bytes(b"stale")
    with pytest.raises(RegistryInputError, match="sidecar"):
        _run(database, tmp_path, FakeTransport(_response()))
    assert database.read_bytes() == before
    sidecar.unlink()

    original_validate = capture._validate_database
    validations = 0

    def fail_candidate(connection):
        nonlocal validations
        validations += 1
        if validations >= 3:
            raise RegistryBuildError("bad candidate")
        return original_validate(connection)

    monkeypatch.setattr(capture, "_validate_database", fail_candidate)
    with pytest.raises(RegistryBuildError):
        _run(database, tmp_path, FakeTransport(_response()))
    assert database.read_bytes() == before


def test_live_fingerprint_race_aborts_atomic_install(tmp_path):
    database = _registry(tmp_path)
    original = database.read_bytes()

    class RacingTransport(FakeTransport):
        def request(self, target, headers, *, timeout, max_bytes):
            with database.open("ab") as handle:
                handle.write(b"race")
            return super().request(target, headers, timeout=timeout, max_bytes=max_bytes)

    with pytest.raises(RegistryLockError, match="changed"):
        _run(database, tmp_path, RacingTransport(_response()))
    assert database.read_bytes() == original + b"race"
    assert not database.with_name(f"{database.name}.lock").exists()


def test_max_body_and_timeout_failures_have_stable_codes():
    with pytest.raises(registry_fetch.FetchFailure) as error:
        registry_fetch.fetch_document(
            "https://example.com/a", resolver=lambda h, p: [PUBLIC_V4],
            transport=FakeTransport(_response(body=b"x" * 20)), max_bytes=10,
        )
    assert error.value.code == "body_too_large"
    with pytest.raises(registry_fetch.FetchFailure) as error:
        registry_fetch.fetch_document(
            "https://example.com/a", resolver=lambda h, p: [PUBLIC_V4],
            transport=FakeTransport(registry_fetch.FetchFailure("timeout", "timeout")),
        )
    assert error.value.code == "timeout"


def test_access_challenge_is_a_failed_fetch_not_a_content_version(tmp_path):
    database = _registry(tmp_path)
    body = b"<html><body><p>Access denied. Verify you are human with CAPTCHA.</p></body></html>"
    result = _run(database, tmp_path, FakeTransport(_response(body=body)))
    assert result["articles"][0]["error_code"] == "blocked_response"
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT http_status, error_code FROM article_fetches").fetchone() == (
        200,
        "blocked_response",
    )
    assert connection.execute("SELECT COUNT(*) FROM article_content_versions").fetchone() == (0,)
    connection.close()
