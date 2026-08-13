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

The future server-side capture/enrichment process is responsible for populating
these fields. Source response headers provide fetch metadata; the extractor
provides Markdown and extraction provenance; deterministic rules or an approved
model provide summary, categories, keywords, language, and generator provenance.
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

Migration 3 is schema and contract only. It performs no network request, invokes
no model, creates no capture CLI or public API, and changes no Hermes job. Runtime
databases and generated content remain outside the repository. A later reviewed
change must implement capture/enrichment and its operational adoption.

## Migration and backup policy

`apply_migrations()` is transactional and idempotent. It refuses to run inside a
caller's active transaction. Persistent updates use one writer, keep the
candidate database on the live database filesystem for atomic replacement, and
use SQLite's backup API rather than copying a live database file. A retained
lock file means the previous process did not complete cleanly and requires
manual reconciliation; it must not be deleted automatically.

The first operational adoption still requires a separate owner-approved server
procedure. This module is not wired into Hermes, the publisher, containers, or
the website.
