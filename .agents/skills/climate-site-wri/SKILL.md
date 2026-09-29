---
name: climate-site-wri
description: "Use for WRI (wri) site acquisition and coverage in climate_monitor_wiki."
---

# WRI source skill

Use this skill only for `source_key: wri`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `wri` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** all configured seeds succeeded and one exact article produced usable governed Markdown; event acquisition, weekly reporting and Registry handoff remain unverified.

The scope review on 2026-05-14 recorded: Reviewed WRI climate, research, news, and insights paths.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:04:43.316179+00:00 (UTC)

VM application revision `2aea05d`; 4/4 attempted seeds succeeded and 8 candidate rows were returned. Per-source evidence summary SHA-256: `e80e9a09737d67270420863c11aa43ab3a0f22b4b67bac41ad9ea41d755bc318`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.wri.org/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.wri.org/climate`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.wri.org/research`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.wri.org/news`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.

Each successful seed produced a versioned `web_listening` SiteSkill and SiteState in the isolated runtime. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Next source-specific check

- Verify Registry binding and meeting capture when WRI is included in a full-cycle rehearsal; the article read alone does not cover either.

## Exact article read: 2026-09-28

The exact article `https://www.wri.org/insights/carbon-dioxide-removal-policies-frameworks` (“As the US Carbon Dioxide Removal Market Retreats, Other Governments Step Up”, 10 September 2026) fetched through the governed reader with HTTP 200 and `robots.allowed`. Markdown length was 30,741 characters; SHA-256 `f5b4464c8b1ab3f1b500f452b6ec1e8405ab6401ecdac42eebc7b5f2c35dfe63`. The title, authors, date and article body are present. About 2 KB of site navigation precedes the article. TypeSafe (`jev-1.13.0`) chose to record this noise and keep the shared reader (confidence 0.97); no WRI cleaner or scope change is justified by this sample. Events remain excluded and meeting capture was not tested.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- All four configured climate/research/news seeds and one exact article were read successfully. Search those reviewed sections for current items; the article Markdown includes about 2 KB of site navigation before the body, so no WRI-specific cleaner is justified by the verified sample.
