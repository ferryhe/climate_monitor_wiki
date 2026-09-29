---
name: climate-site-ifac
description: "Use for IFAC (ifac) site acquisition and coverage in climate_monitor_wiki."
---

# IFAC source skill

Use this skill only for `source_key: ifac`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `ifac` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** partial seed coverage (2/3 seeds, 4 candidate rows); a first-party climate-disclosure article is readable through the governed reader and converts cleanly to Markdown. The RSS seed remains rejected by scope.

The scope review on 2026-05-14 recorded: Reviewed IFAC knowledge gateway, news, sustainability, and RSS paths after /news-events returned 404.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:03:17.283336+00:00 (UTC)

VM application revision `2aea05d`; 2/3 attempted seeds succeeded and 4 candidate rows were returned. Per-source evidence summary SHA-256: `d628ad788fbafd1a8fe64f86ead6461ff27064d9b29e97f0a39faa6f65f7bb38`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.ifac.org/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.ifac.org/knowledge-gateway`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.ifac.org/rss.xml`: `rejected`; `scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.rss`.

Each successful seed produced a versioned `web_listening` SiteSkill and SiteState in the isolated runtime. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Exact climate-article check: 2026-09-28

- IFAC's site search for `climate`, filtered to IFAC and English, returned 255 results. One current first-party knowledge-gateway result was “From Commitment to Action: Six Practical Steps for Government Climate Disclosure”, dated June 1, 2026.
- The exact article fetched through the governed reader: HTTP 200, `robots.allowed`, `acquisition.web_http` + `transform.simple_html_markdown`, 13,638 Markdown characters, SHA-256 `606064f5318bee19c52941ff465a81a1001505f74f9dedd6058f961b56b42dfb`. Title, authors, date, headings and six implementation steps are intact; no material transform defect was observed. Runtime job: `job-4951fd7c32f74995bbb468a38a3b5754`.
- Configured seed coverage remains 2/3 successes and 4 candidates; the `rss.xml` seed is `scope.path_not_included` (summary SHA-256 `d628ad788fbafd1a8fe64f86ead6461ff27064d9b29e97f0a39faa6f65f7bb38`). The article describes a standards launch but is not a structured meeting record.
- TypeSafe chose `partial_first_party_html` (confidence 1.0): record the clean exact body read, keep RSS and overall coverage partial, and do not add an IFAC-specific script or expand scope from one page. External-network results must keep their originating publisher attribution. No content was ingested; meetings, reporting and Registry remain unverified.

## Next source-specific check

- Preserve the successful homepage/knowledge-gateway checkpoints; keep the `rss.xml` scope rejection visible.
- Inspect the rejected redirect or discovered URL, then change only the exact reviewed origin/path in `site_scopes.yaml` if it belongs to this source.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- The news and sustainability areas have partial governed coverage and an exact climate-disclosure article converts cleanly. The RSS seed is scope-rejected; keep its discovery gap explicit and do not expand the scope from this hint.
