import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8');
const slice = (start, end) => {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from);
  assert.notEqual(from, -1, `missing function marker: ${start}`);
  assert.notEqual(to, -1, `missing end marker: ${end}`);
  return source.slice(from, to);
};

async function graphAtSize(size) {
  const calls = [];
  const items = Array.from({ length: size }, (_, index) => ({
    article_id: `article-${index}`,
    canonical_url: `https://example.org/${index}`,
    title: `Climate article ${index}`,
    publisher: 'Example Institute',
    source_kind: index % 3 === 0 ? 'pdf' : 'registry',
    current_version_id: `version-${index}`,
    metadata_provenance: { keywords: index % 3 === 0 ? 'manual_enrichment' : 'source_report' },
    keywords: ['Flood', 'Insurance'],
  }));
  const context = vm.createContext({
    GRAPH_COPY: { articleKeywords: { title: 'Article Keywords', hint: 'hint', legendHtml: '' } },
    state: { articleKeywordGraphLoading: false, graphData: { articleKeywords: null } },
    els: { articleKeywordEvidence: { hidden: true, replaceChildren() {} } },
    fetch: async (url) => {
      calls.push(String(url));
      const parsed = new URL(String(url), 'http://localhost');
      assert.equal(parsed.pathname, '/api/registry/articles', 'graph must use the approved collection endpoint');
      assert.equal(parsed.searchParams.get('include_pdf'), 'true');
      const page = Number(parsed.searchParams.get('page'));
      const pageSize = Number(parsed.searchParams.get('page_size'));
      return { ok: true, json: async () => ({
        items: items.slice((page - 1) * pageSize, page * pageSize),
        pagination: { total: size },
      }) };
    },
    registryErrorMessage: (error) => String(error),
    renderCurrentGraph() {},
  });
  vm.runInContext([
    slice('function buildArticleKeywordGraph(articles) {', '\nasync function loadArticleKeywordGraph()'),
    slice('async function registryFetch(path) {', '\nfunction renderRegistryNotice('),
    slice('async function loadArticleKeywordGraph() {', '\nfunction clearArticleKeywordEvidence()'),
    slice('function clearArticleKeywordEvidence() {', '\nfunction setGraphMode('),
    'globalThis.runGraph = loadArticleKeywordGraph;',
  ].join('\n'), context);
  await context.runGraph();
  const graph = JSON.parse(JSON.stringify(context.state.graphData.articleKeywords));
  const expectedPages = Math.max(1, Math.ceil(size / 100));
  assert.equal(calls.length, expectedPages, `${size} articles should need only collection pages`);
  assert.equal(context.state.articleKeywordGraphLoading, false);
  if (size === 0) {
    assert.deepEqual(graph.nodes, []);
    assert.deepEqual(graph.links, []);
    return;
  }
  const flood = graph.nodes.find((node) => node.label === 'Flood');
  const insurance = graph.nodes.find((node) => node.label === 'Insurance');
  assert.equal(flood.weight, size);
  assert.equal(insurance.weight, size);
  assert.equal(graph.links[0].weight, size);
  assert.equal(flood.articles.length, size);
  const pdfEvidence = flood.articles.find((article) => article.source_kind === 'pdf');
  if (pdfEvidence) {
    assert.equal(pdfEvidence.metadata_provenance.keywords, 'manual_enrichment');
    assert.ok(pdfEvidence.current_version_id);
  }
}

for (const size of [0, 1, 100, 229]) {
  await graphAtSize(size);
}

