import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const fixture = JSON.parse(readFileSync(process.argv[2], "utf8"));
const source = readFileSync(new URL("../showcase/app.js", import.meta.url), "utf8").replaceAll("\r\n", "\n");
const extract = (name) => {
  const marker = source.indexOf(`function ${name}(`) >= 0
    ? source.indexOf(`function ${name}(`) : source.indexOf(`async function ${name}(`);
  assert.ok(marker >= 0, name);
  const start = source.slice(marker - 6, marker) === "async " ? marker - 6 : marker;
  return source.slice(start, source.indexOf("\n}", marker) + 2);
};

class Element {
  constructor(tag = "div", text = "") {
    this.tag = tag; this.textContent = text; this.children = []; this.dataset = {}; this.hidden = false;
    this.attributes = {};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  closest(selector) { return selector === "[data-source-navigation]" && this.dataset.sourceNavigation ? this : null; }
}
const walk = (node) => [node, ...node.children.flatMap(walk)];
const settle = async () => { await new Promise((resolve) => setImmediate(resolve)); await new Promise((resolve) => setImmediate(resolve)); };

async function exercise(sourceItem) {
  const listeners = {};
  const elements = Object.fromEntries(["registryReportDetail", "registryReportTitle", "registryReportMeta", "registryReportsPanel"]
    .map((name) => [name, new Element()]));
  const state = { activeView: "chatView", registry: { evidenceNavigationSequence: 0, reportRequestSequence: 0, selectedReportDate: "" } };
  const history = [];
  const window = { location: { hash: "", origin: "https://climate.example" }, history: {
    pushState(_state, _title, hash) { window.location.hash = hash; history.push(hash); },
  }, addEventListener(type, handler) { listeners[`window-${type}`] = handler; } };
  const document = { addEventListener(type, handler) { listeners[`document-${type}`] = handler; } };
  const context = vm.createContext({ Element, els: elements, state, window, document, URL, URLSearchParams, console,
    registryElement: (tag, _className = "", text = "") => new Element(tag, text),
    registryErrorMessage: (error) => error.message,
    registryFetch: async () => { throw new Error("Simulated historical report detail outage"); },
    showMobileDetail() {}, setRegistryMode() {}, setWorkspaceView(value) { state.activeView = value; },
    clearHistoricalReportContent() { elements.registryReportMeta.replaceChildren(); },
  });
  for (const name of ["escapeHtml", "safeSourceUrl", "safeReportSourceUrl", "reviewedCheckedSourceUrl", "reviewedSourceRoles",
    "sourceCitationHref", "sourceNavigationTarget", "sourceNavigationAttributes", "sourceNavigationHref", "sourceNavigationFromHash",
    "renderSourceCards", "historicalReportDateFromHash", "beginEvidenceNavigation", "openHistoricalReport", "loadRegistryReport",
    "attachSourceCardClickHandler"]) vm.runInContext(extract(name), context);

  const cardsHtml = context.renderSourceCards([sourceItem]);
  assert.match(cardsHtml, /data-source-navigation="report"/);
  const card = new Element("a");
  for (const [, key, value] of cardsHtml.matchAll(/data-([a-z-]+)="([^"]*)"/g)) {
    card.dataset[key.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase())] = value;
  }
  context.attachSourceCardClickHandler();
  listeners["document-click"]({ target: card, button: 0, preventDefault() {} });
  await settle();
  const emittedHash = window.location.hash;
  const firstLinks = walk(elements.registryReportMeta).filter((item) => item.href).map((item) => ({ href: item.href, label: item.textContent }));
  const parsedTarget = context.sourceNavigationFromHash();

  const attachStart = source.indexOf('  window.addEventListener("hashchange", () => {', source.indexOf("function attachEvents()"));
  const attachEnd = source.indexOf("\n\n  document.addEventListener(\"visibilitychange\"", attachStart);
  assert.ok(attachStart > 0 && attachEnd > attachStart);
  vm.runInContext(source.slice(attachStart, attachEnd), context);
  window.location.hash = "#other-state";
  window.location.hash = emittedHash;
  listeners["window-hashchange"]();
  await settle();
  const restoredLinks = walk(elements.registryReportMeta).filter((item) => item.href).map((item) => ({ href: item.href, label: item.textContent }));
  return { history, emittedHash, parsedTarget, firstLinks, restoredLinks };
}

const checked = fixture.source || fixture.offline_answer?.sources?.find((item) => /climate-monitor-2026-09-28\.md$/.test(item.path || "")
  && item.checked_source_url === fixture.checked_url && item.original_source_url === fixture.original_url);
assert.ok(checked, "actual approved Chat report source fixture");
assert.equal(checked.checked_source_url, fixture.checked_url);
assert.equal(checked.original_source_url, fixture.original_url);
const result = await exercise(checked);
assert.ok(result.emittedHash.includes(`&checked=${encodeURIComponent(fixture.checked_url)}`));
assert.ok(result.emittedHash.includes(`&original=${encodeURIComponent(fixture.original_url)}`));
assert.equal(result.parsedTarget.checkedSourceUrl, fixture.checked_url);
assert.equal(result.parsedTarget.originalUrl, fixture.original_url);
assert.ok(result.firstLinks.some((item) => item.href === fixture.checked_url && item.label === "Open checked source"));
assert.ok(result.restoredLinks.some((item) => item.href === fixture.checked_url && item.label === "Open checked source"));
if (fixture.checked_url === fixture.original_url) {
  assert.equal(result.firstLinks.filter((item) => item.href === fixture.checked_url).length, 1);
  assert.equal(result.restoredLinks.filter((item) => item.href === fixture.checked_url).length, 1);
  assert.equal(result.firstLinks.some((item) => item.label === "Open original source"), false);
  assert.equal(result.restoredLinks.some((item) => item.label === "Open original source"), false);
} else {
  assert.ok(result.firstLinks.some((item) => item.href === fixture.original_url && item.label === "Open original source"));
  assert.ok(result.restoredLinks.some((item) => item.href === fixture.original_url && item.label === "Open original source"));
}

const ordinary = { ...checked, checked_source_url: undefined, checked_source_provenance: undefined,
  owner_agent_verification: null, original_source_url: fixture.original_url,
  canonical_url: fixture.original_url, source_urls: [fixture.original_url], url: fixture.original_url };
const ordinaryResult = await exercise(ordinary);
assert.equal(ordinaryResult.parsedTarget.checkedSourceUrl, undefined);
assert.ok(ordinaryResult.restoredLinks.some((item) => item.href === fixture.original_url && item.label === "Open original source"));
assert.equal(ordinaryResult.restoredLinks.some((item) => item.label === "Open checked source"), false);
console.log("Actual report card hash, parser and history outage preserve checked/original roles; ordinary citations stay original.");
