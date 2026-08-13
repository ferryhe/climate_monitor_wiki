# Article Registry

This module builds and incrementally updates a SQLite registry from the Markdown
reports already in `sources/`. It remains operationally isolated: it does not
change the 08:00 Hermes monitor, canonical Markdown, the publisher, the website,
or any scheduled job.

## Boundaries

- Code, migrations, tests, and this contract live in Git.
- Runtime SQLite files, WAL/SHM companions, and generated audit output do not.
- `audit-history` refuses to open an existing database or overwrite an existing
  output directory. Every audit snapshot therefore starts with new destinations.
- Persistent updates are explicit CLI operations. Nothing in this repository
  schedules or automatically invokes them.
- Source Markdown is read-only. The command never rewrites, renames, or deletes
  a report.
- A server deployment should place the database outside the checkout,
  for example `/home/ubuntu/climate_monitor_data/registry/article_registry.sqlite3`.
  The repository does not create that path by default.

## Run an isolated audit

Use explicit new paths (a temporary directory is recommended):

```bash
python -m climate_registry audit-history \
  --source-dir /path/to/climate_monitor_wiki/sources \
  --database /new/path/article-registry.sqlite3 \
  --output-dir /new/path/article-registry-audit
```

The command returns one JSON status line. It creates:

- a SQLite database at the requested path;
- `duplicate-report.json` with repeated canonical URLs, same-title/different-URL
  collisions, within-report duplicates, cross-pillar repeats, and observed
  title/summary version changes;
- `weekly-manifests/weekly-manifest-YYYY-MM-DD.json` for reports in the current
  Pillar A/B weekly format.

The 21 legacy daily reports are backfilled into SQLite for history and duplicate
analysis. Weekly manifests are emitted only for the current weekly format; this
avoids representing legacy reports as if they satisfied the newer contract.

## Plan and apply a persistent update

`plan-update` opens an existing registry read-only. It reports pending schema
migrations, new reports, unchanged reports, and conflicts without creating a
lock, backup, journal, or output artifact:

```bash
python -m climate_registry plan-update \
  --source-dir /path/to/climate_monitor_wiki/sources \
  --database /external/path/article-registry.sqlite3
```

`update` is a separate, explicit mutation. It takes an exclusive sidecar lock,
recomputes the plan, fails closed on conflicts, creates a recoverable backup via
SQLite's backup API, updates a candidate database, validates it, and atomically
installs the candidate:

```bash
python -m climate_registry update \
  --source-dir /path/to/climate_monitor_wiki/sources \
  --database /external/path/article-registry.sqlite3 \
  --backup-dir /external/path/backups
```

The update contract is append-only:

- an already imported report with the same filename and SHA-256 is a no-op;
- the same report date with a different filename or hash is a conflict;
- a report present in the registry but missing from `sources/` is a conflict;
- a newly discovered report older than the registry's latest report is a
  conflict rather than a silent historical rewrite;
- new persistent imports must use the current weekly format and a Monday report
  date; legacy backfill remains an explicit fresh audit/rebuild operation;
- an update with no new report and no pending migration creates no backup;
- a failed candidate build leaves the live database byte-for-byte unchanged;
- active `-wal`, `-shm`, or rollback-journal sidecars cause the update to stop;
  replacing a main database while stale sidecars exist is unsafe.
- the live database fingerprint is checked again immediately before atomic
  replacement; a non-cooperating writer therefore aborts the install.

## Schema and identity

Migration 1 creates:

- `sources`: publisher hostnames;
- `reports`: immutable report identity and file SHA-256;
- `articles`: stable identity from the normalized external URL;
- `url_aliases`: every exact URL spelling observed in reports;
- `article_versions`: each distinct title/summary representation;
- `discoveries`: every parsed occurrence, including duplicates in one report;
- `report_appearances`: one selected appearance per article per report;
- `schema_migrations`: applied migration versions.

Migration 2 adds deterministic publication policy and unambiguous observation
semantics:

- `document_kind`: `article`, `report`, `topic_index`, or `landing_page`;
- `publication_eligible` and `exclusion_reason`;
- `observation_status`: `new_article`, `new_report_representation`, or
  `previously_seen`;
- `external_content_change`, which is deliberately `unknown` because the
  registry has not fetched and versioned the external page body.

Classification uses conservative URL-only rules. Root URLs are landing pages
and are not publication-eligible. PDF URLs are reports. URLs under explicit
`topic`, `topics`, or `activities-topics` paths are topic indexes and are not
publication-eligible. Other URLs remain articles. This policy does not delete
or hide audit history; excluded appearances remain queryable in SQLite and in
the audit manifest's `excluded_articles` list.

URL identity reuses the monitor's established canonicalization (case-normalized
scheme/host, removed fragments/trailing slash, and removed common tracking
parameters). Stable IDs are truncated SHA-256 values over normalized identity
inputs. They do not expose subscriber or SMTP information.

`article_versions.content_fingerprint` is explicitly based on the normalized
**title and summary stored in the canonical report**. It is not a hash of the
external webpage or PDF. External page-body versioning requires a later,
separately reviewed capture pipeline with copyright, retention, and fetch-failure
rules.

