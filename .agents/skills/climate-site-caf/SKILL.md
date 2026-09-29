---
name: climate-site-caf
description: "Use for CAF (caf) site acquisition and coverage in climate_monitor_wiki."
---

# CAF source skill

Use this skill only for `source_key: caf`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `caf` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** official pages are readable in the normal browser, but all four configured seeds and one exact article read are blocked by `gateway.tls_certificate_invalid` in the project Runtime. Do not disable TLS validation. No discovery Runtime SiteSkill was produced.

The scope review on 2026-05-14 recorded: Reviewed CAF with browser_rendered because HTTP fails certificate validation; /knowledge/views now redirects to /en/blog.
Treat that as historical context until a current governed run confirms it. The reviewed scope requests browser mode; verify actual browser use from the governed attempt receipts, because that YAML value currently does not select the runtime tool.

## Isolated seed check: 2026-09-28T16:08:53.879200+00:00 (UTC)

VM application revision `2aea05d`; 0/4 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `c8260b996e1374f63e66bf5592ddbd9834e22b1cf4367f1e47fa32643ab50ecb`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.caf.com/en/`: `rejected`; `gateway.tls_certificate_invalid`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.caf.com/en/currently/news/`: `rejected`; `gateway.tls_certificate_invalid`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.caf.com/en/blog`: `rejected`; `gateway.tls_certificate_invalid`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.caf.com/en/knowledge/`: `rejected`; `gateway.tls_certificate_invalid`; 0 candidate rows; executed tools: `acquisition.web_http`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Exact browser and Runtime check: 2026-09-28

- The configured news listing is readable in the normal browser and displays current dated articles. A climate-relevant example is `https://www.caf.com/en/currently/news/caf-grants-cdema-usd200-000-to-advance-resilient-housing-in-caribbean/`: the browser shows a September 23, 2026 publication date and the full article about a CDEMA grant for disaster-resilient housing and construction standards.
- A bounded exact read of that URL through the governed reader failed in isolated Runtime job `job-4c27af620e104ab9bdbe26abd87b9372`: `acquisition.web_http` returned `gateway.tls_certificate_invalid`, no HTTP status and no content. Evidence JSON SHA-256: `c23ee1cf81a088e74ebf7a99ee5411104205fa2d5bff3002b9f95c4c71881415`.
- The earlier configured seed run also failed on all 4/4 seeds with the same error (summary SHA-256 `c8260b996e1374f63e66bf5592ddbd9834e22b1cf4367f1e47fa32643ab50ecb`). TypeSafe chose `browser_manual_tls_block` (confidence 1.0): keep the manual browser route distinct from automated success and diagnose the browser/Runtime TLS trust difference before retrying automation. Do not relax certificate validation, create a CAF scraper, or ingest browser-copied content as if the Runtime fetched it.

## Next source-specific check

- Investigate why CAF pages validate in the workstation browser but fail certificate validation in the isolated project Runtime. Keep TLS checks enabled; retry only after a valid trust chain is confirmed.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- The configured seeds and an exact current article failed with gateway.tls_certificate_invalid. Keep TLS validation enabled and record the governed fetch as blocked; browser visibility is not proof of a successful Runtime read.
