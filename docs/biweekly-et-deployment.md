# Biweekly ET deployment and no-send rehearsal

The target schedule is every other Monday from **September 14, 2026**, at
08:00, 09:00, 10:00, and 10:30 ET (`America/New_York`). Persisted timestamps
remain UTC instants. The shared guard in `climate_monitor.schedule` preserves
these wall-clock times across daylight saving time and rejects alternate weeks.

| ET | Slot | Completion evidence |
|---|---|---|
| 08:00 | monitor | Managed terminal result, exact Registry-selected evidence, report SHA, and semantic sidecar |
| 09:00 | PDF/delivery | Exact-report PDF and manifest; cutover uses artifact-only no-send, sandbox rehearsal uses `--dry-run` |
| 10:00 | publisher | Isolated rolling-PR boundary; no production checkout write |
| 10:30 | Registry | Separate merge/deploy, identity, enable, and write gates |

The two-hour monitor/publisher gap is intentional. `completed_with_gaps` is
publishable only when the frozen projection says `reportable=true` and all
authoring, executive, finalization, sidecar, PDF, and manifest checks pass.
`no_eligible_information` completes the monitor without fabricating a report,
so the report-consuming slots record the same verified no-report outcome as a
completed no-op rather than an infrastructure failure. `systemic_failure` and
integrity failures fail the run.

## Runtime and scheduler

Use GPT-5.6 Luna (`gpt-5.6-luna`) with a verified provider for acquisition and
authoring. Keep the library's `daily` default unchanged; this deployment opts
into the existing weekly report type on guarded biweekly dates. Store the task,
Registry database, run state, generated sources/wiki, ledger, delivery output,
and scheduler snapshot on persistent paths outside the checkout.

On a UTC Hermes ticker, install script-only, `no_agent=true` checks at:

| Slot | UTC cron checks | Command suffix |
|---|---|---|
| monitor | `0 12,13 * * 1` | `timeout 4200s python scripts/hermes_job.py monitor --managed --scheduled` |
| email | `0 13,14 * * 1` | `scripts/hermes_job.py email --scheduled` |
| publisher | `0 14,15 * * 1` | `scripts/hermes_job.py publisher --scheduled` |
| registry | `30 14,15 * * 1` | `scripts/hermes_job.py registry --scheduled` |

Only the ET-matching check runs business work. Do not use a fixed 336-hour
timer or a day-of-month `*/14` expression. Read back actual job IDs, commands,
timezone, enabled state, and next occurrences. Do not enable these jobs until
the separate deployment authorization. Run `scripts/export_scheduler_status.py`
every five minutes against the real Hermes executions database and an external
four-alias job map; the exporter is read-only except for its atomic status file.
The 4,200-second outer timeout is deliberately longer than the task's current
3,600-second runtime budget plus the bridge's 300-second terminal allowance.
The observed host clock is UTC. Its current Hermes `config.yaml` leaves both
`timezone` and `cron.script_timeout_seconds` unset, and the gateway has no
`TZ`, `HERMES_TIMEZONE`, or `HERMES_CRON_SCRIPT_TIMEOUT` override. Native
Hermes therefore uses its 3,600-second script-timeout default, which would end
the job before the documented 4,200-second wrapper can finish. Before enabling
the four slots, the controller-owned cutover must set
`cron.script_timeout_seconds: 4500`, restart/read back the scheduler, and
preserve the UTC ticker and unrelated cost job. This requirement is not yet
installed; no current Hermes configuration was changed here.

Set `CLIMATE_DELIVERY_NO_SEND=1` only on the 09:00 email job for the prepared
cutover. This is not the global `CLIMATE_DRY_RUN`: it permits the normal
persistent report, state, output, and status paths, invokes the single-report
delivery pipeline in `--artifact-only` mode, verifies the sidecar-bound PDF and
manifest, and never loads mail configuration or calls SMTP. The manifest records
`delivery.status=artifact-only` with `recipients=[]`; scheduler status records
`not_dispatched/delivery_no_send`. Leaving the variable absent preserves the
existing configured sending path and its four-recipient validation.

### Observed paths and required cutover wiring

