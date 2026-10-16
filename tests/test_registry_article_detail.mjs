import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8');
const extract = (name) => source.match(new RegExp(`(?:async )?function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`))?.[0];
const names = ['showMobileDetail', 'registryElement', 'safeSourceUrl', 'reviewedCheckedSourceUrl', 'resetRegistryArticleDetail', 'appendRegistryTags', 'appendPdfVerifiedInformation', 'appendPdfAppearances', 'registryMetric', 'registrySummaryPresentation', 'loadRegistryArticle'];
const created = [];
const document = { createElement(tag) {
  const element = { tag, children: [], append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = [...items]; },
    setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; },
    get childElementCount() { return this.children.length; } };
  created.push(element);
  return element;
} };
const articleDetail = document.createElement('section');
const articleTitle = document.createElement('h2');
const articleMeta = document.createElement('dl');
const articleInfo = document.createElement('details');
const enrichment = document.createElement('div');
const appearancesSection = document.createElement('section');
const appearancesEl = document.createElement('ul');
const contentSection = document.createElement('section');
const originalLink = document.createElement('a');
const context = vm.createContext({ document, URL, console,
  state: { registry: { articleDetailRequestSequence: 0, evidenceNavigationSequence: 0 } },
  els: { registryArticleDetail: articleDetail, registryArticleTitle: articleTitle,
    registryArticleMeta: articleMeta, registryArticleInfo: articleInfo, registryEnrichment: enrichment,
    registryAppearances: appearancesEl, registryAppearancesSection: appearancesSection,
    registryContentSection: contentSection, registryOriginalLink: originalLink,
    registryContentTitle: document.createElement('h3'), registryMarkdown: document.createElement('div') },
  registryErrorMessage: (error) => String(error),
});
vm.runInContext(source.match(/const REGISTRY_SUMMARY_PRESENTATION = \{[\s\S]*?\n\};/)?.[0], context);
for (const name of names) {
  const fn = extract(name);
  assert.ok(fn, `missing ${name}`);
  vm.runInContext(fn, context);
}
const text = (node) => [node.textContent, ...(node.children || []).map(text)].filter(Boolean).join(' ');
const rootTags = (node, label) => node.children.filter((child) => child.children?.[0]?.textContent === label);
const anchors = (node) => [...(node.tag === 'a' ? [{ label: node.textContent, href: node.href }] : []), ...(node.children || []).flatMap(anchors)];

const verified = { children: [], append(...items) { this.children.push(...items); } };
context.appendPdfVerifiedInformation(verified, {
  verification_status: 'partial', checked_at: '2026-08-11', raw_url: 'https://example.org/article',
  summary: 'Do not repeat this PDF passage.',
  verified_information: { summary: 'Checked facts.', categories: ['Climate risk'], keywords: ['insurance'] },
});
assert.match(text(verified), /Summary Checked facts\./);
assert.match(text(verified), /Categories Climate risk/);
assert.match(text(verified), /Source Categories Climate risk/);
assert.match(text(verified), /Keywords insurance/);
assert.match(text(verified), /Website verification: partial · checked 2026-08-11/);
assert.doesNotMatch(text(verified), /Do not repeat this PDF passage\./);
assert.equal(verified.children[0].children.at(-1).href, 'https://example.org/article');

const partial = { children: [], append(...items) { this.children.push(...items); } };
context.appendPdfVerifiedInformation(partial, { verification_status: 'unchecked', verified_information: { keywords: ['retained'] } });
assert.doesNotMatch(text(partial), /Summary|Categories/);
assert.match(text(partial), /Keywords retained/);
assert.match(text(partial), /Website verification: unchecked/);

