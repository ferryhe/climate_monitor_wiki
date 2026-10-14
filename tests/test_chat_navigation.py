import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_later_direct_article_navigation_wins_over_pending_web_lookup():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    functions = []
    for name in ("beginEvidenceNavigation", "openRegistryArticleEvidence", "comparableSourceUrl", "openWebEvidence"):
        start = source.index(f"function {name}(") if name != "openWebEvidence" else source.index(f"async function {name}(")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    script = r'''
const assert = require("node:assert/strict");
const state = {activeView: "chatView", registry: {evidenceNavigationSequence: 0, articleDetailRequestSequence: 0}};
const loaded = [];
let deferredResolve;
let assigned = "";
const window = {location: {hash: "", assign(url) { assigned = url; }}, history: {pushState(_a, _b, hash) { window.location.hash = hash; }}};
const safeSourceUrl = value => value;
const setWorkspaceView = value => { state.activeView = value; };
const setRegistryMode = () => {};
const registryFetch = () => new Promise(resolve => { deferredResolve = resolve; });
const loadRegistryArticle = async id => { loaded.push(id); };
__FUNCTIONS__
(async () => {
  const stale = openWebEvidence("https://publisher.example/old");
  assert.equal(typeof deferredResolve, "function");
  openRegistryArticleEvidence("article-03");
  const directHash = window.location.hash;
  deferredResolve({items: [{article_id: "article-00", canonical_url: "https://publisher.example/old"}],
    pagination: {pages: 1}});
  await stale;
  assert.deepEqual(loaded, ["article-03"]);
  assert.equal(window.location.hash, directHash);
  assert.equal(assigned, "");
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("__FUNCTIONS__", "\n".join(functions))
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_matched_web_evidence_passes_original_url_to_registry_detail_fallback():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    functions = []
    for name in ("safeSourceUrl", "beginEvidenceNavigation", "comparableSourceUrl", "openWebEvidence"):
        start = source.index(f"async function {name}(" if name == "openWebEvidence" else f"function {name}(")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    script = r'''
const assert = require("node:assert/strict");
const state = {activeView: "chatView", registry: {evidenceNavigationSequence: 0, articleDetailRequestSequence: 0}};
const requested = "https://publisher.example/articles/climate-risk/#summary";
const loaded = [];
const window = {location: {hash: "", assign() {}}, history: {pushState(_a, _b, hash) { window.location.hash = hash; }}};
const URLSearchParams = global.URLSearchParams;
const setWorkspaceView = view => { state.activeView = view; };
const setRegistryMode = () => {};
const registryFetch = async () => ({items: [{article_id: "article-1",
  canonical_url: "https://publisher.example/articles/climate-risk/", source_kind: "registry"}],
  pagination: {pages: 1}});
