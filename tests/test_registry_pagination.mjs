import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync('showcase/app.js', 'utf8');
const element = () => ({value: '', children: [], dataset: {},
  append(...items) { this.children.push(...items); },
  replaceChildren(...items) { this.children = items; }});
const state = {registry: {reportPage: 1, articlePage: 1, reportListRequestSequence: 0,
  articleRequestSequence: 0, listPages: new Map(), listVersion: null, listFetchSequence: 0, listVersionSequence: 0}};
const els = {registryReports: element(), registryArticles: element(), registrySearch: element(),
  registryPublisherFilter: element(), registryPublisherCustom: element()};
const requests = [];
const context = vm.createContext({state, els, URLSearchParams,
  registryElement: (tag, className, textContent) => Object.assign(element(), {textContent}),
  renderRegistryNotice: (target, text) => target.replaceChildren({textContent: text}),
  registryErrorMessage: error => error.message,
  updateRegistryPagination() {},
  registryFetch(path) {
    return new Promise((resolve, reject) => requests.push({path, resolve, reject}));
  },
});
for (const name of ['clearRegistryPageCache', 'registryPageFetch', 'prefetchRegistryPage', 'reviewedCheckedSourceUrl', 'reviewedSourceRoles',
  'loadRegistryReports', 'loadRegistryArticles']) {
  const body = source.match(new RegExp(`(?:async )?function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`))?.[0];
  assert.ok(body, name);
  vm.runInContext(body, context);
}
const payload = (page, pages = 3, version = 'v1', title = `Page ${page}`) => ({
  cache_version: version, items: [{article_id: `a${page}`, report_title: title, title,
    report_date: '2026-10-05', article_count: 2, publisher: 'Example'}],
  pagination: {page, pages, total: 3},
});
const flush = () => new Promise(resolve => setImmediate(resolve));

const first = context.loadRegistryReports();
assert.equal(requests.length, 1);
requests[0].resolve(payload(1));
await first;
assert.equal(els.registryReports.children[0].children[0].textContent, 'Page 1');
assert.equal(requests.length, 2, 'only the next page is prefetched after rendering');
assert.match(requests[1].path, /page=2&page_size=12/);
state.registry.reportPage = 2;
const second = context.loadRegistryReports();
assert.equal(requests.length, 2, 'navigation shares the in-flight prefetch');
requests[1].resolve(payload(2));
await flush();
assert.equal(requests[2].path, '/api/registry/status?include_pdf=true');
requests[2].resolve({available: true, cache_version: 'v2'});
await flush();
assert.match(requests[3].path, /page=2&page_size=12/);
requests[3].resolve(payload(2, 3, 'v2', 'Current approved page 2'));
await second;
assert.equal(els.registryReports.children[0].children[0].textContent, 'Current approved page 2',
  'a pending prefetch from an old public version cannot render after navigation');
assert.equal(requests.length, 5);
requests[4].resolve(payload(3, 3, 'v2'));
await flush();
state.registry.reportPage = 3;
const last = context.loadRegistryReports();
assert.equal(requests[5].path, '/api/registry/status?include_pdf=true');
requests[5].resolve({available: true, cache_version: 'v2'});
await last;
assert.equal(requests.length, 6, 'cached last page is reused after a version check, without prefetch');

const delayedNavigation = context.loadRegistryReports();
const currentArticlePath = '/api/registry/articles?page=1&page_size=20&include_pdf=true&query=current';
const currentArticles = context.registryPageFetch(currentArticlePath);
requests[7].resolve(payload(1, 1, 'v3', 'Current article generation'));
await currentArticles;
requests[6].resolve({available: true, cache_version: 'v2'});
await flush();
assert.equal(state.registry.listVersion, 'v3', 'an old cross-list status cannot reset a newer version');
assert.equal(state.registry.listPages.get(currentArticlePath).version, 'v3');
assert.match(requests[8].path, /registry\/reports.*page=3/);
requests[8].resolve(payload(3, 3, 'v3', 'Current report generation'));
await delayedNavigation;
assert.equal(els.registryReports.children[0].children[0].textContent, 'Current report generation',
  'a late status matching an obsolete entry cannot render that old entry');

const oldSearch = context.loadRegistryArticles();
els.registrySearch.value = 'new search';
state.registry.articlePage = 1;
const newSearch = context.loadRegistryArticles();
assert.match(requests[10].path, /query=new\+search/);
requests[10].resolve(payload(1, 1, 'v4', 'New result'));
await newSearch;
requests[9].resolve(payload(1, 2, 'v3', 'Old result'));
await oldSearch;
assert.equal(els.registryArticles.children[0].children[0].textContent, 'New result');
assert.equal(state.registry.listVersion, 'v4', 'old responses cannot reset the current cache version');
assert.equal(requests.length, 11, 'old search responses do not prefetch');

const invalidated = context.loadRegistryArticles();
requests[11].reject(Object.assign(new Error('Registry unavailable'), {status: 503}));
await invalidated;
assert.equal(state.registry.listPages.size, 0);
assert.equal(els.registryArticles.children[0].textContent, 'Registry unavailable');

console.log('PASS: immediate cards, next-page prefetch, in-flight reuse, version checks, filter races and invalidation');
