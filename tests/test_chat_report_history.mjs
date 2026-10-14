import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8').replaceAll('\r\n', '\n');
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
const functionNames = ['escapeHtml', 'inlineFmt', 'safeSourceUrl', 'sourceCitationHref', 'renderSourceCards'];
const helpers = functionNames.map(name => {
  const match = source.match(new RegExp(`function ${name}\\([^]*?\\n\\}`));
  assert.ok(match, name);
  return match[0];
}).join('\n');
const ui = vm.runInNewContext(`${helpers}\n({ inlineFmt, renderSourceCards })`, { URL });
const inline = ui.inlineFmt('Claim [1], then grouped [1, 2].');
const renderedCitations = [...inline.matchAll(/<button class="citation"[^>]*data-citation-index="(\d+)"/g)];
assert.deepEqual(renderedCitations.map(match => match[1]), ['1', '1', '2']);
const renderSourceCards = ui.renderSourceCards;
const cards = renderSourceCards([
  { index: 1, path: 'wiki/article-123.md', url: 'wiki/article-123.md', canonical_url: 'https://publisher.example/article', title: 'article-123', document_title: 'Real article title', corpus: 'wiki', matched_section: 'Verified article content', snippet: 'Exact cited passage.' },
  { index: 2, path: 'wiki/weekly.md', url: 'https://github.example/wiki/weekly.md', source_urls: ['https://unrelated.example'], title: 'weekly', document_title: 'Weekly evidence', corpus: 'wiki', matched_section: 'Second risk', snippet: 'Weekly passage.' },
  { index: 3, path: 'sources/report.md', url: 'https://github.example/sources/report.md', title: 'report', document_title: 'Weekly report', corpus: 'source', matched_section: 'Summary', snippet: 'Report passage.' },
  ...['web', 'meeting'].map((type, index) => ({ index: index + 4, type, path: '', url: `https://example.org/${type}`, title: `Actual ${type} source`, corpus: type, matched_section: 'Official page', snippet: `${type} passage.` })),
]);
assert.ok(cards.includes('href="https://publisher.example/article"'));
assert.ok(cards.includes('href="/wiki/weekly.md"'));
assert.ok(cards.includes('href="/sources/report.md"'));
assert.ok(cards.includes('href="https://example.org/web"'));
assert.ok(cards.includes('href="https://example.org/meeting"'));
assert.equal((cards.match(/target="_blank"/g) || []).length, 5);
assert.ok(cards.includes('Real article title'));
assert.ok(!cards.includes('>article-123</span>'));
assert.ok(cards.includes('WIKI · Verified article content'));
assert.ok(cards.includes('Exact cited passage.'));

const citationBranch = source.match(/    const citation = target\.closest\("\[data-citation-index\]"\);[\s\S]*?(?=\n\n    const tab)/)?.[0];
assert.ok(citationBranch);
const citationHandler = vm.runInNewContext(`(function (event) { const target = event.target; ${citationBranch} })`, { Element: class Element {} });
function citationRow(indices) {
  const cards = indices.map(index => ({ dataset: { index: String(index) }, focused: false, scrolled: false,
    focus(options) { this.focused = options.preventScroll; },
    scrollIntoView(options) { this.scrolled = options.block === 'nearest'; },
  }));
  const sources = { open: false, querySelectorAll: () => cards };
  return { cards, sources, querySelector: () => sources };
}
const rowOne = citationRow([1, 2]), rowTwo = citationRow([1]);
function clickCitation(row, renderedButton) {
  const index = renderedButton[1];
  const citation = { dataset: { citationIndex: String(index) }, closest(selector) { return selector === '.message-row' ? row : null; } };
  citationHandler({ target: { closest(selector) { return selector === '[data-citation-index]' ? citation : null; } } });
}
clickCitation(rowOne, renderedCitations[2]);
assert.deepEqual(rowOne.cards.map(card => [card.focused, card.scrolled]), [[false, false], [true, true]]);
assert.equal(rowOne.sources.open, true);
assert.equal(rowTwo.sources.open, false);
clickCitation(rowTwo, renderedCitations[0]);
assert.equal(rowTwo.cards[0].focused, true);
assert.doesNotThrow(() => clickCitation(rowTwo, ['<button data-citation-index="9">', '9']));
console.log('PASS: bounded report history, full displayed report, user validation, short answers');
