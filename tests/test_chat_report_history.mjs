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
console.log('PASS: bounded report history, full displayed report, user validation, short answers');
