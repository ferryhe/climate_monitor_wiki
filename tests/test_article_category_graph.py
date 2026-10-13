import json
import shutil
import subprocess
from pathlib import Path

import pytest

from climate_monitor.taxonomy import load_article_taxonomy

ROOT = Path(__file__).resolve().parents[1]


def run_js(body):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    start = source.index("function buildArticleCategoryGraph(articles, taxonomy) {")
    end = source.index("\nfunction getNodeRadius(", start)
    evidence_start = source.index("function showArticleCategoryEvidence(")
    evidence_end = source.index("\nasync function fetchMarkdown(", evidence_start)
    taxonomy = {"categories": [{"id": item.id, "label": item.label} for item in load_article_taxonomy().categories]}
    setup = r'''
class Element {
  constructor(tag="div", text="") { this.tag=tag; this.textContent=text; this.children=[]; this.hidden=false; this.dataset={}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=[...children]; }
  focus() {}
}
const state={articleCategoryGraphLoading:false,graphData:{articleCategories:null},graphMode:"article-categories"};
const GRAPH_COPY={articleCategories:{title:"Category Map",hint:"hint",legendHtml:""}};
const els={articleCategoryEvidence:new Element(),graphModeButtons:[]};
function renderCurrentGraph() {}
function registryElement(tag,className="",text="") { return new Element(tag,text); }
function registryErrorMessage(error) { return String(error); }
'''
    script = "const taxonomy=" + json.dumps(taxonomy) + ";" + setup + source[start:end] + source[evidence_start:evidence_end] + body
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_category_map_counts_distinct_canonical_articles_and_pairs():
    result = run_js(r'''
const graph=buildArticleCategoryGraph([
 {article_id:"a",canonical_url:"https://example.org/a",categories:["Physical Risk","Physical Risk","Insurance Risk"]},
 {article_id:"pdf-a",source_kind:"pdf",canonical_url:"https://example.org/a",categories:["Physical Risk"]},
 {article_id:"b",canonical_url:"https://example.org/b",categories:["Physical Risk","Capital & Solvency"]},
 {article_id:"pdf-c",source_kind:"pdf",canonical_url:"https://example.org/c",categories:["Physical Risk","Insurance Risk"],
  metadata_provenance:{categories:"reviewed_category_correction"},published_candidate_sha256:"approved-pdf"},
 {article_id:"d",categories:["Unknown"],keywords:["Climate & Health"]}
],taxonomy);
process.stdout.write(JSON.stringify(graph));
''')
    nodes = {item["label"]: item for item in result["nodes"]}
    assert len(nodes) == 14
    assert nodes["Physical Risk"]["weight"] == 3
    assert nodes["Climate & Health"]["weight"] == 0
    assert sorted(item["weight"] for item in result["links"]) == [1, 2]
    pdf = next(item for item in nodes["Insurance Risk"]["articles"] if item.get("source_kind") == "pdf")
    assert pdf["published_candidate_sha256"] == "approved-pdf"


def test_category_map_fetches_once_and_keeps_compact_approved_evidence():
    result = run_js(r'''
let calls=0;
const article={article_id:"a",title:"Approved",publisher:"Publisher",source_kind:"registry",categories:["Physical Risk"],
 content_version_id:"content-1",published_candidate_sha256:"approved-1",metadata_provenance:{categories:"reviewed_category_correction"}};
async function registryFetch(path) { calls++; if(path!=="/api/registry/article-categories") throw Error(path); return {items:[article],taxonomy}; }
(async()=>{ await loadArticleCategoryGraph(); await loadArticleCategoryGraph();
const node=state.graphData.articleCategories.nodes.find(item=>item.label==="Physical Risk");
showArticleCategoryEvidence(node.label,node.articles);
const entry=els.articleCategoryEvidence.children[1].children[0];
process.stdout.write(JSON.stringify({calls,footer:entry.children[1].textContent,source:entry.children[0].dataset.articleSource})); })();
''')
    assert result == {"calls": 1, "footer": "Publisher · reviewed_category_correction · content version content-1", "source": "registry"}


def test_category_map_error_can_retry_and_clears_old_evidence():
    result = run_js(r'''
let calls=0;
async function registryFetch() { if(++calls===1) throw Error("unavailable"); return {items:[],taxonomy}; }
(async()=>{ showArticleCategoryEvidence("old",[]); await loadArticleCategoryGraph();
const error=state.graphData.articleCategories.emptyMessage;
const hidden=els.articleCategoryEvidence.hidden;
await loadArticleCategoryGraph();setGraphMode("keywords");
process.stdout.write(JSON.stringify({error,hidden,calls,nodes:state.graphData.articleCategories.nodes.length,mode:state.graphMode})); })();
''')
    assert result == {"error": "Article categories could not be loaded.", "hidden": True, "calls": 2, "nodes": 14, "mode": "keywords"}


def test_category_map_visible_labels_and_accessible_edges_replace_old_path():
    app = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    page = (ROOT / "showcase/index.html").read_text(encoding="utf-8")
    css = (ROOT / "showcase/styles.css").read_text(encoding="utf-8")
    assert 'data-graph-mode="article-categories"' in page
    assert "Category Map" in page and "Article Keywords" not in page
    assert "buildArticleKeywordGraph" not in app
    assert 'target.setAttribute("tabindex", "0")' in app
    assert 'target.addEventListener("focus", reveal)' in app
    assert 'label.style.display = "none"' in app
    assert 'Math.max(280, svg.getBoundingClientRect().width' in app
    assert 'node.label.split(" ")' in app
    assert 'function buildKeywordGraph(' in app
    assert "opacity: 0" not in css[css.index(".graph-node--article-categories rect"):css.index(".article-category-evidence {")]


def test_all_category_pair_routes_and_mouse_targets_stay_outside_cards():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    app = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    helper = app[app.index("function categoryEdgeRoutes("):app.index("\nfunction setConnectionStatus(")]
    script = helper + r'''
const results=[280,340,1000,1200].map(width=>{
 const columns=width<520?2:width<850?3:4, cell=width/columns;
 const nodes=Array.from({length:14},(_,index)=>({x:(index%columns+0.5)*cell,y:(Math.floor(index/columns)+0.5)*112+16}));
 const links=[];nodes.forEach((source,index)=>nodes.slice(index+1).forEach(target=>links.push({source,target})));
 const routes=categoryEdgeRoutes(links,width,columns);
 const inside=(point,card)=>point[0]>card.x-cell/2+8 && point[0]<card.x+cell/2-8 && point[1]>card.y-44 && point[1]<card.y+44;
 const intersects=(left,right,card)=>left[0]===right[0]
   ? left[0]>card.x-cell/2+8 && left[0]<card.x+cell/2-8 && Math.max(left[1],right[1])>card.y-44 && Math.min(left[1],right[1])<card.y+44
   : left[1]>card.y-44 && left[1]<card.y+44 && Math.max(left[0],right[0])>card.x-cell/2+8 && Math.min(left[0],right[0])<card.x+cell/2-8;
 return {width,count:routes.length,blocked:routes.some(route=>nodes.some(card=>inside([route.hit.x,route.hit.y],card)
   || route.points.slice(1).some((point,index)=>intersects(route.points[index],point,card)))),
   overlapping:routes.some((route,index)=>routes.slice(index+1).some(other=>Math.hypot(route.hit.x-other.hit.x,route.hit.y-other.hit.y)<=route.radius+other.radius))};
});process.stdout.write(JSON.stringify(results));
'''
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == [{"width": width, "count": 91, "blocked": False, "overlapping": False}
        for width in (280, 340, 1000, 1200)]
