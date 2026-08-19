# Weekly Registry and Article Detail automation

This document defines the supported application-side contract for the proposed
Monday 10:30 Registry update. The base runner is deployed; this validated-
fallback change still requires merge and deployment. The Hermes job is
deliberately **not installed or enabled**. The legacy Publisher identity
repair is complete and valid. Two controlled capture candidates observed 21
successes and four deterministic publisher-wall 403 failures; neither promoted,
and the live DB remained byte-for-byte unchanged. No
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

The chosen design is **Registry DB first, with the existing per-article JSON and
SHA-matched report metadata as whole-bundle compatibility fallbacks**. Registry
schema v4 adds append-only `article_capture_resolutions` audit rows; it does not
turn fallback metadata into fetched content or enrichment. During rollout the
reader accepts exact v3 and exact v4 databases and reports the actual version.
Only the outer weekly candidate is migrated from v3 to v4 before atomic
promotion, so the live v3 database and a v3 backup remain readable.

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

A validated resolution is deliberately narrow. It is allowed only when the
candidate DB proves that the exact latest attempt for that eligible report
article is `failed` with `error_code=http_error`, HTTP 403, and no content
version. The validator then accepts one complete JSON annotation bundle, or—only
when JSON is absent or has no entry for that URL—one complete SHA-matched report
bundle. Missing fields, malformed JSON, canonical identity mismatch, timeout,
DNS failure, 5xx, and `blocked_response` remain unresolved. JSON and report
fields are never spliced. Browserbase, residential proxies, CAPTCHA bypasses,
and similar acquisition workarounds are intentionally excluded: they weaken
provenance and do not turn a bot wall into fetched source evidence.

## Supported CLI

All paths are explicit. Runtime data must remain outside the checkout, and the
lock path must be exactly `<database>.lock` so existing `update` and
`capture-enrich` commands coordinate with the weekly transaction. The lockfile
is a persistent safe inode; one nonblocking OS lock is held on one descriptor
for the entire critical section, interoperates with shell `flock` on POSIX, and
is released by process exit without unlink races.

```bash
python -m climate_registry weekly-sync \
  --date "$REPORT_DATE" \
  --source-dir /srv/climate_monitor_wiki/sources \
  --database /var/lib/climate-registry/article-registry.sqlite3 \
  --artifact-root /var/lib/climate-delivery/output \
  --backup-dir /var/lib/climate-registry/backups \
  --lock-file /var/lib/climate-registry/article-registry.sqlite3.lock \
  --publisher-ledger-dir /var/lib/climate-monitor/weekly-run-ledger \
  --metadata-dir /srv/climate_monitor_wiki/article_metadata \
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
- the annotation catalog stays within 64 files, 2 MiB per file, 16 MiB total,
  and 10,000 articles; each regular non-link file is read through a bounded
  no-follow descriptor and the sorted filename/size/raw-SHA catalog fingerprint
  remains exact through both promotion revalidations;
- the update plan contains no report date other than the explicit target; and
- the backup destination and all configured path components are safe.

`--dry-run` performs those read-only checks and returns exact would-add and
would-capture counts/IDs. It may probe an existing persistent lock inode
nonblockingly, but does not retain the lock, create a candidate or
backup, fetch content, reload, change state, or send mail.

Exit codes for `weekly-sync` are intentionally distinct:

| Code | Meaning |
|---:|---|
| `0` | Successful candidate promotion, including validated fallback coverage |
| `6` | Safe no-op; the exact report and all eligible articles are already current |
| `7` | Upstream or input preflight blocked |
| `5` | Coverage remains unresolved; candidate discarded |
| `8` | Candidate, backup, promotion, or post-promotion validation failed |
| `4` | Lock or live-fingerprint conflict |

Every outcome is one compact JSON line. Top-level `status` remains
`ok`/`partial`/`no-op` for compatibility; additive `coverage_status` is
`ok`, `partial_with_validated_fallback`, or `blocked_unresolved`. Counts keep
real capture failures separate from accepted fallbacks and unresolved articles:
`articles_failed`, `articles_fallback`, and `articles_unresolved`. A
successful/no-op result includes the
date, report SHA, actual and planned counts, target capture IDs, promotion,
reload requirement, before/after DB SHA, exact ordered target article IDs, and
the retained backup's basename. `fallback_article_ids` is sorted, is an exact
subset of the target membership, and has exactly `articles_fallback` entries.
A partial result lists candidate successes and
stable per-article failure codes but exposes no content, URLs, paths, SMTP
state, or recipients.

## Candidate transaction and rollback

Formal sync repeats all preflight checks while holding the standard exclusive
DB lock. It creates a mode-`700` same-filesystem private directory, copies the
live DB into its outer candidate, applies the
historical update there, then captures only target-report eligible articles
whose current enrichment or latest successful fetch is missing/stale. Existing
capture uses `PinnedTransport`, the explicit deadline, bounded redirects/body,
and SSRF/DNS/TLS controls. Refresh is enabled for an existing content version
whose enrichment or latest fetch is not complete.

After capture, exact eligible 403 failures may receive append-only validated
resolution rows in the outer candidate. The failed fetch's `requested_url`
must equal the article canonical URL; a redirected `final_url` is audit data,
not canonical identity. The candidate is discarded if any
article remains unresolved. Before
promotion it must pass the Registry schema, integrity and relationship checks;
the exact report SHA, filename and date; article membership/order/pillar; latest
fetch; current content ownership; structurally valid enrichment; and generator
provenance checks.

After full validation, the candidate's device/inode/size/raw SHA identity is
bound and checked again immediately before replacement. Only then is a
mode-`600`, hash-verified exact backup of the still-locked live
DB created. The code rechecks the live SHA and sidecars, uses same-filesystem
`os.replace`, fsyncs the parent directory, and runs the full report/membership/
coverage candidate validator against the installed live DB. If a
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
  --metadata-dir /srv/climate_monitor_wiki/article_metadata \
  --base-url "https://$SITE_HOST" \
  --expected-api-host "$SITE_HOST"
```

