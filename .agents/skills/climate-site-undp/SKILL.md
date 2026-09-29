---
name: climate-site-undp
description: "Use for UNDP (undp) site acquisition and coverage in climate_monitor_wiki."
---

# UNDP source skill

Use this skill only for `source_key: undp`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `undp` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** all configured UNDP Climate Promise seeds succeeded in the isolated runtime, and two exact article bodies were fetched and converted to Markdown; meetings, report generation and Registry handoff remain unverified.

The scope review on 2026-05-14 recorded: Reviewed UNDP Climate Promise pages because www.undp.org returns 403 from web_listening HTTP and browser probes.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:02:14.356419+00:00 (UTC)

VM application revision `2aea05d`; 3/3 attempted seeds succeeded and 4 candidate rows were returned. Per-source evidence summary SHA-256: `d1923e893efdea97981d56d2c697b72b762042772d087fb7d5cb89c4ea6b29bb`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://climatepromise.undp.org/news-and-stories`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://climatepromise.undp.org/research-and-reports`: `success`; 1 candidate row; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://climatepromise.undp.org/what-we-do`: `success`; 1 candidate row; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.

Each successful seed produced a versioned `web_listening` SiteSkill and SiteState in the isolated runtime. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Article body checks — 2026-09-28 (UTC)

A fresh three-seed collection completed at 20260928T192848Z-52058702: 3/3 seeds succeeded and returned four candidate rows (one duplicate listing URL). Summary SHA-256: 64f287dfefc427ba03e1bf32b605f077aff64c02b296643dc85f0fa5209e54ad. Discovery used acquisition.web_http, discovery.html_links and transform.simple_html_markdown.

Two exact story pages then succeeded through the governed article reader:

- “3 ways to increase influence at climate negotiations”, dated 2026-08-17: 14,884 Markdown characters, SHA-256 c33ac04b18aae36595bf279df381b93b2bb56173187eecace9a7f2af53352009. Runtime job job-6866548f42144134a38366f758ac4166.
- “How Uzbekistan is taking climate action from promise to progress”, dated 2026-09-28: 33,323 Markdown characters, SHA-256 54b9564797661263366bfe1d66bdc9e0f389f977eda8ff4943a11e4359f1cecb. Runtime job job-ee6e5783a72b42a1b5b02df7cbde743d.

Both exact reads returned HTTP 200, robots.allowed, and a successful transform.simple_html_markdown result. The ordinary browser also rendered the Uzbekistan page and showed the same title and date; browser display was reconnaissance, while the Runtime receipt is acquisition evidence. The transformation retains the full story and linked references. Filtered listing URLs carrying ?ctype=blog previously returned web_http.url_redacted; queryless seed pages and exact story paths work.

TypeSafe jev-1.13.0 chose the exact 2026-09-28 story as the next freshness check (confidence 0.95) and, after both body checks, chose no_site_script (confidence 1.0). Keep using shared discovery, fetch_article_content and the common Markdown transform. No scope change or site-specific script is justified.

The original Markdown and receipt files are in ignored .tmp/site-content-undp/; the external run evidence is under /home/ubuntu/climate-site-pilot-20260928/undp/. No content was written to sources/, and Registry was not run.

## Next source-specific check

- Continue with the next configured site, UNEP.
- For each site, first run its configured seeds, then fetch one exact current article if discovery yields one. Record meetings or reports only when the source provides them and the Runtime captures them.
- Leave scope unchanged unless a concrete path is required and TypeSafe's decision is backed by a successful exact read.
- Keep generated web_listening state and evidence outside Git; verify Registry separately after article acquisition is complete.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated web_listening SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- The configured Climate Promise news and research/report seeds and exact article bodies were successfully read. Search these first-party sections for current items; meeting capture and Registry handoff are still separate unverified steps.
