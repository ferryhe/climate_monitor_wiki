from collections import Counter
from datetime import date, timedelta
from pathlib import Path
import re

import anyio
import climate_registry.publication as registry_publication
from agentic_wiki import AgenticWikiResponder, WikiKnowledgeBase
from agentic_wiki.wiki_agent import _expand_query, _is_displayable_graph_concept, _requested_dates, _strip_markdown
from api_server import WikiStaticFiles, app, responder
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _chunk_signature(chunks):
    return [
        (
            chunk.id, chunk.title, chunk.path, chunk.heading, chunk.type,
            chunk.date, chunk.text, chunk.markdown, tuple(chunk.links),
            chunk.corpus, chunk.document_title, chunk.canonical_url,
            chunk.checked_source_url, chunk.original_source_url,
            chunk.checked_source_provenance, tuple(chunk.tokens), tuple(chunk.urls),
            chunk.date_metadata,
        )
        for chunk in chunks
    ]


def _assert_chunks_match_final_documents(kb, chunk_document):
    expected_wiki = [
        chunk for doc in kb.documents for chunk in chunk_document(kb, doc)
    ]
    expected_source = [
        chunk for doc in kb.source_documents for chunk in chunk_document(kb, doc)
    ]
    actual_wiki = [chunk for chunk in kb.chunks if chunk.corpus == "wiki"]
    actual_source = [chunk for chunk in kb.chunks if chunk.corpus == "source"]
    assert _chunk_signature(actual_wiki) == _chunk_signature(expected_wiki)
    assert _chunk_signature(actual_source) == _chunk_signature(expected_source)
    assert _chunk_signature(kb.chunks) == _chunk_signature(expected_wiki + expected_source)
    assert kb.token_chunk_counts == Counter(
        token for chunk in expected_wiki + expected_source for token in set(chunk.tokens)
    )
    return expected_wiki, expected_source


def test_wiki_index_loads_documents_and_chunks():
    kb = WikiKnowledgeBase()

    assert kb.stats()["documents"] >= 30
    assert kb.stats()["source_documents"] >= 10
    assert kb.stats()["concepts"] >= 10
    assert kb.stats()["chunks"] >= kb.stats()["documents"]
    assert any(doc["path"] == "wiki/index.md" for doc in kb.document_catalog())
    assert any(concept["label"] == "Parametric Insurance" for concept in kb.concept_catalog())


