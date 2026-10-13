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
    layout_end = source.index("\nfunction setConnectionStatus(", end)
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
    script = "const taxonomy=" + json.dumps(taxonomy) + ";" + setup + source[start:layout_end] + source[evidence_start:evidence_end] + body
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
const layout=layoutArticleCategoryNodes(graph.nodes,340);
graph.nodes=graph.nodes.map((node,index)=>({...node,...layout.nodes[index]}));
process.stdout.write(JSON.stringify(graph));
''')
    nodes = {item["label"]: item for item in result["nodes"]}
    assert len(nodes) == 14
    assert nodes["Physical Risk"]["weight"] == 3
    assert nodes["Climate & Health"]["weight"] == 0
    assert nodes["Physical Risk"]["radius"] > nodes["Insurance Risk"]["radius"] > nodes["Climate & Health"]["radius"]
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
    assert '.graph-node--article-categories rect' not in css
    assert 'const card = document.createElementNS' not in app
    assert 'circle.setAttribute("r", String(getNodeRadius(node)))' in app
    assert "opacity: 0" not in css[css.index(".graph-node--article-categories circle"):css.index(".article-category-evidence {")]


def test_category_circle_sizes_follow_support_count_instead_of_degree():
    result = run_js(r'''
const weights=[0,1,2,10,129];
const nodes=weights.map((weight,index)=>({id:String(index),label:"Category "+index,mode:"article-categories",weight,degree:999-weight,importance:999-weight}));
const layout=layoutArticleCategoryNodes(nodes,340);
const changed=layoutArticleCategoryNodes(nodes.map(node=>({...node,degree:0,importance:1})),340);
process.stdout.write(JSON.stringify({radii:layout.nodes.map((position,index)=>getNodeRadius({...nodes[index],...position})),
 unchanged:layout.nodes.every((position,index)=>position.radius===changed.nodes[index].radius)}));
''')
    assert all(left < right for left, right in zip(result["radii"], result["radii"][1:]))
    assert result["unchanged"]


def test_category_circles_labels_and_all_pair_targets_fit_responsive_network():
    result = run_js(r'''
const results=[];
for(const width of [280,304,340,390,640,900,1000,1200]) {
 for(const weights of [Array(14).fill(0),Array(14).fill(331),[124,39,129,13,105,29,109,98,64,98,25,30,50,33]]) {
  const input=taxonomy.categories.map((category,index)=>({...category,weight:weights[index]}));
  const layout=layoutArticleCategoryNodes(input,width);
  const nodes=input.map((node,index)=>({...node,...layout.nodes[index]}));
  const links=[];nodes.forEach((source,index)=>nodes.slice(index+1).forEach(target=>links.push({source,target})));
  const routes=categoryEdgeRoutes(links,nodes,width,layout.height);
  const overlaps=(a,b)=>a.left<b.right && a.right>b.left && a.top<b.bottom && a.bottom>b.top;
  results.push({width,count:routes.length,
   clipped:nodes.some(({bounds:b})=>b.left<12 || b.right>width-12 || b.top<12 || b.bottom>layout.height-12),
   nodeOverlap:nodes.some((node,index)=>nodes.slice(index+1).some(other=>overlaps(node.bounds,other.bounds))),
   blocked:routes.some(({hit,radius})=>nodes.some(({bounds:b})=>hit.x+radius>b.left && hit.x-radius<b.right && hit.y+radius>b.top && hit.y-radius<b.bottom)),
   targetOverlap:routes.some((route,index)=>routes.slice(index+1).some(other=>Math.hypot(route.hit.x-other.hit.x,route.hit.y-other.hit.y)<=route.radius+other.radius)),
   disconnected:routes.some(route=>{const p=route.path.match(/-?\d+(?:\.\d+)?/g).map(Number);return Math.hypot((p[0]+2*p[2]+p[4])/4-route.hit.x,(p[1]+2*p[3]+p[5])/4-route.hit.y)>1e-6;})});
 }
}
process.stdout.write(JSON.stringify(results));
''')
    assert len(result) == 24
    for item in result:
        assert item == {"width": item["width"], "count": 91, "clipped": False, "nodeOverlap": False,
            "blocked": False, "targetOverlap": False, "disconnected": False}
