import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../showcase/app.js', import.meta.url), 'utf8');
const styles = readFileSync(new URL('../showcase/styles.css', import.meta.url), 'utf8');
const expandedSelectionRule = styles.match(/\.graph-expanded-selection\s*\{([^}]*)\}/)?.[1] || '';
assert.match(expandedSelectionRule, /min-width:\s*0/, 'the selected-page status may shrink beside Close graph');
assert.match(expandedSelectionRule, /overflow-wrap:\s*anywhere/,
  'long source hosts wrap within the full-screen graph toolbar');
function functionSource(name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing ${name}`);
  const open = source.indexOf('{', start);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`unclosed ${name}`);
}

const documentClasses = new Set();
const viewportClasses = new Set();
function classList(classes) {
  return {
    toggle(name, force) {
      if (force) classes.add(name);
      else classes.delete(name);
      return Boolean(force);
    },
    contains(name) { return classes.has(name); },
  };
}
const document = {
  activeElement: {
    focusCount: 0,
    focus() { this.focusCount += 1; document.activeElement = this; },
  },
  documentElement: { classList: classList(documentClasses) },
};
const originalFocus = document.activeElement;
const expandButton = {
  attributes: {},
  textContent: 'Expand graph',
  setAttribute(name, value) { this.attributes[name] = value; },
  focus() { document.activeElement = this; },
};
const selection = { hidden: true, textContent: '' };
const state = {
  graphExpanded: false,
  graphReturnFocus: null,
  activeContextPath: 'wiki/diabetes.md',
  rows: [{ path: 'wiki/diabetes.md', displayTitle: 'Diabetes', type: 'topic' }],
};
let renders = 0;
const context = vm.createContext({
  document,
  state,
  els: {
    graphViewport: { classList: classList(viewportClasses) },
    graphExpandButton: expandButton,
    graphExpandedSelection: selection,
  },
  renderCurrentGraph() { renders += 1; },
});
for (const name of [
  'projectRadialPosition',
  'graphCanvasHeight',
  'graphNodePosition',
  'renderExpandedGraphSelection',
  'setGraphExpanded',
  'toggleGraphExpanded',
  'handleGraphKeydown',
]) {
  vm.runInContext(functionSource(name), context);
}

const heights = vm.runInContext(`({
  compact: graphCanvasHeight(500),
  fullScreen: graphCanvasHeight(920),
  fallback: graphCanvasHeight(0),
  category: graphCanvasHeight(920, 1240),
})`, context);
assert.deepEqual({ ...heights }, { compact: 500, fullScreen: 920, fallback: 700, category: 1240 });

const layout = vm.runInContext(`(() => {
  const note = { id: 'wiki/a.md' };
  const center = graphNodePosition('notes', note, 0, 12, 600, 500);
  const compact = graphNodePosition('notes', note, 1, 12, 600, 500);
  const expanded = graphNodePosition('notes', note, 1, 12, 1600, 920);
  const concept = graphNodePosition('keywords', note, 1, 12, 1600, 920);
  return { center, compact, expanded, concept };
})()`, context);
assert.deepEqual({ ...layout.center }, { x: 300, y: 250 });
assert.ok(layout.compact.x !== 300 || layout.compact.y !== 250, 'Notes nodes should leave the center on a radial layout');
const compactRadius = Math.hypot(layout.compact.x - 300, layout.compact.y - 250);
const expandedRadius = Math.hypot(layout.expanded.x - 800, layout.expanded.y - 460);
assert.ok(expandedRadius > compactRadius, 'the radial layout should use the larger expanded canvas');
assert.deepEqual({ ...layout.expanded }, { ...layout.concept }, 'Notes and Concept Map should share radial placement');

context.toggleGraphExpanded();
assert.equal(state.graphExpanded, true);
assert.equal(viewportClasses.has('is-expanded'), true);
assert.equal(documentClasses.has('graph-expanded'), true);
assert.equal(expandButton.attributes['aria-expanded'], 'true');
assert.equal(expandButton.textContent, 'Close full-screen graph');
assert.equal(selection.hidden, false);
assert.match(selection.textContent, /Selected page: Diabetes.*Close graph to view Note Detail/);
const longPdfTitle = 'PDF source observation · carbonaccountingfinancials.com · p.24';
state.rows[0].displayTitle = longPdfTitle;
context.renderExpandedGraphSelection();
assert.ok(selection.textContent.includes(longPdfTitle), 'the expanded status preserves a long PDF source title');
assert.equal(renders, 1);

let prevented = false;
context.handleGraphKeydown({ key: 'Escape', preventDefault() { prevented = true; } });
assert.equal(prevented, true);
assert.equal(state.graphExpanded, false);
assert.equal(viewportClasses.has('is-expanded'), false);
assert.equal(documentClasses.has('graph-expanded'), false);
assert.equal(expandButton.attributes['aria-expanded'], 'false');
assert.equal(expandButton.textContent, 'Expand graph');
assert.equal(selection.hidden, true);
assert.equal(document.activeElement, originalFocus, 'closing should restore focus to where expansion began');
assert.equal(originalFocus.focusCount, 1);
assert.equal(renders, 2, 'opening and closing should re-render at the new canvas size');
