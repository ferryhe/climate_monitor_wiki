---
name: climate-site-unwater
description: "Use for UN-Water (unwater) site acquisition and coverage in climate_monitor_wiki."
---

# UN-Water source skill

Use this skill only for `source_key: unwater`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `unwater` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** all configured entrypoints are blocked by `robots.forbidden`; a regular browser reaches a security verification page. No compliant automated acquisition route has been established.

The scope review on 2026-05-14 recorded: News, publications, and events are separate coverage entrypoints; homepage success does not satisfy this scope.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:04:34.110093+00:00 (UTC)

VM application revision `2aea05d`; 0/3 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `99da30e1ce0108f486af5af83a15fd8e51ead2465684aec8e400b483b6eb3824`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.unwater.org/news`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.unwater.org/publications`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.unwater.org/events`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Next source-specific check

- Resume only if UN-Water provides a distinct documented public endpoint or its robots policy changes. Do not query the RSS format on the blocked `/news` path, solve the Cloudflare check, or route around either control.

## Browser and policy check: 2026-09-28

The fresh governed seed run rejected `/news`, `/publications` and `/events` with `robots.forbidden` (0/3 successful; summary SHA-256 `61e977542ecb8b08804a5ccaa8674df80533091da2104bdceec8cc67a0e95a88`). The ordinary browser showed Cloudflare's “Performing security verification” page on the homepage. TypeSafe (`jev-1.13.0`) chose to stop acquisition for now (confidence 0.47; choice probability 0.58). No alternate path was tested, scope changed, page fetched, or content ingested.

A public search result exposed a query-form RSS URL under the same `/news` path (`?format=RSS`). It was not requested: the robots decision is path-based, so changing the query does not make the forbidden route eligible.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- Configured news, publications, and events paths were rejected by robots.forbidden and the normal browser showed a security-verification page. Stop automated collection; do not query alternate feed formats or solve/bypass the verification challenge.
