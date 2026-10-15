import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8');
const start = source.indexOf('async function loadImportedRegistryReport(');
const end = source.indexOf('\nfunction appendRegistryTags(', start);
const element = (tag = 'div', className = '', text = '') => ({
  tag, className, text, children: [], dataset: {}, handlers: {}, attributes: {}, open: false,
  append(...children) { this.children.push(...children); },
  setAttribute(name, value) { this.attributes[name] = value; },
  addEventListener(event, handler) { this.handlers[event] = handler; },
  reportValidity() { return this.valid !== false; },
});
const texts = (node) => [node.text, ...node.children.flatMap(texts)].filter(Boolean);
const nodes = (node) => [node, ...node.children.flatMap(nodes)];
const els = Object.fromEntries(['registryReportDetail', 'registryReportTitle', 'registryReportMeta',
  'registryImportedReport', 'registryReportPdf'].map((key) => [key, element()]));
const calls = [];
let resolveOld;
const context = vm.createContext({
  els, state: { registry: { reportRequestSequence: 0 } }, registryElement: element,
  registryMetric: (label, value) => element('div', '', `${label}: ${value}`),
  registryErrorMessage: String,
  clearHistoricalReportContent() { els.registryImportedReport.children = []; },
  safeSourceUrl: (url) => url?.startsWith('https://') ? url : '',
  appendInformationCheck(container) { container.append(element('p', '', 'PDF import · URL accessible')); },
  registryFetch: async (path) => {
    calls.push(path);
    if (path.includes('old')) return new Promise((resolve) => { resolveOld = resolve; });
    if (path.includes('reference')) return {report_title: 'Reference report', filename: 'second.pdf',
      articles: [{article_id: 'alias-article', title: 'Verified canonical article title', metadata_only: true,
        verified_primary_reference: {entity_kind: 'article', entity_id: 'canonical-article'}, occurrences: []}],
      calendar_items: [{metadata_only: true, page: 12, source_filename: 'second.pdf',
        verified_primary_display: {name: 'IAIS Annual Conference 2027', source_url: 'https://example.org/target'}}],
      pagination: {page: 1, pages: 1, total: 2}};
    const second = path.includes('page=2');
    return { report_title: 'Report', filename: 'report.pdf', executive_summary: ['Complete summary'], executive_summary_label: 'Adopted from the original PDF',
      articles: [{article_id: 'a', title: 'Article', occurrences: [{source_document: 'report.pdf',
        report_date: '2026-09-28', page: 19, raw_url: 'https://example.org/a', summary: 'Real summary'}]}],
      calendar_items: second ? [{name: 'Meeting', kind: 'event', raw_date: 'October 20', organizer: 'Host',
        relevance: 'Climate', location: 'Online', page: 23, date_evidence: 'Internal detail',
        source_urls: ['https://example.org/event']}] : [],
      article_offset: second ? 5 : 0, pagination: {page: second ? 2 : 1, pages: 2, total: 7},
      pages: [{page: 1, text: 'Extracted text should never render'}] };
  },
});
vm.runInContext(source.match(/function showMobileDetail\([^]*?\n\}/)[0] + '\n' + source.slice(start, end) + '\nglobalThis.load = loadImportedRegistryReport;', context);
await context.load('document');
assert.equal(calls[0], '/api/registry/pdf-intake/reports/document?page=1&page_size=5');
assert.ok(texts(els.registryImportedReport).includes('1. Article'));
const firstNodes = nodes(els.registryImportedReport);
const executive = firstNodes.find((node) => node.className.includes('registry-imported-summary'));
assert.equal(executive.tag, 'details');
assert.equal(executive.open, false);
assert.equal(executive.children[0].tag, 'summary');
assert.equal(executive.children[0].text, 'Executive Summary');
assert.ok(texts(executive).includes('Complete summary'));
const executiveBody = executive.children[1];
assert.equal(executiveBody.children.at(-1).text, 'Adopted from the original PDF');
assert.equal(executiveBody.children.at(-1).className, 'muted registry-provenance');
assert.ok(executiveBody.children.findIndex((node) => node.text === 'Complete summary') <
  executiveBody.children.findIndex((node) => node.text === 'Adopted from the original PDF'));

