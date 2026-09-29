---
name: climate-site-world-bank
description: "Use for World Bank (world-bank) site acquisition and coverage in climate_monitor_wiki."
---

# World Bank source skill

Use this skill only for `source_key: world-bank`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `world-bank` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** configured seed discovery remains blocked; one exact World Bank page was successfully read in a fresh isolated Runtime. No discovery SiteSkill was produced.

The scope review on 2026-05-14 recorded: Reviewed World Bank climate, research, news, publication, and current /ext/en redirects.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:02:16.989006+00:00 (UTC)

VM application revision `2aea05d`; 0/4 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `8b67d010a5246963b0530025aac6570e6f22ca0cf98775c9e7c98646744149eb`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.worldbank.org/`: `rejected`; `scope.origin_not_allowed; scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.worldbank.org/en/topic/climatechange`: `rejected`; `scope.origin_not_allowed; scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.worldbank.org/en/research`: `rejected`; `scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.worldbank.org/en/news`: `rejected`; `scope.origin_not_allowed; scope.path_not_included; web_http.url_redacted`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Exact COP31 page check: 2026-09-28 (UTC)

An isolated fresh Runtime volume (`climate_site_wb_cop31_probe_20260928`) fetched the official page `https://www.worldbank.org/en/who-we-are/news/campaigns/2026/the-world-bank-group-at-cop31` with HTTP 200 and `robots.allowed`, using `acquisition.web_http` and `transform.simple_html_markdown`. Runtime job: `job-0105ed603c0a473f802f5698c676c0c4`; Markdown: 5,099 characters, SHA-256 `695ad483c62f10eaec1c0c1c3b70eaf676b9d8810f52422d6524bd1825aafb78`.

The H1 and the COP31 date range (9–20 November 2026) are present. The events section says “coming soon”; no session agenda or individual event times are listed. TypeSafe chose to record this exact-page success while leaving configured discovery unresolved (confidence 0.47; probability of that choice 0.61). Do not broaden `site_scopes.yaml` from this single read. Evidence summary: `/home/ubuntu/climate-site-pilot-20260928/world-bank-fresh/`; no article was ingested and no Registry operation was run.

The original configured-seed Runtime still returned 0/4 successes. Browser visibility of the page is useful for manual review, but does not establish automated discovery coverage.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- Configured seed discovery remains blocked by origin/path policy, while one exact COP31 page was readable in an isolated Runtime. Keep discovery incomplete and only stage a result after the controlled reader accepts its exact URL under this run’s frozen scope.
