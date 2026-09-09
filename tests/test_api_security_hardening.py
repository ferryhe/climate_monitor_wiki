"""Regression tests for the production HTTP security hardening pass.

Covers the behaviours introduced alongside the CORS / input-limit /
config-sanitization / docs-disable changes in `api_server.py`.
"""

from api_server import MAX_MESSAGE_LENGTH, MAX_MESSAGES, MAX_REQUEST_BYTES, app
from fastapi.testclient import TestClient

client = TestClient(app)


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
    monkeypatch.delenv("ENABLE_DOCS", raising=False)
    import importlib

    import api_server

    fresh = importlib.reload(api_server)
    fresh_client = TestClient(fresh.app)
    try:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert fresh_client.get(path).status_code == 404, path
    finally:
        importlib.reload(api_server)


def test_docs_enabled_when_explicitly_opted_in(monkeypatch):
    """Guards against the 404s above passing for an unrelated reason."""
    monkeypatch.setenv("ENABLE_DOCS", "1")
    import importlib

    import api_server

    fresh = importlib.reload(api_server)
    try:
        assert TestClient(fresh.app).get("/openapi.json").status_code == 200
    finally:
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
