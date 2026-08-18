# Weekly Registry and Article Detail automation

This document defines the supported application-side contract for the proposed
Monday 10:30 Registry update. The implementation is deployed, but the Hermes
job is deliberately **not installed or enabled**. Its exact-date production
dry-run is currently blocked by the identity-less legacy Publisher ledger
record; this is a writer/reader contract mismatch, not Registry data damage. No
production database, delivery state, recipient configuration, or email job is
modified.

## Production sequence and ownership

The intended sequence is:

```text
08:00 Monitor
  -> canonical Monday Markdown
09:00 Weekly Climate Email (PDF highlights)
  -> summary.json + PDF + manifest
  -> email to the existing four recipients
10:00 Publisher
  -> sources/ + wiki/ + successful publisher ledger
10:30 Weekly Registry Sync (intended; no job installed)
  -> candidate Registry update + target-only capture/enrichment
  -> atomic Registry promotion -> reload -> read-only API verification
```

The 09:00 job remains enabled and is the only delivery-artifact producer. Its
email delivery to the existing four recipients is intentional. The proposed
10:30 job consumes its content-addressed artifact read-only; it never invokes
delivery code, reads recipient configuration, sends mail, or changes delivery
state. Registry/article automation is not yet configured in Hermes and must not
be described as production-complete until that separately authorized step has
been installed and observed successfully.

## Data-equivalence decision

The chosen design is **Registry DB first, with the existing per-article JSON as
a compatibility fallback**. No external metadata export and no schema migration
are needed.

The two stores are not identical. Legacy annotation JSON records a reviewed
evidence choice; Registry enrichment records deterministic output for a fetched
content version. A DB-derived exporter could not honestly invent the missing
review semantics. New weekly Article Detail is nevertheless complete under the
existing nullable-compatible API contract: `source_annotation` may be `null`,
while identity, report history, title, original report link, pillar, ordering,
content summary/classification, and generator provenance all come from the DB.

| API/JSON field | Registry DB source | Exact parity | Transformation | Missing risk |
|---|---|---:|---|---|
| `canonical_url` / `article_id` | `articles` | Yes | Existing canonical URL and stable-ID rules | None after candidate validation |
| `original_url` | `discoveries.raw_url` for the latest appearance | Yes for report provenance | Keeps the URL spelling published in canonical Markdown | It is not an alternate reviewed evidence URL |
| `title` | current `article_versions.observed_title` | No | JSON reviewed title remains a compatibility overlay when present | New articles use the report title |
| `summary` | complete current `article_enrichments.summary` | No | Deterministic extractive summary; JSON/report fallback only when the DB bundle is absent | Must not be called a human-reviewed annotation |
| `categories` | `article_enrichments.categories_json` | No | Six lowercase deterministic classes, not the legacy eight Title-Case classes | A valid empty list remains valid |
| `keywords` | `article_enrichments.keywords_json` | No | Deterministic source-present keywords | Values/order do not equal legacy review output |
| `pillar`, `ordinal`, appearances | `report_appearances`, `reports` | Yes | Returned in report order | None after membership validation |
| fetch/content provenance | `article_fetches`, `article_content_versions` | Yes | Latest safe fetch plus current retained content under `display_policy` | A failed latest fetch makes the article stale for weekly sync |
| generator provenance | `article_enrichments.generator_*`, `generated_at` | Yes | Returned as the existing `enrichment.generator` object | Not the annotation review date |
| `source_basis`, reviewed `source_url`, review date | No DB equivalent | No | Existing JSON remains the only source for `source_annotation` | New articles legitimately return `source_annotation: null` |
| report briefing / PDF | delivery artifact tree | Not applicable | API validates the exact report date/title/SHA before exposing it | Never written by Registry sync |

The current 161 legacy annotations remain unchanged and continue to load. The
reader treats `{summary, categories, keywords}` as one source bundle: a complete
current-content enrichment supplies all three; otherwise that article falls
back to its JSON annotation, then to SHA-matched report metadata. Empty or
invalid DB list values never cause a silent DB/JSON field splice. JSON may still
supply compatibility-only title and `source_annotation` fields. DB/JSON overlap
is logged at DEBUG level using fixed field names only—never URL, content, path,
or recipient data. If the Registry itself is unavailable, Registry endpoints
continue to return their existing 503 response; annotation JSON cannot recreate
article identity and history.

## Supported CLI

