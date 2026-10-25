import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentic_wiki.wiki_agent import WikiKnowledgeBase


@pytest.fixture
def current_article_catalog(tmp_path):
    from copy import deepcopy
    from climate_registry.article_dates import select_article_dates, content_date_projection
    from climate_registry.wiki import _render_registry_article
    from test_article_content_dates import _bundle_snapshot
    wiki, sources = tmp_path/"wiki", tmp_path/"sources"
    wiki.mkdir(); sources.mkdir()
    base = {"article_id": "a", "title": "Climate loss report", "canonical_url": "https://example.org/a",
        "summary": "Existing loss report summary"}
    updated = {"kind": "page_information", "observed_at": "2026-10-16", "recorded_at": "2026-10-24T12:00:00Z",
        "evidence": {"date_kind": "updated", "date_precision": "day", "source_url": base["canonical_url"],
            "source_system": "fixture", "table": "article_date_observations", "record_id": "update"}}
    records = {"unknown": {**base, "date_observations": [updated], **select_article_dates([updated])},
        "collection": {**base, **select_article_dates([], collection_times=["2026-10-02T00:00:00Z", "2026-10-24T00:00:00Z"])},
        "publication": {**base, "publication_date": "2026-10-10"}}
    snapshots = {}
    for basis in ("updated_date", "revision_date", "content_version_discovery"):
        snapshot = _bundle_snapshot(); bundle = snapshot["derived_display"]["content_update"]
        if basis == "revision_date":
            proof = bundle["date_evidence"].pop("updated_date"); proof["date_kind"] = "revision"
            bundle["date_evidence"][basis] = proof
        elif basis == "content_version_discovery":
            bundle["date_evidence"] = {}
        records[basis] = {**base, "article_id": "good", "publication_date": "2026-10-02",
            "published_candidate_sha256": "c"*64, **snapshot["derived_display"]}
        snapshots[basis] = snapshot
    month = {"kind": "page_information", "observed_at": "2026-10", "evidence": {
        "date_kind": "published", "date_precision": "month", "date_text": "October 2026"}}
    records["month-source"] = {**base, **select_article_dates([month])}
    records["month-human"] = deepcopy(records["month-source"])
    proof = {**records["month-human"]["publication_date_evidence"], "authority": "human_override"}
    records["month-human"]["manual_date_overrides"] = {"publication_date": {"scope": "record", "new_value": "2026-10-15", "evidence": proof}}
    pages = {}
    for name, article in records.items():
        article.update(content_date_projection(article, snapshots.get(name)))
        pages[name] = _render_registry_article(article)
        (wiki/f"article-{name}.md").write_text(pages[name], encoding="utf-8")
    return WikiKnowledgeBase(wiki, sources).document_catalog(), pages


def test_registry_catalog_respects_authoritative_unknown_and_current_date_bases(current_article_catalog):
    catalog, pages = current_article_catalog
    rows = {row["path"].removeprefix("wiki/article-").removesuffix(".md"): row for row in catalog}
    assert rows["unknown"]["display_date"] is None and rows["unknown"]["display_date_basis"] == "unknown"
    assert rows["unknown"]["content_date_authority"] == "unknown"
    assert "Page information date: 2026-10-16" in pages["unknown"] and "page date kind: updated" in pages["unknown"]
    for basis in ("updated_date", "revision_date", "content_version_discovery"):
        assert rows[basis]["display_date"] == "2026-10-16" and rows[basis]["display_date_basis"] == basis
    assert rows["collection"]["display_date"] == "2026-10-02" and rows["collection"]["display_date_basis"] == "collection_time"
    for name in ("month-source", "month-human"):
        assert rows[name]["display_date"] == "2026-10-15" and rows[name]["content_date_precision"] == "month"
        assert rows[name]["content_date_normalization"] == "month_midpoint"
    assert rows["month-human"]["content_date_authority"] == "human_override"


@pytest.mark.parametrize("prefix", ["article", "pdf-article"])
@pytest.mark.parametrize("name", ["unknown", "revision_date", "content_version_discovery", "month-human",
    "updated_date", "collection", "publication", "month-source"])
