# Article Content Adapter (Issue #92)

This document is the contract for the thin content-adapter layer introduced
by Issue #92. It is *not* a downstream contract — the adapter is a pure
pluggable surface that consumes the post-#91 unique-candidate set and emits
a versioned `article-evidence.v1` artifact that Issue #93 can consume.

Reference: [`.issue92/ACCEPTANCE.md`](../.issue92/ACCEPTANCE.md) (Issue #92
acceptance criteria AC-5..AC-7).

## Why this exists

`scripts/step1_pillar_a.py` recovers article URLs from `new_content`
unified-diff snippets. The candidates flow through Step 2 → Step 3, are
deduplicated by `climate_monitor.dedupe.canonical_url` (the URL-first
post-#91 unique set), and are eventually passed to `scripts/run_climate_monitor.py`.

Issue #92 stands between the unique-candidate set and the next layer (Issue
#93). It does **not** fetch content itself: it consumes the ferryhe/
`web_listening#70` public contract for one URL → one record. Today the
contract is **not yet available**, so every record is honest URL-only state
with `status="unavailable"`.

## Public API

```python
from climate_monitor.article_content_adapter import (
    check_dependencies,
    fetch_article_content,
    collect_evidence,
    build_article_evidence_artifact,
    write_article_evidence_artifact,
    article_evidence_artifact_path,
    ARTICLE_EVIDENCE_SCHEMA,
    ARTICLE_EVIDENCE_SCHEMA_VERSION,
)

status = check_dependencies()        # "available" | "partial" | "unavailable"
record = fetch_article_content(article_id, url, providers=...)
records = collect_evidence(unique_articles, providers=...)
artifact = build_article_evidence_artifact(unique_articles, report_date=...)
write_article_evidence_artifact(source_dir, report_date, artifact)
```

### `check_dependencies()`

Probes the upstream `web_listening.contracts.article_content` module via
`importlib.import_module`:

* **`"unavailable"`** — module not importable (current production state).
  Every record is URL-only with `status="unavailable"` and
  `failure_reason="web_listening#70 article_content fallback policy not yet available"`.
* **`"partial"`** — module importable but no `PROVIDERS` iterable declared.
  Adapter does not fabricate content; treat the same as `unavailable` for
  evidence-emission purposes.
* **`"available"`** — module importable with a non-empty `PROVIDERS` iterable.
  Adapter invokes the first provider in `providers=...` once per article.

No code path may fabricate content. When the helper reports
`"unavailable"` or `"partial"`, the adapter never touches the network and
never imports a network-capable library.

### `fetch_article_content(article_id, url, *, providers=())`

One URL → one record. When the dependency check returns `"unavailable"` or
when `providers` is empty, the call returns an URL-only `unavailable` record
without invoking anything. When a provider raises, the record is marked
`status="failed"` and `failure_reason` contains the exception class name
plus the message — no partial artifact, no swallowed traceback.

### `collect_evidence(unique_articles, *, providers=())`

The runner: it deduplicates inputs by `(article_id, url)` (first occurrence
wins, input order preserved) and emits **exactly N** records, where N is the
count of unique inputs. Each distinct `article_id` triggers exactly one
upstream acquisition chain (one provider invocation). Distinct URLs that
happen to share a title still produce two distinct records because identity
is URL/ID-based, not title-based.

Records are emitted in input order. Every record carries a deterministic
`record_hash` so consumers can recompute it from
`RECORD_DIGEST_VERSION + "\n" + canonical_json_bytes(record_without_record_hash)`.

## Versioned artifact: `article-evidence.v1`

The wired entrypoint writes
`<source_dir>/article-evidence.v1_<YYYY-MM-DD>.json` with the documented
schema (also embedded as `ARTICLE_EVIDENCE_SCHEMA` for `jsonschema`
validation):

```json
{
  "schema_version": "article-evidence.v1",
  "report_date": "2026-09-14",
  "generated_at": "",
  "dependency_status": "unavailable",
  "record_count": 2,
  "records": [
    {
      "article_id": "https://example.org/a",
      "requested_url": "https://example.org/a",
      "final_url": null,
      "status": "unavailable",
      "attempts": [],
      "selected_method": null,
      "content_type": null,
      "content_ref": null,
      "content": null,
      "content_hash": null,
      "summary_basis": null,
      "failure_reason": "web_listening#70 article_content fallback policy not yet available",
      "record_hash": "<sha256>"
    }
  ],
  "artifact_digest": "<sha256>"
}
```

* `schema_version` — `"article-evidence.v1"` (string).
* `report_date` — ISO 8601 calendar date (string).
* `generated_at` — populated by the orchestrator when it writes the
  artifact; the adapter-only helper leaves this empty so consumers can
  distinguish the adapter-only path from the wired path.
* `dependency_status` — copied from `check_dependencies()` at build time.
* `record_count` — integer, must equal `len(records)`.
* `records` — ordered list of evidence records, one per unique
  `(article_id, url)` input.
* `artifact_digest` — `sha256(ARTICLE_EVIDENCE_DIGEST_VERSION + "\n" +
  canonical_json_bytes([record["record_hash"] for record in records]))`.

### Record fields

| field | type | meaning |
|---|---|---|
| `article_id` | string | The URL-first identity (canonical URL when the upstream contract returns no `article_id`). |
| `requested_url` | string \| null | URL that the caller asked the adapter to fetch. |
| `final_url` | string \| null | URL the upstream chain ended on (after redirects). May equal `requested_url`. |
| `status` | string | One of `ok`, `no_content`, `failed`, `unavailable`, `deferred`. |
| `attempts` | array | Ordered list of `{provider, status, reason}` records. Empty when `dependency_status` is `unavailable`. |
| `selected_method` | string \| null | Provider that produced `content_hash` (`http`, `browser`, `stealth`, …). Null when `status != "ok"`. |
| `content_type` | string \| null | MIME type / content class (e.g. `text/html`). Null when no content was produced. |
| `content_ref` | string \| null | Stable reference (blob path, object-store key, etc.) when content is held outside the artifact. Null when no content was produced. |
| `content` | string \| null | Bounded body for very small artifacts. The adapter does **not** embed large bodies; use `content_ref` instead. Null when no content was produced. |
| `content_hash` | string \| null | SHA-256 of the canonical content bytes. Null when no content was produced. |
| `summary_basis` | string \| null | Which upstream signal grounds the summary (page / search_result / change_event / upstream_artifact). Null when the adapter cannot fetch. |
| `failure_reason` | string \| null | Populated when `status` is `unavailable` or `failed`. Always points at the upstream cause. |
| `record_hash` | string | Versioned SHA-256 of the canonical record bytes (excluding `record_hash` itself). Recomputable from `RECORD_DIGEST_VERSION + "\n" + canonical_json(record)`. |

### Honest "unavailable" path

When `web_listening#70` is not landed (today's production state):

* `check_dependencies()` returns `"unavailable"`.
* `fetch_article_content` returns an URL-only record with
  `status="unavailable"`, `selected_method=null`, `content=null`,
  `content_hash=null`, `summary_basis=null`,
  `failure_reason="web_listening#70 article_content fallback policy not yet available"`,
  `attempts=[]`.
* `collect_evidence` emits the same shape for every unique input. No record
  is silently dropped. No content is fabricated.
* `build_article_evidence_artifact` reports `dependency_status="unavailable"`
  and includes the honest records verbatim.

This is the contract guarantee. Issue #93 can rely on the artifact shape
*without* needing to re-implement the dependency probe.

## Wiring: `scripts/run_climate_monitor.py`

The wired entrypoint is **additive only**. It does **not** modify Steps
1–5, does **not** touch the Registry, and does **not** introduce a
parallel default fetch pipeline.

After `run_monitor(...)` returns, `_stage_article_evidence` is invoked:

1. Reads `result.items` (already deduplicated to the post-#91 unique
   candidate set by `orchestrator`).
2. Builds the `article-evidence.v1` artifact via
   `build_article_evidence_artifact(unique_articles, report_date=...)`.
3. Atomically writes it under `<source_dir>/article-evidence.v1_<date>.json`.

If staging fails for any reason the failure is logged as a warning and the
report write is not aborted. Steps 1–5 are unchanged.

## Non-goals

This adapter does **not**:

* Summarise or classify articles.
* Generate, send, or schedule anything.
* Touch the Registry or any production state.
* Implement the `web_listening#70` fallback (that contract lives in
  `ferryhe/web_listening` and is consumed, not implemented, here).
* Parse `web_listening`'s SQLite or copy its retry policy.
* Introduce a new framework or speculative abstraction.

## Verification

The wired entrypoint is exercised end-to-end by
`tests/test_article_content_adapter.py::test_run_climate_monitor_wires_article_evidence_artifact`.
The focused adapter unit tests cover AC-5 (`check_dependencies`, `fetch_*`,
`collect_evidence`, dependency-status honesty, failure-path honesty,
one-acquisition-per-article-id invariant) and AC-7 (artifact schema and
digest determinism). The parser regression tests in
`tests/test_step1_pillar_a_parser.py` cover AC-1..AC-4 against the live
`step1_pillar_a.extract_articles_from_changes`.
