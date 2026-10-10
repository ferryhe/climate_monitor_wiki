import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_article_keyword_graph_counts_canonical_articles_and_distinct_pairs():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    start = source.index("function buildArticleKeywordGraph(articles) {")
    end = source.index("\nasync function loadArticleKeywordGraph()", start)
    helper = source[start:end]
    articles = [
        {"article_id": "a", "canonical_url": "https://example.org/a", "keywords": ["Flood", "flood", "Insurance"]},
        {"article_id": "a", "canonical_url": "https://example.org/a", "keywords": ["Other"]},
        {"article_id": "b", "canonical_url": "https://example.org/b", "keywords": ["flood", "Capital"]},
        {"article_id": "c", "canonical_url": "https://example.org/c", "keywords": []},
        {"article_id": "pdf-d", "canonical_url": "https://example.org/d", "source_kind": "pdf",
         "publisher": "Publisher D", "metadata_provenance": {"keywords": "manual_enrichment"},
         "keywords": ["Flood", "Insurance"]},
        {"article_id": "a", "canonical_url": "https://example.org/a", "source_kind": "pdf",
         "keywords": ["Flood"]},
    ]
    script = "const graph=(new Function('GRAPH_COPY','articles', " + json.dumps(
        helper + "; return buildArticleKeywordGraph(articles);"
    ) + "))({articleKeywords:{title:'Article Keywords',hint:'hint',legendHtml:''}}, " + json.dumps(articles) + "); "
    script += "process.stdout.write(JSON.stringify(graph));"
    result = subprocess.run(
        [node, "-e", script], text=True,
        capture_output=True, check=True,
    )
    graph = json.loads(result.stdout)
    nodes = {node["label"].casefold(): node for node in graph["nodes"]}
    pairs = {
        tuple(sorted((link["source"], link["target"]))) : link
        for link in graph["links"]
    }
    flood_id = nodes["flood"]["id"]
    insurance_id = nodes["insurance"]["id"]
    capital_id = nodes["capital"]["id"]
    assert nodes["flood"]["weight"] == 4
    assert len(nodes["flood"]["articles"]) == 4
    assert pairs[tuple(sorted((flood_id, insurance_id)))]["weight"] == 2
    assert pairs[tuple(sorted((flood_id, capital_id)))]["weight"] == 1
    assert graph["mode"] == "article-keywords"
    pdf_article = next(article for article in nodes["flood"]["articles"] if article.get("source_kind") == "pdf")
    assert pdf_article["publisher"] == "Publisher D"
    assert pdf_article["metadata_provenance"]["keywords"] == "manual_enrichment"


def test_article_keyword_graph_controls_expose_keyboard_supporting_articles():
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    page = (ROOT / "showcase/index.html").read_text(encoding="utf-8")
    assert 'data-graph-mode="article-keywords"' in page
    assert 'id="articleKeywordEvidence"' in page
    assert 'line.setAttribute("role", "button")' in source
    assert 'line.setAttribute("tabindex", "0")' in source
    assert 'event.key === "Enter" || event.key === " "' in source
    assert '"/api/registry/pdf-intake/articles"' in source
    assert 'source_kind: item.source_kind || "registry"' in source


def test_article_keyword_graph_uses_only_the_effective_current_keywords():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    start = source.index("function buildArticleKeywordGraph(articles) {")
    end = source.index("\nasync function loadArticleKeywordGraph()", start)
    helper = source[start:end]
    script = "const build=(articles)=>(new Function('GRAPH_COPY','articles', " + json.dumps(
        helper + "; return buildArticleKeywordGraph(articles);"
    ) + "))({articleKeywords:{title:'Article Keywords',hint:'hint',legendHtml:''}}, articles);"
    script += "const empty=build([{article_id:'a',source_kind:'registry',content:{content_version_id:'v2'},keywords:[],available_content:{content_version_id:'v3',keywords:['Pending']}}]);"
    script += "const active=build([{article_id:'a',source_kind:'registry',content:{content_version_id:'v2'},keywords:['New'],metadata_provenance:{keywords:'manual_enrichment'}}]);"
    script += "process.stdout.write(JSON.stringify({empty,active}));"
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, check=True)
    payload = json.loads(result.stdout)
    assert payload["empty"]["nodes"] == []
    assert payload["empty"]["emptyMessage"] == "No effective article keywords are available."
    assert [(node["label"], node["weight"]) for node in payload["active"]["nodes"]] == [("New", 1)]
    assert payload["active"]["nodes"][0]["articles"][0]["metadata_provenance"]["keywords"] == "manual_enrichment"


