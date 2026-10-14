import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync('showcase/app.js', 'utf8');
const styles = fs.readFileSync('showcase/styles.css', 'utf8');
const mobileMessageRule = styles.match(/@media \(max-width: 760px\) \{[\s\S]*?\.message-list\s*\{([^}]*)\}/)?.[1] || '';
const mobileContextRule = styles.match(/@media \(max-width: 760px\) \{[\s\S]*?\.composer-shell__context\s*\{([^}]*)\}/)?.[1] || '';
const mobileBadgeRule = styles.match(/@media \(max-width: 760px\) \{[\s\S]*?\.composer-shell__context \.context-badge\s*\{([^}]*)\}/)?.[1] || '';
assert.match(mobileMessageRule, /height:\s*100%/, 'the mobile message viewport fills its flex-sized chat stage');
assert.match(mobileMessageRule, /min-height:\s*0/, 'the message viewport can shrink when the composer grows');
assert.doesNotMatch(mobileMessageRule, /calc\(100vh\s*-\s*302px\)/,
  'mobile replies do not extend beyond the available chat stage');
assert.match(mobileContextRule, /min-width:\s*0/, 'the composer context row can shrink beside its action');
assert.match(mobileBadgeRule, /min-width:\s*0/, 'a long source reference can shrink inside the context row');
assert.match(mobileBadgeRule, /overflow-wrap:\s*anywhere/,
  'a long source host such as carbonaccountingfinancials.com wraps on a narrow screen');
const extract = (name) => source.match(new RegExp(`function ${name}\\([^]*?\\n\\}`))[0];
let mobile = true;
let focused = null;
let scrolled = null;
const back = {focus() { focused = 'back'; }};
const card = {focus() { focused = 'card'; }};
const classes = new Set();
const panel = {dataset: {}, classList: {
  contains: value => classes.has(value), add: value => classes.add(value), remove: value => classes.delete(value),
}, querySelector: selector => selector === '.mobile-back' ? back : card};
const context = vm.createContext({window: {
  scrollY: 460, matchMedia: () => ({matches: mobile}), scrollTo: value => { scrolled = value.top; },
}});
vm.runInContext(extract('showMobileDetail') + '\n' + extract('closeMobileDetail'), context);
context.showMobileDetail(panel);
assert.equal(scrolled, 0);
assert.equal(focused, 'back');
assert.equal(classes.has('is-detail-open'), true);
context.window.scrollY = 180;
context.showMobileDetail(panel); // PDF pagination stays within the same detail.
assert.equal(panel.dataset.listScroll, '460');
context.closeMobileDetail(panel);
assert.equal(scrolled, 460);
assert.equal(focused, 'card');
assert.equal(classes.has('is-detail-open'), false);
mobile = false;
scrolled = null;
context.showMobileDetail(panel);
assert.equal(classes.has('is-detail-open'), false, 'desktop retains both panes');
assert.equal(scrolled, null, 'desktop selection does not jump the page');
context.showMobileDetail(undefined); // Optional panels in other UI contexts.
console.log('PASS mobile report detail, PDF pagination return position and desktop selection');
