import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../showcase/index.html', import.meta.url), 'utf8');
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
assert.equal(ids.length, new Set(ids).size);
assert.match(html, /id="meetingsTab"[\s\S]*?data-view="meetingsView"[\s\S]*?aria-controls="meetingsView"/);
assert.match(html, /id="meetingsView"[^>]*aria-labelledby="meetingsTab"/);
assert.doesNotMatch(html, /data-registry-mode="meetings"/);
assert.match(source, /setWorkspaceView\(window\.location\.hash === "#meetings" \? "meetingsView" : state\.activeView\)/);

const state = { registry: { loaded: false, meetingPage: 3, meetingRequestSequence: 0 } };
const tabs = ['registryView', 'meetingsView', 'chatView', 'obsidianView'].map((view) => ({
  dataset: { view }, classList: { toggle() {} }, setAttribute(name, value) { this[name] = value; },
}));
const els = Object.fromEntries(tabs.map((tab) => [tab.dataset.view, { hidden: false }]));
Object.assign(els, { workspaceTabs: tabs, registryMeetingSearch: { value: '' }, registryMeetingVerification: { value: '' },
  registryMeetingCounts: {}, registryMeetings: { replaceChildren() {} } });
const calls = [];
const context = vm.createContext({ state, els, URLSearchParams,
  loadRegistry: () => calls.push('registry'), stopGraphAnimation() {}, renderCurrentGraph() {},
  renderRegistryNotice() {}, updateRegistryPagination() {}, registryErrorMessage() {},
  registryFetch: async (url) => {
    calls.push(url);
    return { items: [], pagination: { total: 0 }, base_date: '2026-10-04', verification_counts: {} };
  },
});
for (const name of ['setWorkspaceView', 'loadRegistryMeetings']) {
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
console.log('PASS: independent meetings tab, tab switching, today-based query, search, verification and unique IDs');
