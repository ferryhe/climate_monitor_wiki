---
name: climate-site-unep
description: "Use for UNEP (unep) site acquisition and coverage in climate_monitor_wiki."
---

# UNEP source skill

Use this skill only for source_key: unep. Read the shared workflow
(../../../monitoring/site-skill-workflow.md), then the UNEP entries in the source
inventory and reviewed site scopes. YAML defines the authoritative seeds and
paths; do not expand them from this skill.

Current evidence level: all four configured entry points succeeded through the
isolated Runtime. A fresh run of the news listing still did not yield the
browser-visible September 28 climate story as a candidate, although its exact
body was acquired successfully. Meeting extraction, weekly report generation
and Registry handoff remain unverified.

## Seed and candidate discovery — 2026-09-28

The four-entry run was 20260928T163713Z-d8eb8352 from the all-source isolated
health run. It completed 4/4 seeds and returned 8 candidate rows. Per-site
summary SHA-256: 3ff2baa89a27d401932abccf64710690efcf8718bb217f43c83551322e4e9e92.
The broader 36-source aggregate and its limits are recorded in
PIPELINE_REFERENCE.md.

A fresh one-seed check at 20260928T194951Z-8cddf654 also completed successfully:
one configured news listing, two candidate rows, summary SHA-256
ce9c400de9e5185e36fe687fb2386332b3b52e0e03eeacc1041132ee204dddcb. The selected
candidates were the Interactives index and the September 17 Global Cooling
Pledge press release. The browser showed a September 28 climate story that was
not in either fresh candidate row. TypeSafe jev-1.13.0 recommended recording
this discovery gap (confidence 0.96); do not treat exact-URL readability as
proof that ordinary seed discovery selects the newest article.

The source scope has fetch_mode set to browser, but both seed checks actually
used acquisition.web_http, discovery.html_links and
transform.simple_html_markdown. The shared workflow documents that fetch_mode
is only logged as requested_engine in this adapter; it does not command
Playwright. Keep actual tools and policy receipts as the evidence. No scope path
was changed.

## Exact latest article — 2026-09-28

The browser news listing showed “Rotting food is supercharging the climate
crisis. Here’s what to do about that.”, dated 28 September 2026 and categorized
Climate Action. The exact URL was
https://www.unep.org/news-and-stories/story/rotting-food-supercharging-climate-crisis-heres-what-do-about.

TypeSafe jev-1.13.0 selected that exact URL for the narrow governed-reader
check (confidence 1.0). Runtime job job-0059ef6ba5c54bcda6deed81d207c3a8 returned
HTTP 200 with robots.allowed using acquisition.web_http; the common
transform.simple_html_markdown succeeded. The result has 22,716 Markdown
characters and SHA-256
ee16ed52cbb81d175f2c5b95ec87dffafe5a075e3d2cb9d3d11a772409d7a59f. The date,
title, section headings, article paragraphs and links are retained. The
Markdown has 8,614 characters of site navigation before the article title and
additional resource/footer material after the story. TypeSafe recommended
recording this noise without a UNEP-specific cleaner yet (confidence 0.99);
revisit only if another verified page shows the same noise blocks retrieval or
ingestion.

The story covers food waste as a methane and climate issue, including the
Food Waste Breakthrough and waste diversion/composting measures. The browser
view was used to identify the current exact story; Runtime receipts and
Markdown are the acquisition evidence.

Evidence is stored outside Git in
$HOME/climate-site-pilot-20260928/unep/ and ignored .tmp/site-content-unep/.
No meetings were imported, no source was written under sources/, and Registry
was not run.

## Next site

Continue with the next configured source, WEF. Use the same bounded pattern:
check its configured seeds, inspect actual attempts, and verify one exact current
article before deciding whether site-specific behavior needs a script.

## Hermes production guidance

- An exact current UNEP climate article converted to governed Markdown, but seed discovery remains incomplete. Use only the reviewed climate/news paths and preserve the known discovery gap; a successful exact read does not prove broad site coverage.