Migration 3 defines that later pipeline's storage contract without running it:

- `article_fetches` is the immutable attempt log. It records the requested and
  final URLs, fetch time and status, optional HTTP metadata, and sanitized error
  details. `success` requires a 2xx response and a body version. `not_modified`
  requires HTTP 304 and identifies the previously known body version. Both also
  require the final URL and prohibit error fields. `failed` attempts never point
  at or create a body version and require an error code. Their HTTP status may
  be absent or any valid response status: a 2xx transfer can still fail content
  validation because of a bot challenge, unsupported type, empty or oversized
  body, or extraction failure.
- `article_content_versions` stores immutable extracted Markdown plus hashes of
  the fetched content and Markdown, the response content type and optional byte
  count, and the extractor name/version. A content hash is unique within an
  article. These rows describe the external document body, unlike the existing
  report-derived `article_versions` rows.
- `article_enrichments` stores a versioned summary, category list, keyword list,
  language, and generator provenance for one body version. A failed enrichment
  stores error metadata instead of partial display content. `categories_json`
  and `keywords_json` are UTF-8 JSON encoded as `TEXT`; the schema deliberately
  does not require SQLite's optional JSON1 extension.
- `articles.current_content_version_id` selects the current external body while
  preserving every prior version. `display_policy` is one of `metadata_only`,
  `summary_excerpt`, or `full_markdown`, and defaults conservatively to
  `summary_excerpt`.

The standalone server-side capture/enrichment CLI is responsible for populating
these fields when explicitly invoked. Source response headers provide fetch
metadata; the extractor provides Markdown and extraction provenance;
deterministic rules provide summary, categories, keywords, language, and
generator provenance. This command never invokes a model.
The website will be a read-only consumer and must apply `display_policy`. In
particular, retained Markdown is not automatically licensed for public display:
copyright, publisher terms, and document type may require metadata or a summary
and short excerpt even when the full extraction is retained internally.

Fetch attempts, body versions, and enrichment results are append-only audit
records: UPDATE and DELETE are rejected by schema triggers. Corrections and
retries create new stable IDs rather than rewriting history. Persistent database
validation checks those triggers, critical foreign-key ownership, and the table
and column order of the required history-query indexes. It also rejects an
article whose current body pointer belongs to a different article, even if a
writer bypassed the normal update trigger.

Migration 3 is populated by the separately invoked capture/enrichment command:

```bash
python -m climate_registry capture-enrich \
  --database /external/path/article-registry.sqlite3 \
  --backup-dir /external/path/backups \
  --limit 25
```

Both paths are required and runtime databases, backups, and extracted content
remain outside the repository. By default the command selects, by database
`article_id`, publication-eligible articles without a current captured body.
Repeated `--article-id` options select an exact eligible set; an unknown or
excluded ID fails closed. `--refresh` includes already captured eligible
articles so conditional HTTP validation can check for changes. `--limit` is
restricted to 1–100. Omitting it still applies a default limit of 100, and more
than 100 explicit IDs fail rather than being silently truncated.
Automatic selection orders never-attempted articles first, then articles by
oldest most-recent fetch attempt and stable article ID. Repeated bounded refresh
runs therefore advance through the eligible registry instead of repeatedly
selecting the same first 100 records.

The command accepts no arbitrary URL input. It reads canonical URLs from the
registry and permits only HTTP/HTTPS on ports 80/443 without user information.
Every initial and redirect hostname is resolved and rejected if any answer is
loopback, private, link-local, multicast, reserved, or unspecified. The
registry also rejects IPv4-mapped IPv6, 6to4, and Teredo transition addresses
instead of attempting to infer whether their embedded endpoints are safe. A
mixed DNS answer is rejected when any returned address is prohibited. The
approved IP addresses are passed directly to the connection layer to prevent a
second DNS lookup; HTTPS still verifies the original hostname with normal SNI
and certificate checks. Redirects, time, and response bytes are bounded. TLS is
never downgraded: after any HTTPS hop, a redirect to HTTP is rejected, including
an HTTP-to-HTTPS-to-HTTP chain. Certificate verification cannot be disabled by
the CLI. The monotonic total deadline strictly covers the redirect loop and body
processing. Each outer body-read iteration reapplies the remaining socket
timeout. This also applies to HTTP/1.0 or `Connection: close` bodies where the
active socket is owned by the response object rather than the connection.
After connection establishment, a per-hop absolute-deadline watchdog shuts down
the current active socket at expiry. This interrupts response-header reads,
ordinary bodies, and `http.client` internal multi-receive operations such as
chunk framing when they cross the deadline—even if a peer drips partial
chunk-size lines often enough to avoid a per-operation timeout. Every path
cancels and joins the watchdog, closes the response before the connection, and
never reuses the socket. Connect and TLS operations receive the remaining
socket timeout.
Standard-library DNS resolution cannot be interrupted portably;
its elapsed time is checked against the same deadline immediately after it
returns. Tests inject deterministic resolvers and clocks without network access.

