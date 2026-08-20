# Climate delivery module

`climate_delivery` is a standalone, composable delivery module for an already
generated weekly Climate Monitor report. It does not collect information, call
a model, search the internet, update the Wiki, modify Git, or deploy anything.

The intended separation is:

1. The Monday 08:00 monitor generates the canonical Markdown report.
2. The retained Monday 09:00 Weekly Climate Email (PDF highlights) job invokes
   this module to create a deterministic summary, render a PDF, and send one
   message to each of the existing four recipients.
3. The Monday 10:00 publisher independently turns the canonical Markdown into
   a rolling Wiki pull request.
4. The website can later consume `summary.json` and `manifest.json`; it does not
   own SMTP delivery.

The existing 09:00 Hermes job is **enabled and intentionally retained**. It is
the only production automation authorized to create delivery artifacts, and it
also owns delivery of the PDF highlights email to the existing four recipients.
The deployed Registry sync implementation is a separate 10:30 concern: it
consumes the already validated artifact read-only and does not create or send
mail, change recipients, or mutate delivery state. Its Hermes job is not yet
configured or verified, so the project must not be described as
`PRODUCTION COMPLETE` until that task completes a normal weekly run. The
application code does not modify Hermes, the running website, containers, cron
jobs, or host configuration.

## Input contract

Every command requires explicit **absolute** paths. There is no
current-directory or repo default. All operational inputs, outputs, config,
state, summaries, and PDFs MUST be outside the application repository. For
`run`, `--output-dir` and `--state-dir` MUST be different, non-nested trees.
The CLI rejects relative paths, repo-internal paths, and unsafe output/state
layouts with exit code 2. The report must:

- be named `climate-monitor-YYYY-MM-DD.md`;
- declare the same `Report Date`, and that date must be a Monday;
- contain exactly one H1;
- contain `Executive Summary`, `Pillar A`, `Pillar B`, and `Original Links`
  level-two sections;
- contain `Sites checked`, `succeeded`, and `failed` counts whose arithmetic is
  consistent; and
- contain at least one linked highlight and one original HTTP(S) link.
- be accompanied by the sibling `<report>.semantics.json` semantic sidecar
  that the 08:00 producer committed next to it, bound to the report's exact
  raw-byte SHA-256 and filename. The `run` command verifies this sidecar
  fail-closed: a missing, stale, SHA-mismatched, filename-mismatched, or
  otherwise contract-invalid sidecar aborts delivery outright. There is no
  Markdown-scrape fallback and no model/LLM call on the delivery path.

Extraction is deterministic. The content executive summary contains three or
four sentences derived from report counts, representative report titles, and a
fixed set of climate/actuarial theme-keyword categories. Process bullets from
the source Executive Summary are kept separately as monitoring notes. The
module does not enrich the report with network or model calls.

## Server-only configuration

Copy `config/climate-delivery.example.yaml` to a location outside the checkout.
The checked-in example contains only `example.invalid` placeholders. The real
server-only YAML stores stable recipient IDs and addresses; SMTP settings name
environment variables whose values come from server-only secret management.
Protect the deployed config and environment file with mode `600`. Do not commit
either file, the real recipient list, SMTP credentials, generated mail, or
delivery state.

`smtp.security` must be either `starttls` or `ssl`. Each recipient ID is the
non-secret idempotency key. Changing recipient IDs after a run requires manual
state reconciliation; the module deliberately fails closed.

`smtp.from_name` controls the human-readable sender name while
`smtp.from_address_env` still resolves the authenticated email address. The
recommended display name is `IAA Weekly Climate Newsletter`.

The generated email has a plain-text body, an escaped, table-based HTML
alternative, and a PDF attachment. The email features up to the first three
report-ordered, per-pillar numbered highlights from each pillar. Each featured
title is the source hyperlink and is followed by the report's supporting
summary. The attachment contains every highlight, numbered independently in
Pillar A and Pillar B, plus a content executive summary and a separate
monitoring snapshot. One independent MIME message is built and sent per
recipient.
No `List-Unsubscribe` header is emitted because v1 has no real unsubscribe
endpoint.

## Commands

```bash
python -m climate_delivery summarize \
  --report /srv/climate-input/climate-monitor-2026-08-10.md \
  --output /srv/climate-output/summary.json

python -m climate_delivery render-pdf \
  --summary /srv/climate-output/summary.json \
  --output /srv/climate-output/report.pdf

python -m climate_delivery send-email \
  --summary /srv/climate-output/summary.json \
  --pdf /srv/climate-output/report.pdf \
  --config /etc/climate-delivery/config.yaml \
  --state-dir /var/lib/climate-delivery/state \
  --dry-run

python -m climate_delivery run \
  --report /srv/climate-input/climate-monitor-2026-08-10.md \
  --output-dir /var/lib/climate-delivery/output \
  --state-dir /var/lib/climate-delivery/state \
  --config /etc/climate-delivery/config.yaml \
  --dry-run
```

