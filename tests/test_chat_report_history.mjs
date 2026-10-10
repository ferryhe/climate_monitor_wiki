import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8');
const functionSource = source.match(/function messageToApi\(item\) \{[\s\S]*?\n\}/)?.[0];
assert.ok(functionSource);
const messageToApi = vm.runInNewContext(`${functionSource}\nmessageToApi`);
const fullReport = 'PDF report row\n'.repeat(3000);
const item = { role: 'assistant', content: fullReport };
assert.equal(messageToApi(item).content, fullReport.slice(0, 8000));
assert.equal(item.content, fullReport);
assert.equal(messageToApi({ role: 'user', content: fullReport }).content, fullReport);
assert.equal(messageToApi({ role: 'assistant', content: 'Short answer' }).content, 'Short answer');
assert.equal(messageToApi({ role: 'assistant', content: fullReport, context: 'opaque-frame' }).context, 'opaque-frame');
assert.equal(messageToApi({ role: 'assistant', content: 'legacy' }).context, null);
const starters = JSON.parse(source.match(/const DEFAULT_PROMPT_STARTERS = ([\s\S]*?);\n\nconst GRAPH_COLORS/)?.[1]);
assert.equal(starters.length, 7);
assert.equal(starters[0].label, 'Generate PDF');
const plugin = readFileSync(new URL('../.obsidian/plugins/climate-agent-chat/main.js', import.meta.url), 'utf8');
assert.ok(plugin.includes('context: message.context || null'));
assert.ok(plugin.includes('context: payload.context || null'));
assert.ok(plugin.includes('window.open(source.url'));
const safeSourceUrl = vm.runInNewContext(`${source.match(/function safeSourceUrl\(value\) \{[\s\S]*?\n\}/)?.[0]}\nsafeSourceUrl`, { URL });
const clickSource = source.match(/    const sourceCard = target.closest\("\.source-card"\);[\s\S]*?(?=\n    const wikiLink)/)?.[0];
assert.ok(clickSource);
const opened = [], contexts = [];
function clickCard(dataset) {
  vm.runInNewContext(`(function () {${clickSource}})()`, {
    target: { closest: () => ({ dataset }) },
    window: { open: (...args) => opened.push(args) },
    setActiveContext: (...args) => contexts.push(args), safeSourceUrl,
  });
}
clickCard({ path: '', url: 'https://example.org/official?topic=insurance' });
assert.deepEqual(opened[0], ['https://example.org/official?topic=insurance', '_blank', 'noopener']);
clickCard({ path: 'wiki/risk.md', url: 'https://example.org/official' });
assert.equal(contexts[0][0], 'wiki/risk.md');
assert.equal(opened.length, 1);
clickCard({ path: '', url: 'javascript:alert(1)' });
assert.equal(opened.length, 1);
const renderSourceCards = vm.runInNewContext(`${source.match(/function renderSourceCards\(sources\) \{[\s\S]*?\n\}/)?.[0]}\nrenderSourceCards`, {
  escapeHtml: String, safeSourceUrl,
});
for (const type of ['web', 'meeting']) {
  const rendered = renderSourceCards([{ index: 1, type, path: '', url: 'https://example.org/official', title: 'Actual source' }]);
  assert.ok(rendered.includes('data-url="https://example.org/official"'));
}
console.log('PASS: bounded report history, full displayed report, user validation, short answers');
