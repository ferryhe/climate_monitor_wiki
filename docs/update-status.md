# Weekly run ledger and update-status API

The weekly run ledger is a small public operational contract. It records that a
monitor or publisher attempt finished; it is not a scheduler, alerting system,
content database, or replacement for the Article Registry.

The repo-owned 10:00 Publisher wrapper records its own outcome after the rolling
PR transaction and production-checkout integrity check have completed. The
repository now contains a strict weekly monitor driver and prompt provenance
contract, but the live 08:00 producer still belongs to Hermes until a separately
authorized cutover is performed. This application cannot truthfully reconstruct
Hermes' 57-source results or registry revision from generated Markdown; Monitor
producer wiring and live scheduler changes remain separate concerns.

## Attempt contract

One attempt is one `weekly-run-attempt.v1` JSON object:

```json
{
  "schema_version": "weekly-run-attempt.v1",
  "attempt_id": "20260810t080000z-attempt-01",
  "stage": "monitor",
  "report_date": "2026-08-10",
  "scheduled_for": "2026-08-10T08:00:00Z",
  "finished_at": "2026-08-10T08:30:00Z",
  "status": "partial",
  "result_code": "report_written_with_failures",
  "report": {
    "report_id": "climate-monitor-2026-08-10",
    "report_date": "2026-08-10",
    "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "registry_revision": {
    "namespace": "web-listening:source-registry",
    "revision": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "sources": {
    "total": 2,
    "updated": 1,
    "unchanged": 0,
    "failed": 1,
    "blocked": 0,
    "failures": [
      {"source_id": "unep", "status": "failed", "error_code": "timeout"}
    ]
  }
}
```

Rules:

- `stage` is `monitor` or `publisher`.
- `status` is `success`, `partial`, `failed`, or `no_change`.
- timestamps are strict UTC `Z`; `finished_at` cannot precede `scheduled_for`.
  The UTC calendar date of `scheduled_for` must equal `report_date`, which is
  the canonical Monday for the monitoring week.
- IDs, result/error codes, and revisions are bounded public classification
  tokens. Token syntax validation cannot detect secrets: producers must use an
  approved public classification taxonomy and must never supply credentials,
  prompts, host paths, raw exceptions, or other sensitive values.
- `sources` is optional. If present it is complete: its four status counts sum
  exactly to `total`, and every failed/blocked source has one matching sanitized
  failure entry. If the producer lacks this evidence, it omits `sources`; the
  API does not turn unknown values into zeros.
- When `sources` is present, its evidence must agree with the attempt status:
  `success` has no failed/blocked sources; `no_change` has only unchanged
  sources; `partial` has both successful and failed/blocked outcomes; and
  `failed` has at least one source and no updated/unchanged outcomes. These
  constraints do not apply when source-level evidence is absent.
- `report` and `registry_revision` are optional evidence. A producer must not
  invent them. In particular, the app's 34-source YAML is not a substitute for
  the upstream 57-site registry revision.
- For a Publisher success, `report` is mandatory. `report_id` must equal
  `climate-monitor-<report_date>` and therefore derives the one canonical
  filename `climate-monitor-<report_date>.md`; `sha256` is computed over that
  final source file's raw bytes with no newline or Unicode normalization.

`no_change` is a successful operational completion, so it refreshes run
freshness. It does not by itself claim a new report revision.

## Append-only writer

Prepare the JSON outside the checkout, assign a stable attempt ID at job start,
then run:

```bash
.venv/bin/python -m scripts.record_weekly_run \
  --ledger-dir /external/weekly-run-ledger \
  --input /external/attempt.json
```

Records are stored under:

```text
attempts/<stage>/<report-date>/<attempt-id>.json
```

The writer creates canonical JSON through a same-directory temporary file and
an exclusive atomic link. It never overwrites an attempt. Repeating the same ID
and canonical bytes returns `already_exists`; the same ID with different
content fails as `attempt_conflict`. Retries use new IDs and preserve earlier
failures. Existing path components are checked for symlinks and reparse points,
and created directories are checked for resolved containment. This protects the
operator-owned directory boundary; it is not a claim of resistance to a
hostile same-user process racing filesystem operations.

The writer also keeps a private `.attempt-identities/` hard-link claim for each
ID so concurrent writers cannot reuse an ID under a different stage or week.
The API never scans or exposes those claims.

There is no tracked `latest.json`. The API derives latest state from immutable
attempts, so a publisher `no-op` cannot create Git or rolling-PR churn.

An authorized legacy repair also does not overwrite an attempt. It creates one
strict raw-hash-bound projection under
`.attempt-repairs/publisher/<date>/<attempt-id>.json`; the original attempt and
its `.attempt-identities` hard-link claim remain byte-for-byte unchanged. The
reader accepts the overlay only when it adds the exact canonical report identity
to an otherwise identical legacy Publisher success. Removing that exact overlay
is the audited rollback.

