# Climate delivery module

`climate_delivery` is a standalone, composable delivery module for an already
generated weekly Climate Monitor report. It does not collect information, call
a model, search the internet, update the Wiki, modify Git, or deploy anything.

The intended separation is:

1. The Monday 08:00 monitor generates the canonical Markdown report.
2. A separate Monday 09:00 job may invoke this module to create a deterministic
   summary, render a PDF, and send one message per subscriber.
3. The Monday 10:00 publisher independently turns the canonical Markdown into
   a rolling Wiki pull request.
4. The website can later consume `summary.json` and `manifest.json`; it does not
   own SMTP delivery.

The existing 09:00 Hermes Email/PDF job must remain **paused** until this code
has been reviewed, merged, deployed, configured on the server, and successfully
validated with an owner-authorized test. This module does not modify Hermes,
the running website, containers, cron jobs, or host configuration.

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

Extraction is deterministic. The module does not invent prose or enrich the
report with network or model calls.

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

The generated email has a plain-text body, an escaped HTML alternative, and a
PDF attachment. One independent MIME message is built and sent per recipient.
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

`run` writes the content-addressed directory
`<output-dir>/<date>/<report-sha256>/` containing `summary.json`,
`climate-monitor-<date>.pdf`, and `manifest.json`. Files are atomically
replaced. The manifest records relative artifact paths and SHA-256 hashes for
the summary and PDF. It contains recipient IDs and states but no address,
credential, or absolute repository path.

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
the report SHA and recipient ID. The ID is stable across retries and contains
no recipient address.

The state directory also holds an exclusive per-report lock. V1 intentionally
has no `--force` option. Never delete a lock or rewrite state merely to make a
job run; first establish whether mail was accepted by the SMTP provider.

State is bound to the exact summary JSON hash, PDF hash, recipient IDs, and a
SHA-256 fingerprint of each normalized recipient address. That fingerprint is
server-only delivery state: neither it nor the address may appear in the
manifest, CLI stdout, or error JSON. Changing any bound artifact or address
after a partial delivery fails closed before SMTP is instantiated; already-sent
recipients remain protected from duplicate delivery.

`--dry-run` still validates configuration, generates summary/PDF artifacts,
and constructs every MIME message. It never instantiates or connects to SMTP
and never marks a recipient as sent. If delivery state already exists, dry-run
still validates all artifact/address bindings and ambiguous states without
changing them.

PDF display strings use a deterministic ASCII-safe conversion: unsupported
emoji are removed and common dashes, arrows, bullets, quotes, and ellipses are
mapped to ASCII. This prevents the standard ReportLab font from rendering
unsupported glyphs as black squares; long URLs use forced wrapping.
