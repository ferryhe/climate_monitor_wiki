---
name: climate-site-gca
description: "Use for GCA (gca) site acquisition and coverage in climate_monitor_wiki."
---

# GCA source skill

Use this skill only for `source_key: gca`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `gca` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** partial seed coverage (3/4 seeds, 6 candidate rows); an exact first-party climate-adaptation article is readable as Markdown through the governed reader. The feed seed remains rejected by scope.

The scope review on 2026-05-14 recorded: Reviewed GCA news redirect, reports, and feed paths after /topics returned 404.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:03:10.403033+00:00 (UTC)

VM application revision `2aea05d`; 3/4 attempted seeds succeeded and 6 candidate rows were returned. Per-source evidence summary SHA-256: `f85664d1c2b947b5b8ec4989d745ee1b85a04886fa9c4f27043154d3e898cfc0`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://gca.org/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://gca.org/news`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://gca.org/reports/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://gca.org/feed/`: `rejected`; `scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.rss`.

Each successful seed produced a versioned `web_listening` SiteSkill and SiteState in the isolated runtime. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Exact article check: 2026-09-28

- The browser news page links a first-party June 19, 2026 story, “Homa Bay approves People-Led Climate Adaptation Plan”: `https://gca.org/news/homa-bay-approves-people-led-climate-adaptation-plan/`.
- The exact page fetched through the governed reader: HTTP 200, `robots.allowed`, `acquisition.web_http` + `transform.simple_html_markdown`, 6,244 Markdown characters, SHA-256 `069b88759ee56f9bfcb8059bc431aff258ef9e308b1895c123380feb8fdb1cd0`. The title, date and main body are present, including adoption of a local adaptation plan and community-led planning. There is share/footer noise and a small drop-cap split (“H” / “oma”) in the opening paragraph. Runtime job: `job-aba5699400f64deb9ad36040ffe7edc2`.
- Configured discovery remains partial (3/4 seeds, 6 candidate rows); the `/feed/` seed failed `scope.path_not_included` (summary SHA-256 `f85664d1c2b947b5b8ec4989d745ee1b85a04886fa9c4f27043154d3e898cfc0`).
- TypeSafe chose `partial_html_viable` (confidence 0.99): record the exact article success and feed-scope failure without broadening paths or adding a site-only cleaner from one sample. No ingestion occurred; meetings, reporting and Registry remain unverified.

## Next source-specific check

- Preserve the successful news/report checkpoints; keep the `/feed/` scope rejection visible.
- Inspect the rejected redirect or discovered URL, then change only the exact reviewed origin/path in `site_scopes.yaml` if it belongs to this source.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- The news and reports sections are usable, and one exact first-party climate-adaptation article was read. The feed seed remains scope-rejected; keep that coverage gap explicit and stay within the frozen source scope.
