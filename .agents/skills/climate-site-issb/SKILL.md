---
name: climate-site-issb
description: "Use for ISSB (issb) site acquisition and coverage in climate_monitor_wiki."
---

# ISSB source skill

Use this skill only for `source_key: issb`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `issb` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** partial governed reads work on exact pages, but seed discovery is incomplete; no complete runtime SiteSkill, meeting extraction, or Registry handoff has been verified.

The scope review on 2026-05-14 recorded: Reviewed IFRS ISSB group, sustainability standards, and news paths.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:01:38.848116+00:00 (UTC)

VM application revision `2aea05d`; 0/3 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `23026546f2f3614b33f65252379963698b31c854364457715ef35edc9c2d1a7f`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.ifrs.org/groups/international-sustainability-standards-board/`: `incomplete`; `acquisition.interaction_required`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.ifrs.org/news-and-events/news/`: `rejected`; `scope.origin_not_allowed; scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.ifrs.org/issued-standards/ifrs-sustainability-standards-navigator/`: `rejected`; `scope.origin_not_allowed; scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Next source-specific check

On 2026-09-28, after TypeSafe Choice recommended the ISSB-only monthly updates subtree (confidence 0.81), that exact path was added to the scope. Governed exact-page reads then succeeded through `acquisition.web_http` and `transform.simple_html_markdown`:

- Updates index `https://www.ifrs.org/news-and-events/updates/issb/`: 19,503 Markdown characters; SHA-256 `f417f5d22ab9ea481e724020f79f8535862be8ab16c773c0d4e9acf02deac5cd`.
- May 2026 update `https://www.ifrs.org/news-and-events/updates/issb/2026/issb-update-may-2026/`: 6,770 characters; SHA-256 `718a7398d19d059f75bcadb194d61b5ba64dc13541bc430c31f6fe15679b09b6`.
- The update describes the board's 13 May 2026 meeting and tentative nature-related scenario-analysis decisions. These are preliminary decisions, not final IFRS requirements. The page itself does not establish its publication date; preserve the index's dated listing when creating any candidate record.
- The existing in-scope climate-taxonomy news article also read successfully (5,914 characters; SHA-256 `98bc322b94c271bdaf4b7fec184e8ceae84281aa1ee324e0ecb2d9943dd5c75f`). Its extracted body does not provide a reliable publication date.
- The generic `/news-and-events/news/` index read returned 1,476 characters but no article listing; do not treat it as usable discovery evidence. The original 0/3 seed exploration therefore remains incomplete.

Next, verify whether the official index supplies stable dates and whether a bounded weekly set of update pages becomes qualified candidates. Do not claim SiteSkill or Registry completion from these exact-page reads alone.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- The broad news and standards seeds had scope errors, while the ISSB monthly-updates subtree was later added to the reviewed scope and exact-page reads succeeded. Use only the now-frozen reviewed subtree; do not generalize it to other IFRS paths.