def test_article_keyword_pointer_targets_fit_a_distinct_grid_cell():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    grid_start = source.index("function projectGridPosition(")
    grid_end = source.index("\nfunction projectRadialPosition(", grid_start)
    radius_start = source.index("function getNodeRadius(")
    radius_end = source.index("\nfunction projectGridPosition(", radius_start)
    script = source[grid_start:grid_end] + source[radius_start:radius_end]
    script += "const points=Array.from({length:1493},(_,i)=>projectGridPosition(i,1493,20,1180,20,680));"
    script += "const radius=getNodeRadius({mode:'article-keywords',type:'keyword',weight:135});"
    script += "const nearest=Math.min(...points.map((point,index)=>Math.min(...points.slice(index+1).map(other=>Math.hypot(point.x-other.x,point.y-other.y)).filter(Boolean))));"
    script += "process.stdout.write(JSON.stringify({nearest,radius}));"
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, check=True)
    layout = json.loads(result.stdout)
    assert layout["nearest"] > 2 * layout["radius"]

    app = source[source.index("function renderGraph(graphData) {"):source.index("function showArticleKeywordEvidence(")]
    assert 'projectGridPosition(centralRank.get(node.id) || 0, graphData.nodes.length, 20, width - 20, 20, height - 20)' in app
    assert 'nodeGroup.appendChild(group);\n    if (graphData.mode === "article-keywords")' in app
    assert 'nodeGroup.appendChild(label);' in app


def test_article_keyword_evidence_clears_on_mode_change_and_empty_reload():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    graph_start = source.index("function buildArticleKeywordGraph(articles) {")
    graph_end = source.index("\nfunction getNodeRadius(", graph_start)
    evidence_start = source.index("function showArticleKeywordEvidence(")
    evidence_end = source.index("\nasync function fetchMarkdown(", evidence_start)
    functions = source[graph_start:graph_end] + "\n" + source[evidence_start:evidence_end]
    script = r'''
class MiniElement {
  constructor(tag='div', text='') { this.tag=tag; this.textContent=text; this.children=[]; this.hidden=false; this.dataset={}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=[...children]; }
  focus() {}
}
const state={articleKeywordGraphLoading:false,graphData:{articleKeywords:null},graphMode:'article-keywords'};
const GRAPH_COPY={articleKeywords:{title:'Article Keywords',hint:'hint',legendHtml:''}};
const els={articleKeywordEvidence:new MiniElement(),graphModeButtons:[]};
function renderCurrentGraph() {}
function registryElement(tag,className='',text='') { const element=new MiniElement(tag,text); element.className=className; return element; }
function registryErrorMessage(error) { return String(error); }
async function registryFetch(path) {
  if (path.startsWith('/api/registry/articles?')) return {items:[{article_id:'a',source_kind:'registry'}],pagination:{total:1}};
  return {article_id:'a',title:'Current article',publisher:'Publisher',content:{content_version_id:'v2'},keywords:[]};
}
''' + functions + r'''
(async()=>{
  showArticleKeywordEvidence('Flood',[{article_id:'a',title:'Old article',publisher:'Publisher',content:{content_version_id:'v1'}}]);
  setGraphMode('keywords');
  const afterModeChange={hidden:els.articleKeywordEvidence.hidden,childCount:els.articleKeywordEvidence.children.length};
  state.graphMode='article-keywords';
  showArticleKeywordEvidence('Flood',[{article_id:'a',title:'Old article',publisher:'Publisher',content:{content_version_id:'v1'}}]);
  await loadArticleKeywordGraph();
  const graph=state.graphData.articleKeywords;
  const afterEmptyReload={hidden:els.articleKeywordEvidence.hidden,childCount:els.articleKeywordEvidence.children.length,nodeCount:graph.nodes.length,emptyMessage:graph.emptyMessage};
  process.stdout.write(JSON.stringify({afterModeChange,afterEmptyReload}));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, check=True)
    outcome = json.loads(result.stdout)
    assert outcome["afterModeChange"] == {"hidden": True, "childCount": 0}
    assert outcome["afterEmptyReload"] == {
        "hidden": True,
        "childCount": 0,
        "nodeCount": 0,
        "emptyMessage": "No effective article keywords are available.",
    }


def test_stale_cached_keyword_activation_is_blocked_during_empty_reload():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    graph_start = source.index("function buildArticleKeywordGraph(articles) {")
    graph_end = source.index("\nfunction getNodeRadius(", graph_start)
    evidence_start = source.index("function activateArticleKeywordEvidence(")
    evidence_end = source.index("\nasync function fetchMarkdown(", evidence_start)
    functions = source[graph_start:graph_end] + "\n" + source[evidence_start:evidence_end]
    script = r'''