The 2026-09-13 read-only inspection recorded these host-side mappings (concrete values live only on
the host and in its untracked `.env`); it did not install or change them:

- Hermes execution data is `$HERMES_HOME/cron/executions.db`; the job
  configuration used to obtain and verify the four real IDs is
  `$HERMES_HOME/cron/jobs.json`.
- The `climate-wiki-app` named volume
  `climate_monitor_wiki_climate_runtime` maps host
  `<docker-root>/volumes/climate_monitor_wiki_climate_runtime/_data` to
  `/app/output` read-write. No `/pipeline` mount is currently installed.
- The current v3 task is `/app/output/task/task-definition.json`, with versions
  under `/app/output/task/versions`, Registry at
  `/app/output/climate_registry.sqlite3`, and runs under
  `/app/output/acquisition-runs`. It is already bound to `openai-api`,
  `gpt-5.6-luna`, `America/New_York`, and `report_date=auto`.
- The observed managed state setting is `/app/output/monitor-state`.
  `CLIMATE_MANAGED_SOURCE_DIR` and `CLIMATE_MANAGED_WIKI_DIR` are absent, so the
  application defaults still resolve to checkout-backed source/wiki paths.
- The public app currently sees host
  `<host-data-dir>/job-status` as `/job-status` read-only.
  The host-side exporter must write that host directory; the public app must
  keep the mount read-only. The wrapper/producer needs the same directory
  read-write in its own execution namespace so `update_slot` and Registry
  pending results share `.scheduler-status.lock`. The currently observed
  read-only public-app mount is not sufficient for running the wrapper there.
- Other observed public read-only binds are
  `<host-data-dir>/registry` at `/registry`,
  `<host-delivery-artifacts-dir>` at `/delivery-output`, and
  `<host-data-dir>/update-status` at `/update-status`.
  Delivery generation needs its own read-write producer mount of the delivery
  artifact directory; this does not make the public app mount writable.

The current checkout-backed `sources`, `wiki`, and `article_metadata` mounts
were observed read-write. They are not valid managed-generation targets.
The **required cutover configuration**, which is not installed, adds the same
named volume at `/pipeline` read-write for the producer and sets managed state,
source, and wiki to `/pipeline/monitor-state`,
`/pipeline/generated/sources`, and `/pipeline/generated/wiki`. Their
host-visible paths are the named-volume `_data/monitor-state`,
`_data/generated/sources`, and `_data/generated/wiki` paths. The host's
`/var/lib/docker` is `0710 root:root`; an `ubuntu` host process cannot assume
it can traverse `_data/generated/sources`. Before cutover, validate that the
chosen publisher execution identity and mount namespace supply read access to
the generated source, then keep the existing temporary-clone/rolling-PR flow.
Do not copy the generated report into the
production checkout or point `CLIMATE_MANAGED_SOURCE_DIR` at `/app/sources`.
Deployment of a human-merged report into the public checkout remains a
separate operation.

Under that required cutover configuration, if the wrapper runs in the
application image namespace, keep
`CLIMATE_TASK_CONFIG=/app/output/task/task-definition.json`,
`CLIMATE_TASK_VERSION_DIR=/app/output/task/versions`,
`CLIMATE_ACQUISITION_RUN_DIR=/app/output/acquisition-runs`,
`CLIMATE_MANAGED_STATE_DIR=/pipeline/monitor-state`,
`CLIMATE_MANAGED_SOURCE_DIR=/pipeline/generated/sources`, and
`CLIMATE_MANAGED_WIKI_DIR=/pipeline/generated/wiki`. Bind
`CLIMATE_SOURCE_DIR` to that same managed source directory for the bridge's
report identity check. The producer also needs writable external
`CLIMATE_RUN_LEDGER_DIR` and `CLIMATE_JOB_STATUS_DIR`; keep the public app's
corresponding observer mounts read-only. A host-native wrapper cannot reuse
those container-only strings:
create and validate a task version whose Registry/run paths are the exact
host-visible named-volume paths before starting a new run. Never edit an
existing run binding to migrate it.

