"""The Chat contract uses approved identities and actual reader evidence."""
import copy
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api_server
from agentic_wiki.chat_evidence import ChatEvidence, EvidenceTurn, MAX_TOOLS, window
from agentic_wiki.wiki_agent import AgenticWikiResponder, PROMPT_STARTERS
from climate_registry.acquisition_review import record_knowledge
from climate_registry.publication import _approve, export_public_snapshot, stage_entities
from test_registry_publication import _database, _reader


@pytest.fixture
def mock_reader_policy(monkeypatch):
    """Pair controlled fetch results with a policy seam; no live policy claim."""
    from agentic_wiki import chat_evidence
    class ValidationError(ValueError):
        pass
    monkeypatch.setattr(chat_evidence, "_load_url_policy", lambda: (lambda url: url, ValidationError))


def owner(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "risk.md").write_text("# Insurance\nClimate hazards affect insurance pricing. https://example.org/evidence")
    responder = AgenticWikiResponder(wiki, tmp_path / "sources")
    responder.client = None
    return responder.chat_evidence


def scripted_provider(chat, monkeypatch, provider, script, *, check_drafts=False, script_controls_plan=False):
    requests = []
    script_index = 0
    plan_sent, finish_sent, final_answer, feedback_seen = False, False, None, False
    class Client:
        def with_options(self, **kwargs):
            assert kwargs["max_retries"] == 0 and kwargs["timeout"] <= 20
            return self
        def create(self, **kwargs):
            nonlocal script_index, plan_sent, finish_sent, final_answer, feedback_seen
            requests.append(copy.deepcopy(kwargs))
            offered = [tool.get("function", tool) for tool in kwargs.get("tools", [])]
            if not script_controls_plan and len(offered) == 1 and offered[0]["name"] == "research_state" and (offered[0].get("parameters") or offered[0].get("input_schema"))["properties"]["action"]["enum"] == ["plan"]:
                plan_sent, finish_sent, final_answer, feedback_seen = False, False, None, False
            last = kwargs["messages"][-1]["content"]
            feedback_seen |= isinstance(last, str) and last.startswith("Evidence sufficiency check")
            if not script_controls_plan and not plan_sent:
                plan_sent = True
                value = [("research_state", {"action": "plan", "time_basis": "retained test evidence", "required_outputs": [
                    {"id": "answer", "task": "answer the supplied question", "requires_current_body": False}]})]
            elif finish_sent:
                value = final_answer
            elif not check_drafts and isinstance(last, str) and last.startswith("Evidence sufficiency check"):
                prior = next(message["content"] for message in reversed(kwargs["messages"][:-1]) if message["role"] == "assistant")
                value = prior if isinstance(prior, str) else "\n".join(block["text"] for block in prior if block["type"] == "text")
            else:
                value = script(script_index, kwargs)
                script_index += 1
            if not script_controls_plan and not finish_sent and isinstance(value, str) and feedback_seen and kwargs.get("tools"):
                states = []
                for message in kwargs["messages"]:
                    content = message.get("content")
                    blocks = content if isinstance(content, list) else [{"content": content}]
                    for block in blocks:
                        text = block.get("text") or block.get("content")
                        if not isinstance(text, str): continue
                        if not text.startswith("Shared remaining budget:"):
                            try: text = json.loads(text).get("runtime_state", "")
                            except (ValueError, AttributeError): continue
                        if text.startswith("Shared remaining budget:"):
                            states.append(json.loads(text.split("\n", 1)[1]))
                ids = [row["evidence_id"] for row in states[-1]["canonical_citations"]]
                final_answer, finish_sent = value, True
                value = [("research_state", {"action": "finish", "results": [{"id": "answer", "status": "supported" if ids else "gap",
                    "evidence_ids": ids, **({} if ids else {"gap": "No usable evidence was supplied."})}]})]
            blocks = [{"type": "text", "text": value}] if isinstance(value, str) else [
                {"type": "tool_use", "id": f"call-{len(requests)}-{index}", "name": name, "input": arguments}
                for index, (name, arguments) in enumerate(value)]
            if provider == "anthropic":
                return SimpleNamespace(model_dump=lambda: {"content": blocks, "stop_reason": "end_turn" if isinstance(value, str) else "tool_use"})
            calls = [SimpleNamespace(id=block["id"], function=SimpleNamespace(name=block["name"], arguments=json.dumps(block["input"])))
                for block in blocks if block["type"] == "tool_use"]
            message = SimpleNamespace(content=value if isinstance(value, str) else None, tool_calls=calls,
                model_dump=lambda **kwargs: {"role": "assistant", "tool_calls": [
                    {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}} for call in calls]})
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop" if isinstance(value, str) else "tool_calls")])
    client = Client()
    client.messages = client
    client.chat = SimpleNamespace(completions=client)
    if monkeypatch:
        monkeypatch.setenv("CLIMATE_CHAT_PROVIDER", provider)
    if provider == "anthropic":
        chat.responder.anthropic_client, chat.responder.anthropic_model = client, "mock-claude"
    else:
        chat.responder.client = client
    return requests


def candidate_detail_calls(request):
    """Choose detail tools using the candidates actually returned to the model."""
    results = []
    for message in request["messages"]:
        if message["role"] == "tool":
            results.append(json.loads(message["content"]))
        elif isinstance(message.get("content"), list):
            results.extend(json.loads(block["content"]) for block in message["content"] if block["type"] == "tool_result")
    entries = next(result["evidence"] for result in reversed(results) if result.get("status") == "candidates")
    return [("get_meeting_details" if entry["kind"] == "meeting" else "get_source_details",
        {"event_id" if entry["kind"] == "meeting" else "evidence_id": entry["source"]["evidence_id"]}) for entry in entries]


def feedback_text(requests):
    """Find the actual draft-check user message across both native message shapes."""
    return next(message["content"] for request in requests for message in request["messages"]
        if message["role"] == "user" and isinstance(message.get("content"), str)
        and message["content"].startswith("Evidence sufficiency check"))


