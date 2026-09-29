---
name: climate-site-imf
description: "Use for IMF (imf) site acquisition and coverage in climate_monitor_wiki."
---

# IMF source skill

Use this skill only for `source_key: imf`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `imf` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** browser/manual article access is verified; all configured seeds and one exact article are blocked by Runtime robots policy. No successful runtime SiteSkill was produced.

The 2026-05-14 scope note is historical. Current 2026-09-28 receipts deny the same origin through `robots.forbidden`, including an exact article request. Do not treat browser visibility or a different User-Agent as Runtime permission.

## Isolated seed check: 2026-09-28T16:03:22.341659+00:00 (UTC)

VM application revision `2aea05d`; 0/4 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `d7167b635817b2c22b6c51834f9b6a8ece4077e77c58dfb4bcfb48c842c57544`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.imf.org/`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.imf.org/en/topics/climate-change`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.imf.org/en/publications`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.imf.org/en/news`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Browser and exact-page check: 2026-09-28

The official [climate-change topic page](https://www.imf.org/en/topics/climate-change) was readable in the browser and showed the IMF's climate-policy role and a News/Publications switcher. The IMF's own News Search page returned 275 matches for `climate finance`. The strongest current climate-finance lead inspected was the July 10, 2026 Tanzania Resilience and Sustainability Facility (RSF) press release:

- URL: `https://www.imf.org/en/news/articles/2026/07/10/pr26244-tanzania-imf-exec-board-completes-6th-7th-rev-ecf-arr-3rd-4th-rev-rsf-arr`
- Browser-visible title/date: “IMF Executive Board Completes the Sixth and Seventh Reviews Under the Extended Credit Facility Arrangement and Third and Fourth Reviews Under the Resilience and Sustainability Facility Arrangement with Tanzania”; July 10, 2026.
- The body states the RSF supports reforms to enhance resilience to climate change and mobilize climate finance. It also includes an economic-indicator table.

The exact URL was then attempted once through the governed article reader. Runtime returned `robots.forbidden`; the robots endpoint returned HTTP 403, the article received 0 bytes, and no Markdown was produced. Attempt ID: `job-30f7ff56e60645309244eec8532d3629`; evidence JSON SHA-256: `4565d3ffd887a6a046ed3ac0521a176b8ca5fdc2177cb54870db067a3f2d2548`.

TypeSafe `jev-1.13.0` selected one exact attempt as a bounded diagnostic (confidence 0.76), then classified IMF `browser_manual_only` (confidence 1.0). Do not retry through a custom scraper, alternate User-Agent or unverified host. No scope change, site-specific script, ingestion, meeting extraction, weekly reporting or Registry sync is justified by this evidence.

## Remaining checks

- Use the browser-visible climate topic page and official News Search for manual research leads; treat those leads as uncollected until a governed path is available.
- Revisit automation only if the governed Runtime's current robots policy changes or the IMF provides an officially permitted feed/API path.
- No meeting page was checked; report generation and Registry remain unverified.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- Configured seeds and an exact current article were rejected by robots.forbidden in the governed Runtime. Stop on that result; browser visibility or a changed user-agent does not grant permission to acquire the content.