def test_generated_registry_article_dates_reach_generic_chat_sources(current_article_catalog, tmp_path, monkeypatch, name, prefix):
    from agentic_wiki.wiki_agent import AgenticWikiResponder
    from agentic_wiki.chat_evidence import EvidenceTurn
    catalog, pages = current_article_catalog
    wiki = tmp_path / "retrieval" / "wiki"
    wiki.mkdir(parents=True)
    (wiki / f"{prefix}-{name}.md").write_text(pages[name], encoding="utf-8")
    monkeypatch.delenv("CLIMATE_CHAT_PROVIDER", raising=False)
    responder = AgenticWikiResponder(wiki, tmp_path / "retrieval" / "sources")
    responder.client = None
    responder.anthropic_client = None
    expected = next(row for row in catalog if row["path"] == f"wiki/article-{name}.md")
    date = expected["display_date"] or "-"
    metadata_keys = ("content_date", "content_date_basis", "content_date_precision", "content_date_normalization",
        "content_date_authority", "content_date_evidence", "content_date_label", "published_candidate_sha256", "content_version_id")
    query = "Explain loss report findings"
    hits = responder.kb.search(query)
    assert hits and all(hit.chunk.date == (expected["display_date"] or "") for hit in hits)
    turn = EvidenceTurn(responder.chat_evidence, query, None, [])
    found = turn.search_knowledge(query)  # The ordinary auto route, not recent_articles.
    assert found and turn.sources
    detail = turn.get_source_details(turn.sources[0]["evidence_id"])
    assert detail["source"]["content_date_basis"] == expected["display_date_basis"]
    direct = responder.answer(query, language="en", answer_mode="brief")
    chat = responder.chat_evidence.answer(query, language="en", answer_mode="brief")
    import api_server
    from fastapi.testclient import TestClient
    monkeypatch.setattr(api_server, "responder", responder)
    response = TestClient(api_server.app).post("/api/chat", json={"message": query, "answer_mode": "brief"})
    assert response.status_code == 200, response.text
    for sources in ([hit.to_source(i + 1) for i, hit in enumerate(hits)], turn.sources, direct["sources"], chat["sources"], response.json()["sources"]):
        assert sources
        for source in sources:
            assert source["date"] == date
            assert source["date_kind"] == expected["display_date_basis"]
            assert {key: source[key] for key in metadata_keys} == {key: expected[key] for key in metadata_keys}
    if name == "unknown":
        assert "evidence imported at: 2026-10-24" in pages[name]
        assert date == "-" and expected["content_date_basis"] == "unknown"
    if name == "month-human":
        assert expected["content_date_precision"] == "month"
        assert expected["content_date_normalization"] == "month_midpoint"
        assert expected["content_date_authority"] == "human_override"


