---
name: climate-site-wef
description: "Use for WEF (wef) site acquisition and coverage in climate_monitor_wiki."
---

# WEF source skill

Use this skill only for `source_key: wef`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `wef` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** all three configured seeds succeeded in the isolated Runtime and an exact current article body was acquired. The candidate list missed the browser-visible September 28 story, and governed Markdown omitted that page's H1/date; report and Registry handoff remain unverified.

The 2026-05-14 scope review noted HTTP 403 and an agenda redirect. The 2026-09-28 Runtime evidence supersedes that access observation. The browser hint still does not select the Runtime tool; read actual attempts from receipts.

## Isolated seed check: 2026-09-28T16:08:52.165205+00:00 (UTC)

VM application revision `2aea05d`; 3/3 attempted seeds succeeded and 6 candidate rows were returned. Per-source evidence summary SHA-256: `d5e6f9db90c70232620710841ae0c4f996561a3552b276f2021b717142ecdc85`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.weforum.org/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.weforum.org/stories/climate-action/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.weforum.org/publications/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.

Each successful seed produced a versioned `web_listening` SiteSkill and SiteState in the isolated runtime. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Current article and metadata check — 2026-09-28

The later fresh-state all-source run was 20260928T163727Z-2623bd03 on VM image
2aea05d. It completed 3/3 configured seed checks and returned 6 candidate rows.
Per-source summary SHA-256: 2f38878f2e76606a8822a9ac082794de36355912490445191b4bd71944159058.
The root, climate-action listing and publications listing all used
acquisition.web_http, discovery.html_links and transform.simple_html_markdown;
no Playwright attempt occurred. The publications seed included an unrelated
Annual Report page whose transform failed with transform.ineligible_quality,
while the seed itself remained complete because other pages succeeded.

The ordinary browser showed a current climate story dated 2026-09-28:
“4 reasons why next-generation credits are redefining nature-based carbon
markets” at
https://www.weforum.org/stories/climate-action/climate-next-generation-carbon-credits/.
TypeSafe jev-1.13.0 chose that exact current URL for one governed body check
(confidence 0.99). Runtime job job-5a7b43e8575a4a76a99867c9a0ae3598 returned HTTP
200 with robots.allowed and successful transform.simple_html_markdown. The
14,439-character Markdown body hash is
f796940fcfdf5758559a3801ec78a1873772918e5fa656513be9ef0291f25c9f. The full
article body, author names, summary bullets and links are present, even though
the browser UI displayed an optional-marketing-cookie content notice; no cookie
choice was needed by the governed reader.

There are two gaps to preserve. First, neither the all-source nor current browser
view's exact story appeared in the candidate rows; seed discovery instead found
an older climate resilience article and two publications. Exact URL readability
does not prove the standard discovery path will select it. Second, the Markdown
body has no target-page H1 or publication date. The first heading is an author
name; a September 28 date appears later for a related story. The current
candidate title is slug-derived. In the shared pipeline,
article_title.extract_page_title runs only on HTML evidence, while this Runtime
record is text/markdown. TypeSafe jev-1.13.0 classified this as a shared
metadata/artifact follow-up (confidence 1.0), not a WEF-only parsing script.
Do not use the related-story date as this article's date.

The scope's browser hint did not select Playwright. The scope review note that
HTTP returned 403 is historical; all three seeds and this exact story returned
HTTP 200 in the 2026-09-28 isolated Runtime. Paths and fetch mode were not
changed. The event exclusions remain in effect; this probe did not verify WEF
meeting capture.

The Runtime receipt, exact body and Markdown are in
/home/ubuntu/climate-site-pilot-20260928/wef/ and ignored .tmp/site-content-wef/.
Nothing was written to sources/ and Registry was not run.

## Next source-specific check

- Keep shared discovery and the governed article reader; no WEF-specific script is justified by one page.
- Track candidate freshness and page title/date preservation as shared follow-up work before relying on this source for dated article selection.
- Continue with the next configured source and keep meetings and Registry unverified until directly checked.

For each run, record exact seed outcomes, actual tools and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Keep generated web_listening SiteSkill and SiteState in external runtime storage.

## Hermes production guidance

- All three configured seeds and one exact story were readable, but normal discovery missed a browser-visible current story and Markdown omitted its H1/date. Verify publication date from the exact bound evidence and do not infer completeness from seed success.
