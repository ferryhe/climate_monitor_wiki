from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3

import pytest
from fastapi.testclient import TestClient

import api_server
from climate_monitor.chat_access import CHAT_COOKIE, INVALID_TOKEN, ChatAccessStore, calendar_window


def test_subprocess_chat_database_does_not_write_to_content_fixture(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    (tmp_path / "content.md").write_text("Original content bytes", encoding="utf-8")
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    database = Path(os.environ["CLIMATE_CHAT_ACCESS_DB"])
    assert not database.is_relative_to(tmp_path)
    assert not database.is_relative_to(api_server.ROOT / "output")
    script = '''from pathlib import Path
import os
import api_server
from fastapi.testclient import TestClient
api_server.responder.answer = lambda question, **kwargs: {"text": "Offline answer", "sources": []}
client = TestClient(api_server.app)
assert client.post("/api/chat", json={"message": "A valid question"}).status_code == 200
assert client.get("/api/chat/access").json()["remaining"] == 4
assert api_server._chat_access().path == Path(os.environ["CLIMATE_CHAT_ACCESS_DB"])
'''
    completed = subprocess.run([sys.executable, "-c", script], cwd=api_server.ROOT,
                               env=dict(os.environ, OPENAI_API_KEY="", ANTHROPIC_API_KEY=""),
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert database.is_file()
    assert {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


def test_atomic_reserve_commit_release_and_restart(tmp_path):
    path = tmp_path / "access.sqlite3"
    store = ChatAccessStore(path)

    def reserve(_):
        try:
            return store.reserve("203.0.113.1")
        except OverflowError:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        reservations = [value for value in pool.map(reserve, range(12)) if value]
    assert len(reservations) == 5
    assert store.status("203.0.113.1")["remaining"] == 0
    assert store.status("203.0.113.2")["remaining"] == 5
    store.finish(reservations[0], completed=False)
    replacement = store.reserve("203.0.113.1")
    store.finish(replacement, completed=True)
    store.finish(replacement, completed=True)  # A repeated completion cannot charge twice.
    restarted = ChatAccessStore(path)
    assert restarted.status("203.0.113.1")["remaining"] == 4
    for _ in range(4):
        restarted.finish(restarted.reserve("203.0.113.1"), completed=True)
    with pytest.raises(OverflowError):
        restarted.reserve("203.0.113.1")
    assert ChatAccessStore(path).status("203.0.113.1")["remaining"] == 0


@pytest.mark.parametrize("start,hours,offset", [
    ("2026-03-08T05:00:00+00:00", 23, "-04:00"),
    ("2026-11-01T04:00:00+00:00", 25, "-05:00"),
])
def test_new_york_midnight_across_dst(start, hours, offset):
    now = datetime.fromisoformat(start)
    day, reset = calendar_window(now)
    assert day == now.date().isoformat()
    assert reset.endswith(offset)
    assert (datetime.fromisoformat(reset).astimezone(timezone.utc) - now).total_seconds() == hours * 3600


def test_day_resets_and_inflight_completion_keeps_original_day(tmp_path, monkeypatch):
    import climate_monitor.chat_access as access
    store = ChatAccessStore(tmp_path / "access.sqlite3")
    monkeypatch.setattr(access, "calendar_window", lambda: ("2026-10-10", "2026-10-11T00:00:00-04:00"))
    pending = store.reserve("ip")
    for _ in range(4):
        store.finish(store.reserve("ip"), completed=True)
    monkeypatch.setattr(access, "calendar_window", lambda: ("2026-10-11", "2026-10-12T00:00:00-04:00"))
    assert store.status("ip")["remaining"] == 5
    store.finish(pending, completed=True)
    assert store.status("ip")["remaining"] == 5


@pytest.fixture
def clients(monkeypatch):
    def answer(question, **kwargs):
        return {"text": question, "sources": [], "answer_mode": kwargs["answer_mode"], "agent_mode": "offline"}
    monkeypatch.setattr(api_server.responder, "answer", answer)
    return (TestClient(api_server.app, client=("203.0.113.10", 1000)),
            TestClient(api_server.app, client=("203.0.113.10", 1001)),
            TestClient(api_server.app, client=("203.0.113.11", 1000)))


def test_api_shared_ip_five_six_other_ip_and_validation(clients):
    first, same_ip, other = clients
    assert first.post("/api/chat", json={"message": ""}).status_code == 400
    assert first.post("/api/chat", json={"message": "a" * 8001}).status_code == 422
    for index in range(5):
        response = (first if index % 2 else same_ip).post("/api/chat", json={"messages": [{"role": "user", "content": f"Follow-up {index}"}], "answerMode": "brief"})
        assert response.status_code == 200
        assert response.json()["answer_mode"] == "brief"
    assert first.get("/api/chat/access").json()["remaining"] == 0
    response = same_ip.post("/api/chat", json={"message": "Sixth"}, headers={"X-Forwarded-For": "203.0.113.99"})
    assert response.status_code == 429  # Application never reads untrusted forwarded headers.
    assert response.json()["detail"]["code"] == "chat_quota_exhausted"
    assert other.post("/api/chat", json={"message": "First"}).status_code == 200


def test_api_generation_failures_release_all_reservations(clients, monkeypatch):
    client = clients[0]
    for exception in (ValueError("invalid context"), RuntimeError("generation failed")):
        def fail(*args, **kwargs):
            raise exception
        monkeypatch.setattr(api_server.responder, "answer", fail)
        with TestClient(api_server.app, client=("203.0.113.10", 1000), raise_server_exceptions=False) as failing:
            assert failing.post("/api/chat", json={"message": "Question"}).status_code == (400 if isinstance(exception, ValueError) else 500)
        assert client.get("/api/chat/access").json()["remaining"] == 5


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("failure", ["RuntimeError", "APIConnectionError", "empty_final"])
def test_caught_provider_failure_releases_quota_then_success_charges_once(tmp_path, monkeypatch, provider, failure):
    import httpx
    from openai import APIConnectionError
    from test_chat_evidence import owner, scripted_provider, candidate_detail_calls
    chat = owner(tmp_path)
    monkeypatch.setattr(api_server, "responder", chat.responder)
    def unavailable(index, request):
        if failure == "empty_final":
            if index == 0:
                return [("search_knowledge", {"query": "insurance pricing", "target": "wiki"})]
            if index == 1:
                return candidate_detail_calls(request)
            return ""
        if failure == "APIConnectionError":
            raise APIConnectionError(request=httpx.Request("POST", "https://example.org/mock"))
        raise RuntimeError("mock provider outage")
    requests = []
    client = TestClient(api_server.app, client=("203.0.113.20", 1000))
    for _ in range(6):
        turn_requests = scripted_provider(chat, monkeypatch, provider, unavailable)
        response = client.post("/api/chat", json={"message": "Explain insurance pricing"})
        requests.extend(turn_requests)
        assert response.status_code == 200
        assert "generation_failed" not in response.json()
        assert "incomplete" in response.json()["text"]
        assert ("empty final response" if failure == "empty_final" else "Model unavailable") in response.json()["text"]
        if failure == "empty_final":
            assert response.json()["sources"]  # Every failed turn reached the real source-read branch.
        assert client.get("/api/chat/access").json()["remaining"] == 5
    assert len(requests) == (30 if failure == "empty_final" else 12)  # Existing internal calls remain one submission.

    def success(index, request):
        if index == 0:
            return [("search_knowledge", {"query": "insurance pricing", "target": "wiki"})]
        if index == 1:
            return candidate_detail_calls(request)
        return "Climate hazards affect insurance pricing. [1]"
    scripted_provider(chat, monkeypatch, provider, success)
    response = client.post("/api/chat", json={"message": "Explain insurance pricing"})
    assert response.status_code == 200 and response.json()["agent_mode"] == provider
    assert "generation_failed" not in response.json()
    assert client.get("/api/chat/access").json()["remaining"] == 4

    chat.responder.client = chat.responder.anthropic_client = None
    monkeypatch.setenv("CLIMATE_CHAT_PROVIDER", "")
    response = client.post("/api/chat", json={"message": "What is parametric insurance?"})
    assert response.status_code == 200 and "generation_failed" not in response.json()
    assert client.get("/api/chat/access").json()["remaining"] == 3
    response = client.post("/api/chat", json={"message": "List recent insurance articles"})
    assert response.status_code == 200 and response.json()["agent_mode"] == "offline"
    assert "generation_failed" not in response.json()
    assert client.get("/api/chat/access").json()["remaining"] == 2


def test_api_excess_concurrent_requests_rejected_before_generation(clients, monkeypatch):
    import threading
    from concurrent.futures import wait
    entered = threading.Event()
    release = threading.Event()
    count = 0
    lock = threading.Lock()
    def answer(*args, **kwargs):
        nonlocal count
        with lock:
            count += 1
            if count == 5:
                entered.set()
        assert release.wait(10)
        return {"text": "Answer", "sources": []}
    monkeypatch.setattr(api_server.responder, "answer", answer)
    # Initialize once before the concurrent requests, just as the app does.
    clients[0].get("/api/chat/access")
    with ThreadPoolExecutor(max_workers=6) as pool:
        requests = [pool.submit(clients[0].post, "/api/chat", json={"message": "Question"}) for _ in range(5)]
        try:
            assert entered.wait(10)
            assert clients[0].post("/api/chat", json={"message": "Excess"}).status_code == 429
            assert count == 5
        finally:
            release.set()
        wait(requests)
    assert all(request.result().status_code == 200 for request in requests)
    assert clients[0].get("/api/chat/access").json()["remaining"] == 0


@pytest.mark.parametrize("provider", ["openai", "anthropic", "offline", "source-only"])
@pytest.mark.parametrize("mode", ["brief", "detailed", "executive"])
def test_token_bypass_preserves_answer_and_provider_contract(clients, monkeypatch, provider, mode):
    store = api_server._chat_access()
    for _ in range(5):
        store.finish(store.reserve("203.0.113.10"), completed=True)
    token = store.create_token("Provider contract")["token"]
    expected = {"text": "Answer [1]", "sources": [{"url": "https://example.org", "index": 1}],
                "agent_mode": provider, "answer_mode": mode, "context": "opaque-frame", "tool_trace": ["internal tool"]}
    observed = []
    def answer(question, **kwargs):
        observed.append((question, kwargs))
        return expected
    monkeypatch.setattr(api_server.responder, "answer", answer)
    response = clients[0].post("/api/chat", headers={"Authorization": "Bearer " + token}, json={
        "message": "Follow-up", "messages": [{"role": "assistant", "content": "Prior answer", "context": "opaque-frame"}],
        "contextPath": "wiki/example.md", "answerMode": mode,
    })
    assert response.status_code == 200 and response.json() == expected
    assert observed == [("Follow-up", {"history": [{"role": "assistant", "content": "Prior answer", "context": "opaque-frame"}],
                                      "context_path": "wiki/example.md", "language": "en", "answer_mode": mode, "context": "opaque-frame"})]
    assert store.status("203.0.113.10")["remaining"] == 0


def test_tokens_admin_boundary_secret_once_browser_refresh_revocation(clients):
    client, refreshed, _ = clients
    assert client.post("/api/chat/access", json={"token": ""}).status_code == 422
    assert client.post("/api/chat/access", json={"token": "a" * 257}).status_code == 422
    assert client.get("/api/manage/chat-tokens").status_code == 401
    assert client.post("/api/manage/chat-tokens", json={"label": "Team"}).status_code == 401
    assert client.delete("/api/manage/chat-tokens/anything").status_code == 401
    api_server.app.dependency_overrides[api_server.current_console_user] = lambda: api_server.ConsoleUser(id="operator", email="admin", hashed_password="unused")
    try:
        assert client.post("/api/manage/chat-tokens", json={"label": " "}).status_code == 400
        token = client.post("/api/manage/chat-tokens", json={"label": "Team"}).json()
        listed = client.get("/api/manage/chat-tokens").json()
        assert set(listed[0]) == {"id", "label", "created_at", "status"}
        assert token["token"] not in str(listed)
    finally:
        api_server.app.dependency_overrides.clear()
    for _ in range(5):
        assert client.post("/api/chat", json={"message": "Free"}).status_code == 200
    assert client.post("/api/chat/access", json={"token": "wrong"}).json()["detail"] == INVALID_TOKEN
    enabled = client.post("/api/chat/access", json={"token": token["token"]})
    assert enabled.json()["access_enabled"] is True
    assert "HttpOnly" in enabled.headers["set-cookie"]
    refreshed.cookies.update(client.cookies)
    # Browser restart and service restart retain access; the cookie is Chat-only.
    api_server.chat_access_store = None
    assert refreshed.get("/api/chat/access").json()["access_enabled"] is True
    for mode in ("brief", "detailed", "executive"):
        assert refreshed.post("/api/chat", json={"message": "Token question", "answerMode": mode}).status_code == 200
    assert refreshed.get("/api/manage/chat-tokens").status_code == 401
    assert refreshed.get("/api/manage/session").json()["authenticated"] is False
    direct = TestClient(api_server.app)
    assert direct.post("/api/chat", headers={"Authorization": "Bearer " + token["token"]}, json={"message": "Direct API"}).status_code == 200
    api_server.app.dependency_overrides[api_server.current_console_user] = lambda: api_server.ConsoleUser(id="operator", email="admin", hashed_password="unused")
    try:
        assert client.delete("/api/manage/chat-tokens/" + token["id"]).status_code == 200
        assert client.get("/api/manage/chat-tokens").json()[0]["status"] == "revoked"
    finally:
        api_server.app.dependency_overrides.clear()
    assert refreshed.post("/api/chat", json={"message": "Revoked"}).json()["detail"] == INVALID_TOKEN
    assert refreshed.get("/api/chat/access").json()["invalid_token"] is True
    assert direct.post("/api/chat", headers={"Authorization": "Bearer " + token["token"]}, json={"message": "Revoked"}).status_code == 401
    store = api_server._chat_access()
    with sqlite3.connect(store.path) as connection:
        assert token["token"] not in str(connection.execute("SELECT * FROM tokens").fetchall())
        assert refreshed.cookies.get(CHAT_COOKIE) not in str(connection.execute("SELECT * FROM sessions").fetchall())