def test_catalog_uses_same_page_titles_and_only_explicit_content_dates(tmp_path):
    wiki = tmp_path / "wiki"
    sources = tmp_path / "sources"
    wiki.mkdir()
    sources.mkdir()
    pages = {
        "article-article-a.md": (
            "# First matter title\n\n"
            "Canonical article: https://example.org/shared\n"
            "Collected at: 2026-10-07T22:14:35Z\n"
            "Updated (source observation): 2026-10-07\n"
        ),
        "article-article-b.md": (
            "# Different matter title\n\n"
            "Canonical article: https://example.org/shared\n"
            "Collected at: 2026-10-07T22:14:35Z\n\n"
            "## Date observations\n\n"
            "- Report observation date: 2026-09-14; source report: 2026-09-14; "
            "date basis: daily_or_weekly_report_date\n"
        ),
        "article-article-c.md": "# Published page\n\nPublication date: 2026-09\n",
        "article-article-d.md": "# Topic update\n\nTopic update date: 2025-11-03\n",
        "article-article-year.md": "# Year-only date\n\nPublication date: 2024\n",
        "article-article-quarter.md": "# Quarter date\n\nPublication date: 2026-Q3\n",
        "article-article-unknown.md": "# Date unknown\n\nCollected at: 2026-10-07T22:14:35Z\n",
        "article-article-page-published.md": (
            "# Page date published\n\n"
            "- Page information date: 2026-03-26; source record: source-record; "
            "page date kind: published; source URL: https://example.org/shared; "
            "evidence imported at: 2026-10-05T01:16:09Z\n"
        ),
        "article-article-page-publication-date.md": (
            "# Page date publication date\n\n"
            "- Page information date: 2026-10-13; source record: registry.article_versions 4933; "
            "page date kind: publication_date; source URL: https://example.org/shared; "
            "evidence imported at: 2026-10-15T23:00:00Z\n"
        ),
        "article-article-page-updated.md": (
            "# Page date updated\n\n"
            "- Page information date: 2025-11; source record: source-record; "
            "page date kind: updated; source URL: https://example.org/shared; "
            "evidence imported at: 2026-10-05T01:16:09Z\n"
        ),
        "article-article-page-unknown-kind.md": (
            "# Page date unknown kind\n\n"
            "- Page information date: 2026-08-04; source record: source-record; "
            "page date kind: observed; source URL: https://example.org/shared; "
            "evidence imported at: 2026-10-05T01:16:09Z\n"
        ),
        "other-topic.md": "# Other topic\n\nTopic update date: 2025-11-03\n"
            'Content date metadata: {"content_date":null,"content_date_basis":"unknown"}\n',
        "climate-monitor-2026-09-07.md": "# Weekly report title\n\nReport contents.\n",
        "index.md": "# Wiki index\n\n[[linked-page]]\n",
        "linked-page.md": "# Linked page\n\nUseful page.\n",
        "zero-links.md": "# Unlinked useful page\n\nStill useful to Chat.\n",
    }
    for name, content in pages.items():
        (wiki / name).write_text(content, encoding="utf-8")

    kb = WikiKnowledgeBase(wiki, sources)
    catalog = kb.document_catalog()
    from agentic_wiki.wiki_agent import _extract_date, SearchHit
    for doc in kb.documents:
        assert doc.date == _extract_date(doc.title, doc.markdown)
        assert doc.date_metadata == {}
    for chunk in kb.chunks:
        source = SearchHit(chunk, 1, "legacy").to_source(1)
        assert source["date"] == chunk.date
        assert "content_date_basis" not in source
    by_path = {doc["path"]: doc for doc in catalog}

    first = by_path["wiki/article-article-a.md"]
    second = by_path["wiki/article-article-b.md"]
    assert first["title"] == "article-article-a"
    assert first["file"] == "article-article-a.md"
    assert first["path"] == "wiki/article-article-a.md"
    assert first["type"] == "topic"
    assert first["display_title"] == "First matter title"
    assert first["display_date"] is None
    assert first["display_date_basis"] is None
    assert second["display_title"] == "Different matter title"
    assert second["display_date"] == "2026-09-14"
    assert second["display_date_basis"] == "report_date"
    assert by_path["wiki/article-article-c.md"]["display_date"] == "2026-09"
    assert by_path["wiki/article-article-c.md"]["display_date_basis"] == "publication_date"
    assert by_path["wiki/article-article-d.md"]["display_date"] == "2025-11-03"
    assert by_path["wiki/article-article-d.md"]["display_date_basis"] == "topic_update_date"
    assert by_path["wiki/article-article-year.md"]["display_date"] == "2024"
    assert by_path["wiki/article-article-quarter.md"]["display_date"] == "2026-Q3"
    assert by_path["wiki/article-article-unknown.md"]["display_date"] is None
    published = by_path["wiki/article-article-page-published.md"]
    assert (published["display_date"], published["display_date_basis"]) == (
        "2026-03-26",
        "publication_date",
    )
    owner_publication = by_path["wiki/article-article-page-publication-date.md"]
    assert (owner_publication["display_date"], owner_publication["display_date_basis"]) == (
        "2026-10-13",
        "publication_date",
    )
    updated = by_path["wiki/article-article-page-updated.md"]
    assert (updated["display_date"], updated["display_date_basis"]) == (
        "2025-11",
        "page_update_date",
    )
    unknown_kind = by_path["wiki/article-article-page-unknown-kind.md"]
    assert (unknown_kind["display_date"], unknown_kind["display_date_basis"]) == (
        None,
        None,
    )

    daily = by_path["wiki/climate-monitor-2026-09-07.md"]
    assert (daily["title"], daily["type"], daily["date"]) == (
        "climate-monitor-2026-09-07",
        "daily",
        "2026-09-07",
    )
    assert daily["display_title"] == "Weekly report title"
    assert daily["display_date"] == "2026-09-07"
    assert daily["display_date_basis"] == "report_date"
    assert by_path["wiki/other-topic.md"]["display_date"] == "2025-11-03"
    assert by_path["wiki/other-topic.md"]["display_date_basis"] == "topic_update_date"
    assert "wiki/zero-links.md" in by_path
    assert len(catalog) == len(pages)


