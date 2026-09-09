"""Regression tests for the production HTTP security hardening pass.

Covers the behaviours introduced alongside the CORS / input-limit /
config-sanitization / docs-disable changes in `api_server.py`.
"""

import contextlib
import importlib
import os

import api_server
from api_server import MAX_MESSAGE_LENGTH, MAX_MESSAGES, MAX_REQUEST_BYTES, app
from fastapi.testclient import TestClient

client = TestClient(app)


@contextlib.contextmanager
def reloaded_api_server(monkeypatch, enable_docs):
    """Reload `api_server` with a specific ENABLE_DOCS value, then restore.

    The invariant this enforces: when the block exits, BOTH the environment and
    the imported `api_server` module reflect the state that existed before the
    block was entered. Restoring only the environment (pytest/monkeypatch does
    that automatically) is not enough -- `api_server` caches `ENABLE_DOCS` into
    `_ENABLE_DOCS` at import time, so the module must be reloaded *while* the
    original environment value is in place, not after it has been deleted.
    """
    original = os.environ.get("ENABLE_DOCS")
    if enable_docs is None:
        monkeypatch.delenv("ENABLE_DOCS", raising=False)
    else:
        monkeypatch.setenv("ENABLE_DOCS", enable_docs)
    try:
        yield importlib.reload(api_server)
    finally:
        # Put the ORIGINAL environment back before the final reload so the
        # module ends up in its pre-test configuration.
        if original is None:
            monkeypatch.delenv("ENABLE_DOCS", raising=False)
        else:
            monkeypatch.setenv("ENABLE_DOCS", original)
        importlib.reload(api_server)


def _docs_enabled_in_env():
    return os.getenv("ENABLE_DOCS", "").strip().lower() in {"1", "true", "yes"}


def test_security_headers_present_on_responses():
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "geolocation=()" in response.headers["Permissions-Policy"]
    assert response.headers["Strict-Transport-Security"].startswith("max-age=")


def test_cors_does_not_echo_arbitrary_origin():
    response = client.get("/api/health", headers={"Origin": "https://evil.example.com"})

    assert response.status_code == 200
    assert response.headers.get("Access-Control-Allow-Origin") != "*"
    assert response.headers.get("Access-Control-Allow-Origin") != "https://evil.example.com"


def test_api_config_omits_internal_only_fields():
    payload = client.get("/api/config").json()

    assert "obsidian_plugin" not in payload
    assert "retrieval_corpora" not in payload
    # Public repository link is deliberately retained for frontend source links.
    assert payload["github_blob_base_url"].startswith("https://github.com/")


def test_docs_disabled_by_default(monkeypatch):
    # Build a fresh app with ENABLE_DOCS explicitly absent, so the result does
    # not depend on whatever the developer happens to have exported.
    with reloaded_api_server(monkeypatch, None) as fresh:
        fresh_client = TestClient(fresh.app)
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert fresh_client.get(path).status_code == 404, path


def test_docs_enabled_when_explicitly_opted_in(monkeypatch):
    """Guards against the 404s above passing for an unrelated reason."""
    with reloaded_api_server(monkeypatch, "1") as fresh:
        assert TestClient(fresh.app).get("/openapi.json").status_code == 200


def test_docs_tests_restore_module_state(monkeypatch):
    """Regression: the docs tests must not leak module state.

    After each docs test runs, the imported `api_server` module must reflect the
    same ENABLE_DOCS configuration that existed before the test started. This
    guards the reported failure mode where the module was reloaded with
    ENABLE_DOCS deleted, leaving docs permanently disabled in-process even when
    the pre-test environment had them enabled.
    """
    for pre_state in (None, "1"):
        if pre_state is None:
            monkeypatch.delenv("ENABLE_DOCS", raising=False)
        else:
            monkeypatch.setenv("ENABLE_DOCS", pre_state)
        importlib.reload(api_server)
        expected = _docs_enabled_in_env()
        assert api_server._ENABLE_DOCS is expected

        # Run both docs tests against this pre-state.
        test_docs_disabled_by_default(monkeypatch)
        assert os.environ.get("ENABLE_DOCS") == pre_state
        assert api_server._ENABLE_DOCS is expected, (
            "docs-disabled test leaked module state"
        )

        test_docs_enabled_when_explicitly_opted_in(monkeypatch)
        assert os.environ.get("ENABLE_DOCS") == pre_state
        assert api_server._ENABLE_DOCS is expected, (
            "docs-enabled test leaked module state"
        )

    monkeypatch.delenv("ENABLE_DOCS", raising=False)
    importlib.reload(api_server)


def test_chat_message_content_is_required():
    """A message object with no `content` must be rejected, not defaulted."""
    response = client.post("/api/chat", json={"message": "hi", "messages": [{"role": "user"}]})

    assert response.status_code == 422


def test_chat_rejects_oversized_message():
    response = client.post("/api/chat", json={"message": "a" * (MAX_MESSAGE_LENGTH + 1)})

    assert response.status_code == 422


def test_chat_rejects_too_many_messages():
    messages = [{"role": "user", "content": "hi"} for _ in range(MAX_MESSAGES + 1)]
    response = client.post("/api/chat", json={"message": "hi", "messages": messages})

    assert response.status_code == 400


def test_chat_accepts_normal_request():
    response = client.post("/api/chat", json={"message": "What is parametric insurance?"})

    assert response.status_code == 200
    body = response.json()
    assert body["text"]
    assert body["agent_mode"] == "offline"


def test_oversized_request_body_rejected():
    oversized = b"x" * (MAX_REQUEST_BYTES + 1)
    response = client.post(
        "/api/chat",
        content=oversized,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413


def test_reload_rejects_wrong_token(monkeypatch):
    """Exercise the token branch specifically, not the localhost fallback."""
    monkeypatch.setattr("api_server.RELOAD_TOKEN", "correct-horse-battery-staple")
    response = client.post("/api/reload", headers={"X-Reload-Token": "definitely-wrong"})

    assert response.status_code in {401, 403}


def test_reload_accepts_correct_token(monkeypatch):
    monkeypatch.setattr("api_server.RELOAD_TOKEN", "correct-horse-battery-staple")
    response = client.post(
        "/api/reload", headers={"X-Reload-Token": "correct-horse-battery-staple"}
    )

    assert response.status_code == 200
    # The reload response must be sanitized the same way /api/config is.
    assert "obsidian_plugin" not in response.json()


def test_reload_requires_token():
    response = client.post("/api/reload", headers={"X-Reload-Token": "definitely-wrong"})

    assert response.status_code in {401, 403}
