---
name: climate-site-who
description: "Use for WHO (who) site acquisition and coverage in climate_monitor_wiki."
---

# WHO source skill

Use this skill only for `source_key: who`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `who` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). YAML defines the current seeds and allowed paths; do not substitute another source that shares the host.

**Current evidence level:** partial. An exact official news URL is robots-allowed and the governed reader produced useful Markdown. Configured seed discovery still ends in origin/path policy errors even when the news path was added in memory. Meetings and Registry remain unverified.

The 2026-05-14 scope note is historical. The 2026-09-28 fresh seed run rejected all four configured URLs (0 candidates; summary SHA-256 `75d81660c65a2a958a08adea034d9e4b92248664396d238a1ee16734d1dc2160`) with `scope.origin_not_allowed` and `scope.path_not_included`. The original per-seed run also reported 0/4 (summary SHA-256 `905a12e3f853252cd0d3ab4e554db9b3cc403e9e147232d08688daa6312a9a0c`). Do not infer which individual discovered links caused the policy errors; the earlier run's ledger is unavailable.

## Exact current news item — 2026-09-28

The official [climate and health topic page](https://www.who.int/health-topics/climate-change) is browser-readable and groups overview, news, publications, documents, events and work items. It linked the 5 August 2026 story [WHO launches global research priorities on climate, health and migration](https://www.who.int/news/item/05-08-2026-who-launches-global-research-priorities-on-climate--health-and-migration).

The exact URL was fetched through the governed article reader: HTTP 200, robots allowed, 119,603 source bytes, 5,783 Markdown characters, SHA-256 `527e054a68318cbc22ce74a6afc555a6ddef1222dfc1f6de9ff3cf58d61e98e3`, attempt `job-ec96e06225144e8ca1bcc7c0c03c5094`. The saved Markdown has the correct title, date and story body; it also contains small header placeholders (`Credits **`, empty `Reading time`). Evidence is outside the repo at `$HOME/climate-site-pilot-20260928/who-exact-article/` and ignored `.tmp/who-evidence/`.

A one-seed in-memory probe used only the exact climate topic URL, explicitly omitted the source homepage, and temporarily added `/news/item`. The HTTP request, Markdown transform and link discovery all completed, but the Runtime returned `scope.origin_not_allowed; scope.path_not_included`, no candidates and no checkpoint. Summary SHA-256: `846b9163d26e6541655691135a47362b94e3d272cee581cf2bf8ff0adea55ea0`. TypeSafe `jev-1.13.0` recommends recording this as exact-body partial access and keeping repo scope unchanged (confidence 0.98). Do not add secondary origins or a WHO-specific parser from this evidence; no official PDF was downloaded and the Events section was not opened to a detail page.

No article was ingested, no meeting was imported, no report was generated and Registry was not run. Keep generated Runtime SiteSkill and SiteState outside Git.

## Hermes production guidance

- An exact WHO climate-and-health news article was readable, while configured seed discovery still returned origin/path policy errors. Preserve both outcomes; do not add secondary origins or broader paths from this prompt hint.
