# Sanitized scheduler status snapshot

`GET /api/job-status` reads one sanitized, atomically replaceable observer
snapshot. It is deliberately separate from the detailed producer-owned
`weekly-run-attempt.v1` ledger exposed by `/api/update-status`.

This phase defines only the application-side read contract. It does not create
an exporter, systemd timer, Hermes hook, scheduled job, or production snapshot.
The observer/exporter remains a separately reviewed server operation.

Operationally, the 09:00 Weekly Climate Email job is enabled and retained as the
only delivery-artifact producer and the intentional email path for four current
recipients. Version 1 observes only the existing 08:00, 09:00, and 10:00 aliases;
it does not include or prove the proposed 10:30 Weekly Registry Sync. The
application implementation and tested runner are deployed, but the Hermes task
remains unconfigured. Publisher ledger identity repair/validation, task creation
and enablement, and one observed normal cycle remain before the project can be
described as `PRODUCTION COMPLETE`.

## Contract

The fixed filename is `scheduler-status.json`. The strict
`weekly-job-status.v1` shape is:

```json
{
  "schema_version": "weekly-job-status.v1",
  "generated_at": "2026-08-17T10:05:00Z",
  "jobs": {
    "monitor": {
      "scheduled_for": "2026-08-17T08:00:00Z",
      "state": "completed",
      "claimed_at": "2026-08-17T08:00:01Z",
      "started_at": "2026-08-17T08:00:02Z",
      "finished_at": "2026-08-17T08:40:00Z"
    },
    "email": {
      "scheduled_for": "2026-08-17T09:00:00Z",
      "state": "failed",
      "claimed_at": "2026-08-17T09:00:01Z",
      "started_at": "2026-08-17T09:00:02Z",
      "finished_at": "2026-08-17T09:01:00Z",
      "result_code": "execution_failed"
    },
    "publisher": {
      "scheduled_for": "2026-08-17T10:00:00Z",
      "state": "scheduled"
    }
  }
}
```

The three aliases are public names; Hermes job IDs are never accepted. Version
1 deliberately couples them to the UTC Monday containing `generated_at`, at
08:00, 09:00, and 10:00 UTC. A Monday-morning snapshot may therefore keep the
later Email and Publisher occurrences in `scheduled`. A past or future week's
schedule is invalid. A schedule change requires an application contract update
as well as the separately controlled scheduler change.

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
cannot prove that a scheduler never dispatched a job. A future sanitized
exporter must independently query authoritative execution state and publish
only this allowlisted snapshot. Its implementation and any systemd timer are
deferred.

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

Deployment and rollback are app-only: add or remove
`docker-compose.job-status.yml` and rebuild/recreate only the Wiki app. Do not
restart or reload Caddy. Do not modify the 08:00 Monitor, 09:00 Email, or 10:00
Publisher jobs. This phase creates neither a job nor an exporter, and it does
not create or observe the pending 10:30 Weekly Registry Sync task.