async function keywordEvidenceUsesApprovedContentVersion() {
  const calls = [];
  const section = {
    hidden: true,
    children: [],
    replaceChildren(...children) { this.children = children; },
    append(...children) { this.children.push(...children); },
    focus() {},
  };
  const article = {
    article_id: 'article-a', title: 'Approved article', canonical_url: 'https://example.org/a',
    publisher: 'Publisher', source_kind: 'registry', content_version_id: 'content-a',
    metadata_provenance: { keywords: 'content_enrichment' }, keywords: ['Insurance'],
  };
  const element = (tag, className, text) => ({
    tag, className, text, dataset: {}, children: [],
    append(...children) { this.children.push(...children); },
  });
  const context = vm.createContext({
    GRAPH_COPY: { articleKeywords: { title: 'Article Keywords', hint: 'hint', legendHtml: '' } },
    state: { articleKeywordGraphLoading: false, graphMode: 'article-keywords', graphData: { articleKeywords: null } },
    els: { articleKeywordEvidence: section },
    fetch: async (url) => {
      calls.push(String(url));
      assert.match(String(url), /^\/api\/registry\/articles\?include_pdf=true&page=1&page_size=100$/);
      return { ok: true, json: async () => ({ items: [article], pagination: { total: 1 } }) };
    },
    registryElement: element, registryErrorMessage: String, renderCurrentGraph() {},
  });
  vm.runInContext([
    slice('function buildArticleKeywordGraph(articles) {', '\nasync function loadArticleKeywordGraph()'),
    slice('async function registryFetch(path) {', '\nfunction renderRegistryNotice('),
    slice('async function loadArticleKeywordGraph() {', '\nfunction clearArticleKeywordEvidence()'),
    slice('function clearArticleKeywordEvidence() {', '\nfunction setGraphMode('),
    slice('function showArticleKeywordEvidence(label, articles) {', '\nasync function fetchMarkdown('),
    'globalThis.loadGraph = loadArticleKeywordGraph; globalThis.showEvidence = showArticleKeywordEvidence;',
  ].join('\n'), context);
  await context.loadGraph();
  const keyword = context.state.graphData.articleKeywords.nodes.find((node) => node.label === 'Insurance');
  assert.ok(keyword);
  context.showEvidence(keyword.label, keyword.articles);
  const footer = section.children.find((item) => item.tag === 'ul').children[0].children[1].text;
  assert.equal(footer, 'Publisher · content_enrichment · content version content-a');
  assert.deepEqual(calls, ['/api/registry/articles?include_pdf=true&page=1&page_size=100'],
    'supporting evidence must keep the approved version without fetching article detail');
}

await keywordEvidenceUsesApprovedContentVersion();

async function catalogAtSize(size) {
  const markdownCalls = [];
  const documents = Array.from({ length: size }, (_, index) => ({
    path: `wiki/page-${index}.md`, title: `Page ${index}`, type: index % 2 ? 'index' : 'daily',
    links: [], status: index % 2 ? '-' : index % 4 ? 'Reported' : 'No report',
    display_date: `2026-${String((index % 12) + 1).padStart(2, '0')}-01`,
    display_date_basis: 'report_observation',
  }));
  const catalogContext = vm.createContext({
    state: { graphMode: 'keywords', messages: [], activeContextPath: null,
      markdownByPath: {}, markdownRequests: {} },
    els: { wikiSearch: { value: '' }, detailMarkdown: { textContent: '' } },
    fetch: async (url) => {
      const path = String(url);
      if (path === '/api/config') return { ok: true, json: async () => ({
        concepts: [], graphs: { notes: null, keywords: null }, prompt_starters: ['starter'],
        documents, agent_mode: 'offline', model: 'offline-extractive', default_answer_mode: 'detailed',
      }) };
      if (path.startsWith('/wiki/')) {
        markdownCalls.push(path);
        return { ok: true, text: async () => '# Requested page\n\nNo report.' };
      }
      throw new Error(`unexpected fetch ${path}`);
    },
    applyTableFilter() {}, renderDetail() {}, renderChatContext() {},
    setConnectionStatus() {}, setAnswerMode() {}, setGraphMode() {}, renderMessages() {},
    updateDocumentStatus() {},
  });
  vm.runInContext([
    slice('function displayDateSortKey(value) {', '\nfunction buildWorkspaceData('),
    slice('function buildWorkspaceData(documents) {', '\nfunction keywordNodeId('),
    slice('function normalizeMojibake(text) {', '\nfunction inlineFmt('),
    slice('async function fetchMarkdown(path) {', '\nfunction updateDocumentStatus('),
    slice('async function ensureMarkdownLoaded(path) {', '\nfunction clearContext('),
    slice('async function loadConfig() {', '\nfunction registryElement('),
    'globalThis.loadCatalog = loadConfig; globalThis.loadRequestedPage = ensureMarkdownLoaded;',
  ].join('\n'), catalogContext);
  await catalogContext.loadCatalog();
  assert.equal(catalogContext.state.documents.length, size);
  assert.equal(catalogContext.state.rows.length, size);
  assert.equal(catalogContext.state.rows.filter((row) => row.status === 'Reported').length,
    documents.filter((row) => row.status === 'Reported').length);
  assert.equal(catalogContext.state.rows.filter((row) => row.status === 'No report').length,
    documents.filter((row) => row.status === 'No report').length);
  assert.equal(markdownCalls.length, 0, `${size}-page catalog initialization must not fetch bodies`);
  if (size > 0) {
    const path = 'wiki/page-0.md';
    await Promise.all([
      catalogContext.loadRequestedPage(path),
      catalogContext.loadRequestedPage(path),
    ]);
    assert.deepEqual(markdownCalls, ['/wiki/page-0.md'], `${size} pages should fetch a requested body once`);
  }
}

for (const size of [0, 1, 100, 229]) {
  await catalogAtSize(size);
}
