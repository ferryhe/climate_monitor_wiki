# Report summaries, categories, and keywords

Historical report Markdown in `sources/` remains the source of truth. Existing
reports are not rewritten to add metadata. The registry read API exposes an
Executive Summary or article metadata only when it can parse the corresponding
source file and its SHA-256 still matches the immutable report identity stored
in the external registry.

## Historical unique-article annotations

`article_metadata/articles-*.json` stores one annotation for each unique
canonical article URL found across the historical reports through 2026-08-17.
These files do not rewrite `sources/` and do not duplicate an article merely
because it appeared in more than one report. Later weekly publication is not
blocked when a new URL has not yet received a historical annotation.

Each annotation records a concise summary, controlled categories, keywords,
the representative source URL, and its evidence basis. `original_content`
means the linked page or PDF was read when the annotation was prepared.
`official_replacement` means the historical URL was wrong or retired and a
replacement page on the same publisher's site was used. `publisher_excerpt`
means the publisher exposed a title and excerpt but kept the full article
behind a subscription boundary. `report_fallback` means no publisher content
could be retrieved and the annotation is limited to the historical report.
For the two alternate-source bases, `canonical_url` remains the Registry key
while `source_url` records the publisher page actually used. The API exposes
each distinction as provenance.

The controlled category taxonomy is versioned in
`monitoring/taxonomies/article_categories_v1.yaml`. That YAML file is the
single authority for category labels, signal mappings, count bounds, and
disallowed generic keywords. `climate_monitor` classification and historical
Registry annotation validation both load it. The accompanying
`monitoring/schemas/article_semantic_bundle_v1.schema.json` documents the
portable structural subset of the atomic `{summary, categories, keywords}`
object that the external Hermes monitor will adopt in a later, separately
reviewed migration. Schema-only validation is insufficient: the Python
validator additionally enforces taxonomy membership and identity, NFC and
whitespace normalization, case-insensitive uniqueness, and generic-keyword
rejection. It is intentionally stricter than the JSON Schema: every accepted
string must be strictly UTF-8 encodable, and all Unicode format characters
(`Cf`) are rejected, including directional controls and zero-width characters.
It also rejects the complete `Default_Ignorable_Code_Point` property frozen from
Unicode 17.0.0 `DerivedCoreProperties.txt`, including combining grapheme joiners,
variation selectors, and Hangul fillers. The checked-in range table is
deterministic and requires no runtime Unicode data download. Inputs fail closed
rather than being stripped, replaced, or normalized on the caller's behalf.
Category membership is intentionally validated from the YAML rather than
duplicated as a second enum in the JSON Schema. Each bundle binds both the
taxonomy ID and the exact taxonomy file SHA-256, independent of where the
taxonomy file is stored. Taxonomy v1 is immutable; a future label or meaning
change must add a new explicitly supported versioned identity instead of editing
v1 in place.

Repository tests use the declared `jsonschema>=4.23,<5` dependency to run real
Draft 2020-12 metaschema and bundle validation. The application does not use the
JSON Schema as a runtime substitute for the stricter Python validator.

The shared consumers load the taxonomy during module import. A missing,
unreadable, malformed, or wrong-identity default taxonomy therefore fails
startup with a contract-level `ValueError`; there is no fallback category list.

The planned external-Hermes migration keeps `web_listening` as the acquisition
engine and extends the existing monitor rather than creating another crawler:

```text
web_listening acquisition (existing per-site HTTP/browser policy)
  -> normalization, relevance filtering, Pillar assignment, and de-duplication
  -> final article selection
  -> one Hermes authoring pass produces summary + categories + keywords
  -> deterministic validation against the versioned taxonomy/schema
  -> canonical weekly Markdown
  -> 09:00 summary/PDF and later Registry import
```

The three semantic fields are one atomic bundle and are generated only after
final selection. The deterministic driver validates and renders them; it must
not fetch an article again, call another model, or invent a missing field. The
current production Hermes prompt and external `weekly_driver.py` have not yet
been moved by this contract-only change. That migration requires a separate
review of their exact dependencies and state-commit order before the 08:00 job
is changed.

At runtime, full-content Registry enrichment takes precedence, followed by the
unique article annotation, then explicit metadata embedded in a report. Batch
files fail closed if their schema, canonical URLs, taxonomy, or uniqueness is
invalid.

## Article metadata syntax

New report generation writes deterministic semantic metadata immediately after
each article summary. The in-repository monitor derives controlled categories
from its climate and actuarial signal classifiers and uses its matched monitor
terms as keywords.

The legacy structured-item syntax is:

```markdown
**Summary:** A source-backed summary. <br>
**Categories:** Physical Risk, Insurance Risk <br>
**Keywords:** flood, pricing, catastrophe model <br>
**URL:** https://example.org/article <br>
```

The weekly Pillar A/B syntax is:

```markdown
- **Article title** (web)
  - A source-backed summary.
  - **Categories:** Physical Risk, Insurance Risk
  - **Keywords:** flood, pricing, catastrophe model
  🔗 https://example.org/article
```

Values are comma- or semicolon-separated. Parsing preserves their first-seen
order and removes case-insensitive duplicates. Missing fields produce empty
lists; readers do not infer tags for old reports.

The production weekly reports are generated by the external Hermes/
`web_listening` workflow and then ingested by this repository. That generator
must adopt the weekly syntax above separately; this repository does not edit
its external prompt or configuration.

## Read API provenance

`GET /api/registry/reports/{date}` returns `executive_summary` as an ordered
string list and adds `categories` and `keywords` to each report article.

`GET /api/registry/articles/{article_id}` keeps `enrichment` reserved for
full-content enrichment. It also returns effective top-level `categories` and
`keywords`, `report_metadata`, `source_annotation`, and per-field provenance.
Possible provenance values are `content_enrichment`,
`original_content_annotation`, `official_replacement_annotation`,
`publisher_excerpt_annotation`, `report_fallback_annotation`, `source_report`,
or `null`.

`GET /api/registry/publishers` returns at most 500 deterministic publisher
choices. Each item contains the canonical `hostname` used for filtering and a
short `label` for display.