const loadRegistryArticle = async (...args) => { loaded.push(args); };
__FUNCTIONS__
(async () => {
  await openWebEvidence(requested);
  assert.deepEqual(loaded, [["article-1", "registry", requested]],
    "matched web evidence keeps its original URL available if the Registry detail lookup fails");
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("__FUNCTIONS__", "\n".join(functions))
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_web_registry_search_normalizes_equivalent_urls_without_matching_other_pages():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    functions = []
    for name in ("safeSourceUrl", "beginEvidenceNavigation", "comparableSourceUrl", "openWebEvidence"):
        start = source.index(f"async function {name}(" if name == "openWebEvidence" else f"function {name}(")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    script = r'''
const assert = require("node:assert/strict");
const state = {activeView: "chatView", registry: {evidenceNavigationSequence: 0, articleDetailRequestSequence: 0}};
const record = {article_id: "article-normalized", canonical_url: "https://publisher.example/article", source_kind: "registry"};
const queries = [];
const loaded = [];
const assigned = [];
const window = {location: {hash: "", assign(url) { assigned.push(url); }}, history: {pushState(_a, _b, hash) { window.location.hash = hash; }}};
const URLSearchParams = global.URLSearchParams;
const setWorkspaceView = view => { state.activeView = view; };
const setRegistryMode = () => {};
const registryFetch = async path => {
  const query = new URL(path, "https://local.example").searchParams.get("query");
  queries.push(query);
  const items = record.canonical_url.includes(query) ? [record] : [];
  return {items, pagination: {pages: items.length ? 1 : 0}};
};
const loadRegistryArticle = async (...args) => { loaded.push(args); };
__FUNCTIONS__
(async () => {
  const equivalentUrls = [
    "https://publisher.example/article/",
    "https://publisher.example/article#summary",
  ];
  for (const url of equivalentUrls) await openWebEvidence(url);
  assert.deepEqual(loaded, equivalentUrls.map(url => ["article-normalized", "registry", url]));
  assert.deepEqual(queries, [record.canonical_url, record.canonical_url],
    "the API query uses the same URL normalization as exact Registry identity matching");
  const unrelated = "https://publisher.example/article-other";
  await openWebEvidence(unrelated);
  assert.equal(loaded.length, 2, "an unrelated record must not be opened as the requested article");
  assert.deepEqual(assigned, [unrelated], "unmatched web evidence keeps its original external behavior");
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("__FUNCTIONS__", "\n".join(functions))
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_registry_mode_click_invalidates_pending_web_fallback():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    functions = []
    for name in ("safeSourceUrl", "beginEvidenceNavigation", "comparableSourceUrl", "openWebEvidence"):
        start = source.index(f"async function {name}(" if name == "openWebEvidence" else f"function {name}(")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    registration = re.search(
        r'els\.registryModeButtons\.forEach\(\(button\) => \{\s*button\.addEventListener\("click", \(\) => \{[\s\S]*?\}\);\s*\}\);',
        source,
    )
    assert registration, "missing Registry mode button click callback"
    script = r'''
const assert = require("node:assert/strict");
const state = {activeView: "registryView", registry: {mode: "articles", evidenceNavigationSequence: 0, articleDetailRequestSequence: 0}};
let release;
let assigned = "";
let callback;
const button = {dataset: {registryMode: "reports"}, addEventListener(type, fn) { assert.equal(type, "click"); callback = fn; }};
const els = {registryModeButtons: [button]};
const window = {location: {hash: "", assign(url) { assigned = url; }}, history: {pushState(_a, _b, hash) { window.location.hash = hash; }}};
const URLSearchParams = global.URLSearchParams;
const setWorkspaceView = view => { state.activeView = view; };
const setRegistryMode = mode => { state.registry.mode = mode === "articles" ? "articles" : "reports"; };
const registryFetch = () => new Promise(resolve => { release = resolve; });
const loadRegistryArticle = async () => {};
__FUNCTIONS__
eval(__REGISTRATION__);
(async () => {
  const pending = openWebEvidence("https://publisher.example/unlisted");
  callback();
  assert.equal(state.registry.mode, "reports", "the actual user callback selects Reports");
  release({items: [], pagination: {pages: 0}});
  await pending;
  assert.equal(state.registry.mode, "reports");
  assert.equal(assigned, "", "stale no-match resolution must not navigate away from the selected mode");
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("__FUNCTIONS__", "\n".join(functions)).replace("__REGISTRATION__", json.dumps(registration.group(0)))
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_real_meetings_hashchange_cancels_pending_web_match_or_fallback():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    functions = []
    for name, prefix in (("safeSourceUrl", "function"), ("beginEvidenceNavigation", "function"),
                         ("comparableSourceUrl", "function"), ("sourceNavigationFromHash", "function"),
                         ("openWebEvidence", "async function")):
        start = source.index(f"{prefix} {name}(")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    handler = re.search(
        r'window\.addEventListener\("hashchange", \(\) => \{[\s\S]*?\n  \}\);', source,
    )
    assert handler, "missing actual hashchange handler"
    script = r'''
const assert = require("node:assert/strict");
const state = {activeView: "chatView", registry: {evidenceNavigationSequence: 0, articleDetailRequestSequence: 0}};
let release;
let assigned = "";
let hashchange;
const loaded = [];
const window = {
  location: {hash: "", assign(url) { assigned = url; }},
  history: {pushState(_a, _b, hash) { window.location.hash = hash; }},
  addEventListener(type, listener) { if (type === "hashchange") hashchange = listener; },
};
const URLSearchParams = global.URLSearchParams;
const setWorkspaceView = view => { state.activeView = view; };
const setRegistryMode = () => {};
const registryFetch = () => new Promise(resolve => { release = resolve; });
const loadRegistryArticle = async (...args) => { loaded.push(args); };
__FUNCTIONS__
__HANDLER__
(async () => {
  for (const response of [
    {items: [], pagination: {pages: 0}},
    {items: [{article_id: "article-late", canonical_url: "https://publisher.example/paper"}], pagination: {pages: 1}},
  ]) {
    assigned = "";
    loaded.length = 0;
    const pending = openWebEvidence("https://publisher.example/paper");
    assert.equal(typeof release, "function");
    window.location.hash = "#meetings"; // Browser Back from the in-flight evidence URL.
    hashchange();
    assert.equal(state.activeView, "meetingsView");
    release(response);
    await pending;
    await Promise.resolve();
    assert.equal(assigned, "", "a stale no-match result must not navigate away from Dates");
    assert.deepEqual(loaded, [], "a stale article match must not replace the user's Back navigation");
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("__FUNCTIONS__", "\n".join(functions)).replace("__HANDLER__", handler.group(0))
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_pdf_observation_preserves_full_id_and_detail_recovers_after_missing_match():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    functions = []
    for name in ("safeSourceUrl", "beginEvidenceNavigation", "resetRegistryArticleDetail", "sourceNavigationFromHash", "openPdfObservationEvidence"):
        start = source.index(f"async function {name}(") if name == "openPdfObservationEvidence" else source.index(f"function {name}(")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    script = r'''
const assert = require("node:assert/strict");
const state = {activeView: "chatView", registry: {evidenceNavigationSequence: 0, articleDetailRequestSequence: 0}};
let matchOccurrence = false;
let fetchFails = false;
let detailNotFound = true;
const els = {
  registryArticleTitle: {textContent: "Old title"},
  registryArticleMeta: {textContent: "Old meta", replaceChildren() { this.textContent = ""; }},
  registryEnrichment: {children: ["Old unrelated summary"], replaceChildren(...items) { this.children = items; }, hidden: false},
  registryAppearances: {children: ["Old appearance"], replaceChildren(...items) { this.children = items; }},
  registryAppearancesSection: {hidden: true},
  registryContentSection: {hidden: false},
  registryMarkdown: {textContent: "Old content"},
  registryOriginalLink: {hidden: false, href: "https://old.example/source", textContent: "Old source", removeAttribute() { this.href = ""; }},
  registryArticleDetail: {children: ["retained detail node"], setAttribute() {}}
};
const registryFetch = async path => {
  if (fetchFails) throw new Error("Registry unavailable");
  if (path.startsWith("/api/registry/pdf-intake/articles?"))
    return {items: [{article_id: "pdf-article-1", latest_occurrence: {occurrence_id: "pdf-article-occurrence-newer"}}], pagination: {pages: 1}};
  if (detailNotFound) { const error = new Error("not found"); error.status = 404; throw error; }
  return {occurrences: matchOccurrence ? [{occurrence_id: "pdf-article-occurrence-target"}] : []};
};
const setWorkspaceView = value => { state.activeView = value; };
const setRegistryMode = () => {};
const registryErrorMessage = error => error.message;
const loadRegistryArticle = async id => { els.registryArticleTitle.textContent = `Loaded ${id}`; };
const window = {location: {hash: ""}, history: {pushState(_a, _b, hash) { window.location.hash = hash; }}};
__FUNCTIONS__
(async () => {
  const lookup = {search: "Guide for supervisors", url: "https://publisher.example/source?x=1&y=2", snippet: "Retained PDF passage"};
  await openPdfObservationEvidence("pdf-article-occurrence-target", lookup);
  assert.equal(els.registryArticleTitle.textContent, "PDF observation unavailable");
  assert.deepEqual(els.registryArticleDetail.children, ["retained detail node"]);
  assert.deepEqual(els.registryEnrichment.children, [], "failed lookup must not leave the previous article summary visible");
  assert.deepEqual(els.registryAppearances.children, [], "failed lookup must not leave previous article appearances visible");
  assert.equal(els.registryOriginalLink.href, lookup.url);
  assert.equal(els.registryOriginalLink.hidden, false);
  const deepLink = sourceNavigationFromHash();
  assert.deepEqual(deepLink, {kind: "pdf-observation", id: "pdf-article-occurrence-target", ...lookup});
  fetchFails = true;
  await openPdfObservationEvidence("pdf-article-occurrence-target", lookup);
  assert.equal(els.registryArticleTitle.textContent, "Article unavailable");
  assert.equal(els.registryOriginalLink.href, lookup.url, "fetch errors retain the cited source URL");
  fetchFails = false;
  matchOccurrence = true;
  detailNotFound = false;
  await openPdfObservationEvidence("pdf-article-occurrence-target", lookup);
  assert.equal(els.registryArticleTitle.textContent, "Loaded pdf-article-1");
  assert.deepEqual(els.registryArticleDetail.children, ["retained detail node"]);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("__FUNCTIONS__", "\n".join(functions))
    subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)


def test_pdf_detail_404_keeps_original_url_and_clears_unrelated_detail():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    functions = []
    for name in ("safeSourceUrl", "resetRegistryArticleDetail", "loadRegistryArticle"):
        start = source.index(f"async function {name}(" if name == "loadRegistryArticle" else f"function {name}(")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    script = r'''
const assert = require("node:assert/strict");
const state = {registry: {evidenceNavigationSequence: 0, articleDetailRequestSequence: 0}};
const els = {
  registryArticleDetail: {setAttribute() {}},
  registryArticleTitle: {textContent: "Previous article"},
  registryArticleMeta: {items: ["old metadata"], replaceChildren(...items) { this.items = items; }},
  registryEnrichment: {items: ["unrelated summary"], hidden: false, replaceChildren(...items) { this.items = items; },
    append(item) { this.items.push(item); }},
  registryAppearances: {items: ["old appearance"], replaceChildren(...items) { this.items = items; }},
  registryAppearancesSection: {hidden: false},
  registryContentSection: {hidden: false}, registryMarkdown: {textContent: "old markdown"},
  registryOriginalLink: {hidden: false, href: "https://old.example/article", textContent: "Old source",
    removeAttribute() { this.href = ""; }},
};
const registryFetch = async () => { const error = new Error("not found"); error.status = 404; throw error; };
const registryErrorMessage = error => error.message;
const registryElement = (_tag, className, text) => ({className, textContent: text});
__FUNCTIONS__
(async () => {
  const original = "https://publisher.example/pdf?article=42&source=report";
  await loadRegistryArticle("pdf-article-42", "pdf", original);
  assert.equal(els.registryArticleTitle.textContent, "Article unavailable");
  assert.deepEqual(els.registryArticleMeta.items, [], "a failed lookup must clear prior metadata");
  assert.deepEqual(els.registryAppearances.items, [], "a failed lookup must clear prior appearances");
  assert.deepEqual(els.registryEnrichment.items, [{className: "registry-notice",
    textContent: "not found The original cited source is still available below."}]);
  assert.equal(els.registryOriginalLink.href, original);
  assert.equal(els.registryOriginalLink.textContent, "Open original source");
  assert.equal(els.registryOriginalLink.hidden, false);
  assert.equal(els.registryContentSection.hidden, true);
  assert.equal(els.registryMarkdown.textContent, "");
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("__FUNCTIONS__", "\n".join(functions))
    subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)


def test_stale_pdf_lookup_cannot_replace_a_later_direct_article_navigation():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    functions = []
    for name in ("safeSourceUrl", "beginEvidenceNavigation", "resetRegistryArticleDetail", "openRegistryArticleEvidence", "openPdfObservationEvidence"):
        start = source.index(f"async function {name}(" if name == "openPdfObservationEvidence" else f"function {name}(")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    script = r'''
const assert = require("node:assert/strict");
const state = {activeView: "chatView", registry: {evidenceNavigationSequence: 0, articleDetailRequestSequence: 0}};
let release;
const els = {registryArticleDetail: {setAttribute() {}}, registryArticleTitle: {textContent: "Old"},
  registryArticleMeta: {replaceChildren() {}}, registryEnrichment: {replaceChildren() {}, hidden: true},
  registryAppearances: {replaceChildren() {}}, registryAppearancesSection: {hidden: false}, registryContentSection: {hidden: true},
  registryMarkdown: {textContent: ""}, registryOriginalLink: {hidden: true, removeAttribute() {}}};
const window = {location: {hash: ""}, history: {pushState(_a, _b, hash) { window.location.hash = hash; }}};
const setWorkspaceView = view => { state.activeView = view; };
const setRegistryMode = () => {};
const registryFetch = () => new Promise(resolve => { release = resolve; });
const loadRegistryArticle = async id => { els.registryArticleTitle.textContent = `Article ${id}`; };
__FUNCTIONS__
(async () => {
  const pending = openPdfObservationEvidence("pdf-article-occurrence-old", {
    search: "old observation", url: "https://publisher.example/old", snippet: "old facts"});
  const later = openRegistryArticleEvidence("article-current", "registry", {fallbackSourceUrl: "https://publisher.example/current"});
  const directHash = window.location.hash;
  release({items: [{article_id: "pdf-old", latest_occurrence: {occurrence_id: "pdf-article-occurrence-old"}}],
    pagination: {pages: 1}});
  await pending;
  await later;
  assert.equal(els.registryArticleTitle.textContent, "Article article-current");
  assert.equal(window.location.hash, directHash);
  assert.equal(window.location.hash.includes("old observation"), false);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("__FUNCTIONS__", "\n".join(functions))
    subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)


def test_modified_evidence_click_uses_registered_document_listener_without_interception():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    start = source.index("function attachSourceCardClickHandler()")
    end = source.index("\n}", start) + 2
    handler = source[start:end]
    script = r'''
const assert = require("node:assert/strict");
let listener;
const calls = [];
const document = {addEventListener(type, callback) { assert.equal(type, "click"); listener = callback; }};
class Element {}
const card = Object.assign(new Element(), {dataset: {sourceNavigation: "article", articleId: "article-404",
  articleSource: "registry", originalUrl: "https://publisher.example/original"}});
const target = Object.assign(new Element(), {closest(selector) { return selector === "[data-source-navigation]" ? card : null; }});
const openRegistryArticleEvidence = (...args) => calls.push(args);
const prevent = {count: 0};
__HANDLER__
attachSourceCardClickHandler();
const dispatch = modifiers => listener({target, button: 0, ...modifiers, preventDefault() { prevent.count += 1; }});
dispatch({ctrlKey: true});
assert.equal(prevent.count, 0);
assert.equal(calls.length, 0, "modified click remains available to the browser's native new-tab action");
dispatch({metaKey: true});
dispatch({shiftKey: true});
dispatch({altKey: true});
assert.equal(prevent.count, 0);
assert.equal(calls.length, 0);
dispatch({button: 1});
assert.equal(prevent.count, 0);
assert.equal(calls.length, 0, "middle click retains the browser's native open-in-new-tab behavior");
dispatch({});
assert.equal(prevent.count, 1);
assert.deepEqual(calls, [["article-404", "registry", {fallbackSourceUrl: "https://publisher.example/original"}]]);
'''.replace("__HANDLER__", handler)
    subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)


def test_article_card_navigation_invalidates_pending_web_resolution_and_keeps_source_fallback():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    functions = []
    for name, prefix in (("beginEvidenceNavigation", "function"), ("resetRegistryArticleDetail", "function"), ("comparableSourceUrl", "function"),
                         ("openWebEvidence", "async function"), ("loadRegistryArticle", "async function")):
        start = source.index(f"{prefix} {name}(")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    script = r'''
const assert = require("node:assert/strict");
const state = {activeView: "registryView", registry: {evidenceNavigationSequence: 0, articleDetailRequestSequence: 0}};
let release;
let assigned = "";
const window = {location: {hash: "", assign(url) { assigned = url; }}, history: {pushState(_a, _b, hash) { window.location.hash = hash; }}};
const safeSourceUrl = value => value;
const setWorkspaceView = () => {};
const setRegistryMode = () => {};
const registryFetch = path => {
  if (path.startsWith("/api/registry/articles?")) return new Promise(resolve => { release = resolve; });
  const error = new Error("not found"); error.status = 404; return Promise.reject(error);
};
const els = {registryArticleDetail: {setAttribute() {}}, registryArticleTitle: {textContent: ""},
  registryArticleMeta: {replaceChildren() {}}, registryEnrichment: {replaceChildren() {}, append() {}, hidden: true},
  registryAppearances: {replaceChildren() {}}, registryAppearancesSection: {hidden: false},
  registryContentSection: {hidden: true}, registryMarkdown: {textContent: ""},
  registryOriginalLink: {hidden: true, removeAttribute() {}}};
const registryElement = (_tag, _className, text) => ({textContent: text});
const registryErrorMessage = error => error.message;
__FUNCTIONS__
(async () => {
  const delayed = openWebEvidence("https://publisher.example/old");
  await Promise.resolve();
  await loadRegistryArticle("article-database-selection", "registry", "https://publisher.example/selected");
  release({items: [{article_id: "article-old", canonical_url: "https://publisher.example/old"}], pagination: {pages: 1}});
  await delayed;
  assert.match(els.registryArticleTitle.textContent, /unavailable/);
  assert.equal(els.registryOriginalLink.href, "https://publisher.example/selected");
  assert.equal(assigned, "");
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("__FUNCTIONS__", "\n".join(functions))
    subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)


def test_short_answer_can_align_current_question_after_long_history_and_next_question_resets_tail():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    start = source.index("function renderMessages(")
    end = source.index("\n}", start) + 2
    function = source[start:end]
    script = r'''
const assert = require("node:assert/strict");
const rows = [];
let contentHeight = 0;
const messages = {
  clientHeight: 548,
  style: {paddingBottom: ""},
  set innerHTML(_value) { rows.length = 0; contentHeight = 0; this.scrollTop = 0; },
  get scrollHeight() { return contentHeight + (parseFloat(this.style.paddingBottom) || 0); },
  get scrollTop() { return this._scrollTop || 0; },
  set scrollTop(value) { this._scrollTop = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); },
  appendChild(row) {
    row.offsetTop = contentHeight;
    row.parent = this;
    rows.push(row);
    contentHeight += row.height;
  },
  querySelectorAll(selector) { return selector === ".message-row--user" ? rows.filter(row => row.className.includes("message-row--user")) : []; },
  getBoundingClientRect() { return {top: 100}; },
};
const document = {createElement(tag) {
  return {tag, className: "", height: 80, classList: {add() {}}, children: [],
    appendChild(child) { this.children.push(child); },
    getBoundingClientRect() { return {top: 100 + this.offsetTop - this.parent.scrollTop}; }};
}};
const els = {messages};
const state = {messages: []};
const renderMarkdownFull = value => value;
const renderSourceCards = () => "";
const renderEmptyState = () => {};
const escapeHtml = value => value;
__FUNCTION__
state.messages = Array.from({length: 12}, (_, index) => [
  {role: "user", content: `Earlier question ${index}`},
  {role: "assistant", content: "Earlier answer with detail"},
]).flat();
state.messages.push({role: "user", content: "Current question"}, {role: "assistant", content: "Brief answer"});
renderMessages({focusCurrentQuestion: true});
const question = rows.filter(row => row.className.includes("message-row--user")).at(-1);
assert.equal(question.getBoundingClientRect().top - messages.getBoundingClientRect().top, 0);
assert.ok(messages.scrollHeight - messages.clientHeight >= question.offsetTop);
state.messages.push({role: "user", content: "Next question"}, {role: "assistant", content: "Searching…", pending: true});
renderMessages();
assert.equal(messages.style.paddingBottom, "");
assert.equal(messages.scrollTop, messages.scrollHeight - messages.clientHeight, "new questions keep the intentional scroll-to-bottom behavior");
'''.replace("__FUNCTION__", function)
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_hidden_chat_completion_defers_question_alignment_until_chat_reentry():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    functions = []
    for name in ("setWorkspaceView", "renderMessages"):
        start = source.index(f"function {name}(")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    script = r'''
const assert = require("node:assert/strict");
const rows = [];
let contentHeight = 0;
const chatView = {hidden: true};
const messages = {
  style: {paddingBottom: ""},
  get clientHeight() { return chatView.hidden ? 0 : 548; },
  set innerHTML(_value) { rows.length = 0; contentHeight = 0; this.scrollTop = 0; },
  get scrollHeight() { return contentHeight + (parseFloat(this.style.paddingBottom) || 0); },
  get scrollTop() { return this._scrollTop || 0; },
  set scrollTop(value) { this._scrollTop = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); },
  appendChild(row) { row.offsetTop = contentHeight; row.parent = this; rows.push(row); contentHeight += row.height; },
  querySelectorAll(selector) { return selector === ".message-row--user" ? rows.filter(row => row.className.includes("message-row--user")) : []; },
  getBoundingClientRect() { return {top: chatView.hidden ? 0 : 100}; },
};
const document = {createElement(tag) {
  return {tag, className: "", height: 80, classList: {add() {}}, children: [],
    appendChild(child) { this.children.push(child); },
    getBoundingClientRect() { return {top: chatView.hidden ? 0 : 100 + this.offsetTop - this.parent.scrollTop}; }};
}};
const els = {messages, chatView, obsidianView: {hidden: true}, registryView: {hidden: true},
  meetingsView: {hidden: true}, workspaceTabs: []};
const state = {activeView: "meetingsView", pendingQuestionFocus: false,
  registry: {loaded: true, meetingPage: 1}, messages: []};
const stopGraphAnimation = () => {};
const loadRegistryMeetings = () => {};
const renderCurrentGraph = () => {};
const renderMarkdownFull = value => value;
const renderSourceCards = () => "";
const renderEmptyState = () => {};
const escapeHtml = value => value;
__FUNCTIONS__
state.messages = Array.from({length: 12}, (_, index) => [
  {role: "user", content: `Earlier question ${index}`},
  {role: "assistant", content: "Earlier answer with detail"},
]).flat();
state.messages.push({role: "user", content: "Current question"}, {role: "assistant", content: "Brief answer"});
renderMessages({focusCurrentQuestion: true});
assert.equal(state.pendingQuestionFocus, true, "completion in hidden Chat remembers alignment work");
assert.equal(messages.style.paddingBottom, "", "hidden layout must not calculate a zero-height tail");
assert.equal(messages.scrollTop, 0, "hidden completion does not try to scroll a zero-height viewport");
setWorkspaceView("chatView");
const question = rows.filter(row => row.className.includes("message-row--user")).at(-1);
assert.equal(state.pendingQuestionFocus, false);
assert.equal(question.getBoundingClientRect().top - messages.getBoundingClientRect().top, 0,
  "returning to Chat aligns the current question above its answer");
assert.ok(messages.style.paddingBottom, "visible alignment reserves enough scroll room");

renderMessages({focusCurrentQuestion: true});
const visibleQuestion = rows.filter(row => row.className.includes("message-row--user")).at(-1);
assert.equal(visibleQuestion.getBoundingClientRect().top - messages.getBoundingClientRect().top, 0,
  "completion while Chat is visible still aligns immediately");
messages.scrollTop = 300;
setWorkspaceView("meetingsView");
setWorkspaceView("chatView");
assert.equal(messages.scrollTop, 300, "ordinary return to Chat preserves manual scroll when no alignment is pending");
'''.replace("__FUNCTIONS__", "\n".join(functions))
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_focused_dates_load_unknown_precision_record_by_stable_id():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    source = (ROOT / "showcase/app.js").read_text(encoding="utf-8")
    functions = []
    for name in ("meetingDateGroup", "loadRegistryMeetings"):
        start = source.index(f"async function {name}(" if name == "loadRegistryMeetings" else f"function {name}(")
        end = source.index("\n}", start) + 2
        functions.append(source[start:end])
    script = r'''
const assert = require("node:assert/strict");
const requests = [];
const partial = {canonical_event_id: "meeting-climate-webinar-2028", event_id: "event-source-id",
  name: "Climate Webinar 2028", event_type: "webinar", date_precision: "month",
  raw_time_text: "March 2028", start_date: "2028-03", end_date: null, needs_confirmation: 1,
  source_urls: []};
const makeElement = (tag, className = "", textContent = "") => ({tag, className, textContent,
  children: [], dataset: {}, append(...items) { this.children.push(...items); },
  replaceChildren(...items) { this.children = [...items]; }, scrollIntoView() {}});
const state = {registry: {meetingPage: 1, meetingRequestSequence: 0}};
const els = {registryMeetingSearch: {value: ""}, registryMeetingVerification: {value: ""},
  registryMeetingCounts: {textContent: ""}, registryMeetings: makeElement("div")};
const registryElement = makeElement;
const safeSourceUrl = value => value || "";
const notices = [];
const renderRegistryNotice = (_container, message) => notices.push(message);
const registryErrorMessage = error => error.message;
const registryFetch = async path => {
  requests.push(path);
  const params = new URL(path, "https://local.example").searchParams;
  return {items: params.get("include_unknown") === "true" ? [partial] : [],
    pagination: {page: 1, pages: params.get("include_unknown") === "true" ? 1 : 0, total: params.get("include_unknown") === "true" ? 1 : 0},
    base_date: "1900-01-01", verification_counts: {}};
};
const meetingInstitution = item => item.organizer || "Institution not specified";
const meetingLocationSummary = () => "Location not provided";
const appendMeetingLocations = () => {};
const appendInformationCheck = () => {};
const updateRegistryPagination = () => {};
const requestAnimationFrame = callback => callback();
__FUNCTIONS__
(async () => {
  await loadRegistryMeetings({focusId: partial.canonical_event_id, focusName: partial.name});
  const focusedParams = new URL(requests[0], "https://local.example").searchParams;
  assert.equal(focusedParams.get("include_unknown"), "true");
  assert.equal(focusedParams.get("base_date"), "1900-01-01");
  assert.deepEqual(notices, ["Loading meetings…"]);
  const card = els.registryMeetings.children[0].children[0].children[1].children[0];
  assert.equal(card.dataset.meetingId, partial.canonical_event_id, "focus expands the exact stable record ID");
  assert.equal(card.open, true);
  const dateFields = card.children[1].children[0].children;
  const dateLabel = dateFields.findIndex(field => field.textContent === "Date(s)");
  assert.equal(dateFields[dateLabel + 1].textContent, "March 2028", "the card displays the producer's partial date wording");
  assert.equal(JSON.stringify(card).includes("2028-03"), false, "no ISO day or placeholder date is presented");
  assert.equal(els.registryMeetingCounts.textContent, "Selected evidence record");
  await loadRegistryMeetings();
  assert.equal(new URL(requests[1], "https://local.example").searchParams.has("include_unknown"), false,
    "ordinary Dates keeps its default unknown-date filter");
})().catch(error => { console.error(error); process.exitCode = 1; });
'''.replace("__FUNCTIONS__", "\n".join(functions))
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
