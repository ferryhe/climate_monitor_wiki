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
window.__publisherTruncated = false;
window.__deferNextReport = null;
window.__pendingReportResponses = [];
const registryResponse = (status, data) => Promise.resolve(new Response(
  JSON.stringify(data), {status, headers: {"Content-Type": "application/json"}}
));
const reportResponse = (date, data) => {
  const deferred = window.__deferNextReport;
  if (!deferred || deferred.date !== date) return registryResponse(200, data);
  window.__deferNextReport = null;
  return new Promise((resolve) => {
    window.__pendingReportResponses.push(() => resolve(new Response(
      JSON.stringify(deferred.status >= 400 ? {detail: "Deferred report error."} : data),
      {status: deferred.status, headers: {"Content-Type": "application/json"}}
    )));
  });
};
window.fetch = (input, options = {}) => {
  const url = new URL(input, window.location.href);
  window.__registryRequests.push({method: options.method || "GET", path: url.pathname + url.search});
  if (url.pathname === "/api/config" && window.__registryScenario === "service_unavailable") {
    return registryResponse(503, {detail: "unavailable"});
  }
  if (url.pathname === "/api/config") return registryResponse(200, {
    documents: [{path: "wiki/climate-monitor-2026-08-10.md", title: "climate-monitor-2026-08-10",
      type: "daily", date: "2026-08-10", words: 120, links: [],
      concepts: [{label: "Climate pricing"}]},
      {path: "wiki/topics/climate-pricing.md", title: "Climate pricing topic",
        type: "topic", date: null, words: 80, links: [], concepts: [{label: "Climate pricing"}]}],
    concepts: [{label: "Climate pricing", document_count: 5}],
    graphs: {notes: null, keywords: {static_layout: true, nodes: [
      {id: "wiki/climate-monitor-2026-08-10.md", refPath: "wiki/climate-monitor-2026-08-10.md",
        label: "2026-08-10", kind: "note", type: "daily"},
      {id: "keyword:climate-pricing", label: "Climate pricing", kind: "keyword", type: "keyword", weight: 5}
    ], links: [{source: "wiki/climate-monitor-2026-08-10.md", target: "keyword:climate-pricing"}]}},
    prompt_starters: [],
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
      return registryResponse(200, {available: true, schema_version: 4, reports: 0,
        articles: 0, discoveries: 0, latest_report_date: null});
    }
    return registryResponse(200, {available: true, schema_version: 4, reports: 2,
      articles: 2, discoveries: 2, latest_report_date: "2026-08-10"});
  }
  if (url.pathname === "/api/registry/reports") {
    if (window.__registryScenario === "empty") {
      return registryResponse(200, {items: [], pagination: {page: 1, page_size: 12, total: 0, pages: 0}});
    }
    const page = Number(url.searchParams.get("page"));
    return registryResponse(200, {items: [{report_date: page === 1 ? "2026-08-10" : "2026-08-03",
      report_title: `Report page ${page}`, article_count: page === 1 ? 2 : 1, monitoring_status: "complete"}],
      pagination: {page, page_size: 12, total: 2, pages: 2}});
  }
  if (url.pathname === "/api/registry/publishers") {
    return registryResponse(200, {items: [{hostname: "example.com", label: "example"}],
      truncated: window.__publisherTruncated});
  }
  if (url.pathname === "/api/registry/reports/2026-08-10") {
    const defaultBriefing = {
      executive_summary: [
        "Pricing pressure increased across exposed markets.",
        "Insurers adjusted exposure as climate signals intensified."
      ],
      monitoring_snapshot: {
        sites_checked: 57, sites_succeeded: 57, sites_failed: 0,
        pillar_a_updates: 9, pillar_b_updates: 17,
        notes: ["All monitored sites completed successfully.", "Evidence was checked against source articles."]
      }
    };
    const defaultPdf = {
      filename: "climate-monitor-2026-08-10.pdf",
      download_url: "/api/registry/reports/2026-08-10/pdf"
    };
    return reportResponse("2026-08-10", {report_date: "2026-08-10",
      report_title: window.__reportTitle ?? "Report detail",
      monitoring: {sites_succeeded: 57, sites_checked: 57, sites_failed: 0},
      executive_summary: ["Old operational summary must not be repeated."],
      report_briefing: Object.hasOwn(window, "__reportBriefingOverride")
        ? window.__reportBriefingOverride : defaultBriefing,
      report_pdf: Object.hasOwn(window, "__reportPdfOverride")
        ? window.__reportPdfOverride : defaultPdf,
      articles: [
        {article_id: "article-1", title: "Climate pricing", pillar: "B",
          section: "Pillar B", publisher: "Example", summary: "Weekly article summary",
          summary_provenance: window.__reportSummaryProvenance ?? "content_enrichment",
          categories: ["Insurance"], keywords: ["pricing"],
          source_annotation: {source_basis: window.__reportSourceBasis ?? "original_content", source_url: "https://example.com/article",
            generated_on: "2026-08-17"}},
        {article_id: "article-2", title: "Flood regulation", pillar: "A",
          section: "Pillar A", publisher: "Policy Example", summary: "Second weekly article summary",
          summary_provenance: "publisher_excerpt_annotation",
          categories: ["Regulation"], keywords: ["flood"],
          source_annotation: {source_basis: "publisher_excerpt", source_url: "https://policy.example/flood",
            generated_on: "2026-08-17"}}
      ]});
  }
  if (url.pathname === "/api/registry/reports/2026-08-03") {
    return reportResponse("2026-08-03", {report_date: "2026-08-03", report_title: "Fallback report",
      monitoring: {sites_succeeded: 41, sites_checked: 43, sites_failed: 2},
      executive_summary: ["Markdown operational summary remains available."],
      report_briefing: null, report_pdf: null,
      articles: [{article_id: "article-1", title: "Climate pricing", pillar: "B",
        section: "Pillar B", publisher: "Example", summary: "Fallback weekly article summary",
        summary_provenance: "content_enrichment",
        source_annotation: {source_basis: "original_content", source_url: "https://example.com/article",
          generated_on: "2026-08-17"}}]});
  }
  if (url.pathname === "/api/registry/reports/2026-07-27") {
    return registryResponse(404, {detail: "Registry report not found."});
  }
  if (url.pathname === "/api/registry/reports/2026-07-20") {
    return registryResponse(500, {detail: "Internal error"});
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
      categories: ["Regulation"], keywords: ["premium"],
      summary_provenance: "content_enrichment",
      source_annotation: {source_basis: window.__articleSourceBasis ?? "original_content",
        source_url: window.__articleSourceUrl ?? "https://example.com/article",
        generated_on: "2026-08-17"},
      enrichment: {summary: "Enriched summary", categories: ["Legacy category"], keywords: ["legacy"],
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
        page.get_by_role("tab", name="Historical Reports").wait_for()
        assert page.get_by_role("tab", name="Historical Reports").get_attribute("aria-selected") == "true"
        _visible(page, "Source-only mode")
        _visible(page, "Checking the historical archive…")
        page.evaluate("window.__releaseRegistry()")
        _visible(page, "The archive is not connected yet. Chat and the wiki remain available.")
        page.get_by_role("tab", name="Chat").click()
        page.get_by_role("heading", name="Climate Monitor Wiki Agent").wait_for()
        assert page.locator("#activeContextBadge").is_hidden()
        page.get_by_role("button", name="Open Historical Reports").click()
        page.get_by_role("heading", name="Historical Reports", exact=True).first.wait_for()
        _assert_network_isolation(page, audit)
        page.close()

        page, audit = _new_page(browser, "empty")
        _visible(page, "No historical reports are available.")
        page.get_by_role("button", name="Article Database").click()
        _visible(page, "No articles match these filters.")
        _assert_network_isolation(page, audit)
        page.close()

        page, audit = _new_page(browser, "populated")
        page.get_by_role("button", name="Report page 1", exact=False).wait_for()
        page.get_by_role("button", name="Next").first.click()
        page.get_by_role("button", name="Report page 2", exact=False).wait_for()
        page.get_by_role("button", name="Previous").first.click()
        page.get_by_role("button", name="Report page 1", exact=False).click()
        page.get_by_role("heading", name="Report detail").wait_for()
        _visible(page, "Executive Summary")
        _visible(page, "Pricing pressure increased across exposed markets.")
        _visible(page, "Insurers adjusted exposure as climate signals intensified.")
        assert page.get_by_text("Old operational summary must not be repeated.", exact=True).count() == 0
        briefing_summary = page.locator("#registryBriefingExecutiveSummaryItems")
        assert briefing_summary.locator("p").count() == 2
        assert page.locator("#registryExecutiveSummaryItems").is_hidden()
        assert page.locator("#registryExecutiveSummaryItems li").count() == 0
        snapshot = page.locator("#registryMonitoringSnapshot")
        snapshot.get_by_role("heading", name="Monitoring Snapshot").wait_for()
        assert snapshot.locator("dl").count() == 1
        assert page.viewport_size["width"] >= 1080
        assert page.locator("#registrySnapshotMetrics").evaluate(
            "node => getComputedStyle(node).gridTemplateColumns.split(' ').length"
        ) == 3
        assert snapshot.locator("dt").all_text_contents() == [
            "Sites checked", "Succeeded", "Failed", "Pillar A updates", "Pillar B updates"
        ]
        assert snapshot.locator("dd").all_text_contents() == ["57", "57", "0", "9", "17"]
        _visible(page, "All monitored sites completed successfully.")
        _visible(page, "Evidence was checked against source articles.")
        assert page.evaluate(
            "document.querySelector('#registrySnapshotMetrics').compareDocumentPosition("
            "document.querySelector('#registrySnapshotNotes')) & Node.DOCUMENT_POSITION_FOLLOWING"
        )
        pdf_link = page.get_by_role("link", name="Download report PDF")
        pdf_link.wait_for()
        assert pdf_link.get_attribute("href") == "/api/registry/reports/2026-08-10/pdf"
        assert pdf_link.get_attribute("download") == "climate-monitor-2026-08-10.pdf"
        assert page.locator("#registryReports a").count() == 0
        assert page.evaluate(
            "['registryReportTitle', 'registryReportMeta', 'registryReportPdf', "
            "'registryExecutiveSummary', 'registryMonitoringSnapshot', 'registryReportArticlesTitle']"
            ".map(id => document.getElementById(id)).every((node, index, nodes) => "
            "index === 0 || Boolean(nodes[index - 1].compareDocumentPosition(node) & "
            "Node.DOCUMENT_POSITION_FOLLOWING))"
        )
        _visible(page, "Weekly article summary")
        _visible(page, "Second weekly article summary")
        assert page.locator("#registryReportArticles .registry-article-link").all_text_contents() == [
            "Climate pricing", "Flood regulation"
        ]
        assert page.locator("#registryReportArticles .registry-card__summary").all_text_contents() == [
            "Weekly article summary", "Second weekly article summary"
        ]
        assert page.locator("#registryReportArticles .registry-card__meta").all_text_contents() == [
            "Pillar B · Example · Captured content",
            "Pillar A · Policy Example · Publisher excerpt",
        ]
        _visible(page, "Pillar B · Example · Captured content")
        assert page.get_by_text("Pillar B · Example · Original source", exact=True).count() == 0

        page.evaluate(
            "window.__deferNextReport = {date: '2026-08-10', status: 200}; "
            "void loadRegistryReport('2026-08-10')"
        )
        page.get_by_role("heading", name="Loading report…").wait_for()
        assert page.locator("#registryExecutiveSummary").is_hidden()
        assert page.locator("#registryMonitoringSnapshot").is_hidden()
        assert page.locator("#registryReportPdf").is_hidden()
        assert page.locator("#registryReportPdf").get_attribute("href") is None
        assert page.locator("#registryReportPdf").get_attribute("download") is None
        assert page.locator("#registryExecutiveSummaryItems").text_content() == ""
        assert page.locator("#registrySnapshotMetrics").text_content() == ""
        assert page.locator("#registrySnapshotNotes").text_content() == ""
        page.evaluate("loadRegistryReport('2026-08-03')")
        page.get_by_role("heading", name="Fallback report").wait_for()
        _visible(page, "Markdown operational summary remains available.")
        assert page.locator("#registryBriefingExecutiveSummaryItems").is_hidden()
        assert page.locator("#registryExecutiveSummaryItems").is_visible()
        assert page.locator("#registryExecutiveSummaryItems li").all_text_contents() == [
            "Markdown operational summary remains available."
        ]
        assert page.locator("#registryMonitoringSnapshot").is_hidden()
        assert page.locator("#registryReportPdf").is_hidden()
        assert page.locator("#registryReportPdf").get_attribute("href") is None
        page.evaluate("window.__pendingReportResponses.shift()()")
        page.wait_for_timeout(50)
        assert page.get_by_role("heading", name="Fallback report").is_visible()
        assert page.get_by_text("Markdown operational summary remains available.", exact=True).is_visible()
        assert page.get_by_text("Pricing pressure increased across exposed markets.", exact=True).count() == 0
        assert page.locator("#registryMonitoringSnapshot").is_hidden()
        assert page.locator("#registryReportPdf").is_hidden()

        page.evaluate(
            "window.__reportTitle = 'Older same-date success'; "
            "window.__deferNextReport = {date: '2026-08-10', status: 200}; "
            "void loadRegistryReport('2026-08-10')"
        )
        page.evaluate(
            "window.__reportTitle = 'Fresh same-date success'; loadRegistryReport('2026-08-10')"
        )
        page.get_by_role("heading", name="Fresh same-date success").wait_for()
        page.evaluate("window.__pendingReportResponses.shift()()")
        page.wait_for_timeout(50)
        assert page.get_by_role("heading", name="Fresh same-date success").is_visible()
        assert page.get_by_text("Pricing pressure increased across exposed markets.", exact=True).is_visible()
        assert page.get_by_role("link", name="Download report PDF").is_visible()

        page.evaluate(
            "window.__reportTitle = 'Older same-date error'; "
            "window.__deferNextReport = {date: '2026-08-10', status: 500}; "
            "void loadRegistryReport('2026-08-10')"
        )
        page.evaluate(
            "window.__reportTitle = 'Fresh after late error'; "
            "window.__deferNextReport = {date: '2026-08-10', status: 200}; "
            "void loadRegistryReport('2026-08-10')"
        )
        page.get_by_role("heading", name="Loading report…").wait_for()
        assert page.locator("#registryReportDetail").get_attribute("aria-busy") == "true"
        page.evaluate("window.__pendingReportResponses.shift()()")
        page.wait_for_timeout(50)
        assert page.get_by_role("heading", name="Loading report…").is_visible()
        assert page.locator("#registryReportDetail").get_attribute("aria-busy") == "true"
        assert page.locator("#registryExecutiveSummary").is_hidden()
        page.evaluate("window.__pendingReportResponses.shift()()")
        page.get_by_role("heading", name="Fresh after late error").wait_for()
        assert page.locator("#registryReportDetail").get_attribute("aria-busy") == "false"
        assert page.get_by_role("link", name="Download report PDF").is_visible()
        page.evaluate("delete window.__reportTitle")

        page.evaluate(
            "window.__deferNextReport = {date: '2026-08-10', status: 200}; "
            "void loadRegistryReport('2026-08-10'); resetHistoricalReportDetail()"
        )
        page.get_by_role("heading", name="Select a report").wait_for()
        page.evaluate("window.__pendingReportResponses.shift()()")
        page.wait_for_timeout(50)
        assert page.get_by_role("heading", name="Select a report").is_visible()
        assert page.locator("#registryReportDetail").get_attribute("aria-busy") == "false"
        assert page.locator("#registryExecutiveSummary").is_hidden()
        assert page.locator("#registryReportPdf").is_hidden()

        page.evaluate("loadRegistryReport('2026-07-27')")
        page.get_by_role("heading", name="Report unavailable").wait_for()
        assert page.locator("#registryExecutiveSummary").is_hidden()
        assert page.locator("#registryMonitoringSnapshot").is_hidden()
        assert page.locator("#registryReportPdf").is_hidden()
        assert page.locator("#registryReportPdf").get_attribute("href") is None
        assert page.locator("#registryReportArticles").text_content() == ""
        page.evaluate("loadRegistryReport('2026-07-20')")
        page.get_by_role("heading", name="Report unavailable").wait_for()
        assert page.locator("#registryExecutiveSummary").is_hidden()
        assert page.locator("#registryMonitoringSnapshot").is_hidden()
        assert page.locator("#registryReportPdf").is_hidden()
        page.evaluate("loadRegistryReport('2026-08-10')")
        page.get_by_role("heading", name="Report detail").wait_for()
        _visible(page, "Weekly article summary")

        valid_snapshot = {
            "sites_checked": 57,
            "sites_succeeded": 57,
            "sites_failed": 0,
            "pillar_a_updates": 9,
            "pillar_b_updates": 17,
            "notes": ["A valid note."],
        }
        malformed_briefings = [
            {"executive_summary": ["Valid narrative.", "   "], "monitoring_snapshot": valid_snapshot},
            {"executive_summary": "Not an array.", "monitoring_snapshot": valid_snapshot},
            {
                "executive_summary": ["Valid narrative."],
                "monitoring_snapshot": {**valid_snapshot, "notes": ["Valid note.", ""]},
            },
        ]
        for malformed_briefing in malformed_briefings:
            page.evaluate(
                "briefing => { window.__reportBriefingOverride = briefing; "
                "return loadRegistryReport('2026-08-10'); }",
                malformed_briefing,
            )
            _visible(page, "Old operational summary must not be repeated.")
            assert page.locator("#registryExecutiveSummaryItems li").all_text_contents() == [
                "Old operational summary must not be repeated."
            ]
            assert page.locator("#registryBriefingExecutiveSummaryItems").is_hidden()
            assert page.locator("#registryMonitoringSnapshot").is_hidden()
            assert page.locator("#registryReportPdf").is_hidden()

        page.evaluate("delete window.__reportBriefingOverride")
        invalid_pdfs = [
            {"filename": " climate-monitor-2026-08-10.pdf", "download_url": "/api/registry/reports/2026-08-10/pdf"},
            {"filename": "wrong.pdf", "download_url": "/api/registry/reports/2026-08-10/pdf"},
            {"filename": "climate-monitor-2026-08-10.pdf", "download_url": " /api/registry/reports/2026-08-10/pdf"},
            {"filename": "climate-monitor-2026-08-10.pdf", "download_url": "javascript:alert(1)"},
            {"filename": "climate-monitor-2026-08-10.pdf", "download_url": "https://foreign.example/report.pdf"},
            {"filename": "climate-monitor-2026-08-10.pdf", "download_url": "/api/registry/reports/2026-08-03/pdf"},
            {"filename": "climate-monitor-2026-08-10.pdf", "download_url": "/api/registry/reports/2026-08-10/pdf?raw=1"},
        ]
        for invalid_pdf in invalid_pdfs:
            page.evaluate(
                "pdf => { window.__reportPdfOverride = pdf; return loadRegistryReport('2026-08-10'); }",
                invalid_pdf,
            )
            _visible(page, "Pricing pressure increased across exposed markets.")
            assert page.locator("#registryReportPdf").is_hidden()
            assert page.locator("#registryReportPdf").get_attribute("href") is None
            assert page.locator("#registryReportPdf").get_attribute("download") is None

        page.evaluate(
            "window.__reportPdfOverride = {filename: 'climate-monitor-2026-08-10.pdf', "
            "download_url: window.location.origin + '/api/registry/reports/2026-08-10/pdf'}; "
            "loadRegistryReport('2026-08-10')"
        )
        absolute_pdf_link = page.get_by_role("link", name="Download report PDF")
        absolute_pdf_link.wait_for()
        assert absolute_pdf_link.get_attribute("href") == (
            "http://archive.test/api/registry/reports/2026-08-10/pdf"
        )
        page.evaluate("delete window.__reportPdfOverride; loadRegistryReport('2026-08-10')")
        page.get_by_role("heading", name="Report detail").wait_for()

        page.evaluate(
            "window.__reportSummaryProvenance = 'official_replacement_annotation'; "
            "window.__reportSourceBasis = 'official_replacement'; "
            "loadRegistryReport('2026-08-10')"
        )
        _visible(page, "Pillar B · Example · Official replacement")
        assert page.get_by_text("Pillar B · Example · Original source", exact=True).count() == 0
        page.evaluate(
            "window.__reportSummaryProvenance = 'future_provenance'; "
            "window.__reportSourceBasis = 'original_content'; "
            "loadRegistryReport('2026-08-10')"
        )
        page.wait_for_function(
            "!document.querySelector('#registryReportArticles').textContent.includes('Captured content')"
        )
        assert page.get_by_text("Pillar B · Example · Original source", exact=True).count() == 0
        assert page.get_by_text("Pillar B · Example · Captured content", exact=True).count() == 0
        assert page.get_by_text("Pillar B · Example · Official replacement", exact=True).count() == 0
        page.get_by_role("button", name="Climate pricing").click()
        _visible(page, "Enriched summary")
        _visible(page, "Supporting evidence")
        _visible(page, "Regulation")
        _visible(page, "premium")
        _visible(page, "Summary generated from captured article content")
        assert page.get_by_text("Summary based on the linked original content", exact=False).count() == 0
        assert page.get_by_text("Legacy category", exact=True).count() == 0
        assert page.get_by_text("legacy", exact=True).count() == 0
        assert page.get_by_text("Display", exact=True).count() == 0
        assert page.get_by_text("Content type", exact=True).count() == 0
        assert page.get_by_text("Extraction", exact=True).count() == 0
        assert page.get_by_text("Not captured", exact=True).count() == 0
        _visible(page, "Latest fetch")
        _visible(page, "Captured")
        page.evaluate(
            "window.__articleSourceBasis = 'official_replacement'; "
            "window.__articleSourceUrl = 'https://example.com/corrected-article'; "
            "loadRegistryArticle('article-1')"
        )
        corrected_link = page.get_by_role("link", name="Open official replacement")
        corrected_link.wait_for()
        assert corrected_link.get_attribute("href") == "https://example.com/corrected-article"
        page.get_by_label("Search articles").fill("climate 100%")
        page.get_by_label("Publisher").select_option(label="example")
        page.get_by_role("button", name="Apply").click()
        page.wait_for_function("window.__registryRequests.some(x => x.path.includes('query=climate+100%25') && x.path.includes('source=example.com') && !x.path.includes('pillar='))")
        page.evaluate("window.__publisherTruncated = true; loadRegistryPublishers()")
        page.get_by_label("Other hostname").wait_for(state="visible")
        page.get_by_label("Other hostname").fill("unlisted.example")
        page.get_by_role("button", name="Apply").click()
        page.wait_for_function("window.__registryRequests.some(x => x.path.includes('source=unlisted.example'))")
        page.get_by_role("button", name="Next").last.click()
        page.get_by_role("button", name="Page two article").wait_for()
        page.get_by_role("button", name="Previous").last.click()
        page.evaluate("window.__missingArticle = true")
        page.get_by_role("button", name="Apply").click()
        page.get_by_role("button", name="Missing article").click()
        page.get_by_role("heading", name="Article unavailable").wait_for()

        page.get_by_role("tab", name="Obsidian").click()
        assert page.get_by_role("button", name="Keywords").get_attribute("aria-pressed") == "true"
        graph_box = page.locator(".panel--graph").bounding_box()
        index_box = page.locator(".panel--table").bounding_box()
        assert graph_box and index_box and graph_box["y"] < index_box["y"]
        keyword_circle = page.locator("#graphSvg .graph-node--keyword circle")
        keyword_circle.wait_for()
        assert float(keyword_circle.get_attribute("r")) > 10
        keyword_button = page.get_by_role("button", name="Filter Page Index by keyword Climate pricing")
        keyword_button.focus()
        keyword_button.press("Enter")
        assert page.get_by_label("Search wiki pages").input_value() == "Climate pricing"
        page.get_by_role("cell", name="Climate pricing topic", exact=True).click()
        page.get_by_role("tab", name="Chat").click()
        _visible(page, "Focused note: Climate pricing topic")
        page.get_by_role("tab", name="Obsidian").click()
        page.get_by_role("cell", name="climate-monitor-2026-08-10", exact=True).click()
        page.get_by_role("link", name="Historical report · 2026-08-10").click()
        page.get_by_role("heading", name="Report detail").wait_for()
        assert page.url.endswith("#historical-report=2026-08-10")
        page.reload()
        page.get_by_role("heading", name="Report detail").wait_for()
        page.wait_for_function("window.__registryRequests.some(x => x.path.startsWith('/api/registry/reports?'))")
        assert sum(x["path"] == "/api/registry/status" for x in page.evaluate("window.__registryRequests")) == 1
        assert sum(x["path"] == "/api/registry/publishers" for x in page.evaluate("window.__registryRequests")) == 1
        assert sum(x["path"].startswith("/api/registry/reports?") for x in page.evaluate("window.__registryRequests")) == 1
        page.go_back()
        page.get_by_role("heading", name="Select a report").wait_for()
        assert page.url == "http://archive.test/"
        assert page.locator("#registryExecutiveSummary").is_hidden()
        assert page.locator("#registryMonitoringSnapshot").is_hidden()
        assert page.locator("#registryReportPdf").is_hidden()
        assert page.locator("#registryReportPdf").get_attribute("href") is None
        assert page.locator("#registryReportPdf").get_attribute("download") is None
        page.go_forward()
        page.get_by_role("heading", name="Report detail").wait_for()
        page.get_by_role("tab", name="Obsidian").click()
        page.get_by_role("cell", name="climate-monitor-2026-08-10", exact=True).click()
        page.get_by_role("tab", name="Chat").click()
        _visible(page, "Focused report: climate-monitor-2026-08-10")
        assert page.evaluate("window.__registryRequests.every(x => x.method === 'GET')")
        _assert_network_isolation(page, audit)
        page.close()

        page, audit = _new_page(browser, "populated")
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("button", name="Report page 1", exact=False).click()
        page.get_by_role("heading", name="Report detail").wait_for()
        mobile_detail = page.locator("#registryReportDetail").bounding_box()
        mobile_pdf = page.get_by_role("link", name="Download report PDF").bounding_box()
        mobile_snapshot = page.locator("#registryMonitoringSnapshot").bounding_box()
        assert mobile_detail and mobile_detail["x"] >= 0
        assert mobile_detail["x"] + mobile_detail["width"] <= 390
        assert mobile_pdf and mobile_pdf["x"] + mobile_pdf["width"] <= 390
        assert mobile_snapshot and mobile_snapshot["x"] + mobile_snapshot["width"] <= 390
        assert page.locator("#registrySnapshotMetrics").evaluate(
            "node => getComputedStyle(node).gridTemplateColumns.split(' ').length"
        ) == 1
        _visible(page, "Weekly article summary")
        page.get_by_role("button", name="Climate pricing").click()
        page.get_by_role("heading", name="Climate pricing", exact=True).wait_for()
        _visible(page, "Enriched summary")
        _assert_network_isolation(page, audit)
        page.close()

        page, audit = _new_page(browser, "service_unavailable")
        _visible(page, "Service unavailable")
        _assert_network_isolation(page, audit)
        page.close()

        browser.close()
    print("registry browser smoke: 5 scenarios passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
