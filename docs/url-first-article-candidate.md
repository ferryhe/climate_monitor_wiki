# URL-first article candidate contract v1

This document defines the reusable discovery boundary introduced by
`url-first-article-candidate.v1`. It is a data contract and a set of pure,
read-only adapters. It is not connected to the running aggregation pipeline.

The contract has two schemas:

- `monitoring/schemas/url_first_article_candidate_v1.schema.json`
- `monitoring/schemas/url_first_article_candidate_batch_v1.schema.json`

The Python models, builders, validators, adapters, canonical serializers, and
digest functions are in `climate_monitor.article_candidate_contract`.

## Candidate fields

Every candidate has this fixed v1 field set. Optional display values use JSON
`null`; keeping the keys present makes the canonical v1 shape unambiguous.

| Field | Meaning |
|---|---|
| `schema_version` | Exactly `url-first-article-candidate.v1`. |
| `url` | A valid raw HTTP(S) URL. It is the lexically smallest raw URL retained by the origins. |
| `canonical_url` | URL identity recomputed by `climate_monitor.dedupe.canonical_url(url)`. |
| `article_id` | Stable `article-identity.v1` value recomputed by `climate_monitor.semantic_bundle.article_identity()`. |
| `display_pillar` | `A` if any retained origin is Pillar A; otherwise `B`. |
| `origins` | One or more distinct discovery-origin records in canonical origin order. |
| `title`, `title_basis` | Optional display title and its evidence basis; both are non-null or both are null. |
| `summary`, `summary_basis` | Optional display summary and its evidence basis; both are non-null or both are null. |
| `categories`, `categories_basis` | Optional, sorted, unique upstream categories and the explicit `upstream_classification` basis. |
| `candidate_digest` | Digest binding every other candidate field. |

A candidate with only its URL identity and required origin data is valid.
There is no title requirement. A caller that creates a display label from a
URL must set `title_basis` to `url`. Such a label is not page evidence and must
not be labelled `page`.

## URL and article identity

There is one canonical URL implementation and one article-ID implementation:

```text
canonical_url = climate_monitor.dedupe.canonical_url(raw_url)
article_id    = climate_monitor.semantic_bundle.article_identity({"url": raw_url})
```

`article_identity()` implements the existing formula:

```text
sha256(UTF-8("article-identity.v1\n" + canonical_url))
```

The validator calls both functions and rejects an authored value that differs.
It does not repair the value. Raw URL syntax is checked with the repository's
existing public-HTTP(S) selection validator; this module does not add another
URL parser or normalizer. The shared schema requires URI syntax, a non-empty
DNS-label/IPv4 host or bracketed URI host literal, and a numeric port when one
is present. Its structural pattern enforces the common syntax without optional
format plugins; the `uri` format annotation is supplemental. It does not
duplicate DNS resolution or private-network policy.

The existing canonicalizer removes fragments and known tracking parameters
such as `utm_*`, `fbclid`, and `gclid`. It retains semantic query parameters.
For example:

```text
raw:       https://example.org/report?edition=2026&scenario=stress&utm_source=mail#findings
canonical: https://example.org/report?edition=2026&scenario=stress
```

Only canonical URL equality establishes article identity. These do not:

- equal or similar titles;
- the same publisher;
- the same final redirect destination;
- equal fetched bytes or equal input-artifact hashes.

Therefore the same canonical URL has one `article_id`, while the same title or
the same bytes at different URLs remains separate.

## Origin fields and identity

Each origin records:

- `pillar`: exactly `A` or `B`;
- `source`: the source value supplied by that discovery path;
- `url`: the raw URL supplied by that origin;
- `input_artifact`: `{artifact_id, sha256}` for the exact input artifact;
- `row`: an RFC 6901 JSON Pointer to the input row;
- `discovered_at`: an offset-bearing RFC 3339 timestamp;
- optional `original_title` / `title_basis`;
- optional `original_summary` / `summary_basis`;
- optional `original_snippet` / `snippet_basis`.

Every optional original value and its basis must appear together. Supported
title bases are `page`, `search_result`, `upstream_artifact`, and `url`.
Summary/snippet bases are `page`, `search_result`, `change_event`, and
`upstream_artifact`.

Every candidate display title not marked with `title_basis: url` must exactly
match a retained origin's original title and basis. Every candidate display
summary must likewise match a retained origin's original summary and basis.
The URL-title exemption covers a locally derived label only; it does not claim
page or discovery evidence.

Origin rows are non-root RFC 6901 JSON Pointers. Within each pointer token,
`~0` encodes `~` and `~1` encodes `/`; every other `~` escape is invalid.
Titles are retained verbatim, including internal newlines, and are never
normalized by this contract.

The v1 timestamp lexical form is the common strict RFC 3339 subset
`YYYY-MM-DDTHH:MM:SS[.fraction](Z|±HH:MM)`. Space-separated ISO timestamps,
basic-form dates/times, ISO week dates, and offset-free timestamps are not
contract values. The Python validator and Draft 2020-12 `date-time` format
checker accept the same documented examples.