def test_page_index_sorts_and_searches_by_display_metadata_without_changing_paths(current_article_catalog):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to exercise the Page Index JavaScript functions")

    app_path = Path(__file__).parents[1] / "showcase" / "app.js"
    documents = [
        {"title": "z-old", "path": "wiki/old.md", "type": "topic", "display_title": "Needle old", "display_date": "2025-04", "links": []},
        {"title": "m-tie-a", "path": "wiki/tie-a.md", "type": "topic", "display_title": "Needle tie A", "display_date": "2026-09-14", "display_date_basis": "report_date", "links": ["target"]},
        {"title": "a-new", "path": "wiki/new.md", "type": "topic", "display_title": "Needle newest", "display_date": "2026-10-04", "display_date_basis": "publication_date", "links": ["target"]},
        {"title": "b-tie-b", "path": "wiki/tie-b.md", "type": "topic", "display_title": "Needle tie B", "display_date": "2026-09-14", "display_date_basis": "report_date", "links": []},
        {"title": "quarter", "path": "wiki/quarter.md", "type": "topic", "display_title": "Needle quarter", "display_date": "2026-Q3", "display_date_basis": "publication_date", "links": []},
        {"title": "mid-quarter", "path": "wiki/mid-quarter.md", "type": "topic", "display_title": "Mid quarter date", "display_date": "2026-07-15", "display_date_basis": "publication_date", "links": []},
        {"title": "c-unknown", "path": "wiki/unknown.md", "type": "topic", "display_title": "Unknown", "display_date": None, "links": []},
        {"title": "target", "path": "wiki/target.md", "type": "topic", "display_title": "Readable target", "display_date": None, "links": []},
        {"title": "isolated", "path": "wiki/isolated.md", "type": "topic", "display_title": "Zero-link page", "display_date": None, "links": []},
    ]
    script = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