const pdfProvided = { children: [], append(...items) { this.children.push(...items); } };
context.appendPdfVerifiedInformation(pdfProvided, {
  verification_status: 'partial', checked_at: '2026-08-12', raw_url: 'https://example.org/original.pdf',
  summary: 'Passage summary from the PDF.',
});
assert.match(text(pdfProvided), /Summary Passage summary from the PDF\./);
assert.match(text(pdfProvided), /PDF-provided · Verification: partial · checked 2026-08-12/);
assert.equal(pdfProvided.children[0].children.at(-1).href, 'https://example.org/original.pdf');

const ownerChecked = { children: [], append(...items) { this.children.push(...items); } };
context.appendPdfVerifiedInformation(ownerChecked, {
  verification_status: 'verified', summary: 'Supported corrected summary.',
  owner_agent_verification: { verification_basis: 'owner-agent-information-check.v1' },
});
assert.match(text(ownerChecked), /Owner-agent source verification · Independently reviewed · Verification: verified/);
assert.doesNotMatch(text(ownerChecked), /PDF-provided/);

const missing = { children: [], append(...items) { this.children.push(...items); } };
context.appendPdfVerifiedInformation(missing, { verification_status: 'unchecked' });
assert.equal(missing.children.length, 0);

const appearances = { children: [], append(...items) { this.children.push(...items); } };
context.appendPdfAppearances(appearances, [
  { source_document: 'climate.pdf', page: 22, raw_url: 'https://example.org/a' },
  { source_document: 'climate.pdf', page: 22, raw_url: 'https://example.org/a' },
  { page: 3 },
]);
assert.equal(appearances.children.length, 3);
assert.deepEqual(appearances.children.map(text), [
  'climate.pdf · page 22 Open original source',
  'climate.pdf · page 22 Open original source',
  'page 3',
]);
assert.equal(appearances.children[0].children[1].href, 'https://example.org/a');
assert.doesNotMatch(source.match(/async function loadRegistryArticle\([\s\S]*?\n\}/)?.[0] || '',
  /appendRegistryPdfOccurrences|supporting_excerpt/);

const reviewedPdf = {
  title: 'Reviewed PDF article', publisher: 'Example', first_seen: '2026-01-01', last_seen: '2026-02-01',
  source_kind: 'pdf', categories: ['Capital & Solvency', 'Supervision & Disclosure'], keywords: ['solvency', 'climate risk'],
  metadata_provenance: { categories: 'manual_category_standardization', keywords: 'manual_supplement' },
  occurrences: [{ verification_status: 'verified', verified_information: { categories: ['Website-only category'], keywords: ['website-only keyword'] } }],
  appearances: [],
};
context.registryFetch = async () => reviewedPdf;
await context.loadRegistryArticle('reviewed-pdf', 'pdf');
assert.equal(articleInfo.hidden, false);
articleInfo.open = true;
context.resetRegistryArticleDetail('Loading another article…');
assert.equal(articleInfo.hidden, true);
assert.equal(articleInfo.open, undefined, 'switching articles resets the information disclosure');
await context.loadRegistryArticle('reviewed-pdf', 'pdf');
assert.match(text(enrichment), /Categories Capital & Solvency Supervision & Disclosure/);
assert.match(text(enrichment), /Keywords solvency climate risk/);
assert.doesNotMatch(text(enrichment), /manual_category_standardization|manual_supplement/);
assert.match(text(enrichment), /Website-only category/);
assert.match(text(enrichment), /website-only keyword/);
assert.doesNotMatch(text(enrichment), /Verified information/);
assert.equal(rootTags(enrichment, 'Categories').length, 1, 'current article categories render once');
assert.equal(rootTags(enrichment, 'Keywords').length, 1);
assert.match(text(enrichment), /Source Categories Website-only category/);
assert.doesNotMatch(text(rootTags(enrichment, 'Categories')[0]), /Website-only category/);

enrichment.replaceChildren();
context.registryFetch = async () => ({ ...reviewedPdf, categories: ['Adaptation & Resilience'], keywords: [],
  metadata_provenance: { categories: 'manual_category_standardization' }, occurrences: [], });
