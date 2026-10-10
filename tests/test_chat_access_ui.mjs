import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8');
const names = ['loadThread', 'blockedChatMessage', 'renderChatAccess', 'refreshChatAccess', 'continueWithToken', 'sendMessage', 'setSending', 'appendMessage', 'replacePendingAssistant', 'messageToApi'];
const functions = names.map(name => {
  const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`));
  assert.ok(match, name);
  return match[0];
}).join('\n');
const state = {messages: [{role: 'assistant', content: 'Previous answer', context: 'frame-1'}], activeContextPath: 'wiki/example.md', answerMode: 'brief', isSending: false};
const button = {disabled: false};
const els = {send: {}, form: {setAttribute() {}}, input: {focus() {}}, chatAllowance: {}, chatAccessMessage: {}, retryChatQuestion: {}, chatToken: {value: ''}, chatTokenForm: {hidden: false, querySelector: () => button}};
const quota = {remaining: 0, limit: 5, reset_at: '2026-10-11T00:00:00-04:00', access_enabled: false};
let tokenEnabled = false;
let accessRequests = 0;
let holdGeneration = false;
let finishOriginalGeneration;
let saved;
const requests = [];
const fetch = async (url, options = {}) => {
  const body = options.body ? JSON.parse(options.body) : null;
  if (url === '/api/chat/access') {
    if (options.method === 'POST') {
      accessRequests += 1;
      if (body.token === 'malformed') return {ok: false, status: 422, json: async () => ({detail: [{loc: ['body', 'token'], msg: 'Invalid token'}]})};
      if (body.token !== 'valid') return {ok: false, status: 401, json: async () => ({detail: 'This token is invalid or has been revoked.'})};
      tokenEnabled = true;
    }
    return {ok: true, json: async () => ({...quota, access_enabled: tokenEnabled})};
  }
  requests.push(body);
  if (tokenEnabled && holdGeneration) return new Promise(resolve => { finishOriginalGeneration = resolve; });
  return tokenEnabled
    ? {ok: true, json: async () => ({text: 'Resumed answer', sources: [], agent_mode: 'offline', answer_mode: body.answerMode})}
    : {ok: false, status: 429, json: async () => ({detail: {code: 'chat_quota_exhausted', ...quota}})};
};
const context = vm.createContext({state, els, fetch, Intl, Date, console,
  renderMessages() {}, saveThread() {saved = JSON.stringify(state.messages);}, setConnectionStatus() {}, setAnswerMode() {}});
vm.runInContext(functions, context);
await context.sendMessage('Keep this exact question');
await new Promise(setImmediate);
assert.equal(state.messages.filter(item => item.role === 'user').length, 1);
assert.equal(state.messages.filter(item => item.pending).length, 0);
assert.ok(state.messages.at(-1).blockedRequest);
assert.equal(els.send.disabled, true);
assert.equal(els.retryChatQuestion.hidden, true);
assert.match(els.chatAllowance.textContent, /^Free questions today: 0 of 5 remaining\./);
assert.match(els.chatAllowance.textContent, /New York/);
assert.match(els.chatAccessMessage.textContent, /^You've used today's 5 free questions for this network\. Enter an access token to continue, or return after .+\.$/);

state.messages = JSON.parse(saved); // Refresh preserves the blocked question and full request.
state.answerMode = 'executive';
state.activeContextPath = null;
const pendingRequest = JSON.stringify(state.messages.at(-1).blockedRequest);
els.chatToken.value = '   ';
await context.continueWithToken({preventDefault() {}});
assert.equal(els.chatAccessMessage.textContent, 'This token is invalid or has been revoked.');
assert.equal(JSON.stringify(state.messages.at(-1).blockedRequest), pendingRequest);
assert.equal(accessRequests, 0);
assert.equal(requests.length, 1);
assert.equal(button.disabled, false);
els.chatToken.value = 'malformed';
await context.continueWithToken({preventDefault() {}});
assert.equal(els.chatAccessMessage.textContent, 'This token is invalid or has been revoked.');
assert.equal(JSON.stringify(state.messages.at(-1).blockedRequest), pendingRequest);
assert.equal(requests.length, 1);
els.chatToken.value = 'invalid';
await context.continueWithToken({preventDefault() {}});
assert.equal(els.chatAccessMessage.textContent, 'This token is invalid or has been revoked.');
assert.ok(state.messages.at(-1).blockedRequest);
assert.equal(requests.length, 1);
assert.equal(button.disabled, false);

els.chatToken.value = 'valid';
await context.continueWithToken({preventDefault() {}});
await new Promise(setImmediate);
assert.deepEqual(requests[1], requests[0]);
assert.equal(requests[1].answerMode, 'brief');
assert.equal(requests[1].contextPath, 'wiki/example.md');
assert.equal(state.messages.filter(item => item.role === 'user').length, 1);
assert.equal(state.messages.at(-1).content, 'Resumed answer');
assert.equal(context.blockedChatMessage(), undefined);
assert.equal(els.chatAccessMessage.textContent, 'Access enabled. You can continue asking questions.');
assert.equal(els.chatToken.value, '');
assert.equal(els.send.disabled, false);
assert.equal(els.chatTokenForm.hidden, true);
state.messages.push({role: 'user', content: 'After midnight', blockedRequest: requests[0]});
context.renderChatAccess({...quota, remaining: 5});
assert.equal(els.retryChatQuestion.hidden, false);

// Refresh after access is enabled but before the automatic retry finishes.
state.messages = [];
tokenEnabled = false;
await context.sendMessage('Refresh during token retry');
await new Promise(setImmediate);
holdGeneration = true;
els.chatToken.value = 'valid';
const inFlight = context.continueWithToken({preventDefault() {}});
await new Promise(setImmediate);
assert.equal(tokenEnabled, true);
const persistedRace = JSON.parse(saved);
assert.equal(persistedRace.filter(item => item.pending).length, 1);
assert.ok(persistedRace.find(item => item.role === 'user').blockedRequest);
const originalRequest = requests.at(-1);
const reloadedState = {...state, messages: [], isSending: false};
const reloadedEls = {send: {}, form: {setAttribute() {}}, input: {focus() {}}, chatAllowance: {}, chatAccessMessage: {}, retryChatQuestion: {}, chatToken: {value: ''}, chatTokenForm: {querySelector: () => ({})}};
let reloadedSaved = saved;
const reloaded = vm.createContext({state: reloadedState, els: reloadedEls, fetch, Intl, Date, STORAGE_KEY: 'thread',
  localStorage: {getItem: () => reloadedSaved, removeItem() {}}, renderMessages() {},
  saveThread() {reloadedSaved = JSON.stringify(reloadedState.messages);}, setConnectionStatus() {}, setAnswerMode() {}});
vm.runInContext(functions, reloaded);
reloaded.loadThread();
reloaded.setSending(false);
await reloaded.refreshChatAccess();
assert.equal(reloadedState.messages.filter(item => item.pending).length, 0);
assert.equal(reloadedEls.send.disabled, true);
assert.equal(reloadedEls.chatTokenForm.hidden, true);
assert.equal(reloadedEls.retryChatQuestion.hidden, false);
assert.equal(reloadedEls.retryChatQuestion.disabled, false);
holdGeneration = false;
let retryClick;
reloadedEls.retryChatQuestion.addEventListener = (event, callback) => { assert.equal(event, 'click'); retryClick = callback; };
const retryHandler = source.match(/  els\.retryChatQuestion\?\.addEventListener\("click", \(\) => \{[\s\S]*?\n  \}\);/)?.[0];
assert.ok(retryHandler);
vm.runInContext(retryHandler, reloaded);
retryClick();
await new Promise(setImmediate);
assert.deepEqual(requests.at(-1), originalRequest);
assert.equal(reloadedState.messages.filter(item => item.role === 'user').length, 1);
assert.equal(reloadedState.messages.filter(item => item.pending).length, 0);
assert.equal(reloadedState.messages.filter(item => item.role === 'assistant').length, 1);
assert.equal(reloadedState.messages.at(-1).content, 'Resumed answer');
assert.equal(reloaded.blockedChatMessage(), undefined);
assert.equal(reloadedEls.retryChatQuestion.hidden, true);
finishOriginalGeneration({ok: true, json: async () => ({text: 'Original tab completed', sources: [], answer_mode: 'brief'})});
await inFlight;
const html = readFileSync(new URL('../showcase/index.html', import.meta.url), 'utf8');
assert.ok(html.includes('>Access token</label>'));
assert.ok(html.includes('>Continue with token</button>'));
console.log('PASS: blocked request persistence, invalid token retention, exact resume, established-token refresh recovery, single user history, allowance and required copy');
