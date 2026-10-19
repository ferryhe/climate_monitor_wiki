import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../showcase/index.html', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../showcase/styles.css', import.meta.url), 'utf8');
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
assert.equal(ids.length, new Set(ids).size);
assert.match(html, /id="chatTab"[^>]*data-view="chatView"[^>]*aria-selected="true">Chat<\/button>[\s\S]*?id="registryTab"[\s\S]*?id="meetingsTab"[\s\S]*?>Dates<\/button>/);
assert.match(html, /id="chatView"[^>]*aria-labelledby="chatTab"(?![^>]*\shidden)/);
assert.match(html, /id="registryView"[\s\S]*?aria-labelledby="registryTab"[\s\S]*?hidden/);
assert.match(html, /id="meetingsTab"[\s\S]*?data-view="meetingsView"[\s\S]*?aria-controls="meetingsView"/);
assert.match(html, /id="meetingsView"[^>]*aria-labelledby="meetingsTab"/);
assert.match(html, /<h2>Dates<\/h2>/);
assert.doesNotMatch(html, /Browse upcoming meetings and key deadlines/);
assert.doesNotMatch(html, /Minimal Chat Surface|Ask about climate risk and insurance/);
assert.match(html, /id="registrySearchForm"[\s\S]*?>Search<\/button>/);
assert.match(html, /id="registryMeetingSearchForm"[\s\S]*?>Search<\/button>/);
assert.match(html, /<label class="sr-only" for="registryPublisherFilter">Publisher<\/label>[\s\S]*?<select id="registryPublisherFilter"/);
assert.match(html, /<label class="sr-only" for="registryMeetingVerification">Verification<\/label>[\s\S]*?<select id="registryMeetingVerification"/);
assert.match(html, /<label class="sr-only" for="registryMeetingDateRange">Date range<\/label>[\s\S]*?<select id="registryMeetingDateRange"[\s\S]*?<option value="upcoming" selected>Upcoming<\/option>[\s\S]*?<option value="all">All dates<\/option>/);
assert.match(styles, /select\.search-input option\s*\{[^}]*background: var\(--bg-elevated\);[^}]*color: var\(--ink\);/);
assert.match(styles, /\.search-input:focus[\s\S]*?outline: 2px solid/);
assert.doesNotMatch(html, /data-registry-mode="meetings"/);
assert.match(source, /if \(window\.location\.hash === "#meetings"\) \{\s+beginEvidenceNavigation\(\);\s+setWorkspaceView\("meetingsView"\);/);
assert.match(source, /sourceTarget\?\.kind === "meeting" \|\| window\.location\.hash === "#meetings"/,
  'startup routing supports exact meeting evidence links and preserves the general #meetings view');
assert.match(source, /activeView: "chatView"/);
assert.doesNotMatch(source, /Start with a task, not just a topic|Switch to the Obsidian tab whenever/);
assert.match(source, /data-answer-mode="\$\{escapeHtml\(starter\.answer_mode/);
assert.match(source, /setAnswerMode\(button\.getAttribute\("data-answer-mode"\)/);

const state = { registry: { loaded: false, meetingPage: 3, meetingRequestSequence: 0, articlePage: 1, articleRequestSequence: 0, listPages: new Map(), listVersion: null, listFetchSequence: 0, listVersionSequence: 0 } };
const tabs = ['registryView', 'meetingsView', 'chatView', 'obsidianView'].map((view) => ({
  dataset: { view }, classList: { toggle() {} }, setAttribute(name, value) { this[name] = value; },
}));
const els = Object.fromEntries(tabs.map((tab) => [tab.dataset.view, { hidden: false }]));
Object.assign(els, { workspaceTabs: tabs, registryMeetingSearch: { value: '' }, registryMeetingVerification: { value: '' },
  registryMeetingTitle: {},
  registryMeetingCounts: {}, registryMeetings: { replaceChildren() {}, append() {} },
  registryArticles: { replaceChildren() {} }, registrySearch: { value: '' }, registryPublisherFilter: { value: '' },
  registryPublisherCustom: { value: '' } });
const calls = [];
let evidenceNavigationCount = 0;
const context = vm.createContext({ state, els, URL, URLSearchParams,
  loadRegistry: () => calls.push('registry'), stopGraphAnimation() {}, renderCurrentGraph() {},
  handleGraphKeydown() {},
  beginEvidenceNavigation: () => { evidenceNavigationCount += 1; },
  renderRegistryNotice() {}, updateRegistryPagination() {}, registryErrorMessage() {},
  registryFetch: async (url) => {
    calls.push(url);
    return { items: [], pagination: { total: 0 }, base_date: '2026-10-04', verification_counts: {} };
  },
});
for (const name of ['clearRegistryPageCache', 'registryPageFetch', 'prefetchRegistryPage', 'closeNavigation', 'setWorkspaceView',
  'safeSourceUrl', 'reviewedCheckedSourceUrl', 'reviewedSourceRoles', 'meetingDateGroup', 'meetingInstitution', 'registryDateEndBound', 'loadRegistryMeetings', 'loadRegistryArticles']) {
  const fn = source.match(new RegExp(`(?:async )?function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`))?.[0];
  assert.ok(fn, name);
  vm.runInContext(fn, context);
}
context.setWorkspaceView('meetingsView');
await new Promise((resolve) => setImmediate(resolve));
assert.equal(state.registry.meetingPage, 1);
assert.equal(state.activeView, 'meetingsView');
assert.equal(els.meetingsView.hidden, false);
assert.equal(els.registryView.hidden, true);
assert.equal(tabs[1]['aria-selected'], 'true');
assert.equal(tabs[0]['aria-selected'], 'false');
assert.deepEqual(calls, ['/api/registry/meetings?page=1&page_size=20']);
assert.match(els.registryMeetingCounts.textContent, /As of 2026-10-04 \(UTC\)/);

const created = [];
const document = { createElement(tag) {
  const element = { tag, children: [], dataset: {}, append(...items) { this.children.push(...items); } };
  created.push(element);
  return element;
} };
context.document = document;
for (const name of ['registryElement', 'meetingLocationSummary', 'appendMeetingLocations', 'appendInformationCheck']) {
  const fn = source.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`))?.[0];
  assert.ok(fn, name);
  vm.runInContext(fn, context);
}
const websiteCandidate = { location: 'Website location', name: 'Climate Conference', start_date: '2026-10-27' };
const conflict = { location: 'Website location', verification_status: 'verified', source_kind: 'pdf',
  collected_candidate: websiteCandidate,
  checks: [
    { source_url: 'https://example.org/old', verification_status: 'partial', website_candidate: { location: 'Stale' } },
    { source_url: 'https://example.org/current', verification_status: 'verified', website_candidate: websiteCandidate },
  ],
  pdf_observations: [{ location: 'PDF location', source_filename: 'events.pdf', page: 7 }] };
assert.equal(context.meetingLocationSummary(conflict), 'Website location');
assert.equal(context.meetingLocationSummary({ name: 'Conference in Hong Kong' }), 'Location not provided');
const details = { children: [], append(...items) { this.children.push(...items); } };
context.appendMeetingLocations(details, conflict);
assert.deepEqual(details.children.filter((element) => element.tag === 'dd').map((element) => element.textContent),
  ['PDF location\nPDF · events.pdf · page 7', 'Website location\nWebsite · https://example.org/current']);
const checkDetails = { children: [], append(...items) { this.children.push(...items); } };
context.appendInformationCheck(checkDetails, conflict);
assert.match(checkDetails.children[0].textContent, /Verified \/ collected/);
assert.equal(conflict.verification_status, 'verified');
const conciseDetails = { children: [], append(...items) { this.children.push(...items); } };
context.appendInformationCheck(conciseDetails, conflict, true);
assert.equal(conciseDetails.children.length, 1);
assert.doesNotMatch(conciseDetails.children[0].textContent, /Field checks and evidence|https:\/\/example\.org\/current/);
const publishedDetails = { children: [], append(...items) { this.children.push(...items); } };
context.appendMeetingLocations(publishedDetails, { ...conflict, checks: [],
  collected_candidate_source_url: 'https://example.org/current' });
assert.match(publishedDetails.children.at(-1).textContent, /Website · https:\/\/example\.org\/current/);
context.setWorkspaceView('chatView');
assert.equal(els.meetingsView.hidden, true);
assert.equal(els.chatView.hidden, false);
context.setWorkspaceView('registryView');
assert.equal(els.registryView.hidden, false);
assert.equal(calls.at(-1), 'registry');
els.registryMeetingSearch.value = 'COP31';
els.registryMeetingVerification.value = 'unchecked';
await context.loadRegistryMeetings();
assert.match(calls.at(-1), /query=COP31&base_date=1900-01-01&verification_status=unchecked$/);

const input = (value = '') => ({ value, listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; } });
const form = input();
const verification = input('');
const dateRange = input('upcoming');
const publisher = input('');
const custom = input('old.example');
const jumpToReports = input();
Object.assign(els, {
  registryMeetingSearchForm: form,
  registryMeetingVerification: verification,
  registryMeetingDateRange: dateRange,
  registrySearchForm: input(),
  registryPublisherFilter: publisher,
  registryPublisherCustom: custom,
  registrySearch: input(),
  registryArticles: { replaceChildren() {} },
  meetingsPrevious: null, meetingsNext: null, form: null,
  registryModeButtons: [], answerModeButtons: [], graphModeButtons: [], jumpToReports,
  reportsPrevious: null, reportsNext: null, articlesPrevious: null, articlesNext: null,
  input: null, wikiSearch: null, useInChat: null, clearChat: null,
  clearContext: null, clearSelection: null,
});
const paginationUpdates = [];
const pending = [];
context.updateRegistryPagination = (name, pagination) => paginationUpdates.push([name, pagination?.tag]);
context.registryFetch = (url) => new Promise((resolve) => pending.push({ resolve, url }));
context.registryElement = (tag, className, textContent) => ({ tag, className, textContent, append() {} });
context.document = { ...document, addEventListener() {}, querySelectorAll() { return []; } };
context.Element = class {};
context.window = { addEventListener() {}, location: { hash: '' } };
context.attachSourceCardClickHandler = () => {};
for (const name of ['attachEvents']) {
  const fn = source.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`))?.[0];
  assert.ok(fn, name);
  vm.runInContext(fn, context);
}
context.attachEvents();
context.setWorkspaceView('chatView');
jumpToReports.listeners.click();
assert.equal(evidenceNavigationCount, 1, 'opening Historical Reports starts evidence navigation');
assert.equal(state.activeView, 'registryView', 'the reports action opens the Reports workspace');
assert.equal(els.registryView.hidden, false);
state.registry.meetingPage = 4;
verification.value = 'verified';
verification.listeners.change();
assert.equal(state.registry.meetingPage, 1);
verification.value = 'partial';
verification.listeners.change();
assert.equal(pending.length, 2);
assert.match(pending[0].url, /page=1&page_size=20&query=COP31&base_date=1900-01-01&verification_status=verified/);
assert.match(pending[1].url, /page=1&page_size=20&query=COP31&base_date=1900-01-01&verification_status=partial/);
pending[1].resolve({ items: [], pagination: { tag: 'latest', page: 1, pages: 1, total: 0 }, base_date: '2026-10-04', verification_counts: {} });
await new Promise((resolve) => setImmediate(resolve));
pending[0].resolve({ items: [], pagination: { tag: 'stale', page: 1, pages: 1, total: 0 }, base_date: '2026-10-04', verification_counts: {} });
await new Promise((resolve) => setImmediate(resolve));
assert.deepEqual(paginationUpdates.filter(([name]) => name === 'meetings'), [['meetings', 'latest']]);
state.registry.articlePage = 5;
const articleRequestStart = pending.length;
publisher.value = 'first.example';
publisher.listeners.change();
assert.equal(state.registry.articlePage, 1);
assert.equal(custom.value, '');
publisher.value = 'second.example';
publisher.listeners.change();
assert.equal(pending.length - articleRequestStart, 2);
assert.match(pending[articleRequestStart].url, /page=1&page_size=20&include_pdf=true&source=first\.example/);
assert.match(pending[articleRequestStart + 1].url, /page=1&page_size=20&include_pdf=true&source=second\.example/);
pending[articleRequestStart + 1].resolve({ items: [], pagination: { tag: 'latest', page: 1, pages: 1, total: 0 } });
await new Promise((resolve) => setImmediate(resolve));
pending[articleRequestStart].resolve({ items: [], pagination: { tag: 'stale', page: 1, pages: 1, total: 0 } });
await new Promise((resolve) => setImmediate(resolve));
assert.deepEqual(paginationUpdates.filter(([name]) => name === 'articles'), [['articles', 'latest']]);
state.registry.meetingPage = 3;
form.listeners.submit({ preventDefault() {} });
assert.equal(state.registry.meetingPage, 1);
assert.match(pending.at(-1).url, /page=1&page_size=20&query=COP31&base_date=1900-01-01&verification_status=partial/);
pending.at(-1).resolve({ items: [], pagination: { tag: 'search', page: 1, pages: 1, total: 0 }, base_date: '2026-10-04', verification_counts: {} });
state.registry.articlePage = 3;
els.registrySearch.value = 'flood risk';
els.registryPublisherFilter.value = 'publisher.example';
els.registryPublisherCustom.value = '';
els.registrySearchForm.listeners.submit({ preventDefault() {} });
assert.equal(state.registry.articlePage, 1);
assert.match(pending.at(-1).url, /page=1&page_size=20&include_pdf=true&query=flood\+risk&source=publisher\.example/);
assert.match(source, /registrySearchForm\.addEventListener\("submit"/);
assert.match(source, /registryMeetingSearchForm\?\.addEventListener\("submit"/);
els.registryMeetings = { children: [], replaceChildren(...items) { this.children = [...items]; }, append(...items) { this.children.push(...items); } };
const checked = 'https://example.org/events/current', original = 'https://example.org/events/landing';
const receipt = { verification_status: 'verified', review_id: 'review-1', reviewer: { id: 'reviewer' },
  source_url: original, final_url: checked };
context.registryFetch = async () => ({ items: [{ event_id: 'native-1', name: 'World Climate Summit', origin: 'web_collection',
  verification_status: 'verified', access_status: 'accessible', start_date: '2027-06-10', date_precision: 'day',
  checked_source_url: checked, original_source_url: original, source_urls: [checked],
  sources: [{ source_url: original }], owner_agent_verification: receipt }],
  pagination: { total: 1, pages: 1 }, base_date: '2026-10-04', verification_counts: { verified: 1 } });
context.registryElement = (tag, className = '', textContent = '') => {
  const element = document.createElement(tag);
  element.className = className;
  if (textContent !== '') element.textContent = textContent;
  return element;
};
context.registryFetch = async url => {
  assert.match(url, /api\/registry\/meetings/);
  return { items: [{ event_id: 'native-1', name: 'World Climate Summit', origin: 'web_collection', status: 'scheduled',
    verification_status: 'verified', access_status: 'accessible', start_date: '2027-06-10', date_precision: 'day', deadline_date: '2000-01-01',
    checked_source_url: checked, original_source_url: original, source_urls: [checked],
    sources: [{ source_url: original }], owner_agent_verification: receipt }],
    pagination: { total: 1, pages: 1 }, base_date: '2026-10-04', verification_counts: { verified: 1 } };
};
await context.loadRegistryMeetings();
const meetingValues = [];
const collectDetails = node => { if (node.tag === 'dd') meetingValues.push(node.textContent); (node.children || []).forEach(collectDetails); };
collectDetails(els.registryMeetings);
assert.ok(meetingValues.includes('scheduled'), 'a future event remains scheduled after its registration deadline');
assert.ok(!meetingValues.includes('Past'));
const links = [];
const collectLinks = node => { if (node.tag === 'a') links.push(node); (node.children || []).forEach(collectLinks); };
collectLinks(els.registryMeetings);
assert.deepEqual(links.map(link => [link.textContent, link.href]), [
  ['Open checked source', checked], ['Original source link (provenance)', original],
]);
assert.match(source, /function registryDateEndBound\(date, precision\)/,
  'month and year dates have a calendar-period end bound');
context.registryFetch = async () => ({ items: [
  { event_id: 'past-month', name: 'Past month event', status: 'scheduled', start_date: '2000-01-15', date_precision: 'month' },
  { event_id: 'past-year', name: 'Past year event', status: 'scheduled', start_date: '2000', date_precision: 'year' },
  { event_id: 'future-month', name: 'Future month event', status: 'scheduled', start_date: '2099-12', date_precision: 'month' },
], pagination: { total: 3, pages: 1 }, base_date: '2026-10-04', verification_counts: {} });
await context.loadRegistryMeetings();
meetingValues.length = 0;
collectDetails(els.registryMeetings);
assert.equal(meetingValues.filter(value => value === 'Past').length, 2,
  'completed month- and year-precision dates are labeled Past');
assert.equal(meetingValues.filter(value => value === 'scheduled').length, 1,
  'a future month-precision event remains scheduled');
const rangeRequests = [];
const rangeNotices = [];
context.registryFetch = async url => {
  rangeRequests.push(new URL(url, 'https://local.example').searchParams);
  return { items: [], pagination: { total: 0, page: 1, pages: 1 }, base_date: '2026-10-04', verification_counts: {} };
};
context.renderRegistryNotice = (_container, message) => rangeNotices.push(message);
els.registryMeetingSearch.value = '';
verification.value = '';
state.registry.meetingPage = 4;
dateRange.value = 'all';
dateRange.listeners.change();
await new Promise(resolve => setImmediate(resolve));
assert.equal(state.registry.meetingPage, 1, 'changing date range starts on page one');
assert.equal(rangeRequests.at(-1).get('base_date'), '1900-01-01');
assert.equal(rangeRequests.at(-1).has('include_unknown'), false);
assert.equal(els.registryMeetingTitle.textContent, 'All dates');
assert.match(els.registryMeetingCounts.textContent, /^All date records · 0/);
assert.equal(rangeNotices.at(-1), 'No date records match these filters.');
dateRange.value = 'upcoming';
dateRange.listeners.change();
await new Promise(resolve => setImmediate(resolve));
assert.equal(rangeRequests.at(-1).has('base_date'), false, 'Upcoming restores the server default date');
assert.equal(els.registryMeetingTitle.textContent, 'Upcoming meetings');
assert.match(els.registryMeetingCounts.textContent, /current \/ upcoming/);
assert.equal(rangeNotices.at(-1), 'No current meetings match these filters.');
els.registryMeetingSearch.value = 'past conference';
await context.loadRegistryMeetings();
assert.equal(rangeRequests.at(-1).get('base_date'), '1900-01-01', 'search still includes historical evidence in Upcoming mode');
assert.equal(els.registryMeetingTitle.textContent, 'Matching dates');
assert.match(els.registryMeetingCounts.textContent, /^Matching date records/);
console.log('PASS: default Chat, Dates range toggle and copy, concise status, immediate page-one filters, stale response guard and search forms');