def test_explorer_graph_uses_display_titles_and_filters_only_graph_concepts(tmp_path):
    wiki, sources = tmp_path/"wiki", tmp_path/"sources"
    wiki.mkdir(); sources.mkdir()
    fixture_articles = {
        "80e0b5d29c869655b3c4fa50": ("Diabetes", "who.int"),
        "9d539ef65354a1bfdc7b135c": ("climate", "wri.org"),
        "b59d17b73ebf5cba1f54e094": ("news", "wri.org"),
        "f725cf7cae3ac18f4bdfa9a2": ("unep fi at cop31", "unepfi.org"),
    }
    for identity, (title, host) in fixture_articles.items():
        (wiki/f"article-article-{identity}.md").write_text(
            f"# {title}\n\nCanonical article: https://{host}/fixture\n\n## Date Observations\n\nClimate Risk.", encoding="utf-8")
    for identity, host, page in (("0c5feabb1862ce55d5d0a549", "imf.org", 20), ("3b48d57498ab73453817d499", "g20.org", 19)):
        (wiki/f"registry-source-observation-pdf-article-occurrence-{identity}.md").write_text(
            f"# {host}\n\n## PDF report observation: fixture\n\nWebsite: {host}\nPDF: fixture.pdf; page {page}\nClimate Risk.", encoding="utf-8")
    kb = WikiKnowledgeBase(wiki, sources)
    catalog = {row["path"]: row for row in kb.document_catalog()}
    notes = kb.graph_catalog()["notes"]
    note_nodes = {node["id"]: node for node in notes["nodes"]}
    examples = {
        "wiki/article-article-80e0b5d29c869655b3c4fa50.md": "Diabetes",
        "wiki/article-article-9d539ef65354a1bfdc7b135c.md": "Climate — wri.org",
        "wiki/article-article-b59d17b73ebf5cba1f54e094.md": "News — wri.org",
        "wiki/article-article-f725cf7cae3ac18f4bdfa9a2.md": "UNEP FI at COP31 — unepfi.org",
        "wiki/registry-source-observation-pdf-article-occurrence-0c5feabb1862ce55d5d0a549.md": "PDF source observation · imf.org · p. 20",
        "wiki/registry-source-observation-pdf-article-occurrence-3b48d57498ab73453817d499.md": "PDF source observation · g20.org · p. 19",
    }

    assert notes["static_layout"] is True
    for path, display_title in examples.items():
        assert catalog[path]["display_title"] == display_title
        assert note_nodes[path]["label"] == display_title
        assert note_nodes[path]["refPath"] == path
    assert catalog["wiki/article-article-80e0b5d29c869655b3c4fa50.md"]["title"].startswith("article-article-")

    concept_labels = {node["label"] for node in kb.graph_catalog()["keywords"]["nodes"] if node["kind"] == "keyword"}
    assert "Date Observations" in {concept["label"] for concept in kb.concept_catalog()}
    assert "Date Observations" not in concept_labels
    assert "Climate Risk" in concept_labels
    assert _is_displayable_graph_concept("Diabetes") is True
    assert _is_displayable_graph_concept("Climate Risk") is True
    assert _is_displayable_graph_concept("imf.org") is False


def test_subject_rarity_and_chunk_length_outweigh_repeated_audience_words(tmp_path):
    wiki, sources = tmp_path / "wiki", tmp_path / "sources"
    wiki.mkdir()
    sources.mkdir()
    for index in range(20):
        (wiki / f"background-{index}.md").write_text("# Insurance background\n\n" + "Insurance actuaries climate risk disclosure. " * 100)
    (sources / "climate-monitor-2026-10-08.md").write_text("# climate-monitor-2026-10-08\n\n## Executive Summary\n\n" + "Insurance actuaries climate risk disclosure. " * 100)
    (wiki / "storage.md").write_text("# Renewable energy storage\n\nEnergy transition battery storage brings construction and fire risks.")
    kb = WikiKnowledgeBase(wiki, sources)
    hits = kb.search("recent energy transition insurance actuaries climate risk", top_k=3)
    assert hits[0].chunk.path == "wiki/storage.md"
    assert "latest dated report" in next(hit.reason for hit in kb.search("latest insurance report", top_k=30) if hit.chunk.corpus == "source")


