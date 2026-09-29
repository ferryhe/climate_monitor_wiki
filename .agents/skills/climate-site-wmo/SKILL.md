---
name: climate-site-wmo
description: "Use for WMO (wmo) site acquisition and coverage in climate_monitor_wiki."
---

# WMO source skill

Use this skill only for `source_key: wmo`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `wmo` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). YAML defines the current seeds and allowed paths; do not substitute another source that shares the host.

**Current evidence level:** partial automated access. Two of three configured seeds succeeded, and a known current report detail page produced good Markdown. The configured publication-series index is stale; its official filtered edition listing is not Runtime-readable with its current query string. Meetings, weekly output and Registry remain unverified.

## Seed and publication checks — 2026-09-28

The fresh seed run completed 2/3 with three candidate rows (summary SHA-256 `bb3aab75ba0097e37c4e5b23da82d97c5cc7afaef0cc8af105a69dc1be4a65e0`). The climate topic and news seeds succeeded with `acquisition.web_http`, `discovery.html_links` and `transform.simple_html_markdown`. The `https://wmo.int/publication-series` seed was rejected with `gateway.http_status`. `fetch_mode: browser` is a YAML hint only; those receipts did not use a browser.

The normal browser resolves the climate topic route to [Climate](https://wmo.int/themes/climate). The configured publication-series index resolves to `/resources/publication-series`, which is a 404. The Climate page links to the current [State of the Global Climate series](https://wmo.int/publication-series/state-of-global-climate), whose detail page links to its 2025 edition.

An in-memory probe used only the exact series page as a seed, without saving a checkpoint or changing repo scope. It succeeded and returned one candidate, `https://wmo.int/news`, not the annual report; summary SHA-256 `98273babd9283670e0ebb84d5c2441ef6302902dbb1822d0e3ff390dc3e3387f`. Do not replace the failing seed with this page solely from that result.

The exact [State of the Global Climate 2025 detail page](https://wmo.int/publication-series/state-of-global-climate/state-of-global-climate-2025) was read through the governed article reader: HTTP 200, robots allowed, 125,222 source bytes, 6,471 Markdown characters, SHA-256 `4173051554615ede4ff72149c871b60bf6f757d424bdc2fdf23a0c23ebeb055d`, attempt `job-3876ec7b46aa42d89c3e666d35206c5c`. The page is dated 23 March 2026 and retains its key messages and summary. Its Full Report link points to `library.wmo.int`; the PDF was not downloaded. Evidence is outside Git at `$HOME/climate-site-pilot-20260928/wmo-exact-report/` and ignored `.tmp/wmo-evidence/`.

The official “View all editions” link uses a query string at `https://wmo.int/resources/publications?f%5B0%5D=publication_series%3A669`. The in-memory Runtime probe stopped before any HTTP request with `web_http.url_redacted` (summary SHA-256 `47deb8a1c62c8c5566d6b67c1b68e57a2a97d609cf6a6d0c9d4af2465a51f203`). TypeSafe `jev-1.13.0` recommends relying on exact known report URLs and recording the filtered-list limitation; it rejected scope changes and a WMO-specific scraper. No seed/path change, report import or Registry sync was made. The current scope excludes `/events`; no meeting detail was checked.

For manual discovery, use WMO Climate → Related publications, or WMO News Portal's Climate topic facet. For exact known report URLs, the governed reader and shared HTML-to-Markdown transform work. Preserve the source URL, publication date, title, key messages and official Full Report link. Keep Runtime SiteSkill and SiteState outside Git.

## Hermes production guidance

- The climate and news seeds and one exact State of the Global Climate report page were readable; the configured series index is stale and a filtered query URL was rejected before fetch. Use the exact reviewed report route, preserve the listing gap, and keep meetings outside scope unless explicitly bound.
