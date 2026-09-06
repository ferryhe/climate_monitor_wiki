# Article content adapter (Issue #92)

The adapter consumes the orchestrator's selected, URL-deduplicated candidates
and stages `article-evidence.v1_<date>.json` beside the report for Issue #93.
It retains PR #98's public functions and schema constant. Acquisition and
fallback policy belong to the public upstream article reader.

## Producer path

The canonical integration run uses `--manifest-fixture` with the #67 producer's
`web-listening-manifest.v1` export (`discovered_items`). The existing
`climate_monitor.web_listening_adapter.read_manifest_items` seeds the
orchestrator, which filters and deduplicates candidates before staging. The
producer owns acquisition-batch-result.v2 acquisition state and its manifest
export; this consumer does not parse that batch envelope as a manifest.
The historical SQLite/diff parser remains a compatibility entrypoint.

The consumer imports no upstream Crawler, Storage, or diff implementation and
maintains no producer checkpoint state. Staging joins manifest provenance onto
already-selected candidates by source name, item ID, and URL; it preserves
`source_item_id`, `source_name`, `source_id`, `run_id`, and the original manifest
`status` as `extra.item_status`. No new status is inferred from a later snapshot.

`tests/fixtures/article_content/manifests/climate_92_v2.json` is an independently
authored six-case consumer fixture using the inspected upstream manifest shape:

| Item suffix | Producer status | Candidate? |
| --- | --- | --- |
| html | new | Yes: ordinary HTML article |
| no-pdf | new | Yes: ordinary page link, no PDF required |
| bootstrap | existing | No: first snapshot, no prior checkpoint |
| increment | updated | Yes: normal increment |
| waiting | new | Yes: pending consumption retains its original status |
| removed | removed | No: removal is not a new article |

`read_manifest_items` returns four items in that order, with stable source/item
identity. The producer's actionable statuses are `changed`, `downloaded`, `new`,
and `updated`; the existing reader also retains its legacy absent-status behavior.
The fixture is synthetic test data, not a captured production run.

## Provider and resolver contract

Public API:

```python
from climate_monitor.article_content_adapter import (
    ARTICLE_EVIDENCE_SCHEMA, ARTICLE_EVIDENCE_SCHEMA_VERSION,
    ArticleContentAdapterError, check_dependencies, fetch_article_content,
    map_tool_result_to_record, resolve_content_ref, verify_record,
    collect_evidence, build_article_evidence_artifact,
    write_article_evidence_artifact, article_evidence_artifact_path,
    run_article_evidence,
)
```

Explicit `providers=` always wins, including when the dependency probe reports
`partial` or `unavailable`. Only `providers[0](article_id, url)` is called; the
consumer does not attempt subsequent providers. Without injection the adapter
wraps `web_listening.blocks.article_content.fetch_article_content(url)`, whose
public signature takes a URL and keyword options, not an article ID.

`check_dependencies()` reports `available` when that public reader is importable.
It retains the older contract-module/`PROVIDERS` probe for compatibility
(`partial` when only that module exists; `unavailable` when neither exists).
The probe is descriptive and does not suppress explicit providers. Importability
does not imply permission to read a site: the inspected upstream reader returns
`permission_denied/no_reviewed_profile` without a reviewed profile. This adapter
does not create or bypass acquisition authority.

Results may be real Pydantic `ToolResult` objects (`model_dump`) or mappings with
the same fields: `data_status`, `stop_reason`, `error`, and `data`, including the
ordered `data.attempts`. PR #98's flat provider mappings remain accepted as a
compatibility input. Identity fields, if supplied by the provider, must exactly
match the call. Upstream `data.sha256` becomes `content_hash`; legacy
`content_hash` is accepted when `sha256` is absent.

`resolve_content_ref(ref, hash)` delegates to upstream
`_read_evidence(_output_path(None), ref, hash)`, matching its default output
location and three-argument signature. Missing resolver support raises
`ArticleContentAdapterError("content_ref_unresolvable")`. Tests may replace
`resolve_content_ref` or attach `content_resolver(ref, hash) -> bytes` to an
explicit provider. The registered loopbacks use an in-process resolver and
write no body files. Their `memory:` references last only for that process;
they are test fixtures, not durable production references.

## Status mapping and content integrity

