---
name: climate-site-carbonpool
description: "Use for CarbonPool (carbonpool) site acquisition and coverage in climate_monitor_wiki."
---

# CarbonPool source skill

Use this skill only for `source_key: carbonpool`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `carbonpool` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** browser pages and exact `/post/` bodies are readable through the governed reader; general blog discovery misses post links, and Markdown omits H1/date metadata. Weekly reporting and Registry handoff remain unverified.

The scope review on 2026-05-14 recorded: Knowledge/news and webinar entrypoints are tracked independently rather than treating homepage success as full coverage.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:04:36.448405+00:00 (UTC)

VM application revision `2aea05d`; 1/3 attempted seeds succeeded and 3 candidate rows were returned. Per-source evidence summary SHA-256: `785988ec3273e0855505faf1eb58cf7c3133df3524b640f1416f349864c03513`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.carbonpool.earth/knowledge`: `rejected`; `scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.carbonpool.earth/carbon-credit-delivery-offtake-agreements`: `rejected`; `scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.carbonpool.earth/webinar-and-qa-the-quest-for-permanence-are-registry-buffer-pools-the-right-solution`: `success`; 3 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.

Each successful seed produced a versioned `web_listening` SiteSkill and SiteState in the isolated runtime. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Next source-specific check

- Diagnose why `discovery.html_links` promotes category links but not visible `/post/` links before changing the blog scope or adding a CarbonPool script.
- Verify title/date binding for exact posts and the linked PDF through shared pipeline contracts.

## Browser and exact reads: 2026-09-28

The ordinary browser shows a Wix blog at canonical `/blog`; `/knowledge` redirects there. A temporary governed collector probe for `/blog` and a second probe for `/blog/categories/knowledge` both returned HTTP 200 and succeeded through `transform.simple_html_markdown`, but they produced category/blog candidate rows and no `/post/` candidates, despite those links being visible in the browser. Their summary SHA-256 values are `dc94f681666f967358953462081442211e4b9418222b965f485d76becd8ae93d` and `4388b2806f943a18d31202bdec085246a7065f7347894821fa8dc7e8a445ba4a`; both probes had checkpoint updates disabled and did not alter the configured scope.

The exact forest-risk post [`Why do Forests Disappear?`](https://www.carbonpool.earth/post/amazon-forest-carbon-loss-fire-drought) fetched with HTTP 200 and `robots.allowed`; it produced 5,173 Markdown characters, SHA-256 `2c4821706c2aaf3cfea5f950a52e7a4c460fc5b346fae0f962d651429596ab0a`. The main text, image links and author are present, but the H1 and date are absent; the browser shows “Aug 25” without a year. The shared conversion also keeps “top of page”, “Search” and an empty heading.

The configured old webinar URL redirects in the browser to the canonical `/post/` page. A direct governed read of the old path was rejected as `scope.path_not_included`; the exact canonical `/post/webinar-and-q-a-the-quest-for-permanence-are-registry-buffer-pools-the-right-solution` read succeeded with HTTP 200 and `robots.allowed`, returning 16,107 Markdown characters (SHA-256 `08709e695d72e86cb4635880571426477c1546ce42ff7cd3fca01c16c91e66e5`). Its body says the webinar was hosted on July 22; browser metadata dates the article to August 5, 2024 and shows an August 18 update. Treat July 22 as the event date (year inferred from article timing), separate from publication/update dates. This is a recorded webinar Q&A article, not a structured meeting record.

The exact webinar article “Promises made, promises kept” was also browser-visible with publication date 21 February 2025 and fetched as 8,873 Markdown characters (SHA-256 `07d971022222f2c772bc891cf402bfa637c061301fccc64e194d96f5153bbe4c`). Its body is a full Q&A; it says the webinar was held on January 16 without a year. The Markdown again omits title and publication date, so keep those metadata fields separate from the event date.

The scope now points that exact webinar seed at its verified canonical `/post/` path, already covered by the existing `/post/` include. A fresh three-seed isolated run then succeeded on 1/3 seeds, returned two current `/post/` candidates and failed the stale `/knowledge` and `/carbon-credit-delivery-offtake-agreements` paths with `scope.path_not_included`; summary SHA-256 `fab68196366761d809cb9c517e355b66975b525be846cf95a8f8252d144e610a`. The browser redirects those paths to `/blog` and `/carbon-insurance`; the temporary `/blog` test found categories only. TypeSafe (`jev-1.13.0`) selected the canonical seed change (confidence 0.95) and an upstream discovery follow-up instead of a CarbonPool-only parser (confidence 0.5). No CarbonPool content was ingested or synced to Registry.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- General blog discovery misses visible /post/ links. Exact /post/ bodies are readable, but the Markdown may omit the H1 and a complete publication date; verify date evidence independently and never infer a year from a month/day alone.
