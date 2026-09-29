---
name: climate-site-iea
description: "Use for IEA (iea) site acquisition and coverage in climate_monitor_wiki."
---

# IEA source skill

Use this skill only for `source_key: iea`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `iea` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** configured news/topic seeds remain blocked and no `web_listening` SiteSkill was produced, but one exact linked IEA report PDF was fetched and converted to Markdown in the isolated pilot. Registry handoff is unverified.

The scope review on 2026-05-14 recorded: Reviewed IEA public news, climate topic, and sitemap inventory after /reports returned 404; GitHub runner requires browser_rendered.
Treat that as historical context until a current governed run confirms it. The reviewed scope requests browser mode; verify actual browser use from the governed attempt receipts, because that YAML value currently does not select the runtime tool.

On 2026-09-28, an isolated run against VM application revision `2aea05d` returned no candidates. `/news` and `/topics/climate-change` returned `acquisition.auth_required` through `acquisition.web_http`. `/sitemap.xml` redirected to `/sitemap-index.xml` and hit `scope.path_not_included`. Narrow diagnostic reads of `/sitemap-news.xml` and `/sitemap-reports.xml` fetched and parsed their sitemaps, but the discovered article bodies still returned `acquisition.auth_required`. The isolated Runtime had no active browser tool, so this does not establish whether a qualified browser can read the site.

TypeSafe Choice previously recommended `qualify_browser_then_recheck`. That check ran on 2026-09-28 in an isolated Docker volume with a qualified, active Playwright adapter. The configured `/news` seed still returned `acquisition.auth_required` at `acquisition.web_http`; the governed runtime treated it as terminal and did not attempt Playwright. It returned 0 candidates. The external run summary has SHA-256 `9be1afdaa38cdd554cf8e934c43c159f742de73be643a16372c4c45dca949539`.

## Follow-up document check: 2026-09-28

An explicit bounded Playwright request for an IEA news page was eligible and used `acquisition.playwright`, but ended after 61.1 seconds with `eligibility.runtime_budget_exhausted` (2 requests, 233,046 bytes, no content). A separate explicit `/news` browser request ended with `scope.path_not_included`. Do not infer a successful browser read from browser qualification.

The official [Breakthrough Agenda Report 2026 page](https://www.iea.org/reports/breakthrough-agenda-report-2026) links its PDF on `iea.blob.core.windows.net`. TypeSafe Choice recommended allowlisting only that exact second-origin PDF URL (confidence 0.94), not the blob host or `/assets/` prefix. `monitoring/site_scopes.yaml` now lists the exact PDF URL and path. The governed Runtime fetched it with HTTP 200 via `acquisition.web_http` (5,435,604 bytes, 2 requests). The blob host's `robots.txt` returned HTTP 400, so the Runtime recorded `unknown_allow` under `robots-unknown-allow.v1`.

The standard article adapter returned `failed: no cleaned content artifact` while preserving the acquired PDF source artifact. The existing `pypdf` dependency extracted 62 pages and 160,101 text characters to Markdown. The report page states publication date 2026-06-09; the PDF cover title is “Breakthrough Agenda Special Report 2026”. PDF SHA-256: `760a71a7d2ee7c078ea16dd52d88b67652a34fd39f2aaabb996396ac147a15ba`; exploratory Markdown SHA-256: `4f2da3f11cf76fc44ed62acc20f201d743a581e5f7490412cfe093b6939c523e`.

Pilot files are outside the repository at `/home/ubuntu/.local/share/climate-monitor/site-browser-pilot/site-content/iea/`; local review copies are under `.tmp/site-content-iea/`. This verifies exact PDF acquisition and a manual generic PDF-to-Markdown conversion, not adapter-produced content, complete news coverage, ingestion or Registry handoff. Do not add an IEA-only script for generic `pypdf` conversion; revisit a shared converter if other sources need the same step.

Next, verify the extracted report text/date against Registry binding in an isolated candidate. Continue searching for a public first-party IEA news body through the governed reader; keep the general news/topic routes blocked until their content is actually returned. Do not treat sitemap parsing or browser installation as content success.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- Configured news/topic routes currently require authentication or exceed the bounded browser runtime, but one exact linked report PDF was fetched in an isolated pilot. Do not infer news-body success from sitemap parsing or browser qualification; treat report PDF conversion as unverified unless this run returns its own governed text artifact.