| Upstream data_status | Evidence status | Summary basis / failure |
| --- | --- | --- |
| present | ok | page; reference and SHA-256 required |
| present, truncated=true | ok | preview_only; extra.content_status=present_preview_only |
| no_content | no_content | none; content/ref/hash are null |
| not_found | failed | upstream stop_reason |
| auth_required | failed | upstream stop_reason |
| permission_denied | failed | upstream stop_reason |
| blocked | failed | upstream stop_reason |
| interaction_required | failed | upstream stop_reason |
| failed_quality_gate | failed | upstream stop_reason |
| error | failed | stop_reason or error code |
| redirected | no_content | none; final URL retained |
| reader not importable | unavailable | none; explicit failure_reason |

Records retain method, content type, extraction metadata (under `extra`), and
ordered attempts. A differing final URL on a non-failure, non-safety result sets
`extra.redirected=true` and marks the final attempt `redirected=true` when one
exists. Safety failures retain their failed status.

The upstream `full_text` (legacy `content`) may populate `content` only for an
untruncated successful read. A 2,000-character `truncated_preview` never becomes
the body: `content=null`, `summary_basis=preview_only`, and the complete body's
reference/hash remain available. Verification always rereads the complete bytes.

A snippet-only result requires a nonempty **input** `search_snippet`. It stays in
`extra.search_snippet` with `summary_basis=search_snippet`; it does not become
canonical body content. Upstream snippets are ignored. Candidate summaries and
generic evidence snippets are not implicitly relabeled as search snippets.
URL-only results without that input use `summary_basis=none`.

## Batch verification and atomic publication

Input identity is `(article_id, canonical_url)`, first occurrence wins. A/B
aliases sharing a canonical URL collapse to the first record and one fetch.
Different URLs with the same title remain separate. Reusing one article ID for
different URLs is rejected before any fetch. Inputs without a URL/usable identity
are rejected rather than silently omitted.

Before writing, `verify_record` checks input membership and requested URL. Every
`ok` record must resolve its reference to bytes whose SHA-256 equals
`content_hash`; an embedded full body must equal those bytes. The batch must
contain exactly one record for every unique input. Damaged references, hash or
identity mismatches, missing/extra/duplicate outputs, and malformed results raise
`ArticleContentAdapterError` and reject the **whole batch**. A preceding good
record cannot cause a partial write. An existing artifact remains unchanged.
Upstream content-reference and capture-integrity error codes also reject the
batch. Ordinary provider runtime exceptions remain honest failed records.

`build_article_evidence_artifact` validates entirely in memory. The public
`write_article_evidence_artifact` accepts an already-validated artifact, writes
a sibling temporary file, then replaces the destination. Callers supplying
manually assembled dictionaries must validate them before using that low-level
writer; `run_article_evidence` combines build and write.

Canonical JSON uses UTF-8, sorted keys, no NaN, and separators `(',', ':')`:

- `record_hash = sha256(RECORD_DIGEST_VERSION + '\n' + canonical_json(record_without_record_hash))`.
- `artifact_digest = sha256(ARTICLE_EVIDENCE_DIGEST_VERSION + '\n' + canonical_json(ordered_record_hashes))`.

The artifact carries schema version, report date, generated-at (empty unless
explicitly supplied), dependency status, record count, records, and digest.
`ARTICLE_EVIDENCE_SCHEMA` is the co-located JSON Schema for downstream validation.

## Driver wiring and local smoke

`--source-dir` takes precedence over `load_run_config(...).source_dir`; an empty
CLI value falls back to the configured directory. Staging occurs after report
writing, whenever there are items, including JSON-output mode and unavailable
dependency states. Staging failures produce a clear warning on stdout (inside
the JSON warnings array in JSON mode) and leave the written report intact.

`--article-evidence-loopback=module:callable` imports an explicit test/CI provider.
No Registry, email, publisher, reload, or scheduling operation is added.

```bash
TMP=$(mktemp -d)
python3 -m scripts.run_climate_monitor \
  --manifest-fixture tests/fixtures/article_content/manifests/climate_92_v2.json \
  --article-evidence-loopback tests.fixtures.article_content.providers:loopback_success_provider \
  --date 2026-09-07 --source-dir "$TMP/sources" --wiki-dir "$TMP/wiki" \
  --no-sync --no-update-seen-state
```

The existing report writer also emits its semantic/candidate sidecars and creates
an empty wiki directory under the override. Evidence staging adds only its own
artifact. Adapter tests cover statuses, verification, and digest replay;
`tests/test_article_evidence_for_issue93.py` validates the manifest round trip and
reads/schema-validates the artifact from an actual CLI run.
