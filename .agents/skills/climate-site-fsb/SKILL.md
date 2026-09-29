---
name: climate-site-fsb
description: "Use for FSB (fsb) site acquisition and coverage in climate_monitor_wiki."
---

# FSB source skill

Use this skill only for `source_key: fsb`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `fsb` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** partial seed coverage (2/4 seeds, 4 candidate rows); the climate topic page, filtered publications page and one exact climate-report summary are readable through the governed Runtime. Full report/PDF content and press coverage remain unverified.

The scope review on 2026-05-14 recorded: Reviewed FSB climate-related risks, publications, and press paths.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:02:57.229768+00:00 (UTC)

VM application revision `2aea05d`; 2/4 attempted seeds succeeded and 4 candidate rows were returned. Per-source evidence summary SHA-256: `717f1779a11bba4cb94a40b5dab78c7945dfe697c7a633469331ea7df02b79f6`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.fsb.org/`: `rejected`; `scope.origin_not_allowed; scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.fsb.org/work-of-the-fsb/financial-innovation-and-structural-change/climate-related-risks/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.fsb.org/publications/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.fsb.org/press/`: `rejected`; `scope.origin_not_allowed; scope.path_not_included; web_http.url_redacted`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.

Each successful seed produced a versioned `web_listening` SiteSkill and SiteState in the isolated runtime. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Exact climate-publication check: 2026-09-28

- In the browser, the climate-risk topic page links to a climate-filtered publication list. The July 14, 2025 item “FSB Roadmap for Addressing Financial Risks from Climate Change: 2025 update” was fetched through the governed reader: HTTP 200, `robots.allowed`, `acquisition.web_http` + `transform.simple_html_markdown`, 2,284 Markdown characters, SHA-256 `0a80f5d47d141152087b2c5c805d6b3b07ce129dbb6081204b90ea3513f13a80`. It preserves the title, date and report summary covering disclosures, data, vulnerability analysis, and supervisory practices. Runtime job: `job-313d2b71a5cf49f097fb5e74d778f5b7`.
- The exact HTML page is a report summary; it does not establish full report/PDF acquisition. The original 2/4 seed run returned four candidate rows, with climate and publication seeds succeeding. The homepage was rejected by origin/path scope and the press seed had `web_http.url_redacted` on query-bearing links.
- TypeSafe chose `partial_html_viable` (confidence 1.0): keep the exact HTML success separate from partial discovery; leave PDF and press coverage unresolved. No scope expansion or FSB-specific script is justified by this evidence. Reporting and Registry remain unverified.

## Next source-specific check

- Preserve the successful climate/publications checkpoints; keep the root and press failures visible.
- Inspect the rejected redirect or discovered URL, then change only the exact reviewed origin/path in `site_scopes.yaml` if it belongs to this source.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- The climate-risk and publications sections are readable, but only two of four configured seeds succeeded; full report/PDF content and press coverage remain unverified. Preserve those as separate gaps rather than treating a readable summary as the full report.