### Historical artifact-only backfill

The independent `backfill` subcommand can reconstruct complete Historical
Reports artifacts from existing, immutable inputs. It does not collect data,
call a model, load delivery configuration or recipient state, or send email.
Run a read-only audit first:

```bash
python -m climate_delivery backfill \
  --all-missing \
  --sources-dir /srv/climate-sources \
  --registry-db /var/lib/climate-registry/registry.sqlite3 \
  --article-artifacts-dir /srv/climate-article-metadata \
  --output-dir /var/lib/climate-delivery/output \
  --dry-run
```

Replace `--all-missing` with `--date 2026-07-20` to audit or generate one
date. Exactly one selector is required. Every path is explicit and absolute.
The source and article-artifact inputs may be the application's existing
read-only repository mounts. The Registry database and output root remain
external to the application checkout; the output tree must be separate from
both read-only directory inputs and must not contain the Registry database.

Eligibility is fail-closed. A report is generated only when all of these bind
exactly:

- the canonical weekly Markdown filename, date, title, raw-byte SHA-256, site
  statistics, article order, and Pillar A/B sections;
- the supported Registry report identity and every historical `report_appearances`
  row, including canonical URL, ordinal, pillar, and the report-time article
  title and summary; and
- one unambiguous, schema-valid `article_metadata/articles-*.json` annotation
  for every Registry canonical URL.

The annotation supplies the canonical article title and summary. It never
changes historical report membership, ordering, pillar assignment, monitoring
facts, or source SHA. Legacy reports without complete monitoring statistics or
Pillar A/B membership, duplicate mappings, missing inputs, and conflicting
inputs are reported as `skipped`; no partial artifact is created.

For an eligible report, the command reuses the delivery summary builder, PDF
renderer, manifest writer, and web artifact validator. It stages all three
files on the output filesystem, validates the complete staged artifact, and
then atomically publishes `<date>/<source-sha>/`. Its manifest uses delivery
status `artifact-only` with an empty recipient list. A valid existing artifact
is `already_valid` and is never rewritten. An invalid existing destination is
`failed` and is also left untouched. Thus the validated 2026-08-17 delivery
artifact remains byte-for-byte unchanged. That date is explicitly protected:
if its valid delivery artifact is unavailable, backfill reports a skip and
does not create a replacement from historical annotations.

One deterministic JSON audit object is printed with `generated`, `skipped`,
`already_valid`, and `failed` lists plus their counts. In `--dry-run`, eligible
entries have action `would_generate`; validation and deterministic rendering
still run in temporary storage, but the configured output directory is not
created or modified. A non-dry run performs only artifact generation. Running
the same inputs again reports `already_valid` and preserves every artifact.

This repository task does not authorize running the production backfill. A
server operator must separately mount the reviewed inputs read-only, run the
dry-run, inspect representative summaries/PDFs and the audit reasons, and only
then obtain authorization for the final artifact-only run.

`run` writes the content-addressed directory
`<output-dir>/<date>/<report-sha256>/` containing `summary.json`,
`climate-monitor-<date>.pdf`, and `manifest.json`. Files are atomically
replaced. The manifest records relative artifact paths and SHA-256 hashes for
the summary and PDF. It contains recipient IDs and states but no address,
credential, or absolute repository path.

The `summary.json` preserves the v1 highlight shape (`pillar`, `title`,
`summary`, `url`) exactly and additionally carries a top-level
`article_semantics` map keyed by highlight URL. Each entry holds the verified
`summary`, `categories`, and `keywords` taken verbatim from the sidecar's
article bundle. The PDF renders those verified categories and keywords beneath
each highlight. The delivery module never derives, scrapes, or infers semantics
itself; whatever the sidecar does not verifiably provide is simply absent.

## Optional Historical Reports integration

The web app can read complete delivery artifacts without using delivery
configuration, recipient data, SMTP, or delivery state. For a directly launched
app, set `CLIMATE_DELIVERY_OUTPUT_DIR` to an absolute, readable artifact root.
For Compose, use the independent read-only override:

```bash
export CLIMATE_DELIVERY_ARTIFACTS_HOST_DIR=/external/climate-delivery-output
.venv/bin/python -m scripts.safe_compose \
  -f docker-compose.yml \
  -f docker-compose.delivery.yml \
  config --quiet
```

