---
name: climate-site-sif
description: "Use for SIF (sif) site acquisition and coverage in climate_monitor_wiki."
---

# SIF source skill

Use this skill only for `source_key: sif`. Read the [shared workflow](../../../monitoring/site-skill-workflow.md), then the exact `sif` entries in [source inventory](../../../monitoring/supranational_sources.yaml) and [reviewed site scopes](../../../monitoring/site_scopes.yaml). YAML defines the current seeds and allowed paths; do not substitute another source that shares the host.

**Current evidence level:** partial. Both configured seeds work and a known exact resource page can be read through the governed reader, but normal seed discovery does not surface that resource. Its linked PDF is acquirable, while generic text extraction is not clean enough to trust all labels. Meetings, weekly output and Registry remain unverified.

The 2026-05-14 scope note is historical. On 2026-09-28, the two configured seeds succeeded in a fresh Runtime (`2/2`, four candidate rows; summary SHA-256 `41e9db45f60a9340e28a5065103b5c0c85d703fae9621f008cf4a9dbecda9781`). Tools were `acquisition.web_http`, `discovery.html_links` and `transform.simple_html_markdown`.

## Exact resource and discovery check — 2026-09-28

The browser-visible [SIF survey resource](https://sdgfinance.undp.org/resource-library/nature-insurance-nexus-results-sif-survey-nature-related-risks) is titled “Nature-insurance nexus: Results of SIF survey on nature-related risks” and dated 22 April 2025. The governed HTML reader returned HTTP 200, robots allowed, 6,217 Markdown characters, SHA-256 `4ad682f5d8d897bf0e34e1d6a897d1ff2b5b51513b316f92b780c46d1bdbe788` (attempt `job-809e098059524fd7b009e5f23b381c21`). It retains the title, date, summary and PDF link, with navigation/share/footer noise.

The linked [official PDF](https://sdgfinance.undp.org/sites/default/files/2026-06/Nature-%E2%80%93-Insurance-Nexus-Results-Final.pdf) was fetched through the governed reader with HTTP 200 and robots allowed. The source artifact was 1,828,984 bytes, SHA-256 `0410d870c202f2386a5c21ec761f323263b0884c8e2c595bd18d30f623fb7d43`. The article adapter did not produce cleaned text. The shared `pypdf` extraction produced one page and 4,157 text characters, but ligature and reading-order artifacts make individual infographic labels uncertain. Do not quote its numeric results without visual validation. TypeSafe `jev-1.13.0` chose the shared PDF extractor over an SIF-specific parser (confidence 1.0).

An in-memory discovery probe added only the exact `/resource-library/nature-insurance-nexus-results-sif-survey-nature-related-risks` path. Both seeds still succeeded and returned four candidate rows, but the exact report was absent; summary SHA-256 `a643fc5810a9123cef89887258aa45ec605ca1b8223b3b3e2c90996db674b110`. TypeSafe recommends recording partial coverage and keeping scope unchanged (confidence 0.99). Do not add all of `/resource-library`, persist this path, or create a SIF PDF parser from this evidence. Use the governed exact reader for a known URL; discover new URLs from the SIF page in the browser.

No article was ingested, no meeting was imported, and Registry was not run. Keep generated Runtime SiteSkill and SiteState outside Git.

## Hermes production guidance

- The official seeds work, but normal discovery misses one exact resource page. A linked PDF was fetched while generic text extraction did not preserve all labels; do not claim clean PDF extraction or add broad resource paths from the exact-page probe.
