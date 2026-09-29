---
name: climate-site-unfccc
description: "Use for UNFCCC (unfccc) site acquisition and coverage in climate_monitor_wiki."
---

# UNFCCC source skill

Use this skill only for `source_key: unfccc`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `unfccc` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). YAML defines the current seeds and allowed paths; do not substitute another source that shares the host.

**Current evidence level:** browser/manual only. The normal browser displays current UNFCCC news, documents and exact article bodies. Runtime HTTP receives HTML but the shared transformer rejects it, so discovery returns no candidates. No successful Runtime SiteSkill was produced.

## Isolated checks — 2026-09-28

The existing sitemap seed `https://unfccc.int/sitemap.xml` was incomplete (0 candidates; summary SHA-256 `94d6c81c073e3e40f62b2c1a6f761e9b58258e729271024f8df2baa898730bc6`). Errors included `discovery.feed_malformed`, `web_http.url_redacted` and budget exhaustion. Its original run ledger was not persisted, so do not infer which sitemap entries caused the exhaustion.

The browser-readable homepage linked to `/news` and `/documents`, both already represented in the reviewed include paths. An in-memory one-seed `/news` run received HTTP 200 but failed `transform.ineligible_quality` and `discovery.no_candidates` (0 rows; summary SHA-256 `1282fdbc0c7b9d061768016578a4368f69dee4577f34769a70bc83d7c8579260`). A one-seed exact article discovery probe returned the same transform and candidate failures (summary SHA-256 `7408e8d97decae511b4c90825732a6064d02c31ddcaf38203d919473ffc06890`). Both used only `acquisition.web_http`, `transform.simple_html_markdown` and `discovery.html_links`.

## Browser article route — 2026-09-28

The official homepage and exact article were readable in the normal browser. The verified article is [UN Climate Chief major keynote: thanks to global renewables, humanity avoided almost half a trillion US dollars in fossil fuel costs last year](https://unfccc.int/news/un-climate-chief-major-keynote-thanks-to-global-renewables-humanity-avoided-almost-half-a-trillion), dated 21 September 2026. The page renders the title, date, category and full speech transcript. It covers renewable deployment and investment, climate finance, energy costs and climate-related economic risks.

The governed exact reader got HTTP 200 with robots allowed and 2,862 bytes, but `transform.simple_html_markdown` returned `transform.ineligible_quality`; no Markdown artifact was produced (attempt `job-cf1354ee194e494aac7e408c49e975e5`). The browser view confirms the content exists, but it does not verify that the Runtime can convert it. For manual review, use the exact page and preserve title, date, source URL and the main transcript; do not copy the site's share and related-content blocks as story text.

The temporary homepage seed failed with `acquisition.script_only`; cloakbrowser reported `eligibility.not_installed`, and Playwright was out of scope for `/`. Do not broaden the root path or add a scraper. TypeSafe `jev-1.13.0` selected the exact article as a bounded probe (confidence 0.56), then recommended recording browser/manual-only access and keeping scope unchanged (confidence 0.67). The existing scope excludes `/calendar` and `/events`; no meeting page or agenda was tested. No content was written to `sources/`, no report was generated, and Registry was not run. Keep Runtime SiteSkill and SiteState outside Git.

## Hermes production guidance

- Browser-visible news and documents returned HTTP content to the Runtime, but the shared HTML-to-Markdown transform rejected it and produced no reliable candidates. Keep this as an upstream transform gap; do not add a UNFCCC-only fetcher or claim the browser view as acquired Markdown.
