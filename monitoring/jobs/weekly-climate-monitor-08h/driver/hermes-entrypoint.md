# Hermes Entrypoint Notes

Current status (2026-09-08 SSH audit): production has 12 enabled legacy Step jobs;
the four-slot wrapper schedule is not installed. The same-run monitor path is
implemented and sandbox-tested. Issue #87 is owner-closed. Deployment still
requires the input and full-chain gates in
[PIPELINE_REFERENCE.md](../../../../PIPELINE_REFERENCE.md).

The production wrapper invokes the existing CLI with `--authoring-mode run`:
prepare frozen evidence, author independent URLs serially, checkpoint each result,
generate the executive narrative once, then finalize. It requires matching
upstream outcome/manifest and Pillar B artifacts; it does not produce those inputs.
Relevance and semantic authoring share each URL's request. Failed URLs remain
resumable, and unfinished work blocks the final report.


Hermes remains the live scheduler and runtime owner. This file documents how a
separately authorized Hermes command may call the repository-owned strict
driver from an exact deployed commit.

Wrapper template (operator supplies real, absolute paths from
[PIPELINE_REFERENCE.md](../../../../PIPELINE_REFERENCE.md); missing configuration fails closed):

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

`authoring_response.json` in staging combines validated per-URL checkpoints and
the executive result. The library driver validates that completed response and
writes canonical artifacts only after validation passes; it does not call a
second model or recrawl evidence during finalize.
Provider credentials, runtime limits, host paths, logs, and actual Hermes job
payloads stay outside this repository.

Editable search and relevance templates live under `../prompts/` and are loaded
through `prompt_loader`. URL/executive instruction templates remain in the
existing CLI. The exact `weekly-monitor-v1.prompt.md` remains pinned legacy
provenance/compatibility evidence, not a second live whole-week task. Its bytes
and the raw uncommitted `job-08h-monitor.json` are not scheduler configuration.

Fixtures require `CLIMATE_DRY_RUN=1`, `CLIMATE_DRY_RUN_FIXTURE_DIR` and an isolated
`CLIMATE_DRY_RUN_ROOT`. Read-only preflight and fixture success do not establish
production readiness. Wrapper snapshots are local-only; Render has no shared
source and `/api/job-status` remains 503 `not_configured` without a snapshot.

This file is not `job-08h-monitor.json`, not an active Hermes config, and not a
replacement scheduler.

The raw `job-08h-monitor.json` has been provided as an external local
attachment and verified, but it is intentionally not committed. The earlier
`monitor-08h-package.tar.gz` still reportedly did not contain the README-claimed
`job-08h-monitor.json`. The repository records only redacted metadata for the
job as observed at capture time in
`../provenance/captures/hermes-job-f5259a8ec2d9.redacted.json`.
