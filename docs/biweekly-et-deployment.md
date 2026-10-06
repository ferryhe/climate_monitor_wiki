# Biweekly ET deployment and no-send rehearsal

The current Issue #197 deployment target is the `independent-et` profile:
ten Hermes cron jobs (T1–T10) plus the existing independent PDF intake writer.
Retain T1 article and meeting information checks daily at 05:00 ET
(`America/New_York`). Use the [ten-role configuration](../PIPELINE_CONFIG.md#independent-task-profile)
and [independent task cutover](#independent-task-cutover) below for the current
schedule and deployment gates.

## Historical four-slot profile (compatibility only)

The four-slot schedule and procedures below are retained only for historical
receipts and controlled compatibility. They are superseded by the Issue #197
target and must not be installed alongside it. The old schedule is every other
Monday from **September 14, 2026**, at 08:00, 09:00, 10:00, and 10:30 ET.
Persisted timestamps remain UTC instants. The shared guard in
`climate_monitor.schedule` preserves these wall-clock times across daylight
saving time and rejects alternate weeks.

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

## Independent task cutover

The Issue #197 target supersedes the four-slot business chain above; those
contracts remain for historical receipts and controlled compatibility. The
October 6, 2026 Issue inventory reported eight enabled jobs and twelve disabled
Step jobs, with T10 uninstalled. This is dated evidence, not current deployment
proof. Use the [ten-role configuration](../PIPELINE_CONFIG.md#independent-task-profile).

Before switch, back up Registry databases and necessary SQLite sidecars, active
pointers/immutable generations, all external runs/reviews/report/mail state, task
definitions, job configuration/IDs and old image identity. Use normal schema
migration with exact backup/restore verification. Read back actual deployed
commit/image/mounts/fonts/Poppler/native tools and four-recipient private config.
Stop the old Monitor/Artifact/Publisher/Registry dispatch before enabling the six
new jobs; retain T1/T7–T9, the writer and twelve disabled Steps. Verify ten actual
IDs, prompts/scripts, UTC ticker, no_agent, enabled and next occurrence.

Native T5/T10 must run independent Hermes agent cron sessions. The identity
bridge preflight proves only current session injection. Real T5 text/page-tool
inspection, mixed five-source T10 review/recovery, original budget reuse,
partial activation, governed refusals and proposals require isolated real runs.
Keep T6 no-send throughout rehearsal; it must never load production SMTP.
Verify repeated ticks, late approval, unchanged vs substantive checks, old PASS
invalidations, code blockers, hash changes and unknown delivery.

For a private native probe or rehearsal, use an external sandbox for every database,
writer queue/runtime, managed run, rotation state, report and check output.
Point `CLIMATE_ACQUISITION_RUN_DIR`, `CLIMATE_REPORT_REVIEW_DIR`,
`CLIMATE_INTAKE_QUEUE_DIR` and the managed task's Registry/run paths there.
Keep the native Hermes session DB readable by the review CLI (a read-only mount
is sufficient if commands execute in the application container). The actual
`HERMES_SESSION_ID` is injected per terminal command; do not select a latest
session. Native `cron.scheduler.run_job(job, execution_id=...)` can run an
isolated capability probe without registering it in the production job list: use a unique
job ID, `no_agent=false`, `context_from=None`, the application workdir and the
existing effective provider/model. T5 needs native file/terminal/vision tools;
T10 needs file/terminal and the existing governed recovery environment.

Use the exact installed prompt text from `monitoring/report-review.md` or
`monitoring/acquisition-review.md`, with the read-only `review_pipeline.py peek`
pre-script. Record that script/prompt hash, native job/session/tool-call IDs,
run/batch/task/packet identity and all real outcomes. The peek JSON supplies the
precise target and resolved frozen text/page paths. These commands are also
available without installing jobs:

```bash
python scripts/review_pipeline.py peek --kind acquisition --root "$CLIMATE_ACQUISITION_RUN_DIR"
python scripts/generate_range_report.py --biweekly-date YYYY-MM-DD --database SANDBOX_PUBLIC_DB --artifact-root "$CLIMATE_REPORT_REVIEW_DIR" --runtime-dir SANDBOX_RUNTIME --queue-dir "$CLIMATE_INTAKE_QUEUE_DIR"
python scripts/review_pipeline.py peek --kind report --root "$CLIMATE_REPORT_REVIEW_DIR"
python scripts/review_pipeline.py send --root "$CLIMATE_REPORT_REVIEW_DIR" --target YYYY-MM-DD
```

For AC-12/T5 rehearsal, use an isolated `HERMES_HOME` with temporary installed
native cron definitions and real scheduler dispatches. Isolate the complete
Hermes HOME, not only the cron store: `state.db`, `executions.db`, output,
locks and workers must belong to that sandbox. Place pre-scripts in its
`scripts/` directory and use an external absolute workdir. Dispatch with
`cron.scheduler.tick(verbose=False, sync=True)` so due scanning, scheduler
execution history and CAS occurrence claiming run through the actual entry.
Keep production job definitions unchanged. Save precise before/after production
snapshots and probe execution queries; distinguish ordinary production scheduler
timestamps/history updates from sandbox writes, and do not claim whole-file SHA
equality unless measured. Use a declared sandbox tick frequency, retain actual next
dispatches, executions/history and native sessions, and require two distinct
dispatch sessions for modification followed by PASS. Direct `run_job` probes
prove identity/tool capability only and do not satisfy that next-cron gate.

The last command defaults to no-send and needs no SMTP configuration. Do not
pass `--send` during rehearsal. A changes-requested submission creates a new
revision; wait for the next actual isolated scheduler dispatch to inspect it. `recover` uses the original
run and token, while `activate` consumes only exact current PASS candidates.
Read `state.json`, immutable packets/revisions, native claim history, review
receipts, writer `status.json` and per-recipient delivery state; a native engine
success is not a business approval. Use actual clocks for the sixty-minute
real rehearsal gate; local tests alone exercise substituted boundary times.

After deployment verify compose health, sanitized `/api/config`, activated
Web/Chat/report corpus identity and fresh scheduler/business snapshots. The
first real scheduled T1 article AND meeting results, plus a complete real
biweekly production cycle and separately authorized exact-file sending, remain
completion gates. Tests, preflight, job enabled/cron exit zero, no-send and Issue
closure cannot substitute for them.
