import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8');
const extract = (name) => source.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`))?.[0];
const names = ['registryElement', 'safeSourceUrl', 'appendRegistryTags', 'appendPdfVerifiedInformation', 'appendPdfAppearances'];
const created = [];
const document = { createElement(tag) {
  const element = { tag, children: [], append(...items) { this.children.push(...items); },
    setAttribute(name, value) { this[name] = value; } };
  created.push(element);
  return element;
} };
const context = vm.createContext({ document, URL });
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
console.log('PASS Registry/PDF detail labels, verified-summary precedence, PDF-provided summaries, missing values, source links, duplicate appearances, and excerpt hiding');
