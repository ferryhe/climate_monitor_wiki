# Sanitized scheduler status snapshot

`GET /api/job-status` reads one sanitized, atomically replaceable observer
snapshot. It is deliberately separate from the detailed producer-owned
`weekly-run-attempt.v1` ledger exposed by `/api/update-status`.

The API defines the application-side read contract. `scripts/export_scheduler_status.py`
projects actual Hermes execution rows; installing its observer is a separately
reviewed server operation and merely setting an API path creates no snapshot.

The 2026-09-08 SSH audit found 12 enabled legacy Step jobs on the real server;
the target four-slot wrapper schedule was not installed. The wrappers write
local-only evidence. Render has no shared source
for those files and remains HTTP 503 `not_configured`. Setting a path on Render
alone cannot transport the snapshot; do not request an unavailable mounted volume.
The existing deployed `/api/update-status` and Registry API expose their own
available evidence; they do not prove Hermes dispatch. No new transport is added.
See [current cutover status](../PIPELINE_REFERENCE.md#verification-and-cutover).

The current four slots use 08:00, 09:00, 10:00 and 10:30 ET every other Monday,
anchored to September 14, 2026. `biweekly-job-status.v1` stores UTC instants but
validates them against `America/New_York`, including DST. See
[the ET runbook](biweekly-et-deployment.md).
An explicit dry-run never marks a production slot completed. A current-week
Registry rehearsal after 10:30 ET records `registry_dry_run_exit_N` in its
isolated snapshot with `not_dispatched`; before-slot/historical rehearsals
report the exit code only in stdout. Production gate failures remain pending. A blocked Registry
is pending, never evidence of end-to-end completion.

The prepared 09:00 no-send mode is different from global dry-run. After exact
report, semantic-sidecar, PDF, and manifest verification, it records the email
slot as `not_dispatched` with `result_code=delivery_no_send`; it does not claim
an email was sent. The exporter preserves that same-occurrence application
disposition while Hermes catches up, but never carries it to a newer execution
or fortnight.

## Contract

The fixed filename is `scheduler-status.json`. The strict
exporter contract uses `biweekly-job-status.v1` and all four public aliases:

```json
{
  "schema_version": "biweekly-job-status.v1",
  "generated_at": "2026-09-14T12:05:00Z",
  "jobs": {
    "monitor": {
      "scheduled_for": "2026-09-14T12:00:00Z",
      "state": "completed",
      "claimed_at": "2026-09-14T12:00:01Z",
      "started_at": "2026-09-14T12:00:02Z",
      "finished_at": "2026-09-14T12:04:00Z"
    },
    "email": {
      "scheduled_for": "2026-09-14T13:00:00Z",
      "state": "scheduled"
    },
    "publisher": {
      "scheduled_for": "2026-09-14T14:00:00Z",
      "state": "scheduled"
    },
    "registry": {
      "scheduled_for": "2026-09-14T14:30:00Z",
      "state": "scheduled"
    }
  }
}
```

The four aliases are public names; Hermes job IDs are never accepted. Eligible
run dates are every 14 days from the Monday anchor `2026-09-14`. The slots stay
at 08:00, 09:00, 10:00, and 10:30 in `America/New_York`; only their UTC offset
changes with DST. On September 14, 2026 they are 12:00, 13:00, 14:00, and
14:30Z. On November 9, 2026 they are 13:00, 14:00, 15:00, and 15:30Z.
`generated_at` binds the snapshot to its containing anchored fortnight, so a
snapshot after Monitor but before Email may truthfully keep the later
same-occurrence slots in `scheduled`. A schedule change requires an application
contract update as well as the separately controlled scheduler change.

The reader also accepts historical `weekly-job-status.v1` snapshots as a
compatibility input. That legacy three-slot UTC format is not the current
exporter or rollout contract.

The states have these exact meanings and fields:

| State | Meaning | Required execution fields |
|---|---|---|
| `scheduled` | Expected occurrence has no observed dispatch yet and is not overdue under the observer's policy | none |
| `running` | Hermes claimed the occurrence; execution may or may not have started | `claimed_at`; optional `started_at` |
| `completed` | Hermes recorded a normal terminal completion | `claimed_at`, `started_at`, `finished_at` |
| `failed` | Hermes recorded a normal terminal failure | all three timestamps, `result_code=execution_failed` |
| `unknown` | Dispatch occurred but recovery cannot prove its outcome | `claimed_at`, `finished_at`, optional `started_at`, `result_code=execution_unknown` |
| `not_dispatched` | The independent observer's documented grace expired with no execution row | `result_code=not_dispatched` |

All timestamps use second-precision UTC `Z`. Execution timestamps cannot
precede `scheduled_for`, must be ordered, and cannot follow `generated_at`.
`generated_at` cannot be in the future. Unknown fields, missing/extra jobs,
arbitrary result codes, free text, URLs, paths, prompts, raw errors, and nulls
are rejected. Producer code must construct this allowlisted projection; token
validation is not a secret detector.

The application reports whether the observer snapshot itself is stale. The
default boundary is 15 minutes: exactly 15 minutes old is current and anything
older by a published whole second is stale. The response derives
`observer.is_stale` from its integer fields: ages of 900 and 900.5 seconds both
publish `age_seconds=900` and are current; 901 seconds is stale. This measures
exporter freshness only. It does not decide when a weekly job is overdue; grace
periods and `not_dispatched` classification belong to the independent observer.

## Read-only deployment wiring

Prepare an external parent directory, owned by the deployment account and not
inside the application checkout. The finalized snapshot should be written to a
temporary regular file in the same directory, flushed, and atomically renamed
to `scheduler-status.json`. Never update the live file in place.

```bash
export CLIMATE_JOB_STATUS_HOST_DIR=/external/sanitized-job-status
.venv/bin/python -m scripts.safe_compose \
  -f docker-compose.yml \
  -f docker-compose.job-status.yml \
  config --quiet
```

This read-only `config` command uses Compose's normal passthrough behavior. Use
the same wrapper and override set for a later container-creating command so its
final resolved mount and host path receive the documented preflight checks.

The override mounts the parent directory read-only at `/job-status`, disables
implicit host-path creation, and sets `CLIMATE_JOB_STATUS_DIR=/job-status`.
The app performs a bounded, no-follow regular-file read for every request, so
atomic replacement is visible without restart. It never creates, repairs,
caches, or writes the snapshot.

The raw configured path must be absolute and cannot contain `..`, a symlink, or
a Windows reparse point. On POSIX the reader anchors the file open to a verified
directory descriptor. Other platforms verify the parent and file identities
before and after the read and fail closed on a change. These checks support the
read-only deployment boundary and catch replacement races; they are not a
claim of resistance to a hostile same-user process controlling the trusted
host directory.

Do not mount the Hermes database, `jobs.json`, prompts, logs, or any Hermes
state directory into the public application container. They may contain
operational data, recipients, or secrets. Native best-effort hooks alone also
cannot prove that a scheduler never dispatched a job.

`scripts/export_scheduler_status.py` is the implemented sanitized exporter. It
opens the Hermes executions SQLite database read-only, accepts a separate
four-alias/ID allowlist, and writes only this validated snapshot. The exporter
and application slot writer share the stable external
`.scheduler-status.lock` across each complete read/validate/write transaction;
same-occurrence merging retains a newer verified application completion while
Hermes is catching up, but a newer execution or fortnight supersedes it. The
observed production execution database is
`/home/ubuntu/.hermes/cron/executions.db`, and IDs must be read back from
`/home/ubuntu/.hermes/cron/jobs.json` into a separate sanitized map. Exporter
systemd timer installation and scheduler cutover remain unperformed and require their
own deployment evidence.

## API and rollback

| HTTP | Reason | Meaning |
|---|---|---|
| 200 | — | Strict snapshot loaded; `observer.is_stale` reports freshness |
| 503 | `not_configured` | `CLIMATE_JOB_STATUS_DIR` is unset |
| 503 | `invalid_location` | Parent is relative, inside the repo, not a directory, or linked |
| 503 | `snapshot_unavailable` | Snapshot parent/file is missing or unreadable |
| 503 | `invalid_snapshot` | File is nonregular, oversized, corrupt, raced, or violates v1 |

These failures do not change `/api/health`, the homepage, Chat, Registry, or
`/api/update-status`.

Public reader deployment and rollback are app-only: add or remove
`docker-compose.job-status.yml` and rebuild/recreate only the Wiki app. Do not
restart or reload Caddy. Do not modify the 08:00 Monitor, 09:00 Email, or 10:00
Publisher jobs. This reader phase creates no scheduler job or exporter timer
and does not create the pending 10:30 Weekly Registry Sync task.
