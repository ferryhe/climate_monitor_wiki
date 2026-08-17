from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "showcase" / "index.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "showcase" / "app.js").read_text(encoding="utf-8")
STYLES = (ROOT / "showcase" / "styles.css").read_text(encoding="utf-8")
CHROME_CANDIDATES = tuple(
    Path(candidate)
    for candidate in (
        os.getenv("SYSTEM_CHROME", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    )
    if candidate
)

MOCK_FETCH = r"""
window.__registryRequests = [];
window.__registryScenario = "loading";
window.__missingArticle = false;
const registryResponse = (status, data) => Promise.resolve(new Response(
  JSON.stringify(data), {status, headers: {"Content-Type": "application/json"}}
));
window.fetch = (input, options = {}) => {
  const url = new URL(input, window.location.href);
  window.__registryRequests.push({method: options.method || "GET", path: url.pathname + url.search});
  if (url.pathname === "/api/config") return registryResponse(200, {
    documents: [], concepts: [], graphs: {notes: null, keywords: null}, prompt_starters: [],
    agent_mode: "offline", model: "offline-extractive", default_answer_mode: "detailed"
  });
  if (url.pathname === "/api/registry/status") {
    if (window.__registryScenario === "loading") {
      return new Promise((resolve) => { window.__releaseRegistry = () => resolve(
        new Response(JSON.stringify({available: false, reason: "not_configured"}),
          {status: 503, headers: {"Content-Type": "application/json"}})
      ); });
    }
    if (window.__registryScenario === "unavailable") {
      return registryResponse(503, {available: false, reason: "database_unavailable"});
    }
    if (window.__registryScenario === "empty") {
      return registryResponse(200, {available: true, schema_version: 3, reports: 0,
        articles: 0, discoveries: 0, latest_report_date: null});
    }
    return registryResponse(200, {available: true, schema_version: 3, reports: 2,
      articles: 2, discoveries: 2, latest_report_date: "2026-08-10"});
  }
  if (url.pathname === "/api/registry/reports") {
    if (window.__registryScenario === "empty") {
      return registryResponse(200, {items: [], pagination: {page: 1, page_size: 12, total: 0, pages: 0}});
    }
    const page = Number(url.searchParams.get("page"));
    return registryResponse(200, {items: [{report_date: page === 1 ? "2026-08-10" : "2026-08-03",
      report_title: `Report page ${page}`, article_count: 1, monitoring_status: "complete"}],
      pagination: {page, page_size: 12, total: 2, pages: 2}});
  }
  if (url.pathname === "/api/registry/reports/2026-08-10") {
    return registryResponse(200, {report_date: "2026-08-10", report_title: "Report detail",
      monitoring: {sites_succeeded: 57, sites_checked: 57, sites_failed: 0}, articles: [
        {article_id: "article-1", title: "Climate pricing", pillar: "B",
          section: "Pillar B", publisher: "Example"}
      ]});
  }
  if (url.pathname === "/api/registry/articles") {
    if (window.__registryScenario === "empty") {
      return registryResponse(200, {items: [], pagination: {page: 1, page_size: 20, total: 0, pages: 0}});
    }
    const page = Number(url.searchParams.get("page"));
    return registryResponse(200, {items: [{article_id: window.__missingArticle ? "missing" : "article-1",
      title: window.__missingArticle ? "Missing article" : (page === 1 ? "Climate pricing" : "Page two article"), publisher: "Example",
      last_seen: "2026-08-10", report_summary: "Pricing summary"}],
      pagination: {page, page_size: 20, total: 2, pages: 2}});
  }
  if (url.pathname === "/api/registry/articles/article-1") {
    return registryResponse(200, {article_id: "article-1", title: "Climate pricing",
      original_url: "https://example.com/article", canonical_url: "https://example.com/article",
      publisher: "Example", source: "example.com", first_seen: "2026-08-03", last_seen: "2026-08-10",
      display_policy: "summary_excerpt", latest_fetch: {fetch_status: "success"},
      content: {supporting_excerpt: "Supporting evidence", content_type: "text/html",
        extraction_method: "html-to-markdown", fetched_at: "2026-08-13"},
      enrichment: {summary: "Enriched summary", categories: ["Insurance"], keywords: ["pricing"],
        language: "en", generator: {kind: "deterministic", name: "rules", version: "1", generated_at: "2026-08-13"}},
      appearances: [{report_title: "Report detail", report_date: "2026-08-10", pillar: "B", section: "Pillar B"}]});
  }
  return registryResponse(404, {detail: "Registry record not found."});
};
"""


def _new_page(browser, scenario: str):
    page = browser.new_page()
    network_audit = {
        "external_attempted": [],
        "external_aborted": [],
        "external_completed": [],
    }
    page.add_init_script(MOCK_FETCH + f'\nwindow.__registryScenario = "{scenario}";')

    def record_response(response):
        if not response.url.startswith("http://archive.test/"):
            network_audit["external_completed"].append(response.url)

    page.on("response", record_response)

    def route_request(route):
        url = route.request.url
        if not url.startswith("http://archive.test/"):
            network_audit["external_attempted"].append(url)
            route.abort()
            network_audit["external_aborted"].append(url)
            return
        path = url.removeprefix("http://archive.test")
        if path == "/":
            route.fulfill(content_type="text/html", body=INDEX)
        elif path == "/showcase/app.js":
            route.fulfill(content_type="text/javascript", body=SCRIPT)
        elif path == "/showcase/styles.css":
            route.fulfill(content_type="text/css", body=STYLES)
        else:
            route.abort()

    page.route("**/*", route_request)
    page.goto("http://archive.test/")
    return page, network_audit


def _visible(page, text: str) -> None:
    page.get_by_text(text, exact=True).wait_for(state="visible")


def _assert_network_isolation(page, audit) -> None:
    page.wait_for_timeout(50)
    assert audit["external_attempted"], "fixture should exercise the external-font guard"
    assert audit["external_aborted"] == audit["external_attempted"]
    assert audit["external_completed"] == []


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("registry browser smoke requires: pip install playwright==1.62.1", file=sys.stderr)
        return 2

    chrome = next((candidate for candidate in CHROME_CANDIDATES if candidate.is_file()), None)
    if chrome is None:
        print("registry browser smoke requires SYSTEM_CHROME or local Chrome/Chromium", file=sys.stderr)
        return 2
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(chrome), headless=True)

        page, audit = _new_page(browser, "loading")
        page.get_by_role("tab", name="Archive").click()
        _visible(page, "Checking the historical archive…")
        page.evaluate("window.__releaseRegistry()")
        _visible(page, "The archive is not connected yet. Chat and the wiki remain available.")
        page.get_by_role("tab", name="Chat").click()
        page.get_by_role("heading", name="Climate Monitor Wiki Agent").wait_for()
        _assert_network_isolation(page, audit)
        page.close()

        page, audit = _new_page(browser, "empty")
        page.get_by_role("tab", name="Archive").click()
        _visible(page, "No historical reports are available.")
        page.get_by_role("button", name="Article Archive").click()
        _visible(page, "No articles match these filters.")
        _assert_network_isolation(page, audit)
        page.close()

        page, audit = _new_page(browser, "populated")
        page.get_by_role("tab", name="Archive").click()
        page.get_by_role("button", name="Report page 1", exact=False).wait_for()
        page.get_by_role("button", name="Next").first.click()
        page.get_by_role("button", name="Report page 2", exact=False).wait_for()
        page.get_by_role("button", name="Previous").first.click()
        page.get_by_role("button", name="Report page 1", exact=False).click()
        page.get_by_role("heading", name="Report detail").wait_for()
        page.get_by_role("button", name="Climate pricing").click()
        _visible(page, "Enriched summary")
        _visible(page, "Supporting evidence")
        page.get_by_label("Search articles").fill("climate 100%")
        page.get_by_label("Publisher hostname").fill("example.com")
        page.get_by_label("Pillar").select_option("B")
        page.get_by_role("button", name="Apply").click()
        page.wait_for_function("window.__registryRequests.some(x => x.path.includes('query=climate+100%25') && x.path.includes('source=example.com') && x.path.includes('pillar=B'))")
        page.get_by_role("button", name="Next").last.click()
        page.get_by_role("button", name="Page two article").wait_for()
        page.get_by_role("button", name="Previous").last.click()
        page.evaluate("window.__missingArticle = true")
        page.get_by_role("button", name="Apply").click()
        page.get_by_role("button", name="Missing article").click()
        page.get_by_role("heading", name="Article unavailable").wait_for()
        assert page.evaluate("window.__registryRequests.every(x => x.method === 'GET')")
        _assert_network_isolation(page, audit)
        page.close()

        browser.close()
    print("registry browser smoke: 3 scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