All paths are explicit. Runtime data must remain outside the checkout, and the
lock path must be exactly `<database>.lock` so existing `update` and
`capture-enrich` commands coordinate with the weekly transaction.

```bash
python -m climate_registry weekly-sync \
  --date "$REPORT_DATE" \
  --source-dir /srv/climate_monitor_wiki/sources \
  --database /var/lib/climate-registry/article-registry.sqlite3 \
  --artifact-root /var/lib/climate-delivery/output \
  --backup-dir /var/lib/climate-registry/backups \
  --lock-file /var/lib/climate-registry/article-registry.sqlite3.lock \
  --publisher-ledger-dir /var/lib/climate-monitor/weekly-run-ledger \
  --dry-run
```

The date is mandatory and must be a Monday; the command never guesses `latest`.
Before any mutation it verifies:

- the source checkout is clean and the exact source is tracked at `HEAD`;
- the latest Publisher attempt for that date is `success` or `no_change` and
  carries the exact formal v1 report identity: `report_id` derives the canonical
  `climate-monitor-<date>.md` filename, `report_date` is the explicit Monday,
  and `sha256` is the canonical source file's raw-byte digest;
- the delivery manifest, narrative summary, monitoring snapshot, PDF, report
  date/title/filename, and SHA form one valid artifact;
- the live DB is a regular non-link file outside the checkout, has no SQLite
  sidecars, and uses the standard lock;
- the update plan contains no report date other than the explicit target; and
- the backup destination and all configured path components are safe.

`--dry-run` performs those read-only checks and returns exact would-add and
would-capture counts/IDs. It does not acquire the lock, create a candidate or
backup, fetch content, reload, change state, or send mail.

Exit codes for `weekly-sync` are intentionally distinct:

| Code | Meaning |
|---:|---|
| `0` | Successful candidate promotion |
| `6` | Safe no-op; the exact report and all eligible articles are already current |
| `7` | Upstream or input preflight blocked |
| `5` | Capture/enrichment was partial; candidate discarded |
| `8` | Candidate, backup, promotion, or post-promotion validation failed |
| `4` | Lock or live-fingerprint conflict |

Every outcome is one compact JSON line. A successful/no-op result includes the
date, report SHA, actual and planned counts, target capture IDs, promotion,
reload requirement, before/after DB SHA, exact ordered target article IDs, and
the retained backup's basename. A partial result lists candidate successes and
stable per-article failure codes but exposes no content, URLs, paths, SMTP
state, or recipients.

## Candidate transaction and rollback

Formal sync repeats all preflight checks while holding the standard exclusive
DB lock. It copies the live DB to a same-filesystem outer candidate, applies the
historical update there, then captures only target-report eligible articles
whose current enrichment or latest successful fetch is missing/stale. Existing
capture uses `PinnedTransport`, the explicit deadline, bounded redirects/body,
and SSRF/DNS/TLS controls. Refresh is enabled for an existing content version
whose enrichment or latest fetch is not complete.

The outer candidate is discarded if any fetch/enrichment is partial. Before
promotion it must pass the Registry schema, integrity and relationship checks;
the exact report SHA, filename and date; article membership/order/pillar; latest
fetch; current content ownership; structurally valid enrichment; and generator
provenance checks.

Only then is a mode-`600`, hash-verified exact backup of the still-locked live
DB created. The code rechecks the live SHA and sidecars, uses same-filesystem
`os.replace`, fsyncs the parent directory, and validates the installed DB. If a
failure occurs after replacement, it reconstructs and atomically installs the
exact verified backup with the original live DB mode and POSIX ownership before
reporting failure.
It also repeats the clean-HEAD, Publisher ledger, artifact and source-SHA checks
after network capture and immediately before promotion. The backup is retained
as the operator's audited rollback source. A later API reload failure does not
undo or damage the already validated DB; it is reported as an operational
verification failure and can be retried separately.

Successful promotion can be rolled back through the supported exact-backup
entrypoint. Use the `backup_name` and `database_sha256_before` returned by the
sync; all paths remain explicit:

```bash
python -m climate_registry restore-backup \
  --database /var/lib/climate-registry/article-registry.sqlite3 \
  --backup /var/lib/climate-registry/backups/EXACT_BACKUP_NAME \
  --expected-sha256 "$DATABASE_SHA256_BEFORE" \
  --backup-dir /var/lib/climate-registry/restore-backups \
  --lock-file /var/lib/climate-registry/article-registry.sqlite3.lock
```

