import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8');
const start = source.indexOf('async function loadImportedRegistryReport(');
const end = source.indexOf('\nfunction appendRegistryTags(', start);
const element = (tag = 'div', className = '', text = '') => ({
  tag, className, text, children: [], dataset: {}, handlers: {},
  append(...children) { this.children.push(...children); },
  setAttribute() {}, addEventListener(event, handler) { this.handlers[event] = handler; },
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
  appendVerifiedInformation() {},
  registryFetch: async (path) => {
    calls.push(path);
    if (path.includes('old')) return new Promise((resolve) => { resolveOld = resolve; });
    const second = path.includes('page=2');
    return { report_title: 'Report', filename: 'report.pdf', executive_summary: ['Complete summary'],
      articles: [{article_id: 'a', title: 'Article', occurrences: [{source_document: 'report.pdf',
        report_date: '2026-09-28', page: 19, raw_url: 'https://example.org/a', summary: 'Real summary'}]}],
      calendar_items: second ? [{name: 'Meeting', kind: 'event', raw_date: 'October 20', organizer: 'Host',
        relevance: 'Climate', location: 'Online', page: 23, date_evidence: 'Internal detail',
        source_urls: ['https://example.org/event']}] : [],
      article_offset: second ? 5 : 0, pagination: {page: second ? 2 : 1, pages: 2, total: 7},
      pages: [{page: 1, text: 'Extracted text should never render'}] };
  },
});
vm.runInContext(source.slice(start, end) + '\nglobalThis.load = loadImportedRegistryReport;', context);
await context.load('document');
assert.equal(calls[0], '/api/registry/pdf-intake/reports/document?page=1&page_size=5');
assert.ok(texts(els.registryImportedReport).includes('1. Article'));
const firstNodes = nodes(els.registryImportedReport);
const list = firstNodes.find((node) => node.className === 'registry-pdf-history__list');
assert.equal(list.tag, 'ul');
const occurrence = list.children[0];
assert.ok(occurrence.children.findIndex((node) => node.text === 'Open source link') <
  occurrence.children.findIndex((node) => node.text === 'PDF import · URL accessible'));
assert.equal(firstNodes.find((node) => node.text === 'Previous').disabled, true);
assert.equal(firstNodes.find((node) => node.text === 'Next').disabled, false);
firstNodes.find((node) => node.text === 'Next').handlers.click();
await new Promise((resolve) => setImmediate(resolve));
const displayed = texts(els.registryImportedReport);
assert.ok(displayed.includes('6. Article'));
assert.ok(displayed.includes('Complete summary'));
assert.ok(displayed.includes('Date(s)') && displayed.includes('Host') && displayed.includes('Location'));
assert.ok(!displayed.includes('Internal detail'));
assert.ok(!displayed.some((text) => text.includes('Extracted text')));
const secondNodes = nodes(els.registryImportedReport);
assert.equal(secondNodes.find((node) => node.text === 'Next').disabled, true);
assert.equal(secondNodes.find((node) => node.className === 'meeting-entry').tag, 'details');
const old = context.load('old');
await context.load('current');
resolveOld({report_title: 'Stale report'});
await old;
assert.equal(els.registryReportTitle.textContent, 'Report');
console.log('PDF pagination, numbering, meeting fields, source order and stale requests passed');