Origin identity is the exact tuple:

```text
(pillar, source, input_artifact.artifact_id,
 input_artifact.sha256, row)
```

Origins are sorted by that tuple. A merge collapses a repeated origin identity
only when the complete origin records are equal. Two records with the same
origin identity but conflicting values fail closed. Different origin
identities are all retained, including A and B discoveries of the same URL.
The batch validator separately rejects reuse of the artifact-row key
`(artifact_id, artifact_sha256, row)` across different candidate entries,
even when pillar or source differs. Reuse inside one candidate remains governed
by the full origin-identity rules above.

Every origin raw URL must canonicalize to the candidate `canonical_url`.
`display_pillar` is derived only after all origins are retained: A wins for
display when any A origin exists, otherwise the display pillar is B.

## Current-artifact adapters

`adapt_article_changes()` reads the current full
`article_changes_DATE.json` object emitted by `scripts/step1_pillar_a.py`.
It requires the current counters, `generated_at`, and grouped
`articles[].items[]` shape. It verifies counter consistency, preserves each
organization as `source`, records each item JSON Pointer, keeps the raw URL,
retains a non-empty original title with `upstream_artifact` basis, and retains
the upstream classifications.

Pillar A's current artifact does not say whether Step 1 obtained a title from
a page heading or derived it from a URL slug. The adapter therefore does not
promote that title to the candidate display title and does not claim page
evidence. A non-empty upstream string remains available on the origin.

`adapt_pillar_b()` reads the current array of exactly
`{title, url, source, summary}` rows. The current producer declares
`source: "web"`; other values fail. Search titles and non-empty summaries are
retained with `search_result` basis. Since the Pillar B array has no timestamp,
the caller supplies `discovered_at` without requiring an upstream shape change.

For both current shapes, an exactly empty `title` string means that title
evidence is absent and maps to null candidate/origin title fields and bases.
Whitespace-only titles and non-string title values remain invalid.

Both adapters also require the caller to supply the input artifact ID and
SHA-256. They return new model values and do not mutate the input object. Their
field sets are exact and malformed rows fail closed. In particular, a raw
web-listening change-event row containing only `id`, `change_type`,
`detected_at`, `summary`, and `diff_snippet` has no real article URL and is
rejected; the Issue #88 regression fixture proves this behavior.

The adapters do not alter `scripts/step3_aggregate.py`, monitor totals,
provenance, seen state, or upstream files.

## Canonical JSON and digests

`serialize_candidate()` and `serialize_candidate_batch()` first run full
Python validation. They then emit:

1. UTF-8 JSON with non-ASCII characters preserved;
2. object keys sorted lexically at every depth;
3. compact separators `,` and `:` with no added spaces;
4. no carriage returns;
5. exactly one trailing LF.

Canonical arrays have contract-defined order:

- origins by the origin-identity tuple above;
- candidates by `canonical_url`;
- categories in lexical order.

No trailing LF participates in either digest. Candidate digest is:

```text
sha256(UTF-8("url-first-article-candidate-digest.v1\n")
       + canonical_json(candidate without candidate_digest))
```

Batch digest is:

```text
sha256(UTF-8("url-first-article-candidate-batch-digest.v1\n")
       + canonical_json(batch without batch_digest))
```

The batch body includes each candidate's validated `candidate_digest`.
`candidate_digest()` and `batch_digest()` expose recomputation after full
validation. A changed identity, evidence value, order, count, or digest is
rejected rather than rewritten.

## Strict versions and validation layers

Both schemas declare JSON Schema Draft 2020-12 and use
`additionalProperties: false`. Unsupported version strings, missing fixed
fields, unknown fields, and incompatible nested shapes fail closed. A breaking
field change requires a new schema version; v1 validators do not guess how a
future shape should map.

JSON Schema validates the portable structure. Python validation is also
required before consumption because it recomputes URL/article identities,
origin identity uniqueness and ordering, display mapping, counts, and digests.
Positive and negative fixtures are under
`monitoring/fixtures/url_first_article_candidate_v1/`.

## Downstream mapping

A future consumer can map the contract without discarding provenance:

- join or key by `article_id` / `canonical_url`;
- show `title` only when non-null and carry `title_basis` with it;
- use `display_pillar` for one display slot while retaining the full `origins`;
- use origin `source`, artifact identity, and row for trace-back;
- treat summaries/categories as optional evidence, not identity inputs.

Consumers must not substitute title, publisher, redirects, or bytes for URL
identity and must not discard B origins merely because an A origin exists.

## Explicit non-goals

This contract does not implement or change:

- runtime Pillar A/B aggregation or `scripts/step3_aggregate.py`;
- seen-state or monitoring-state mutation;
- URL fetching, redirect resolution, agents, or semantic generation;
- Registry reads or writes, migrations, or historical source changes;
- Markdown, PDF, email, wiki, or delivery behavior;
- schedules, web-listening behavior, or upstream artifact schemas.
