---
name: climate-site-ngfs
description: "Use for NGFS (ngfs) site acquisition and coverage in climate_monitor_wiki."
---

# NGFS source skill

Use this skill only for `source_key: ngfs`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `ngfs` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** current publication is browser-readable, but all configured seeds and the exact detail page are blocked by Runtime robots policy. No successful runtime SiteSkill was produced.

The 2026-05-14 scope note is historical. Current 2026-09-28 receipts deny the same origin through `robots.forbidden`, including an exact current publication detail request. Do not treat browser visibility or direct same-origin downloads as Runtime permission.

## Isolated seed check: 2026-09-28T16:03:23.310509+00:00 (UTC)

VM application revision `2aea05d`; 0/4 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `81131c865f499ad5064b98dfbed7443d29cbbe685442c155afd5b9cc8b44d6e4`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.ngfs.net/`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.ngfs.net/sitemap.xml`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.ngfs.net/en/press-release`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.ngfs.net/en/news`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Browser and exact-page check: 2026-09-28

The official [NGFS publications directory](https://www.ngfs.net/en/publications-and-statistics/ngfs-publications) showed a current guide for prudential supervisors, published September 21 and updated September 24, 2026. Its official detail page describes a practical reference for integrating climate- and nature-related financial risks into supervision. The page exposes a 2.14 MB guide PDF and a 726.25 KB outreach-slide PDF through Download buttons. This is a browser route only; neither file was downloaded in this diagnostic.

The exact detail page was attempted once with the governed article reader. Runtime returned `robots.forbidden`; the robots endpoint returned HTTP 403, article bytes were zero, and no Markdown was produced. Attempt ID: `job-880fc2c093d64619bcf05be93f34c68c`; evidence JSON SHA-256: `033e8c04a3aefa5d1dd8f3b2da542b82b9a2cd5af117e2e128eb85e067a0df35`.

The official `/en/news` directory is also browser-readable and includes 2026 interviews. Its `Scenario design and analysis` filter showed older interview items, so the publications directory is the better manual route to current research. TypeSafe `jev-1.13.0` chose one exact detail-page probe (confidence 0.83), then classified NGFS `browser_manual_only` (confidence 1.0). Do not add a same-origin PDF scope exception, scraper or browser bypass. No scope change, content import, meeting extraction, weekly report or Registry sync was performed.

## Remaining checks

- For manual research, open the official publications directory, select the latest relevant detail page, and use its own PDF download button if needed.
- Revisit automated acquisition only if NGFS publishes an officially permitted feed/API path or its current Runtime robots result changes.
- No event agenda or meeting body was checked; reporting and Registry remain unverified.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- The homepage, sitemap, press route, and an exact current publication were rejected by robots.forbidden. Keep automated acquisition blocked and do not treat browser-readable publications or direct downloads as governed success.
