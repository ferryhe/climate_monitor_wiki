# Owner-agent verification and independent publication

This schema23 route records an owner-authorized agent source check. It is not a
Hermes T1 run, TypeSafe result, or historical manual receipt. No scheduler changes
are needed. Original PDF bytes, observations, stored fields, checks, and receipts
remain immutable. Captures and original dispositions are private; public readers
receive only independently reviewed source-supported fields and citation metadata.

## Export a live plan

Run with the actual canonical database outside the checkout. The plan is read-only
and canonicalizes linked identities. It binds all current targets, their original
fields/revisions/PDF page and payload hashes, the live source snapshot, and the
latest/published/visibility pointers. Reconcile changed live plans; do not replace
old bindings in an audit result without rechecking it.

```sh
python -m climate_registry plan-owner-check --database /data/registry.sqlite3 --kind pdf_meeting --id EVENT_ID > check.json
```

For article classification that is independently reviewed with corrected source
facts, request the explicit v2 scope:

```sh
python -m climate_registry plan-owner-check --database /data/registry.sqlite3 --kind article --id ARTICLE_ID --with-editorial-metadata > check-v2.json
```

The v2 plan freezes the current article taxonomy. Each article observation must
include `editorial_metadata`: either `null` (an explicit source-only hold) or a
scope bound to that observation's capture SHA and corrected public-fields SHA.
Every category and keyword needs its own literal excerpt, record context, and
editorial reason from that same capture. The current taxonomy limits output to
1–4 exact categories and 3–8 specific keywords; unsupported labels, generic
padding, and evidence copied from another observation are rejected. A source-only
hold remains visibly uncategorized and must not inherit legacy enrichment.

V2 uses a separate `owner-agent-information-review.v2` receipt with the explicit
`source_fields_and_article_editorial_metadata` review scope. A v1 receipt cannot
approve v2 metadata. V1 plans, packet hashes, receipts, and their intentionally
empty owner classification projection keep their existing behavior. The flag is
not available for meeting records.

Fill the exported object with actual `actor: {id, task_id}`,
`authorization_reference`, `audit_reference`, UTC `checked_at`, and `observations`.
Each observation has:

- `target`: one exact unchanged entry from `plan.targets`.
- `access_status`: `accessible`, `unavailable`, or `failed`.
- `capture`: `requested_url` (exact target URL), actual `final_url`, `title`,
  `captured_at`, `tool`, `text`, `bytes_base64`, and `sha256`. Archive the actual
  UTF-8 rendered tool-output bytes; decoding them must equal `text` exactly.
  A screenshot filename, narrative conclusion, or locator alone is insufficient.
- `original_comparisons`: every nonempty original `target.fields` key except
  `source_document_sha256`, mapped to `{status: supported|missing|conflict}`.
  These are dispositions of the original claims, separately inspected at review.
- `public_fields`: the complete supported public scope. Articles require useful
  `title` and body `summary`, with optional day-precision `publication_date`.
  Meetings require `name`, explicit `date_precision`, and `start_date` (plus the
  entire explicitly printed end bound) or day-precision `deadline_date`.
  Other meeting fields are optional and require their own support. Native meeting
  storage also requires source-supported `event_type` and `status`.
- `field_evidence`: exactly the public field keys. Each value has `value` matching
  the proposed value, literal `excerpt`, and literal `record_context`, both present
  in the capture. Summary evidence additionally has `section: article_body` and
  `support: complete`: a faithful paraphrase is allowed, but the independent
  reviewer must check every material factual claim against the literal excerpts.
  Other text values use official literal wording (case differences are allowed).
  ISO dates and precision must match deterministic parsing of the quoted explicit
  date. For example `9 to 20 November 2026` supports start `2026-11-09`, end
  `2026-11-20`, precision `day`; a missing year never acquires one from the PDF.
- `excluded_fields`: sorted nonempty original field keys (except document SHA)
  whose values differ from, or are absent in, `public_fields`. No excluded raw
  prose, classification, relevance, venue, timezone, or deadline fills the scope.

Example field evidence:

```json
{"name":{"value":"COP31","excerpt":"COP31","record_context":"COP31"},
 "start_date":{"value":"2026-11-09","excerpt":"9 to 20 November 2026","record_context":"COP31"},
 "end_date":{"value":"2026-11-20","excerpt":"9 to 20 November 2026","record_context":"COP31"},
 "date_precision":{"value":"day","excerpt":"9 to 20 November 2026","record_context":"COP31"}}
```

An unavailable/unsupported observation has empty `public_fields` and
`field_evidence`. It can be recorded truthfully, but cannot establish publication.
A complete corrected public scope can be verified while the original remains
partial/conflicting. This does not label the unsupported original as verified.

## Ingest, inspect the new candidate, then independently approve

The same commands accept one object or a JSON list of up to 100 objects. Use a
list for bounded multi-entity work: all live bindings are checked before writes,
with one intake-writer lock, one Registry transaction, and one backup per phase.
Canonical identities must be unique in an ingest batch. Dry runs make no backup
or business write. Results return the exact `backup_path`; retain/verify it using
the existing operational backup policy. Use the real intake queue directory.

```sh
python -m climate_registry ingest-owner-check --database /data/registry.sqlite3 --queue-dir /data/intake --input checks.json
python -m climate_registry ingest-owner-check --database /data/registry.sqlite3 --queue-dir /data/intake --input checks.json --backup-dir /data/backups --apply
python -m climate_registry plan-owner-review --database /data/registry.sqlite3 --candidate-sha256 NEW_SHA > review.json
```

Ingest appends `owner-agent-information-check.v1` run/attempt JSON and stages a
new canonical candidate. It does not approve or publish it. The final review
must inspect that new candidate, actual capture hashes, every public field, and
original exclusions. Fill the review template with a genuinely separate actor
and task, actual authorization/audit reference, UTC `reviewed_at`, `decision:
pass`, and a useful `reason`. Keep its `source_checks` exact. Review is
`owner-agent-information-review.v1`, not a native job/session claim. A review
of the old candidate does not approve the new one.

```sh
python -m climate_registry approve-owner-check --database /data/registry.sqlite3 --queue-dir /data/intake --input reviews.json
python -m climate_registry approve-owner-check --database /data/registry.sqlite3 --queue-dir /data/intake --input reviews.json --backup-dir /data/backups --apply
```

The wrapper verifies the current live candidate, source state, evidence bindings,
independent identities, supported public scope, and then uses the existing exact
approval primitive. Exact retries are idempotent; changed live sources require
reconciliation and another review. Failed or unsupported checks cannot replace a
verified public version. Owner readiness is scoped: it does not manufacture T1
completion for unchecked sibling observations. Those retain only approved report
and page references; previously supported sibling facts retain their own proof.
Generic v2 readiness and historical manual/original-summary contracts are unchanged.

Owner-scoped projections hide original classifier output, enrichment taxonomy and
annotation metadata that the owner scope did not review. They do not alter those
stored records or the legacy/v2 metadata contract. A reviewed PDF publication date
also becomes source-backed date evidence on its linked canonical article. Meeting
Wiki views use the supported structured interval/precision/deadline when raw PDF
date text is excluded; they do not reconstruct that text.

The normal T10 verification inventory reports corrected public eligibility,
original dispositions and exclusions, pending review, and retained older verified
versions. It does not dispatch or claim a native reviewer. Publication remains
separate from activation: use the existing writer `refresh_checks(run_id)` only
after approval. Its expected-generation reload protocol preserves the prior
active projection on reload failure; verify the actual API/corpus and report
membership after a successful activation. Original PDF downloads remain independent.
