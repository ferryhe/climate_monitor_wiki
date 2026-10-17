import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const taxonomy = {categories:[{id:"flood",label:"Flood"},{id:"insurance",label:"Insurance"}]};
const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8');
const index = readFileSync(new URL('../showcase/index.html', import.meta.url), 'utf8');
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
    metadata_provenance: { categories: index % 3 === 0 ? 'manual_enrichment' : 'source_report' },
    categories: ['Flood', 'Insurance'],
  }));
  const context = vm.createContext({
    GRAPH_COPY: { articleCategories: { title: 'Category Map', hint: 'hint', legendHtml: '' } },
    state: { articleCategoryGraphLoading: false, graphData: { articleCategories: null } },
    els: { articleCategoryEvidence: { hidden: true, replaceChildren() {} } },
    fetch: async (url) => {
      calls.push(String(url));
      const parsed = new URL(String(url), 'http://localhost');
      assert.equal(parsed.pathname, '/api/registry/article-categories', 'graph must use the snapshot-backed category endpoint');
      return { ok: true, json: async () => ({ items, taxonomy }) };
    },
    registryErrorMessage: (error) => String(error),
    renderCurrentGraph() {},
  });
  vm.runInContext([
    slice('function buildArticleCategoryGraph(articles, taxonomy) {', '\nasync function loadArticleCategoryGraph()'),
    slice('async function registryFetch(path) {', '\nfunction renderRegistryNotice('),
    slice('async function loadArticleCategoryGraph() {', '\nfunction clearArticleCategoryEvidence()'),
    slice('function clearArticleCategoryEvidence() {', '\nfunction setGraphMode('),
    'globalThis.runGraph = loadArticleCategoryGraph;',
  ].join('\n'), context);
  await context.runGraph();
  const graph = JSON.parse(JSON.stringify(context.state.graphData.articleCategories));
  assert.equal(calls.length, 1, `${size} articles should need one complete collection request`);
  assert.equal(context.state.articleCategoryGraphLoading, false);
  if (size === 0) {
    assert.equal(graph.nodes.length, 2);
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
    assert.equal(pdfEvidence.metadata_provenance.categories, 'manual_enrichment');
    assert.ok(pdfEvidence.current_version_id);
  }
}

for (const size of [0, 1, 100, 229]) {
  await graphAtSize(size);
}

async function categoryEvidenceUsesApprovedContentVersion() {
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
    metadata_provenance: { categories: 'content_enrichment' }, categories: ['Insurance'],
  };
  const element = (tag, className, text) => ({
    tag, className, text, dataset: {}, children: [],
    append(...children) { this.children.push(...children); },
  });
  const context = vm.createContext({
    GRAPH_COPY: { articleCategories: { title: 'Category Map', hint: 'hint', legendHtml: '' } },
    state: { articleCategoryGraphLoading: false, graphMode: 'article-categories', graphData: { articleCategories: null } },
    els: { articleCategoryEvidence: section },
    fetch: async (url) => {
      calls.push(String(url));
      assert.equal(String(url), '/api/registry/article-categories');
      return { ok: true, json: async () => ({ items: [article], taxonomy }) };
    },
    registryElement: element, registryErrorMessage: String, renderCurrentGraph() {},
  });
  vm.runInContext([
    slice('function buildArticleCategoryGraph(articles, taxonomy) {', '\nasync function loadArticleCategoryGraph()'),
    slice('async function registryFetch(path) {', '\nfunction renderRegistryNotice('),
    slice('async function loadArticleCategoryGraph() {', '\nfunction clearArticleCategoryEvidence()'),
    slice('function clearArticleCategoryEvidence() {', '\nfunction setGraphMode('),
    slice('function showArticleCategoryEvidence(label, articles) {', '\nasync function fetchMarkdown('),
    'globalThis.loadGraph = loadArticleCategoryGraph; globalThis.showEvidence = showArticleCategoryEvidence;',
  ].join('\n'), context);
  await context.loadGraph();
  const keyword = context.state.graphData.articleCategories.nodes.find((node) => node.label === 'Insurance');
  assert.ok(keyword);
  context.showEvidence(keyword.label, keyword.articles);
  const footer = section.children.find((item) => item.tag === 'ul').children[0].children[1].text;
  assert.equal(footer, 'Publisher · content_enrichment · content version content-a');
  assert.deepEqual(calls, ['/api/registry/article-categories'],
    'supporting evidence must keep the approved version without fetching article detail');
}

await categoryEvidenceUsesApprovedContentVersion();

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

