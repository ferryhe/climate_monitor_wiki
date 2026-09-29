---
name: climate-site-g20
description: "Use for G20 (g20) site acquisition and coverage in climate_monitor_wiki."
---

# G20 source skill

Use this skill only for `source_key: g20`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `g20` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** the official homepage, media list and events calendar are readable in the normal browser, but the Runtime denies all tested G20 paths with `robots.forbidden`. Browser details are manual evidence only; automated acquisition is blocked.

The scope review on 2026-05-14 recorded: Reviewed G20 homepage and RSS feed after /news and /documents returned 404.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:02:58.036837+00:00 (UTC)

VM application revision `2aea05d`; 0/2 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `1ef96de3fbd197878b97b5025c08c98022a587de3c42f75204958c858fd68c0c`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://g20.org/`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://g20.org/feed/`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Browser calendar and exact Runtime check: 2026-09-28

- The normal browser shows the G20 Miami 2026 homepage, a News and Media list, and `https://g20.org/events-calendar/`. The calendar lists upcoming 2026 ministerial, Sherpa and Leaders’ Summit dates; it does not show meeting agendas or climate-specific sessions. The media list links mostly to external US agency announcements, so those pages must not be attributed to G20 as first-party content.
- Both configured seeds (`https://g20.org/` and `/feed/`) returned `robots.forbidden` with 0 candidates; seed-summary SHA-256 `1ef96de3fbd197878b97b5025c08c98022a587de3c42f75204958c858fd68c0c`.
- A bounded exact read of the official events calendar also returned `robots.forbidden` (HTTP 403 from `robots.txt`) in Runtime job `job-94087bd147ea4aadba9f2c1d5645be6c`. No content was acquired; evidence JSON SHA-256 `9380c2d5da2c45acaf73631829cf54fe56911282f45282e55ec084ae2645600d`.
- TypeSafe chose `browser_manual_only` (confidence 1.0): keep the calendar as normal-browser evidence, respect the robots denial, and do not use stealth access, change scope, or label linked agencies’ pages as G20 content. No content was ingested; reporting and Registry remain unverified.

## Next source-specific check

- Keep the source blocked until its robots policy changes or a publisher-approved collection route is provided. Do not use stealth browsing or misattribute linked third-party articles.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- The homepage and feed seeds were rejected by robots.forbidden although a normal browser showed media and calendar content. Do not retry through a browser or alternate endpoint as a bypass; retain the source as blocked unless the governed policy changes.
