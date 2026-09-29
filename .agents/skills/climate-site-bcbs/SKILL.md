---
name: climate-site-bcbs
description: "Use for BCBS (bcbs) site acquisition and coverage in climate_monitor_wiki."
---

# BCBS source skill

Use this skill only for `source_key: bcbs`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `bcbs` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** configured seed discovery is blocked by the current origin/path policy; exact climate HTML is readable through the governed article reader; PDF body extraction remains unverified in the project Runtime. No successful discovery Runtime SiteSkill was produced.

The scope review on 2026-05-14 recorded: Reviewed BCBS index/publications and the current /committees/bcbs/overview redirect on BIS.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:02:21.760155+00:00 (UTC)

VM application revision `2aea05d`; 0/2 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `39940a80477d9a808248bfc256d360c7b3ded81500ef4332a552f68dc25f731e`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.bis.org/bcbs/index.htm`: `rejected`; `scope.origin_not_allowed; scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.bis.org/bcbs/publications.htm`: `rejected`; `scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Exact climate-content checks: 2026-09-28

- The BIS browser search, filtered to “Climate-related financial risks”, exposed the current BCBS climate-risk guideline dated 15 June 2022. Its exact HTML page was fetched through the governed reader: HTTP 200, `robots.allowed`, `acquisition.web_http` + `transform.simple_html_markdown`, 2,956 Markdown characters, SHA-256 `0dba3d312eb11fcae158b4c3f2e05266f68880e3adc261354ca3882d9f550e7e`.
- The official 15-page PDF was readable in the browser, but the project Runtime did not produce a cleaned PDF content artifact. The exact PDF probe returned HTTP 200 then `discovery.no_candidates; runtime.discovery_unavailable`; do not report this as a successful project PDF extraction.
- TypeSafe chose `html_manual_pdf_shared_gap` (confidence 1.0): record the exact HTML success, keep seed discovery blocked, and track PDF parsing as a shared Runtime transform gap. Do not widen BCBS scope or add a site-only PDF script from this evidence.
- An unrelated September 2026 Basel III monitoring report also fetched successfully as HTML; it is not climate-specific and is not a BCBS climate candidate.

## Next source-specific check

- Keep the verified exact HTML route available for bounded reads. Revisit seed policy only with evidence that a specific BCBS climate path should be included. Track PDF extraction with the shared reader work; do not claim a BCBS-specific script is needed.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- Seed discovery is blocked by the current origin/path policy, while one exact climate-risk HTML page was readable. Prefer the exact in-scope HTML route only when it is already represented in the frozen scope; PDF text extraction remains unverified, so do not claim the PDF body was acquired.
