# Article Registry: audit-only foundation

This module builds a new SQLite registry from the Markdown reports already in
`sources/`. It is deliberately **audit-only**: it does not change the 08:00
Hermes monitor, canonical Markdown, the publisher, the website, or any existing
database.

## Boundaries

- Code, migrations, tests, and this contract live in Git.
- Runtime SQLite files, WAL/SHM companions, and generated audit output do not.
- The audit command refuses to open an existing database or overwrite an
  existing output directory. Every run therefore starts with new destinations.
- Source Markdown is read-only. The command never rewrites, renames, or deletes
  a report.
- A future server deployment should place the database outside the checkout,
  for example `/home/ubuntu/climate_monitor_data/registry/article_registry.sqlite3`.
  This PR does not create or migrate that production path.

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

URL identity reuses the monitor's established canonicalization (case-normalized
scheme/host, removed fragments/trailing slash, and removed common tracking
parameters). Stable IDs are truncated SHA-256 values over normalized identity
inputs. They do not expose subscriber or SMTP information.

`article_versions.content_fingerprint` is explicitly based on the normalized
**title and summary stored in the canonical report**. It is not a hash of the
external webpage or PDF. External page-body versioning requires a later,
separately reviewed capture pipeline with copyright, retention, and fetch-failure
rules.

## Migration and backup policy

`apply_migrations()` is transactional and idempotent. A later production
adoption should have one writer, enable foreign keys, keep the database and its
WAL/SHM files on the same host filesystem, and use SQLite's backup API rather
than copying a live database file. Those operational changes are outside this
audit-only PR.
