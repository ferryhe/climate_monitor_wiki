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
assert.match(html, /<h2>Meetings &amp; Key Dates<\/h2>[\s\S]*?Browse upcoming meetings and key deadlines\./);
assert.match(html, /Ask about climate risk and insurance\. Answers include sources\./);
assert.match(html, /id="registrySearchForm"[\s\S]*?>Search<\/button>/);
assert.match(html, /id="registryMeetingSearchForm"[\s\S]*?>Search<\/button>/);
assert.match(html, /<label class="sr-only" for="registryPublisherFilter">Publisher<\/label>[\s\S]*?<select id="registryPublisherFilter"/);
assert.match(html, /<label class="sr-only" for="registryMeetingVerification">Verification<\/label>[\s\S]*?<select id="registryMeetingVerification"/);
assert.match(styles, /select\.search-input option\s*\{[^}]*background: var\(--bg-elevated\);[^}]*color: var\(--ink\);/);
assert.match(styles, /\.search-input:focus[\s\S]*?outline: 2px solid/);
assert.doesNotMatch(html, /data-registry-mode="meetings"/);
assert.match(source, /setWorkspaceView\(window\.location\.hash === "#meetings" \? "meetingsView" : state\.activeView\)/);
assert.match(source, /activeView: "chatView"/);
assert.doesNotMatch(source, /Start with a task, not just a topic|Switch to the Obsidian tab whenever/);
assert.match(source, /data-answer-mode="\$\{escapeHtml\(starter\.answer_mode/);
assert.match(source, /setAnswerMode\(button\.getAttribute\("data-answer-mode"\)/);

const state = { registry: { loaded: false, meetingPage: 3, meetingRequestSequence: 0, articlePage: 1, articleRequestSequence: 0 } };
const tabs = ['registryView', 'meetingsView', 'chatView', 'obsidianView'].map((view) => ({
  dataset: { view }, classList: { toggle() {} }, setAttribute(name, value) { this[name] = value; },
}));
const els = Object.fromEntries(tabs.map((tab) => [tab.dataset.view, { hidden: false }]));
Object.assign(els, { workspaceTabs: tabs, registryMeetingSearch: { value: '' }, registryMeetingVerification: { value: '' },
  registryMeetingCounts: {}, registryMeetings: { replaceChildren() {}, append() {} },
  registryArticles: { replaceChildren() {} }, registrySearch: { value: '' }, registryPublisherFilter: { value: '' },
  registryPublisherCustom: { value: '' } });
const calls = [];
const context = vm.createContext({ state, els, URLSearchParams,
  loadRegistry: () => calls.push('registry'), stopGraphAnimation() {}, renderCurrentGraph() {},
  renderRegistryNotice() {}, updateRegistryPagination() {}, registryErrorMessage() {},
  registryFetch: async (url) => {
    calls.push(url);
    return { items: [], pagination: { total: 0 }, base_date: '2026-10-04', verification_counts: {} };
  },
});
for (const name of ['setWorkspaceView', 'loadRegistryMeetings', 'loadRegistryArticles']) {
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
  const element = { tag, children: [], append(...items) { this.children.push(...items); } };
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
assert.match(calls.at(-1), /query=COP31&verification_status=unchecked$/);
assert.doesNotMatch(calls.at(-1), /base_date/);

const input = (value = '') => ({ value, listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; } });
const form = input();
const verification = input('');
const publisher = input('');
const custom = input('old.example');
Object.assign(els, {
  registryMeetingSearchForm: form,
  registryMeetingVerification: verification,
  registrySearchForm: input(),
  registryPublisherFilter: publisher,
  registryPublisherCustom: custom,
  registrySearch: input(),
  registryArticles: { replaceChildren() {} },
  meetingsPrevious: null, meetingsNext: null, form: null,
  registryModeButtons: [], answerModeButtons: [], graphModeButtons: [],
  reportsPrevious: null, reportsNext: null, articlesPrevious: null, articlesNext: null,
  input: null, wikiSearch: null, jumpToReports: null, useInChat: null, clearChat: null,
  clearContext: null, clearSelection: null,
});
const paginationUpdates = [];
const pending = [];
context.updateRegistryPagination = (name, pagination) => paginationUpdates.push([name, pagination?.tag]);
context.registryFetch = (url) => new Promise((resolve) => pending.push({ resolve, url }));
context.registryElement = (tag, className, textContent) => ({ tag, className, textContent, append() {} });
context.document = { ...document, addEventListener() {} };
context.Element = class {};
context.window = { addEventListener() {}, location: { hash: '' } };
for (const name of ['attachEvents']) {
  const fn = source.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`))?.[0];
  assert.ok(fn, name);
  vm.runInContext(fn, context);
}
context.attachEvents();
state.registry.meetingPage = 4;
verification.value = 'verified';
verification.listeners.change();
assert.equal(state.registry.meetingPage, 1);
verification.value = 'partial';
verification.listeners.change();
assert.equal(pending.length, 2);
assert.match(pending[0].url, /page=1&page_size=20&query=COP31&verification_status=verified/);
assert.match(pending[1].url, /page=1&page_size=20&query=COP31&verification_status=partial/);
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
assert.match(pending.at(-1).url, /page=1&page_size=20&query=COP31&verification_status=partial/);
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
console.log('PASS: default Chat, Dates copy, concise status, immediate page-one filters, stale response guard and search forms');
