# Article content adapter

`climate_monitor.article_content_adapter` turns each selected canonical URL into
one hash-bound `article-evidence.v1` record. It does not choose or implement a
crawler. The only production provider is the public `web_listening` URL-fetch
workflow pinned by `requirements.txt` to `web_listening_new` revision
`ac2343f89bc7939736d85f049ebe2beac571034a`.

## Runtime path

Site acquisition and selected-URL retrieval must open the same persistent
`RuntimeService` root. Production sets:

```text
CLIMATE_WEB_LISTENING_DATA_DIR=/opt/web-listening-data
```

The managed producer mounts the dedicated whole-root named volume at that path.
The root owns lifecycle state, jobs, artifacts, browser runtimes and SQLite
journals. Do not point either adapter at a host runtime, a single database file,
or a second per-caller root.

For one URL the adapter:

1. validates the candidate against its unique frozen source binding;
2. builds a public `Request` with one canonical seed, that seed's reviewed
   origin, its exact path, `explore_all_tools=True`, and finite
   request/byte/time/tool-attempt budgets;
3. executes it through the targeted public `RuntimeService.retrieve` operation,
   which atomically claims only that request and leaves older submitted work pending;
4. reads the exact caller-owned terminal job with `get_owned_job`;
5. preserves the complete generic Job/Result envelope and every actual attempt,
   artifact, exclusion and failure;
6. opens the selected derived artifact through
   `RuntimeService.open_owned_artifact`, then checks size, SHA-256, UTF-8 and
   non-empty content before returning it.

The record retains the source artifact, selected derivative, selected
acquisition tool and the complete upstream Runtime Job/Result under
`extra.extraction_metadata`. The cleaned Markdown is inline for immutable report
staging; its `content_ref` remains the upstream artifact identity. The original
source is not overwritten or relabelled as cleaned content.

The exact-path request deliberately does not navigate HTML links or authorize a
redirect to another path or origin. Those cases remain explicit coverage gaps
with the Runtime's real rejection evidence. A candidate already discovered on a
reviewed secondary origin is submitted separately under that origin and exact
path. The adapter never widens a frozen source scope to follow it.

## Budget and failure behavior

A managed request ledger reserves the request maximum before Runtime dispatch.
When the Runtime returns measured usage, the ledger reconciles to its actual
network-request count. A failure without measured usage keeps the reservation
spent. This prevents a crash or unknown failure from manufacturing unused
capacity.

Only tools that the Runtime reports installed, enabled, healthy, qualified and
eligible can run. The adapter does not activate tools, broaden scope, retry
outside the request, or reinterpret HTTP as browser execution. Authentication,
robots, network and scope refusals remain failed evidence with their concrete
attempts and error codes. Failure for one URL produces an honest record and does
not stop other URLs.

A successful evidence record requires verified complete content. If retrieval
has no selected derivative, the record is `failed`; an input search snippet may
be retained as `summary_basis=search_snippet`, but it never becomes fetched
content. No content and no snippet remains `summary_basis=none`.

## Identity and publication

Canonical URL identity and input order are preserved. Duplicate URLs collapse
before fetch while conflicting article IDs fail closed. Every record is hashed,
and the artifact digest binds the ordered record hashes. Prepare stores this
artifact before authoring; resume and finalize revalidate report date, candidate
set, record hashes, content hashes and selection bindings.

Explicit provider callables remain a test/CI seam. They must return the same
validated data shape and cannot be combined with a managed request budget. Local
fixture providers do not establish live Runtime, browser or production coverage.

## Deployment checks

Before enabling the monitor, verify in the exact producer image and mounted
Runtime root:

- the installed `web-listening` direct URL names revision
  `ac2343f89bc7939736d85f049ebe2beac571034a`;
- lifecycle inspection and the method catalog agree on browser qualification;
- a controlled upstream fixture qualification succeeds inside that container;
- Runtime reopen after container recreation keeps lifecycle and artifact hashes;
- the bounded exact-20 comparison records actual attempts and exclusions;
- report/PDF rehearsal uses real eligible evidence or completes honestly with no
  report, with delivery kept in no-send mode.

Production changes, scheduler updates and qualification are controller-owned
after review and merge.