For the 09:00 producer, resolve `CLIMATE_REPORT_PATH` to that occurrence's
canonical `climate-monitor-YYYY-MM-DD.md` under the same generated source
directory, and give the delivery process writable `CLIMATE_DELIVERY_OUTPUT_DIR`
and `CLIMATE_DELIVERY_STATE_DIR`. Its public `/delivery-output` mount remains
read-only. With `CLIMATE_DELIVERY_NO_SEND=1`, do not set or invent a delivery
config or SMTP values; they are outside the artifact-only contract. The 10:00
publisher consumes the generated source only through the validated execution
identity/mount above and writes only its temporary clone and rolling branch.
The observed remote is HTTPS; `/usr/bin/gh`, the existing `ubuntu` GitHub CLI
configuration, and the project virtual environment are present, but their
access must still be verified under the exact scheduled publisher identity.
Do not alter host permissions or security configuration.

The acquisition writer requires the Registry schema pinned by
`climate_registry.acquisition.ACQUISITION_WRITER_SCHEMA_VERSION` (12 since the
2026-09-22 migration of both databases, recorded in issue #150 — see the upgrade
checklist in [deployment.md](deployment.md#upgrade-checklist)); readers retain their
supported older-schema read-only compatibility. Before migrating the runtime
and public Registry databases, the controller must quiesce all writers and
capture a verified private full-database backup together with its sidecars,
exact path/role identity, and hashes. Run the normal Registry migration, then
read back and validate the resulting schema and data before enabling any slot.
Before any schema-10 recovery write, rollback may restore that complete backup
and the old image. After a schema-10-only `no_search` to `attempted` recovery
transition, do not downgrade the database or selectively restore rows: retain
the schema then in place, or restore the entire pre-migration snapshot only under an
explicit data-loss decision. No production Registry migration was performed here.

No production mount, task, job, or file was changed while preparing this
runbook. A private host-only rollback baseline exists at
`<host-only-rollback-baseline-dir>`; do not copy it into this
repository or publish it.

Run the exporter on the host against the actual database. `--jobs-map` is a
separate sanitized JSON object with exactly the `monitor`, `email`,
`publisher`, and `registry` aliases and the four IDs read back from
`jobs.json`; `jobs.json` itself is not that allowlisted schema. No deployed
path for this sanitized map has yet been evidenced, so the operator must set
and record its absolute external path rather than guessing one:

```bash
export ISSUE124_JOB_MAP=/absolute/external/path/verified-hermes-job-map.json
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"   # the exporter reads the host Hermes home
test -f "$ISSUE124_JOB_MAP"
timeout 60s python scripts/export_scheduler_status.py \
  --executions-db "$HERMES_HOME/cron/executions.db" \
  --jobs-map "$ISSUE124_JOB_MAP" \
  --status-dir /path/to/job-status
```

Read back the four IDs and the generated snapshot before installing a
five-minute timer. Exporter/timer installation and the four-slot cutover have
not been performed.

### Explicit same-run recovery

A retryable managed failure is recovered only by its exact scheduled run ID.
Do not add `--scheduled`; recovery may occur outside the five-minute cron tick
but must remain in the current report fortnight and after the monitor slot:

```bash
export REPORT_DATE=YYYY-MM-DD
export ISSUE124_RUN_ID=the-exact-scheduled-run-id
timeout 4200s python scripts/hermes_job.py monitor --managed \
  --resume-run-id "$ISSUE124_RUN_ID"
```

The bridge rejects manual runs, another report date/task/runtime path, and
non-retryable failures. It attaches to an already-active attempt, resumes a
retryable attempt through `ManagementService.resume`, or idempotently
reconciles an already completed terminal receipt. A normal scheduled start for
the same task/date is rejected with the existing run ID; it never creates a
fresh lineage. Preserve the failed attempt, checkpoint, request ledger,
cumulative budget, and successful receipts when diagnosing recovery.

## Isolated real-source rehearsal

Run this only in a separate server sandbox. Do not mount production sources,
state, Registry, publisher credentials, SMTP credentials, or mail state. Set
`CLIMATE_DRY_RUN=1`, `CLIMATE_SCHEDULE=biweekly-et`, `TZ=America/New_York`, and
`HERMES_TIMEZONE=America/New_York`. Use a sandbox task definition and database,
but the real governed `web_listening` runtime and GPT-5.6 Luna.

Create the isolated root first. The management task that creates the binding
must place every runtime path below this root. Supply only the newly created
binding and delivery config paths; never point either variable at `/srv`, the
production checkout, or production mail state.

```bash
set -eu
export ISSUE124_SANDBOX_ROOT="$(mktemp -d /tmp/climate-issue124.XXXXXX)"
chmod 700 "$ISSUE124_SANDBOX_ROOT"
export CLIMATE_DRY_RUN=1 CLIMATE_SCHEDULE=biweekly-et
export TZ=America/New_York HERMES_TIMEZONE=America/New_York
export ISSUE124_BINDING="$ISSUE124_SANDBOX_ROOT/run/attempt-1.json"
export ISSUE124_DELIVERY_CONFIG="$ISSUE124_SANDBOX_ROOT/delivery-config.yaml"
chmod 600 "$ISSUE124_DELIVERY_CONFIG"
test -f "$ISSUE124_BINDING"
test -f "$ISSUE124_DELIVERY_CONFIG"
case "$(realpath -- "$ISSUE124_BINDING")" in "$ISSUE124_SANDBOX_ROOT"/*) ;; *) exit 2;; esac
case "$(realpath -- "$ISSUE124_DELIVERY_CONFIG")" in "$ISSUE124_SANDBOX_ROOT"/*) ;; *) exit 2;; esac
```

1. Configure one Pillar A source URL that is known to return a real reader
   failure in the sandbox, plus the normal governed Pillar B capability. Do not
   fabricate a manifest or edit the returned status.
2. Run `python scripts/run_agent_acquisition.py --binding "$ISSUE124_BINDING"`.
   Inspect `attempt-1-trusted-context.json`, the Hermes transcript/provenance,
   `attempt-1-acquisition.json`, and the Registry readback. They must show the
   actual Pillar A reason, the agent's alternative/retry or Pillar B decision,
   and no repeated identical dispatch.
3. Continue only if at least one eligible exact content version is selected.
   Verify `frozen-report-input.json` reports `completed_with_gaps`, retains the
   limitation, and passes Registry readback. Let the existing serial authoring,
   executive, and finalize path write the report and semantic sidecar.
4. Point `ISSUE124_REPORT` at the sandbox report, then run:

   ```bash
   export ISSUE124_REPORT_DATE=YYYY-MM-DD
   export ISSUE124_REPORT="$ISSUE124_SANDBOX_ROOT/sources/climate-monitor-$ISSUE124_REPORT_DATE.md"
   test -f "$ISSUE124_REPORT"
   ISSUE124_REPORT_SHA="$(sha256sum "$ISSUE124_REPORT" | cut -d' ' -f1)"
   python -m climate_delivery.cli run \
     --report "$ISSUE124_REPORT" \
     --output-dir "$ISSUE124_SANDBOX_ROOT/delivery" \
     --state-dir "$ISSUE124_SANDBOX_ROOT/mail-state" \
     --config "$ISSUE124_DELIVERY_CONFIG" \
     --expected-report-sha256 "$ISSUE124_REPORT_SHA" \
     --dry-run | tee "$ISSUE124_SANDBOX_ROOT/delivery-result.json"
   ```

   The sandbox delivery config must use only non-routable `example.invalid`
   recipients and sandbox-specific environment-variable names; never copy the
   production recipient file or credentials. Confirm the PDF and manifest bind
   that SHA and the dry-run result says no dispatch. Invalid/non-routable SMTP
   values are an additional guard, not a substitute for `--dry-run`.
5. Do not invoke the publisher or Registry writer. Preserve the artifacts and
   record exact commands, revision, model/provider, source outcome, selected
   count, report/sidecar/PDF/manifest hashes, and zero-send evidence.

If the source failure becomes systemic, if no exact eligible item is selected,
or if selected-content integrity fails, the expected result is respectively
`systemic_failure`, `no_eligible_information`, or a validation error—not a
placeholder report. A useful partial report is valid evidence of recovery; it
must never be described as full coverage.