def test_runtime_registry_overlay_merges_public_history_and_keeps_other_precedence(tmp_path, monkeypatch):
    base, overlay, sources = (
        tmp_path / "wiki", tmp_path / "runtime", tmp_path / "sources"
    )
    for directory in (base, overlay, sources):
        directory.mkdir()
    (base / "article-shared.md").write_text(
        "# Shared\n\nPublished base body.\n\n"
        "## PDF report observation: Public report\n\n"
        "PDF: public-report.pdf; SHA-256: " + "b" * 64 + "; page 4\n",
        encoding="utf-8",
    )
    (overlay / "article-shared.md").write_text(
        "# Shared\n\nActivated runtime Web body.\n", encoding="utf-8"
    )
    (base / "registry-source-observations.md").write_text(
        "# Public source history\n\nPublic PDF history.\n", encoding="utf-8"
    )
    (overlay / "registry-source-observations.md").write_text(
        "# Runtime source observations\n\nActivated PDF source.\n", encoding="utf-8"
    )
    (overlay / "article-runtime-only.md").write_text(
        "# Runtime only\n\nActivated-only evidence.\n", encoding="utf-8"
    )
    (base / "manual-shared.md").write_text(
        "# Manual\n\nBase manual body.\n", encoding="utf-8"
    )
    (overlay / "manual-shared.md").write_text(
        "# Manual\n\nRuntime manual body.\n", encoding="utf-8"
    )
    (base / "asset.bin").write_bytes(b"\xffbase")
    (overlay / "asset.bin").write_bytes(b"\xffruntime")
    for name in ("article-history.md", "registry-manual-history.md", "article-manual.notes.md"):
        (base / name).write_text(f"# {name}\n\nBase history.\n", encoding="utf-8")

    (sources / "climate-monitor-2026-10-27.md").write_text(
        "# climate-monitor-2026-10-27\n\nSource evidence remains available.\n",
        encoding="utf-8",
    )
    chunk_calls = Counter()
    chunk_document = WikiKnowledgeBase._chunk_document

    def count_chunk_calls(self, doc):
        chunk_calls[(doc.corpus, doc.path)] += 1
        return chunk_document(self, doc)

    monkeypatch.setattr(WikiKnowledgeBase, "_chunk_document", count_chunk_calls)
    kb = WikiKnowledgeBase(base, sources, overlay)
    static = WikiStaticFiles(directory=base)
    static.all_directories = [str(overlay), str(base)]
    static_app = FastAPI()
    static_app.mount("/wiki", static)
    client = TestClient(static_app)

    shared = [doc for doc in kb.documents if doc.path == "wiki/article-shared.md"]
    assert len(shared) == 1
    assert "Published base body" in shared[0].markdown
    assert "Activated runtime Web body" in shared[0].markdown
    public_pdf_chunks = [
        chunk for chunk in kb.chunks if "public-report.pdf" in chunk.markdown
    ]
    assert public_pdf_chunks
    assert all(
        "Activated runtime Web body" not in chunk.markdown
        for chunk in public_pdf_chunks
    )
    assert any(
        "Activated runtime Web body" in chunk.markdown for chunk in kb.chunks
    )
    source_history = next(
        doc for doc in kb.documents
        if doc.path == "wiki/registry-source-observations.md"
    )
    assert "Public PDF history" in source_history.markdown
    assert "Activated PDF source" in source_history.markdown
    merged = client.get("/wiki/article-shared.md")
    assert "Published base body" in merged.text
    assert "Activated runtime Web body" in merged.text
    head = client.head("/wiki/article-shared.md")
    assert head.content == b""
    assert int(head.headers["content-length"]) == len(merged.content)
    direct_get = anyio.run(
        static.get_response, "article-shared.md", {"method": "GET"}
    )
    direct_head = anyio.run(
        static.get_response, "article-shared.md", {"method": "HEAD"}
    )
    assert direct_head.body == b""
    assert direct_head.headers["content-length"] == str(len(direct_get.body))
    source_page = client.get("/wiki/registry-source-observations.md")
    assert "Public PDF history" in source_page.text
    assert "Activated PDF source" in source_page.text
    manual = client.get("/wiki/manual-shared.md")
    assert "Runtime manual body" in manual.text
    assert "Base manual body" not in manual.text
    assert client.get("/wiki/asset.bin").content == b"\xffruntime"
    paths = {doc.path for doc in kb.documents}
    for name in ("article-history.md", "registry-manual-history.md", "article-manual.notes.md"):
        assert f"wiki/{name}" in paths
        assert static.lookup_path(name)[1] is not None

    expected_paths = [
        "wiki/article-history.md",
        "wiki/article-manual.notes.md",
        "wiki/registry-manual-history.md",
        "wiki/article-runtime-only.md",
        "wiki/article-shared.md",
        "wiki/manual-shared.md",
        "wiki/registry-source-observations.md",
    ]
    assert [doc.path for doc in kb.documents] == expected_paths
    kb_by_path = {doc.path: doc for doc in kb.documents}
    assert "Published base body" in kb_by_path["wiki/article-shared.md"].markdown
    assert "Activated runtime Web body" in kb_by_path["wiki/article-shared.md"].markdown
    assert "Public PDF history" in kb_by_path["wiki/registry-source-observations.md"].markdown
    assert "Activated PDF source" in kb_by_path["wiki/registry-source-observations.md"].markdown
    assert "Runtime manual body" in kb_by_path["wiki/manual-shared.md"].markdown
    assert "Base manual body" not in kb_by_path["wiki/manual-shared.md"].markdown
    assert [doc.path for doc in kb.source_documents] == [
        "sources/climate-monitor-2026-10-27.md"
    ]
    expected_wiki_chunks, expected_source_chunks = _assert_chunks_match_final_documents(
        kb, chunk_document
    )
    assert expected_wiki_chunks and expected_source_chunks
    assert chunk_calls == Counter(
        {("wiki", doc.path): 1 for doc in kb.documents}
        | {("source", doc.path): 1 for doc in kb.source_documents}
    )

    next_generation = tmp_path / "next-runtime"
    next_generation.mkdir()
    fallback = WikiKnowledgeBase(base, sources, next_generation)
    fallback_paths = {doc.path for doc in fallback.documents}
    fallback_shared = next(
        doc for doc in fallback.documents if doc.path == "wiki/article-shared.md"
    )
    assert "Published base body" in fallback_shared.markdown
    assert "Activated runtime body" not in fallback_shared.markdown
    assert "wiki/article-runtime-only.md" not in fallback_paths
    static.all_directories = [str(next_generation), str(base)]
    fallback_page = client.get("/wiki/article-shared.md")
    assert "Published base body" in fallback_page.text
    assert "Activated runtime Web body" not in fallback_page.text
    assert client.get("/wiki/article-runtime-only.md").status_code == 404