function functionSource(name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`Missing ${name}`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`Unclosed ${name}`);
}
const context = {
  GRAPH_COPY: { notes: { title: "Notes", hint: "", legendHtml: "" } },
  state: { rows: [], filteredRows: [] },
  normalizeSearchText: (value) => String(value).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim(),
  renderRows: () => {},
};
vm.createContext(context);
for (const name of ["displayDateSortKey", "buildWorkspaceData", "applyTableFilter", "normalizeGraphData", "registryArticleDateLabel", "displayDateLabel"]) {
  vm.runInContext(functionSource(name), context);
}
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const docs = payload.documents;
const data = context.buildWorkspaceData(docs);
const current = context.buildWorkspaceData(payload.current);
context.state.rows = data.rows;
context.state.rows.push({
  path: "wiki/page-update.md",
  displayTitle: "Updated page",
  displayDate: "2025-11",
  displayDateBasis: "page_update_date",
  type: "topic",
});
context.applyTableFilter("last updated date");
const updateBasisPaths = context.state.filteredRows.map((row) => row.path);
context.state.rows.pop();
context.applyTableFilter("needle");
const graph = context.normalizeGraphData("notes", {
  nodes: [{ id: "wiki/target.md", label: "target", kind: "note", type: "topic" }],
  links: [{ source: "wiki/new.md", target: "wiki/target.md" }],
});
process.stdout.write(JSON.stringify({
  currentRows: current.rows.map(row => ({path: row.path, label: context.displayDateLabel(row)})),
  rows: data.rows.map(({ path, displayTitle, inlinks }) => ({ path, displayTitle, inlinks })),
  dateLabels: [
    context.displayDateLabel(data.rows.find((row) => row.path === "wiki/new.md")),
    context.displayDateLabel(data.rows.find((row) => row.path === "wiki/unknown.md")),
    context.displayDateLabel({ displayDate: "2025-11", displayDateBasis: "page_update_date" }),
    context.displayDateLabel({ displayDate: "2026-Q3", displayDateBasis: "publication_date" }),
  ],
  edges: data.edges,
  filteredPaths: context.state.filteredRows.map((row) => row.path),
  updateBasisPaths,
  graphNodes: graph.nodes,
  graphLinks: graph.links,
}));
"""
    result = subprocess.run(
        [node, "-e", script, str(app_path)],
        input=json.dumps({"documents": documents, "current": current_article_catalog[0]}),
        text=True,
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)
    current = {row["path"].removeprefix("wiki/article-").removesuffix(".md"): row["label"] for row in data["currentRows"]}
    assert current == {
        "unknown": "Unknown", "publication": "2026-10-10 (publication date)",
        "updated_date": "2026-10-16 (source updated date)", "revision_date": "2026-10-16 (source revision date)",
        "content_version_discovery": "2026-10-16 (first observed content update)",
        "collection": "2026-10-02 (first collection date)",
        "month-source": "October 2026 (normalized to 2026-10-15 for sorting) (publication date)",
        "month-human": "October 2026 (normalized to 2026-10-15 for sorting) · human override (publication date)",
    }
    current_paths = [row["path"] for row in data["currentRows"]]
    assert current_paths[-1] == "wiki/article-unknown.md"
    assert current_paths.index("wiki/article-month-source.md") < current_paths.index("wiki/article-publication.md")

    paths = [row["path"] for row in data["rows"]]
    assert paths == [
        "wiki/new.md",
        "wiki/tie-a.md",
        "wiki/tie-b.md",
        "wiki/mid-quarter.md",
        "wiki/quarter.md",
        "wiki/old.md",
        "wiki/unknown.md",
        "wiki/target.md",
        "wiki/isolated.md",
    ]
    assert data["dateLabels"] == [
        "2026-10-04 (publication date)",
        "Unknown",
        "2025-11 (last updated date)",
        "2026-Q3 (publication date)",
    ]
    assert [row["path"] for row in data["rows"] if row["inlinks"] == 0] == [
        "wiki/new.md",
        "wiki/tie-a.md",
        "wiki/tie-b.md",
        "wiki/mid-quarter.md",
        "wiki/quarter.md",
        "wiki/old.md",
        "wiki/unknown.md",
        "wiki/isolated.md",
    ]
    assert data["edges"] == [
        {"source": "wiki/tie-a.md", "target": "wiki/target.md"},
        {"source": "wiki/new.md", "target": "wiki/target.md"},
    ]
    assert data["filteredPaths"] == ["wiki/new.md", "wiki/tie-a.md", "wiki/tie-b.md", "wiki/quarter.md", "wiki/old.md"]
    assert data["updateBasisPaths"] == ["wiki/page-update.md"]
    assert data["graphNodes"][0]["id"] == "wiki/target.md"
    assert data["graphNodes"][0]["label"] == "Readable target"
    assert data["graphLinks"] == [{"source": "wiki/new.md", "target": "wiki/target.md"}]


def test_page_index_explains_internal_links_and_zero_link_pages():
    html = (Path(__file__).parents[1] / "showcase" / "index.html").read_text(encoding="utf-8")
    assert "internal wiki links" in html.lower()
    assert "0-link pages remain useful" in html.lower()


def test_local_markdown_links_and_obsidian_aliases_build_wiki_graph_edges(tmp_path):
    wiki, sources = tmp_path / "wiki", tmp_path / "sources"
    wiki.mkdir()
    sources.mkdir()
    (wiki / "index.md").write_text(
        "# Wiki index\n\n"
        "[[article-article-a|Readable alias]]\n"
        "[Generated index link](article-article-a.md)\n"
        "[Wiki path link](wiki/article-article-a.md)\n"
        "[External article](https://example.org/article-article-a.md)\n"
        "[Source report](sources/climate-monitor-2026-09-07.md)\n"
        "[Outside page](../article-article-a.md)\n",
        encoding="utf-8",
    )
    (wiki / "article-article-a.md").write_text("# Climate and Diabetes\n\nArticle text.\n", encoding="utf-8")
    kb = WikiKnowledgeBase(wiki, sources)

    index = next(row for row in kb.document_catalog() if row["path"] == "wiki/index.md")
    assert index["links"] == ["article-article-a"]
    assert kb.graph_catalog()["notes"]["links"] == [
        {"source": "wiki/index.md", "target": "wiki/article-article-a.md"}
    ]
