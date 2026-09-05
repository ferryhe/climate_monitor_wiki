# URL-first article candidate contract v1

This document defines the reusable discovery boundary introduced by
`url-first-article-candidate.v1`. It is a data contract and a set of pure,
read-only adapters. Step 2 now consumes this boundary in both the historical
`step3_aggregate.py` path and the modern monitor orchestrator.

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

The adapters do not mutate upstream files. Runtime aggregation builds on their
canonical URL, article identity, origin, and merge rules as described below.

## Step 2 runtime integration

`scripts/step3_aggregate.py` reads the unchanged current Step 1a/1b shapes,
computes each input file's SHA-256, calls `adapt_article_changes()` and
`adapt_pillar_b()`, and merges through `merge_candidates()`. Its historical
`aggregated_DATE.json` output remains available to later step scripts. Each
flat compatibility item now also carries `article_id`, `canonical_url`, and
the complete `origins` array.

The script also emits canonical `combined-candidates_DATE.json` bytes with
schema version `combined-candidates.v1`. The artifact contains retained
`items`, complete candidates excluded by URL history under `history_skips`,
and row identities/reasons under `invalid_rows` when a non-fatal runtime
adapter can supply them. Its counts are derived from those arrays:

- `pillar_a_rows` and `pillar_b_rows` count retained origins plus explicitly
  identified invalid rows;
- `unique_urls` counts retained plus history-skipped candidates;
- `cross_pillar_merges` counts URL identities carrying both A and B origins;
- `history_skips` and `invalid_rows` count their corresponding evidence rows.

The candidates, origins, invalid evidence, keys, digest, and trailing LF all
have canonical ordering. Swapping the A/B collection order therefore does not
change the bytes. A malformed current artifact still fails closed. Both the
historical aggregate and combined output are invalidated before input
validation so an older success cannot be reused after a failed run.

The modern `run_monitor()` path performs the same URL merge and history check
before relevance classification. It writes the combined artifact and a
canonical `candidate-items_DATE.json` snapshot beside the final Markdown and
semantic sidecar, and commits all four with the existing recoverable
pending-file protocol. The snapshot retains every report-relevant
`CandidateItem` field for every retained combined item, is bound to the report
date plus the exact report and combined-artifact SHA-256 values, and is
validated against the shared canonical URL/article identities. It does not
alter the semantic sidecar or Markdown contracts. The optional CLI pair
`--article-changes-artifact` / `--pillar-b-artifact` lets a controlled run use
the exact current Step 1 artifacts; it cannot be mixed with manifest/research
fixtures.

If classification or semantic selection produces no report, the modern path
still atomically publishes the validated combined-candidate evidence by itself;
it does not create empty Markdown/semantic output or advance URL state. If a
process stops after the full report bundle commits but before URL state does,
the pending delta's own report date and combined-evidence digest locate the
correct bundle; the modern delta also binds the exact expected Markdown and
candidate-item snapshot digests. A new-flow pending delta cannot advance URL
state if that snapshot is missing or inconsistent.
The next state-enabled invocation validates that bundle,
applies the delta idempotently, and then either returns the recovered same-date
run or continues a later report date. A different-date run cannot silently
discard or overwrite an older transaction.

A same-date successful replay validates the existing complete report, semantic
sidecar, combined evidence, and candidate-item snapshot before reading history.
Its validated `items` and full item state (but not old `history_skips` or
`invalid_rows`) are carried into the shared URL-first merge alongside the
current inputs, retaining their original origins, classifier evidence,
timestamps, semantics, and document metadata. A pre-snapshot bundle may be
returned unchanged when there is no new input, but it is never incrementally
rewritten from the lossy semantic sidecar reconstruction.
URLs rendered by that bundle are treated as that date's own history and remain
eligible, while genuinely older URLs remain excluded. Thus a promoted live
checkpoint may emit no old row, and an incremental legacy artifact may contain
only the new row, without either path dropping the committed articles. A live
replay with no new inputs returns the verified bundle without rewriting it.
An incomplete or inconsistent same-date bundle fails closed before acquisition.

Title state is compatibility-only. Neither runtime path reads or writes
`seen_titles` for identity or exclusion, and existing title-state files are
left byte-for-byte untouched. URL history uses a two-phase delta:

1. aggregation prepares a pending canonical-URL delta without changing the
   canonical state file; the delta records the report date and exact
   `combined-candidates.v1` byte digest, plus the expected Markdown and
   candidate-item snapshot digests when the modern path has already rendered
   them;
2. the delta is atomically applied only after all final report artifacts have
   committed successfully.

If a report transaction is interrupted before a complete bundle exists, a
well-formed pending delta is discarded only when its recorded canonical-state
base is still byte-identical; the candidates are then collected again. A
damaged delta, changed base, or inconsistent committed bundle remains a
fail-closed error. `--no-update-seen-state` never applies, discards, or
overwrites an existing pending transaction. For a same-date modern pending it
validates and returns the already committed bundle without writing any of its
paths; a different-date no-update run remains isolated and usable.

For the modern path this happens inside `run_monitor()`. In the historical
step sequence, Step 1 neither suppresses URL-history rediscoveries nor writes
newly found URLs; Step 3 owns the merge/history split. The first call to
`step2_save_state.py` is intentionally non-mutating. After Step 5 succeeds,
run `step2_save_state.py --date DATE --commit-pending`; it verifies the final
Markdown, report JSON evidence, and canonical combined artifact before applying
the delta. The pending transaction records the combined artifact SHA-256, and
the report evidence counts and rendered URLs must match that candidate set, so
the historical steps cannot commit against a stale same-date report.
When a complete same-date report already exists, Step 3 writes the next
combined evidence to a staged sibling and leaves the committed Markdown,
report JSON, and canonical combined bytes unchanged. Step 5 validates and
recoverably promotes those three artifacts together, then removes the staged
combined file. When Step 3 uses `--combined-output PATH`, pass the same path to
Step 5 as `--combined PATH` and to Step 2 as `--combined PATH`; the default path
remains unchanged.
`--dry-run` verifies without applying it, while
`--no-update-seen-state` leaves both canonical state and pending state alone.
The historical filenames are retained for compatibility.

Live web-listening link checkpoints follow the same publication boundary.
Each seed writes a staged checkpoint during collection; a candidate-bearing
checkpoint is promoted only after the report bundle and canonical URL delta
both commit. A rejected/no-report candidate or an interrupted run therefore
leaves the canonical source checkpoint unchanged and is rediscovered on retry.
Bootstrap, no-new-link, and history-only checkpoints may advance after a
successful no-report run because they contain no uncommitted candidate URL.
`--no-update-seen-state` neither stages nor commits these source checkpoints.

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

- URL fetching, redirect resolution, agents, or semantic generation;
- Registry reads or writes, migrations, or historical source changes;
- Markdown, PDF, email, wiki, or delivery behavior;
- schedules, web-listening behavior, or upstream artifact schemas.
