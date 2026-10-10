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
def reloaded_api_server(monkeypatch, enable_docs, tmp_path):
    """Reload `api_server` with a specific ENABLE_DOCS value, then restore.

    The invariant this enforces: when the block exits, BOTH the environment and
    the imported `api_server` module reflect the state that existed before the
    block was entered. Restoring only the environment (pytest/monkeypatch does
    that automatically) is not enough because `api_server` caches `ENABLE_DOCS`
    into `_ENABLE_DOCS` at import time. Reload against a tiny corpus, then
    restore the original module dictionary so its app and responder stay intact.
    """
    module_state = api_server.__dict__.copy()
    original = {key: os.environ.get(key) for key in ("ENABLE_DOCS", "WIKI_DIR", "SOURCE_DIR")}
    wiki_dir = tmp_path / "wiki"
    source_dir = tmp_path / "sources"
    wiki_dir.mkdir(parents=True)
    source_dir.mkdir()
    monkeypatch.setenv("WIKI_DIR", str(wiki_dir))
    monkeypatch.setenv("SOURCE_DIR", str(source_dir))
    if enable_docs is None:
        monkeypatch.delenv("ENABLE_DOCS", raising=False)
    else:
        monkeypatch.setenv("ENABLE_DOCS", enable_docs)
    try:
        yield importlib.reload(api_server)
    finally:
        for key, value in original.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        api_server.__dict__.clear()
        api_server.__dict__.update(module_state)


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


def test_docs_disabled_by_default(monkeypatch, tmp_path):
    # Build a fresh app with ENABLE_DOCS explicitly absent, so the result does
    # not depend on whatever the developer happens to have exported.
    with reloaded_api_server(monkeypatch, None, tmp_path) as fresh:
        fresh_client = TestClient(fresh.app)
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert fresh_client.get(path).status_code == 404, path


def test_docs_enabled_when_explicitly_opted_in(monkeypatch, tmp_path):
    """Guards against the 404s above passing for an unrelated reason."""
    with reloaded_api_server(monkeypatch, "1", tmp_path) as fresh:
        assert TestClient(fresh.app).get("/openapi.json").status_code == 200


def test_docs_tests_restore_module_state(monkeypatch, tmp_path):
    """Regression: the docs tests must not leak module state.

    After each docs test runs, the imported `api_server` module must reflect the
    same ENABLE_DOCS configuration that existed before the test started. This
    guards the reported failure mode where the module was reloaded with
    ENABLE_DOCS deleted, leaving docs permanently disabled in-process even when
    the pre-test environment had them enabled.
    """
    original = {key: os.environ.get(key) for key in ("ENABLE_DOCS", "WIKI_DIR", "SOURCE_DIR")}
    original_app = api_server.app
    original_responder = api_server.responder
    try:
        for index, pre_state in enumerate((None, "1")):
            with reloaded_api_server(monkeypatch, pre_state, tmp_path / f"pre-{index}") as fresh:
                expected = _docs_enabled_in_env()
                assert fresh._ENABLE_DOCS is expected

                # Run both docs tests against this pre-state.
                test_docs_disabled_by_default(monkeypatch, tmp_path / f"disabled-{index}")
                assert os.environ.get("ENABLE_DOCS") == pre_state
                assert api_server._ENABLE_DOCS is expected, (
                    "docs-disabled test leaked module state"
                )

                test_docs_enabled_when_explicitly_opted_in(monkeypatch, tmp_path / f"enabled-{index}")
                assert os.environ.get("ENABLE_DOCS") == pre_state
                assert api_server._ENABLE_DOCS is expected, (
                    "docs-enabled test leaked module state"
                )
                assert api_server.app is fresh.app
                assert api_server.responder is fresh.responder
    finally:
        # This test must honour the very invariant it asserts: restore the
        # ORIGINAL environment value before restoring the original module state.
        for key, value in original.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        # The outer context restores the complete module snapshot without loading
        # the production corpus again.

    assert api_server.app is original_app
    assert api_server.responder is original_responder
    assert {key: os.environ.get(key) for key in original} == original


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


def test_reload_accepts_correct_token(monkeypatch, tmp_path):
    original_responder = api_server.responder
    original_kb = original_responder.kb
    wiki_dir = tmp_path / "wiki"
    source_dir = tmp_path / "sources"
    wiki_dir.mkdir()
    source_dir.mkdir()
    with monkeypatch.context() as isolated:
        isolated.setattr("api_server.RELOAD_TOKEN", "correct-horse-battery-staple")
        isolated.setattr(api_server, "WIKI_DIR", wiki_dir)
        isolated.setattr(api_server, "SOURCE_DIR", source_dir)
        isolated.setattr(
            api_server,
            "responder",
            api_server.AgenticWikiResponder(wiki_dir, source_dir),
        )
        response = client.post(
            "/api/reload", headers={"X-Reload-Token": "correct-horse-battery-staple"}
        )

        assert response.status_code == 200
        # The reload response must be sanitized the same way /api/config is.
        assert "obsidian_plugin" not in response.json()

    assert api_server.responder is original_responder
    assert api_server.responder.kb is original_kb


def test_reload_requires_token():
    response = client.post("/api/reload", headers={"X-Reload-Token": "definitely-wrong"})

    assert response.status_code in {401, 403}