Restore validates both databases, the exact backup hash, standard lock,
sidecars and path boundary; creates a second exact backup of the displaced live
DB; preserves the live DB's operational file mode and POSIX ownership; and
atomically replaces and revalidates the target. Its JSON result sets
`reload_required: true`. This PR
tests the operation but does not authorize running it against production.

## Proposed disabled 10:30 Hermes job (not installed)

After code deployment and separate owner authorization, the proposed Monday
10:30 UTC job command is:

```bash
cd /srv/climate_monitor_wiki
.venv/bin/python scripts/weekly_registry_refresh.py \
  --date "$REPORT_DATE" \
  --source-dir /srv/climate_monitor_wiki/sources \
  --database /var/lib/climate-registry/article-registry.sqlite3 \
  --artifact-root /var/lib/climate-delivery/output \
  --backup-dir /var/lib/climate-registry/backups \
  --lock-file /var/lib/climate-registry/article-registry.sqlite3.lock \
  --publisher-ledger-dir /var/lib/climate-monitor/weekly-run-ledger \
  --base-url "https://$SITE_HOST"
```

`RELOAD_TOKEN` is read from the server environment and is never placed on the
command line. The runner executes dry-run, then formal sync only after the
dry-run passes. It passes the dry-run report SHA into formal sync as a mandatory
pre-write expected identity. It validates both machine results, rechecks the
source, Registry identity/current DB SHA, artifact and exact article membership,
calls `/api/reload`, and then
requires Registry latest date, report 200, the already proven
source/Registry/artifact SHA binding, non-null narrative briefing, monitoring
snapshot and PDF, article count, and one complete eligible Article Detail with
content-enrichment provenance. It exits nonzero with a stable sanitized reason
so Hermes can alert and stop. It has no email or delivery-state code path.
The runner's final JSON safely retains `backup_name` and the before/after DB
hashes, so an audited `restore-backup` does not depend on directory scanning or
hash recomputation.

This is a tested draft, not a job creation request. Do not schedule it merely
because 10:30 has arrived: its Publisher ledger and artifact checks are the
authorization boundary for that week's data.

## Legacy Publisher ledger repair

The supported upgrade entrypoint is
`scripts/repair_publisher_ledger.py`. It requires one explicit Monday plus the
ledger, source, Registry database, artifact, and Publisher-lock paths. It has no
`--all`, latest selector, or caller-provided SHA. Default execution is a
zero-write dry-run; mutation requires `--apply` and separate authorization. The
lock path must equal `CLIMATE_PUBLISH_LOCK` (or its documented default) and the
lock file must already exist, so a typo cannot create an unrelated lock.

The command independently binds canonical source raw bytes, Registry identity
when the date exists, the content-addressed artifact directory, manifest report
identity, and summary/PDF hashes. Any mismatch, linked/reparse path, traversal,
lock conflict, malformed legacy record, or competing overlay fails closed.
Apply adds one atomic `.attempt-repairs` overlay bound to the untouched legacy
attempt's raw hash. The original attempt and `.attempt-identities` claim remain
the exact rollback source. Stable statuses are `would_repair`, `repaired`,
`already_valid`, `preflight_failed`, `validation_failed`, and `lock_conflict`;
preflight, validation, and lock failures use distinct nonzero exits.

For the staged deployment, exact dry-run/apply/weekly-sync/disabled-job sequence
and the required 2026-08-17 SHA, see
[`deployment.md`](deployment.md#ledger-contract-rollout-and-1030-gate).

## Future adoption checklist

This PR itself stops before deployment or scheduling. A separately authorized
production adoption should:

1. merge the ledger-contract PR and deploy the resulting latest `main` from a
   clean checkout;
2. retain the existing 08:00 Monitor, enabled 09:00 Email/PDF job and four
   recipients, and the 10:00 Publisher schedule/rolling-PR behavior; deploy the
   repo-owned Publisher ledger recorder as the sole recorder and remove or
   disable any legacy external flat-record writer;
3. confirm the explicit external DB/artifact/ledger/backup paths and permissions;
4. run and archive the exact-date legacy-ledger repair dry-run;
5. under separate authorization apply the repair, then pass exact-date
   weekly-sync dry-run and a safe formal no-op/controlled first sync;
6. create the 10:30 Hermes job disabled, validate it, and enable it only under a
   further separate authorization; and
7. observe at least one normal Monday before calling Registry/article metadata
   weekly automation production-complete.
