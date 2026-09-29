---
name: climate-site-psi
description: "Use for PSI (psi) site acquisition and coverage in climate_monitor_wiki."
---

# PSI source skill

Use this skill only for `source_key: psi`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `psi` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** two exact PSI COP31 pages and the official news RSS were verified through isolated governed reads; full weekly candidate promotion, meeting import and Registry handoff remain unverified.

The scope review on 2026-05-14 recorded: Reviewed UNEP FI PSI insurance, news, publications, and climate paths.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:02:03.894970+00:00 (UTC)

VM application revision `2aea05d`; 2/3 attempted seeds succeeded and 4 candidate rows were returned. Per-source evidence summary SHA-256: `b0fe05947e151d1b9f3da42e0dcc2166ebd6fe7fd1a025e94bba5cf2befeb8b8`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.unepfi.org/insurance/insurance/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.unepfi.org/category/news/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.unepfi.org/publications/`: `rejected`; `gateway.https_downgrade`; 0 candidate rows; executed tools: `acquisition.web_http`.

Each successful seed produced a versioned `web_listening` SiteSkill and SiteState in the isolated runtime. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Dated follow-up: 2026-09-28

The VM image was `2aea05d`; all acquisition and conversion evidence was kept in the isolated `climate_site_browser_pilot_20260928` Runtime volume and `/home/ubuntu/climate-site-pilot-20260928/psi`, outside production state and `sources/`.

- The exact official queryless news feed `https://www.unepfi.org/category/news/feed/` returned HTTP 200 (`acquisition.web_http`), `application/rss+xml`, and a verified 10,032-byte source artifact, SHA-256 `2f03db257ac7ae1603bbe93108c3c211bf3dd0cff074941d4592e3e4c2d2d6b4`. A one-off standard-library XML parse found 10 dated items, including the 17 September 2026 COP31 sustainable-insurance-summit announcement. The article adapter has no XML-to-Markdown artifact; the normal source collector still emits generic changed-page candidates and does not promote RSS items as report candidates.
- The announcement URL `https://www.unepfi.org/industries/insurance/cop31-global-sustainable-insurance-summit-to-catalyze-climate-resilience-across-the-economy/` returned HTTP 200, robots allowed, and 6,482 Markdown characters via `acquisition.web_http` plus `transform.simple_html_markdown`. Content SHA-256: `1336bb8a80b9ef67c55b2c0a03923f50626d377469e4a0e2f5cf9f907cdffd48`. It states that UNEP and the Insurance Association of Türkiye will convene the summit on 13–14 November 2026 at Insurance House in Antalya during COP31.
- The linked event page `https://www.unepfi.org/industries/insurance/cop31/` returned HTTP 200, robots allowed, and 4,649 Markdown characters through the same two tools. Content SHA-256: `4d9e2cb8eed6ee314ed0a947ab0a3a1b1dc29768238e5ffd08a44a98b7f3fe3e`. It includes registration, venue, and separate 13 and 14 November agendas. The generic Markdown transform concatenated agenda table cells.
- The verified Runtime source artifact for the event page was HTML, 115,002 bytes, SHA-256 `a2ceb42930caccb4c36c1e181f36bca6057c07a96bb90d2d6b36aa6ddef72ef1`. A temporary Python standard-library `HTMLParser` pass recovered two dated tables (15 and 14 agenda rows, plus headers) into Markdown; output SHA-256 `c75f81659eda292e623cce1b0bb09db7e669450fcd86cdf5eed3889fa24153e5`. TypeSafe Choice (`jev-1.13.0`, confidence 1.0) recommended this one-off proof and deferring a committed PSI parser until the same need recurs on another PSI page.
- TypeSafe Choice (`jev-1.13.0`, confidence 1.0) recommended probing the announcement first, then its exact linked event page. Only those two exact `/industries/insurance/` paths were added to the reviewed scope. No `/industries/` prefix was added.
- The original `/publications/` seed remains rejected with `gateway.https_downgrade`. No PSI report, article or meeting artifact was ingested into `sources/` or synced to Registry.

## Remaining gaps

The feed is usable for dated discovery, but the ordinary source collector does not turn RSS items into candidates. The event-page schedule can be recovered from the Runtime source artifact, but the generic Markdown alone is not a reliable agenda representation. Do not add a PSI-specific acquisition crawler. Reconsider a small deterministic agenda normalizer only if additional PSI event pages repeat the table behavior; keep isolated source files and trial conversions outside Git.

For each single-source run, record the run ID, date, configured seed outcomes, actual tools and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill, SiteState and raw content outside Git.

## Hermes production guidance

- The official news feed and two exact COP31 pages were readable; /publications remains rejected. The generic Markdown transform flattens event agenda tables, so verify row/date boundaries from the bound source body before extracting meeting sessions.
