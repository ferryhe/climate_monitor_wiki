---
name: climate-site-oecd
description: "Use for OECD (oecd) site acquisition and coverage in climate_monitor_wiki."
---

# OECD source skill

Use this skill only for `source_key: oecd`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `oecd` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** the four configured entries remain blocked in the Runtime; the official web UI exposed a search/RSS path, but the governed reader did not produce a body or candidate from it. No successful runtime SiteSkill was produced.

The scope review on 2026-05-14 recorded: Reviewed OECD with browser_rendered; legacy /en/news.html returned 404 and newsroom is now under /en/about/newsroom.html.
Treat that as historical context until a current governed run confirms it. The reviewed scope requests browser mode; verify actual browser use from the governed attempt receipts, because that YAML value currently does not select the runtime tool.

## Isolated seed check: 2026-09-28T16:08:12.004740+00:00 (UTC)

VM application revision `2aea05d`; 0/4 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `38615790f12a8d3ca6e8bcfd789daaa73398b87a782ddd8633beaf2a2cdd687b`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.oecd.org/`: `rejected`; `acquisition.cloudflare_blocked; eligibility.not_installed`; 0 candidate rows; executed tools: `acquisition.web_http`; skipped tool candidates: `acquisition.cloakbrowser`, `acquisition.playwright`.
- `https://www.oecd.org/en/topics/climate-change.html`: `rejected`; `acquisition.cloudflare_blocked; eligibility.not_installed`; 0 candidate rows; executed tools: `acquisition.web_http`; skipped tool candidates: `acquisition.cloakbrowser`, `acquisition.playwright`.
- `https://www.oecd.org/en/publications.html`: `rejected`; `acquisition.cloudflare_blocked; eligibility.not_installed`; 0 candidate rows; executed tools: `acquisition.web_http`; skipped tool candidates: `acquisition.cloakbrowser`, `acquisition.playwright`.
- `https://www.oecd.org/en/about/newsroom.html`: `rejected`; `acquisition.cloudflare_blocked; eligibility.not_installed`; 0 candidate rows; executed tools: `acquisition.web_http`; skipped tool candidates: `acquisition.cloakbrowser`, `acquisition.playwright`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

In a second isolated run on 2026-09-28, Playwright 1.0.0 was qualified and active in the same Runtime volume. The configured newsroom seed executed `acquisition.web_http` and then `acquisition.playwright`; both returned `acquisition.cloudflare_blocked`. CloakBrowser was not installed. No candidates or SiteSkill were produced. The external run summary has SHA-256 `5f467fa89016f9a32f7cfd8df9fc9278fc61f1a2aa9640cff47a33c681eb1c4f`.

The later fresh-state all-source exploration confirmed this on all four OECD seeds: Playwright executed four times, 0/4 seeds succeeded and 0 candidates were returned. Per-source summary SHA-256: `afbe68dcf6d1a8a885bf1dbc9de586dfe19c2ff99a70a3d87d2855ccc6a1d10b`.

## Official search/RSS path probe: 2026-09-28

The public OECD newsroom and its News search UI were readable in the desktop browser. Searching `climate change` showed 308 English news results; 113 were tagged with the Climate change topic. Result rows exposed title, date, summary and official article URL. The official **Get RSS link** dialog generated a feed on `https://api.oecd.org/webcms/search/rss` with `searchTerm=climate change` and English-language parameters. This is browser reconnaissance, not a governed acquisition receipt. Opening the feed in the desktop browser returned `ERR_BLOCKED_BY_CLIENT`.

TypeSafe Choice (`jev-1.13.0`) recommended first trying the exact feed URL through the isolated governed reader (confidence 1.0), then recommended one Playwright attempt after the result (confidence 1.0). The first fresh-runtime attempt and the later run in the Playwright-qualified pilot both stopped with `web_http.url_redacted`; only `acquisition.web_http` executed, no HTTP status/body was recorded, and no candidates were returned. The installed Runtime replaces query text with a hash in the returned URL, after which its `web_http` adapter rejects the non-exact URL. The browser tool was not dispatched. Per-source pilot summary SHA-256: `d2592b869d8fde8cae9155a04ebaaf52052b1927310f7816e819d640cf451628`.

Do not add `api.oecd.org` or `/webcms/search/rss` to persistent scope yet. The search UI has shown a useful route, but its query-bearing feed cannot currently yield a governed content artifact. Do not remove the query, build a direct scraper or treat browser-visible snippets as source content. Resume when the governed reader can preserve the exact query URL or OECD provides a queryless, governed-accessible feed.

## Next source-specific check

- Keep the source blocked while the governed reader receives the site challenge. Check for an authorized public OECD feed or endpoint before proposing a narrow scope change; do not bypass the challenge.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- The configured seeds and browser-qualified attempts produced no governed body or candidate; the query-bearing search/RSS path also did not yield a content artifact. Keep scope unchanged and do not strip query parameters or copy browser snippets as evidence.
