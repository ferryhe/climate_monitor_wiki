---
name: climate-site-adb
description: "Use for ADB (adb) site acquisition and coverage in climate_monitor_wiki."
---

# ADB source skill

Use this skill only for `source_key: adb`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `adb` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** automated acquisition is blocked by `robots.forbidden`; no successful runtime SiteSkill was produced. Public pages are readable in a normal browser for manual review.

The scope review on 2026-05-14 recorded: Reviewed ADB climate change, news, and publications paths.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:02:17.809129+00:00 (UTC)

VM application revision `2aea05d`; 0/3 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `d74fde9a35063cee7588edfdc5ee9dd2f259cfd7ebeac20a700b64dd8f9002a5`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.adb.org/climate-change`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.adb.org/news`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.adb.org/publications`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Browser and exact RSS checks: 2026-09-28 (UTC)

The public climate page `https://www.adb.org/climate-change` redirects to `/what-we-do/topics/climate-change`. In a normal browser it showed a “What's New” section with dated news and publication links. The public news page showed current dated news and event listings and links to the official RSS page at `https://www.adb.org/rss`.

The configured Runtime seed check returned 0/3 successes; each seed (`/climate-change`, `/news`, `/publications`) was rejected by `robots.forbidden`. TypeSafe recommended one bounded exact-feed probe (confidence 0.73). A fresh isolated Runtime then tried the browser-discovered RSS URL under a temporary `/rss` scope: it was also rejected by `robots.forbidden`, returned no candidates and produced no SiteSkill. Evidence summary SHA-256: `a8bd089e729d4c7d81ddf423e349015db90123cabc6ec0c424ab4f770d6251f3`; evidence is under `$HOME/climate-site-pilot-20260928/adb-rss/`.

For a concrete browser-readable example, the climate page linked “Strengthening Climate Resilience Through Social Protection Programs” (`https://www.adb.org/publications/climate-resilience-social-protection-programs`). The page exposed the title, “Publication | September 2026”, an abstract and description, DOI `10.22617/BRF260396-2`, and an official PDF link. The PDF viewer did not render readable text in this check, so its contents and extraction are unverified.

TypeSafe selected `blocked_automated_manual_browser` (confidence 1.0): keep browser reading as a manual option, do not add an ADB-specific browser scraper or widen the repo scope. Revisit automated collection only if the governed reader's policy or an official machine-readable endpoint is approved and readable. Do not bypass robots.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- The configured climate, news, publications, and exact official RSS probes were rejected by robots.forbidden. Keep these as acquisition gaps when the governed reader reports the same policy result; browser-visible pages and PDF links are manual leads, not fetched evidence. Do not retry through another user-agent or browser route.
