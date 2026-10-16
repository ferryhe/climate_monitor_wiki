import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const fixture = JSON.parse(readFileSync(process.argv[2], "utf8"));
const source = readFileSync(new URL("../showcase/app.js", import.meta.url), "utf8");
const extract = (name) => {
  const index = source.indexOf(`function ${name}(`) >= 0
    ? source.indexOf(`function ${name}(`) : source.indexOf(`async function ${name}(`);
  assert.ok(index >= 0, name);
  const start = source.slice(index - 6, index) === "async " ? index - 6 : index;
  return source.slice(start, source.indexOf("\n}", index) + 2);
};

class Element {
  constructor(tag = "div", text = "") {
    this.tag = tag; this.textContent = text; this.children = []; this.dataset = {}; this.hidden = false;
    this.classList = { add() {}, remove() {} };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  addEventListener() {}
  closest(selector) {
    if (selector === "[data-article-id]" && this.dataset.articleId) return this;
    if (selector === "[data-source-navigation]" && this.dataset.sourceNavigation) return this;
    return null;
  }
}
const walk = (node) => [node, ...node.children.flatMap(walk)];
const makeContext = (reportPayload, pdfListPayload = null) => {
  const listeners = [];
  const names = ["registryReportDetail", "registryReportTitle", "registryReportMeta", "registryImportedReport", "registryReportPdf",
    "registryExecutiveSummary", "registryBriefingExecutiveSummaryItems", "registryExecutiveSummaryItems", "registryMonitoringSnapshot",
    "registrySnapshotMetrics", "registrySnapshotNotes", "registryReportArticlesTitle", "registryReportArticles",
    "registryArticleDetail", "registryArticleTitle", "registryArticleMeta", "registryArticleInfo", "registryEnrichment",
    "registryAppearances", "registryAppearancesSection", "registryContentSection", "registryMarkdown", "registryOriginalLink",
    "registryArticlesPanel"];
  const els = Object.fromEntries(names.map((name) => [name, new Element()]));
  const state = { activeView: "registryView", registry: { reportRequestSequence: 0, selectedReportDate: null,
    articleDetailRequestSequence: 0, evidenceNavigationSequence: 0 } };
  const window = { location: { hash: "" }, history: { pushState(_state, _title, hash) { window.location.hash = hash; } },
    matchMedia: () => ({ matches: false }), addEventListener() {} };
  const document = { createElement: (tag) => new Element(tag), addEventListener(type, fn) { if (type === "click") listeners.push(fn); } };
  const context = vm.createContext({ Element, document, window, els, state, URL, URLSearchParams, console,
    registryElement: (tag, _className = "", text = "") => new Element(tag, text),
    registryMetric: (label, value) => new Element("div", `${label}: ${value}`),
    registryErrorMessage: (error) => error.message, appendInformationCheck() {},
    clearHistoricalReportContent() { els.registryImportedReport.replaceChildren(); },
    showMobileDetail() {}, setWorkspaceView(value) { state.activeView = value; }, setRegistryMode() {}, requestAnimationFrame() {},
    registryFetch: async (path) => {
      if (path.startsWith("/api/registry/pdf-intake/reports/")) return reportPayload;
      if (path.startsWith("/api/registry/pdf-intake/articles?")) return pdfListPayload;
      throw new Error("Independent simulated detail outage");
    },
  });
  for (const name of ["escapeHtml", "safeSourceUrl", "safeReportSourceUrl", "reviewedCheckedSourceUrl", "reviewedSourceRoles", "registryElement",
    "showMobileDetail", "resetRegistryArticleDetail", "loadRegistryArticle", "appendRegistryPdfOccurrences",
    "loadImportedRegistryReport", "beginEvidenceNavigation", "sourceNavigationTarget", "sourceNavigationAttributes",
    "sourceNavigationHref", "sourceNavigationFromHash", "openPdfObservationEvidence", "attachSourceCardClickHandler"]) {
    vm.runInContext(extract(name), context);
  }
  context.runActualArticleClick = () => {
    const start = source.indexOf('  document.addEventListener("click", (event) => {\n    const target = event.target;', source.indexOf("function attachEvents()"));
    const end = source.indexOf('\n  window.addEventListener("resize"', start);
    assert.ok(start > 0 && end > start);
    vm.runInContext(source.slice(start, end), context);
  };
  return { context, els, listeners };
};

const report = makeContext(fixture.pdf_report);
await report.context.loadImportedRegistryReport(fixture.pdf_report.document_sha256);
const articleButton = walk(report.els.registryImportedReport).find((item) => item.dataset.articleId);
assert.ok(articleButton);
assert.equal(articleButton.dataset.sourceChecked, "true");
assert.equal(articleButton.dataset.sourceUrl, fixture.checked_url);
assert.equal(articleButton.dataset.originalUrl, fixture.original_url);
report.context.runActualArticleClick();
report.listeners[0]({ target: articleButton, button: 0, preventDefault() {} });
await new Promise((resolve) => setImmediate(resolve));
assert.equal(report.els.registryOriginalLink.href, fixture.checked_url);
assert.equal(report.els.registryOriginalLink.textContent, "Open checked source");
if (fixture.checked_url === fixture.original_url) {
  assert.equal(walk(report.els.registryEnrichment).filter((item) => item.href === fixture.checked_url).length, 0);
} else {
  assert.ok(walk(report.els.registryEnrichment).some((item) => item.href === fixture.original_url));
}

const observation = makeContext({}, fixture.pdf_list);
const target = observation.context.sourceNavigationTarget(fixture.chat_source);
assert.equal(target.kind, "pdf-observation");
assert.equal(target.checkedSourceUrl, fixture.checked_url);
assert.equal(target.originalUrl, fixture.original_url);
const attributes = observation.context.sourceNavigationAttributes(fixture.chat_source);
const card = new Element("a");
for (const [, name, value] of attributes.matchAll(/data-([a-z-]+)="([^"]*)"/g)) {
  card.dataset[name.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase())] = value;
}
assert.equal(card.dataset.sourceChecked, "true");
observation.context.attachSourceCardClickHandler();
observation.listeners[0]({ target: card, button: 0, preventDefault() {} });
await new Promise((resolve) => setImmediate(resolve));
assert.equal(observation.context.sourceNavigationFromHash().checkedSourceUrl, fixture.checked_url);
assert.equal(observation.els.registryOriginalLink.href, fixture.checked_url);
assert.equal(observation.els.registryOriginalLink.textContent, "Open checked source");
if (fixture.checked_url === fixture.original_url) {
  assert.equal(walk(observation.els.registryEnrichment).filter((item) => item.href === fixture.checked_url).length, 0);
} else {
  assert.ok(walk(observation.els.registryEnrichment).some((item) => item.href === fixture.original_url));
}

console.log("Actual imported PDF report and Wiki-to-Chat PDF observation routes retain checked and original roles through detail outages.");
