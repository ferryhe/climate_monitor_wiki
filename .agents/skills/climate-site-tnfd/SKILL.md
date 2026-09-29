---
name: climate-site-tnfd
description: "Use for TNFD (tnfd) site acquisition and coverage in climate_monitor_wiki."
---

# TNFD source skill

Use this skill only for `source_key: tnfd`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `tnfd` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). The YAML entries, not this skill, define the current seeds and allowed paths. Do not substitute another source that happens to share a host.

**Current evidence level:** the official news UI is readable in a browser, the canonical publication page was acquired through the governed reader, and one exact PDF was acquired and converted under a temporary scope; recurring article discovery and Registry remain unverified.

The scope review on 2026-05-14 recorded: Reviewed TNFD with browser_rendered; HTTP feed works, but rendered pages expose richer news and publication links.
Treat that as historical context until a current governed run confirms it. The reviewed scope requests browser mode; verify actual browser use from the governed attempt receipts, because that YAML value currently does not select the runtime tool.

## Isolated seed check: 2026-09-28T16:08:32.509788+00:00 (UTC)

VM application revision `2aea05d`; 2/4 attempted seeds succeeded and 2 candidate rows were returned. Per-source evidence summary SHA-256: `ee5ae32450b8044b53c1c77d3e7643bc285c86452a281cd55f204d30060272c6`. This run used a fresh `web_listening` Runtime without an active browser tool.

- `https://tnfd.global/`: `success`; 1 candidate row; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://tnfd.global/news/`: `incomplete`; `acquisition.interaction_required`; 0 candidate rows; executed tools: `acquisition.web_http`.
- `https://tnfd.global/tnfd-publications/`: `success`; 1 candidate row; executed tools: `acquisition.web_http`, `discovery.html_links`, `transform.simple_html_markdown`.
- `https://tnfd.global/knowledge-bank/`: `rejected`; `gateway.redirect_invalid`; 0 candidate rows; executed tools: `acquisition.web_http`.

Each successful seed produced a versioned `web_listening` SiteSkill and SiteState in the isolated runtime. This pass did not verify discovered article bodies, meeting extraction, weekly reporting or Registry handoff.

## Dated follow-up: 2026-09-28

VM application image `2aea05d`; isolated Runtime and external evidence directory `/home/ubuntu/climate-site-pilot-20260928/tnfd`. No production files, `sources/`, or Registry were changed.

- One-source run `20260928T190422Z-9cf00010` (summary SHA-256 `783e3406dbf9235c7015d86d4a90afcdd2ae7126e1f9c2f9226a990cc719d618`) was partial: 2/4 seeds succeeded. Home and publication seeds each yielded the same generic Knowledge Bank candidate. `/news/` returned `acquisition.interaction_required`; `/knowledge-bank/` returned `gateway.redirect_invalid`.
- The ordinary in-app browser rendered `/news/` and showed 90 results. Its newest item was “TNFD releases final sector guidance for technology and communications,” dated 22 September 2026. The exact article page was visible in the browser, but an isolated governed exact-URL read returned `acquisition.interaction_required` with no body artifact. Treat browser content as discovery evidence only.
- Following the article's exact official publication link in the browser reached the canonical `/publication/additional-sector-guidance-technology-and-communications/` page, which is already under reviewed `/publication/`. A governed read returned HTTP 200 and 5,763 Markdown characters; content SHA-256 `276336874baaf3270e4ed9a9b1222c1e0fccf445d705a601c6f733db57412486`. The page is final guidance, Version 1.0, last updated September 2026, but the generic Markdown transform omitted that metadata and the direct PDF download link.
- The browser exposed the exact file `/wp-content/uploads/2026/09/Additional-sector-guidance-Technology-and-communications_DIGITAL.pdf?v=1790082323`. The governed reader rejected the query-bearing URL as `web_http.url_redacted`; a one-off TypeSafe-approved retry to the exact same path without its changing `v` query returned HTTP 200, robots allowed. The verified Runtime source artifact was 3,190,624 bytes, SHA-256 `262a703673ea7a44aca6bdae9db11469b6ecf44fcc72fd8c960a310e6e16ed97`. The article adapter has no PDF-derived Markdown.
- Shared `pypdf` 6.10.2 extracted 135 pages / 231,970 characters. The cover identifies September 2026, Version 1.0. The guidance applies TNFD's LEAP assessment and disclosure metrics to technology and communications, including semiconductors and data centres, with substantial freshwater-dependency and water-stress coverage. Extracted Markdown SHA-256: `68b7f5d241154fc5be44c93c587fb1057a64476542a3063ba21242e440ae5496`.
- A temporary exact-path site probe did not promote the PDF as a candidate; it returned one unrelated existing Knowledge Bank candidate. TypeSafe Choice (`jev-1.13.0`) slightly favored leaving the persistent TNFD scope unchanged over adding this exact path (confidence 0.40; probabilities 0.52 vs 0.45). Keep the PDF and conversion external; do not add `/uploads/` broadly. No TNFD-specific script is needed for generic PDF extraction.
- The YAML `fetch_mode: browser` did not correspond to a browser attempt in this site-refresh run: the receipt lists only `acquisition.web_http`. Trust the recorded tools, not the mode label. Browser UI discovery and governed Runtime acquisition are separate evidence paths.

## Remaining gaps

A current news list is visible in the ordinary browser, but the governed reader cannot acquire its index or a direct news article. The publication page and PDF are readable through bounded exact paths, but the normal collector did not turn the PDF into a candidate. Repeated weekly discovery, article Markdown through the governed Runtime, and Registry handoff remain unverified. The exact PDF path is not in committed scope; only the temporary probe allowed this one acquisition.

For each single-source run, record the run ID, date, configured seed outcomes, actual tools and policy attempts, article Markdown/content hashes, meeting evidence when present, and Registry disposition. Add only verified site-specific lessons here. Keep generated `web_listening` SiteSkill, SiteState and raw content outside Git.

## Hermes production guidance

- The homepage and publications area work, while news and knowledge-bank attempts have interaction/redirect failures. One PDF conversion used temporary diagnostic scope; acquire only URLs admitted by this run’s frozen scope and keep recurring news coverage partial.
