from agentic_wiki import AgenticWikiResponder, WikiKnowledgeBase


def test_wiki_index_loads_documents_and_chunks():
    kb = WikiKnowledgeBase()

    assert kb.stats()["documents"] >= 30
    assert kb.stats()["chunks"] >= kb.stats()["documents"]
    assert any(doc["path"] == "wiki/index.md" for doc in kb.document_catalog())


def test_context_path_prioritizes_active_note():
    result = AgenticWikiResponder().answer(
        "这页主要讲什么？",
        context_path="wiki/parametric-insurance.md",
        language="zh",
    )

    assert result["sources"]
    assert result["sources"][0]["path"] == "wiki/parametric-insurance.md"