def evidence_trace(result):
    """Keep existing evidence assertions separate from the newly tested plan/finish calls."""
    trace = result["tool_execution"]
    if result.get("agent_mode") in {"openai", "anthropic"}:
        assert any(row["tool"] == "research_state" and row["status"] == "planned" for row in trace)
    return [row for row in trace if row["tool"] != "research_state"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_research_handoff_first_request_only_plan_and_text_cannot_skip_it(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    def script(index, request):
        tools = [tool.get("function", tool) for tool in request.get("tools", [])]
        if index < 3:
            assert [tool["name"] for tool in tools] == ["research_state"]
            schema = tools[0].get("parameters", tools[0].get("input_schema"))
            assert schema["properties"]["action"]["enum"] == ["plan"]
            assert "tool_choice" not in request
            if index < 2:
                return "A premature answer without a task plan."
            return [("research_state", {"action": "plan", "time_basis": "approved retained findings", "required_outputs": [
                {"id": "finding", "task": "climate hazard finding", "requires_current_body": False}]})]
        if index == 3:
            assert "search_knowledge" in [tool["name"] for tool in tools]
            return [("search_knowledge", {"query": "climate hazards", "target": "wiki"})]
        if index == 4:
            return candidate_detail_calls(request)
        if index == 5:
            key = next(iter(chat.responder.kb.chunks)).id
            return [("research_state", {"action": "finish", "results": [{"id": "finding", "status": "supported", "evidence_ids": [key]}]})]
        assert tools  # An accepted evidence handoff leaves tools available for the one answer check.
        return "Climate hazards affect pricing [[cite:" + chat.responder.kb.chunks[0].id + "]]."
    requests = scripted_provider(chat, monkeypatch, provider, script, check_drafts=True, script_controls_plan=True)
    result = chat.answer("Explain climate hazards")
    assert len(requests) == 8 and result["text"] == "Climate hazards affect pricing [1]."
    assert [row["tool"] for row in result["tool_execution"]] == ["research_state", "search_knowledge", "get_source_details", "research_state"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_research_handoff_pending_current_support_is_per_task_and_sent_at_limit(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    def script(index, request):
        assert not request.get("tools")
        serialized = json.dumps(request)
        assert "research_pending" in serialized and "current_support" in serialized
        assert "https://example.org/current-rule" in serialized
        assert "approved Registry's first-ingested" in serialized
        return "Current rule date is unknown; verify the official source. This answer is partial."
    scripted_provider(chat, monkeypatch, provider, script, script_controls_plan=True)
    turn = EvidenceTurn(chat, "Explain reports added in the last 14 days and the current rule date", None, [])
    turn.now = datetime(2026, 10, 10, 1, tzinfo=timezone.utc)
    source = turn.add({"evidence_id": "rule", "title": "Rule", "source_urls": ["https://example.org/current-rule"]}, kind="wiki", text="Retained rule date.")
    turn.add({"evidence_id": "other", "url": "https://example.org/other", "body": "Another freshly read page."}, kind="web", text="Another freshly read page.")
    turn.fresh_web_ids.add("other")
    turn.execute("research_state", {"action": "plan", "time_basis": "canonical knowledge window plus current rule", "required_outputs": [
        {"id": "rule", "task": "current rule date", "requires_current_body": True}]})
    turn.calls = MAX_TOOLS - 1
    pending = turn.execute("research_state", {"action": "finish", "results": [{"id": "rule", "status": "supported", "evidence_ids": ["rule"], "pending_targets": ["https://example.org/current-rule"]}]})
    assert pending["status"] == "research_pending"
    handoff = turn.research_handoff()
    assert handoff["tasks"][0]["current_support"] == [] and handoff["tasks"][0]["retained_only"] == ["rule"]
    assert handoff["tasks"][0]["unattempted_current_targets"] == ["https://example.org/current-rule"]
    assert handoff["last_validation"]["status"] == "research_pending"
    assert "partial" in turn.model_answer().lower()


def test_research_handoff_model_and_tool_limits_are_independent():
    from agentic_wiki.chat_evidence import MAX_MODELS, MAX_READS, MAX_TEXT, MAX_SECONDS
    assert (MAX_MODELS, MAX_TOOLS, MAX_READS, MAX_TEXT, MAX_SECONDS) == (14, 16, 4, 32000, 120)


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_task_directed_finish_accepts_shared_schema_fields_without_rewriting_plan(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    def script(index, request):
        if index == 0:
            schema = next(tool.get("function", tool) for tool in request["tools"]
                if tool.get("function", tool)["name"] == "research_state")
            fields = schema.get("parameters", schema.get("input_schema"))["properties"]
            assert "time_basis" in fields and "required_outputs" in fields and "results" in fields
            return [("research_state", {"action": "plan", "time_basis": "original chronology", "results": [],
                "required_outputs": [{"id": "date", "task": "approved chronology", "requires_current_body": False}]})]
        if index == 1:
            return [("research_state", {"action": "finish", "time_basis": "untrusted replacement", "required_outputs": [],
                "results": [{"id": "date", "status": "gap", "evidence_ids": [], "gap": "Approved chronology absent."}]})]
        serialized = json.dumps(request)
        assert "finish_accepted" in serialized and "original chronology" in serialized
        return "Approved chronology is unknown; check the approved Registry history."
    scripted_provider(chat, monkeypatch, provider, script, check_drafts=True, script_controls_plan=True)
    result = chat.answer("Articles added in the last 14 days")
    assert [row["status"] for row in result["tool_execution"]] == ["planned", "finish_accepted"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_task_directed_runtime_preserves_new_york_window_across_utc_midnight(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    def script(index, request):
        system = request.get("system") or request["messages"][0]["content"]
        serialized = json.dumps(request)
        assert "2026-10-10T01:00:00+00:00" in system
        assert '"new_york_date": "2026-10-09"' in system
        assert "America/New_York" in system
        assert "2026-09-26" in serialized and "2026-10-09" in serialized
        return "The approved knowledge window is September 26–October 9; chronology is unknown."
    scripted_provider(chat, monkeypatch, provider, script)
    turn = EvidenceTurn(chat, "Reports added in the last 14 days", None, [])
    turn.now = datetime(2026, 10, 10, 1, tzinfo=timezone.utc)
    assert "September 26" in turn.model_answer()
    assert turn.now.isoformat() == "2026-10-10T01:00:00+00:00"


def test_task_directed_online_article_candidates_are_distinct_but_aggregate_sections_remain(tmp_path, monkeypatch):
    from dataclasses import replace
    from agentic_wiki.wiki_agent import SearchHit
    chat = owner(tmp_path)
    chat.responder.client = object()
    seed = chat.responder.kb.chunks[0]
    chunks = [replace(seed, id=f"one:{i}", path="wiki/article-one.md", heading=f"Section {i}") for i in range(8)]
    chunks += [replace(seed, id=f"other:{i}", path=f"wiki/article-other-{i}.md", text=f"Distinct findings {i}") for i in range(5)]
    monkeypatch.setattr(chat.responder.kb, "search", lambda query, top_k, **kwargs: [SearchHit(chunk, 1, "matched") for chunk in chunks[:top_k]])
    turn = EvidenceTurn(chat, "Find climate articles", None, [])
    result = turn.search_knowledge("climate", target="wiki")
    assert len(result) == 6 and len({row["source"]["path"] for row in result}) == 6
    assert result[0]["source"]["evidence_id"] == "one:0"
    chunks[:] = [replace(seed, id=f"aggregate:{i}", path="wiki/climate-monitor-2026-10-05.md", heading=f"Article {i}") for i in range(6)]
    result = EvidenceTurn(chat, "Find climate articles", None, []).search_knowledge("climate", target="wiki")
    assert len(result) == 6 and len({row["source"]["evidence_id"] for row in result}) == 6
    chunks[:] = [replace(seed, id=f"weekly:{i}", path=f"wiki/climate-monitor-2026-09-{i+1:02d}.md", heading="IFRS webinar", text="Same approved summary") for i in range(4)]
    chunks += [replace(seed, id="updated", path="wiki/climate-monitor-2026-10-05.md", heading="IFRS webinar", text="Changed approved summary")]
    result = EvidenceTurn(chat, "Find climate articles", None, []).search_knowledge("climate", target="wiki")
    assert [row["source"]["evidence_id"] for row in result] == ["weekly:0", "updated"]


def test_task_directed_wiki_verification_uses_cited_passage_not_question_or_first_link():
    from agentic_wiki.chat_evidence import verification_guidance
    source = {"index": 2, "heading": "wiki", "path": "wiki/article-one.md", "url": "wiki/article-one.md",
        "title": "An article", "matched_section": "Findings", "source_urls": ["https://unrelated.example/first"]}
    guidance = verification_guidance("Explain energy transition developments for actuaries", [source], "A finding [2].")
    assert "Wiki passage [2]" in guidance and "original source links" in guidance
    assert "official source for Explain" not in guidance and "unrelated.example" not in guidance
    assert "publisher or regulator" in verification_guidance("Explain a missing topic", [])


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_task_directed_shared_manual_selects_entry_and_preserves_research_contracts(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    def script(index, request):
        system = request.get("system") or request["messages"][0]["content"]
        assert "Choose the entry that answers the task" in system
        assert "events or participation dates: meeting index" in system
        assert "known URL needing current facts: read_url directly" in system
        assert "reserve finish and synthesis" in system
        assert "non-window-qualified findings" in system
        assert "Audience is not the search subject" in system
        assert "citation_token" in system and "requires_current_body" in system
        return "Evidence is incomplete; check the relevant publisher's original source."
    scripted_provider(chat, monkeypatch, provider, script)
    result = chat.answer("Explain energy transition developments for insurers")
    assert result["text"].startswith("Evidence is incomplete; check the relevant publisher")


def test_wiki_first_online_discovery_excludes_archive_but_explicit_archive_remains(tmp_path):
    from dataclasses import replace
    chat = owner(tmp_path)
    kb = chat.responder.kb
    archive = replace(kb.chunks[0], id="archive:source:1", path="sources/archive.txt", corpus="source")
    kb.chunks.append(archive)
    chat.responder.client = object()
    turn = EvidenceTurn(chat, "Climate hazards", None, [])
    hits = turn.search_knowledge("climate hazards", target="wiki")
    assert hits and all(row["source"]["corpus"] == "wiki" for row in hits)
    assert any(hit.chunk.corpus == "source" for hit in kb.search("climate hazards"))
    explicit = turn.search_knowledge("climate hazards", "sources/archive.txt", target="wiki")
    assert any(row["source"]["corpus"] == "source" for row in explicit)
    assert any(row["content_kind"] == "immutable_report_archive" for row in explicit)


def test_wiki_first_meeting_index_matches_summary_topic_and_keeps_identity(tmp_path):
    chat = owner(tmp_path)
    records = [meeting("one", "Alpha conference", "Paris"), meeting("two", "Beta conference", "London")]
    records[0]["summary"] = "Flood risk and adaptation."
    records[1]["summary"] = "Energy transition grid investment."
    chat.reader_factory = lambda: MeetingsReader(records)
    chat.responder.client = object()
    turn = EvidenceTurn(chat, "Find energy transition events", None, [])
    result = turn.search_knowledge("energy transition", target="meetings")
    assert [row["source"]["evidence_id"] for row in result] == ["two"]
    assert result[0]["content_kind"] == "approved_event_index"
    assert result[0]["dates"]["event_start"] == records[1]["start_date"]
    assert not turn.sources


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_wiki_first_explicit_archive_corpus_parameter(tmp_path, monkeypatch, provider):
    from dataclasses import replace
    chat = owner(tmp_path)
    chat.responder.kb.chunks.append(replace(chat.responder.kb.chunks[0],
        id="archive:source:1", path="sources/archive.txt", corpus="source"))
    def script(index, request):
        tools = request.get("tools", [])
        schema = next((tool.get("function", tool) for tool in tools
            if tool.get("function", tool).get("name") == "search_knowledge"), None)
        if index == 0:
            parameters = schema.get("parameters", schema.get("input_schema"))
            assert parameters["properties"]["corpus"]["enum"] == ["wiki", "source"]
            return [("search_knowledge", {"query": "climate hazards", "target": "wiki", "corpus": "source"})]
        results = []
        for message in request["messages"]:
            if message["role"] == "tool": results.append(json.loads(message["content"]))
            elif isinstance(message.get("content"), list):
                results.extend(json.loads(block["content"]) for block in message["content"] if block["type"] == "tool_result")
        result = next(result for result in results if result.get("status") == "candidates")
        assert result["evidence"] and all(entry["content_kind"] == "immutable_report_archive" for entry in result["evidence"])
        return "Archive candidates found; read details to verify their findings."
    scripted_provider(chat, monkeypatch, provider, script)
    result = chat.answer("Find the original archived climate hazards report")
    assert evidence_trace(result)[0]["status"] == "candidates"
    turn = EvidenceTurn(chat, "Find an event", None, [])
    invalid = turn.execute("search_knowledge", {"query": "climate", "target": "meetings", "corpus": "source"})
    assert invalid["status"] == "invalid"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_wiki_first_empty_final_retries_synthesis_once_then_compact_failure(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    def script(index, request):
        if index == 0:
            return [("search_knowledge", {"query": "climate hazards", "target": "wiki"})]
        return ""
    requests = scripted_provider(chat, monkeypatch, provider, script, check_drafts=True)
    result = chat.answer("Explain climate hazards")
    assert len(requests) == 4
    assert not requests[-1].get("tools")
    assert "empty" in result["text"].lower() and "Manual verification:" in result["text"]
    assert len(result["text"]) < 2000


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_wiki_first_loaded_manual_and_current_budget_only(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    requests = scripted_provider(chat, monkeypatch, provider, lambda index, request:
        [("search_knowledge", {"query": "climate hazards", "target": "wiki"})] if index == 0 else
        "Missing current verification; check the official source.")
    chat.answer("Explain climate hazards")
    systems = []
    for request in requests:
        system = request.get("system") or request["messages"][0]["content"]
        systems.append(system)
        assert "Wiki is the knowledge foundation" in system and "subject, all requested outputs and time basis" in system
        assert "Shared remaining budget:" not in system
        assert json.dumps(request["messages"]).count("Shared remaining budget:") == 1
    assert len(set(systems)) == 1


def test_wiki_first_focus_returns_new_passage_for_same_id_with_shared_budget(tmp_path):
    chat = owner(tmp_path)
    document = tmp_path / "wiki" / "risk.md"
    document.write_text("# Risk\n\n" + "Flood adaptation finding. " * 160 + "\n\n" + "Battery storage finding. " * 160)
    chat.responder.kb.reload(); chat.responder.client = object()
    turn = EvidenceTurn(chat, "Explain climate risk for insurers", None, [])
    candidate = turn.search_knowledge("risk", target="wiki")[0]
    key = candidate["source"]["evidence_id"]
    first = turn.execute("get_source_details", {"evidence_id": key, "focus": "flood adaptation"})
    spent = turn.chars
    second = turn.execute("get_source_details", {"evidence_id": key, "focus": "battery storage"})
    assert "Battery storage finding" in second["text"] and second["text"] != first["text"]
    assert first["source"]["index"] == second["source"]["index"] and len(turn.sources) == 1
    assert turn.chars == spent + len(second["text"])


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("annotation_only", [False, True])
def test_wiki_first_search_candidates_keep_real_metadata_as_unread_hints(tmp_path, monkeypatch, provider, annotation_only):
    chat = owner(tmp_path); monkeypatch.setenv("CLIMATE_CHAT_PROVIDER", provider)
    requests = []
    class Client:
        def with_options(self, **kwargs): return self
        def create(self, **kwargs):
            requests.append(kwargs)
            row = {"url": "https://example.org/report", "title": "Battery storage evidence", "description": "Candidate summary only."}
            payload = {"content": [{"type": "web_search_tool_result", "content": [{"type": "web_search_result", **row}]}]} if provider == "anthropic" else {
                "output": [{"type": "web_search_call", "status": "completed", "action": {"sources": [row]}}]}
            if annotation_only:
                if provider == "anthropic":
                    payload = {"content": [{"type": "web_search_tool_result", "content": [{"type": "web_search_result", "url": row["url"]}]},
                        {"type": "text", "text": "Candidate", "citations": [{"type": "web_search_result_location", "url": row["url"], "title": row["title"], "cited_text": row["description"]}]}]}
                else:
                    payload = {"output": [{"type": "web_search_call", "status": "completed", "action": {"sources": [{"url": row["url"]}]}},
                        {"content": [{"annotations": [{"type": "url_citation", "url": row["url"], "title": row["title"], "cited_text": row["description"]}]}]}]}
            return SimpleNamespace(model_dump=lambda: payload)
    client = Client(); client.messages = client.responses = client
    if provider == "anthropic": chat.responder.anthropic_client = client
    else: chat.responder.client = client
    turn = EvidenceTurn(chat, "Recent battery storage developments", None, [])
    result = turn.execute("search_web", {"query": "battery storage"})
    candidate = result["candidates"][0]
    assert candidate["title"] == "Battery storage evidence" and candidate["preview"] == "Candidate summary only."
    assert candidate["read_status"] == "unread search hint" and not turn.sources and turn.reads == 0
    text = json.dumps(requests[0]); assert turn.now.isoformat() in text
    turn.model_data(result); assert turn.model_chars == len(candidate["preview"])


def test_wiki_first_aggregate_detail_keeps_matched_section_and_verification(tmp_path):
    chat = owner(tmp_path)
    document = tmp_path / "wiki" / "registry-meetings.md"
    document.write_text("# Meetings\n\nProvenance: PDF observed record; unchecked\n\n"
        "## Alpha conference\n\nAlpha conference concerns energy grids.\n\n"
        "## Beta conference\n\nBeta conference concerns flood damage.\n")
    chat.responder.kb.reload(); chat.responder.client = object()
    turn = EvidenceTurn(chat, "Explain Alpha conference", None, [])
    candidates = turn.search_knowledge("Alpha conference", target="wiki")
    hit = next(row for row in candidates if row["source"].get("matched_section") == "Alpha conference")
    detail = turn.get_source_details(hit["source"]["evidence_id"])
    assert "energy grids" in detail["text"] and "Beta conference" not in detail["text"]
    assert "unchecked" in detail["text"] and detail["source"]["matched_section"] == "Alpha conference"


def test_wiki_first_registered_text_is_not_deduplicated_against_unread_preview(tmp_path):
    turn = EvidenceTurn(owner(tmp_path), "Explain climate hazards", None, [])
    turn.source_only = False
    candidate = turn.search_knowledge("climate hazards", target="wiki")[0]
    turn.model_data(candidate)
    detail = turn.get_source_details(candidate["source"]["evidence_id"])
    transmitted = turn.model_data(detail)
    assert transmitted["text"] == detail["text"] and transmitted["source"]["index"] == 1
    assert turn.model_chars == len(candidate["preview"]) + len(detail["text"])


def test_canonical_citation_tokens_bind_ids_before_display_order(tmp_path):
    turn = EvidenceTurn(owner(tmp_path), "Explain findings", None, [])
    for key in ("first", "second", "third"):
        turn.add({"evidence_id": key, "title": key, "path": "wiki/risk.md", "corpus": "wiki"}, kind="wiki", text=key + " finding")
    catalog = turn.citation_catalog()
    assert catalog[2]["citation_label"] == "[3]" and catalog[2]["citation_token"] == "[[cite:third]]"
    assert catalog[2]["document_title"] == "Insurance"
    assert turn.final_text("Third [[cite:third]], first [[cite:first]].") == "Third [3], first [1]."
    assert "unread" not in turn.final_text("Unknown [[cite:unread]].")
    assert "Citation limitation" in turn.final_text("Unknown [[cite:unread]].")
    assert turn.final_text("Legacy [3].") == "Legacy [3]."


def test_research_state_requires_read_ids_and_preserves_real_chronology_gap(tmp_path):
    chat = owner(tmp_path); chat.responder.client = object()
    turn = EvidenceTurn(chat, "Explain findings and newly added items", None, [])
    candidate = turn.execute("search_knowledge", {"target": "wiki", "query": "climate hazards"})["evidence"][0]
    key = candidate["source"]["evidence_id"]
    plan = {"action": "plan", "time_basis": "approved knowledge time", "required_outputs": [
        {"id": "findings", "task": "Explain the findings", "requires_current_body": False},
        {"id": "newness", "task": "Confirm approved first-ingested time", "requires_current_body": False}]}
    assert turn.execute("research_state", plan)["status"] == "planned"
    finish = {"action": "finish", "results": [
        {"id": "findings", "status": "supported", "evidence_ids": [key]},
        {"id": "newness", "status": "gap", "evidence_ids": [], "gap": "Approved chronology is absent."}]}
    assert turn.execute("research_state", finish)["status"] == "research_pending"
    turn.execute("get_source_details", {"evidence_id": key})
    assert turn.execute("research_state", finish)["status"] == "finish_accepted"
    switched = EvidenceTurn(chat, "New unrelated topic", None, [])
    assert switched.research_plan is None


@pytest.mark.usefixtures("mock_reader_policy")
def test_research_state_current_excerpt_requires_body_but_failed_target_allows_gap(tmp_path, monkeypatch):
    chat = owner(tmp_path); chat.responder.client = object()
    turn = EvidenceTurn(chat, "What is current regulation?", None, [])
    turn.runtime_dir = tmp_path
    candidate = turn.execute("search_knowledge", {"target": "wiki", "query": "climate hazards"})["evidence"][0]
    key = candidate["source"]["evidence_id"]
    turn.execute("get_source_details", {"evidence_id": key})
    turn.execute("research_state", {"action": "plan", "time_basis": "current source body", "required_outputs": [
        {"id": "rule", "task": "Check applicable rule and effective date", "requires_current_body": True}]})
    result = turn.execute("research_state", {"action": "finish", "results": [
        {"id": "rule", "status": "supported", "evidence_ids": [key], "pending_targets": ["https://example.org/evidence"]}]})
    assert result["status"] == "research_pending"
    gap_finish = {"action": "finish", "results": [{"id": "rule", "status": "gap", "evidence_ids": [key], "pending_targets": ["https://example.org/evidence"], "gap": "Current rule date is unverified."}]}
    assert turn.execute("research_state", gap_finish)["status"] == "research_pending"
    from climate_monitor import article_content_adapter
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *args, **kwargs:
        {"status": "failed", "failure_reason": "robots.forbidden", "final_url": args[1]})
    assert turn.execute("read_url", {"url": "https://example.org/evidence"})["status"] != "read"
    result = turn.execute("research_state", gap_finish)
    assert result["status"] == "finish_accepted"


def test_research_state_discovery_zero_increment_returns_existing_unread_targets(tmp_path):
    chat = owner(tmp_path); chat.responder.client = object()
    turn = EvidenceTurn(chat, "Climate hazards", None, [])
    first = turn.execute("search_knowledge", {"target": "wiki", "query": "climate hazards"})
    second = turn.execute("search_knowledge", {"target": "wiki", "query": "hazards climate"})
    assert first["new_candidate_count"] > 0
    assert second["status"] == "no_new_candidates" and second["new_candidate_count"] == 0
    assert first["evidence"][0]["source"]["evidence_id"] in second["available_targets"]
    assert not turn.sources and turn.calls == 2


def test_research_state_last_tool_cannot_upgrade_unread_candidate(tmp_path):
    chat = owner(tmp_path); chat.responder.client = object()
    turn = EvidenceTurn(chat, "Explain findings", None, [])
    candidate = turn.execute("search_knowledge", {"target": "wiki", "query": "climate hazards"})["evidence"][0]
    plan = {"action": "plan", "time_basis": "retained findings", "required_outputs": [
        {"id": "facts", "task": "Read the findings", "requires_current_body": False}]}
    turn.execute("research_state", plan)
    turn.calls = MAX_TOOLS - 1
    result = turn.execute("research_state", {"action": "finish", "results": [
        {"id": "facts", "status": "supported", "evidence_ids": [candidate["source"]["evidence_id"]]}]})
    assert result["status"] == "research_pending" and "unread" in json.dumps(result).lower()
    honest = EvidenceTurn(chat, "Explain findings", None, [])
    honest.execute("research_state", plan); honest.calls = MAX_TOOLS - 1
    assert honest.execute("research_state", {"action": "finish", "results": [
        {"id": "facts", "status": "gap", "evidence_ids": [], "gap": "Tool allowance exhausted before reading relevant evidence."}]})["status"] == "finish_accepted"


@pytest.mark.usefixtures("mock_reader_policy")
def test_research_state_current_read_replaces_same_id_retained_body(tmp_path, monkeypatch):
    chat = owner(tmp_path); chat.responder.client = object()
    turn = EvidenceTurn(chat, "Check current registration", None, []); turn.runtime_dir = tmp_path
    url = "https://example.org/event"
    import hashlib
    key = "chat-" + hashlib.sha256(url.encode()).hexdigest()[:24]
    turn.web[url] = {"evidence_id": key, "title": url, "body": "Registration is open.", "content_hash": "old", "final_url": url, "truncated": False}
    turn.execute("read_url", {"url": url})
    from climate_monitor import article_content_adapter
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *args, **kwargs:
        {"status": "ok", "content": "Registration is closed.", "content_hash": "new", "final_url": url})
    fresh = turn.execute("read_url", {"url": url, "refresh": True})
    # Reader-generated stable URL ID is used for this same-page refresh.
    assert len(turn.evidence) == 1 and "Registration is closed." in turn.evidence[-1]["text"]
    assert fresh["source"]["version"] == "new" and fresh["source"]["evidence_id"] in turn.fresh_web_ids


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_research_state_provider_finish_pending_then_read_and_canonical_synthesis(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    def script(index, request):
        serialized = json.dumps(request["messages"])
        if index == 0:
            return [("research_state", {"action": "plan", "time_basis": "retained findings", "required_outputs": [
                {"id": "facts", "task": "Read findings", "requires_current_body": False}]})]
        if index == 1: return [("search_knowledge", {"target": "wiki", "query": "climate hazards"})]
        key = next(chunk.id for chunk in chat.responder.kb.chunks if chunk.corpus == "wiki")
        if index in (2, 4):
            return [("research_state", {"action": "finish", "results": [{"id": "facts", "status": "supported", "evidence_ids": [key]}]})]
        if index == 3:
            assert "research_pending" in serialized
            return candidate_detail_calls(request)
        assert request.get("tools") and "canonical_citations" in serialized
        return "Climate hazards affect pricing [[cite:" + key + "]]."
    requests = scripted_provider(chat, monkeypatch, provider, script, check_drafts=True, script_controls_plan=True)
    result = chat.answer("Explain climate hazards")
    assert result["text"] == "Climate hazards affect pricing [1]." and len(requests) == 7
    assert [row["tool"] for row in result["tool_execution"]] == ["research_state", "search_knowledge", "research_state", "get_source_details", "research_state"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("mode,limit", [("brief", 3000), ("detailed", 6000), ("executive", 6000)])
def test_quality_output_space_and_task_coverage(tmp_path, monkeypatch, provider, mode, limit):
    chat = owner(tmp_path)
    def script(index, request):
        assert request.get("max_tokens", request.get("max_completion_tokens")) == limit
        system = request.get("system") or request["messages"][0]["content"]
        assert "all requested" in system and "relevance" in system and "remaining budget" in json.dumps(request)
        return "Evidence is incomplete; check the official insurer source for current pricing, reserving and capital findings."
    scripted_provider(chat, monkeypatch, provider, script)
    result = chat.answer("Explain pricing, reserving and capital", answer_mode=mode)
    assert "participation" not in result["text"] and "event organizer" not in result["text"]


def test_quality_wiki_identity_compact_search_and_detail_expansion(tmp_path):
    chat = owner(tmp_path)
    document = tmp_path / "wiki" / "risk.md"
    document.write_text("# Insurance\n" + "Unrelated background. " * 200 +
        "\n\nMunich Re insurance losses reached 44 billion. [Munich Re](https://www.munichre.com/actual)\n" +
        "More background. " * 200)
    chat.responder.kb.reload()
    chat.responder.client = object()
    turn = EvidenceTurn(chat, "Explain Munich Re insurance losses", None, [])
    result = turn.search_knowledge("Munich Re insurance losses", target="wiki")
    assert result and sum(len(row["preview"]) for row in result) < 3000 and not turn.sources
    source = result[0]["source"]
    assert source["path"] == "wiki/risk.md"
    assert source["url"] == (chat.responder.base_source_url + "/" if chat.responder.base_source_url else "") + source["path"]
    assert "https://www.munichre.com/actual" in result[0]["preview"]
    detail = turn.get_source_details(source["evidence_id"])
    assert len(detail["text"]) <= 3000 and detail["source"]["truncated"]
    assert detail["source"]["evidence_id"] == source["evidence_id"]
    assert "44 billion" in detail["text"] and len(result[0]["preview"]) <= 300
    turn.question = "Give the full original text of this source"
    full = turn.get_source_details(source["evidence_id"])
    assert len(full["text"]) > 3000


def test_quality_final_check_preserves_approved_citations_and_hides_internal_corrections(tmp_path):
    turn = EvidenceTurn(owner(tmp_path), "Explain insurance pricing, reserving and capital", None, [])
    turn.add({"path": "wiki/risk.md", "title": "Approved insurance summary"}, kind="wiki", text="Loss uncertainty affects reserves and capital.")
    feedback = turn.final_feedback("Reserves and capital are affected [1].")
    assert "Approved excerpts" in feedback and "not a citation whitelist" in feedback
    assert "self-contained" in feedback and "internal" in feedback


def test_quality_metadata_details_expand_current_document_and_keep_identity(tmp_path):
    chat = owner(tmp_path)
    document = tmp_path / "wiki" / "battery.md"
    document.write_text("# Battery storage report\n\nCanonical article: https://example.org/battery\n\n"
        "## Verified findings\n\nBattery storage project delays increase construction exposures.\n\n" +
        "Unrelated navigation background. " * 160)
    chat.responder.kb.reload()
    chat.responder.client = object()
    turn = EvidenceTurn(chat, "Explain the battery storage report's key findings", None, [])
    candidates = turn.search_knowledge("Battery storage report", target="wiki")
    metadata = next(row for row in candidates if row["source"]["evidence_id"].endswith(":wiki:1"))
    detail = turn.get_source_details(metadata["source"]["evidence_id"])
    assert "delays increase construction exposures" in detail["text"]
    assert detail["source"]["evidence_id"] == metadata["source"]["evidence_id"]
    assert detail["source"]["path"] == "wiki/battery.md" and detail["source"]["truncated"]
    assert len(detail["text"]) <= 3000 and detail["source"]["heading"] == "wiki"
    document.write_text("# Battery storage report\n\n## Verified findings\n\nCurrent battery storage findings identify fire protection.")
    chat.responder.kb.reload()
    current_turn = EvidenceTurn(chat, turn.question, None, [])
    current_turn.search_knowledge("Battery storage report", target="wiki")
    current = current_turn.get_source_details(metadata["source"]["evidence_id"])
    assert "identify fire protection" in current["text"]
    document.unlink()
    chat.responder.kb.reload()
    assert turn.get_source_details(metadata["source"]["evidence_id"])["status"] == "unavailable"


def test_quality_discovery_description_uses_subject_not_audience(tmp_path, monkeypatch):
    chat = owner(tmp_path)
    def script(index, request):
        schema = next(row for row in request["tools"] if row["function"]["name"] == "search_knowledge")
        assert "short subject" in schema["function"]["description"]
        assert "audience" in schema["function"]["description"]
        return "No current page evidence; verify the official energy source."
    requests = scripted_provider(chat, monkeypatch, "openai", script)
    result = chat.answer("What recent energy transition developments matter to insurers?")
    assert result["agent_mode"] == "openai" and len(requests) == 4


@pytest.mark.parametrize("question", [
    "What climate and insurance content was recently added?",
    "What recently added climate and insurance content is available?",
    "What content was recently added?",
    "What content was recently updated?",
    "What recently updated climate insurance content is available?",
    "最近新增了哪些气候保险内容？",
    "最近更新了哪些气候保险内容？",
])
def test_offline_recent_content_uses_approved_knowledge_chronology(tmp_path, question):
    from datetime import timedelta
    from contextlib import nullcontext

    chat = owner(tmp_path)
    ingested = datetime.now(timezone.utc) - timedelta(hours=1)
    updated_query = bool(re.search(r"recently updated|最近更新", question, re.I))
    class Reader:
        calls = []
        def public_snapshot(self): return nullcontext()
        def knowledge_chronology(self):
            self.calls.append("knowledge_chronology")
            return {"article:actual": {"first_ingested_at": "2024-01-01T12:00:00Z" if updated_query else ingested.isoformat(),
                "substantive_updated_at": ingested.isoformat() if updated_query else None,
                "knowledge_id": "approved-knowledge-version"}}
        def articles(self, **kwargs):
            self.calls.append("articles")
            return {"items": [{"article_id": "actual", "title": "Climate insurance update",
                "summary": "Approved climate risk findings", "published_candidate_sha256": "approved-article-version"}],
                "pagination": {"pages": 1}}
        def pdf_articles_all(self): return []
        def article(self, article_id):
            self.calls.append("article")
            assert article_id == "actual"
            return {"article_id": "actual", "title": "Climate insurance update", "summary": "Approved climate risk findings",
                "publisher": "Approved source", "publication_date": "2026-08-01", "categories": ["climate risk"],
                "published_candidate_sha256": "approved-article-version", "canonical_url": "https://example.org/climate"}

    chat.reader_factory = Reader
    result = chat.answer(question)
    generic = chat.answer("What recent climate insurance developments matter?")

    assert Reader.calls == ["knowledge_chronology", "articles", "article"]
    assert len(result["sources"]) == 1 and result["sources"][0]["evidence_id"] == "actual"
    assert result["sources"][0]["version"] == "approved-article-version"
    if updated_query:
        assert "Material update: " + ingested.isoformat() in result["text"]
    else:
        assert "First added: " + ingested.isoformat() in result["text"]
    assert "[1]" in result["text"]
    assert not any(source["evidence_id"].endswith(":wiki:1") for source in result["sources"])
    assert any(source["evidence_id"].endswith(":wiki:1") for source in generic["sources"])


def test_quality_wiki_excerpt_keeps_findings_not_technical_provenance(tmp_path):
    chat = owner(tmp_path)
    document = tmp_path / "wiki" / "article-report.md"
    technical = "\n\n".join(f"Report citation: climate insurance report sources; SHA-256: {index:064x}" for index in range(70))
    findings = "Insurance protection gaps create financial stability risks across six case studies. " * 7
    document.write_text("# Insurance report\n\nCanonical article: https://example.org/report\n\n"
        "## Date observations\n\nPage information date: 2025-11-06; page date kind: published\n\n" + technical +
        "\n\n## Article semantic summary\n\n" + findings +
        "\n\nRegistry content version: content-actual\n\nProvenance: retained approved summary")
    chat.responder.kb.reload()
    chat.responder.client = object()
    turn = EvidenceTurn(chat, "Summarize climate insurance reports, their findings and sources", None, [])
    candidates = turn.search_knowledge("Insurance report", target="wiki")
    metadata = next(row for row in candidates if row["source"]["evidence_id"].endswith(":wiki:1"))
    detail = turn.get_source_details(metadata["source"]["evidence_id"])
    assert findings.strip() in detail["text"] and "2025-11-06" in detail["text"]
    assert "Report citation:" not in detail["text"] and "Provenance:" not in detail["text"]
    assert detail["source"]["version"] == "content-actual" and detail["source"]["truncated"]
    turn.question = "Give the full verbatim original source"
    full = turn.get_source_details(metadata["source"]["evidence_id"])
    assert "Report citation:" in full["text"] and "Provenance:" in full["text"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_quality_common_research_flow_scopes_event_and_knowledge_requirements(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    requests = scripted_provider(chat, monkeypatch, provider, lambda *args: "No verified current topic findings; check the official publisher page.")
    result = chat.answer("What recent energy transition developments affect insurers?")
    assert result["agent_mode"] == provider and len(requests) == 4
    system = requests[0].get("system") or requests[0]["messages"][0]["content"]
    event_clause = system.index("For requested events/participation,")
    assert "list multiple events" not in system[:event_clause]
    assert "Extract the subject" in system[:event_clause] and "check relevance" in system[:event_clause]
    assert system.count("current availability is unverified") == 1
    assert "short subject" in json.dumps(requests[1]) and "auxiliary index" in json.dumps(requests[1])
    feedback = feedback_text(requests)
    assert "Only when the user asks for added/material-update history" in feedback
    assert "ordinary recent/latest topics" in feedback


def test_quality_meeting_results_disclose_actual_scope_without_topic_claim(tmp_path):
    chat = owner(tmp_path)
    chat.reader_factory = lambda: MeetingsReader([meeting("one", "Alpha conference", "Paris"), meeting("two", "Beta conference", "London")])
    turn = EvidenceTurn(chat, "Energy transition findings for insurers", None, [])
    broad = turn.execute("search_knowledge", {"query": "energy transition climate risk insurance", "target": "meetings"})
    assert len(broad["evidence"]) == 2 and not turn.clarification
    assert any("general topic words are not matched" in note for note in broad["notes"])
    assert any('"name"' not in note and '"type"' not in note for note in broad["notes"] if note.startswith("Meeting query scope:"))
    selected = turn.execute("search_knowledge", {"query": "Alpha conference", "target": "meetings"})
    assert [row["source"]["evidence_id"] for row in selected["evidence"]] == ["one"]
    assert '"name": ["Alpha conference"]' in selected["notes"][-1] or any('"name": ["Alpha conference"]' in note for note in selected["notes"])


def test_quality_recent_window_uses_question_and_only_relevant_compact_candidates(tmp_path):
    from contextlib import nullcontext
    chat = owner(tmp_path)
    items = [{"article_id": str(i), "title": "Generic politics", "summary": "Unrelated politics " * 800} for i in range(9)]
    items.append({"article_id": "energy", "title": "Energy transition battery insurance", "summary": "Battery fire losses affect energy insurance. " * 300})
    class Reader:
        def public_snapshot(self): return nullcontext()
        def knowledge_chronology(self): return {}
        def articles(self, **kwargs): return {"items": items, "pagination": {"pages": 1}}
        def pdf_articles_all(self): return []
        def article(self, key): return next(item for item in items if item["article_id"] == key)
    chat.reader_factory = Reader
    chat.responder.client = object()
    turn = EvidenceTurn(chat, "List energy transition insurance articles added in the last 14 days", None, [])
    turn.now = datetime(2026, 10, 9, 16, tzinfo=timezone.utc)
    result = turn.search_knowledge("energy battery insurance from 2026-09-25 to 2026-10-09", target="recent_articles")
    assert [row["source"]["evidence_id"] for row in result] == ["energy"]
    assert len(result[0]["preview"]) < 1500 and not turn.sources
    assert any("2026-09-26" in note for note in turn.notes) and not any("2026-09-25" in note for note in turn.notes)
    assert any("10 scanned article/PDF record rows have unknown" in note and "not a unique-article count" in note for note in turn.notes)


def test_quality_model_evidence_omits_duplicate_body_and_snippet(tmp_path):
    turn = EvidenceTurn(owner(tmp_path), "Explain risks", None, [])
    body = "Actual body facts." * 1000
    first = turn.model_data({"body": body, "text": body, "source": {"evidence_id": "page", "snippet": body[:600]}})
    assert turn.model_chars == len(body) and first["body"] == body
    second = turn.model_data({"body": body, "source": {"evidence_id": "page", "snippet": body[:600]}})
    assert turn.model_chars == len(body) and second["source"]["evidence_id"] == "page"


def test_quality_final_answer_uses_only_cited_verification_and_no_raw_notes(tmp_path, monkeypatch):
    chat = owner(tmp_path)
    def script(index, request):
        if index == 0: return [("search_knowledge", {"query": "insurance", "target": "wiki"})]
        if index == 1: return candidate_detail_calls(request)
        return "Insurance pricing evidence remains incomplete. [1]"
    scripted_provider(chat, monkeypatch, "openai", script)
    result = chat.answer("Explain current insurance pricing")
    assert "Manual verification:" in result["text"]
    assert "https://example.org/evidence" not in result["text"]
    assert "participation" not in result["text"]


@pytest.mark.usefixtures("mock_reader_policy")
def test_quality_page_relevance_precedes_clipping_and_reserves_other_reads(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    body = "Navigation links and background. " * 1500 + "\n\nEnergy transition battery insurance losses: thermal runaway needs fire protection."
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw:
        {"status": "ok", "content": body, "final_url": a[1], "content_hash": "full-body-hash"})
    def script(index, request):
        assert "thermal runaway needs fire protection" in json.dumps(request)
        return "Battery insurance requires fire protection. [1]"
    scripted_provider(chat, monkeypatch, "openai", script)
    result = chat.answer("Read https://example.org/energy and explain energy transition battery insurance losses")
    assert result["sources"][0]["version"] == "full-body-hash" and result["sources"][0]["truncated"]
    frame = chat.frames.get(result["context"])
    assert "thermal runaway" in next(iter(frame["web"].values()))["body"]
    assert "Reading window limitation" in result["text"]


def test_quality_candidates_do_not_consume_citation_slots(tmp_path):
    chat = owner(tmp_path)
    chat.responder.client = object()
    turn = EvidenceTurn(chat, "Explain insurance pricing", None, [])
    result = turn.search_knowledge("insurance", target="wiki")
    assert result and not turn.sources and not turn.evidence
    assert result[0]["preview"] and "index" not in result[0]["source"]
    detail = turn.get_source_details(result[0]["source"]["evidence_id"])
    assert detail["source"]["index"] == 1 and "Climate hazards" in detail["text"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_quality_meeting_tools_do_not_hide_online_searches(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    chat.reader_factory = lambda: MeetingsReader([meeting("one", "Alpha conference", "Paris")])
    def script(index, request):
        if index == 0: return [("get_meeting_details", {"event_id": "one"})]
        assert "stored approved status; not current verification" in json.dumps(request)
        return "The archived record gives a registration deadline; current availability is unverified. [1]"
    requests = scripted_provider(chat, monkeypatch, provider, script)
    result = chat.answer("What are Alpha conference's current registration deadlines?")
    assert len(requests) == 5 and [row["tool"] for row in evidence_trace(result)] == ["get_meeting_details"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_quality_reserves_synthesis_after_provider_search(tmp_path, monkeypatch, provider):
    from agentic_wiki.chat_evidence import MAX_MODELS
    chat = owner(tmp_path)
    scripted_provider(chat, monkeypatch, provider, lambda *args: pytest.fail("Late search cannot send a provider request"))
    turn = EvidenceTurn(chat, "Find current developments", None, [])
    turn.models = MAX_MODELS - 2
    result = turn.search_web("current developments")
    assert result["status"] == "unavailable" and "reserved URL-selection and final-synthesis" in result["reason"]
    assert turn.models == MAX_MODELS - 2 and turn.searches == 0


def test_quality_chronology_scope_is_once_and_only_requested_gap_is_appended(tmp_path):
    from contextlib import nullcontext
    chat = owner(tmp_path)
    class Reader:
        def public_snapshot(self): return nullcontext()
        def articles(self, **kwargs): return {"items": [], "pagination": {"pages": 1}}
        def pdf_articles_all(self): return []
        def knowledge_chronology(self): return {}
    chat.reader_factory = Reader
    chat.responder.client = object()
    turn = EvidenceTurn(chat, "Explain recent energy transition developments", None, [])
    turn.execute("search_knowledge", {"query": "energy", "target": "recent_articles"})
    repeated = turn.execute("search_knowledge", {"query": "battery", "target": "recent_articles"})
    assert repeated["status"] == "stopped" and "chronology" in repeated["reason"]
    assert "added/material-update window" not in turn.final_text("Energy findings are incomplete.")
    turn.question = "Which articles were added in the last 14 days?"
    assert "added/material-update window" in turn.final_text("Findings are incomplete.")


def test_quality_coverage_and_reader_failures_keep_specific_answers_without_citations(tmp_path):
    turn = EvidenceTurn(owner(tmp_path), "List added articles", None, [])
    answer = "337 approved items lack ingestion chronology; no confirmed new items can be identified. Check their official publication pages manually."
    assert turn.final_text(answer) == answer
    failure = "The requested official page could not be read because robots.forbidden; its early-bird date remains unknown."
    assert turn.final_text(failure) == failure
    guarded = turn.final_text("Unverified preview [1] [private-id, index 10].")
    assert "[1]" not in guarded and "private-id" not in guarded and "Citation limitation" in guarded


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_quality_candidate_answer_uses_remaining_turn_for_evidence_check(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    def script(index, request):
        if index == 0:
            return [("search_knowledge", {"query": "insurance", "target": "wiki"})]
        if index == 1:
            return "Climate hazards affect pricing. [1]"
        if index == 2:
            assert "Evidence sufficiency check" in json.dumps(request)
            return candidate_detail_calls(request)
        return "Climate hazards affect pricing. [1]"
    requests = scripted_provider(chat, monkeypatch, provider, script, check_drafts=True)
    result = chat.answer("Explain insurance pricing")
    assert len(requests) == 6 and [row["tool"] for row in evidence_trace(result)] == ["search_knowledge", "get_source_details"]
    assert result["sources"][0]["index"] == 1 and "[1]" in result["text"]


def test_quality_candidate_preview_counts_toward_shared_evidence_budget(tmp_path):
    from agentic_wiki.chat_evidence import MAX_TEXT
    turn = EvidenceTurn(owner(tmp_path), "Explain findings", None, [])
    result = turn.model_data({"preview": "A" * 10000, "evidence": [{"text": "B" * 25000}]})
    assert turn.model_chars == MAX_TEXT and len(result["preview"]) + len(result["evidence"][0]["text"]) == MAX_TEXT
    assert result["evidence"][0]["truncated"] and "partial" in result["coverage_note"]


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_quality_first_draft_checks_original_topic_window_and_unread_citations(tmp_path, monkeypatch, provider):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    original_question = "Explain energy transition reports added in the last 14 days for insurers."
    url = "https://example.org/official-energy"
    def script(index, request):
        if index == 0:
            return "General catastrophe losses and disclosures were added September 25. [9]"
        if index == 1:
            feedback = request["messages"][-1]["content"]
            assert "Evidence sufficiency check" in feedback and original_question in feedback
            assert "America/New_York" in feedback and "knowledge_window" in feedback
            assert '"unallocated_citations": [9]' in feedback and '"registered_citations": []' in feedback
            assert "publication" in feedback and "ingestion" in feedback
            return [("search_web", {"query": "official energy transition battery insurance"})]
        if index == 2:
            assert url in json.dumps(request)
            return [("read_url", {"url": url})]
        assert "Battery fire protection" in json.dumps(request)
        return "Battery fire protection affects transition underwriting. [1] The page does not establish its ingestion date; verify the approved knowledge history."
    requests = scripted_provider(chat, monkeypatch, provider, script, check_drafts=True)
    search_calls = install_fake_native_search(chat, url)
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw:
        {"status": "ok", "content": "Battery fire protection is necessary for energy transition insurance.", "final_url": url, "content_hash": "energy-body"})
    result = chat.answer(original_question)
    assert len(requests) == 6 and len(search_calls) == 1
    assert [row["tool"] for row in evidence_trace(result)] == ["search_web", "read_url"]
    assert "catastrophe" not in result["text"] and "September 25" not in result["text"] and "[9]" not in result["text"]
    assert result["sources"][0]["url"] == url and result["sources"][0]["version"] == "energy-body"
    feedback = json.loads(feedback_text(requests).split("\n", 1)[1])
    start, end = window(original_question, datetime.fromisoformat(result["as_of"]))
    assert feedback["knowledge_window"] == {"start": start.isoformat(), "end": end.isoformat(), "zone": "America/New_York"}


def install_fake_native_search(chat, url):
    client = chat.client
    original = client.create
    calls = []
    def create(**request):
        native = "input" in request or any(tool.get("type") == "web_search_20250305" for tool in request.get("tools", []))
        if not native:
            return original(**request)
        calls.append(request)
        selected = url[len(calls) - 1] if isinstance(url, list) else url
        payload = ({"output": [{"type": "web_search_call", "status": "completed", "action": {"sources": [{"url": selected}]}}]}
            if chat.provider == "openai" else {"content": [{"type": "web_search_tool_result", "content": [{"type": "web_search_result", "url": selected}]}]})
        return SimpleNamespace(model_dump=lambda: payload)
    client.create = create
    client.responses = client
    return calls


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_quality_model_visible_knowledge_changes_is_not_recent_developments(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    def script(index, request):
        schema = next(tool for tool in request["tools"] if (tool.get("name") or tool.get("function", {}).get("name")) == "search_knowledge")
        properties = (schema.get("input_schema") or schema["function"]["parameters"])["properties"]
        assert properties["target"]["enum"] == ["wiki", "meetings", "knowledge_changes"]
        return [("ask_clarification", {"question": "Which named source do you mean?"})]
    scripted_provider(chat, monkeypatch, provider, script)
    assert chat.answer("Which recent developments matter?")["needs_clarification"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_quality_fourteen_requests_cover_plan_nine_tool_rounds_two_searches_check_and_synthesis(tmp_path, monkeypatch, provider):
    from agentic_wiki.chat_evidence import MAX_MODELS, MAX_READS, MAX_TEXT, MAX_SECONDS
    chat = owner(tmp_path)
    def script(index, request):
        if index == 0:
            return "No evidence is yet available; the original request needs more investigation."
        if index in {1, 3}:
            return [("search_web", {"query": "official climate evidence " + str(index)})]
        if request.get("tools"):
            return [("read_url", {"url": "invalid-" + str(index)})]
        assert not request.get("tools") and "final cited answer" in json.dumps(request["messages"])
        return "No usable page bodies were read; open the official source manually to verify the requested facts."
    requests = scripted_provider(chat, monkeypatch, provider, script, check_drafts=True)
    search_calls = install_fake_native_search(chat, "https://example.org/unread")
    result = chat.answer("Find evidence and explain climate risk")
    assert MAX_MODELS == 14 and len(requests) + len(search_calls) == MAX_MODELS and len(search_calls) == 2
    assert len(result["tool_execution"]) == 10 and MAX_TOOLS == 16 and (MAX_READS, MAX_TEXT, MAX_SECONDS) == (4, 32000, 120)
    assert result["agent_mode"] == provider and "No usable page bodies" in result["text"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_quality_tool_cap_requests_direct_supported_synthesis(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    question = "What recent energy-transition developments affect insurers?"
    def script(index, request):
        if index == 0:
            return [("get_source_details", {"evidence_id": "missing-" + str(i)}) for i in range(MAX_TOOLS - 1)]
        assert not request.get("tools")
        supplied = json.dumps(request)
        assert "Evidence sufficiency check" not in supplied and question in supplied
        assert "No further tools are available" in supplied
        assert "Research is partial" in supplied
        return "No energy-transition findings could be verified. Open official energy-system reports to check the requested developments."
    requests = scripted_provider(chat, monkeypatch, provider, script, check_drafts=True)
    result = chat.answer(question)
    assert len(requests) == 3 and len(result["tool_execution"]) == MAX_TOOLS
    assert "Climate disclosure" not in result["text"] and "No energy-transition findings" in result["text"]


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_quality_failed_primary_body_can_reformulate_search_for_another_readable_source(tmp_path, monkeypatch, provider):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    blocked, alternate = "https://example.org/blocked-primary", "https://example.org/alternate-primary"
    def script(index, request):
        if index == 0:
            return [("search_web", {"query": "official energy transition report"})]
        if index == 1:
            return [("read_url", {"url": blocked})]
        if index == 2:
            assert "robots.forbidden" in json.dumps(request)
            return [("search_web", {"query": "other official energy transition battery report"})]
        if index == 3:
            assert alternate in json.dumps(request)
            return [("read_url", {"url": alternate})]
        assert "Battery fire protection" in json.dumps(request)
        return "Battery fire protection matters for energy-transition underwriting. [1]"
    requests = scripted_provider(chat, monkeypatch, provider, script)
    search_calls = install_fake_native_search(chat, [blocked, alternate])
    reads = []
    def read(key, url, **kwargs):
        reads.append(url)
        if url == blocked:
            return {"status": "failed", "failure_reason": "robots.forbidden"}
        return {"status": "ok", "content": "Battery fire protection matters for energy transition insurance.", "final_url": url, "content_hash": "alternate-body"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    result = chat.answer("Explain energy-transition developments for insurers")
    assert len(search_calls) == 2 and len(requests) == 8 and reads == [blocked, alternate]
    assert [row["tool"] for row in evidence_trace(result)] == ["search_web", "read_url", "search_web", "read_url"]
    assert result["sources"][0]["url"] == alternate and result["sources"][0]["version"] == "alternate-body"
    assert "[1]" in result["text"] and len(result["sources"]) == 1


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("fresh", [False, True])
def test_identity_verification_reuses_body_unless_current_status_requested(tmp_path, monkeypatch, provider, fresh):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    url = "https://example.org/official"
    reads = []
    def read(key, target, **kwargs):
        reads.append(target)
        return {"status": "ok", "content": "Registration is " + ("open." if len(reads) == 1 else "closed."),
            "final_url": target, "content_hash": f"body-{len(reads)}"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    first = chat.answer("Read " + url)
    evidence_id = first["sources"][0]["evidence_id"]
    def script(index, request):
        if index == 0:
            assert evidence_id in json.dumps(request)
            return [("get_source_details", {"evidence_id": evidence_id}), ("read_url", {"url": url})]
        supplied = json.dumps(request)
        assert ('Registration is closed.' if fresh else 'Registration is open.') in supplied
        return ("Current registration is closed. [1]" if fresh else "The retained official source identity and body match. [1]")
    requests = scripted_provider(chat, monkeypatch, provider, script)
    question = "Verify its retained identity" + (" and current status." if fresh else ".")
    result = chat.answer(question, context=first["context"])
    assert len(requests) == 5 and result["agent_mode"] == provider
    assert [call["tool"] for call in evidence_trace(result)] == ["get_source_details", "read_url"]
    assert [call["status"] for call in evidence_trace(result)] == (["read", "read"] if fresh else ["cached", "cached"])
    assert len(reads) == (3 if fresh else 1)
    assert result["sources"] and result["sources"][0]["evidence_id"] == evidence_id


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_assessment_cycle_start_is_not_report_publication(tmp_path, monkeypatch, provider):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    url = "https://www.ipcc.ch/assessment-report/ar7/"
    # Actual paragraph retained by acceptance-*-two-round-live.json; no publication date in this body.
    body = ("The IPCC is currently in its seventh assessment cycle which formally began in July 2023 "
        "with elections of the new Chair and new IPCC and TFI Bureaus.")
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw: {
        "status": "ok", "content": body, "final_url": url, "content_hash": "actual-cycle-paragraph"})
    def script(index, request):
        system = request.get("system") or request["messages"][0]["content"]
        assert "assessment cycle start" in system and "report publication" in system
        assert "does not establish" in system and "date type" in system
        assert body in json.dumps(request)
        return "The seventh assessment cycle began in July 2023. [1] AR7 report publication date: unknown; check the official IPCC report page for publication details."
    requests = scripted_provider(chat, monkeypatch, provider, script)
    result = chat.answer("Read " + url + ". When did AR7 begin, and what is its report publication date?")
    assert len(requests) == 4 and "assessment cycle began in July 2023" in result["text"]
    assert "report publication date: unknown" in result["text"] and "check the official IPCC report page" in result["text"]
    assert result["sources"][0]["url"] == url and "[1]" in result["text"]


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("absent", ["does not specify", "does not state"])
def test_missing_publication_wording_keeps_publisher_verification(tmp_path, monkeypatch, provider, absent):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    url = "https://www.ipcc.ch/assessment-report/ar7/"
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw: {
        "status": "ok", "content": "The seventh assessment cycle formally began in July 2023.",
        "final_url": url, "content_hash": "cycle-only"})
    # Exact missing-date phrasing observed in the successful bounded OpenAI live response.
    text = f"The seventh assessment cycle began in July 2023. The page {absent} an AR7 report publication date. [1]"
    scripted_provider(chat, monkeypatch, provider, lambda index, request: text)
    result = chat.answer("Read " + url + ". What is the report publication date?")
    assert text in result["text"]
    assert "Manual verification:" in result["text"]
    guidance = result["text"].split("Manual verification:", 1)[1]
    assert url in guidance and "publisher" in guidance and "publication date" in guidance
    assert "event organizer" not in guidance and "participation" not in guidance


def identity_model(chat):
    """Script the model's choices for identity/reader regressions, using actual returned tool evidence."""
    def script(index, request):
        messages = request["messages"]
        candidates = next(json.loads(message["content"].split("\n", 1)[1]) for message in messages
            if isinstance(message.get("content"), str) and message["content"].startswith("Conversation identity candidates"))
        initial = next(json.loads(message["content"].split("\n", 1)[1]) for message in messages
            if isinstance(message.get("content"), str) and message["content"].startswith("Existing read evidence"))
        question = next(message["content"] for message in messages if message.get("role") == "user"
            and isinstance(message.get("content"), str) and not message["content"].startswith(("Conversation identity", "Existing read evidence")))
        results = [json.loads(message["content"]) for message in messages if message.get("role") == "tool"
            and json.loads(message["content"]).get("status") not in {"planned", "finish_accepted", "research_pending"}]
        if results and results[-1].get("status") == "clarification":
            return results[-1]["question"]
        evidence = list(initial["evidence"])
        discoveries = []
        def collect(value):
            if isinstance(value, list):
                for entry in value: collect(entry)
            elif isinstance(value, dict):
                if value.get("kind") and value.get("source") and ("preview" in value or "preview_reference" in value):
                    discoveries.append(value)
                elif value.get("kind") and value.get("source") and "text_reference" in value:
                    original = next((entry for entry in evidence if entry["source"].get("evidence_id") == value["text_reference"]), None)
                    if original:
                        evidence.append({**value, "text": original["text"]})
                elif value.get("kind") and value.get("source") and "text" in value:
                    evidence.append(value)
                    collect(value.get("read_results", []))
                elif value.get("body") and value.get("source"):
                    evidence.append({"text": value["body"], "source": value["source"]})
                else:
                    for child in value.values(): collect(child)
        for value in results: collect(value)
        if discoveries and not evidence:
            return [("get_meeting_details" if entry["kind"] == "meeting" else "get_source_details",
                {"event_id" if entry["kind"] == "meeting" else "evidence_id": entry["source"]["evidence_id"]}) for entry in discoveries[:8]]
        if evidence and re.search(r"\b(is|are)\b.{0,30}\b(open|closed|available)\b", question, re.I) and not any(entry["source"].get("heading") == "web" for entry in evidence):
            targets = [url for entry in evidence for url in entry["source"].get("source_urls", [])[:1]]
            if targets and not any(result.get("status") in {"failed", "unavailable"} for result in results):
                return [("read_url", {"url": targets[0], "refresh": True})]
        if evidence:
            unique = {entry["source"]["index"]: entry for entry in evidence}
            return "\n".join(f"- **{entry['source']['title']}** [{number}]\n{entry['text']}" for number, entry in sorted(unique.items()))
        if results:
            return "No usable evidence is available; " + str(results[-1].get("reason") or "missing source facts") + "; manually verify the official source URL."
        if any(note.startswith("read_url:") for note in initial["notes"]) and not candidates["sources"]:
            return "No usable evidence is available. " + "; ".join(initial["notes"])
        sources = candidates["sources"]
        position = 1 if re.search(r"\bsecond\b|\b(?:meeting|source)\s*2\b", question, re.I) else 2 if "third" in question else 0
        topic = bool(re.search(r"\bnew\b.*\b(reports?|articles?)\b|\b(reports?|articles?)\b.*\badded\b", question, re.I))
        if topic:
            return [("search_knowledge", {"query": question, "target": "recent_articles"})]
        explicit_ordinal = bool(re.search(r"\b(first|second|third|above)\b|\bsource\s*\d|\bmeeting\s*\d", question, re.I))
        page_sources = [source for source in sources if source["kind"] == "web"]
        new_topic = "IFRS" in question or "Explain physical risk" in question
        if page_sources and not new_topic and not re.search(r"\blist\b.*\bconferences\b", question, re.I):
            if candidates["source_focus"] and not explicit_ordinal:
                return [("get_source_details", {"evidence_id": candidates["source_focus"]})]
            if len(page_sources) > 1 and not explicit_ordinal:
                return [("ask_clarification", {"question": "Please identify the page by source number or URL."})]
            source = page_sources[position] if position < len(page_sources) else None
            if source:
                return [("get_source_details", {"evidence_id": source["evidence_id"]})]
        article_reference = bool(re.search(r"\b(article|report)\b", question, re.I)) and bool(sources)
        if candidates["source_focus"] and re.search(r"\b(it|its)\b", question, re.I) and not new_topic:
            return [("get_source_details", {"evidence_id": candidates["source_focus"]})]
        if article_reference and not new_topic:
            selected = [source for source in sources if source["kind"] in {"wiki", "article", "pdf_article"}]
            if len(selected) > 1 and not explicit_ordinal:
                return [("ask_clarification", {"question": "Please identify the source by its number or URL."})]
            if position < len(selected):
                return [("get_source_details", {"evidence_id": selected[position]["evidence_id"]})]
        meeting_reference = candidates["ordered"] and not new_topic and (explicit_ordinal or re.search(r"\b(it|its)\b", question, re.I))
        if meeting_reference:
            key = candidates["ordered"][position] if explicit_ordinal and position < len(candidates["ordered"]) else candidates["focus"] or (candidates["ordered"][0] if len(candidates["ordered"]) == 1 else None)
            return [("get_meeting_details", {"event_id": key})] if key else [("ask_clarification", {"question": "Please identify the meeting by its number or URL."})]
        if explicit_ordinal:
            return [("ask_clarification", {"question": "The original result identity is unavailable; provide its name or URL."})]
        return [("search_knowledge", {"query": question, "target": "meetings" if re.search(r"conference|meeting|deadline|consultation", question, re.I) else "wiki"})]
    requests = scripted_provider(chat, None, "openai", script)
    chat.responder.client.responses = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(model_dump=lambda: {"output": []}))
    return requests


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("focused", [False, True])
def test_online_agent_selects_topic_and_iterates_after_page_body(tmp_path, monkeypatch, provider, focused):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    article = {"article_id": "a", "title": "Approved report", "summary": "Approved article findings",
        "canonical_url": "https://example.org/article"}
    class Reader(MeetingsReader):
        def articles(self, **kwargs): return {"items": [article], "pagination": {"pages": 1}}
        def article(self, key): return copy.deepcopy(article)
        def knowledge_chronology(self): return {"article:a": {"first_ingested_at": "2026-10-08T12:00:00Z"}}
    chat.reader_factory = lambda: Reader([meeting("one", "Alpha conference", "Paris"), meeting("two", "Beta conference", "London")])
    listed = chat.answer("List upcoming conferences")
    if focused:
        scripted_provider(chat, monkeypatch, provider, lambda index, request:
            [("get_meeting_details", {"event_id": "two"})] if index == 0 else "Beta conference [1].")
        previous = chat.answer("Tell me more about the second meeting above", context=listed["context"])
    else:
        previous = listed
    reads = []
    def read(key, url, **kwargs):
        reads.append(url)
        return {"status": "ok", "content": "Actual page facts require further insurance evidence.", "final_url": url, "content_hash": "page-hash"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    def script(index, request):
        serialized = json.dumps(request)
        tools = {tool.get("name") or tool["function"]["name"] for tool in request.get("tools", [])}
        if index == 0:
            assert not reads and not chat.frames.get(previous["context"])["web"]
            assert "Conversation identity candidates" in serialized and "two" in serialized and "one" in serialized
            assert "get_source_details" in tools and "ask_clarification" in tools
            assert "Existing read evidence" in serialized and '"evidence": []' in str(request)
            return [("search_knowledge", {"query": "New reports for this climate committee", "target": "recent_articles"})]
        if index == 1:
            assert "Approved article findings" in serialized and "start_date: 2027" not in serialized
            return [("get_source_details", {"evidence_id": "a"})]
        if index == 2:
            assert "Original stable source identity is unavailable" not in serialized
            return [("read_url", {"url": article["canonical_url"]})]
        if index == 3:
            assert "Actual page facts" in serialized and "search_knowledge" in tools
            return [("search_knowledge", {"query": "insurance pricing", "target": "wiki"})]
        if index == 4:
            return candidate_detail_calls(request)
        assert tools and "Climate hazards affect insurance pricing" in serialized
        return "Approved findings [1], actual page [2], insurance evidence [3]."
    requests = scripted_provider(chat, monkeypatch, provider, script)
    result = chat.answer("Summarize new reports added in the last 14 days for this climate committee.", context=previous["context"])
    assert result["agent_mode"] == provider and len(requests) == 9
    assert [entry["tool"] for entry in evidence_trace(result)] == ["search_knowledge", "get_source_details", "read_url", "search_knowledge", "get_source_details"]
    assert not any(source["type"] == "meeting" for source in result["sources"])
    assert reads == [article["canonical_url"]]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_online_agent_decides_ambiguity_and_budget_gap(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    chat.reader_factory = lambda: MeetingsReader([meeting("one", "Alpha conference", "Paris"), meeting("two", "Beta conference", "London")])
    previous = chat.answer("List upcoming conferences")
    def clarify(index, request):
        assert "Conversation identity candidates" in json.dumps(request)
        return [("ask_clarification", {"question": "Which conference, Alpha or Beta?"})] if index == 0 else "Which conference, Alpha or Beta?"
    requests = scripted_provider(chat, monkeypatch, provider, clarify)
    ambiguous = chat.answer("What is its registration deadline?", context=previous["context"])
    assert len(requests) == 2 and ambiguous["needs_clarification"] and not ambiguous["sources"]
    assert evidence_trace(ambiguous)[0]["tool"] == "ask_clarification"
    def exhaust(index, request):
        if index == 0:
            return [("read_url", {"url": "invalid-" + str(i)}) for i in range(MAX_TOOLS + 1)]
        assert not request.get("tools") and "budget_exhausted" in json.dumps(request)
        return "No verified page evidence is available."
    requests = scripted_provider(chat, monkeypatch, provider, exhaust)
    gap = chat.answer("Find missing facts")
    assert len(requests) == 3 and "Manual verification" in gap["text"] and not gap["sources"]


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_clarification_terminates_provider_and_same_batch_tools(tmp_path, monkeypatch, provider):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    chat.reader_factory = lambda: MeetingsReader([meeting("one", "Alpha conference", "Paris"), meeting("two", "Beta conference", "London")])
    listed = chat.answer("List upcoming conferences")
    reads = []
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw: reads.append(a[1]) or {"status": "failed"})
    def script(index, request):
        if index == 0:
            return [("search_knowledge", {"query": "List upcoming conferences", "target": "meetings"})]
        if index == 1:
            return [("ask_clarification", {"question": "Which conference, Alpha or Beta?"}),
                ("read_url", {"url": "https://example.org/must-not-read"})]
        raise AssertionError("No provider call is permitted after clarification")
    requests = scripted_provider(chat, monkeypatch, provider, script)
    result = chat.answer("List conferences and identify which one I mean", context=listed["context"])
    assert len(requests) == 3 and reads == []
    assert [entry["tool"] for entry in evidence_trace(result)] == ["search_knowledge", "ask_clarification"]
    assert result["needs_clarification"] and result["text"] == "Which conference, Alpha or Beta?"
    assert chat.frames.get(result["context"])["ordered"] == ["one", "two"]
    assert [entry["source"]["evidence_id"] for entry in chat.frames.get(result["context"])["source_order"]] == ["one", "two"]


def test_explicit_knowledge_tool_category_and_invalid_category(tmp_path):
    chat = owner(tmp_path)
    chat.reader_factory = lambda: (_ for _ in ()).throw(AssertionError("Wiki target must not query meetings"))
    turn = EvidenceTurn(chat, "conference insurance pricing", None, [])
    result = turn.execute("search_knowledge", {"query": "conference insurance pricing", "target": "wiki"})
    assert result["evidence"][0]["kind"] == "wiki"
    assert turn.execute("search_knowledge", {"query": "insurance", "target": "private_db"})["status"] == "invalid"


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("question", ["What is its registration deadline?", "Which questions concern insurers?",
    "Read source 1", "Tell me more about the second article above"])
def test_source_only_never_consumes_prior_context_or_history(tmp_path, monkeypatch, question):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    reads = []
    def read(key, url, **kwargs):
        reads.append(url)
        return {"status": "ok", "content": "Question 1 asks insurers to quantify climate risks. Registration deadline: 2027-04-20.",
            "final_url": url, "content_hash": str(len(reads))}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    chat.reader_factory = lambda: MeetingsReader([])
    first = chat.answer("Read https://example.org/consultation")
    followup = chat.answer(question, context=first["context"], history=[{"role": "assistant", "content": first["text"]}])
    assert not followup["sources"] and "2027-04-20" not in followup["text"] and len(reads) == 1
    assert "Manual verification" in followup["text"]
    explicit = chat.answer("Read https://example.org/consultation", context=first["context"])
    assert len(reads) == 2 and explicit["sources"][0]["version"] == "2"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("question", ["What is its registration deadline?", "Search the web for missing climate facts"])
def test_first_model_failure_uses_one_context_free_source_only_fallback(tmp_path, monkeypatch, provider, question):
    chat = owner(tmp_path)
    reader = MeetingsReader([meeting("one", "Alpha conference", "Paris")])
    chat.reader_factory = lambda: reader
    prior = chat.answer("List upcoming conferences")
    def fail(index, request):
        raise RuntimeError("bounded mock outage")
    requests = scripted_provider(chat, monkeypatch, provider, fail)
    result = chat.answer(question, context=prior["context"])
    assert len(requests) == 2
    if "its" in question:
        assert not result["sources"]
    else:
        assert result["sources"] and all(source["heading"] == "wiki" for source in result["sources"])
    assert result["needs_clarification"] is ("its" in question)
    assert all(call["tool"] == "search_knowledge" for call in evidence_trace(result))
    assert "Manual verification" in result["text"]


class MeetingsReader:
    def __init__(self, records):
        self.records = records

    def public_snapshot(self):
        from contextlib import nullcontext
        return nullcontext()

    def knowledge_chronology(self):
        return {"meeting:one": {"knowledge_id": "approved-one", "first_ingested_at": "2020-01-01T00:00:00Z"}}

    def meetings(self, **kwargs):
        return {"items": copy.deepcopy(self.records), "pagination": {"pages": 1}, "coverage": {"status": "partial"}}

    def pdf_articles_all(self):
        return []


def meeting(key, name, location):
    return {"event_id": key, "name": name, "start_date": "2027-06-10", "organizer": "Publisher",
        "raw_time_text": "10:00 EDT", "event_timezone": "America/New_York", "location": location,
        "deadline_type": "registration", "deadline_date": "2027-05-01", "status": "scheduled",
        "source_urls": ["https://example.org/" + key], "verification_status": "partial",
        "pdf_observations": [{"deadline_type": "expert_review", "deadline_date": "2027-04-01", "source_urls": ["https://example.org/review"]}]}


@pytest.mark.parametrize("question", [
    "For Alpha conference, what is its registration deadline?",
    "What is Alpha conference's registration deadline?"])
def test_review19_source_only_resolves_current_unique_name_before_pronoun(tmp_path, question):
    chat = owner(tmp_path)
    records = [meeting("alpha", "Alpha conference", "Paris"), meeting("beta", "Beta conference", "London")]
    chat.reader_factory = lambda: MeetingsReader(records)
    prior = chat.answer("Beta conference")
    result = chat.answer(question, context=prior["context"], history=[
        {"role": "assistant", "content": "Beta conference is the selected event."}])
    assert not result["needs_clarification"]
    assert [source["evidence_id"] for source in result["sources"]] == ["alpha"]
    assert "registration: 2027-05-01" in result["text"] and "[1]" in result["text"]
    assert result["sources"][0]["url"] == "https://example.org/alpha"
    assert len(evidence_trace(result)) == 1
    ambiguous = chat.answer("What is its registration deadline?", context=result["context"])
    assert ambiguous["needs_clarification"] and not ambiguous["sources"]
    chat.reader_factory = lambda: MeetingsReader([*records, meeting("alpha-2", "Alpha conference", "Rome")])
    duplicated = chat.answer("For Alpha conference, what is its registration deadline?")
    assert duplicated["needs_clarification"] and not duplicated["sources"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_review19_partial_model_output_has_cited_verification_step(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    scripted_provider(chat, monkeypatch, provider, lambda index, request:
        [("search_knowledge", {"query": "insurance pricing", "target": "wiki"})] if index == 0 else
        candidate_detail_calls(request) if index == 1 else
        "Climate hazards affect insurance pricing. [1]")
    client = chat.client
    original = client.create
    def partial(**kwargs):
        response = original(**kwargs)
        if provider == "anthropic":
            payload = response.model_dump()
            if payload["stop_reason"] == "end_turn":
                payload["stop_reason"] = "max_tokens"
            return SimpleNamespace(model_dump=lambda: payload)
        if response.choices[0].finish_reason == "stop":
            response.choices[0].finish_reason = "length"
        return response
    client.create = partial
    result = chat.answer("Explain insurance pricing")
    assert "Climate hazards affect insurance pricing. [1]" in result["text"]
    assert "answer is partial" in result["text"] and not result["needs_clarification"]
    assert "Manual verification:" in result["text"]
    assert "https://example.org/evidence" not in result["text"]  # Unread external links are not this Wiki passage's identity.
    assert result["sources"][0]["url"] == (chat.responder.base_source_url + "/" if chat.responder.base_source_url else "") + result["sources"][0]["path"]
    assert "full source" in result["text"]


def test_review19_source_only_unknown_participation_has_named_verification_step(tmp_path):
    chat = owner(tmp_path)
    chat.reader_factory = lambda: MeetingsReader([meeting("alpha", "Alpha conference", "Paris")])
    result = chat.answer("Alpha conference registration requirements")
    assert "Eligibility / submission requirements / additional deadlines: not recorded" in result["text"]
    assert not result["needs_clarification"] and result["sources"][0]["evidence_id"] == "alpha"
    assert "Manual verification:" in result["text"]
    assert "https://example.org/alpha" in result["text"]
    assert "organizer" in result["text"] and "Alpha conference" in result["text"]


@pytest.mark.usefixtures("mock_reader_policy")
def test_review19_source_only_truncated_page_has_verification_step(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    url = "https://example.org/long"
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw:
        {"status": "ok", "content": "Climate insurance findings. " * 2000,
            "final_url": url, "content_hash": "long-body"})
    result = chat.answer(url)
    assert result["sources"][0]["truncated"] and "Reading window limitation" in result["text"]
    assert "Manual verification:" in result["text"] and url in result["text"]
    assert not result["needs_clarification"]


@pytest.mark.parametrize("prior", ["none", "history", "context"])
@pytest.mark.parametrize("kind", ["article", "page", "PDF article"])
def test_review20_current_named_page_is_independent_of_prior_turn(tmp_path, prior, kind):
    chat = owner(tmp_path)
    title = "Leaders should plan for colliding risks, not isolated crises"
    (tmp_path / "wiki" / "article-approved.md").write_text(
        f"# {title}\nRisks cascade and compound; boards should map dependencies.\nhttps://example.org/approved")
    chat.responder.kb.reload()
    question = f"For the {kind} '{title}', what are its key findings?"
    kwargs = {"history": [{"role": "assistant", "content": "Another article says an unrelated thing."}]} if prior == "history" else {"context": "unrelated-handle"} if prior == "context" else {}
    result = chat.responder.answer(question, **kwargs)
    assert not result.get("needs_clarification") and result["sources"]
    assert "Risks cascade and compound" in result["text"] and "[1]" in result["text"]
    assert {source["path"] for source in result["sources"]} == {"wiki/article-approved.md"}
    assert result["sources"][0]["evidence_id"] and result["sources"][0]["url"] == (chat.responder.base_source_url + "/" if chat.responder.base_source_url else "") + "wiki/article-approved.md"
    if prior != "none":
        (tmp_path / "wiki" / "article-duplicate.md").write_text(f"# {title}\nAnother visible record.\nhttps://example.org/duplicate")
        chat.responder.kb.reload()
        ambiguous = chat.responder.answer(question, **kwargs)
        assert ambiguous["needs_clarification"] and not ambiguous["sources"]


def test_real_approved_article_offline_distinguishes_publication_from_collection(monkeypatch):
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLIMATE_CHAT_PROVIDER", "CLIMATE_REGISTRY_DB"):
        monkeypatch.delenv(key, raising=False)
    responder = AgenticWikiResponder()
    result = responder.answer(
        "For the article Leaders should plan for colliding risks, not isolated crises, "
        "what is its publication date and what are its key findings?")
    assert not result["needs_clarification"] and result["agent_mode"] == "offline"
    assert "Publication date: unknown" in result["text"]
    assert "Collection time: 2026-10-07T22:14:35.367266Z" in result["text"]
    assert "Collection time is not a publication date" in result["text"]
    assert "Key findings:" in result["text"] and "Risks interact" in result["text"]
    assert "Manual verification:" in result["text"]
    guidance = result["text"].split("Manual verification:", 1)[1]
    assert "publication date" in guidance and "publisher" in guidance
    assert "participation" not in guidance and "event organizer" not in guidance
    source = result["sources"][0]
    assert source["evidence_id"] == "article-00de8b1c45986f255c385eb6"
    assert source["url"] in result["text"]
    assert source["url"] == "https://www.weforum.org/stories/geo-economics-and-politics/how-leaders-anticipate-risks-govern-boards-avoid-crises"


def test_offline_named_article_keeps_sourced_publication_date(tmp_path):
    from contextlib import nullcontext
    chat = owner(tmp_path)
    (tmp_path / "wiki" / "article-article-a.md").write_text(
        "# Approved report\nPublication date: 2026-09-14\nCollection time: 2026-10-07T22:14:35Z\n"
        "Risks cascade and compound.\nhttps://example.org/approved")
    chat.responder.kb.reload()
    class Reader:
        def public_snapshot(self): return nullcontext()
        def knowledge_chronology(self): return {}
        def article(self, article_id):
            assert article_id == "article-a"
            return {"article_id": article_id, "title": "Approved report", "publication_date": "2026-09-14",
                "collected_at": "2026-10-07T22:14:35Z", "summary": "Risks cascade and compound.",
                "canonical_url": "https://example.org/approved", "published_candidate_sha256": "approved-version"}
    chat.reader_factory = Reader
    result = chat.responder.answer("For the article Approved report, what is its publication date and key findings?")
    assert "Publication date: 2026-09-14" in result["text"]
    assert "Collection time: 2026-10-07T22:14:35Z" in result["text"]
    assert "Publication date: unknown" not in result["text"]
    assert result["sources"][0]["evidence_id"] == "article-article-a.md:wiki:1"
    assert result["sources"][0]["url"] == (chat.responder.base_source_url + "/" if chat.responder.base_source_url else "") + "wiki/article-article-a.md"


@pytest.mark.parametrize("entrypoint", ["legacy", "evidence"])
def test_review20_empty_source_only_uses_shared_manual_guidance(tmp_path, monkeypatch, entrypoint):
    chat = owner(tmp_path)
    monkeypatch.setattr(chat.responder.kb, "search", lambda *args, **kwargs: [])
    answer = chat.responder.answer if entrypoint == "legacy" else chat.answer
    result = answer("Explain climate insurance pricing")
    assert not result["sources"] and result["agent_mode"] == "offline"
    assert not result.get("needs_clarification")
    assert "Manual verification:" in result["text"]
    assert "official source" in result["text"] and "publisher" in result["text"]
    assert "publisher or regulator" in result["text"]
    assert "official source for Explain" not in result["text"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("path", ["wiki/parametric-insurance.md", "sources/active-report.md", "../../not-a-current-page.md"])
def test_review20_model_selected_search_retains_validated_active_path(tmp_path, monkeypatch, provider, path):
    chat = owner(tmp_path)
    (tmp_path / "wiki" / "parametric-insurance.md").write_text(
        "# Parametric insurance\nA parametric trigger pays using a measured index. https://example.org/parametric")
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "active-report.md").write_text("# Active report\nA sourced report discusses insurance. https://example.org/report")
    chat.responder.kb.reload()
    reads = []
    original = chat.responder.kb.search
    def search(query, **kwargs):
        reads.append(kwargs.get("context_path"))
        return original(query, **kwargs)
    monkeypatch.setattr(chat.responder.kb, "search", search)
    def script(index, request):
        if index == 0:
            identity = next(json.loads(message["content"].split("\n", 1)[1]) for message in request["messages"]
                if isinstance(message.get("content"), str) and message["content"].startswith("Conversation identity candidates"))
            assert identity["context_path"] == (path if not path.startswith("../") else None)
            return [("search_knowledge", {"query": "What is this page mainly about?", "target": "wiki"})]
        if index == 1 and not path.startswith("../"):
            return candidate_detail_calls(request)
        return "The current page describes insurance. [1]" if not path.startswith("../") else "No evidence; verify the official source."
    requests = scripted_provider(chat, monkeypatch, provider, script)
    result = chat.responder.answer("What is this page mainly about?", context_path=path, answer_mode="brief")
    assert reads == [path if not path.startswith("../") else None] and len(requests) == (6 if not path.startswith("../") else 5)
    if not path.startswith("../"):
        assert result["sources"][0]["path"] == path
    assert result["agent_mode"] == provider


@pytest.mark.parametrize("question", [
    "Summarize new reports added in the last 14 days for this climate committee.",
    "Summarize new articles added in the last 14 days for that climate committee."])
def test_recent_article_topic_switch_ignores_committee_audience_reference(tmp_path, question):
    chat = owner(tmp_path)
    identity_model(chat)
    article = {"article_id": "a", "title": "Approved report", "publication_date": "2024-01-01",
        "summary": "Actual approved article findings", "canonical_url": "https://example.org/article"}
    class Reader(MeetingsReader):
        def articles(self, **kwargs):
            return {"items": [copy.deepcopy(article)], "pagination": {"pages": 1}}
        def article(self, key):
            assert key == "a"
            return copy.deepcopy(article)
        def knowledge_chronology(self):
            return {"article:a": {"first_ingested_at": "2026-10-08T12:00:00Z"}}
    chat.reader_factory = lambda: Reader([meeting("one", "First conference", "Paris"),
        meeting("two", "Second conference", "New York")])
    standalone = chat.answer(question)
    assert [source["evidence_id"] for source in standalone["sources"]] == ["a"]
    listed = chat.answer("List upcoming conferences")
    unfocused = chat.answer(question, context=listed["context"])
    assert not unfocused["needs_clarification"]
    assert evidence_trace(unfocused)[0]["tool"] == "search_knowledge"
    assert [source["evidence_id"] for source in unfocused["sources"]] == ["a"]
    focused = chat.answer("Tell me more about the second meeting above", context=listed["context"])
    assert focused["sources"][0]["evidence_id"] == "two"
    result = chat.answer(question, context=focused["context"])
    assert evidence_trace(result)[0]["tool"] == "search_knowledge"
    assert [source["evidence_id"] for source in result["sources"]] == ["a"]
    assert "Actual approved article findings" in result["text"] and "Second conference" not in result["text"]
    assert not chat.frames.get(result["context"])["ordered"]
    genuine = chat.answer("What is its registration deadline?", context=focused["context"])
    assert any(call["tool"] == "get_meeting_details" and call["target"] == "two" for call in evidence_trace(genuine))
    assert genuine["sources"][0]["evidence_id"] == "two" and "registration: 2027-05-01" in genuine["text"]


@pytest.mark.parametrize("event_start", ["2026-06-15", None])
def test_pdf_observation_deadline_matches_explicit_business_window(tmp_path, event_start):
    chat = owner(tmp_path)
    item = meeting("one", "Review event", "Paris")
    item.update(start_date=event_start, end_date="2026-06-16" if event_start else None,
        deadline_type="registration", deadline_date="2026-05-01",
        pdf_observations=[{"deadline_type": "expert_review", "deadline_date": "2026-04-15",
            "source_urls": ["https://example.org/official-review.pdf"]}])
    chat.reader_factory = lambda: MeetingsReader([item])
    result = chat.answer("List expert-review deadlines from 2026-04-01 to 2026-04-30")
    assert [source["evidence_id"] for source in result["sources"]] == ["one"]
    assert "expert_review: 2026-04-15" in result["text"]
    assert "registration: 2026-05-01" in result["text"]
    assert "https://example.org/official-review.pdf" in result["text"]
    if event_start:
        assert "start_date: 2026-06-15" in result["text"] and "end_date: 2026-06-16" in result["text"]
    outside = chat.answer("List expert-review deadlines from 2026-03-01 to 2026-03-31")
    assert not outside["sources"]


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("count", [1, 2])
def test_natural_insurer_question_reuses_retained_consultation_body(tmp_path, monkeypatch, count):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    identity_model(chat)
    reads = []
    def read(key, url, **kwargs):
        reads.append(url)
        return {"status": "ok", "content": "Question 1 asks insurers to quantify climate risks.",
            "final_url": url, "content_hash": "retained-consultation-body"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    chat.reader_factory = lambda: MeetingsReader([])
    urls = ["https://example.org/consultation" + str(index) for index in range(count)]
    first = chat.answer("Read " + " and ".join(urls))
    result = chat.answer("Which questions concern insurers?", context=first["context"])
    assert result["needs_clarification"] is (count == 2)
    assert len(reads) == count
    if count == 1:
        assert "Question 1 asks insurers to quantify climate risks" in result["text"]
        assert [source["url"] for source in result["sources"]] == urls
        assert result["sources"][0]["version"] == "retained-consultation-body"
        assert evidence_trace(result)[0]["status"] == "cached"
    else:
        assert evidence_trace(result)[0]["tool"] == "ask_clarification" and "identify the page" in result["text"].lower()
    no_context = chat.answer("Which questions concern insurers?")
    assert not no_context["sources"] and "No usable evidence" in no_context["text"]


def test_matched_seven_starters_and_pdf_boundary(monkeypatch):
    expected = ["Generate PDF", "Key dates & opportunities", "New reports & articles", "Insurance implications",
        "Regulation & disclosure", "Physical risks", "Transition risks"]
    assert [item["label"] for item in PROMPT_STARTERS] == expected
    source = Path("showcase/app.js").read_text()
    fallback = json.loads(source.split("const DEFAULT_PROMPT_STARTERS = ", 1)[1].split(";\n\nconst GRAPH_COLORS", 1)[0])
    assert fallback == list(PROMPT_STARTERS)
    calls = []
    monkeypatch.setattr(api_server.responder, "answer", lambda question, **kwargs: calls.append(question) or {"text": "ordinary answer", "sources": []})
    token = api_server._chat_access().create_token("Prompt routing fixture")["token"]
    client = TestClient(api_server.app, headers={"Authorization": "Bearer " + token})
    for item in PROMPT_STARTERS[1:]:
        assert client.post("/api/chat", json={"message": item["prompt"]}).json()["text"] == "ordinary answer"
    assert len(calls) == 6
    assert client.post("/api/chat", json={"message": "Generate a PDF report"}).json()["needs_clarification"]
    assert client.post("/api/chat", json={"message": "Download a PDF"}).json()["needs_clarification"]
    assert client.post("/api/chat", json={"message": "Explain this PDF report"}).json()["text"] == "ordinary answer"
    assert client.post("/api/chat", json={"message": "Do not generate a PDF for this query."}).json()["text"] == "ordinary answer"
    assert client.post("/api/chat", json={"message": "Summarize articles from 2026-09-01 to 2026-09-30"}).json()["text"] == "ordinary answer"


def test_stable_order_focus_truncation_and_untrusted_context(tmp_path):
    chat = owner(tmp_path)
    identity_model(chat)
    reader = MeetingsReader([meeting("one", "First conference", "Paris"), meeting("two", "Second conference", "New York")])
    chat.reader_factory = lambda: reader
    first = chat.answer("List upcoming conferences and opportunities")
    assert "registration: 2027-05-01" in first["text"] and "expert_review: 2027-04-01" in first["text"]
    assert "partial" in first["text"] and "not recorded" in first["text"]
    reader.records.reverse()
    second = chat.answer("Tell me more about the second meeting above", context=first["context"],
        history=[{"role": "assistant", "content": "truncated"}])
    assert "Second conference" in second["text"] and "First conference" not in second["text"]
    third = chat.answer("What is its registration deadline, and can actuaries attend?", context=second["context"])
    assert "Second conference" in third["text"] and "Eligibility" in third["text"]
    assert third["sources"][0]["evidence_id"] == "two"
    new = chat.answer("Tell me more about the second meeting above")
    assert new["needs_clarification"] and not new["sources"]
    forged = chat.answer("Tell me more about the second meeting above", context="client-invented")
    assert forged["needs_clarification"]
    reader.records = []
    removed = chat.answer("What is its deadline?", context=third["context"])
    assert not removed["sources"] and "no longer" in removed["text"]


def wiki_result_fixture(tmp_path, monkeypatch):
    from agentic_wiki.wiki_agent import SearchHit
    chat = owner(tmp_path)
    for title in ("Alpha", "Beta"):
        (tmp_path / "wiki" / (title + ".md")).write_text(f"# {title}\n{title} insurance finding: {title.lower()} capital buffers.\nhttps://example.org/{title.lower()}")
    chat.responder.kb.reload()
    chunks = {chunk.title: chunk for chunk in chat.responder.kb.chunks if chunk.title in {"Alpha", "Beta"}}
    hits = [SearchHit(chunks[title], 10 - index, "fixture ranking") for index, title in enumerate(("Beta", "Alpha"))]
    monkeypatch.setattr(chat.responder.kb, "search", lambda *args, **kwargs: hits)
    monkeypatch.setattr(chat.responder, "_rank_for_answer", lambda question, values, **kwargs: values)
    return chat, hits


@pytest.mark.parametrize("entrypoint", ["legacy", "evidence"])
def test_wiki_second_visible_article_uses_stable_id_and_keeps_original_order(tmp_path, monkeypatch, entrypoint):
    chat, hits = wiki_result_fixture(tmp_path, monkeypatch)
    identity_model(chat)
    answer = chat.responder.answer if entrypoint == "legacy" else chat.answer
    first = answer("Compare insurance findings")
    assert [source["title"] for source in first["sources"]] == ["Beta", "Alpha"]
    chat.responder.kb.chunks.reverse()
    hits.reverse()  # Subsequent retrieval has a different order.
    second = answer("Tell me more about the second article above", context=first["context"])
    assert not second["needs_clarification"]
    assert [source["title"] for source in second["sources"]] == ["Alpha"]
    assert second["sources"][0]["evidence_id"] == first["sources"][1]["evidence_id"]
    assert "Alpha insurance finding" in second["text"] and "Beta insurance finding" not in second["text"]
    focused = answer("What are its findings?", context=second["context"])
    assert focused["sources"][0]["evidence_id"] == second["sources"][0]["evidence_id"]
    original_first = answer("Tell me more about the first article above", context=focused["context"])
    assert original_first["sources"][0]["evidence_id"] == first["sources"][0]["evidence_id"]
    chat.responder.kb.chunks = [chunk for chunk in chat.responder.kb.chunks if chunk.id != second["sources"][0]["evidence_id"]]
    removed = answer("Tell me more about the second article above", context=first["context"])
    assert not removed["sources"] and "no longer" in removed["text"]


def test_approved_article_second_result_resolves_current_record_by_id(tmp_path):
    chat = owner(tmp_path)
    identity_model(chat)
    class Reader(MeetingsReader):
        def knowledge_chronology(self):
            return {"article:" + key: {"first_ingested_at": "2026-10-08T12:00:00Z"} for key in ("beta", "alpha")}
        def articles(self, **kwargs):
            return {"items": self.records, "pagination": {"pages": 1}}
        def article(self, key):
            return next(copy.deepcopy(item) for item in self.records if item["article_id"] == key)
    reader = Reader([{"article_id": key, "title": key.title(), "publisher": "Publisher",
        "summary": key + " approved insurance findings", "canonical_url": "https://example.org/" + key,
        "published_candidate_sha256": "approved-" + key} for key in ("beta", "alpha")])
    chat.reader_factory = lambda: reader
    first = chat.answer("New reports and articles")
    assert [source["evidence_id"] for source in first["sources"]] == ["beta", "alpha"]
    reader.records.reverse()
    second = chat.answer("Tell me more about the second article above", context=first["context"])
    assert not second["needs_clarification"] and [source["evidence_id"] for source in second["sources"]] == ["alpha"]
    assert "alpha approved insurance findings" in second["text"]
    assert second["sources"][0]["version"] == "approved-alpha"


def test_model_article_enumeration_order_overrides_initial_retrieval_order(tmp_path, monkeypatch):
    chat, hits = wiki_result_fixture(tmp_path, monkeypatch)
    def script(index, request):
        if index == 1:
            return candidate_detail_calls(request)
        if index > 1:
            index -= 1
        return [
            [("search_knowledge", {"query": "insurance findings", "target": "wiki"})],
            "Summary [1][2].\n- Alpha [2]\n- Beta [1]",
            [("get_source_details", {"evidence_id": hits[0].chunk.id})],
            "Beta insurance findings. [1]"][index]
    scripted_provider(chat, monkeypatch, "openai", script)
    first = chat.answer("Compare insurance findings")
    second = chat.answer("Tell me more about the second article above", context=first["context"])
    assert not second["needs_clarification"] and second["sources"][0]["evidence_id"] == hits[0].chunk.id


def test_approved_chronology_survives_export_and_pending_changes(tmp_path, monkeypatch):
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        record_knowledge(db, kind="article", entity_id="a", source_kind="information_check", source_ref="one",
            fields={"summary": "baseline"}, evidence={}, recorded_at="2026-09-20T12:00:00Z")
        record_knowledge(db, kind="article", entity_id="a", source_kind="information_check", source_ref="same",
            fields={"summary": "baseline"}, evidence={}, recorded_at="2026-10-01T12:00:00Z")
        for sha in stage_entities(db):
            _approve(db, sha, {"basis": "fixture approval"}, status="accepted_legacy")
    reader = _reader(database, tmp_path)
    before = reader.knowledge_chronology()
    assert before["article:a"]["first_ingested_at"] == "2026-09-20T12:00:00Z"
    assert before["article:a"]["substantive_updated_at"] is None
    with sqlite3.connect(database) as db:
        record_knowledge(db, kind="article", entity_id="a", source_kind="information_check", source_ref="pending",
            fields={"summary": "unapproved material update"}, evidence={}, recorded_at="2026-10-02T12:00:00Z")
        stage_entities(db)
    assert reader.knowledge_chronology() == before
    output = tmp_path / "application/wiki/public-registry.json"
    output.parent.mkdir(parents=True)
    export_public_snapshot(database, output)
    assert json.loads(output.read_text())["knowledge_chronology"] == before
    monkeypatch.setenv("CLIMATE_REGISTRY_STATIC_SNAPSHOT", "1")
    assert _reader(database, tmp_path).knowledge_chronology() == before
    chat = owner(tmp_path)
    chat.reader_factory = lambda: reader
    turn = EvidenceTurn(chat, "New articles added in the last 14 days", None, [])
    turn.now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    turn.initial(None)
    assert "unknown chronology" in turn.extractive()
    assert "unapproved material update" not in turn.extractive()
    assert "last_seen" in turn.extractive()


def test_new_york_calendar_window_dst_and_future():
    now = datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc)  # November 1, 23:30 EST
    start, end = window("new articles", now)
    assert start.isoformat() == "2026-10-19T00:00:00-04:00"
    assert end.isoformat() == "2026-11-01T23:30:00-05:00"
    start, end = window("articles from 2026-10-25 to 2026-11-10", now)
    assert start.date().isoformat() == "2026-10-25" and end == now


def test_chinese_unspaced_explicit_recent_window_selects_only_requested_month(tmp_path):
    from contextlib import nullcontext

    chat = owner(tmp_path)
    class Reader:
        def public_snapshot(self): return nullcontext()
        def knowledge_chronology(self):
            return {"article:september": {"first_ingested_at": "2026-09-15T12:00:00Z"},
                "article:october": {"first_ingested_at": "2026-10-10T12:40:00Z"}}
        def articles(self, **kwargs):
            return {"items": [{"article_id": key, "title": "Climate insurance " + key,
                "summary": "Climate insurance findings"} for key in ("september", "october")],
                "pagination": {"pages": 1}}
        def pdf_articles_all(self): return []
        def article(self, key):
            return {"article_id": key, "title": "Climate insurance " + key, "summary": "Climate insurance findings",
                "publication_date": "2024-01-01", "published_candidate_sha256": "approved-" + key,
                "canonical_url": "https://example.org/" + key}

    chat.reader_factory = Reader
    turn = EvidenceTurn(chat, "最近新增的气候保险文章从2026-09-01到2026-09-30有哪些？", None, [])
    turn.now = datetime(2026, 10, 10, 13, 40, tzinfo=timezone.utc)
    turn.initial(None)

    assert [source["evidence_id"] for source in turn.sources] == ["september"]
    assert any("2026-09-01T00:00:00-04:00" in note and "2026-09-30T23:59:59.999999-04:00" in note for note in turn.notes)


@pytest.mark.usefixtures("mock_reader_policy")
def test_actual_url_body_reused_and_failure_is_honest(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    identity_model(chat)
    calls = []
    def read(key, url, **kwargs):
        calls.append((url, kwargs))
        return {"status": "ok", "content": "Consultation questions 3 and 7 concern insurers. Responses close 2027-04-20.",
            "final_url": url, "content_hash": "actual-body-hash"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    first = chat.answer("https://example.org/consultation")
    assert len(calls) == 1 and calls[0][1]["budget"]
    assert "questions 3 and 7" in first["text"] and first["sources"][0]["retrieved_at"]
    second = chat.answer("Which questions concern insurers?", context=first["context"])
    assert "questions 3 and 7" in second["text"] and len(calls) == 1
    third = chat.answer("Verify this URL https://example.org/consultation", context=second["context"])
    assert len(calls) == 2 and evidence_trace(third)[0]["status"] == "read"
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw: {"status": "failed", "failure_reason": "HTTP 403"})
    failed = chat.answer("https://example.org/blocked")
    assert "403" in failed["text"] and not failed["sources"]
    assert evidence_trace(failed)[0]["status"] == "failed"


def test_tool_budget_and_provider_results_return_to_model(tmp_path, monkeypatch):
    chat = owner(tmp_path)
    responses = []
    class Message:
        def __init__(self, calls=None, content=None):
            self.tool_calls, self.content = calls, content
        def model_dump(self, **kwargs):
            return {"role": "assistant", "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in self.tool_calls]}
    call = SimpleNamespace(id="tool-one", function=SimpleNamespace(name="search_knowledge", arguments='{"query":"insurance pricing"}'))
    class Client:
        def with_options(self, **kwargs):
            assert kwargs["max_retries"] == 0 and kwargs["timeout"] <= 20
            return self
        def create(self, **kwargs):
            assert kwargs["reasoning_effort"] == "none"
            responses.append(kwargs)
            if len(responses) == 1:
                planned = SimpleNamespace(id="plan", function=SimpleNamespace(name="research_state", arguments=json.dumps({"action":"plan","time_basis":"retained findings","required_outputs":[{"id":"answer","task":"pricing finding","requires_current_body":False}]})))
                message = Message([planned])
            elif len(responses) == 3:
                name, arguments = candidate_detail_calls(kwargs)[0]
                detail = SimpleNamespace(id="tool-detail", function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))
                message = Message([detail])
            else:
                message = Message([call]) if len(responses) == 2 else Message(content="Climate hazards affect insurance pricing. [1]")
            if len(responses) == 5:
                finish = SimpleNamespace(id="finish", function=SimpleNamespace(name="research_state", arguments=json.dumps({"action":"finish","results":[{"id":"answer","status":"supported","evidence_ids":[chat.responder.kb.chunks[0].id]}]})))
                message = Message([finish])
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length" if len(responses) == 4 else "tool_calls", message=message)])
    client = Client()
    client.chat = SimpleNamespace(completions=client)
    chat.responder.client = client
    result = chat.answer("Explain insurance pricing")
    assert result["agent_mode"] == "openai" and len(responses) == 6
    assert "output budget reached" in result["text"] and "partial" in result["text"]
    tool_message = next(message for message in responses[-1]["messages"] if message["role"] == "tool" and message["tool_call_id"] == "tool-one")
    assert "Climate hazards affect insurance pricing" in tool_message["content"]
    assert tool_message["tool_call_id"] == "tool-one"
    turn = EvidenceTurn(chat, "insurance", None, [])
    for i in range(MAX_TOOLS + 2):
        result = turn.execute("read_url", {"url": "invalid-" + str(i)})
    assert turn.calls == MAX_TOOLS and result["status"] == "budget_exhausted"
    turn.deadline = 0
    assert turn.execute("search_knowledge", {"query": "insurance"})["status"] == "budget_exhausted"


def test_client_context_api_roundtrip_and_legacy_payload(monkeypatch):
    seen = []
    monkeypatch.setattr(api_server.responder, "answer", lambda question, **kwargs: seen.append(kwargs) or {"text": "ok", "sources": []})
    client = TestClient(api_server.app)
    assert client.post("/api/chat", json={"messages": [{"role": "user", "content": "insurance"}]}).status_code == 200
    assert "context" not in seen[-1]
    assert client.post("/api/chat", json={"messages": [
        {"role": "assistant", "content": "truncated", "context": "opaque-server-token"},
        {"role": "user", "content": "its details"}]}).status_code == 200
    assert seen[-1]["context"] == "opaque-server-token"


@pytest.mark.usefixtures("mock_reader_policy")
def test_search_reads_candidates_and_does_not_cite_snippets(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    class Client:
        def with_options(self, **kwargs):
            return self
        def create(self, **kwargs):
            assert kwargs["max_tool_calls"] == 1
            assert kwargs["include"] == ["web_search_call.action.sources"]
            return SimpleNamespace(model_dump=lambda: {"output": [
                {"type": "web_search_call", "status": "completed", "action": {"sources": [{"url": "https://example.org/agenda"}]}},
                {"content": [{"text": "Search snippet alone says unsupported eligibility",
                    "annotations": []}]}]})
    client = Client()
    client.responses = client
    chat.responder.client = client
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw:
        {"status": "ok", "content": "Read body: agenda opens with insurance risk.", "final_url": "https://example.org/agenda", "content_hash": "body-hash"})
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        turn = EvidenceTurn(chat, "Find the latest agenda", None, [])
        turn.runtime_dir = Path(directory)
        result = turn.execute("search_web", {"query": "latest agenda"})
        assert not turn.reads and not turn.sources and not result["read_results"]
        read_result = turn.execute("read_url", {"url": result["candidate_urls"][0]})
    assert result["status"] == "searched"
    assert read_result["body"].startswith("Read body")
    assert turn.sources[0]["url"] == "https://example.org/agenda"
    assert "unsupported eligibility" not in json.dumps(turn.evidence)
    second = turn.execute("search_web", {"query": "another agenda"})
    assert second["status"] == "no_new_candidates" and second["new_candidate_count"] == 0
    assert second["candidate_urls"] == result["candidate_urls"]
    assert turn.execute("search_web", {"query": "third agenda"})["status"] == "unavailable"


def test_unconfigured_database_uses_verified_static_export(tmp_path):
    from climate_registry.acquisition_review import digest
    from climate_registry.read_api import RegistryReader, RegistryContractError
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    artifact = {"schema_version": "climate-public-snapshot.v1", "articles": [{"article_id": "old",
        "title": "Archived approved report", "summary": "Insurers face physical risk", "publisher": "Publisher",
        "publication_date": "2001-01-01", "last_seen": "2026-10-08", "canonical_url": "https://example.org/old"}],
        "meeting_records": [meeting("one", "Approved conference", "Paris")], "meeting_coverage": {"status": "partial"}}
    artifact["artifact_sha256"] = digest(artifact)
    path = wiki / "public-registry.json"
    path.write_text(json.dumps(artifact))
    before = path.read_bytes()
    reader = RegistryReader.from_public_export(tmp_path)
    assert reader.database is None and reader.knowledge_chronology() == {}
    assert reader.article("old")["publication_date"] == "2001-01-01"
    assert reader.articles()["items"][0]["article_id"] == "old"
    assert reader.meetings(base_date="2026-10-08")["items"][0]["event_id"] == "one"
    assert path.read_bytes() == before
    artifact["articles"][0]["summary"] = "tampered"
    path.write_text(json.dumps(artifact))
    with pytest.raises(RegistryContractError):
        RegistryReader.from_public_export(tmp_path)


def test_meeting_type_and_location_filters_are_useful_offline(tmp_path):
    chat = owner(tmp_path)
    records = [meeting("one", "Conference in Paris", "Paris"), meeting("two", "Conference in New York", "New York"),
        {**meeting("review", "IPCC expert review", "Online"), "deadline_type": "expert_review"}]
    chat.reader_factory = lambda: MeetingsReader(records)
    answer = chat.answer("Upcoming conferences in Paris")
    assert "Conference in Paris" in answer["text"] and "Conference in New York" not in answer["text"]
    assert "IPCC expert review" not in answer["text"]


def test_meeting_filters_are_conjunctive_and_disclose_unknown_coverage(tmp_path):
    paris = {**meeting("offline-paris", "Paris conference", "Paris"), "event_type": "conference",
        "organizer": "OECD", "event_timezone": "Europe/Paris", "online_url": None}
    virtual = {**meeting("online-ny", "Virtual conference", "Virtual"), "event_type": "conference",
        "organizer": "IPCC", "event_timezone": "America/New_York", "online_url": "https://example.org/join"}
    onsite_registration = {**meeting("onsite-registration", "Onsite conference", "Paris conference venue"),
        "event_type": "conference", "event_timezone": "Europe/Paris", "online_url": "https://example.org/register"}
    online_no_link = {**meeting("online-no-link", "No-link virtual meeting", "Online"),
        "event_type": "conference", "event_timezone": "Europe/Paris", "online_url": None}
    unknown = {**meeting("unknown", "Unspecified conference", None), "event_type": "conference",
        "organizer": None, "event_timezone": None, "online_url": None}
    chat = owner(tmp_path)
    chat.reader_factory = lambda: MeetingsReader([paris, virtual, onsite_registration, online_no_link, unknown])

    mismatch = EvidenceTurn(chat, "List upcoming conferences organized by IPCC in Paris", None, [])
    assert mismatch.search_knowledge(mismatch.question, target="meetings") == []
    scope = next(note for note in mismatch.notes if note.startswith("Meeting query scope:"))
    assert '"organizer": ["IPCC"]' in scope and '"location": ["Paris"]' in scope
    assert any("Location filter coverage: 1 meetings" in note for note in mismatch.notes)

    absent = EvidenceTurn(chat, "List upcoming conferences organized by IPCC in Berlin", None, [])
    assert absent.search_knowledge(absent.question, target="meetings") == []
    scope = next(note for note in absent.notes if note.startswith("Meeting query scope:"))
    assert '"organizer": ["IPCC"]' in scope and '"location": ["Berlin"]' in scope

    online = EvidenceTurn(chat, "List upcoming online conferences", None, [])
    online_results = online.search_knowledge(online.question, target="meetings")
    assert [row["source"]["evidence_id"] for row in online_results] == ["online-ny", "online-no-link"]
    assert all(row["source"]["evidence_id"] != "onsite-registration" for row in online_results)
    assert any("Online-only filter coverage: 1 meetings" in note for note in online.notes)

    zone = EvidenceTurn(chat, "List upcoming conferences whose event timezone is America/New_York", None, [])
    assert [row["source"]["evidence_id"] for row in zone.search_knowledge(zone.question, target="meetings")] == ["online-ny"]
    assert any("Event-timezone filter coverage: 1 meetings" in note for note in zone.notes)

    online_zone = EvidenceTurn(chat, "List upcoming online meetings in America/New_York", None, [])
    assert [row["source"]["evidence_id"] for row in online_zone.search_knowledge(online_zone.question, target="meetings")] == ["online-ny"]
    scope = next(note for note in online_zone.notes if note.startswith("Meeting query scope:"))
    assert '"event_timezone": ["America/New_York"]' in scope
    assert '"location"' not in scope


@pytest.mark.parametrize("question,field,value,event_date", [
    ("List conferences organized by IAIS from 2026-11-01 to 2026-11-30", "organizer", "IAIS", "2026-11-12"),
    ("List conferences from 2026-11-01 to 2026-11-30 organized by IAIS", "organizer", "IAIS", "2026-11-12"),
    ("List conferences in Paris from 2027-06-01 to 2027-06-30", "location", "Paris", "2027-06-10"),
    ("List conferences from 2027-06-01 to 2027-06-30 in Paris", "location", "Paris", "2027-06-10"),
    ("List conferences organized by IAIS 从2026-11-01到2026-11-30", "organizer", "IAIS", "2026-11-12"),
])
def test_meeting_date_ranges_remain_separate_from_explicit_filters(tmp_path, question, field, value, event_date):
    item = {**meeting("range-match", "IAIS Annual Conference", "Paris"), "organizer": "IAIS",
        "event_type": "conference", "start_date": event_date, "end_date": event_date, "date_precision": "day"}
    chat = owner(tmp_path)
    chat.reader_factory = lambda: MeetingsReader([item])
    turn = EvidenceTurn(chat, question, None, [])
    assert [row["source"]["evidence_id"] for row in turn.search_knowledge(question, target="meetings")] == ["range-match"]
    scope = next(note for note in turn.notes if note.startswith("Meeting query scope:"))
    assert f'"{field}": ["{value}"]' in scope
    assert '"event_or_deadline_window"' in scope


@pytest.mark.parametrize("raw_time_text", ["09:00–10:00 EDT", "09:00–10:00 UTC-04:00"])
def test_upcoming_meeting_uses_known_same_day_end_time_and_query_instant(tmp_path, raw_time_text):
    item = {**meeting("morning", "Morning conference", "Virtual"), "event_type": "conference",
        "start_date": "2026-10-10", "end_date": "2026-10-10", "date_precision": "day",
        "raw_time_text": raw_time_text, "event_timezone": "America/New_York", "online_url": "https://example.org/join"}
    chat = owner(tmp_path)
    chat.reader_factory = lambda: MeetingsReader([item])

    before = EvidenceTurn(chat, "List upcoming conferences", None, [])
    before.now = datetime(2026, 10, 10, 13, 0, tzinfo=timezone.utc)  # 09:00 EDT
    assert [row["source"]["evidence_id"] for row in before.search_knowledge(before.question, target="meetings")] == ["morning"]

    after = EvidenceTurn(chat, "List upcoming conferences", None, [])
    after.now = datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)  # 10:00 EDT
    assert after.search_knowledge(after.question, target="meetings") == []
    assert any("Excluded 1 meeting(s)" in note and "end time" in note for note in after.notes)

    item["event_timezone"] = None
    unknown = EvidenceTurn(chat, "List upcoming conferences", None, [])
    unknown.now = after.now
    assert [row["source"]["evidence_id"] for row in unknown.search_knowledge(unknown.question, target="meetings")] == ["morning"]
    assert any("timing is uncertain" in note for note in unknown.notes)

    item["start_date"] = "2026-10-09"  # A multi-day meeting still ends today.
    item["event_timezone"] = "America/New_York"
    multi_day = EvidenceTurn(chat, "List upcoming conferences", None, [])
    multi_day.now = after.now
    assert [row["source"]["evidence_id"] for row in multi_day.search_knowledge(multi_day.question, target="meetings")] == []
    assert any("Excluded 1 meeting(s)" in note for note in multi_day.notes)

    item["event_timezone"] = None
    uncertain = EvidenceTurn(chat, "List upcoming conferences", None, [])
    uncertain.now = after.now
    assert [row["source"]["evidence_id"] for row in uncertain.search_knowledge(uncertain.question, target="meetings")] == ["morning"]
    assert any("timing is uncertain" in note for note in uncertain.notes)


def test_upcoming_meeting_parses_pm_end_time(tmp_path):
    from agentic_wiki.chat_evidence import meeting_end

    item = {**meeting("evening", "Evening conference", "Virtual"), "event_type": "conference",
        "start_date": "2026-10-10", "end_date": "2026-10-10", "date_precision": "day",
        "raw_time_text": "09:00-10:00 PM", "event_timezone": "America/New_York"}
    assert meeting_end(item).isoformat() == "2026-10-10T22:00:00-04:00"
    chat = owner(tmp_path)
    chat.reader_factory = lambda: MeetingsReader([item])
    before = EvidenceTurn(chat, "List upcoming conferences", None, [])
    before.now = datetime(2026, 10, 10, 14, tzinfo=timezone.utc)
    assert [row["source"]["evidence_id"] for row in before.search_knowledge(before.question, target="meetings")] == ["evening"]
    after = EvidenceTurn(chat, "List upcoming conferences", None, [])
    after.now = datetime(2026, 10, 11, 2, tzinfo=timezone.utc)
    assert after.search_knowledge(after.question, target="meetings") == []


def test_upcoming_keeps_western_event_running_after_new_york_midnight(tmp_path):
    item = {**meeting("la", "Los Angeles conference", "Los Angeles"), "event_type": "conference",
        "start_date": "2026-10-10", "end_date": "2026-10-10", "date_precision": "day",
        "raw_time_text": "21:00–23:00 PDT", "event_timezone": "America/Los_Angeles"}
    class DateFilteredReader(MeetingsReader):
        def meetings(self, **kwargs):
            self.base_date = kwargs["base_date"]
            items = [row for row in self.records if row.get("end_date", "9999") >= self.base_date]
            return {"items": copy.deepcopy(items), "pagination": {"pages": 1}, "coverage": {"status": "partial"}}

    reader = DateFilteredReader([item])
    chat = owner(tmp_path)
    chat.reader_factory = lambda: reader
    turn = EvidenceTurn(chat, "List upcoming conferences", None, [])
    turn.now = datetime(2026, 10, 11, 4, 30, tzinfo=timezone.utc)  # October 10, 21:30 PDT.
    result = turn.search_knowledge(turn.question, target="meetings")

    assert reader.base_date == "2026-10-10"
    assert [row["source"]["evidence_id"] for row in result] == ["la"]
    assert "Opportunity timing: ended" not in result[0]["text"]
    assert '"business_date_lower": "2026-10-10"' in next(note for note in turn.notes if note.startswith("Meeting query scope:"))


def test_anthropic_native_tool_results_and_provider_preference(tmp_path, monkeypatch):
    chat = owner(tmp_path)
    requests = []
    class Client:
        def with_options(self, **kwargs):
            assert kwargs["max_retries"] == 0
            return self
        def create(self, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            if len(requests) == 1:
                content = [{"type":"tool_use","id":"plan","name":"research_state","input":{"action":"plan","time_basis":"retained findings","required_outputs":[{"id":"answer","task":"pricing finding","requires_current_body":False}]}}]
            elif len(requests) == 3:
                name, arguments = candidate_detail_calls(kwargs)[0]
                content = [{"type": "tool_use", "id": "claude-detail", "name": name, "input": arguments}]
            else:
                content = [{"type": "tool_use", "id": "claude-tool", "name": "search_knowledge", "input": {"query": "insurance pricing"}}] if len(requests) == 2 else [{"type": "text", "text": "Climate hazards affect pricing. [1]"}]
            if len(requests) == 5:
                content = [{"type":"tool_use","id":"finish","name":"research_state","input":{"action":"finish","results":[{"id":"answer","status":"supported","evidence_ids":[chat.responder.kb.chunks[0].id]}]}}]
            return SimpleNamespace(model_dump=lambda: {"content": content, "stop_reason": "max_tokens" if len(requests) == 4 else "tool_use"})
    client = Client()
    client.messages = client
    chat.responder.anthropic_client = client
    chat.responder.anthropic_model = "configured-claude-model"
    assert chat.provider == "anthropic"
    chat.responder.client = object()
    assert chat.provider == "openai"
    monkeypatch.setenv("CLIMATE_CHAT_PROVIDER", "anthropic")
    result = chat.answer("Explain insurance pricing")
    assert result["agent_mode"] == "anthropic" and result["model"] == "configured-claude-model"
    assert "output budget reached" in result["text"]
    assert len(requests) == 6 and "input_schema" in requests[0]["tools"][0]
    tool = requests[2]["messages"][-1]["content"][0]
    assert tool["type"] == "tool_result" and tool["tool_use_id"] == "claude-tool"
    assert "Climate hazards affect insurance pricing" in tool["content"]
    def invalid(**kwargs):
        raise RuntimeError("Unauthorized")
    client.create = invalid
    failed = chat.answer("Explain insurance pricing")
    assert failed["agent_mode"] == "offline" and "Model unavailable" in failed["text"]
    assert chat.responder.client is not None  # A failing Claude key never disables OpenAI.


def test_compose_forwards_native_claude_configuration():
    import yaml
    environment = yaml.safe_load(Path("docker-compose.yml").read_text())["services"]["wiki"]["environment"]
    assert environment["ANTHROPIC_API_KEY"] == "${ANTHROPIC_API_KEY:-}"
    assert environment["ANTHROPIC_MODEL"] == "${ANTHROPIC_MODEL:-}"
    assert environment["CLIMATE_CHAT_PROVIDER"] == "${CLIMATE_CHAT_PROVIDER:-}"
    assert environment["ANTHROPIC_BASE_URL"] == "${ANTHROPIC_BASE_URL:-https://api.anthropic.com}"


def test_native_claude_sdk_environment_and_messages_contract(tmp_path, monkeypatch):
    import anthropic
    import httpx
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("CLIMATE_CHAT_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    requests = []

    def handle(request):
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "test-only-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["model"] == "claude-haiku-5-5"
        assert isinstance(payload["system"], str)
        assert all(item["role"] != "system" for item in payload["messages"])
        if len(requests) == 1:
            content = [{"type":"tool_use","id":"plan","name":"research_state","input":{"action":"plan","time_basis":"retained findings","required_outputs":[{"id":"answer","task":"pricing finding","requires_current_body":False}]}}]
        elif len(requests) == 3:
            name, arguments = candidate_detail_calls(payload)[0]
            content = [{"type": "tool_use", "id": "native-detail", "name": name, "input": arguments}]
        else:
            content = ([{"type": "tool_use", "id": "native-tool", "name": "search_knowledge", "input": {"query": "insurance pricing"}}]
                if len(requests) == 2 else [{"type": "text", "text": "Climate hazards affect pricing. [1]"}])
        if len(requests) == 5:
            read_id = candidate_detail_calls(requests[2])[0][1]["evidence_id"]
            content = [{"type":"tool_use","id":"finish","name":"research_state","input":{"action":"finish","results":[{"id":"answer","status":"supported","evidence_ids":[read_id]}]}}]
        return httpx.Response(200, json={"id": "mock-message", "type": "message", "role": "assistant",
            "model": "claude-haiku-5-5", "content": content,
            "stop_reason": "tool_use" if len(requests) in {1,2,3,5} else "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10}})

    sdk = anthropic.Anthropic(http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(anthropic, "Anthropic", lambda: sdk)
    result = owner(tmp_path).answer("Explain insurance pricing")
    assert result["agent_mode"] == "anthropic" and len(requests) == 6
    assert requests[2]["messages"][-1]["content"][0]["type"] == "tool_result"
    assert requests[2]["messages"][-1]["content"][0]["tool_use_id"] == "native-tool"
    assert "Climate hazards affect insurance pricing" in requests[2]["messages"][-1]["content"][0]["content"]
    sdk.close()


@pytest.mark.usefixtures("mock_reader_policy")
def test_anthropic_search_candidates_use_same_governed_reader(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    class Client:
        def with_options(self, **kwargs):
            return self
        def create(self, **kwargs):
            assert kwargs["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}]
            return SimpleNamespace(model_dump=lambda: {"content": [{"type": "web_search_tool_result",
                "content": [{"type": "web_search_result", "url": "https://example.org/claude-search"}]}]})
    client = Client()
    client.messages = client
    chat.responder.anthropic_client = client
    chat.responder.anthropic_model = "configured-claude-model"
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw:
        {"status": "ok", "content": "Actual governed page body", "final_url": a[1], "content_hash": "read-body"})
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        turn = EvidenceTurn(chat, "Find latest agenda", None, [])
        turn.runtime_dir = Path(directory)
        result = turn.execute("search_web", {"query": "agenda"})
        assert not turn.sources and not turn.reads
        turn.execute("read_url", {"url": result["candidate_urls"][0]})
    assert result["provider"] == "anthropic" and result["status"] == "searched"
    assert turn.sources[0]["version"] == "read-body" and turn.models == 1


@pytest.mark.parametrize("selected", ["anthropic", "unsupported-provider"])
def test_explicit_unavailable_provider_never_falls_back_to_paid_openai(tmp_path, monkeypatch, selected):
    chat = owner(tmp_path)
    class ForbiddenClient:
        def with_options(self, **kwargs):
            pytest.fail("Explicit provider selection must not silently use OpenAI")
    chat.responder.client = ForbiddenClient()
    chat.responder.anthropic_client = None
    monkeypatch.setenv("CLIMATE_CHAT_PROVIDER", selected)
    assert chat.client is None
    assert chat.responder.config()["agent_mode"] == "offline"
    answer = chat.responder.answer("Explain insurance pricing")
    assert answer["agent_mode"] == "offline" and answer["sources"]
    assert "no autonomous live search" in answer["text"]


@pytest.mark.parametrize("language", ["zh", "es"])
def test_existing_chat_api_is_english_only(language):
    response = TestClient(api_server.app).post("/api/chat", json={"message": "insurance", "language": language})
    assert response.status_code == 422


@pytest.mark.usefixtures("mock_reader_policy")
def test_generic_search_then_open_url_is_not_a_previous_citation_request(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    calls = []
    wiki_queries = []
    original_search = chat.responder.kb.search
    def wiki_search(query, **kwargs):
        wiki_queries.append(query)
        return original_search(query, **kwargs)
    monkeypatch.setattr(chat.responder.kb, "search", wiki_search)
    class Client:
        autonomous = True
        def with_options(self, **kwargs):
            return self
        def create(self, **kwargs):
            if "input" in kwargs:
                calls.append("search")
                return SimpleNamespace(model_dump=lambda: {"output": [
                    {"type": "web_search_call", "status": "completed"},
                    {"content": [{"annotations": [{"type": "url_citation", "url": "https://example.org/official"}]}]}]})
            calls.append("model")
            stage = calls.count("model")
            if stage in {1,2,3,5}:
                assert "tools" in kwargs
                read_ids = [json.loads(message["content"])["source"]["evidence_id"] for message in kwargs["messages"]
                    if message["role"] == "tool" and json.loads(message["content"]).get("source")]
                name, arguments = {
                    1: ("research_state", {"action":"plan","time_basis":"current official body","required_outputs":[{"id":"answer","task":"official consultation facts","requires_current_body":True}]}),
                    2: ("search_web", {"query":"official climate consultation"}),
                    3: ("read_url", {"url":"https://example.org/official"}),
                    5: ("research_state", {"action":"finish","results":[{"id":"answer","status":"supported","evidence_ids":read_ids}]})}[stage]
                call = SimpleNamespace(id="call-"+str(stage), function=SimpleNamespace(name=name,arguments=json.dumps(arguments)))
                message = SimpleNamespace(tool_calls=[call],content=None,model_dump=lambda **kw:{"role":"assistant","tool_calls":[{"type":"function","id":call.id,"function":{"name":name,"arguments":call.function.arguments}}]})
            else:
                assert "Actual official body" in json.dumps(kwargs["messages"])
                message = SimpleNamespace(tool_calls=[],content="Actual official body. [1]")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
    client = Client()
    client.chat = SimpleNamespace(completions=client)
    client.responses = client
    chat.responder.client = client
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw:
        {"status": "ok", "content": "Actual official body", "final_url": a[1], "content_hash": "official-read"})
    result = chat.answer("Search the web now and open one official source URL through the reader")
    assert not result["needs_clarification"] and result["agent_mode"] == "openai"
    assert calls == ["model", "model", "search", "model", "model", "model", "model"]
    assert wiki_queries == []  # Explicit web research preserves its evidence budget for actual URL bodies.
    assert result["sources"][0]["version"] == "official-read"
    identity_model(chat)
    prior = chat.answer("Read source 1", context=result["context"])
    assert not prior["needs_clarification"] and prior["sources"][0]["url"] == "https://example.org/official"
    calls.clear()
    chat.responder.client = client
    client.autonomous = True
    autonomous = chat.answer("Explain official climate consultation requirements missing from the knowledge base")
    assert autonomous["agent_mode"] == "openai" and calls == ["model", "model", "search", "model", "model", "model", "model"]


def test_tracking_urls_and_invalid_paths_preserve_reader_budget(tmp_path, monkeypatch):
    pytest.importorskip("web_listening.request.scope", reason="Real governed URL policy requires the Python >=3.12 reader dependency")
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    sent = []
    def read(key, url, **kwargs):
        sent.append(url)
        if "required=yes" in url:
            return {"status": "failed", "failure_reason": "web_http.url_redacted"}
        return {"status": "ok", "content": "Official HTML body", "final_url": url, "content_hash": "body"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        turn = EvidenceTurn(chat, "read URL", None, [])
        turn.runtime_dir = Path(directory)
        bad = turn.execute("read_url", {"url": "https://example.org/report%20name.pdf?utm_source=openai"})
        assert bad["status"] == "invalid" and "scope.path_escape" in bad["reason"]
        assert turn.reads == 0 and not sent
        result = turn.execute("read_url", {"url": "https://example.org/meeting/?utm_source=openai"})
        assert result["status"] == "read" and sent == ["https://example.org/meeting/"]
        assert result["source"]["requested_url"].endswith("?utm_source=openai")
        assert result["source"]["reader_url"] == result["source"]["final_url"] == "https://example.org/meeting/"
        failed = turn.execute("read_url", {"url": "https://example.org/meeting/?required=yes&utm_source=openai"})
        assert sent[-1].endswith("?required=yes") and failed["status"] == "failed"
        assert turn.reads == 2  # Functional parameters are retained and real failures spend budget.


@pytest.mark.usefixtures("mock_reader_policy")
def test_automatic_corpus_retrieval_reserves_space_for_real_url_body(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter
    from agentic_wiki.chat_evidence import MAX_TEXT
    chat = owner(tmp_path)
    chat.responder.client = object()
    hit = chat.responder.kb.search("insurance", top_k=1)[0]
    hit.chunk.text = "Broad existing evidence. " * 2000
    monkeypatch.setattr(chat.responder.kb, "search", lambda *a, **kw: [hit])
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw:
        {"status": "ok", "content": "Verified page-only eligibility fact.", "final_url": a[1], "content_hash": "actual-page"})
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        turn = EvidenceTurn(chat, "Explain the requirements", None, [])
        turn.runtime_dir = Path(directory)
        turn.initial(None)
        assert turn.chars <= 1400  # Compact search leaves room for page verification and detail expansion.
        result = turn.execute("read_url", {"url": "https://example.org/requirements"})
    assert result["status"] == "read" and result["source"]["version"] == "actual-page"
    assert "Verified page-only eligibility fact" in turn.evidence[-1]["text"]


@pytest.mark.usefixtures("mock_reader_policy")
def test_captured_real_responses_sources_regresses_empty_search_parser(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter
    fixture = json.loads(Path("tests/fixtures/chat_search_action_sources.json").read_text())
    # The previous annotation-only parser returned empty for this actual successful response.
    old_urls = [annotation["url"] for output in fixture["output"] for content in output.get("content", [])
        for annotation in content.get("annotations", []) if annotation.get("type") == "url_citation"]
    assert old_urls == []
    chat = owner(tmp_path)
    class Client:
        def with_options(self, **kwargs):
            return self
        def create(self, **kwargs):
            assert kwargs["include"] == ["web_search_call.action.sources"] and kwargs["max_tool_calls"] == 1
            return SimpleNamespace(model_dump=lambda: fixture)
    client = Client()
    client.responses = client
    chat.responder.client = client
    sent = []
    def read(key, url, **kwargs):
        sent.append(url)
        return {"status": "ok", "content": "Verified source body states an AR7 milestone.",
            "final_url": url, "content_hash": "fixture-page-body"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        turn = EvidenceTurn(chat, "Find official IPCC milestones", None, [])
        turn.runtime_dir = Path(directory)
        result = turn.execute("search_web", {"query": "IPCC AR7 milestones"})
    from agentic_wiki.chat_evidence import MAX_READS
    assert result["status"] == "searched" and not sent and not turn.sources
    assert result["candidate_urls"]  # Structured candidates await the model's explicit governed read.
    assert all(source["version"] == "fixture-page-body" for source in turn.sources)


def test_future_meeting_business_window_keeps_knowledge_time_separate(tmp_path):
    chat = owner(tmp_path)
    records = [meeting("one", "June conference", "Paris"), meeting("two", "July conference", "New York")]
    records[1]["start_date"] = "2027-07-10"
    reader = MeetingsReader(records)
    chat.reader_factory = lambda: reader
    result = chat.answer("List conferences from 2027-06-01 to 2027-06-30")
    assert [source["evidence_id"] for source in result["sources"]] == ["one"]
    assert "First added: 2020-01-01" in result["text"] and "older item" in result["text"]
    with pytest.raises(ValueError):
        window("new articles from 2027-06-01 to 2027-06-30", datetime(2026, 10, 8, tzinfo=timezone.utc))


@pytest.mark.usefixtures("mock_reader_policy")
def test_explicit_url_wins_over_previous_meeting_reference(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    chat.reader_factory = lambda: MeetingsReader([meeting("one", "First conference", "Paris"), meeting("two", "Second conference", "New York")])
    first = chat.answer("List upcoming conferences")
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw:
        {"status": "ok", "content": "Consultation requirement: provide cited evidence.", "final_url": a[1], "content_hash": "consultation"})
    result = chat.answer("Read this URL and explain the consultation requirements: https://example.org/consultation", context=first["context"])
    assert not result["needs_clarification"] and "provide cited evidence" in result["text"]
    assert [row["tool"] for row in evidence_trace(result)] == ["read_url"]


def test_model_visible_meeting_order_binds_ordinal_to_stable_ids(tmp_path):
    chat = owner(tmp_path)
    reader = MeetingsReader([meeting("one", "First conference", "Paris"), meeting("two", "Second conference", "New York")])
    chat.reader_factory = lambda: reader
    scripted_provider(chat, None, "openai", lambda index, request: [
        [("search_knowledge", {"query": "List upcoming conferences", "target": "meetings"})],
        [("get_meeting_details", {"event_id": "one"}), ("get_meeting_details", {"event_id": "two"})],
        "1. Second conference [2]\n2. First conference [1]",
        [("get_meeting_details", {"event_id": "one"})], "First conference [1]",
        [("get_meeting_details", {"event_id": "two"})], "Second conference [1]"][index])
    first = chat.answer("List upcoming conferences")
    reader.records.reverse()
    result = chat.answer("Tell me more about the second meeting above", context=first["context"], history=[{"role": "assistant", "content": "truncated"}])
    assert [source["evidence_id"] for source in result["sources"]] == ["one"]
    assert "First conference" in result["text"] and "Second conference" not in result["text"]
    assert chat.frames.get(result["context"])["ordered"] == ["two", "one"]
    third = chat.answer("Tell me more about the first meeting above", context=result["context"])
    assert third["sources"][0]["evidence_id"] == "two"


def test_real_summary_citations_do_not_override_visible_meeting_bullets(tmp_path):
    fixture = json.loads(Path("tests/fixtures/chat_meeting_visible_order.json").read_text(encoding="utf-8-sig"))
    chat = owner(tmp_path)
    reader = MeetingsReader([meeting(row["evidence_id"], row["title"], "unknown") for row in fixture["sources"]])
    chat.reader_factory = lambda: reader
    scripted_provider(chat, None, "openai", lambda index, request: [
        [("search_knowledge", {"query": "List upcoming conferences", "target": "meetings"})],
        [("get_meeting_details", {"event_id": row["evidence_id"]}) for row in fixture["sources"]], fixture["answer"],
        [("get_meeting_details", {"event_id": fixture["expected_visible_ids"][1]})], "Selected meeting [1]"][index])
    first = chat.answer("List two upcoming conferences")
    assert chat.frames.get(first["context"])["ordered"] == fixture["expected_visible_ids"]
    reader.records.reverse()
    second = chat.answer("Tell me more about the second meeting above", context=first["context"], history=[{"role": "assistant", "content": "truncated"}])
    assert evidence_trace(second)[0]["target"] == fixture["expected_visible_ids"][1]
    assert second["sources"][0]["evidence_id"] == fixture["expected_visible_ids"][1]
    assert chat.frames.get(second["context"])["ordered"] == fixture["expected_visible_ids"]


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_page_tool_text_shared_budget_across_model_messages(tmp_path, monkeypatch, provider):
    from climate_monitor import article_content_adapter
    from agentic_wiki.chat_evidence import MAX_TEXT, MAX_READS
    chat = owner(tmp_path)
    monkeypatch.setenv("CLIMATE_CHAT_PROVIDER", provider)
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw:
        {"status": "ok", "content": "PAGE_BODY" * MAX_TEXT, "final_url": a[1], "content_hash": a[1]})
    requests = scripted_provider(chat, monkeypatch, provider, lambda index, request:
        [("read_url", {"url": f"https://example.org/page{i}"}) for i in range(MAX_READS)] if index == 0 else "Confirmed page evidence [2].")
    result = chat.answer("Explain insurance pricing and read the requested pages")
    raw_results = []
    for message in requests[-1]["messages"]:
        if message["role"] == "tool": raw_results.append(message["content"])
        elif isinstance(message.get("content"), list):
            raw_results.extend(block["content"] for block in message["content"] if block["type"] == "tool_result")
    assert sum(text.count("PAGE_BODY") * len("PAGE_BODY") for text in raw_results) <= MAX_TEXT
    payloads = []
    for message in requests[-1]["messages"]:
        if message["role"] == "tool": payloads.append(json.loads(message["content"]))
        elif isinstance(message.get("content"), list):
            payloads.extend(json.loads(block["content"]) for block in message["content"] if block["type"] == "tool_result")
    def text_count(value):
        if isinstance(value, dict): return sum(len(item) if key in {"body", "text", "snippet"} and isinstance(item, str) else text_count(item) for key, item in value.items())
        if isinstance(value, list): return sum(map(text_count, value))
        return 0
    assert sum(map(text_count, payloads)) <= MAX_TEXT
    assert result["agent_mode"] == provider and result["sources"][0]["url"] == "https://example.org/page0"
    assert "partial" in result["text"].lower() or "incomplete" in result["text"].lower()


@pytest.mark.usefixtures("mock_reader_policy")
def test_offline_deadline_followup_requires_current_explicit_source(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    reads = []
    def read(key, url, **kwargs):
        reads.append(url)
        return {"status": "ok", "content": "Registration deadline: 2027-04-20.", "final_url": url, "content_hash": "deadline-body"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    first = chat.answer("Read https://example.org/registration")
    lookups = []
    chat.reader_factory = lambda: lookups.append("unrelated meeting lookup") or MeetingsReader([])
    result = chat.answer("What is its registration deadline?", context=first["context"])
    assert "2027-04-20" not in result["text"] and not result["sources"]
    assert result["needs_clarification"] and "Manual verification" in result["text"]
    assert reads == ["https://example.org/registration"] and not lookups
    # A current explicit approved name remains available without inheriting the earlier page.
    reader = MeetingsReader([meeting("one", "First conference", "Paris")])
    chat.reader_factory = lambda: reader
    listed = chat.answer("List upcoming conferences")
    detail = chat.answer("Show First conference meeting details", context=listed["context"])
    assert detail["sources"][0]["evidence_id"] == "one"
    assert evidence_trace(detail)[0]["tool"] == "search_knowledge"


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("question", ["What is the consultation deadline?", "What are the registration deadlines?", "When is the registration deadline in 2027?"])
def test_single_current_page_fact_followup_without_pronoun_and_topic_switch(tmp_path, monkeypatch, question):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    identity_model(chat)
    reads, lookups = [], []
    def read(key, url, **kwargs):
        reads.append(url)
        return {"status": "ok", "content": "Registration deadline: 2027-04-20. Consultation questions concern insurers.",
            "final_url": url, "content_hash": "consultation-body"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    chat.reader_factory = lambda: lookups.append("knowledge lookup") or MeetingsReader([])
    first = chat.answer("Read https://example.org/consultation")
    result = chat.answer(question, context=first["context"])
    assert [source["url"] for source in result["sources"]] == ["https://example.org/consultation"]
    assert result["sources"][0]["version"] == "consultation-body"
    assert "Registration deadline: 2027-04-20" in result["text"] and "Consultation deadline: 2027-04-20" not in result["text"]
    assert evidence_trace(result)[0]["status"] == "cached" and not lookups
    assert reads == ["https://example.org/consultation"]
    # New topics cannot acquire the old page as their evidence or as a latent current target.
    for new_topic in ["What is the IFRS consultation deadline?", "Explain physical risk"]:
        switched = chat.answer(new_topic, context=first["context"])
        assert all(source.get("url") != "https://example.org/consultation" for source in switched["sources"])
        assert evidence_trace(switched)[0]["tool"] == "search_knowledge"
        after_switch = chat.answer(question, context=switched["context"])
        assert all(source.get("url") != "https://example.org/consultation" for source in after_switch["sources"])
    # A current stable meeting identity still wins over historical page evidence.
    chat.reader_factory = lambda: MeetingsReader([meeting("one", "Current conference", "Paris")])
    listed = chat.answer("List upcoming conferences", context=first["context"])
    detail = chat.answer("What is its registration deadline?", context=listed["context"])
    assert detail["sources"][0]["evidence_id"] == "one"
    assert any(call["tool"] == "get_meeting_details" and call["target"] == "one" for call in evidence_trace(detail))


def test_opportunity_newness_honors_custom_knowledge_range(tmp_path):
    chat = owner(tmp_path)
    item = meeting("one", "Conference", "Paris")
    facts = {"first_ingested_at": "2026-09-18T12:00:00Z"}
    turn = EvidenceTurn(chat, "Show opportunities added in the last 30 days", None, [])
    turn.now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    assert "recent addition/material update" in turn.meeting_text(item, facts)
    default = EvidenceTurn(chat, "Show upcoming opportunities", None, [])
    default.now = turn.now
    assert "older item" in default.meeting_text(item, facts)
    explicit = EvidenceTurn(chat, "Show opportunities added from 2026-09-09 to 2026-09-30", None, [])
    explicit.now = turn.now
    assert "recent addition/material update" in explicit.meeting_text(item, facts)
    business = EvidenceTurn(chat, "List New York conferences from 2027-06-01 to 2027-06-30", None, [])
    business.now = turn.now
    assert "older item" in business.meeting_text(item, facts)


@pytest.mark.parametrize("end_date, ended", [("2026-10-07", True), ("2026-10-08", False), ("2026-10-09", False), (None, False), ("unknown", False)])
def test_recently_added_meeting_ended_uses_known_end_and_new_york_date(tmp_path, end_date, ended):
    chat = owner(tmp_path)
    item = meeting("one", "Recently added conference", "Paris")
    item.update(start_date="2026-09-30", end_date=end_date, date_precision="day", deadline_date=None, pdf_observations=[])
    turn = EvidenceTurn(chat, "Show recently added meeting opportunities", None, [])
    turn.now = datetime(2026, 10, 9, 0, 30, tzinfo=timezone.utc)  # Still October8 in NewYork.
    text = turn.meeting_text(item, {"first_ingested_at": "2026-10-08T12:00:00Z"})
    assert "status: scheduled" in text and "Deadlines: unknown" in text
    if ended:
        assert "ended; not a current participation opportunity" in text
        assert "recent addition/material update; participation availability" not in text
    else:
        assert "ended;" not in text
        assert "recent addition/material update; participation availability requires source confirmation" in text


def test_past_expert_review_does_not_close_future_registration(tmp_path):
    turn = EvidenceTurn(owner(tmp_path), "Show recent meeting opportunities", None, [])
    turn.now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    item = meeting("one", "Conference", "Paris")
    item.update(deadline_type="expert_review", deadline_date="2026-09-30",
        pdf_observations=[{"deadline_type": "registration", "deadline_date": "2027-04-20", "source_urls": ["https://example.org/register"]}])
    facts = {"first_ingested_at": "2026-10-07T12:00:00Z"}
    text = turn.meeting_text(item, facts)
    assert "expert_review: 2026-09-30" in text and "registration: 2027-04-20" in text
    assert "expert_review: 2026-09-30 [expired" in text
    assert "registration: 2027-04-20 [future deadline" in text
    assert "closed/cancelled;" not in text and "participation availability requires source confirmation" in text
    item["status"] = "closed"
    assert "closed/cancelled;" in turn.meeting_text(item, facts)
    item["status"] = "scheduled"
    item["pdf_observations"] = []
    assert "closed/cancelled;" not in turn.meeting_text(item, facts)


def test_upcoming_event_past_sole_registration_deadline_is_expired(tmp_path):
    turn = EvidenceTurn(owner(tmp_path), "Show recent meeting opportunities", None, [])
    turn.now = datetime(2026, 10, 9, 0, 30, tzinfo=timezone.utc)  # Still October8 in NewYork.
    item = meeting("one", "Upcoming conference", "Paris")
    item.update(deadline_type="registration", deadline_date="2026-10-07", pdf_observations=[])
    facts = {"first_ingested_at": "2026-10-08T12:00:00Z"}
    text = turn.meeting_text(item, facts)
    assert "status: scheduled" in text and "start_date: 2027-06-10" in text
    assert "registration: 2026-10-07 [expired" in text and "https://example.org/one" in text
    assert "Opportunity timing: recorded participation deadlines expired" in text
    assert "recent addition/material update; participation availability" not in text
    assert "First added: 2026-10-08T12:00:00Z" in text
    item["deadline_date"] = "2026-10-08"
    today = turn.meeting_text(item, facts)
    assert "registration: 2026-10-08 [due today" in today and "[expired" not in today
    item["deadline_date"] = "2026-10-09"
    future = turn.meeting_text(item, facts)
    assert "registration: 2026-10-09 [future deadline" in future and "[expired" not in future
    item["deadline_date"] = "unknown"
    unknown = turn.meeting_text(item, facts)
    assert "registration: unknown [timing unknown" in unknown and "[expired" not in unknown


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("truncated", [False, True])
def test_url_only_overview_receives_retained_body_and_reading_limit(tmp_path, monkeypatch, provider, truncated):
    from agentic_wiki.chat_evidence import MAX_TEXT
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    monkeypatch.setenv("CLIMATE_CHAT_PROVIDER", provider)
    later = "Key finding: insurance exposure increases. Registration deadline: 2027-04-20. Action: register through the official form."
    body = "Background information. " * (1600 if truncated else 150) + later
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *args, **kwargs:
        {"status": "ok", "content": body, "final_url": args[1], "content_hash": "overview-body"})
    calls = []
    class Client:
        def with_options(self, **kwargs):
            assert kwargs["max_retries"] == 0
            return self
        def create(self, **kwargs):
            calls.append(copy.deepcopy(kwargs))
            if provider == "anthropic":
                return SimpleNamespace(model_dump=lambda: {"content": [{"type": "text", "text": "Brief page overview. [1]"}], "stop_reason": "end_turn"})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Brief page overview. [1]", tool_calls=[]), finish_reason="stop")])
    client = Client()
    client.messages = client
    client.chat = SimpleNamespace(completions=client)
    chat.responder.client = client if provider == "openai" else None
    chat.responder.anthropic_client = client if provider == "anthropic" else None
    chat.responder.anthropic_model = "fixture-model"
    result = chat.answer("https://example.org/overview")
    request = calls[0]
    system = request.get("system") or request["messages"][0]["content"]
    evidence = json.loads(request["messages"][-1]["content"].split("\n", 1)[1])["evidence"][0]["text"]
    assert evidence == body[:MAX_TEXT]
    assert "concise overview" in system and "findings" in system and "dates" in system and "actions" in system
    assert result["sources"][0]["version"] == "overview-body" and result["sources"][0]["url"] == "https://example.org/overview"
    assert result["sources"][0]["truncated"] is truncated
    if truncated:
        assert later not in evidence
        assert "Reading window limitation" in result["text"] and "omitted text" in result["text"]
    else:
        assert later in evidence and "Reading window limitation" not in result["text"]


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("truncated", [False, True])
def test_offline_url_only_overview_is_short_and_preserves_retained_body(tmp_path, monkeypatch, truncated):
    from agentic_wiki.chat_evidence import MAX_TEXT
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    later = "Key finding: insurance exposure increases. Registration deadline: 2027-04-20. Action: register through the official form."
    body = "Background information. " * (1600 if truncated else 150) + later
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *args, **kwargs:
        {"status": "ok", "content": body, "final_url": args[1], "content_hash": "offline-overview-body"})
    result = chat.answer("https://example.org/offline-overview")
    assert result["agent_mode"] == "offline" and len(result["text"]) < 2000
    assert result["sources"][0]["version"] == "offline-overview-body"
    assert next(iter(chat.frames.get(result["context"])["web"].values()))["body"] == body[:MAX_TEXT]
    assert "selected passages" in result["text"]
    if truncated:
        assert "Reading window limitation" in result["text"] and later not in result["text"]
    else:
        assert later in " ".join(result["text"].split())


@pytest.mark.parametrize("question", [PROMPT_STARTERS[2]["prompt"], "Do not generate a PDF for the last 14 days", "Explain physical risk from 2026-09-01 to 2026-09-30"])
def test_pending_pdf_topic_change_never_writes_an_artifact(monkeypatch, question):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    writes = []
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    monkeypatch.setattr(api_server, "_registry_reader", lambda: object())
    def intercepted(*args, **kwargs):
        writes.append(kwargs)
        raise api_server.RegistryUnavailableError("fixture stop before artifact write")
    monkeypatch.setattr(api_server, "freeze_range_report", intercepted)
    monkeypatch.setattr(api_server.responder, "answer", lambda question, **kwargs: {"text": "normal topic", "sources": []})
    client = TestClient(api_server.app)
    first = client.post("/api/chat", json={"message": "Generate a PDF report"}).json()
    history = [{"role": "user", "content": "Generate a PDF report"}, {"role": "assistant", "content": first["text"]}]
    response = client.post("/api/chat", json={"messages": [*history, {"role": "user", "content": question}]})
    assert not writes and response.json()["text"] == "normal topic"
    incomplete = client.post("/api/chat", json={"messages": [*history, {"role": "user", "content": "2026-09-01"}]})
    assert incomplete.json()["needs_clarification"] and not writes
    for correction in ["2026-09-01 to 2026-09-30", "Generate a PDF for 2026-09-01 to 2026-09-30"]:
        response = client.post("/api/chat", json={"messages": [*history, {"role": "user", "content": correction}]})
        assert response.status_code == 503  # Intercepted before any artifact mutation.
        assert writes[-1]["start_date"] == "2026-09-01" and writes[-1]["end_date"] == "2026-09-30"


@pytest.mark.parametrize("question", ["What is its registration deadline in 2027?", "Can I attend it on June 10?"])
def test_date_numbers_do_not_replace_focused_meeting_identity(tmp_path, question):
    chat = owner(tmp_path)
    identity_model(chat)
    reader = MeetingsReader([meeting("one", "First conference", "Paris")])
    chat.reader_factory = lambda: reader
    first = chat.answer("List upcoming conferences")
    result = chat.answer(question, context=first["context"])
    assert not result["needs_clarification"] and result["sources"][0]["evidence_id"] == "one"
    reader.records.append(meeting("two", "Second conference", "New York"))
    multiple = chat.answer("List upcoming conferences")
    ambiguous = chat.answer(question, context=multiple["context"])
    assert ambiguous["needs_clarification"] and not ambiguous["sources"]
    focused = chat.answer("Tell me more about meeting 2", context=multiple["context"])
    assert focused["sources"][0]["evidence_id"] == "two"
    detail = chat.answer(question, context=focused["context"])
    assert not detail["needs_clarification"] and detail["sources"][0]["evidence_id"] == "two"


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("question", ["Is it still open now?", "Is it open?"])
def test_current_open_status_requires_a_fresh_governed_read(tmp_path, monkeypatch, question):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    identity_model(chat)
    reads = []
    def read(key, url, **kwargs):
        reads.append(url)
        return {"status": "ok", "content": "Registration is open." if len(reads) == 1 else "Registration is closed.",
            "final_url": url, "content_hash": f"status-{len(reads)}"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    first = chat.answer("Read https://example.org/registration")
    result = chat.answer(question, context=first["context"])
    assert len(reads) == 2 and "Registration is closed" in result["text"]
    assert result["sources"][0]["version"] == "status-2" and evidence_trace(result)[0]["status"] == "read"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_answer_modes_reach_both_provider_model_prompts(tmp_path, monkeypatch, provider):
    chat = owner(tmp_path)
    monkeypatch.setenv("CLIMATE_CHAT_PROVIDER", provider)
    requests = scripted_provider(chat, monkeypatch, provider, lambda index, request: "Supported evidence [1].")
    modes = {"brief": "concise answer", "detailed": "supporting evidence, concrete figures, dates",
        "executive": "Executive Summary, Major Themes, Date Coverage"}
    for mode in modes:
        assert chat.answer("Explain insurance pricing", answer_mode=mode)["answer_mode"] == mode
    assert len(requests) == 12
    for index, (mode, instruction) in enumerate(modes.items()):
        for request in requests[index * 4:index * 4 + 4]:
            prompt = json.dumps(request, ensure_ascii=False)
            assert f"Answer mode: {mode}" in prompt and instruction in prompt


def test_existing_fifty_message_api_cap_is_preserved(monkeypatch):
    assert api_server.MAX_MESSAGES == 50
    monkeypatch.setattr(api_server.responder, "answer", lambda question, **kwargs: {"text": "normal topic", "sources": []})
    client = TestClient(api_server.app)
    messages = [{"role": "user", "content": "insurance"}] * 50
    assert client.post("/api/chat", json={"messages": messages}).status_code == 200
    assert client.post("/api/chat", json={"messages": messages + messages[:1]}).status_code == 400


def test_added_range_filters_approved_chronology_not_future_event_dates(tmp_path):
    chat = owner(tmp_path)
    records = [meeting(key, f"Expert review {key}", "Paris") for key in ("one", "older", "unknown")]
    for item in records:
        item.update(event_type="expert_review", deadline_type="expert_review", deadline_date="2027-04-01", last_seen="2026-10-08")
    class Reader(MeetingsReader):
        def knowledge_chronology(self):
            return {"meeting:one": {"knowledge_id": "approved-one", "first_ingested_at": "2026-09-18T12:00:00Z", "substantive_updated_at": "2026-09-19T12:00:00Z"},
                "meeting:older": {"knowledge_id": "approved-old", "first_ingested_at": "2026-08-18T12:00:00Z"}}
    chat.reader_factory = lambda: Reader(records)
    for question in ["Show expert-review opportunities added from 2026-09-09 to 2026-09-30",
        "Show expert-review opportunities added in the last 30 days",
        "Show expert-review opportunities material-update from 2026-09-09 to 2026-09-30"]:
        turn = EvidenceTurn(chat, question, None, [])
        turn.now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        turn.initial(None)
        assert [row["evidence_id"] for row in turn.sources] == ["one"]
        assert "2027-06-10" in turn.extractive() and "expert_review: 2027-04-01" in turn.extractive()
        assert "unknown added/material-update time" in turn.extractive()
        assert "recent addition/material update" in turn.extractive()
    business = EvidenceTurn(chat, "Show expert-review opportunities from 2027-06-01 to 2027-06-30", None, [])
    business.now = turn.now
    business.initial(None)
    assert [row["evidence_id"] for row in business.sources] == ["one", "older", "unknown"]


@pytest.mark.usefixtures("mock_reader_policy")
def test_explicit_new_url_becomes_current_pronoun_target(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    identity_model(chat)
    reads = []
    def read(key, url, **kwargs):
        reads.append(url)
        day = "2027-01-10" if url.endswith("/first") else "2027-04-20"
        return {"status": "ok", "content": f"Registration deadline: {day}.", "final_url": url, "content_hash": url}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    first = chat.answer("Read https://example.org/first")
    second = chat.answer("Read https://example.org/second", context=first["context"])
    result = chat.answer("What is its registration deadline?", context=second["context"])
    assert [source["url"] for source in result["sources"]] == ["https://example.org/second"]
    assert "2027-04-20" in result["text"] and "2027-01-10" not in result["text"]
    assert reads == ["https://example.org/first", "https://example.org/second"]
    multiple = chat.answer("Read both https://example.org/first and https://example.org/second", context=result["context"])
    assert [source["url"] for source in multiple["sources"]] == ["https://example.org/first", "https://example.org/second"]
    ambiguous = chat.answer("What is its registration deadline?", context=multiple["context"])
    assert ambiguous["needs_clarification"] and "2027-01-10" not in ambiguous["text"] and "2027-04-20" not in ambiguous["text"]
    selected = chat.answer("Read source 2", context=ambiguous["context"])
    assert selected["sources"][0]["url"] == "https://example.org/second"
    followup = chat.answer("What is its registration deadline?", context=selected["context"])
    assert [source["url"] for source in followup["sources"]] == ["https://example.org/second"]


@pytest.mark.parametrize("static", [False, True])
@pytest.mark.parametrize("known_time", [False, True])
def test_recent_standalone_pdf_approved_projection_and_followup(tmp_path, static, known_time):
    from climate_registry.read_api import RegistryReader
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute("""INSERT INTO pdf_intake_documents(document_sha256,source_path,filename,media_type,
            size_bytes,extracted_text_sha256,document_json,imported_at)
            VALUES (?, 'fixture.pdf', 'fixture.pdf', 'application/pdf', 0, ?, '{}', '2026-10-08T12:00:00Z')""", ("a" * 64, "b" * 64))
        for key in ("standalone", "pending"):
            db.execute("""INSERT INTO pdf_intake_articles(article_id,canonical_url,title,type_safe_classification_json,imported_at)
                VALUES (?, ?, ?, '{"label":"article"}', '2026-10-08T12:00:00Z')""", (key, "https://example.org/" + key, key.title()))
            db.execute("""INSERT INTO pdf_intake_article_occurrences(occurrence_id,article_id,source_document_sha256,
                page,raw_url,content_sha256,page_sha256,occurrence_json)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?)""", ("occ-" + key, key, "a" * 64, "https://example.org/" + key,
                "c" * 64, "d" * 64, json.dumps({"occurrence_id": "occ-" + key, "summary": key + " insurance findings"})))
        if known_time:
            record_knowledge(db, kind="article", entity_id="standalone", source_kind="pdf", source_ref="fixture",
                fields={"summary": "standalone insurance findings"}, evidence={}, recorded_at="2026-10-08T12:00:00Z")
            assert db.execute("SELECT count(*) FROM knowledge_versions WHERE entity_id='standalone'").fetchone()[0] == 1
        for sha in stage_entities(db):
            kind, key = db.execute("SELECT entity_kind,entity_id FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()
            if kind != "pdf_article" or key != "pending":
                _approve(db, sha, {"basis": "fixture approved snapshot"}, status="accepted_legacy")
                if key == "standalone" and known_time:
                    frozen = json.loads(db.execute("SELECT snapshot_json FROM registry_candidates WHERE candidate_sha256=?", (sha,)).fetchone()[0])
                    assert frozen["tables"]["knowledge_versions"]
    reader = _reader(database, tmp_path)
    assert [item["article_id"] for item in reader.pdf_articles_all()] == ["standalone"]
    if known_time:
        assert reader.knowledge_chronology()["article:standalone"]["first_ingested_at"] == "2026-10-08T12:00:00Z"
    if static:
        export_public_snapshot(database, tmp_path / "wiki" / "public-registry.json")
        reader = RegistryReader.from_public_export(tmp_path)
    (tmp_path / "chat").mkdir()
    chat = owner(tmp_path / "chat")
    identity_model(chat)
    chat.reader_factory = lambda: reader
    first = chat.answer("New articles added from 2026-10-01 to 2026-10-08")
    pdf = next((source for source in first["sources"] if source["evidence_id"] == "standalone"), None)
    assert pdf is not None and not any(source["evidence_id"] == "pending" for source in first["sources"])
    assert pdf["url"] == "https://example.org/standalone"
    if known_time:
        assert pdf["version"] == reader.knowledge_chronology()["article:standalone"]["knowledge_id"]
    assert "standalone insurance findings" in first["text"]
    assert ("First added: 2026-10-08T12:00:00Z" if known_time else "First added: unknown") in first["text"]
    assert ("No confirmed matches" in first["text"]) is (not known_time)
    position = first["sources"].index(pdf) + 1
    ordinal = {1: "first", 2: "second", 3: "third"}[position]
    followup = chat.answer(f"Tell me more about the {ordinal} article above", context=first["context"])
    assert not followup["needs_clarification"] and [source["evidence_id"] for source in followup["sources"]] == ["standalone"]
    assert "standalone insurance findings" in followup["text"]


@pytest.mark.parametrize("value,precision,expected", [("2027-06", "month", True), ("2027", "year", True),
    ("2027-07", "month", False), ("2028", "year", False)])
def test_explicit_meeting_window_preserves_partial_date_precision(tmp_path, value, precision, expected):
    chat = owner(tmp_path)
    item = meeting("one", "Precision event", "Paris")
    item.update(start_date=value, end_date=None, date_precision=precision, deadline_date=None,
        pdf_observations=[], verification_status="verified")
    chat.reader_factory = lambda: MeetingsReader([item])
    result = chat.answer("List meetings from 2027-06-01 to 2027-06-30")
    assert bool(result["sources"]) is expected
    if expected:
        assert f"start_date: {value}" in result["text"] and f"date_precision: {precision}" in result["text"]


@pytest.mark.parametrize("question,artifact", [("Give me a PDF report for the last 14 days", True),
    ("Do not give me a PDF report for the last 14 days", False),
    ("Explain how to give me a PDF report for the last 14 days", False)])
def test_give_pdf_intent_routes_only_affirmative_artifact_requests(monkeypatch, question, artifact):
    writes = []
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    monkeypatch.setattr(api_server, "_registry_reader", lambda: object())
    def intercepted(*args, **kwargs):
        writes.append(kwargs)
        raise api_server.RegistryUnavailableError("fixture stop before artifact write")
    monkeypatch.setattr(api_server, "freeze_range_report", intercepted)
    monkeypatch.setattr(api_server.responder, "answer", lambda *args, **kwargs: {"text": "ordinary chat", "sources": []})
    response = TestClient(api_server.app).post("/api/chat", json={"message": question})
    assert bool(writes) is artifact
    assert response.status_code == (503 if artifact else 200)
    if not artifact:
        assert response.json()["text"] == "ordinary chat"


@pytest.mark.parametrize("time_label", ["first-ingested", "first ingested"])
def test_recent_article_temporal_metadata_is_not_an_ordinal(tmp_path, time_label):
    chat = owner(tmp_path)
    identity_model(chat)
    class Reader(MeetingsReader):
        def articles(self, **kwargs):
            return {"items": self.records, "pagination": {"pages": 1}}
        def article(self, key):
            return next(copy.deepcopy(item) for item in self.records if item["article_id"] == key)
        def knowledge_chronology(self):
            return {"article:a": {"first_ingested_at": "2026-10-08T12:00:00Z"}}
    chat.reader_factory = lambda: Reader([{"article_id": "a", "title": "Approved report", "publication_date": "2024-01-01",
        "summary": "Actual approved findings", "canonical_url": "https://example.org/a"}])
    question = "What reports or articles were added or materially updated in the last 14 days? Include the publication date separately from the " + time_label + " time."
    result = chat.answer(question)
    assert not result["needs_clarification"] and result["sources"][0]["evidence_id"] == "a"
    assert "publication_date: 2024-01-01" in result["text"] and "First added: 2026-10-08T12:00:00Z" in result["text"]
    assert evidence_trace(result)[0]["tool"] == "search_knowledge"
    followup = chat.answer("Tell me about the first article above", context=result["context"])
    assert not followup["needs_clarification"] and followup["sources"][0]["evidence_id"] == "a"


@pytest.mark.parametrize("question,artifact", [
    ("Tell me how to generate a PDF report for the last 14 days", False),
    ("Please explain whether I should create a PDF report for the last 14 days", False),
    ("If you can create a PDF report for the last 14 days, tell me how", False),
    ("Can you please generate a PDF report for the last 14 days?", True),
    ("I'd like you to generate a PDF report for the last 14 days", True),
    ("I’d like you to generate a PDF report for the last 14 days", True),
    ("Let's generate a PDF report for the last 14 days", True),
    ("I want you to create a PDF report for the last 14 days", True),
    ("Please generate a PDF report for the last 14 days", True),
    ("Could you please create a PDF report for the last 14 days?", True),
    ("Can you explain how to generate a PDF report for the last 14 days?", False),
    ("Do you think I should generate a PDF report for the last 14 days?", False),
    ("I'd like to understand how to generate a PDF report for the last 14 days", False),
    ("Please explain whether I should create one", False),
    ("If you can create one, tell me how", False),
    ("Don't generate a PDF report for the last 14 days", False),
    ("Don’t generate a PDF report for the last 14 days", False),
    ("Generate a PDF report for the last 14 days—actually, don't.", False),
    ("Generate a PDF report for the last 14 days; actually, don’t.", False),
    ("Generate a PDF report for the last 14 days, but do not email it.", True),
    ("Generate a PDF report for the last 14 days—actually, don't send it.", True),
    ("I need to understand what the PDF report from the last 14 days found", False),
    ("I need a PDF report for the last 14 days", True),
    ("I need a climate PDF report using meeting-snapshot-" + "c" * 24, True),
    ("Give me a PDF report for the last 14 days", True),
    ("Generate a PDF report for the last 14 days", True),
    ("Explain what the PDF report from the last 14 days found", False),
    ("Do not generate a PDF report for the last 14 days", False)])
def test_pdf_understanding_request_does_not_create_an_artifact(monkeypatch, question, artifact):
    writes, ordinary = [], []
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    monkeypatch.setattr(api_server, "_registry_reader", lambda: object())
    def intercepted(*args, **kwargs):
        writes.append(kwargs)
        raise api_server.RegistryUnavailableError("fixture stop before artifact write")
    monkeypatch.setattr(api_server, "freeze_range_report", intercepted)
    monkeypatch.setattr(api_server.responder, "answer", lambda *args, **kwargs: ordinary.append(args) or {"text": "ordinary content answer", "sources": []})
    response = TestClient(api_server.app).post("/api/chat", json={"message": question})
    if artifact:
        assert not ordinary
        if writes:
            assert response.status_code == 503
        else:
            assert response.status_code == 200 and response.json()["needs_clarification"]
    else:
        assert not writes and ordinary and response.status_code == 200
        assert response.json()["text"] == "ordinary content answer"


def seven_relevant_paragraphs():
    base = "Consultation requirements and registration deadline details: "
    tails = ["."] * 6 + [". Registration deadline: 2027-11-30."]
    extra = 5209 - len("\n\n".join(base + tail for tail in tails))
    count, remainder = divmod(extra, 7)
    body = "\n\n".join(base + "x" * (count + (index < remainder)) + tail for index, tail in enumerate(tails))
    assert len(body) == 5209
    return body


@pytest.mark.usefixtures("mock_reader_policy")
def test_relevant_seventh_url_passage_uses_remaining_evidence_room(tmp_path, monkeypatch):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    body = seven_relevant_paragraphs()
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *args, **kwargs:
        {"status": "ok", "content": body, "final_url": args[1], "content_hash": "seven-paragraph-body"})
    result = chat.answer("Read this URL and explain the consultation requirements, especially the registration deadline: https://example.org/requirements")
    assert "2027-11-30" in result["text"]
    assert result["sources"][0]["truncated"] is False
    assert "Reading window limitation" not in result["text"]
    assert next(iter(chat.frames.get(result["context"])["web"].values()))["body"] == body


@pytest.mark.usefixtures("mock_reader_policy")
def test_cached_url_excerpt_omission_has_honest_metadata_and_note(tmp_path, monkeypatch):
    from agentic_wiki.chat_evidence import MAX_TEXT
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    body = seven_relevant_paragraphs()
    url = "https://example.org/requirements"
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *args, **kwargs:
        {"status": "ok", "content": body, "final_url": args[1], "content_hash": "seven-paragraph-body"})
    first = chat.answer(url)
    turn = EvidenceTurn(chat, "Explain the consultation requirements and registration deadline", first["context"], [])
    turn.chars = MAX_TEXT - 4500  # Other confirmed evidence has already consumed this turn's allowance.
    result = turn.read_url(url)
    assert result["status"] == "cached" and result["source"]["truncated"] is True
    assert any("Reading window limitation" in note for note in turn.notes)
    assert len(turn.evidence[0]["text"]) <= 4500 and turn.chars <= MAX_TEXT
    assert result["body"] == body and result["source"]["version"] == "seven-paragraph-body"


@pytest.mark.parametrize("pdf", [False, True])
@pytest.mark.parametrize("count", [1, 2])
def test_report_pronoun_binds_unique_source_and_clarifies_multiple(tmp_path, pdf, count):
    chat = owner(tmp_path)
    identity_model(chat)
    class Reader(MeetingsReader):
        def articles(self, **kwargs):
            return {"items": [] if pdf else self.records, "pagination": {"pages": 1}}
        def pdf_articles_all(self):
            return self.records if pdf else []
        def article(self, key):
            return next(copy.deepcopy(item) for item in self.records if item["article_id"] == key)
        pdf_article = article
        def knowledge_chronology(self):
            return {"article:" + item["article_id"]: {"first_ingested_at": "2026-10-08T12:00:00Z"} for item in self.records}
    reader = Reader([{"article_id": "unique-" + str(index), "title": "Approved report " + str(index),
        "summary": "Original approved findings", "canonical_url": "https://example.org/report-" + str(index),
        "published_candidate_sha256": "v1-" + str(index)} for index in range(count)])
    chat.reader_factory = lambda: reader
    first = chat.answer("New reports added from 2026-10-01 to 2026-10-08")
    assert len(first["sources"]) == count and chat.frames.get(first["context"])["source_focus"] is None
    reader.records.reverse()
    for item in reader.records:
        item.update(summary="Current approved findings for " + item["article_id"], published_candidate_sha256="v2-" + item["article_id"])
    second = chat.answer("Tell me more about that report", context=first["context"])
    if count == 1:
        assert not second["needs_clarification"]
        assert evidence_trace(second)[0]["tool"] == "get_source_details"
        assert evidence_trace(second)[0]["target"] == first["sources"][0]["evidence_id"]
        assert second["sources"][0]["version"] == "v2-unique-0" and "Current approved findings for unique-0" in second["text"]
    else:
        assert second["needs_clarification"] and not second["sources"]
        assert evidence_trace(second)[0]["tool"] == "ask_clarification"  # No unrelated retrieval.


@pytest.mark.parametrize("question,artifact", [
    ("Should I generate a PDF report for the last 14 days?", False),
    ("Should we create a PDF report for the last 14 days?", False),
    ("Can I generate a PDF report for the last 14 days?", False),
    ("Is it necessary to generate a PDF report for the last 14 days?", False),
    ("Do I need to generate a PDF report for the last 14 days?", False),
    ("What is the process to generate a PDF report for the last 14 days?", False),
    ("What does it mean to generate a PDF report for the last 14 days?", False),
    ("How do I generate a PDF report for the last 14 days?", False),
    ("Generate a PDF report for the last 14 days", True),
    ("Create a PDF report for the last 14 days", True),
    ("Make a PDF report for the last 14 days", True),
    ("Give me a PDF report for the last 14 days", True),
    ("Can you generate a PDF report for the last 14 days?", True),
    ("Explain what the PDF report from the last 14 days found", False),
    ("Do not generate a PDF report for the last 14 days", False)])
def test_pdf_advice_and_precondition_questions_do_not_execute(monkeypatch, question, artifact):
    writes, ordinary = [], []
    monkeypatch.setattr(api_server, "_range_report_overlay", lambda: (None, None, None))
    monkeypatch.setattr(api_server, "_registry_reader", lambda: object())
    def intercepted(*args, **kwargs):
        writes.append(kwargs)
        raise api_server.RegistryUnavailableError("fixture stop before artifact write")
    monkeypatch.setattr(api_server, "freeze_range_report", intercepted)
    monkeypatch.setattr(api_server.responder, "answer", lambda *args, **kwargs: ordinary.append(args) or {"text": "ordinary advice/content", "sources": []})
    response = TestClient(api_server.app).post("/api/chat", json={"message": question})
    assert bool(writes) is artifact and bool(ordinary) is (not artifact)
    assert response.status_code == (503 if artifact else 200)
    if not artifact:
        assert response.json()["text"] == "ordinary advice/content"


@pytest.mark.parametrize("count", [1, 2])
def test_source_only_legacy_history_does_not_supply_meeting_identity(tmp_path, count):
    chat = owner(tmp_path)
    records = [meeting("one", "Alpha conference", "Paris"), meeting("two", "Beta conference", "London")]
    chat.reader_factory = lambda: MeetingsReader(records)
    first = chat.answer("List upcoming conferences")
    history = [{"role": "user", "content": "List upcoming conferences"},
        {"role": "assistant", "content": first["text"] if count == 2 else "Alpha conference is upcoming."}]
    result = chat.responder.answer("What is its registration deadline?", history=history)
    assert result["needs_clarification"] and not result["sources"] and not evidence_trace(result)
    assert "Manual verification" in result["text"]


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("count", [1, 2])
def test_page_reference_reuses_body_and_clarifies_multiple_pages(tmp_path, monkeypatch, count):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    identity_model(chat)
    reads = []
    def read(key, url, **kwargs):
        reads.append(url)
        return {"status": "ok", "content": "Insurer solvency requires sufficient capital.",
            "final_url": url, "content_hash": "solvency-body"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    urls = ["https://example.org/solvency" + str(index) for index in range(count)]
    first = chat.answer("Read " + " and ".join(urls))
    result = chat.answer("What does the page say about insurer solvency?", context=first["context"])
    assert result["needs_clarification"] is (count == 2)
    assert len(reads) == count
    if count == 1:
        assert "Insurer solvency requires sufficient capital" in result["text"]
        assert [source["url"] for source in result["sources"]] == urls
        assert evidence_trace(result)[0]["status"] == "cached"
    else:
        assert evidence_trace(result)[0]["tool"] == "ask_clarification" and "identify the page" in result["text"].lower()


@pytest.mark.parametrize("question", ["List articles added in the last 3 months.",
    "Show meeting opportunities added in the last 3 months."])
def test_explicit_month_period_requests_date_endpoints_not_default_window(tmp_path, question):
    now = datetime(2026, 10, 9, 16, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="start and end dates"):
        window(question, now)
    chat = owner(tmp_path)
    reads = []
    chat.reader_factory = lambda: reads.append("unexpected lookup") or MeetingsReader([])
    result = chat.answer(question)
    assert result["needs_clarification"] and not result["sources"] and not reads
    assert "start and end dates" in result["text"] and "3 months" in result["text"]
    assert "Added/material-update window:" not in result["text"]
    start, end = window("List recent articles", now)
    assert (end.date() - start.date()).days == 13  # The ordinary recent default stays14 days.
    start, end = window(question + " from 2026-07-09 to 2026-10-09", now)
    assert start.date().isoformat() == "2026-07-09" and end == now


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("outcome", ["ok", "failed", "missing_url"])
@pytest.mark.parametrize("focused", [False, True])
def test_registry_current_registration_status_uses_fresh_reader_or_explicit_gap(tmp_path, monkeypatch, outcome, focused):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    if focused:
        identity_model(chat)
    item = meeting("one", "Climate conference", "Paris")
    item.update(status="open", source_urls=[] if outcome == "missing_url" else ["https://example.org/conference"], pdf_observations=[])
    chat.reader_factory = lambda: MeetingsReader([item])
    reads = []
    def read(key, url, **kwargs):
        reads.append(url)
        if outcome == "failed":
            return {"status": "failed", "failure_reason": "reader unavailable"}
        return {"status": "ok", "content": "Registration is closed.", "final_url": url, "content_hash": "current-closed"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    first = chat.answer("List upcoming conferences") if focused else None
    question = "Is registration still open for the climate conference?" if not focused else "Is it still open?"
    result = chat.answer(question, context=first["context"] if first else None)
    assert reads == ([] if outcome == "missing_url" else ["https://example.org/conference"])
    assert "stored approved status" in result["text"]
    if outcome == "ok":
        assert "Registration is closed" in result["text"]
        source = next(source for source in result["sources"] if source["heading"] == "web")
        assert source["url"] == "https://example.org/conference" and source["version"] == "current-closed"
        assert any(call["tool"] == "read_url" and call["status"] == "read" for call in evidence_trace(result))
    else:
        assert "Fresh current status could not be verified" in result["text"]
        assert all(source["heading"] != "web" for source in result["sources"])


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("readable", [True, False])
@pytest.mark.parametrize("blocked_first", [False, True])
def test_unique_meeting_deadline_search_reads_official_body_before_generic_sources(tmp_path, monkeypatch, provider, readable, blocked_first):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    generic, official, news = "https://www.understandrisk.org/", "https://www.urabudhabi.com/", "https://news.example.org/ur"
    blocked = "https://www.gfdrr.org/en/understandingrisk"
    selected = [blocked, official] if blocked_first else [official]
    item = meeting("one", "Understanding Risk Global Forum, Abu Dhabi", "Abu Dhabi")
    item.update(deadline_date=None, pdf_observations=[], source_urls=[generic])
    chat.reader_factory = lambda: MeetingsReader([item])
    first = chat.answer("List upcoming conferences")
    operations = []
    body = "UR Abu Dhabi: May 10-14, 2027. The Call for Proposals will remain open until November 13, 2026."
    def read(key, url, **kwargs):
        operations.append("read:" + url)
        if url == blocked:
            return {"status": "failed", "failure_reason": "robots.forbidden"}
        assert url == official  # A stored generic URL/news item cannot spend the directed-read budget first.
        return {"status": "ok", "content": body, "final_url": url, "content_hash": "official-deadline-body"} if readable else {"status": "failed", "failure_reason": "official body unavailable"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    class Client:
        def with_options(self, **kwargs):
            return self
        def create(self, **kwargs):
            search = "input" in kwargs or any(tool.get("type") == "web_search_20250305" for tool in kwargs.get("tools", []))
            if search:
                operations.append("search")
                prompt = kwargs.get("input") or kwargs["messages"][0]["content"]
                assert item["name"] in prompt and "official" in prompt.lower()
                if provider == "openai":
                    payload = {"output": [{"type": "web_search_call", "status": "completed", "action": {"sources": [{"url": url} for url in [generic, news, *selected]]}},
                        {"content": [{"text": "Unread snippet: early-bird deadline 2026-10-01", "annotations": [{"type": "url_citation", "url": url} for url in selected]}]}]}
                else:
                    payload = {"content": [{"type": "web_search_tool_result", "content": [{"type": "web_search_result", "url": url} for url in [generic, news, *selected]]},
                        {"type": "text", "text": "Search preparation", "citations": None},
                        {"type": "text", "text": "Unread snippet: early-bird deadline 2026-10-01", "citations": [{"type": "web_search_result_location", "url": url} for url in selected]}]}
                return SimpleNamespace(model_dump=lambda: payload)
            operations.append("model")
            stage = operations.count("model")
            if stage <= 4 or stage == 6:
                assert "Conversation identity candidates" in json.dumps(kwargs["messages"])
                supplied_results = []
                for message in kwargs["messages"]:
                    if message["role"] == "tool": supplied_results.append(json.loads(message["content"]))
                    elif isinstance(message.get("content"), list): supplied_results.extend(json.loads(block["content"]) for block in message["content"] if block["type"] == "tool_result")
                read_ids = [row["source"]["evidence_id"] for row in supplied_results if row.get("body") and row.get("status") == "read"]
                choices = ([('research_state', {'action':'plan','time_basis':'current official deadlines','required_outputs':[{'id':'dates','task':'CFP and early-bird facts','requires_current_body':True}]})] if stage == 1 else
                    [('get_meeting_details', {'event_id':'one'})] if stage == 2 else
                    [('search_web', {'query':item['name']+': official CFP and early-bird deadlines'})] if stage == 3 else
                    [('read_url', {'url':url}) for url in selected] if stage == 4 else
                    [('research_state', {'action':'finish','results':[{'id':'dates','status':'supported' if readable else 'gap','evidence_ids':read_ids,'gap':'' if readable else 'Official page reads failed.'}]})])
                if stage == 4:
                    assert all(url in json.dumps(kwargs["messages"]) for url in selected)
                if provider == "anthropic":
                    return SimpleNamespace(model_dump=lambda: {"content": [{"type": "tool_use", "id": str(i), "name": name, "input": arguments} for i, (name, arguments) in enumerate(choices)]})
                calls = [SimpleNamespace(id=str(i), function=SimpleNamespace(name=name, arguments=json.dumps(arguments))) for i, (name, arguments) in enumerate(choices)]
                message = SimpleNamespace(content=None, tool_calls=calls, model_dump=lambda **kw: {"role": "assistant", "tool_calls": [{"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}} for call in calls]})
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])
            supplied = json.dumps(kwargs["messages"])
            assert "2026-10-01" not in supplied  # Search snippets never establish deadline evidence.
            if readable:
                assert body in supplied
                assert "tools" in kwargs  # Finish hands off evidence without closing the answer-check loop.
                answer = "CFP deadline: November 13, 2026. [2] Early-bird registration deadline: unknown."
            else:
                assert body not in supplied
                answer = "Current CFP and early-bird registration deadlines are unknown; official page reading failed."
            if provider == "anthropic":
                return SimpleNamespace(model_dump=lambda: {"content": [{"type": "text", "text": answer}]})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[], content=answer))])
    client = Client()
    if provider == "anthropic":
        client.messages = client
        chat.responder.anthropic_client, chat.responder.anthropic_model = client, "configured-claude-model"
    else:
        client.responses, client.chat = client, SimpleNamespace(completions=client)
        chat.responder.client = client
    monkeypatch.setenv("CLIMATE_CHAT_PROVIDER", provider)
    result = chat.answer("What are its current CFP and early-bird registration deadlines?", context=first["context"])
    assert operations == ["model", "model", "model", "search", "model", *(["read:" + blocked] if blocked_first else []), "read:" + official, "model", "model", "model"]
    assert chat.frames.get(result["context"])["ordered"] == ["one"]
    assert any(call["tool"] == "search_web" and call["status"] == "searched" for call in evidence_trace(result))
    web = [source for source in result["sources"] if source["heading"] == "web"]
    if readable:
        assert [source["url"] for source in web] == [official]
        assert web[0]["version"] == "official-deadline-body" and web[0]["truncated"] is False
        assert "November 13, 2026. [2]" in result["text"] and "Early-bird registration deadline: unknown" in result["text"]
    else:
        assert not web and "unknown" in result["text"]


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_captured_official_search_without_text_citations_keeps_structured_candidates(tmp_path, monkeypatch, provider):
    from climate_monitor import article_content_adapter
    import tempfile
    chat = owner(tmp_path)
    captured = json.loads(Path("tests/fixtures/chat_official_search_no_citations.json").read_text())
    source_roles = captured.pop("_fixture_source_roles")  # Test provenance never supplies authority to the application.
    rows = next(block["content"] for block in captured["content"] if block["type"] == "web_search_tool_result" and isinstance(block["content"], list))
    official = "https://www.urabudhabi.com/"
    assert next(index for index, row in enumerate(rows) if row["url"] == official) >= 5
    class Client:
        def with_options(self, **kwargs):
            return self
        def create(self, **kwargs):
            payload = captured if provider == "anthropic" else {"output": [{"type": "web_search_call", "status": "completed", "action": {"sources": rows}}, {"content": [{"annotations": []}]}]}
            return SimpleNamespace(model_dump=lambda: payload)
    client = Client()
    if provider == "anthropic":
        client.messages = client
        chat.responder.anthropic_client, chat.responder.anthropic_model = client, "configured-claude-model"
    else:
        client.responses = client
        chat.responder.client = client
    monkeypatch.setenv("CLIMATE_CHAT_PROVIDER", provider)
    reads = []
    def read(key, url, **kwargs):
        reads.append(url)
        assert url == official
        return {"status": "ok", "content": "Official read body: CFP deadline November 13, 2026.", "final_url": url, "content_hash": "actual-candidate-read"}
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", read)
    with tempfile.TemporaryDirectory() as directory:
        turn = EvidenceTurn(chat, "What are its current CFP deadlines?", None, [])
        turn.runtime_dir = Path(directory)
        result = turn.execute("search_web", {"query": "Understanding Risk Global Forum, Abu Dhabi: official deadlines",
            "official_only": True, "event_hint": "Understanding Risk Global Forum, Abu Dhabi", "prior_urls": ["https://www.understandrisk.org/"]})
        assert result["status"] == "searched" and not reads and not turn.sources
        assert official in result["candidate_urls"]
        read_result = turn.execute("read_url", {"url": official})
    assert reads == [official]
    assert source_roles[reads[0]]["level"] == "official_event"
    assert source_roles["https://www.gfdrr.org/en/understandingrisk"]["level"] == "official_organizer"
    assert source_roles["https://gulfnews.com/uae/abu-dhabi-reschedules-understanding-risk-global-forum-to-may-2027-1.500549289"]["level"] == "news"
    assert read_result["body"].startswith("Official read body")
    assert turn.sources[0]["url"] == official and turn.sources[0]["version"] == "actual-candidate-read"
    assert turn.models == 1 and turn.reads == 1 and turn.calls == 2


@pytest.mark.parametrize("online", [True, False])
def test_links_reserve_single_article_canonical_keeps_offline_and_aggregate_links(tmp_path, online):
    wiki = tmp_path / "wiki"; wiki.mkdir()
    canonical = "https://example.org/energy-report"
    (wiki / "article-energy.md").write_text("# Energy report\nCanonical article: [Energy](" + canonical + ")\n\n## Date observations\nEnergy transition update; collection source https://example.org/;\n\n## Findings\nBattery storage reduces curtailment.")
    (wiki / "weekly.md").write_text("# Weekly\n## First energy finding\nEnergy transition battery financing. https://example.org/first\n## Second energy finding\nEnergy transition grid financing. https://example.org/second")
    r = AgenticWikiResponder(wiki, tmp_path / "sources"); r.client = object() if online else None
    turn = EvidenceTurn(r.chat_evidence, "energy transition", None, [])
    rows = turn.execute("search_knowledge", {"query": "energy transition", "target": "wiki"})["evidence"]
    article = next(row for row in rows if row["source"]["path"].endswith("article-energy.md"))
    assert article["source"]["url"].endswith("wiki/article-energy.md")
    assert article["source"]["source_urls"] == ([canonical] if online else ["https://example.org/;"])
    detail = turn.execute("get_source_details", {"evidence_id": article["source"]["evidence_id"]})
    assert detail["source"]["source_urls"] == ([canonical] if online else ["https://example.org/;"])
    aggregate = [row for row in rows if row["source"]["path"].endswith("weekly.md")]
    assert {url for row in aggregate for url in row["source"]["source_urls"]} == {"https://example.org/first", "https://example.org/second"}


def test_links_reserve_finish_only_selected_targets_not_every_observed_link(tmp_path):
    chat = owner(tmp_path);chat.responder.client = object()
    turn = EvidenceTurn(chat, "current energy facts", None, [])
    entry = turn.add({"evidence_id": "retained", "title": "Energy", "source_urls": ["https://example.org/primary", "https://example.org/;"]}, kind="wiki", text="Energy finding.")
    turn.execute("research_state", {"action":"plan","time_basis":"current","required_outputs":[{"id":"facts","task":"current energy facts","requires_current_body":True}]})
    result = turn.execute("research_state", {"action":"finish","results":[{"id":"facts","status":"gap","evidence_ids":["retained"],"gap":"Current page body is unavailable; no additional related target was selected."}]})
    assert result["status"] == "finish_accepted"
    handoff = turn.research_handoff()
    assert handoff["tasks"][0]["current_support"] == []
    assert handoff["tasks"][0]["optional_source_urls"] == entry["source"]["source_urls"]
    assert handoff["tasks"][0]["unattempted_current_targets"] == []
    other = EvidenceTurn(chat, "current energy facts", None, [])
    other.add(entry["source"], kind="wiki", text="Energy finding.")
    other.execute("research_state", {"action":"plan","time_basis":"current","required_outputs":[{"id":"facts","task":"current energy facts","requires_current_body":True}]})
    pending = other.execute("research_state", {"action":"finish","results":[{"id":"facts","status":"gap","evidence_ids":["retained"],"pending_targets":["https://example.org/primary"],"gap":"Current body missing."}]})
    assert [row["target"] for row in pending["pending"] if "target" in row] == ["https://example.org/primary"]


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("phase", ["no_plan", "pending", "search", "reader"])
def test_links_reserve_final_is_one_request_after_research_time(tmp_path, monkeypatch, provider, phase):
    from agentic_wiki import chat_evidence
    from climate_monitor import article_content_adapter
    chat=owner(tmp_path)
    elapsed=[0.0];monkeypatch.setattr(chat_evidence.time,"monotonic",lambda:elapsed[0])
    requests=[]
    def script(index,request):
        if phase in {"search","reader"} and index==0:
            assert request.get("tools")
            return [("search_web",{"query":"energy transition"})] if phase=="search" else [("read_url",{"url":"https://example.org/current"}),("read_url",{"url":"https://example.org/next"})]
        assert not request.get("tools") and "research_seconds" in json.dumps(request)
        assert turn.research_remaining() == 0
        assert "final cited answer" in json.dumps(request)
        return "Current energy findings remain unverified. Check the official publisher."
    requests=scripted_provider(chat,monkeypatch,provider,script,check_drafts=True,script_controls_plan=True)
    turn=EvidenceTurn(chat, "Explain current energy findings", None, []);turn.runtime_dir=tmp_path
    timeouts=[];client=chat.client;old_options=client.with_options
    client.with_options=lambda **kwargs: timeouts.append(kwargs["timeout"]) or old_options(**kwargs)
    if phase!="no_plan":
        turn.execute("research_state",{"action":"plan","time_basis":"current","required_outputs":[{"id":"facts","task":"current energy facts","requires_current_body":True}]})
        if phase=="pending":
            turn.add({"evidence_id":"retained","title":"Energy"},kind="wiki",text="Retained energy finding.")
            assert turn.execute("research_state",{"action":"finish","results":[{"id":"facts","status":"supported","evidence_ids":["retained"]}]})["status"] == "research_pending"
    if phase in {"search","reader"}:
        elapsed[0]=85
        if phase=="search":
            original=client.create
            def native(**request):
                if "input" in request or any(row.get("type")=="web_search_20250305" for row in request.get("tools",[])):
                    assert timeouts[-1]==15
                    elapsed[0]=101
                    return SimpleNamespace(model_dump=lambda:{"output":[{"type":"web_search_call","status":"completed","action":{"sources":[{"url":"https://example.org/alternative"}]}}]})
                return original(**request)
            client.create=native;client.responses=client
        else:
            def read(*args,**kwargs):
                assert kwargs["budget"].limits["runtime_seconds"]==15
                elapsed[0]=101
                return {"status":"failed","failure_reason":"robots.forbidden"}
            monkeypatch.setattr(article_content_adapter,"fetch_article_content",read)
    else:elapsed[0]=101
    text=turn.model_answer()
    assert "unverified" in text and len(requests)==(2 if phase in {"search","reader"} else 1)
    assert timeouts[-1]==19
    assert "Evidence sufficiency check" not in json.dumps(requests[-1])
    assert turn.reads==(1 if phase=="reader" else 0)


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_links_reserve_research_model_timeout_still_uses_final_window(tmp_path, monkeypatch, provider):
    from agentic_wiki import chat_evidence
    elapsed=[0.0];monkeypatch.setattr(chat_evidence.time,"monotonic",lambda:elapsed[0])
    chat=owner(tmp_path)
    def script(index,request):
        if len(requests)==1:
            assert request.get("tools")
            elapsed[0]=101
            raise TimeoutError("Research request used its research allowance")
        assert not request.get("tools")
        return "The current facts could not be verified; check the official source."
    requests=scripted_provider(chat,monkeypatch,provider,script,script_controls_plan=True)
    turn=EvidenceTurn(chat,"Explain current energy facts",None,[])
    turn.execute("research_state",{"action":"plan","time_basis":"current","required_outputs":[{"id":"facts","task":"current energy facts","requires_current_body":True}]})
    elapsed[0]=85
    answer=turn.model_answer()
    assert answer and "could not be verified" in answer and len(requests)==2
    assert turn.models==2 and turn.calls==1


@pytest.mark.parametrize("style", ["single", "multiple"])
def test_native_finish_registered_native_wrappers_bind_fixed_source_indices(tmp_path, style):
    turn=EvidenceTurn(owner(tmp_path),"Explain findings",None,[])
    for key in ("first", "wiki:second", "chat-third"):
        turn.add({"evidence_id":key,"title":key},kind="wiki",text="Retained finding "+key)
    raw=("Third citechat-third, first citefirst." if style=="single" else "Third then first citechat-thirdfirst.")
    answer=turn.final_text(raw+" Second [[cite:wiki:second]]. Unknown citeunregistered.")
    assert "[3]" in answer and "[1]" in answer and answer.index("[3]")<answer.index("[1]")
    assert "[2]" in answer and "" not in answer and "unregistered" not in answer
    assert "Citation limitation" in answer and len(turn.sources)==3


def test_native_finish_evidence_tool_invalidates_handoff_without_resetting_plan_or_check(tmp_path):
    chat=owner(tmp_path);chat.responder.client=object()
    turn=EvidenceTurn(chat,"Explain climate hazards",None,[])
    rows=turn.execute("search_knowledge",{"query":"climate hazards","target":"wiki"})["evidence"]
    key=rows[0]["source"]["evidence_id"]
    turn.execute("get_source_details",{"evidence_id":key})
    plan={"action":"plan","time_basis":"retained findings","required_outputs":[{"id":"facts","task":"hazard findings","requires_current_body":False}]}
    turn.execute("research_state",plan)
    assert turn.execute("research_state",{"action":"finish","results":[{"id":"facts","status":"supported","evidence_ids":[key]}]})["status"]=="finish_accepted"
    turn.final_checked=True
    result=turn.execute("get_source_details",{"evidence_id":key,"focus":"insurance pricing"})
    assert result.get("source",{}).get("evidence_id")==key and turn.research_finish is None
    assert turn.research_plan["required_outputs"]==plan["required_outputs"] and turn.final_checked and len(turn.sources)==1


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_native_finish_enough_evidence_still_gets_one_check_before_answer(tmp_path, monkeypatch, provider):
    chat=owner(tmp_path)
    key=chat.responder.kb.chunks[0].id
    def script(index,request):
        if index==0:return [("research_state",{"action":"plan","time_basis":"retained","required_outputs":[{"id":"facts","task":"climate hazard finding","requires_current_body":False}]})]
        if index==1:return [("search_knowledge",{"target":"wiki","query":"climate hazards"})]
        if index==2:return [("get_source_details",{"evidence_id":key})]
        if index==3:return [("research_state",{"action":"finish","results":[{"id":"facts","status":"supported","evidence_ids":[key]}]})]
        assert request.get("tools")
        if index==5:assert "Evidence sufficiency check" in json.dumps(request)
        return "Climate hazards affect insurance pricing [[cite:"+key+"]]."
    requests=scripted_provider(chat,monkeypatch,provider,script,script_controls_plan=True,check_drafts=True)
    result=chat.answer("Explain climate hazards")
    assert result["text"]=="Climate hazards affect insurance pricing [1]." and result["agent_mode"]==provider
    assert len(requests)==6 and len(result["tool_execution"])==4
    checks=[message for message in requests[-1]["messages"] if message["role"]=="user" and isinstance(message.get("content"),str) and message["content"].startswith("Evidence sufficiency check")]
    assert len(checks)==1


@pytest.mark.usefixtures("mock_reader_policy")
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_native_finish_gap_check_continues_real_search_read_and_new_finish(tmp_path, monkeypatch, provider):
    from climate_monitor import article_content_adapter
    import hashlib
    chat=owner(tmp_path);key=chat.responder.kb.chunks[0].id;url="https://example.org/current-energy"
    fresh="chat-"+hashlib.sha256(url.encode()).hexdigest()[:24]
    def script(index,request):
        if index==0:return [("research_state",{"action":"plan","time_basis":"current","required_outputs":[{"id":"facts","task":"current energy finding","requires_current_body":True}]})]
        if index==1:return [("search_knowledge",{"target":"wiki","query":"climate hazards"})]
        if index==2:return [("get_source_details",{"evidence_id":key})]
        if index==3:return [("research_state",{"action":"finish","results":[{"id":"facts","status":"gap","evidence_ids":[key],"gap":"No current energy body has been read."}]})]
        if index==4:return "Current energy details remain unverified."
        if index==5:
            assert "Evidence sufficiency check" in json.dumps(request) and request.get("tools")
            return [("search_web",{"query":"energy transition official facts"})]
        if index==6:
            turn=chat._test_turn
            assert turn.research_finish is None and turn.final_checked and turn.research_plan
            return [("read_url",{"url":url})]
        if index==7:return [("research_state",{"action":"finish","results":[{"id":"facts","status":"supported","evidence_ids":[fresh]}]})]
        return "The official body confirms battery storage grid investment cite"+fresh+"."
    requests=scripted_provider(chat,monkeypatch,provider,script,script_controls_plan=True,check_drafts=True)
    searches=install_fake_native_search(chat,url)
    init=EvidenceTurn.__init__
    def capture(self,*args,**kwargs):init(self,*args,**kwargs);chat._test_turn=self
    monkeypatch.setattr(EvidenceTurn,"__init__",capture)
    reads=[]
    monkeypatch.setattr(article_content_adapter,"fetch_article_content",lambda key,page,**kwargs:reads.append(page) or {"status":"ok","content":"Battery storage grid investment is confirmed in this official body.","final_url":page,"content_hash":"actual-energy-body"})
    result=chat.answer("Explain current energy transition findings")
    assert len(requests)==9 and len(searches)==1 and reads==[url]
    assert [row["tool"] for row in result["tool_execution"]]==["research_state","search_knowledge","get_source_details","research_state","search_web","read_url","research_state"]
    assert result["text"].startswith("The official body confirms battery storage grid investment [2].")
    assert result["agent_mode"]==provider and result["sources"][1]["version"]=="actual-energy-body"
    assert sum(message["role"]=="user" and isinstance(message.get("content"),str) and message["content"].startswith("Evidence sufficiency check") for message in requests[-1]["messages"])==1

@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_native_finish_tool_cap_synthesizes_without_another_check(tmp_path, monkeypatch, provider):
    from agentic_wiki import chat_evidence
    chat=owner(tmp_path)
    def script(index,request):
        assert not request.get("tools")
        assert "Evidence sufficiency check" not in json.dumps(request)
        return "Retained facts are partial; check the official publisher for current facts."
    requests=scripted_provider(chat,monkeypatch,provider,script,check_drafts=True,script_controls_plan=True)
    turn=EvidenceTurn(chat,"Explain current facts",None,[])
    turn.execute("research_state",{"action":"plan","time_basis":"current","required_outputs":[{"id":"facts","task":"current facts","requires_current_body":True}]})
    turn.calls=chat_evidence.MAX_TOOLS
    text=turn.model_answer()
    assert "partial" in text and len(requests)==1 and not turn.final_checked

@pytest.mark.parametrize("style", ["single", "multiple", "mixed"])
def test_native_wrapped_canonical_tokens_keep_registered_identity_and_reject_unknown(tmp_path, style):
    turn=EvidenceTurn(owner(tmp_path),"Explain findings",None,[])
    for key in ("first", "wiki:second", "chat-third"):
        turn.add({"evidence_id":key,"title":key},kind="wiki",text="Retained finding "+key)
    native=("Third cite[[cite:chat-third]] then first cite[[cite:first]]." if style=="single" else
        "Third then first cite[[cite:chat-third]][[cite:first]]." if style=="multiple" else
        "Third then first cite[[cite:chat-third]]first.")
    answer=turn.final_text(native+" Second [[cite:wiki:second]]. Unknown cite[[cite:unregistered]].")
    assert "[3]" in answer and "[1]" in answer and answer.index("[3]")<answer.index("[1]")
    assert "[2]" in answer and "" not in answer and "[[cite:" not in answer and "unregistered" not in answer
    assert "Second [2]. Unknown ." in answer and "Citation limitation" in answer
    assert len(turn.sources)==3


def test_native_wrapped_real_openai2_tokens_keep_fixed_registered_indices(tmp_path):
    # Captured first-attempt4fb OpenAI2 response: the wrapper contains the full canonical token.
    ids=["event-cf76e68c09bc6448b0a936bc","event-d01c144e347592cf50d3a989","event-fac277962acf96bea74ba384",
        "article-3d6956d25e27ad6a96a428b7","chat-cabb520f692a7a2920f7e924"]
    turn=EvidenceTurn(owner(tmp_path),"Summarize upcoming dates",None,[])
    for key in ids:turn.add({"evidence_id":key,"title":key},kind="wiki",text="Registered retained evidence.")
    raw="First cite[[cite:event-cf76e68c09bc6448b0a936bc]]. Fifth cite[[cite:chat-cabb520f692a7a2920f7e924]]. Third cite[[cite:event-fac277962acf96bea74ba384]]. Fourth cite[[cite:article-3d6956d25e27ad6a96a428b7]]."
    assert turn.final_text(raw)=="First [1]. Fifth [5]. Third [3]. Fourth [4]."


@pytest.fixture
def reader_dependency_missing(monkeypatch):
    import builtins
    original = builtins.__import__
    def missing(name, *args, **kwargs):
        if name.startswith("web_listening"):
            raise ModuleNotFoundError("No module named 'web_listening'")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", missing)


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("through_tool", [False, True])
def test_missing_reader_policy_is_unavailable_without_fetch_or_cached_evidence(tmp_path, monkeypatch, reader_dependency_missing, cached, through_tool):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    turn = EvidenceTurn(chat, "Read https://example.org/page", None, [])
    url = "https://example.org/page"
    if cached:
        turn.web[url] = {"evidence_id": "chat-cached", "body": "Old retained page body."}
    prior_web = copy.deepcopy(turn.web)
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw: pytest.fail("Unavailable policy must not fetch"))
    result = turn.execute("read_url", {"url": url}) if through_tool else turn.read_url(url)
    assert result["status"] == "unavailable" and result["reason"] == article_content_adapter.UNAVAILABLE_REASON
    assert result["requested_url"] == result["reader_url"] == url
    assert turn.web == prior_web and not turn.sources and not turn.evidence and turn.reads == 0
    if through_tool:
        assert turn.trace[-1]["status"] == "unavailable"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_missing_reader_policy_result_reaches_provider_honestly(tmp_path, monkeypatch, reader_dependency_missing, provider):
    from climate_monitor import article_content_adapter
    chat = owner(tmp_path)
    url = "https://example.org/page"
    monkeypatch.setattr(article_content_adapter, "fetch_article_content", lambda *a, **kw: pytest.fail("Unavailable policy must not fetch"))
    def script(index, request):
        if index == 0:
            return [("read_url", {"url": url})]
        payload = json.dumps(request)
        assert article_content_adapter.UNAVAILABLE_REASON in payload and 'unavailable' in payload
        return "The governed reader is unavailable; this page could not be read. Open the official page manually to verify the requested facts."
    scripted_provider(chat, monkeypatch, provider, script)
    result = chat.answer("Check the supplied page evidence")
    assert result["agent_mode"] == provider and not result["sources"] and "could not be read" in result["text"]
    assert any(row["tool"] == "read_url" and row["status"] == "unavailable" for row in evidence_trace(result))
    assert not chat.frames.get(result["context"])["web"]
