from datetime import date, timedelta

from agentic_wiki import AgenticWikiResponder, WikiKnowledgeBase
from agentic_wiki.wiki_agent import _expand_query, _requested_dates, _strip_markdown
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


def test_strip_markdown_removes_report_break_tags():
    text = _strip_markdown("**URL:** https://example.com/report.pdf <br>\n**Title:** Foo <br />")

    assert "<br" not in text.lower()
    assert text == "URL: https://example.com/report.pdf Title: Foo"


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
    assert [item["label"] for item in payload["prompt_starters"]] == [
        "Last 4 weeks",
        "Last 12 weeks",
        "Insurer implications",
        "Pricing explainer",
        "Latest report",
    ]
    assert all("daily" not in item["description"].lower() for item in payload["prompt_starters"])
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


def test_robots_txt_disallows_crawling():
    client = TestClient(app)

    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "User-agent: *\nDisallow: /\n"


def test_showcase_app_exposes_mode_aware_prompt_starters():
    client = TestClient(app)

    response = client.get("/showcase/app.js")

    assert response.status_code == 200
    body = response.text
    assert "DEFAULT_PROMPT_STARTERS" in body
    assert "prompt_starters" in body
    assert "data-answer-mode" in body
    assert "Summarize the past 4 weeks by theme" in body
    assert "Summarize the latest Climate Monitor report in five bullets" in body
    assert "Summarize the past 14 days by theme" not in body
    for starter in responder.config()["prompt_starters"]:
        for value in starter.values():
            assert value in body


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


def test_latest_alias_uses_runtime_corpus_date_without_stale_daily_bias():
    expanded = _expand_query("What is the latest update today?", "2026-08-10")

    assert "2026-08-10" in expanded
    assert "2026-04-20" not in expanded
    assert "daily report" not in expanded.lower()


def test_latest_answer_does_not_cite_the_stale_hardcoded_date():
    responder_instance = AgenticWikiResponder()
    responder_instance.client = None

    result = responder_instance.answer(
        "What are the latest Climate Monitor highlights?",
        language="en",
        answer_mode="brief",
    )

    source_dates = {source["date"] for source in result["sources"] if source["date"]}
    assert responder_instance.kb.latest_date in source_dates
    assert "2026-04-20" not in source_dates


def test_requested_dates_supports_english_month_and_range_phrases():
    assert _requested_dates("Give me a report for this month", "2026-04-22")[0] == "2026-04-01"
    assert _requested_dates("Give me a report for this month", "2026-04-22")[-1] == "2026-04-22"
    assert _requested_dates("Summarize reports from 2026-04-14 to 2026-04-16", "2026-04-22") == [
        "2026-04-14",
        "2026-04-15",
        "2026-04-16",
    ]
    two_weeks = _requested_dates("Summarize the past 2 weeks of reports", "2026-08-10")
    assert len(two_weeks) == 14
    assert two_weeks[0] == "2026-07-28"
    assert two_weeks[-1] == "2026-08-10"


def test_executive_mode_produces_structured_window_brief_offline():
    responder_instance = AgenticWikiResponder()
    responder_instance.client = None

    result = responder_instance.answer(
        "Give me a report for April 2026.",
        language="en",
        answer_mode="executive",
    )

    assert result["answer_mode"] == "executive"
    assert "Executive Summary:" in result["text"]
    assert "Major Themes:" in result["text"]
    assert "Date Coverage:" in result["text"]
    assert "Report-by-Report Coverage:" in result["text"]
    assert "report(s) | dates:" in result["text"]
    assert "Summary:" in result["text"]
    assert "Coverage window: 2026-04-01 to 2026-04-30" in result["text"]
    assert any(source["path"] == "wiki/climate-monitor-2026-04-01.md" for source in result["sources"])
    assert any(source["path"].startswith("sources/") for source in result["sources"])