`RELOAD_TOKEN` is read from the server environment and is never placed on the
command line. The runner executes dry-run, then formal sync only after the
dry-run passes. It passes the dry-run report SHA into formal sync as a mandatory
pre-write expected identity. It validates both machine results, rechecks the
source, Registry identity/current DB SHA, artifact and exact article membership,
and calls `/api/reload` only after an actual promotion. A formal no-op completes
from those local bindings with `reload=not-needed` and makes no API or other
network request. For a promoted run it then
requires Registry latest date, report 200, the already proven
source/Registry/artifact SHA binding, non-null narrative briefing, monitoring
snapshot and PDF, article count, and every eligible Article Detail. DB-first
enrichment may have structurally valid empty category/keyword lists; a fallback
detail must be complete/nonempty, have one provenance, show the real latest
failed 403, and be named in the sync's bound `fallback_article_ids`. The API
still chooses content DB → JSON → report; resolution rows are coverage audit,
not a content source. Remote API URLs require HTTPS and an exact explicit
`--expected-api-host` (or `SITE_HOST`); literal loopback is the only HTTP
exception. Userinfo and redirects are rejected, so a reload token is never
forwarded. It exits nonzero with a stable sanitized reason
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

1. merge the validated-fallback change and deploy the resulting latest `main` from a
   clean checkout;
2. retain the existing 08:00 Monitor, enabled 09:00 Email/PDF job and four
   recipients, and the 10:00 Publisher schedule/rolling-PR behavior; deploy the
   repo-owned Publisher ledger recorder as the sole recorder and remove or
   disable any legacy external flat-record writer;
3. confirm the explicit external DB/artifact/ledger/backup paths and permissions;
4. retain the completed exact-date legacy-ledger repair evidence; do not reapply it;
5. pass exact-date weekly-sync dry-run and a controlled formal sync. Expect
   21 captured, four real failed 403s, four validated fallbacks and zero
   unresolved, or a no-op if those exact resolutions are already current;
6. create the 10:30 Hermes job disabled, validate it, and enable it only under a
   further separate authorization; and
7. observe at least one normal Monday before calling Registry/article metadata
   weekly automation production-complete.
