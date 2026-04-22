from datetime import date, timedelta

from agentic_wiki import AgenticWikiResponder, WikiKnowledgeBase
from agentic_wiki.wiki_agent import _requested_dates
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
    assert payload["github_blob_base_url"].startswith("https://github.com/")
    assert payload["default_answer_mode"] == "detailed"
    assert payload["answer_modes"] == ["brief", "detailed", "executive"]
    assert payload["prompt_starters"]
    assert payload["prompt_starters"][0]["answer_mode"] == "executive"
    assert payload["prompt_starters"][0]["prompt"].startswith("Give me a report for this month")
    assert payload["graphs"]["notes"]["nodes"]
    assert payload["graphs"]["notes"]["links"]
    assert payload["graphs"]["keywords"]["nodes"]
    assert payload["graphs"]["keywords"]["links"]
    assert payload["graphs"]["keywords"]["static_layout"] is True

    index_doc = next(doc for doc in payload["documents"] if doc["path"] == "wiki/index.md")
    assert index_doc["title"] == "index"
    assert index_doc["type"] == "index"
    assert isinstance(index_doc["links"], list)
    assert isinstance(index_doc["words"], int)
    assert isinstance(index_doc["concepts"], list)

    parametric_doc = next(doc for doc in payload["documents"] if doc["path"] == "wiki/parametric-insurance.md")
    assert any(concept["label"] == "Parametric Insurance" for concept in parametric_doc["concepts"])
    assert any(concept["label"] == "IAIS" for concept in payload["concepts"])

    daily_doc = next(doc for doc in payload["documents"] if doc["path"] == "wiki/climate-monitor-2026-04-02.md")
    assert daily_doc["source_path"] == "sources/climate-monitor-2026-04-02.md"
    assert (
        daily_doc["source_url"]
        == "https://github.com/ferryhe/climate_monitor_wiki/blob/main/sources/climate-monitor-2026-04-02.md"
    )


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
    assert 'id="answerModeHint"' in body
    assert 'data-graph-mode="keywords"' in body
    assert body.index("Page Index") < body.index("Graph View")


def test_showcase_app_exposes_mode_aware_prompt_starters():
    client = TestClient(app)

    response = client.get("/showcase/app.js")

    assert response.status_code == 200
    body = response.text
    assert "DEFAULT_PROMPT_STARTERS" in body
    assert "prompt_starters" in body
    assert "data-answer-mode" in body
    assert "Give me a report for this month. Cover major themes, notable signals, and gaps." in body


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


def test_requested_dates_supports_english_month_and_range_phrases():
    assert _requested_dates("Give me a report for this month", "2026-04-22")[0] == "2026-04-01"
    assert _requested_dates("Give me a report for this month", "2026-04-22")[-1] == "2026-04-22"
    assert _requested_dates("Summarize reports from 2026-04-14 to 2026-04-16", "2026-04-22") == [
        "2026-04-14",
        "2026-04-15",
        "2026-04-16",
    ]


def test_executive_mode_produces_structured_window_brief_offline():
    responder_instance = AgenticWikiResponder()
    responder_instance.client = None
    latest_date_value = responder_instance.kb.latest_date
    assert latest_date_value is not None

    result = responder_instance.answer(
        "Give me a report for this month.",
        language="en",
        answer_mode="executive",
    )

    assert result["answer_mode"] == "executive"
    assert "Executive Summary:" in result["text"]
    assert "Major Themes:" in result["text"]
    assert "Date Coverage:" in result["text"]
    assert "Day-by-Day Coverage:" in result["text"]
    assert "day(s) | dates:" in result["text"]
    assert "Summary:" in result["text"]
    assert f"Coverage window: 2026-04-01 to {latest_date_value}" in result["text"]
    assert any(source["path"] == "wiki/climate-monitor-2026-04-01.md" for source in result["sources"])
    assert any(source["path"].startswith("sources/") for source in result["sources"])


def test_past_week_daily_summary_covers_requested_window_offline():
    responder_instance = AgenticWikiResponder()
    responder_instance.client = None
    latest_date_value = responder_instance.kb.latest_date
    assert latest_date_value is not None
    latest_date = date.fromisoformat(latest_date_value)
    window_start = latest_date - timedelta(days=6)

    result = responder_instance.answer(
        "Summarize the past 7 days of reports for me.",
        language="en",
        answer_mode="detailed",
    )

    assert result["answer_mode"] == "detailed"
    assert f"Coverage window: {window_start.isoformat()} to {latest_date.isoformat()}" in result["text"]
    assert f"- {latest_date.isoformat()}:" in result["text"]

    source_dates = {source["date"] for source in result["sources"]}
    expected_dates = {
        (window_start + timedelta(days=offset)).isoformat()
        for offset in range(7)
    }
    assert expected_dates.issubset(source_dates)