const list = firstNodes.find((node) => node.className === 'registry-pdf-history__list');
assert.equal(list.tag, 'ul');
const occurrence = list.children[0];
const information = occurrence.children.find((node) => node.tag === 'details');
assert.equal(information.open, false);
assert.equal(information.children[0].text, 'Report information');
assert.ok(texts(information).includes('report.pdf · Report date 2026-09-28 · page 19'));
assert.ok(texts(information).includes('PDF import · URL accessible'));
assert.ok(occurrence.children.some((node) => node.text === 'Real summary'));
assert.ok(occurrence.children.findIndex((node) => node.text === 'Open source link') <
  occurrence.children.indexOf(information));
assert.equal(firstNodes.find((node) => node.text === 'Previous').disabled, true);
assert.equal(firstNodes.find((node) => node.text === 'Next').disabled, false);
firstNodes.find((node) => node.text === 'Next').handlers.click();
await new Promise((resolve) => setImmediate(resolve));
const displayed = texts(els.registryImportedReport);
assert.ok(displayed.includes('6. Article'));
assert.ok(!displayed.includes('Complete summary'));
assert.ok(!displayed.includes('Executive Summary'));
assert.ok(displayed.includes('Date(s)') && displayed.includes('Host') && displayed.includes('Location'));
assert.ok(!displayed.includes('Internal detail'));
assert.ok(!displayed.some((text) => text.includes('Extracted text')));
const secondNodes = nodes(els.registryImportedReport);
assert.equal(secondNodes.find((node) => node.text === 'Next').disabled, true);
assert.equal(secondNodes.find((node) => node.className === 'meeting-entry').tag, 'details');
const jump = secondNodes.find((node) => node.tag === 'form');
const input = jump.children[0];
assert.equal(input.type, 'number');
assert.equal(input.min, '1');
assert.equal(input.max, '2');
assert.equal(input.step, '1');
assert.equal(input.required, true);
assert.equal(input.value, '2');
assert.equal(input.attributes['aria-label'], 'Page number');
assert.equal(jump.children[1].type, 'submit');
const submit = () => jump.handlers.submit({preventDefault() {}});
let callCount = calls.length;
submit();
assert.equal(calls.length, callCount, 'Current-page submission must not reload');
input.value = '0'; input.valid = false;
submit();
assert.equal(calls.length, callCount, 'Invalid page must not issue a request');
input.value = '1'; input.valid = true;
submit();
await new Promise((resolve) => setImmediate(resolve));
assert.equal(calls.at(-1), '/api/registry/pdf-intake/reports/document?page=1&page_size=5');
assert.equal(nodes(els.registryImportedReport).find((node) => node.tag === 'details').open, false);
const old = context.load('old');
await context.load('current');
resolveOld({report_title: 'Stale report'});
await old;
assert.equal(els.registryReportTitle.textContent, 'Report');
console.log('PDF summary disclosure, page jump, pagination, numbering, meetings and stale requests passed');

const reviewedReport = element();
context.appendRegistryPdfOccurrences(reviewedReport, [{
  source_document: 'reviewed.pdf', report_date: '2026-09-14', page: 13,
  raw_url: 'https://example.org/article', summary: 'Mixed raw PDF passage.',
  display_summary: 'Reviewed report summary.', display_summary_provenance: 'owner_reviewed_source_evidence',
  verified_information: { summary: 'Outdated checked summary.', keywords: ['insurance'] },
}]);
assert.ok(texts(reviewedReport).includes('Reviewed report summary.'));
assert.ok(texts(reviewedReport).includes('Approved reviewed summary'));
assert.ok(!texts(reviewedReport).includes('Mixed raw PDF passage.'));
assert.ok(texts(reviewedReport).includes('reviewed.pdf · Report date 2026-09-14 · page 13'));

assert.ok(!texts(reviewedReport).includes('Verified information'));
assert.ok(!texts(reviewedReport).includes('Outdated checked summary.'));
assert.ok(texts(reviewedReport).includes('PDF import · URL accessible'));
assert.ok(texts(reviewedReport).includes('Open source link'));

await context.load('reference');
const references = nodes(els.registryImportedReport);
assert.ok(texts(els.registryImportedReport).includes('IAIS Annual Conference 2027'));
assert.ok(texts(els.registryImportedReport).includes('second.pdf · page 12'));
assert.ok(texts(els.registryImportedReport).includes('Reference to a verified meeting; this report observation is unverified.'));
assert.ok(!texts(els.registryImportedReport).includes('Calendar item'));
assert.equal(references.find((node) => node.text === 'IAIS Annual Conference 2027').href, 'https://example.org/target');
const articleReference = references.find((node) => node.className === 'registry-article-link');
assert.equal(articleReference.dataset.articleId, 'canonical-article');
assert.equal(articleReference.dataset.articleSource, 'registry');
