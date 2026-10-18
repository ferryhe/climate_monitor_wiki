from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_registry_reports_render_before_metadata_and_startup_notice_is_transient():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")

    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('showcase/app.js', 'utf8');
const functionSource = (name) => {
  const match = source.match(new RegExp(`(?:async )?function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
  assert.ok(match, `missing ${name}`);
  return match[0];
};
const element = () => ({
  children: [], value: '', hidden: false, textContent: '', dataset: {}, classList: { toggle() {}, add() {} },
  append(...items) { this.children.push(...items); },
  replaceChildren(...items) { this.children = items; },
  setAttribute() {}, focus() {},
});

async function checkRegistryLoading() {
  const state = { registry: {
    loaded: false, available: false, mode: 'reports', reportPage: 1, articlePage: 1,
    reportPagination: null, articlePagination: null, reportListRequestSequence: 0,
    listPages: new Map(), listVersion: null, listFetchSequence: 0, listVersionSequence: 0, publishersLoaded: false,
    publishersPromise: null, loadPromise: null,
  } };
  const reports = element();
  const articles = element();
  const status = element();
  const publisherFilter = element();
  const publisherCustom = element();
  const calls = [];
  const pagination = [];
  let resolveStatus;
  let rejectPublishers;
  let resolvePublishers;
  let publisherCalls = 0;
  let articleCalls = 0;
  const statusPromise = new Promise((resolve) => { resolveStatus = resolve; });
  const publisherDeferred = () => new Promise((resolve, reject) => {
    resolvePublishers = resolve;
    rejectPublishers = reject;
  });
  const context = vm.createContext({
    state,
    els: {
      registryReports: reports, registryArticles: articles, registryStatus: status,
      registryPublisherFilter: publisherFilter, registryPublisherCustom: publisherCustom,
      registryReportsPanel: element(), registryArticlesPanel: element(), registryModeButtons: [],
    },
    document: { createElement: element },
    registryFetch(url) {
      calls.push(url);
      if (url.startsWith('/api/registry/reports')) return Promise.resolve({
        items: [
          { report_date: '2026-10-05', report_title: 'Weekly report', article_count: 8 },
          { source_kind: 'pdf', report_id: 'pdf-1', report_date: '2026-10-04', report_title: 'PDF report', filename: 'report.pdf', article_count: 3 },
        ],
        pagination: { total: 2, page: 1, pages: 1 },
      });
      if (url.startsWith('/api/registry/status')) return statusPromise;
      if (url.startsWith('/api/registry/publishers')) {
        publisherCalls += 1;
        return publisherDeferred();
      }
      throw new Error(`unexpected request: ${url}`);
    },
    updateRegistryPagination: (...args) => pagination.push(args),
    loadRegistryArticles: () => { articleCalls += 1; },
  });
  for (const name of ['registryElement', 'registryArticleDateSummary', 'safeSourceUrl', 'reviewedCheckedSourceUrl', 'reviewedSourceRoles',
    'registryErrorMessage', 'renderRegistryNotice',
    'clearRegistryPageCache', 'registryPageFetch', 'prefetchRegistryPage', 'loadRegistryReports', 'loadRegistryPublishers', 'loadRegistryOnce', 'setRegistryMode']) {
    vm.runInContext(functionSource(name), context);
  }

  const loading = context.loadRegistryOnce();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, ['/api/registry/reports?include_pdf=true&page=1&page_size=12', '/api/registry/status?include_pdf=true']);
  assert.equal(reports.children.length, 2, 'cards render while the status request is unresolved');
  assert.equal(reports.children[0].dataset.reportDate, '2026-10-05');
  assert.equal(reports.children[1].dataset.pdfReportId, 'pdf-1');
  assert.equal(status.textContent, '2 reports');
  assert.equal(publisherCalls, 0, 'reports mode does not fetch the publisher filter');

  context.setRegistryMode('articles');
  resolveStatus({ available: true, reports: 2, articles: 411 });
  await loading;
  assert.equal(status.textContent, '2 reports · 411 articles');
  assert.equal(publisherCalls, 1);
  assert.equal(articleCalls, 1);
  state.registry.reportPage = 2;
  await context.loadRegistryReports();
  assert.equal(status.textContent, '2 reports · 411 articles', 'report pagination keeps the article count');
  context.setRegistryMode('articles');
  context.setRegistryMode('articles');
  assert.equal(publisherCalls, 1, 'an in-flight publisher request is shared');

  rejectPublishers(new Error('temporary publisher failure'));
  await state.registry.publishersPromise?.catch(() => {});
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(state.registry.publishersLoaded, false);
  const retry = context.loadRegistryPublishers();
  assert.equal(publisherCalls, 2, 'a failed publisher request can be retried');
  resolvePublishers({ items: [{ hostname: 'example.org', label: 'Example' }], truncated: false });
  await retry;
  assert.equal(state.registry.publishersLoaded, true);
  assert.equal(publisherCalls, 2, 'a successful publisher list is reused');
}

async function checkReportFailureDoesNotBlockArticles() {
  const state = { registry: {
    loaded: false, available: false, mode: 'reports', reportPage: 1, articlePage: 1,
    reportPagination: null, articlePagination: null, articleRequestSequence: 0, reportListRequestSequence: 0,
    listPages: new Map(), listVersion: null, listFetchSequence: 0, listVersionSequence: 0,
    publishersLoaded: false, publishersPromise: null,
  } };
  const reports = element();
  const articles = element();
  const status = element();
  const publisherFilter = element();
  const publisherCustom = element();
  let reportCalls = 0;
  let statusCalls = 0;
  let articleCalls = 0;
  const context = vm.createContext({
    state,
    els: {
      registryReports: reports, registryArticles: articles, registryStatus: status,
      registryPublisherFilter: publisherFilter, registryPublisherCustom: publisherCustom,
      registryReportsPanel: element(), registryArticlesPanel: element(), registryModeButtons: [],
      registrySearch: { value: '' },
    },
    document: { createElement: element }, URL, URLSearchParams,
    registryFetch(url) {
      if (url.startsWith('/api/registry/reports') && ++reportCalls === 1) {
        const error = new Error('Registry request failed (500)');
        error.status = 500;
        throw error;
      }
      if (url.startsWith('/api/registry/reports')) return Promise.resolve({
        items: [{ report_date: '2026-10-05', report_title: 'Weekly report', article_count: 8 }],
        pagination: { total: 1, page: 1, pages: 1 },
      });
      if (url.startsWith('/api/registry/status')) {
        statusCalls += 1;
        return Promise.resolve({ available: true, reports: 1, articles: 411 });
      }
      if (url.startsWith('/api/registry/publishers')) return Promise.resolve({ items: [], truncated: false });
      if (url.startsWith('/api/registry/articles')) {
        articleCalls += 1;
        return Promise.resolve({
          items: [{ article_id: 'article-1', title: 'Visible article', publisher: 'Example' }],
          pagination: { total: 1, page: 1, pages: 1 },
        });
      }
      throw new Error(`unexpected request: ${url}`);
    },
    updateRegistryPagination() {},
  });
  for (const name of ['registryElement', 'registryArticleDateSummary', 'safeSourceUrl', 'reviewedCheckedSourceUrl', 'reviewedSourceRoles',
    'registryErrorMessage', 'renderRegistryNotice',
    'clearRegistryPageCache', 'registryPageFetch', 'prefetchRegistryPage', 'loadRegistryReports', 'loadRegistryPublishers', 'loadRegistryOnce', 'loadRegistryArticles', 'setRegistryMode']) {
    vm.runInContext(functionSource(name), context);
  }

  await context.loadRegistryOnce();
  assert.equal(state.registry.available, true, 'status readiness survives a report-list failure');
  assert.equal(state.registry.loaded, false, 'the failed report list remains eligible for retry');
  assert.match(reports.children[0].textContent, /could not be loaded/);
  context.setRegistryMode('articles');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(articleCalls, 1);
  assert.equal(articles.children[0].children[0].textContent, 'Visible article');

  await context.loadRegistryOnce();
  assert.equal(reportCalls, 2, 're-entering can retry the failed report list once');
  assert.equal(statusCalls, 2);
  assert.equal(articleCalls, 1, 'the successful article page is not fetched again on report retry');
  assert.equal(reports.children[0].dataset.reportDate, '2026-10-05');
}

async function checkStartupNoticePersistence() {
  const legacy = 'Could not reach the backend service: API 503\n\nStart it with `uvicorn api_server:app --host 0.0.0.0 --port 8501`.';
  const persisted = [
    { role: 'user', content: 'Keep my question.' },
    { role: 'assistant', content: 'Keep the cited answer.', sources: [{ title: 'Source' }] },
    { role: 'assistant', content: 'Request failed: API 503' },
    { role: 'assistant', content: legacy },
    { role: 'user', content: legacy },
  ];
  const storage = new Map([['history', JSON.stringify(persisted)]]);
  const state = { messages: [], activeContextPath: null, answerMode: 'detailed', isSending: false };
  const status = element();
  const graphHint = element();
  const context = vm.createContext({
    state, STORAGE_KEY: 'history',
    localStorage: {
      getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
    els: { status, graphHint, input: element(), hermesLink: null },
    window: { location: { hash: '' } },
    fetch: async (url) => url === '/api/manage/session' ? { ok: false } : { ok: true },
    setSending() {}, setAnswerMode() {}, setGraphMode() {}, setWorkspaceView() {},
    renderMessages() {}, attachEvents() {}, historicalReportDateFromHash: () => '',
    refreshChatAccess: async () => {}, sourceNavigationFromHash: () => null,
    loadConfig: async () => { throw new Error('API 503'); },
    setConnectionStatus() {},
  });
  for (const name of ['isLegacyStartupFailureNotice', 'loadThread', 'saveThread',
    'messageToApi', 'appendMessage', 'replacePendingAssistant', 'sendMessage', 'main']) {
    vm.runInContext(functionSource(name), context);
  }
  context.loadThread();
  assert.equal(state.messages.length, 4, 'only the recognizable old assistant startup notice is removed');
  assert.deepEqual(JSON.parse(storage.get('history')).map((item) => item.content),
    ['Keep my question.', 'Keep the cited answer.', 'Request failed: API 503', legacy]);

  await context.main();
  const notice = state.messages.at(-1);
  assert.equal(notice.transient, true);
  assert.equal(notice.content, 'The service could not be reached. Please refresh and try again.');
  assert.doesNotMatch(notice.content, /uvicorn|API 503/);
  assert.equal(JSON.parse(storage.get('history')).some((item) => item.content === notice.content), false);

  context.appendMessage('assistant', '', { pending: true });
  context.replacePendingAssistant(legacy, [], null, 'opaque-frame');
  state.messages = [];
  context.loadThread();
  const actualReply = state.messages.find((item) => item.role === 'assistant' && item.content === legacy);
  assert.equal(actualReply.context, 'opaque-frame', 'reply metadata prevents a quoted notice from being classified as startup UI');

  let request;
  context.fetch = async (url, options) => {
    if (url === '/api/chat') {
      request = JSON.parse(options.body);
      return { ok: true, json: async () => ({ text: 'A cited reply.', sources: [] }) };
    }
    return { ok: false };
  };
  await context.sendMessage('A follow-up question.');
  assert.deepEqual(request.messages.map((item) => item.content),
    ['Keep my question.', 'Keep the cited answer.', 'Request failed: API 503', legacy, legacy, 'A follow-up question.']);
  assert.equal(request.messages.find((item) => item.role === 'assistant' && item.content === legacy).context, 'opaque-frame');
  assert.equal(JSON.parse(storage.get('history')).some((item) => item.content === notice.content), false);
}

(async () => {
  await checkRegistryLoading();
  await checkReportFailureDoesNotBlockArticles();
  await checkStartupNoticePersistence();
  console.log('PASS: report-first UI, independent article readiness, publisher retry and transient startup notices');
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
