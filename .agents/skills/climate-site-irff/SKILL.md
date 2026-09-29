---
name: climate-site-irff
description: "Use for IRFF (irff) site acquisition and coverage in climate_monitor_wiki."
---

# IRFF source skill

Use this skill only for `source_key: irff`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `irff` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** the configured seed pages were explored and one relevant IRFF news article body verified in the isolated VM; meeting extraction and Registry handoff remain unverified.

The scope review on 2026-05-14 recorded: Reviewed UNDP IRFF news and publications paths.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

On 2026-09-28, an isolated run against VM application revision `2aea05d` completed all three configured seeds with no warnings and returned five candidate rows. Each seed used `acquisition.web_http`, `discovery.html_links` and `transform.simple_html_markdown`; each produced a version 1 `web_listening` SiteSkill and matching SiteState. The external pilot summary has SHA-256 `facecf7b2b17d30a20d3b366e23f94b05fe48ca2c92d7eca1cea8166e1c97b5d`. This proves seed-page access only; inspect actual article content and Registry results before widening the claim.

## Article-body check: 2026-09-28

Fetched the discovered article [Designing Risk-Sharing Platforms for Agricultural Insurance in Africa: Key Takeaways from AIO 2026](https://irff.undp.org/news/designing-risk-sharing-platforms-agricultural-insurance-africa-key-takeaways-aio-2026) through the governed exact-URL reader. HTTP 200 via `acquisition.web_http`; robots allowed it. `transform.simple_html_markdown` returned 7,485 characters with SHA-256 `cfc2452c5f0a01a7cfc79c0e7c16e148bb7b86a6fedb249b5f4096e0da612510`. The body says `POSTED 10 June, 2026`, which is usable publication-date evidence.

The article summarizes agricultural insurance risk-sharing discussions at the AIO 2026 conference in Cairo. It describes platform experiences in Ethiopia, Tanzania, Uganda and Senegal; recurring points include public-private/government anchoring, pooled underwriting and reinsurance capacity, shared services, governance and pricing discipline to support bankability. The event date is not given in the article body, so do not infer one from the posting date.

The isolated Markdown and receipt are retained at `$HOME/.local/share/climate-monitor/site-browser-pilot/site-content/irff/aio-2026.*` on the VM; local review copies are under `.tmp/site-content-irff/`. This verifies one news body only, not weekly report or Registry ingestion. No IRFF-specific script is needed for this ordinary HTML page.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- Configured seeds and one exact IRFF news article were successfully read through the governed Runtime. Use the first-party news routes for current climate-risk items; meeting extraction and Registry handoff remain unverified.
