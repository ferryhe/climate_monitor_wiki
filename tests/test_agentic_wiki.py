from agentic_wiki import AgenticWikiResponder, WikiKnowledgeBase
from api_server import app, responder
from fastapi.testclient import TestClient


def test_wiki_index_loads_documents_and_chunks():
    kb = WikiKnowledgeBase()

    assert kb.stats()["documents"] >= 30
    assert kb.stats()["source_documents"] >= 10
    assert kb.stats()["concepts"] >= 10
    assert kb.stats()["chunks"] >= kb.stats()["documents"]
    assert any(doc["path"] == "wiki/index.md" for doc in kb.document_catalog())
    assert any(concept["label"] == "Parametric Insurance" for concept in kb.concept_catalog())


def test_context_path_prioritizes_active_note():
    responder_instance = AgenticWikiResponder()
    responder_instance.client = None

    result = responder_instance.answer(
        "What is this page mainly about?",
        context_path="wiki/parametric-insurance.md",
        language="en",
        answer_mode="brief",
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
            "answerMode": "brief",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sources"]
    assert payload["sources"][0]["path"] == "wiki/parametric-insurance.md"


def test_api_config_exposes_graph_and_dataview_fields():
    client = TestClient(app)

    response = client.get("/api/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["wiki"]["documents"] >= 30
    assert payload["wiki"]["source_documents"] >= 10
    assert payload["wiki"]["concepts"] >= 10
    assert payload["documents"]
    assert payload["concepts"]
    assert payload["default_answer_mode"] == "detailed"
    assert payload["answer_modes"] == ["brief", "detailed"]

    index_doc = next(doc for doc in payload["documents"] if doc["path"] == "wiki/index.md")
    assert index_doc["title"] == "index"
    assert index_doc["type"] == "index"
    assert isinstance(index_doc["links"], list)
    assert isinstance(index_doc["words"], int)
    assert isinstance(index_doc["concepts"], list)

    parametric_doc = next(doc for doc in payload["documents"] if doc["path"] == "wiki/parametric-insurance.md")
    assert any(concept["label"] == "Parametric Insurance" for concept in parametric_doc["concepts"])
    assert any(concept["label"] == "IAIS" for concept in payload["concepts"])


def test_showcase_root_contains_chat_and_obsidian_workspace():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert 'id="chatView"' in body
    assert 'id="obsidianView"' in body
    assert 'id="messageInput"' in body
    assert 'id="graphSvg"' in body
    assert 'id="rows"' in body
    assert 'data-answer-mode="detailed"' in body
    assert 'data-graph-mode="keywords"' in body


def test_detailed_mode_brings_in_raw_source_evidence():
    responder_instance = AgenticWikiResponder()
    responder_instance.client = None

    result = responder_instance.answer(
        "Summarize the key updates in detail.",
        context_path="wiki/climate-monitor-2026-04-04.md",
        language="en",
        answer_mode="detailed",
    )

    assert result["answer_mode"] == "detailed"
    assert any(source["path"] == "sources/climate-monitor-2026-04-04.md" for source in result["sources"])
    assert result["retrieval_summary"]["source_hits"] >= 1


def test_detailed_mode_is_richer_than_brief_mode_offline():
    responder_instance = AgenticWikiResponder()
    responder_instance.client = None

    brief = responder_instance.answer(
        "What are the latest Climate Monitor highlights?",
        language="en",
        answer_mode="brief",
    )
    detailed = responder_instance.answer(
        "What are the latest Climate Monitor highlights?",
        language="en",
        answer_mode="detailed",
    )

    assert brief["answer_mode"] == "brief"
    assert detailed["answer_mode"] == "detailed"
    assert len(detailed["text"]) > len(brief["text"])
    assert "Detailed evidence:" in detailed["text"]


def test_past_week_daily_summary_covers_requested_window_offline():
    responder_instance = AgenticWikiResponder()
    responder_instance.client = None

    result = responder_instance.answer(
        "帮我把过去7天的日报总结一下给我",
        language="en",
        answer_mode="detailed",
    )

    assert result["answer_mode"] == "detailed"
    assert "Coverage window: 2026-04-14 to 2026-04-20" in result["text"]
    assert "- 2026-04-19:" in result["text"]

    source_paths = [source["path"] for source in result["sources"][:7]]
    assert source_paths == [
        "wiki/climate-monitor-2026-04-14.md",
        "wiki/climate-monitor-2026-04-15.md",
        "wiki/climate-monitor-2026-04-16.md",
        "wiki/climate-monitor-2026-04-17.md",
        "wiki/climate-monitor-2026-04-18.md",
        "wiki/climate-monitor-2026-04-19.md",
        "wiki/climate-monitor-2026-04-20.md",
    ]
