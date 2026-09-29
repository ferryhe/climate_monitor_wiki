---
name: climate-site-issa
description: "Use for ISSA (issa) site acquisition and coverage in climate_monitor_wiki."
---

# ISSA source skill

Use this skill only for `source_key: issa`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `issa` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** the root, one in-scope climate analysis page and one in-scope official publications page were all rejected by Runtime robots policy; no content body or successful Runtime SiteSkill was produced.

The scope review on 2026-05-14 recorded: Reviewed ISSA with browser_rendered; homepage is reachable while news and analysis section seeds trigger security verification.
Treat that as historical context until a current governed run confirms it. The reviewed scope requests browser mode; verify actual browser use from the governed attempt receipts, because that YAML value currently does not select the runtime tool.

## Isolated seed check: 2026-09-28T16:08:10.743141+00:00 (UTC)

VM application revision `2aea05d`; 0/1 attempted seeds succeeded and 0 candidate rows were returned. Per-source evidence summary SHA-256: `f9e75a4de230248f3205129e6223a19386f2fc9250b473e24f342b240647630c`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://www.issa.int/`: `rejected`; `robots.forbidden`; 0 candidate rows; executed tools: `acquisition.web_http`.

No versioned `web_listening` SiteSkill was generated in this run. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Follow-up checks: 2026-09-28

Tried the exact in-scope page `https://www.issa.int/analysis/social-security-response-climate-change-and-environmental-degradation` and the official index `https://www.issa.int/prevention-research/publications` through the governed exact-URL reader. Both returned `robots.forbidden` before content was acquired; neither produced an article record. No path or origin was added to `site_scopes.yaml`.

The public search index surfaced candidate leads, including an ISSA analysis on the social-security response to climate change (dated 12 December 2023) and the ISSA publications index listing “Actuarial Considerations around Climate-Related Risks on Social Security” (June 2024). Those are discovery hints only: the bodies were not retrieved through the governed Runtime and must not be treated as ingested evidence. Do not retry through an alternate crawler or browser to evade robots. The next useful step is an upstream policy/access change or a separately governed source import that credits its actual source; until then, keep ISSA blocked.

## Next source-specific check

- Stop on robots-forbidden paths. Check for an official in-scope feed or publication endpoint; do not bypass the policy.

For a single-source run, record the run ID, date, each configured seed's outcome, actual tool and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill and SiteState in persistent runtime storage outside Git.

## Hermes production guidance

- The homepage, exact in-scope analysis page, and official publications index were rejected by robots.forbidden. Search snippets are discovery leads only; do not stage or describe them as fetched source evidence.
