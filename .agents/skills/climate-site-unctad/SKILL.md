---
name: climate-site-unctad
description: "Use for UNCTAD (unctad) site acquisition and coverage in climate_monitor_wiki."
---

# UNCTAD source skill

Use this skill only for `source_key: unctad`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `unctad` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). YAML defines the current seeds and allowed paths; do not substitute another source that shares the host.

**Current evidence level:** browser/manual only. The public homepage, current canonical climate topic and exact article and meeting pages are readable in a normal browser. Runtime seed discovery and an exact article read are denied by the site's robots endpoint. No successful Runtime SiteSkill was produced.

The old 2026-05-14 scope note and 2026-09-28 seed check recorded the climate topic and `/news` as missing/404 and both configured seeds as `robots.forbidden` (0/2, 0 candidates; summary SHA-256 `336cb9aa02444a2c7b218ef177a48a5a974c5a16d318bc3c213d34343ac8cd06`). Each robots check returned HTTP 403.

## Browser routes and exact content — 2026-09-28

The current [Trade and climate change topic page](https://unctad.org/topic/trade-and-environment/climate-change) is the canonical route observed in the browser. It groups climate-related news, publications, documents and meetings. The configured include path `/topic/climate-change` does not match this current page. However, a governed read of one exact article on the same origin was denied, so TypeSafe `jev-1.13.0` recommends recording browser/manual access and keeping repo scope unchanged until a permitted Runtime path succeeds (confidence 0.93).

The topic page linked the exact first-party article [Climate finance at a crossroads: Restoring trust through transparency](https://unctad.org/news/climate-finance-crossroads-restoring-trust-through-transparency), dated 13 March 2026. Its browser-rendered body and section headings were readable and link to the report “Beyond creative accounting: Restoring trust in the climate finance regime.” The exact governed-reader attempt returned `robots.forbidden`, robots endpoint HTTP 403, zero bytes; attempt `job-fe6fcfb494774bb38eb4979feb8ded69`. Do not retry with an alternate User-Agent, scraper or browser-policy bypass. The site-wide search pages triggered a Cloudflare verification page; the climate topic and linked detail pages themselves were readable without solving a challenge.

The climate topic page also links [a course completion page](https://unctad.org/meeting/2nd-edition-unctad-e-learning-course-trade-and-climate-action-brings-together-participants). Its browser-rendered fields include title, event dates 27 April–8 June 2026, location Online, body text and an official Add to Calendar link. The current scope explicitly excludes `/meetings` and `/events`; TypeSafe recommends keeping scope unchanged for now. Do not ingest this meeting until the policy and governed acquisition path are reviewed.

For manual review, navigate from the official climate topic page to a News, Publications or Meetings detail page. When preparing a future import, preserve the exact URL, title and page date; for a meeting, preserve its event date range and location separately from the publication date. No browser content was saved into `sources/`, no report was produced and Registry was not run. Keep generated Runtime SiteSkill and SiteState outside Git.

## Hermes production guidance

- The normal browser can display the climate topic and exact pages, but governed seeds and exact reads were denied by robots.forbidden. Treat these as manual discovery leads only and do not retry by browser, direct download, or a different origin.
