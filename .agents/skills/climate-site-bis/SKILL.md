---
name: climate-site-bis
description: "Use for BIS (bis) site acquisition and coverage in climate_monitor_wiki."
---

# BIS source skill

Use this skill only for `source_key: bis`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `bis` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** configured seed discovery is blocked; a specific official climate event page is readable through the governed article reader. This does not establish recurring event discovery or structured meeting extraction. No successful discovery Runtime SiteSkill was produced.

The scope review on 2026-05-14 recorded: Reviewed BIS green finance, working papers, and RSS paths after old climate/publications/press paths returned 404.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:02:27.201686+00:00 (UTC)

VM application revision `2aea05d`; 0/4 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `2a2e45ba1c2a8fe7d8d989778b6eadb1375427601ede8761502efb2ab1b56b2a`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.bis.org/index.htm`: `rejected`; `scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.bis.org/topic/green_finance.htm`: `rejected`; `scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.bis.org/wpapers/index.htm`: `rejected`; `scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.bis.org/rss/index.htm`: `rejected`; `scope.origin_not_allowed; scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Exact event-content check: 2026-09-28

- Browser route: BIS global Search, keyword `climate`; the results exposed “Climate-related financial risks” and “Green finance” topic filters. A current result was the official Green Swan 2026 event page: `https://www.bis.org/events/20261019-green-swan-2026-navigating-climate-risks-amid-uncertainties`.
- The exact page fetched through the governed reader: HTTP 200, `robots.allowed`, `acquisition.web_http` + `transform.simple_html_markdown`, 2,482 Markdown characters, SHA-256 `971019dbe39184f23d62372a24dd84af01c0996201af617bcdadd6a49d965ebf`. The title, 19 October 2026 date, Manila location, conference summary and agenda topics/speakers are present. Several adjacent agenda table row boundaries are flattened together, so review the output before using it as a structured meeting record.
- The configured four-seed run still returned 0 candidates; its summary SHA-256 is `2a2e45ba1c2a8fe7d8d989778b6eadb1375427601ede8761502efb2ab1b56b2a`. This exact read does not fix discovery.
- TypeSafe chose `record_partial_shared_quality_gap` (confidence 1.0): keep the exact-page success and seed failure distinct, record table-boundary loss as a shared transform quality issue, and defer a BIS parser or broader scope until repeated evidence shows a site-specific need.
- No BIS-specific script or persistent scope change was made. Browser search is a manual discovery aid; it is not evidence of Runtime search coverage. Meetings, weekly reporting and Registry remain unverified.

## Next source-specific check

- Keep the search page as a manual discovery route. Review one more representative BIS event or publication before considering a site-specific parser or changes to the reviewed seed/path scope.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- Configured seed discovery remains scope-blocked, although one exact official Green Swan event page was readable. Its Markdown flattens some agenda rows; verify dates and session boundaries from the body before treating it as meeting data.