def test_past_week_report_summary_covers_requested_window_offline():
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
    # Under the weekly cadence the corpus no longer contains a report for every
    # calendar day, so requiring all 7 dates would assert on the ingest schedule
    # rather than on retrieval. The contract is: every report that EXISTS in the
    # requested window is covered, and the window's latest report is included.
    corpus_dates = {
        document.date
        for document in (
            *responder_instance.kb.documents,
            *getattr(responder_instance.kb, "source_documents", ()),
        )
        if getattr(document, "date", None)
    }
    expected_dates = {
        (window_start + timedelta(days=offset)).isoformat() for offset in range(7)
    } & corpus_dates
    assert expected_dates, "no reports in the requested window to cover"
    assert expected_dates.issubset(source_dates)
    assert source_dates.issubset(
        {
            (window_start + timedelta(days=offset)).isoformat()
            for offset in range(7)
        }
    )
    assert latest_date.isoformat() in source_dates


def test_past_two_weeks_summary_parses_weeks_and_scopes_sources_offline():
    responder_instance = AgenticWikiResponder()
    responder_instance.client = None
    latest_date_value = responder_instance.kb.latest_date
    assert latest_date_value is not None
    latest_date = date.fromisoformat(latest_date_value)
    window_start = latest_date - timedelta(days=13)
    window_dates = {
        (window_start + timedelta(days=offset)).isoformat() for offset in range(14)
    }

    result = responder_instance.answer(
        "Summarize the past 2 weeks of reports for me.",
        language="en",
        answer_mode="detailed",
    )

    assert result["answer_mode"] == "detailed"
    assert f"Coverage window: {window_start.isoformat()} to {latest_date.isoformat()}" in result["text"]

    source_dates = {source["date"] for source in result["sources"]}
    corpus_dates = {
        document.date
        for document in (
            *responder_instance.kb.documents,
            *getattr(responder_instance.kb, "source_documents", ()),
        )
        if getattr(document, "date", None)
    }
    expected_dates = window_dates & corpus_dates
    assert expected_dates, "no reports in the requested window to cover"
    assert expected_dates.issubset(source_dates)
    assert source_dates.issubset(window_dates)


def test_four_week_executive_summary_counts_reports_not_calendar_days():
    responder_instance = AgenticWikiResponder()
    responder_instance.client = None
    latest_date = date.fromisoformat(responder_instance.kb.latest_date)
    window_start = latest_date - timedelta(days=27)
    window_dates = {
        (window_start + timedelta(days=offset)).isoformat() for offset in range(28)
    }
    corpus_dates = {
        document.date
        for document in (
            *responder_instance.kb.documents,
            *responder_instance.kb.source_documents,
        )
        if document.date in window_dates
    }

    result = responder_instance.answer(
        "Summarize the past 4 weeks by theme and identify material changes across the weekly reports.",
        language="en",
        answer_mode="executive",
    )

    assert f"Reports with evidence: {len(corpus_dates)}" in result["text"]
    assert "Report-by-Report Coverage:" in result["text"]
    assert "Daily pages with evidence" not in result["text"]
    assert "Missing or no-report days" not in result["text"]
    for report_date in corpus_dates:
        assert f"- {report_date}:" in result["text"]


def test_timeline_distinguishes_retrieval_gap_from_missing_report(tmp_path):
    wiki_dir = tmp_path / "wiki"
    source_dir = tmp_path / "sources"
    wiki_dir.mkdir()
    source_dir.mkdir()
    (source_dir / "climate-monitor-2026-08-10.md").write_text("", encoding="utf-8")
    responder_instance = AgenticWikiResponder(wiki_dir=wiki_dir, source_dir=source_dir)
    responder_instance.client = None

    entry = responder_instance._timeline_entries(["2026-08-10"], [])[0]

    assert entry["summary"] == "No retrieved evidence was selected for this report date."
    assert entry["has_evidence"] is False
    assert "has_report" not in entry


def test_unknown_requested_date_keeps_relevant_evidence_offline():
    responder_instance = AgenticWikiResponder()
    responder_instance.client = None

    result = responder_instance.answer(
        "What are the main climate insurance themes on 2099-01-01?",
        language="en",
        answer_mode="detailed",
    )

    assert result["sources"]
    assert "could not find enough evidence" not in result["text"]