## Read-only API

Set `CLIMATE_UPDATE_STATUS_DIR` to an absolute directory outside the
application repository. `GET /api/update-status` opens and validates the files
again on every request; it caches no file descriptor, inode, or derived pointer
and never creates or modifies a file.

Stable unavailable responses are:

| HTTP | Reason | Meaning |
|---|---|---|
| 503 | `not_configured` | `CLIMATE_UPDATE_STATUS_DIR` is unset |
| 503 | `invalid_location` | path is relative, inside the repo, or a symlink |
| 503 | `ledger_unavailable` | configured directory is absent or unreadable |
| 503 | `invalid_ledger` | a record/layout/schema/resource bound is invalid |

A valid empty directory returns HTTP 200 with `state: "empty"`. A malformed
attempt fails the ledger closed; it is never presented as an empty success.
Homepage, health, Chat, static files, and Article Registry routes remain
independent.

The response separates `stages.monitor` and `stages.publisher`. Each has its
own `last_attempt`, `last_success`, `has_newer_unsuccessful_attempt` flag, and
stale result. The flag is true when the latest finished attempt is `failed` or
`partial`; the ledger does not claim visibility into attempts that never finish
or never invoke the writer.
The top-level `stale` is explicitly copied from the monitor stage and includes
`stale_source: "monitor"`; publisher freshness remains visible only under its
own stage. The deterministic stale threshold is 192 hours (one weekly cadence
plus 24 hours). Reasons are `no_successful_attempt`,
`latest_attempt_failed`, `latest_attempt_partial`, `last_success_expired`, or
`current`.

Attempts whose `finished_at` is later than the API's UTC clock fail the ledger
closed as `invalid_ledger`; there is deliberately no clock-skew allowance.
Equality with the request clock is accepted. Operators must keep producer and
application clocks synchronized. Top-level `latest_successful_report` and
`latest_successful_registry_revision` contain only evidence from `success` or
`no_change` attempts. Evidence attached to a `partial` attempt remains visible
under that stage's `last_attempt` but is not mislabeled as successful.

Resource limits are 128 KiB per attempt, 20,000 attempts, 32 MiB total attempt
bytes, 20,000 visited entries, and 6,000 visited directories. At two normal
weekly stages, the attempt-count ceiling covers more than 190 years; the lower
independent byte and traversal budgets normally bind first and prevent a
junk-filled directory or unusually large history from consuming unbounded
resources. Traversal uses iterative streaming directory scans and counts every
entry before inspecting it. Files are opened without following links and with
non-blocking mode on POSIX, must be regular files, and are rejected if their
identity or size changes while being read.

## Container configuration

The optional Compose override mounts the operator-owned parent directory, not
an individual file:

```bash
export CLIMATE_UPDATE_STATUS_HOST_DIR=/external/weekly-run-ledger
.venv/bin/python -m scripts.safe_compose \
  -f docker-compose.yml \
  -f docker-compose.update-status.yml \
  config --quiet
```

This read-only `config` command uses Compose's normal passthrough behavior. Use
the same wrapper and override set for a later container-creating command so its
final resolved mount and host path receive the documented preflight checks.

When the Article Registry is also enabled, include
`-f docker-compose.registry.yml` in the same invocation; the two overrides are
independent and are tested together.

It mounts the directory read-only at `/update-status`, sets
`CLIMATE_UPDATE_STATUS_DIR=/update-status`, and disables implicit host-path
creation. The host directory should be operator-owned mode `0750`; canonical
attempt files should be mode `0640`. The token schema is not a secret scanner:
producers must place only approved public classification codes in this public
projection, never credentials or raw logs.

Rollback is app-only: remove `docker-compose.update-status.yml` from the app
Compose invocation (and remove the environment setting if separately set),
then recreate only the Wiki app. Existing append-only attempts may be retained.
Caddy, the Article Registry, reports, Chat, and Hermes jobs do not need changes.

## Producer integration

The Publisher maps `published` to `success` and
`cleaned`/`unchanged`/`no-op` to `no_change`, and constructs the attempt identity
and outcome when appending. Ordinary failures use sanitized codes. Success is appended only
after `publish()` returns, so a failed remote/PR/checkout-integrity condition
cannot be mislabeled as successful. The upstream monitor must still supply its
own source evidence and revision; this repo does not infer them from report
prose.

No recorder can report a scheduler, interpreter, host, or process crash that
happens before the recorder is invoked. Real-time operations alerting remains a
separate requirement and must not be replaced by this historical API.
