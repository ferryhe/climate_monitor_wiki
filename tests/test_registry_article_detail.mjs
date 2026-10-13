import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8');
const extract = (name) => source.match(new RegExp(`(?:async )?function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`))?.[0];
const names = ['registryElement', 'safeSourceUrl', 'appendRegistryTags', 'appendPdfVerifiedInformation', 'appendPdfAppearances', 'registryMetric', 'registrySummaryPresentation', 'loadRegistryArticle'];
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
const enrichment = document.createElement('div');
const appearancesSection = document.createElement('section');
const appearancesEl = document.createElement('ul');
const contentSection = document.createElement('section');
const originalLink = document.createElement('a');
const context = vm.createContext({ document, URL, console,
  state: { registry: { articleDetailRequestSequence: 0 } },
  els: { registryArticleDetail: articleDetail, registryArticleTitle: articleTitle,
    registryArticleMeta: articleMeta, registryEnrichment: enrichment,
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

const verified = { children: [], append(...items) { this.children.push(...items); } };
context.appendPdfVerifiedInformation(verified, {
  verification_status: 'partial', checked_at: '2026-08-11', raw_url: 'https://example.org/article',
  summary: 'Do not repeat this PDF passage.',
  verified_information: { summary: 'Checked facts.', categories: ['Climate risk'], keywords: ['insurance'] },
});
assert.match(text(verified), /Summary Checked facts\./);
assert.match(text(verified), /Categories Climate risk/);
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
assert.match(text(enrichment), /Categories Capital & Solvency Supervision & Disclosure/);
assert.match(text(enrichment), /Keywords solvency climate risk/);
assert.doesNotMatch(text(enrichment), /manual_category_standardization|manual_supplement/);
assert.match(text(enrichment), /Website-only category/);
assert.match(text(enrichment), /website-only keyword/);
assert.doesNotMatch(text(enrichment), /Verified information/);

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
console.log('PASS full Registry/PDF detail render: reviewed PDF tags and field sources, occurrence website values, missing tags, legacy provenance hiding, Registry values, summaries, links, and appearances');
