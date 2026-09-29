---
name: climate-site-iais
description: "Use for IAIS (iais) site acquisition and coverage in climate_monitor_wiki."
---

# IAIS source skill

Use this skill only for `source_key: iais`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `iais` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** all configured seed pages explored, and one official climate-risk publication body extracted in the isolated pilot; Registry handoff remains unverified.

The scope review on 2026-05-14 recorded: Reviewed IAIS public publications and climate risk topic pages.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:01:24.236901+00:00 (UTC)

VM application revision `2aea05d`; 3/3 attempted seeds succeeded and 4 candidate rows were returned. Per-source evidence summary SHA-256: `4b20751c90cab56a7d41379fde6280fc092859085f35bad831879b3778014181`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.iais.org/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.iais.org/activities-topics/climate-risk/`: `success`; 1 candidate row; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.iais.org/publications/`: `success`; 1 candidate row; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.

Each successful seed produced a versioned `web_listening` SiteSkill and SiteState in the isolated runtime. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Next source-specific check

- Preserve the successful seed checkpoints; diagnose only incomplete or rejected seeds.
- Retrieve representative discovered article bodies through the governed reader, then check Markdown, dates and Registry binding.

## Article-body check: 2026-09-28

In the isolated Runtime volume `climate_site_browser_pilot_20260928`, the governed exact-URL reader fetched the climate-risk landing page (HTTP 200, `acquisition.web_http`, 13,154 Markdown characters, SHA-256 `c397fa2c7aa0f869df7cecd7f59a1f2e260e2ba42aa5a5e1bcc3abd81d9e1518`) and the publications page (HTTP 200, `acquisition.web_http`, 6,521 Markdown characters, SHA-256 `87db5d49c99a9d7f0760a3e60ec62ccceaa359a39bbddaed4b3775b969e05a8e`). The climate page links the official April 2025 Application Paper PDF. TypeSafe Choice recommended allowing only this exact PDF path (confidence 0.99); `monitoring/site_scopes.yaml` records that path. Do not generalize it to `/uploads/`.

The isolated Runtime fetched the exact PDF with HTTP 200 via `acquisition.web_http` (4,009,194 response bytes, 2 HTTP requests); robots allowed the URL. The standard governed reader returned `failed: no cleaned content artifact`: it retained the PDF source artifact but no Markdown derivative. Using the already-installed `pypdf` dependency, the source artifact was extracted to 126-page Markdown with 388,439 extracted text characters. The cover provides publication-date evidence `April 2025`. Raw PDF SHA-256: `5fa22ef03e2cfd4a3f58e284b14aa7ec0107ad5ff74dce91dffc2d96bd7a2796`; exploratory Markdown SHA-256: `d7470a06acb9743cec23af7cbebc38e009298f60d50856965d6a7e4f46679885`.

Pilot summaries and Markdown are retained outside the repository at `$HOME/.local/share/climate-monitor/site-browser-pilot/site-content/iais/` on the VM. A local review copy is under `.tmp/site-content-iais/`. This validates a bounded manual PDF-to-Markdown path, not adapter-produced article content, ingestion, or a production run. No IAIS-only script was added: `pypdf` conversion is generic; add a shared reusable script only if more sources show the same need.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- Configured climate-related seeds and an exact official climate-risk publication were explored successfully. Use those reviewed first-party routes for current items; keep Registry handoff and complete weekly-cycle coverage unverified until the run proves them.
