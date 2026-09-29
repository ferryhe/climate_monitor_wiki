---
name: climate-site-fit
description: "Use for FIT (fit) site acquisition and coverage in climate_monitor_wiki."
---

# FIT source skill

Use this skill only for `source_key: fit`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `fit` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** partial seed coverage (1/3 seeds, 2 candidate rows); an exact current article is readable through the governed reader. The linked report detail page is public, but its PDF download requires personal details through a third-party form. Do not submit the form or claim the PDF was acquired.

The scope review on 2026-05-14 recorded: Reviewed UNEP FI FIT page, news, and publications paths.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:02:38.539681+00:00 (UTC)

VM application revision `2aea05d`; 1/3 attempted seeds succeeded and 2 candidate rows were returned. Per-source evidence summary SHA-256: `01e117cb1ec5aaab05d5b8b296c47bbecbf19ba3a71b80c76a380b13c7fc099b`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.unepfi.org/forum-for-insurance-transition-to-net-zero/`: `incomplete`; `acquisition.interaction_required`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.unepfi.org/category/news/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.unepfi.org/publications/`: `rejected`; `gateway.https_downgrade`; 0 candidate rows; executed tools: `acquisition.web_http`.

Each successful seed produced a versioned `web_listening` SiteSkill and SiteState in the isolated runtime. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Exact article and report check: 2026-09-28

- The forum homepage is readable in the normal browser and links to current FIT news. The governed seed results were: forum landing page `acquisition.interaction_required`; `/category/news/` succeeded with 2 candidate rows; `/publications/` failed with `gateway.https_downgrade`. The 1/3 success summary SHA-256 is `01e117cb1ec5aaab05d5b8b296c47bbecbf19ba3a71b80c76a380b13c7fc099b`.
- The exact 22 June 2026 implementation-guide launch article was fetched: HTTP 200, `robots.allowed`, `acquisition.web_http` + `transform.simple_html_markdown`, 7,760 Markdown characters, SHA-256 `23a6169753f13b84c46aa561ec18cf678d695e29605f3f47bf8af644594533e6`. The title, date, announcement and main body are present; the page also carries social links and some malformed unrelated historical links. Runtime job: `job-4449e5ccf0594ac6bac76ba22636fc3b`.
- The linked June 2026 report detail page is readable in the browser and gives a summary. Its “Download the report” link opens a third-party email-marketing form that requires first/last name, job title, organisation, email and country. No data was entered or submitted; the report PDF was not obtained.
- TypeSafe chose `article_access_report_gated` (confidence 1.0): record the article route as viable, keep seed coverage partial and the PDF unverified. Do not bypass the form, submit user data, add a FIT-only downloader or widen scope from this one sample.
- Browser lookup is a manual discovery aid. Meeting extraction, weekly reporting and Registry remain unverified.

## Next source-specific check

- Preserve the successful news checkpoint and its two candidates; diagnose only the interaction-required and HTTPS-downgrade seeds.
- Keep the article route as the verified public path. Revisit the report only if an approved access route is documented; do not submit the form as part of collection.
- Recheck the `/publications/` HTTPS path only with normal validation; never allow an HTTP downgrade.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- The news listing has partial governed coverage. The linked report PDF requires personal details through a third-party form; do not submit it or claim the PDF was acquired. Keep the PDF as a gap and use only publicly readable, in-scope article content.