HTML is converted to structural Markdown using the standard-library parser;
scripts and styles are discarded, relative links are resolved against the final
response URL, and a declared valid HTTP charset is honored. Unknown or invalid
declared charsets fail with a stable error code. PDFs are text-extracted with
`pypdf` and have fixed page-count and extracted-character limits. PDF parsing is
in-process and does not claim a separately enforceable wall-clock timeout; the
page, text, response-byte, and overall processing limits are the current
fail-closed controls. HTML parsing and every final extracted Markdown value use
the same character ceiling, including text added by resolving relative links.
Raw response bodies are not retained. Unsupported,
invalid, empty, or oversized
documents create a failed fetch audit row with a stable error code and a short
sanitized message. Successful bodies store raw-byte and Markdown SHA-256 values.
The most recent non-empty ETag and Last-Modified validators associated with the
current body version are sent on later attempts. HTTP 304 is accepted only when
the actual request hop carried at least one validator; validators are removed on
cross-origin redirects, preventing an unrelated target from claiming a cached
body. A valid 304 reuses the current body version, and a 200 response with an
existing raw hash also reuses it.

Deterministic enrichment version 1 copies a short factual summary from the
extracted text, assigns only categories from a fixed climate/actuarial taxonomy,
extracts 8–12 source-present keywords, and records `en`, `zh`, `mixed`, or
`unknown`. Content too sparse for those conservative rules receives an
append-only failed enrichment rather than invented text. A content version and
generator name/version pair is idempotent; changing the rules requires a new
generator version.

Exit codes are:

- `0`: all selected articles captured/not-modified, or no eligible work;
- `2`: unsafe/invalid input or registry contract;
- `3`: candidate build or validation failure;
- `4`: lock/fingerprint concurrency failure;
- `5`: the atomic batch was installed but one or more articles recorded a
  failed fetch or enrichment, including an HTTP 304 whose existing enrichment
  is failed.

Output is one compact JSON line containing article IDs, per-article statuses,
stable error codes, counts, and backup path. It never includes response bodies,
Markdown, full URLs, or detailed server errors.
Argument errors use the same one-line JSON contract and do not emit argparse
usage text to stderr.

Capture uses the same fail-closed lock, SQLite backup API, candidate validation,
live fingerprint/sidecar check, atomic replacement, and parent-directory fsync
as persistent report updates. The backup is the rollback source. Retained locks
or SQLite sidecars require manual reconciliation and are never auto-removed.
Failed candidate construction leaves the live database unchanged; ordinary
per-article failures are intentionally saved as audit records and reported with
exit code 5.

Operational adoption remains separate: this command does not create or change
a Hermes job, call a model, expose a write API, modify canonical Markdown, or
restart a service. Operators must review publisher terms, robots/access policy,
copyright display policy, and internal retention needs before first production
capture. Retaining Markdown for audit does not authorize public full-text
display; the future read-only website must enforce `display_policy`.

## Migration and backup policy

`apply_migrations()` is transactional and idempotent. It refuses to run inside a
caller's active transaction. Persistent updates use one writer, keep the
candidate database on the live database filesystem for atomic replacement, and
use SQLite's backup API rather than copying a live database file. A retained
lock file means the previous process did not complete cleanly and requires
manual reconciliation; it must not be deleted automatically.

## Read-only website access

The website includes a read-only Archive workspace with Historical Reports,
Article Archive, and Article Detail views. It is deliberately independent from
the update and capture commands. Configure one absolute, external database path:

```bash
CLIMATE_REGISTRY_DB=/external/path/article-registry.sqlite3
```

The path must resolve outside the repository. If it is absent, unavailable, or
not at schema version 3, the Registry endpoints fail closed while Chat, the
Wiki, and `/api/health` remain available. The application never creates,
migrates, replaces, or repairs this database. Every request opens a fresh
SQLite URI connection using `mode=ro&immutable=1` and `query_only`, so an atomic
replacement made by the standalone capture/update workflow is observed by the
next request without sharing a long-lived connection.

The public contract is GET-only:

- `/api/registry/status` reports availability and non-sensitive counts;
- `/api/registry/reports` and `/api/registry/reports/{report_date}` provide
  newest-first weekly history and ordered source appearances;
- `/api/registry/articles` provides bounded pagination, literal title/summary
  search, and source/pillar/report-date filters;
- `/api/registry/articles/{article_id}` provides metadata, source links,
  appearances, fetch status, and permitted enrichment/content.

Article Detail enforces `display_policy`. Enrichment summary, categories,
keywords, language, and generator provenance remain display metadata for all
policies. `metadata_only` exposes no stored body or excerpt.
`summary_excerpt` adds a bounded supporting excerpt, never full Markdown.
`full_markdown` may expose the retained Markdown. Internal paths, content
hashes, detailed capture errors, and SQLite exceptions are not returned.
There are no Registry write endpoints or website controls for capture,
classification, editing, deletion, model use, or scheduling.

The first operational adoption still requires a separate owner-approved server
procedure to install a production database and make it readable at the
configured external path. This module is not wired into Hermes, the publisher,
containers, or any scheduled job.
