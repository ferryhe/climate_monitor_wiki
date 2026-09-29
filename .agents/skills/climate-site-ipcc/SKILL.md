---
name: climate-site-ipcc
description: "Use for IPCC (ipcc) site acquisition and coverage in climate_monitor_wiki."
---

# IPCC source skill

Use this skill only for `source_key: ipcc`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `ipcc` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** all configured seed pages explored and one in-scope AR7 report-cycle page body verified; Registry handoff remains unverified.

The scope review on 2026-05-14 recorded: Reviewed IPCC report and news paths.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:01:35.718899+00:00 (UTC)

VM application revision `2aea05d`; 3/3 attempted seeds succeeded and 5 candidate rows were returned. Per-source evidence summary SHA-256: `62cf22ce0b41fe82021836930921d4457185bf56b0650dd2a1692f757654a8eb`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.ipcc.ch/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.ipcc.ch/reports/`: `success`; 1 candidate row; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.ipcc.ch/news/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.

Each successful seed produced a versioned `web_listening` SiteSkill and SiteState in the isolated runtime. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Article-body check: 2026-09-28

Fetched the discovered `https://www.ipcc.ch/assessment-report/ar7/` page through the governed exact-URL reader in the isolated Runtime. The result was HTTP 200 via `acquisition.web_http`; robots allowed it. `transform.simple_html_markdown` produced 13,153 Markdown characters with SHA-256 `3891590be7a612895ff8ee2591d199bf65cfc3503c5e731ded07ce8a01fd9e47`.

The page says the seventh assessment cycle began in July 2023, the three Working Group report outlines were agreed at IPCC-62 in February 2025, and the AR7 Synthesis Report is expected in late 2029. It also lists several special/methodology reports planned for 2027. The page does not state its own publication date; do not use its event dates or the Runtime observation time as that date.

The verified Markdown and evidence summary are outside the repository at `/home/ubuntu/.local/share/climate-monitor/site-browser-pilot/site-content/ipcc/ar7.*` on the VM; local review copies are under `.tmp/site-content-ipcc/`. This validates one page body only, not the linked PDFs, a weekly run or Registry ingestion. No IPCC-specific script is needed for this ordinary HTML-to-Markdown path.

## Next source-specific check

- Preserve the successful seed checkpoints; diagnose only incomplete or rejected seeds.
- Retrieve a dated IPCC news item or linked report through the governed reader, then verify its publication date and Registry binding.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- Configured report-cycle seeds and one exact AR7 report-cycle page were readable. Prioritize current first-party report-cycle updates within the frozen scope, and keep Registry handoff separate from page-read success.
