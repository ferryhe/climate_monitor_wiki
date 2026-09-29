---
name: climate-site-wto
description: "Use for WTO (wto) site acquisition and coverage in climate_monitor_wiki."
---

# WTO source skill

Use this skill only for `source_key: wto`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `wto` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** browser pages are readable; governed HTTP acquisition succeeds, but both tested pages fail the shared HTML-to-Markdown transform. Automated article output, weekly reporting and Registry handoff remain blocked or unverified.

The scope review on 2026-05-14 recorded: Reviewed WTO climate challenge, news, and publications paths after climate_change_e.htm resolved to the WTO error page.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:04:46.066006+00:00 (UTC)

VM application revision `2aea05d`; 0/4 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `085fbabeb332dd610b9c5699bb99f0b5628b831dd065ab9e063b7a320734c13b`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.wto.org/`: `rejected`; `scope.origin_not_allowed; scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.wto.org/english/tratop_e/envir_e/climate_challenge_e.htm`: `rejected`; `scope.origin_not_allowed; scope.path_not_included; transform.html_invalid`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.wto.org/english/news_e/news_e.htm`: `rejected`; `scope.origin_not_allowed; scope.path_not_included; transform.html_invalid`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.wto.org/english/res_e/publications_e/publications_e.htm`: `rejected`; `scope.origin_not_allowed; scope.path_not_included; transform.html_invalid`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Next source-specific check

- Keep browser-captured exact pages as manual evidence and retest only after the governed HTML transform supports the WTO response. Reconcile the upstream SiteSkill/runtime origin policy with the repository scope before any seed changes.

## Browser and exact reader checks: 2026-09-28

The regular browser rendered the configured climate page and news list. The list exposed a current climate-relevant item: [“Panel to review EU carbon border adjustment mechanism and emissions trading scheme”](https://www.wto.org/english/news_e/news26_e/dsb_25sep26_496_e.htm), dated 25 September 2026. Its body and dispute detail are readable; it states the DSB met on 25 September and the next regular meeting is 27 October 2026. The page also links to the meeting's associated documents through a JavaScript popup, which was not separately tested.

Using the governed article reader against the exact configured climate page and this exact news article produced HTTP 200 with `robots.allowed` (25,304 and 27,857 bytes, respectively). Both then failed at `transform.simple_html_markdown` with `transform.html_invalid`; neither produced Markdown. The exact article attempt was `job-7b963fef612e4205acae34298eae3e29`. The earlier fresh seed run also reported `scope.origin_not_allowed` / `scope.path_not_included`, so discovery and content transformation have separate failure points. No production scope change was made.

TypeSafe (`jev-1.13.0`) selected an upstream transform follow-up (confidence 0.75): keep WTO scope unchanged, record browser readability for manual exact-page capture, and fix/retest the governed HTML transform before automating this source. A WTO-specific network/parser script is not justified. No page was ingested or synced to Registry.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- Governed HTTP returned source HTML, but the shared HTML-to-Markdown transform failed with transform.html_invalid on the exact climate page and article. Keep automated content extraction blocked until the shared transform works; do not add a WTO-only scraper.
