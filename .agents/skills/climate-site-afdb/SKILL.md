---
name: climate-site-afdb
description: "Use for AFDB (afdb) site acquisition and coverage in climate_monitor_wiki."
---

# AFDB source skill

Use this skill only for `source_key: afdb`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `afdb` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** automated acquisition is blocked by `robots.forbidden`; no successful runtime SiteSkill was produced. The climate page and exact news articles are readable in a normal browser for manual review.

The scope review on 2026-05-14 recorded: Reviewed AfDB with browser_rendered; HTTP returns 403 and old initiative climate/documents paths returned 404.
Treat that as historical context until a current governed run confirms it. The reviewed scope requests browser mode; verify actual browser use from the governed attempt receipts, because that YAML value currently does not select the runtime tool.

## Isolated seed check: 2026-09-28T16:08:53.041648+00:00 (UTC)

VM application revision `2aea05d`; 0/4 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `89d7dd49bb0d2379feffd4fd1d5bfa06bfd0c8f7eb23013d9ff8db92e48c58e2`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.afdb.org/en`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.afdb.org/en/topics-and-sectors/sectors/climate-change`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.afdb.org/en/news-and-events`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.afdb.org/en/documents/publications`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Browser and exact article check: 2026-09-28 (UTC)

The public climate page `https://www.afdb.org/en/topics-and-sectors/sectors/climate-change` rendered a current “News and Events” list, publication links and dated stories in a normal browser. Its September 28 news item linked to `https://www.afdb.org/en/news-and-events/press-releases/cote-divoire-and-african-development-bank-group-launch-24-million-investment-strengthen-climate-resilience-and-restore-forest-landscapes-97094`. The article showed the title, `28-Sep-2026` date and readable body about USD 24.07 million of new Niger Basin program funding, forest restoration and climate resilience work.

The configured Runtime run returned 0/4 successes; the home, climate, news and publication seeds all returned `robots.forbidden`. TypeSafe slightly favored one exact governed article probe (confidence 0.36; choice probability 0.52). A fresh isolated Runtime tried that exact browser-verified article path, but `acquisition.web_http` was also rejected by `robots.forbidden`: 0 candidates and no SiteSkill. Evidence summary SHA-256: `e1e02e7111a8fe2d99407f6934d719fffd1cd015dd51f5608cef146f755be10f`; external evidence is under `/home/ubuntu/climate-site-pilot-20260928/afdb-article/`.

TypeSafe then selected `browser_manual_only` (confidence 1.0). Keep the normal browser as a manual reading path; do not add a browser scraper or expand scope. Automated acquisition needs an approved upstream policy or another official endpoint that is readable under the governed reader. No content was ingested and no Registry operation was run.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- Configured seeds and one exact browser-verified article were rejected by robots.forbidden in the governed Runtime. Preserve that as a blocked source; do not convert normal-browser visibility into acquisition success or try a scraper/browser bypass.
