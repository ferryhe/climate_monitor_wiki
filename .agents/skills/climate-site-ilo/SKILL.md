---
name: climate-site-ilo
description: "Use for ILO (ilo) site acquisition and coverage in climate_monitor_wiki."
---

# ILO source skill

Use this skill only for `source_key: ilo`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `ilo` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** partial configured-seed discovery; one exact first-party article body is verified. Meetings, a full weekly run and Registry remain unverified.

The 2026-05-14 scope note is historical. On 2026-09-28, the rejected legacy green-jobs seed was replaced with the verified canonical just-transition page. The configured RSS seed remains unresolved after an HTTP-status failure.

## Isolated seed check: 2026-09-28T16:03:21.471304+00:00 (UTC)

VM application revision `2aea05d`; 2/4 attempted seeds succeeded and 5 candidate rows were returned. Per-source evidence summary SHA-256: `b373e2feae944fc0cd3fe79bc1f9e53a7efff5921e86949cf43b8041107abe27`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.ilo.org/`: `success`; 2 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.ilo.org/global/topics/green-jobs/lang--en/index.htm`: `rejected`; `scope.path_not_included`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://www.ilo.org/publications`: `success`; 3 candidate rows; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://www.ilo.org/rss.xml`: `rejected`; `gateway.http_status`; 0 candidate rows; executed tools: `acquisition.web_http`.

Each successful seed produced a versioned `web_listening` SiteSkill and SiteState in the isolated runtime. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Canonical seed and exact article check: 2026-09-28

A one-off `web_listening` discovery probe used the browser-observed canonical topic page in memory, with the current reviewed include patterns and no persisted checkpoint. It succeeded (HTTP 200; robots allowed) with `acquisition.web_http`, `transform.simple_html_markdown` and `discovery.html_links`. It returned two publication candidates. Attempt ID: `job-c4a2e29be9cb46dd9026b4542759f8de-seed`; SiteSkill digest: `sha256:f49967913f62b9f27a71e3f5481488923457dad3c851ef1cdb4aab7b655755da`. The summary JSON SHA-256 is `7b4c2142659ddd0e48848d224f835516037013be986e26b104194c8e6eb8936f`.

The exact first-party article [Small businesses embrace greener practices to grow and create new opportunities in Indonesia](https://www.ilo.org/resource/article/small-businesses-embrace-greener-practices-grow-and-create-new) was fetched separately through the governed reader: HTTP 200, robots allowed, 66,324 bytes received, 9,880 Markdown characters, SHA-256 `183559df14ae04fe7b2f569d13fe34593f6bfcf6129edaebbd7172097fe38fc4`. The Markdown preserves title, date, headings and full body; it contains duplicate image-credit lines and navigation/share links. Runtime attempt ID: `job-c65fbc6cef9b45279f502ba5a50ec6cd`.

A second in-memory discovery probe added `/resource/article` to the include patterns but still returned the same two publication candidates. Do not persist that broader path from this evidence. TypeSafe `jev-1.13.0` classified ILO as `partial_site_viable` (confidence 1.0), chose a canonical-seed replacement (confidence 0.95), and rejected adding `/resource/article` (probability 0.0). The configured legacy seed has now been replaced by the verified canonical topic URL; the aggregate seed count has not yet been rerun. No site-specific script, article import or Registry sync was performed.

## Remaining checks

- Run a fresh diagnostic over all current ILO seeds before claiming aggregate coverage changed.
- Diagnose the configured `/rss.xml` HTTP-status failure using an official current feed path.
- Keep `/resource/article` outside persistent scope unless a bounded governed probe later demonstrates useful candidates.
- No ILO meeting page or meeting extraction was checked. Weekly report and Registry handoff remain unverified.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- The stale green-jobs entry was replaced by a verified canonical just-transition page; use that frozen seed and the official news routes for discovery. The RSS outcome remains unresolved, so do not claim full coverage from the readable article alone.
