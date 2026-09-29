---
name: climate-site-pcaf
description: "Use for PCAF (pcaf) site acquisition and coverage in climate_monitor_wiki."
---

# PCAF source skill

Use this skill only for `source_key: pcaf`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `pcaf` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** partial. The resources and standard hubs and two current PDFs are readable through the governed Runtime, but the source collector still produced no candidate rows. No complete runtime SiteSkill, weekly reporting flow, or Registry handoff has been verified.

The scope review on 2026-05-14 recorded: Reviewed PCAF resources, standard, and publications paths after /news returned 404.
Treat that as historical context until a current governed run confirms it. The reviewed scope does not request browser mode; let the governed runtime choose a qualified tool and verify what it actually used.

## Isolated seed check: 2026-09-28T16:01:43.360856+00:00 (UTC)

VM application revision `2aea05d`; 0/3 seeds became successful candidate sets and 0 candidate rows were returned. The seed pages themselves did return HTTP 200 and passed HTML-to-Markdown/link transforms; their discovered links were rejected because they fell outside the reviewed PCAF origin/path scope. Per-source evidence summary SHA-256: `f41aab3f9cc1bd6a2b6511f9a0ec6d439c57d365db6be4a81ae66af27f649f75`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://carbonaccountingfinancials.com/`: `rejected`; `scope.origin_not_allowed; scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://carbonaccountingfinancials.com/resources`: `rejected`; `scope.origin_not_allowed; scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://carbonaccountingfinancials.com/standard`: `rejected`; `scope.origin_not_allowed; scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered document bodies, meeting extraction, weekly reporting or Registry handoff.

## Dated follow-up: 2026-09-28

On 2026-09-28, the governed `/resources` and `/standard` pages were read separately:

- `/resources`: 5,536 Markdown characters, SHA-256 `431785e46a59fa77fe0badb5c1f88900e232edad5e11cd5f782cb05026a80649`.
- `/standard`: 6,996 characters, SHA-256 `cec911d6d46949e192d5d8ff1102f9f5d1114dbc3e9ceb6b93d5580ff5db2df3`.
- Both pages link current full Part A and Part C standards. The `/publications` path returned `gateway.http_status` without an HTTP status; do not rely on it as a discovery route.

TypeSafe Choice (`jev-1.13.0`) recommended only the exact full Part A third-edition and Part C second-edition PDF paths (confidence 0.88). Those two paths are now the only `/files/` entries in `site_scopes.yaml`; do not widen to `/files/` or the whole standard-launch prefix.

The source collector still returned 0 candidates with those exact paths enabled in an isolated scope probe. It fetched Part A (HTTP 200, 6,964,006 bytes), then hit `budget.bytes` on Part C (4,387,398 bytes); per-source summary is in the external pilot directory. Direct, one-URL-at-a-time reads through the existing `fetch_article_content` adapter then preserved both source PDFs in the Runtime artifact store. Robots were recorded as allowed. The adapter correctly reported `no cleaned content artifact` for each PDF; this is not a Markdown extraction success.

The generic installed `pypdf` package extracted both verified Runtime source artifacts for inspection. Part A is 209 pages / 515,826 extracted characters; its cover dates the third edition to December 2025. Part C is 134 pages / 314,249 characters; its cover dates the second edition to December 2025. Part C covers commercial lines, project insurance, personal motor and treaty reinsurance. It explicitly says adoption is voluntary and its methods are not prescriptive; applicable law prevails. Part A PDF SHA-256 `7c2b6b9725df9723e2837a42faf96cb5af8d821a7936b401d6ca58fbcc394a2c`; extracted-text SHA-256 `a85bad1bcdb2de70c14364e057a106e5d6ef3aa00867e15526053826682810da`. Part C PDF SHA-256 `b9c10b591345425c209d71dafbed02bce8606534ba4d55f970de9682fb316ba7`; extracted-text SHA-256 `19f3a802edd413085b6437824cb075d84e03bbf0f12b4d5f68ac75b1f39846d6`. Keep both original PDFs and derived text outside `sources/` until the separate import and Registry contract is followed.

Use the standard hub for discovery, then fetch large exact PDFs one at a time with the shared reader and convert with shared PDF tooling. TypeSafe Choice (`jev-1.13.0`, confidence 1.0) recommended keeping exact scope/reader guidance and receipts in this repo skill while leaving PDF/text copies in isolated evidence storage until the normal import/Registry contract is followed. No PCAF-specific script is justified: the observed work uses the existing governed reader and generic PDF extraction. Candidate promotion, publication-date validation and Registry sync remain unverified.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- Exact resource pages and current PDFs were readable, but configured seed discovery produced no candidate rows because discovered links fell outside the frozen origin/path scope. Keep the distinction between exact-read success and discovery coverage; do not widen scope from this prompt hint.