def test_approved_public_snapshot_chunks_final_documents_once(tmp_path, monkeypatch):
    wiki, sources = tmp_path / "wiki", tmp_path / "sources"
    wiki.mkdir()
    sources.mkdir()
    (sources / "climate-monitor-2026-10-27.md").write_text(
        "# climate-monitor-2026-10-27\n\nDisk source is not part of the approved snapshot branch.\n",
        encoding="utf-8",
    )
    pages = {
        "registry-source-observations.md": (
            "# Public observations\n\n"
            "## PDF report observation: Approved report\n\n"
            "Public approved summary.\n"
            "PDF: approved-report.pdf; SHA-256: " + "c" * 64 + "; page 4\n"
        ),
        "topic-approved.md": "# Approved topic\n\nApproved snapshot body.\n",
    }
    monkeypatch.setattr(
        registry_publication,
        "public_wiki_snapshot",
        lambda database, *, wiki_dir=None: (pages, "revision-7"),
    )
    chunk_calls = Counter()
    chunk_document = WikiKnowledgeBase._chunk_document

    def count_chunk_calls(self, doc):
        chunk_calls[(doc.corpus, doc.path)] += 1
        return chunk_document(self, doc)

    monkeypatch.setattr(WikiKnowledgeBase, "_chunk_document", count_chunk_calls)
    kb = WikiKnowledgeBase(
        wiki, sources, approved_registry_snapshot=tmp_path / "approved.sqlite3"
    )

    assert kb.public_registry_revision == "revision-7"
    assert [doc.path for doc in kb.documents] == [
        "wiki/registry-source-observations.md",
        "wiki/topic-approved.md",
    ]
    assert [doc.markdown for doc in kb.documents] == [
        pages["registry-source-observations.md"],
        pages["topic-approved.md"],
    ]
    assert kb.source_documents == []
    expected_wiki_chunks, expected_source_chunks = _assert_chunks_match_final_documents(
        kb, chunk_document
    )
    assert expected_wiki_chunks and expected_source_chunks == []
    assert chunk_calls == Counter({("wiki", doc.path): 1 for doc in kb.documents})


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