This read-only `config` command is passed through with Compose's normal
semantics. Use the same wrapper and override set for any later `up` or `create`;
that all-profile creation preflight validates the final resolved mount
and rejects linked, reparse-point, missing, non-directory, or writable sources.
See [`deployment.md`](deployment.md#optional-read-only-article-registry) for
the remaining TOCTOU boundary and recovery-command behavior.

The override binds that directory read-only at `/delivery-output`, disables
implicit host-directory creation, and sets the fixed container path. The base
Compose stack has no delivery-artifact dependency. Do not mount delivery config,
recipient, or state directories into the web container.

For each Registry report, the app uses only the canonical Registry date,
filename, title, and report SHA to open the exact
`<root>/<date>/<report-sha>/manifest.json`; it never scans for a latest
directory. The reader validates containment, symlink resolution, the v1
manifest and summary contracts, report identity, monitoring arithmetic, and
summary/PDF hashes. A missing, unreadable, oversized, mismatched, or incomplete
artifact is ignored as a whole, leaving the existing Markdown-based report UI
available.

When the artifact is valid, `GET /api/registry/reports/{date}` adds
`report_briefing` and `report_pdf`. Otherwise both fields are `null`. The
controlled `GET /api/registry/reports/{date}/pdf` endpoint revalidates Registry
identity and every artifact before returning validated bytes as an attachment;
the artifact root is never exposed as a static directory.

An existing content-addressed summary or PDF is immutable. A run first renders
both candidates in a temporary directory, hashes them, then takes the report
lock. Existing artifacts must have the exact candidate hashes; a renderer or
extractor change under the same report SHA fails closed with exit code 5 and
does not overwrite either artifact. Missing artifacts are atomically installed.
On POSIX, every atomic replacement also fsyncs its parent directory; Windows
skips directory fsync safely.

Every CLI invocation writes exactly one redacted JSON object to stdout. Exit
codes are:

| Code | Meaning |
|---:|---|
| 0 | Success, dry-run, or all recipients already sent |
| 2 | Input or server configuration error |
| 3 | Summary/PDF/artifact generation error |
| 4 | Explicit SMTP rejection persisted as `failed`; safe to retry only unsent recipients |
| 5 | Fail-closed manual reconciliation: concurrent lock, unknown/ambiguous delivery, state persistence failure, or artifact/state/config binding mismatch |

## Delivery safety and recovery

Recipient states are `pending`, `sending`, `sent`, `failed`, or `unknown`.
State changes to `sending` are atomically persisted before any SMTP object is
created. A confirmed return from SMTP changes it to `sent`. A known exception
changes it to `failed`; a retry skips every `sent` recipient and resumes at the
failure. A process interruption can leave `sending`, whose delivery outcome is
unknowable; both `sending` and `unknown` fail closed for manual reconciliation.
An unknown SMTP outcome returns exit 5 after persisting `unknown`; only an
explicit, durably persisted rejection returns exit 4. Any failure to persist
`sending`, `failed`, `unknown`, or `sent` also returns exit 5 with the original
storage error chained, because the authoritative delivery state cannot be
trusted. The manifest reports such outcomes as `ambiguous`, while known
rejections are `failed`.

Each MIME message includes a UTC `Date` and a deterministic Message-ID based on
the report SHA, recipient ID, and an opaque fingerprint of the actual delivery
payload. The fingerprint binds the subject, sender identity, recipient,
plain-text and HTML bodies, attachment filename, and PDF hash. The ID is stable
across retries of identical content, changes when the rendered message changes,
and contains no recipient address.

The state directory also holds an exclusive per-report lock. V1 intentionally
has no `--force` option. Never delete a lock or rewrite state merely to make a
job run; first establish whether mail was accepted by the SMTP provider.

State schema v2 is bound to the exact summary JSON hash, PDF hash, recipient
IDs, a SHA-256 fingerprint of each normalized recipient address, and the full
message-payload fingerprint used for Message-ID identity. Those fingerprints
are server-only delivery state: neither they nor an address may appear in the
manifest, CLI stdout, or error JSON. Changing any bound artifact, address,
sender, body template, or attachment after a partial delivery fails closed
before SMTP is instantiated; already-sent recipients remain protected from
duplicate delivery. Legacy schema-v1 state lacks the payload binding and
therefore requires manual reconciliation rather than automatic migration.

`--dry-run` still validates configuration, generates summary/PDF artifacts,
and constructs every MIME message. It never instantiates or connects to SMTP
and never marks a recipient as sent. If delivery state already exists, dry-run
still validates all artifact/address bindings and ambiguous states without
changing them.

PDF display strings use a deterministic ASCII-safe conversion: unsupported
emoji are removed and common dashes, arrows, bullets, quotes, and ellipses are
mapped to ASCII. This prevents the standard ReportLab font from rendering
unsupported glyphs as black squares. Source URLs are attached to the numbered
highlight titles instead of being printed as long raw strings.