await context.loadRegistryArticle('pdf-categories-only', 'pdf');
assert.match(text(enrichment), /Categories Adaptation & Resilience/);
assert.doesNotMatch(text(enrichment), /Keywords/);

enrichment.replaceChildren();
context.registryFetch = async () => ({ ...reviewedPdf, categories: ['Legacy fixture category'], keywords: ['Legacy fixture keyword'],
  metadata_provenance: {}, occurrences: [], });
await context.loadRegistryArticle('pdf-legacy', 'pdf');
assert.doesNotMatch(text(enrichment), /Legacy fixture/);

enrichment.replaceChildren();
context.registryFetch = async () => ({ ...reviewedPdf, source_kind: 'registry',
  categories: ['Registry category'], keywords: ['Registry keyword'], metadata_provenance: {}, occurrences: [],
  pdf_occurrences: [], });
await context.loadRegistryArticle('registry-article');
assert.match(text(enrichment), /Registry category/);
assert.match(text(enrichment), /Registry keyword/);

context.registryFetch = async () => ({ ...reviewedPdf, categories: ['Food & Water Security'],
  occurrences: [
    { verified_information: { summary: 'First checked source.', categories: ['First source tag'], keywords: ['first-source-keyword'] } },
    { verified_information: { summary: 'Second checked source.', categories: ['Second source tag'], keywords: ['second-source-keyword'] } },
  ],
});
await context.loadRegistryArticle('multiple-sources', 'pdf');
assert.equal(rootTags(enrichment, 'Categories').length, 1);
assert.match(text(rootTags(enrichment, 'Categories')[0]), /Categories Food & Water Security/);
assert.doesNotMatch(text(rootTags(enrichment, 'Categories')[0]), /source tag/);
assert.match(text(enrichment), /Summary First checked source\. Source Categories First source tag Keywords first-source-keyword/);
assert.match(text(enrichment), /Summary Second checked source\. Source Categories Second source tag Keywords second-source-keyword/);

for (const categories of [[], undefined]) {
  context.registryFetch = async () => ({ ...reviewedPdf, categories, keywords: [],
    enrichment: { categories: ['Stale enrichment category'], keywords: ['Stale enrichment keyword'] },
  });
  await context.loadRegistryArticle('pdf-empty-or-missing', 'pdf');
  assert.equal(rootTags(enrichment, 'Categories').length, 0, 'empty or missing PDF root categories never borrow older values');
  assert.equal(rootTags(enrichment, 'Keywords').length, 0);
  assert.doesNotMatch(text(enrichment), /Stale enrichment/);
  assert.match(text(enrichment), /Source Categories Website-only category/);
}

for (const field of ['categories', 'keywords']) {
  context.registryFetch = async () => ({ ...reviewedPdf,
    categories: ['Article-only category'], keywords: ['article-only-keyword'],
    metadata_provenance: { [field]: 'reviewed_field' },
  });
  await context.loadRegistryArticle('mixed-field-provenance', 'pdf');
  assert.equal(rootTags(enrichment, 'Categories').length, field === 'categories' ? 1 : 0);
  assert.equal(rootTags(enrichment, 'Keywords').length, field === 'keywords' ? 1 : 0);
  assert.match(text(enrichment), /Source Categories Website-only category/);
  assert.ok(!text(enrichment).includes(field === 'categories' ? 'article-only-keyword' : 'Article-only category'));
}

const emptyRegistry = { ...reviewedPdf, categories: [], enrichment: { categories: ['Stale Registry category'] },
  occurrences: [], pdf_occurrences: [] };