def test_api_config_exposes_graph_and_dataview_fields(monkeypatch):
    base = "https://github.com/ferryhe/climate_monitor_wiki/blob/main"
    monkeypatch.setattr(responder, "base_source_url", base)
    monkeypatch.setattr(responder, "source_document_base_url", base)
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
        "Generate PDF report",
        "Key dates & opportunities",
        "New reports & articles",
        "Insurance implications",
        "Regulation & disclosure",
        "Physical risks",
        "Transition risks",
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

    daily_doc = next(doc for doc in payload["documents"] if doc["path"] == "wiki/climate-monitor-2026-08-17.md")
    assert daily_doc["source_path"] == "sources/climate-monitor-2026-08-17.md"
    assert (
        daily_doc["source_url"]
        == "https://github.com/ferryhe/climate_monitor_wiki/blob/main/sources/climate-monitor-2026-08-17.md"
    )


def test_showcase_root_defaults_to_chat_and_contains_workspaces():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert 'id="chatView"' in body
    assert 'id="registryView"' in body
    assert 'id="obsidianView"' in body
    assert 'id="messageInput"' in body
    assert 'id="graphSvg"' in body
    assert 'id="rows"' in body
    assert 'data-answer-mode="detailed"' in body
    assert 'id="answerModeHint"' not in body
    assert 'id="registryPublisherCustom"' in body
    assert 'data-graph-mode="keywords"' in body
    assert body.index('id="chatTab"') < body.index('id="registryTab"')
    chat_tab_opening_tag = body[body.index('id="chatTab"') : body.index(">", body.index('id="chatTab"'))]
    assert 'class="tabbar__tab is-active"' in chat_tab_opening_tag
    assert 'aria-selected="true"' in chat_tab_opening_tag
    registry_tab_opening_tag = body[body.index('id="registryTab"') : body.index(">", body.index('id="registryTab"'))]
    assert 'aria-selected="false"' in registry_tab_opening_tag
    registry_opening_tag = body[body.index('id="registryView"') : body.index(">", body.index('id="registryView"'))]
    assert "hidden" in registry_opening_tag
    chat_opening_tag = body[body.index('id="chatView"') : body.index(">", body.index('id="chatView"'))]
    assert "hidden" not in chat_opening_tag
    assert body.index("Graph View") < body.index("Page Index")
    keyword_button = body.index('data-graph-mode="keywords"')
    assert 'is-active' in body[keyword_button - 120 : keyword_button]


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
    assert "Generate a monitoring PDF report for the last 14 days." in body
    assert "New reports & articles" in body
    assert "Summarize the past 4 weeks by theme" not in body
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


def test_executive_mode_produces_structured_window_brief_offline(tmp_path):
    wiki, sources = tmp_path/"wiki", tmp_path/"sources"
    wiki.mkdir(); sources.mkdir()
    report = "# Climate Monitor 2026-04-01\n\n## Executive Summary\n\nPhysical climate risk and insurance monitoring evidence.\n"
    for directory in (wiki, sources):
        (directory/"climate-monitor-2026-04-01.md").write_text(report, encoding="utf-8")
        (directory/"climate-monitor-2026-05-04.md").write_text(report.replace("2026-04-01", "2026-05-04"), encoding="utf-8")
    responder_instance = AgenticWikiResponder(wiki_dir=wiki, source_dir=sources)
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
        match.group(1)
        for document in (
            *responder_instance.kb.documents,
            *getattr(responder_instance.kb, "source_documents", ()),
        )
        if (match:=re.fullmatch(r"climate-monitor-(\d{4}-\d{2}-\d{2})\.md",document.file))
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
        match.group(1)
        for document in (
            *responder_instance.kb.documents,
            *getattr(responder_instance.kb, "source_documents", ()),
        )
        if (match:=re.fullmatch(r"climate-monitor-(\d{4}-\d{2}-\d{2})\.md",document.file))
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
        match.group(1)
        for document in (
            *responder_instance.kb.documents,
            *responder_instance.kb.source_documents,
        )
        if (match:=re.fullmatch(r"climate-monitor-(\d{4}-\d{2}-\d{2})\.md",document.file)) and match.group(1) in window_dates
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
