import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../showcase/index.html', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../showcase/styles.css', import.meta.url), 'utf8');
function functionSource(name) {
  let start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing ${name}`);
  if (source.slice(start - 6, start) === 'async ') start -= 6;
  const open = source.indexOf('{', start);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`unclosed ${name}`);
}

function classList() {
  const values = new Set();
  return {
    toggle(name, force) { force ? values.add(name) : values.delete(name); },
    add(name) { values.add(name); },
    remove(name) { values.delete(name); },
    values,
  };
}
function graphNode(id) {
  return {
    id,
    kind: 'note',
    group: { classList: classList(), attributes: {}, setAttribute(key, value) { this.attributes[key] = value; }, querySelector: () => ({ setAttribute() {} }) },
  };
}
const left = graphNode('wiki/left.md');
const right = graphNode('wiki/right.md');
const edge = { source: left, target: right };
const edgeElement = { classList: classList(), attributes: {}, setAttribute(key, value) { this.attributes[key] = value; } };
const state = {
  activeContextPath: 'wiki/chat-context.md',
  graphSelection: null,
  graph: {
    nodes: [left, right],
    nodeEls: [left.group, right.group],
    links: [edge],
    linkEls: [edgeElement],
    edgeTargetEls: [],
    edgeLabelEls: [],
  },
};
let wikiRequests = 0;
const context = vm.createContext({
  state,
  fetch: (url) => { if (String(url).startsWith('/wiki/')) wikiRequests += 1; },
  getNodeRadius: () => 5,
  renderExpandedGraphSelection() {},
});
vm.runInContext([
  functionSource('updateGraphSelection'),
  functionSource('selectGraphNode'),
  functionSource('selectGraphEdge'),
].join('\n'), context);

context.selectGraphNode(left);
assert.deepEqual(JSON.parse(JSON.stringify(state.graphSelection)), { nodeId: 'wiki/left.md' });
assert.equal(left.group.classList.values.has('is-active'), true);
assert.equal(right.group.classList.values.has('is-neighbor'), true);
assert.equal(edgeElement.classList.values.has('is-highlighted'), true);
assert.equal(state.activeContextPath, 'wiki/chat-context.md', 'graph selection must leave Chat context alone');
assert.equal(wikiRequests, 0, 'graph selection must not fetch Wiki Markdown');

context.selectGraphEdge(edge);
assert.deepEqual(JSON.parse(JSON.stringify(state.graphSelection)), { edgeKey: 'wiki/left.md\0wiki/right.md' });
assert.equal(edgeElement.classList.values.has('is-highlighted'), true);
assert.equal(state.activeContextPath, 'wiki/chat-context.md');
assert.equal(wikiRequests, 0);

for (const id of ['rows', 'wikiSearch', 'detailMarkdown', 'detailFile', 'useInChatButton', 'clearSelectionButton', 'articleCategoryEvidence']) {
  assert.doesNotMatch(html, new RegExp(`id="${id}"`), `${id} is no longer part of Graph`);
}
assert.match(source, /const activateNode = \(\) => selectGraphNode\(node\);/);
assert.match(source, /const selectEdge = \(\) => selectGraphEdge\(edge\);/);
assert.match(source, /event\.key === "Enter" \|\| event\.key === " "/);
assert.doesNotMatch(source, /fetch\(`\/\$\{path\}`\)/, 'Graph selection no longer has a Markdown fetch path');

const catalogState = { documents: [], rows: [], edges: [], graphData: {}, promptStarters: [], messages: [], activeContextPath: null };
const requests = [];
const catalogContext = vm.createContext({
  state: catalogState,
  els: {},
  fetch: async (url) => {
    requests.push(String(url));
    return { ok: true, json: async () => ({ documents: [{ path: 'wiki/a.md' }], concepts: [], graphs: {}, prompt_starters: [] }) };
  },
  buildWorkspaceData: (documents) => ({ rows: documents.map((doc) => ({ ...doc })), edges: [] }),
  renderChatContext() {},
  setConnectionStatus() {},
  setAnswerMode() {},
  setGraphMode() {},
  renderMessages() {},
  DEFAULT_PROMPT_STARTERS: [],
});
vm.runInContext(functionSource('loadConfig'), catalogContext);
await catalogContext.loadConfig();
assert.deepEqual(requests, ['/api/config'], 'initializing without Page Index and detail elements makes no Wiki request');
assert.equal(catalogState.rows.length, 1);

assert.match(html, /<pre id="registryMarkdown" class="md-preview"><\/pre>/);
const markdownStyle = styles.match(/\.md-preview\s*\{([^}]*)\}/)?.[1] || '';
assert.match(markdownStyle, /white-space:\s*pre-wrap/, 'Reports Markdown continues to wrap long lines');
assert.match(markdownStyle, /word-break:\s*break-word/);
assert.match(markdownStyle, /overflow:\s*auto/);

class SvgElement {
  constructor(name) {
    this.localName = name;
    this.attributes = {};
    this.children = [];
    this.listeners = {};
    this.style = {};
    this.classList = classList();
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  dispatch(type, event = {}) { for (const callback of this.listeners[type] || []) callback(event); }
  querySelector(name) { return this.children.find((child) => child.localName === name) || null; }
  cloneNode() { const clone = new SvgElement(this.localName); clone.textContent = this.textContent; return clone; }
  getBoundingClientRect() { return { width: 420, height: 360, left: 0, top: 0 }; }
  set innerHTML(value) { if (value === '') this.children = []; }
}
const svg = new SvgElement('svg');
const rendererState = { graphMode: 'article-categories', graphExpanded: false, graphFrame: 0, activeView: 'obsidianView', graphSelection: null };
const rendererContext = vm.createContext({
  state: rendererState,
  els: { graphSvg: svg },
  document: { hidden: false, createElementNS: (_ns, name) => new SvgElement(name) },
  GRAPH_COLORS: { topic: '#7eb696', keyword: '#83a6c8' },
  stopGraphAnimation() {},
  getNodeRadius: (node) => node.radius || 8,
  graphCanvasHeight: (_height, categoryHeight) => categoryHeight || 360,
  projectRadialPosition: () => ({ x: 100, y: 120 }),
  layoutArticleCategoryNodes: (nodes) => ({ height: 360, nodes: nodes.map((node, index) => ({
    x: 100 + index * 150, y: 120, radius: 24, labelLines: [node.label],
  })) }),
  graphNodePosition: (_mode, _node, _rank, _count, _width, _height, categoryPosition) => categoryPosition,
  categoryEdgeRoutes: () => [{ hit: { x: 175, y: 120 }, radius: 5, path: 'M100,120 Q175,120 250,120' }],
  renderExpandedGraphSelection() {},
});
vm.runInContext([
  functionSource('updateGraphSelection'),
  functionSource('selectGraphNode'),
  functionSource('selectGraphEdge'),
  functionSource('renderGraph'),
].join('\n'), rendererContext);
rendererContext.renderGraph({
  mode: 'article-categories', title: 'Category Map', nodes: [
    { id: 'a', label: 'Flood', kind: 'category', weight: 2, radius: 24, labelLines: ['Flood'] },
    { id: 'b', label: 'Insurance', kind: 'category', weight: 2, radius: 24, labelLines: ['Insurance'] },
  ],
  links: [{ source: 'a', target: 'b', weight: 1 }], staticLayout: true,
});
const [categoryNode] = rendererState.graph.nodeEls;
const [categoryLine] = rendererState.graph.linkEls;
const [edgeTarget] = rendererState.graph.edgeTargetEls;
categoryNode.dispatch('click');
assert.equal(categoryLine.classList.values.has('is-highlighted'), true);
categoryLine.dispatch('pointerenter');
categoryLine.dispatch('pointerleave');
assert.equal(categoryLine.classList.values.has('is-highlighted'), true, 'pointer cleanup restores the selected node relationship');
edgeTarget.dispatch('focus');
edgeTarget.dispatch('blur');
assert.equal(categoryLine.classList.values.has('is-highlighted'), true, 'focus cleanup restores the selected node relationship');
assert.equal(categoryNode.getAttribute('aria-pressed'), 'true');
