from copy import deepcopy
from pathlib import Path

from agentic_wiki import WikiKnowledgeBase
from climate_registry.wiki import _registry_source_observation_pages, _render_registry_source_observations
from scripts.publish_weekly_reports import validate_allowlist


def _observation(identifier="original", **changes):
    return {"occurrence_id": identifier, "imported_at": "2026-09-28T00:00:00Z",
        "source_document_sha256": "a" * 64, "source_document": "approved.pdf", "page": 26,
        "raw_url": "https://example.org/study", "anchor_text": "Bank nature-risk guidance",
        "summary": "Banks assess pollinator risk in agricultural credit.", **changes}


def test_pages_are_stable_distinct_and_incremental():
    first = _observation(source_observations=[{"filename": "approved.pdf", "path": "/archive/original.pdf"}])
    duplicate = _observation("copy", imported_at="2026-10-01T00:00:00Z",
        source_observations=[{"filename": "approved-copy.pdf", "path": "/archive/copy.pdf"}], verified_information={
        "summary": "Website confirms nature-risk guidance.", "categories": ["Nature risk"],
        "keywords": ["pollinator"], "source_url": first["raw_url"], "generated_at": "2026-10-01"})
    separate = _observation("separate", summary="Insurance covers a separate nature matter.")
    items = [{"occurrences": [first, duplicate, separate]}]
    pages = _registry_source_observation_pages(items)
    assert pages == _registry_source_observation_pages(deepcopy(items))
    assert pages == _registry_source_observation_pages([{"occurrences": list(reversed(items[0]["occurrences"]))}])
    assert len(pages) == 2
    original_page = "registry-source-observation-original.md"
    assert original_page in pages
    assert first["summary"] in pages[original_page]
    assert duplicate["verified_information"]["summary"] in pages[original_page]
    assert "approved.pdf" in pages[original_page] and "approved-copy.pdf" in pages[original_page]
    assert "page 26" in pages[original_page]
    from climate_registry.information_checks import deduplicate_pdf_occurrences
    merged = deduplicate_pdf_occurrences([first, duplicate], preserve_first_identity=True)[0]
    assert merged["source_observations"] == first["source_observations"] + duplicate["source_observations"]
    assert merged["verified_information"] == duplicate["verified_information"]
    assert merged["occurrence_id"] == first["occurrence_id"]
    assert "a" * 64 in pages[original_page] and first["raw_url"] in pages[original_page]
    assert pages[original_page].startswith("# Bank nature-risk guidance")
    directory = _render_registry_source_observations(items)
    assert first["summary"] not in directory and separate["summary"] not in directory
    assert all(name.removesuffix(".md") in directory for name in pages)
    addition = _observation("new", imported_at="2026-10-02T00:00:00Z", page=27)
    next_pages = _registry_source_observation_pages([{"occurrences": [*items[0]["occurrences"], addition,
        {**duplicate, "occurrence_id": "new-copy", "imported_at": "2026-10-03T00:00:00Z"}]}])
    assert len(next_pages) == 3 and all(next_pages[name] == content for name, content in pages.items())
    validate_allowlist([("A", "wiki/" + name) for name in pages])


def test_public_runtime_pages_deduplicate_and_remain_in_catalog_and_graph(tmp_path):
    wiki, overlay, sources = [tmp_path / name for name in ("wiki", "overlay", "sources")]
    for directory in (wiki, overlay, sources):
        directory.mkdir()
    original = _observation()
    public = _registry_source_observation_pages([{"occurrences": [original]}])
    checked = {**original, "occurrence_id": "runtime-copy", "imported_at": "2026-10-01T00:00:00Z",
        "verified_information": {"summary": "Website confirms nature-risk guidance.",
            "categories": ["Nature risk"], "keywords": ["pollinator"],
            "source_url": original["raw_url"], "generated_at": "2026-10-01"}}
    runtime = _registry_source_observation_pages([{"occurrences": [checked]}])
    for directory, pages in ((wiki, public), (overlay, runtime)):
        for name, content in pages.items():
            (directory / name).write_text(content, encoding="utf-8")
    (wiki / "registry-source-observations.md").write_text(
        _render_registry_source_observations([{"occurrences": [original]}]), encoding="utf-8")
    (wiki / "climate-monitor-2026-09-28.md").write_text("# Weekly report\n\nReal weekly content.")
    kb = WikiKnowledgeBase(wiki, sources, overlay)
    assert sum(chunk.markdown.count(original["summary"]) for chunk in kb.chunks) == 1
    assert not any(chunk.path == "wiki/registry-source-observations.md" for chunk in kb.chunks)
    observation_chunks = [chunk for chunk in kb.chunks if chunk.path.startswith("wiki/registry-source-observation-")]
    assert len(observation_chunks) == 1
    assert observation_chunks[0].heading.startswith("PDF report observation:")
    assert "Verified information" in "\n".join(doc.markdown for doc in kb.documents)
    assert any(item["display_title"] == "Bank nature-risk guidance" for item in kb.document_catalog())
    assert any(doc.type == "daily" for doc in kb.documents)
    graph = kb.graph_catalog()["notes"]
    assert any(node["id"] == "wiki/" + next(iter(public)) for node in graph["nodes"])
    assert any(original["raw_url"] in hit.chunk.urls and "page 26" in hit.chunk.markdown
        for hit in kb.search("pollinator agricultural credit", top_k=10))

    from api_server import WikiStaticFiles
    static = WikiStaticFiles(directory=str(wiki))
    static.all_directories.insert(0, str(overlay))
    served = {name: static._merged_markdown(name) for name in [*public, *runtime]}
    assert all(content.count(original["summary"]) == 1 for content in served.values())
    assert "Verified information" in served[next(iter(runtime))]



def test_untitled_observation_has_readable_citation_title():
    pages = _registry_source_observation_pages([{"occurrences": [_observation(anchor_text="", title=None)]}])
    assert next(iter(pages.values())).startswith("# PDF source observation: approved.pdf (page 26)")



def test_real_directory_page_keeps_pdf_passage_also_projected_into_article(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api_server import WikiStaticFiles

    monkeypatch.delenv("CLIMATE_REGISTRY_DB", raising=False)
    wiki = Path(__file__).resolve().parents[1] / "wiki"
    filename = "registry-source-observation-pdf-article-occurrence-79311091c7319e201be9bae5.md"
    expected = (wiki / filename).read_text(encoding="utf-8")
    assert filename.removesuffix(".md") in (wiki / "registry-source-observations.md").read_text(encoding="utf-8")
    article = (wiki / "article-article-03c3752c72830e638093f6c3.md").read_text(encoding="utf-8")
    passage = "USD 7.3 trillion flowed to nature-negative activities in 2023"
    assert passage in expected and passage in article
    app = FastAPI()
    app.mount("/wiki", WikiStaticFiles(directory=str(wiki)))
    response = TestClient(app).get("/wiki/" + filename)
    assert response.status_code == 200 and response.text == expected
    assert passage in response.text and "IAA_CSC_Climate_Report_20260928.pdf" in response.text
    assert "c4cebda1bafb1cfd2c0e9c178db8b53b69f2b746cae543231603475ca6a66abc" in response.text
    assert "page 33" in response.text
    assert "https://www.weforum.org/stories/climate-action/healthy-landscapes-and-seascapes-need-more-than-finance/" in response.text