class MiniElement {
  constructor(tag='div', text='') { this.tag=tag; this.textContent=text; this.children=[]; this.hidden=false; this.dataset={}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=[...children]; }
  focus() {}
}
const state={articleKeywordGraphLoading:false,graphData:{articleKeywords:null},graphMode:'article-keywords'};
const GRAPH_COPY={articleKeywords:{title:'Article Keywords',hint:'hint',legendHtml:''}};
const els={articleKeywordEvidence:new MiniElement(),graphModeButtons:[]};
function renderCurrentGraph() {}
function registryElement(tag,className='',text='') { const element=new MiniElement(tag,text); element.className=className; return element; }
function registryErrorMessage(error) { return String(error); }
let resolveListPage;
async function registryFetch(path) {
  if (path.startsWith('/api/registry/articles?')) return new Promise(resolve=>{resolveListPage=resolve;});
  return {article_id:'a',title:'Current article',publisher:'Publisher',content:{content_version_id:'v2'},keywords:[]};
}
function textContent(element) { return element.textContent + element.children.map(textContent).join(''); }
''' + functions + r'''
(async()=>{
  const oldGraph=buildArticleKeywordGraph([{article_id:'a',source_kind:'registry',title:'Old article',publisher:'Publisher',content:{content_version_id:'v1'},metadata_provenance:{keywords:'manual_enrichment'},keywords:['Flood','Insurance']}]);
  const flood=oldGraph.nodes.find(item=>item.label==='Flood');
  const cachedFloodClick=()=>activateArticleKeywordEvidence(flood.label,flood.articles);
  cachedFloodClick();
  const beforeReload=textContent(els.articleKeywordEvidence);
  const pendingLoad=loadArticleKeywordGraph();
  cachedFloodClick();
  const duringReload={hidden:els.articleKeywordEvidence.hidden,childCount:els.articleKeywordEvidence.children.length,text:textContent(els.articleKeywordEvidence)};
  resolveListPage({items:[{article_id:'a',source_kind:'registry'}],pagination:{total:1}});
  await pendingLoad;
  const graph=state.graphData.articleKeywords;
  const afterReload={hidden:els.articleKeywordEvidence.hidden,childCount:els.articleKeywordEvidence.children.length,text:textContent(els.articleKeywordEvidence),nodeCount:graph.nodes.length,emptyMessage:graph.emptyMessage,loading:state.articleKeywordGraphLoading};
  process.stdout.write(JSON.stringify({beforeReload,duringReload,afterReload}));
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, check=True)
    outcome = json.loads(result.stdout)
    assert "Supporting articles: Flood" in outcome["beforeReload"]
    assert "content version v1" in outcome["beforeReload"]
    assert outcome["duringReload"] == {"hidden": True, "childCount": 0, "text": ""}
    assert outcome["afterReload"] == {
        "hidden": True,
        "childCount": 0,
        "text": "",
        "nodeCount": 0,
        "emptyMessage": "No effective article keywords are available.",
        "loading": False,
    }