context.registryFetch = async () => emptyRegistry;
await context.loadRegistryArticle('explicit-empty-registry');
assert.equal(rootTags(enrichment, 'Categories').length, 0);
assert.doesNotMatch(text(enrichment), /Stale Registry category/);
delete emptyRegistry.categories;
await context.loadRegistryArticle('absent-legacy-registry');
assert.equal(rootTags(enrichment, 'Categories').length, 1);
assert.match(text(enrichment), /Stale Registry category/);
console.log('PASS full Registry/PDF detail render: reviewed PDF tags and field sources, occurrence website values, missing tags, legacy provenance hiding, Registry values, summaries, links, and appearances');

// Reviewed presentation never rewrites the retained PDF or website evidence.
const curated = { children: [], append(...items) { this.children.push(...items); } };
context.appendPdfVerifiedInformation(curated, {
  display_summary: 'Reviewed source summary.', display_summary_provenance: 'owner_reviewed_source_evidence',
  summary: 'Mixed PDF passage.', verification_status: 'partial', checked_at: '2026-10-15',
  verified_information: { summary: 'Retained check summary.', keywords: ['insurance'], source_url: 'https://example.org/checked' },
});
assert.match(text(curated), /Reviewed source summary\./);
assert.match(text(curated), /Approved reviewed summary/);
assert.match(text(curated), /Website verification: partial/);
assert.match(text(curated), /Keywords insurance/);
assert.doesNotMatch(text(curated), /Mixed PDF passage|Retained check summary/);
assert.equal(curated.children.at(-1).children.at(-1).href, 'https://example.org/checked');

const originalUrl = 'https://example.org/pdf-only';
const checkedUrl = 'https://example.org/articles/current';
const ownerReview = { verification_status: 'verified', review_id: 'review-1', reviewer: { id: 'reviewer' }, final_url: checkedUrl };
const occurrence = { raw_url: originalUrl, checked_source_url: checkedUrl, owner_agent_verification: ownerReview,
  verification_status: 'verified', summary: 'Supported summary.' };
for (const articleSource of ['pdf', 'registry']) {
  context.registryFetch = async () => ({ article_id: 'pdf-only', title: 'Reviewed article', canonical_url: originalUrl,
    original_url: originalUrl, source_kind: articleSource === 'pdf' ? 'pdf' : 'registry', checked_source_url: checkedUrl,
    owner_agent_verification: ownerReview, occurrences: [occurrence], pdf_occurrences: [occurrence], appearances: [] });
  await context.loadRegistryArticle('pdf-only', articleSource);
  assert.equal(originalLink.href, checkedUrl);
  assert.equal(originalLink.textContent, 'Open checked source');
  const detailLinks = anchors(enrichment), appearanceLinks = anchors(appearancesEl);
  assert.ok(detailLinks.some((link) => link.href === checkedUrl && link.label === 'Open checked source'));
  assert.ok(detailLinks.some((link) => link.href === originalUrl && link.label === 'Open original source'));
  assert.ok(appearanceLinks.some((link) => link.href === checkedUrl && link.label === 'Open checked source'));
  assert.ok(appearanceLinks.some((link) => link.href === originalUrl && link.label === 'Open original source'));
}
context.registryFetch = async () => ({ article_id: 'pending', title: 'Pending article', canonical_url: originalUrl,
  checked_source_url: checkedUrl, owner_agent_verification: { ...ownerReview, review_id: null }, occurrences: [], pdf_occurrences: [] });
await context.loadRegistryArticle('pending');
assert.equal(originalLink.href, originalUrl, 'unreviewed checked URL fields cannot replace the original destination');
assert.equal(originalLink.textContent, 'Open original source');

enrichment.replaceChildren();
context.registryFetch = async () => { throw new Error('Registry unavailable'); };
await context.loadRegistryArticle('good', 'registry', checkedUrl, true, originalUrl);
assert.equal(originalLink.href, checkedUrl, 'a detail failure keeps the approved checked destination as primary');
assert.equal(originalLink.textContent, 'Open checked source');
assert.ok(anchors(enrichment).some((link) => link.href === originalUrl && link.label === 'Open original source'),
  'a differing canonical URL remains separately labeled as provenance after a detail failure');
