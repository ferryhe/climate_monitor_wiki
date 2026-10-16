import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const root = process.cwd();
const approvedList = JSON.parse(process.argv[2]);
const sameUrlList = process.argv[3] ? JSON.parse(process.argv[3]) : null;
const approvedDetail = process.argv[4] ? JSON.parse(process.argv[4]) : null;
const sameUrlDetail = process.argv[5] ? JSON.parse(process.argv[5]) : null;
const source = readFileSync(`${root}/showcase/app.js`, "utf8");
const extract = name => {
  const index = source.indexOf(`function ${name}(`) >= 0
    ? source.indexOf(`function ${name}(`)
    : source.indexOf(`async function ${name}(`);
  assert.ok(index >= 0, name);
  const begin = source.slice(index - 6, index) === "async " ? index - 6 : index;
  return source.slice(begin, source.indexOf("\n}", index) + 2);
};

class Element {
  constructor() { this.children = []; this.dataset = {}; this.hidden = false; this.value = ""; this.classList = { add() {} }; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  setAttribute(name, value) { this[name] = value; }
  removeAttribute(name) { delete this[name]; }
  closest(selector) { return selector === "[data-article-id]" && this.dataset.articleId ? this : null; }
}

async function run(list, detail = null) {
  const listeners = [];
  const names = ["registryArticles", "registryArticleDetail", "registryArticleTitle", "registryArticleMeta",
    "registryArticleInfo", "registryEnrichment", "registryAppearances", "registryAppearancesSection",
    "registryContentSection", "registryMarkdown", "registryOriginalLink", "registryArticlesPanel",
    "registrySearch", "registryPublisherFilter"];
  const els = Object.fromEntries(names.map(name => [name, new Element()]));
  const state = { activeView: "registryView", registry: {
    articlePage: 1, articleRequestSequence: 0, articleDetailRequestSequence: 0, evidenceNavigationSequence: 0,
  } };
  const document = { createElement: () => new Element(), addEventListener(type, fn) { if (type === "click") listeners.push(fn); } };
  const window = { location: { hash: "" }, history: { pushState(_state, _title, hash) { window.location.hash = hash; } },
    matchMedia: () => ({ matches: false }), addEventListener() {} };
  const context = vm.createContext({
    document, window, els, state, Element, URL, URLSearchParams,
    registryPageFetch: async () => list,
    registryFetch: async () => {
      if (detail) return detail;
      throw new Error("Fixture detail unavailable");
    },
    registryErrorMessage: error => error.message,
    renderRegistryNotice() {}, updateRegistryPagination() {}, prefetchRegistryPage() {},
    setWorkspaceView(value) { state.activeView = value; }, setRegistryMode() {}, showMobileDetail() {},
    console, requestAnimationFrame() {},
  });
  for (const name of ["registryElement", "registryMetric", "registrySummaryPresentation", "appendRegistryTags",
    "appendPdfAppearances", "safeSourceUrl", "reviewedCheckedSourceUrl", "reviewedSourceRoles", "loadRegistryArticles",
    "sourceNavigationTarget", "sourceNavigationHref", "sourceNavigationFromHash", "resetRegistryArticleDetail", "loadRegistryArticle"])
    vm.runInContext(extract(name), context);
  const summaryPresentationStart = source.indexOf("const REGISTRY_SUMMARY_PRESENTATION = {");
  const summaryPresentationEnd = source.indexOf("\n};", summaryPresentationStart) + 3;
  vm.runInContext(source.slice(summaryPresentationStart, summaryPresentationEnd), context);
  const start = source.indexOf('  document.addEventListener("click", (event) => {\n    const target = event.target;',
    source.indexOf("function attachEvents()"));
  const end = source.indexOf('\n  window.addEventListener("resize"', start);
  assert.ok(start > 0 && end > start);
  vm.runInContext(source.slice(start, end), context);
  await context.loadRegistryArticles();
  const card = els.registryArticles.children[0];
  listeners[0]({ target: card, button: 0, preventDefault() {} });
  await new Promise(resolve => setImmediate(resolve));
  return { card, els, context };
}

assert.equal(approvedList.items.length, 1);
const approved = approvedList.items[0];
assert.equal(approved.owner_agent_verification?.verification_status, "verified");
assert.equal(approved.checked_source_url, approved.owner_agent_verification.final_url);
const checked = await run(approvedList);
  const checkedTarget = checked.context.sourceNavigationTarget(approved);
  assert.equal(checkedTarget.kind, "article");
  assert.equal(checkedTarget.checkedSourceUrl, approved.checked_source_url);
  assert.equal(checkedTarget.originalUrl, approved.canonical_url);
  const checkedHash = checked.context.sourceNavigationHref(approved);
  checked.context.window.location.hash = checkedHash;
  assert.deepEqual(checked.context.sourceNavigationFromHash(), checkedTarget);
assert.equal(checked.card.dataset.sourceChecked, "true");
assert.equal(checked.card.dataset.sourceUrl, approved.checked_source_url);
assert.equal(checked.els.registryArticleTitle.textContent, "Article unavailable");
assert.equal(checked.els.registryOriginalLink.hidden, false);
assert.equal(checked.els.registryOriginalLink.href, approved.checked_source_url);
assert.equal(checked.els.registryOriginalLink.textContent, "Open checked source");
assert.equal(checked.els.registryEnrichment.children.some(row => row.textContent.includes("Original canonical/source link (provenance)")),
  approved.checked_source_url !== approved.canonical_url);

const linksWithin = element => [
  ...(element.href ? [{ href: element.href, label: element.textContent }] : []),
  ...(element.children || []).flatMap(linksWithin),
];
if (approvedDetail) {
  assert.equal(approvedDetail.checked_source_url, approved.checked_source_url);
  const successful = await run(approvedList, approvedDetail);
  assert.equal(successful.els.registryArticleTitle.textContent, approvedDetail.title);
  assert.equal(successful.els.registryOriginalLink.href, approved.checked_source_url);
  assert.equal(successful.els.registryOriginalLink.textContent, "Open checked source");
  const successfulLinks = [
    { href: successful.els.registryOriginalLink.href, label: successful.els.registryOriginalLink.textContent },
    ...linksWithin(successful.els.registryEnrichment),
  ];
  const provenanceLink = successfulLinks.find(link => link.href === approved.canonical_url
    && link.label === "Open original source");
  assert.ok(provenanceLink, "successful native detail retains its distinct original provenance link");
  assert.equal(successfulLinks.filter(link => link.href === approved.checked_source_url).length, 1,
    "the checked source remains the single primary action");

  const ordinaryListItem = { ...approved, owner_agent_verification: null };
  delete ordinaryListItem.checked_source_url;
  const ordinaryDetail = { ...approvedDetail, owner_agent_verification: null };
  delete ordinaryDetail.checked_source_url;
  delete ordinaryDetail.checked_source_provenance;
  delete ordinaryDetail.original_source_url;
  const ordinary = await run({ ...approvedList, items: [ordinaryListItem] }, ordinaryDetail);
  assert.equal(ordinary.els.registryOriginalLink.href, ordinaryDetail.canonical_url);
  assert.equal(ordinary.els.registryOriginalLink.textContent, "Open original source");
  assert.equal(linksWithin(ordinary.els.registryEnrichment).some(link => link.href === approved.canonical_url
    && link.label === "Open original source"), false,
  "a record without owner receipt is not upgraded into checked-source provenance");
}

if (sameUrlList) {
  assert.equal(sameUrlList.items.length, 1);
  const sameUrl = sameUrlList.items[0];
  assert.equal(sameUrl.owner_agent_verification?.verification_status, "verified");
  assert.equal(sameUrl.canonical_url, sameUrl.checked_source_url);
  assert.equal(sameUrl.owner_agent_verification.final_url, sameUrl.canonical_url);
  const sameUrlResult = await run(sameUrlList);
  const sameUrlTarget = sameUrlResult.context.sourceNavigationTarget(sameUrl);
  assert.equal(sameUrlTarget.checkedSourceUrl, sameUrl.canonical_url);
  assert.equal(sameUrlTarget.originalUrl, sameUrl.canonical_url);
  const sameUrlHash = sameUrlResult.context.sourceNavigationHref(sameUrl);
  sameUrlResult.context.window.location.hash = sameUrlHash;
  assert.deepEqual(sameUrlResult.context.sourceNavigationFromHash(), sameUrlTarget);
  assert.equal(sameUrlResult.card.dataset.sourceChecked, "true");
  assert.equal(sameUrlResult.card.dataset.sourceUrl, sameUrl.canonical_url);
  assert.equal(sameUrlResult.els.registryArticleTitle.textContent, "Article unavailable");
  assert.equal(sameUrlResult.els.registryOriginalLink.hidden, false);
  assert.equal(sameUrlResult.els.registryOriginalLink.href, sameUrl.canonical_url);
  assert.equal(sameUrlResult.els.registryOriginalLink.textContent, "Open checked source");
  assert.equal(sameUrlResult.els.registryEnrichment.children.some(row => row.textContent.includes("provenance")), false);
  if (sameUrlDetail) {
    const sameUrlSuccess = await run(sameUrlList, sameUrlDetail);
    assert.equal(sameUrlSuccess.els.registryArticleTitle.textContent, sameUrlDetail.title);
    assert.equal(sameUrlSuccess.els.registryOriginalLink.href, sameUrl.canonical_url);
    assert.equal(sameUrlSuccess.els.registryOriginalLink.textContent, "Open checked source");
    assert.equal(linksWithin(sameUrlSuccess.els.registryEnrichment).some(link =>
      link.href === sameUrl.canonical_url && link.label === "Open original source"), false,
    "equal reviewed URLs render one checked-source action without a duplicate provenance action");
  }
}

const ordinary = { ...approved, owner_agent_verification: null, checked_source_url: undefined };
const unchanged = await run({ ...approvedList, items: [ordinary] });
assert.equal(unchanged.card.dataset.sourceChecked, undefined);
assert.equal(unchanged.els.registryOriginalLink.hidden, true);
console.log("Approved list card retains checked-source fallback and original provenance after detail failure; ordinary card behavior is unchanged.");
