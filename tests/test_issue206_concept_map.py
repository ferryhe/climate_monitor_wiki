import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_concept_map_filters_both_graph_paths_without_changing_note_contracts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to exercise the Concept Map JavaScript functions")

    root = Path(__file__).parents[1]
    app_path = root / "showcase" / "app.js"
    excluded = [
        "Date Observations",
        "Article Semantic Summary",
        "Report Observation",
        "Verified Article Content",
        "Registry Article-Version Summary",
        "Acquisition Observation",
        "Pdf Report Observation",
        "Registry Source Observations",
        "Original Links",
        "Article Article 0123456789abcdef01234567",
    ]
    concepts = [
        {"label": label, "document_count": 4}
        for label in excluded
    ] + [
        {"label": "Climate Risk", "document_count": 3},
        {"label": "Parametric Insurance", "document_count": 2},
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
function declarationSource(name) {
  const start = source.indexOf(`const ${name} =`);
  const end = source.indexOf("]);", start);
  if (start < 0 || end < 0) throw new Error(`Missing ${name}`);
  return source.slice(start, end + 3);
}
const fixture = JSON.parse(fs.readFileSync(0, "utf8"));
const context = {
  GRAPH_COPY: {
    notes: { title: "Vault Links", hint: "", legendHtml: "" },
    keywords: { title: "Concept Map", hint: "Edges explain concepts.", legendHtml: "Concept" },
  },
  state: { rows: fixture.rows, concepts: fixture.concepts },
};
vm.createContext(context);
vm.runInContext(declarationSource("HIDDEN_GRAPH_CONCEPTS"), context);
for (const name of ["keywordNodeId", "isDisplayableGraphConcept", "buildKeywordGraph", "normalizeGraphData"]) {
  vm.runInContext(functionSource(name), context);
}
const sourceGraph = {
  nodes: [
    { id: "wiki/a.md", refPath: "wiki/a.md", label: "article-article-a", kind: "note", type: "topic" },
    { id: "wiki/orphan.md", refPath: "wiki/orphan.md", label: "article-article-orphan", kind: "note", type: "topic" },
    ...fixture.concepts.map((concept) => ({
      id: context.keywordNodeId(concept.label), label: concept.label, kind: "keyword", type: "keyword", weight: concept.document_count,
    })),
  ],
  links: [
    ...fixture.concepts.map((concept) => ({ source: "wiki/a.md", target: context.keywordNodeId(concept.label) })),
    { source: "wiki/orphan.md", target: context.keywordNodeId("Date Observations") },
  ],
  static_layout: true,
};
const sourceBefore = JSON.stringify(sourceGraph);
const precomputed = context.normalizeGraphData("keywords", sourceGraph);
const notesGraph = context.normalizeGraphData("notes", {
  nodes: [sourceGraph.nodes[0], sourceGraph.nodes[1]],
  links: [{ source: "wiki/a.md", target: "wiki/orphan.md" }],
});
context.state.rows = fixture.rows;
const fallback = context.buildKeywordGraph(fixture.rows);
process.stdout.write(JSON.stringify({
  sourceUnchanged: JSON.stringify(sourceGraph) === sourceBefore,
  casingVariantsHidden: ["PDF Report Observation", "pdf report observation", "Original Links"].every(
    (label) => !context.isDisplayableGraphConcept(label),
  ),
  notesGraphNodeIds: notesGraph.nodes.map((node) => node.id),
  notesGraphLinks: notesGraph.links,
  precomputed,
  fallback,
}));
"""
    rows = [
        {
            "path": "wiki/a.md",
            "displayTitle": "Readable Article Title",
            "type": "topic",
            "concepts": [{"label": label} for label in [*excluded, "Climate Risk", "Parametric Insurance"]],
        },
        {
            "path": "wiki/b.md",
            "displayTitle": "Second Article Title",
            "type": "topic",
            "concepts": [{"label": label} for label in ["Climate Risk", "Parametric Insurance"]],
        },
        {
            "path": "wiki/orphan.md",
            "displayTitle": "Unconnected Template Page",
            "type": "topic",
            "concepts": [{"label": label} for label in excluded if label != "Article Article 0123456789abcdef01234567"],
        },
    ]
    result = subprocess.run(
        [node, "-e", script, str(app_path)],
        input=json.dumps({"excluded": excluded, "concepts": concepts, "rows": rows}),
        text=True,
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)

    assert data["sourceUnchanged"] is True
    assert data["casingVariantsHidden"] is True
    assert data["notesGraphNodeIds"] == ["wiki/a.md", "wiki/orphan.md"]
    assert data["notesGraphLinks"] == [{"source": "wiki/a.md", "target": "wiki/orphan.md"}]
    precomputed = data["precomputed"]
    assert precomputed["title"] == "Concept Map"
    assert precomputed["staticLayout"] is True
    assert [node["label"] for node in precomputed["nodes"]] == [
        "Readable Article Title",
        "Climate Risk",
        "Parametric Insurance",
    ]
    assert precomputed["nodes"][0]["id"] == "wiki/a.md"
    assert precomputed["nodes"][0]["refPath"] == "wiki/a.md"
    assert all(node["id"] != "wiki/orphan.md" for node in precomputed["nodes"])
    assert precomputed["links"] == [
        {"source": "wiki/a.md", "target": "keyword:climate-risk"},
        {"source": "wiki/a.md", "target": "keyword:parametric-insurance"},
    ]

    fallback = data["fallback"]
    assert fallback["title"] == "Concept Map"
    assert [node["label"] for node in fallback["nodes"]] == [
        "Readable Article Title",
        "Second Article Title",
        "Climate Risk",
        "Parametric Insurance",
    ]
    assert [node["id"] for node in fallback["nodes"][:2]] == ["wiki/a.md", "wiki/b.md"]
    assert [node["refPath"] for node in fallback["nodes"][:2]] == ["wiki/a.md", "wiki/b.md"]
    assert fallback["links"] == [
        {"source": "wiki/a.md", "target": "keyword:climate-risk"},
        {"source": "wiki/a.md", "target": "keyword:parametric-insurance"},
        {"source": "wiki/b.md", "target": "keyword:climate-risk"},
        {"source": "wiki/b.md", "target": "keyword:parametric-insurance"},
    ]


def test_concept_map_copy_and_note_label_accessibility_contract():
    root = Path(__file__).parents[1]
    app = (root / "showcase" / "app.js").read_text(encoding="utf-8")
    html = (root / "showcase" / "index.html").read_text(encoding="utf-8")
    css = (root / "showcase" / "styles.css").read_text(encoding="utf-8")

    assert 'title: "Concept Map"' in app
    assert "Edges connect a note to each detected concept it contains." in app
    assert "Concept size shows the number of linked notes." in app
    assert 'id="graphTitle">Concept Map</h3>' in html
    assert 'data-graph-mode="keywords"' in html
    assert ".graph-node--keywords.graph-node--note text {" in css
    assert ".graph-node--keywords.graph-node--note:hover text," in css
    assert ".graph-node--keywords.graph-node--note:focus-visible text," in css
    assert ".graph-node--keywords.graph-node--note.is-active text" in css
    assert 'node.kind === "keyword" ? `Filter Page Index by keyword ${node.label}` : `Open note ${node.label}`' in app
    assert 'group.addEventListener("click", activateNode);' in app
    assert 'if (event.key === "Enter" || event.key === " ")' in app
    assert "setActiveContext(node.refPath || node.id);" in app