async function articleDetailUsesCurrentCategories() {
  const element = (tag = 'div', className = '', text = '') => ({
    tag, className, text, children: [], hidden: false,
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute() {},
    removeAttribute(name) { delete this[name]; },
    get childElementCount() { return this.children.length; },
  });
  const els = Object.fromEntries([
    'registryArticleDetail', 'registryArticleTitle', 'registryArticleMeta', 'registryEnrichment',
    'registryAppearances', 'registryAppearancesSection', 'registryContentSection',
    'registryOriginalLink', 'registryContentTitle', 'registryMarkdown',
  ].map((name) => [name, element()]));
  let article;
  const context = vm.createContext({
    els, state: { registry: { articleDetailRequestSequence: 0, evidenceNavigationSequence: 0 } },
    registryElement: element, registryMetric: element, registryFetch: async () => article,
    safeSourceUrl: (value) => value || '', reviewedCheckedSourceUrl: () => '', registryErrorMessage: String,
    registrySummaryPresentation: () => ({}), appendPdfAppearances() {},
  });
  vm.runInContext([
    slice('function showMobileDetail(panel) {', '\nfunction closeMobileDetail('),
    slice('function appendRegistryTags(container, label, values) {', '\nasync function loadRegistryArticle('),
    slice('function resetRegistryArticleDetail(title, fallbackSourceUrl = "", fallbackSourceIsChecked = false) {', '\nasync function loadRegistryArticle('),
    slice('async function loadRegistryArticle(articleId, articleSource = "registry", fallbackSourceUrl = "",', '\nfunction attachSourceCardClickHandler('),    'globalThis.loadDetail = loadRegistryArticle;',
  ].join('\n'), context);
  const texts = (node) => [node.text, ...node.children.flatMap(texts)].filter(Boolean);
  article = {
    title: 'Approved PDF article', categories: ['Food & Water Security'],
    metadata_provenance: { categories: 'reviewed_category_correction' },
    occurrences: [{ verified_information: {
      summary: 'Checked summary', categories: ['Old source category'], keywords: ['Observed keyword'],
    } }],
  };
  await context.loadDetail('pdf-article', 'pdf');
  let displayed = texts(els.registryEnrichment);
  assert.equal(displayed.filter((text) => text === 'Categories').length, 1);
  assert.ok(displayed.includes('Food & Water Security'));
  assert.ok(!displayed.includes('Source Categories'));
  assert.ok(!displayed.includes('Old source category'));
  const currentCategories = els.registryEnrichment.children.find((block) => block.children[0]?.text === 'Categories');
  assert.ok(!texts(currentCategories).includes('Old source category'), 'source categories do not become article categories');
  assert.ok(!displayed.includes('Checked summary'));
  assert.ok(!displayed.includes('Observed keyword'));
  article = { title: 'Unclassified article', categories: [], enrichment: { categories: ['Stale category'] } };
  await context.loadDetail('core-article');
  displayed = texts(els.registryEnrichment);
  assert.ok(!displayed.includes('Stale category'), 'an explicit empty approved classification must stay empty');
  delete article.categories;
  await context.loadDetail('legacy-core-article');
  assert.ok(texts(els.registryEnrichment).includes('Stale category'), 'legacy absent category fields retain enrichment fallback');
}

await articleDetailUsesCurrentCategories();

async function importedPdfDetailOmitsUnwantedMetadata() {
  const element = (tag = 'div', className = '', text = '') => ({
    tag, className, text, children: [], hidden: false, dataset: {},
    append(...children) { this.children.push(...children); },
    setAttribute() {}, addEventListener() {},
  });
  const els = Object.fromEntries([
    'registryReportDetail', 'registryReportTitle', 'registryReportMeta', 'registryImportedReport', 'registryReportPdf',
  ].map((name) => [name, element()]));
  const calls = [];
  const context = vm.createContext({
    els, state: { registry: { reportRequestSequence: 0 } },
    registryFetch: async (path) => {
      calls.push(path);
      return {
        report_title: 'Imported report', report_date: '2026-09-28', filename: 'report.pdf',
        source_filenames: ['report.pdf'], edition: 5, reporting_period: 'Sep',
        period_start: '2026-09-01', period_end: '2026-09-28', page_count: 12,
        pdf_created_at: 'created', pdf_modified_at: 'modified', imported_at: 'imported',
        pdf_metadata: { '/CreationDate': 'creation', '/Creator': 'creator', '/ModDate': 'modification',
          '/Producer': 'producer', '/Title': 'Useful PDF title' },
        report_pdf: { filename: 'report.pdf', download_url: '/api/registry/pdf-intake/reports/doc-1/pdf' },
      };
    },
    registryElement: element,
    registryMetric: (label, value) => element('div', 'metric', `${label}: ${value}`),
    clearHistoricalReportContent() {}, registryErrorMessage: String,
    appendRegistryPdfOccurrences() {}, appendInformationCheck() {}, safeSourceUrl: (value) => value,
  });
  vm.runInContext([
    slice('function showMobileDetail(panel) {', '\nfunction closeMobileDetail('),
    slice('async function loadImportedRegistryReport(documentId, page = 1) {', '\nfunction appendRegistryPdfOccurrences('),
    'globalThis.loadImported = loadImportedRegistryReport;',
  ].join('\n'), context);
  await context.loadImported('doc-1');
  const texts = (node) => [node.text, ...node.children.flatMap(texts)].filter(Boolean);
  const displayed = texts(els.registryImportedReport).join(' ');
  for (const hidden of ['Sources', 'Edition', 'Period start', 'Period end',
    'PDF created', 'PDF modified', 'CreationDate', 'Creator', 'ModDate', 'Producer', 'creation', 'producer']) {
    assert.ok(!displayed.includes(hidden), `${hidden} should not appear in imported PDF detail`);
  }
  assert.ok(displayed.includes('Report date: 2026-09-28'));
  assert.ok(displayed.includes('Reporting period: Sep'));
  assert.ok(displayed.includes('Pages: 12'));
  assert.ok(displayed.includes('Imported: imported'));
  assert.ok(displayed.includes('Title: Useful PDF title'));
  assert.deepEqual(calls, ['/api/registry/pdf-intake/reports/doc-1?page=1&page_size=5']);
  assert.equal(els.registryReportPdf.href, '/api/registry/pdf-intake/reports/doc-1/pdf');
}

await importedPdfDetailOmitsUnwantedMetadata();

assert.ok(!source.includes('loadFinalPipelineReports'));
assert.ok(!source.includes("fetch('/api/registry/final-reports')"));
assert.ok(!source.includes("fetch('/api/job-status')"));
assert.ok(!index.includes('Enter for a new line. Use Ctrl/⌘ + Enter to send.'));
assert.ok(source.includes('(event.metaKey || event.ctrlKey) && event.key === "Enter"'));
