# Hermes Entrypoint Notes

Current operations caveat (2026-09-07 audit): the four climate cron jobs and
runtime configuration are not provisioned. Monday 08/09/10/10:30 UTC means
16/17/18/18:30 CST in Hermes (Asia/Shanghai); never change the global timezone.
Each `scripts/hermes_job_*.sh --preflight` is read-only and fails closed on
missing configuration. Production monitor acquisition remains blocked until
the same-run outcome/evidence/authoring link is executable. Fixtures require
`CLIMATE_DRY_RUN=1` plus `CLIMATE_DRY_RUN_FIXTURE_DIR` and an isolated
`CLIMATE_DRY_RUN_ROOT`. Render has no shared source for local scheduler evidence;
`/api/job-status` remains HTTP 503 `not_configured`. Issue #87 stays OPEN until
a normal Monday run matches through delivery, reviewed publication, deployment
and Registry. See PIPELINE_REFERENCE.md for the intended wrapper contracts;
historical job IDs below are not proof of current provisioning.


Hermes remains the live scheduler and runtime owner. This file documents how a
separately authorized Hermes command may call the repository-owned strict
driver from an exact deployed commit.

Wrapper template (operator supplies real, absolute paths from
`PIPELINE_REFERENCE.md`; missing configuration fails closed):

```bash
export REPO=/absolute/reviewed/application/checkout
export PYTHON=/absolute/reviewed/runtime/bin/python
export REPORT_DATE="$(date -u +%F)"
bash "$REPO/scripts/hermes_job_monitor.sh" --preflight
```

Read-only preflight does not provision the upstream chain or authorize dispatch.
Once actual prerequisites are satisfied, the corresponding normal wrapper
command is the same command without `--preflight`. Do not remove that flag to
work around a failed check. Configure Hermes's local schedule explicitly:
`0 16 * * 1` (Asia/Shanghai) is Monday 08:00 UTC. The remaining slots are
`0 17 * * 1`, `0 18 * * 1`, and `30 18 * * 1`. Job readback is required;
this template and historical capture do not prove that a job exists.

`authoring-response.json` is the output of the single existing authoring pass.
The repository validates it and writes artifacts only after validation passes.
Provider credentials, runtime limits, host paths, logs, and actual Hermes job
payloads stay outside this repository.

The versioned prompt body used by this path is the exact captured production
prompt in `../prompts/weekly-monitor-v1.prompt.md`; the raw
`job-08h-monitor.json` payload remains uncommitted.

This file is not `job-08h-monitor.json`, not an active Hermes config, and not a
replacement scheduler.

The raw `job-08h-monitor.json` has been provided as an external local
attachment and verified, but it is intentionally not committed. The earlier
`monitor-08h-package.tar.gz` still reportedly did not contain the README-claimed
`job-08h-monitor.json`. The repository records only redacted metadata for the
live job in
`../provenance/captures/hermes-job-f5259a8ec2d9.redacted.json`.
