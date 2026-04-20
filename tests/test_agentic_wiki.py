from agentic_wiki import AgenticWikiResponder, WikiKnowledgeBase
from api_server import app, responder
from fastapi.testclient import TestClient


def test_wiki_index_loads_documents_and_chunks():
    kb = WikiKnowledgeBase()

    assert kb.stats()["documents"] >= 30
    assert kb.stats()["chunks"] >= kb.stats()["documents"]
    assert any(doc["path"] == "wiki/index.md" for doc in kb.document_catalog())


def test_context_path_prioritizes_active_note():
    result = AgenticWikiResponder().answer(
        "What is this page mainly about?",
        context_path="wiki/parametric-insurance.md",
        language="en",
    )

    assert result["sources"]
    assert result["sources"][0]["path"] == "wiki/parametric-insurance.md"


def test_api_chat_prioritizes_context_path_source():
    responder.client = None
    client = TestClient(app)

    response = client.post(
        "/api/chat",
        json={
            "message": "What is this page mainly about?",
            "contextPath": "wiki/parametric-insurance.md",
            "language": "en",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sources"]
    assert payload["sources"][0]["path"] == "wiki/parametric-insurance.md"
