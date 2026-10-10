import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentic_wiki.wiki_agent import WikiKnowledgeBase


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
        "climate-monitor-2026-09-07.md": "# Weekly report title\n\nReport contents.\n",
        "index.md": "# Wiki index\n\n[[linked-page]]\n",
        "linked-page.md": "# Linked page\n\nUseful page.\n",
        "zero-links.md": "# Unlinked useful page\n\nStill useful to Chat.\n",
    }
    for name, content in pages.items():
        (wiki / name).write_text(content, encoding="utf-8")

    catalog = WikiKnowledgeBase(wiki, sources).document_catalog()
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
    assert "wiki/zero-links.md" in by_path
    assert len(catalog) == len(pages)


def test_page_index_sorts_and_searches_by_display_metadata_without_changing_paths():
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
for (const name of ["displayDateSortKey", "buildWorkspaceData", "applyTableFilter", "normalizeGraphData", "displayDateLabel"]) {
  vm.runInContext(functionSource(name), context);
}
const docs = JSON.parse(fs.readFileSync(0, "utf8"));
const data = context.buildWorkspaceData(docs);
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
        input=json.dumps(documents),
        text=True,
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)

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
