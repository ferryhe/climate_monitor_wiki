# Parity, Cutover, And Rollback

This runbook is for separately authorized host-runtime work. This repository
change does not perform cutover, does not edit live Hermes jobs, and does not
claim an observed Monday production run.

## Controlled Parity

Reference the sanitized capture metadata at
`provenance/captures/hermes-job-f5259a8ec2d9.redacted.json` only as provenance.
It records externally verified metadata from a local `job-08h-monitor.json`
attachment, including file and prompt hashes, while leaving the raw job JSON
uncommitted. It also records that the earlier `monitor-08h-package.tar.gz`
omitted the README-claimed `job-08h-monitor.json`. The redacted capture is not
a raw job record and not an executable Hermes payload.

Before changing the live Hermes job, run the previous external workflow and the
repository-owned strict driver against the same captured inputs in a
no-production-write environment.

Compare:

- Final article identities and order.
- Markdown bytes.
- Semantic sidecar bytes and validation result.
- Warnings and dedupe notes.
- Dedupe seen-state mutations.
- Prompt SHA, taxonomy SHA, report SHA, and sidecar SHA.
- Model/provider call count; there must be no additional per-article call.

Explain every difference. Do not weaken assertions or silently repair invalid
authoring output to make parity pass.

## Hermes Cutover

Cutover is a host-runtime operation and requires separate authorization.

Required constraints:

- Preserve Hermes job ID `f5259a8ec2d9`.
- Preserve Monday 08:00 UTC dispatch.
- Preserve runtime credentials, host paths, resource limits, and logs/state.
- Preserve the 09:00 email/PDF producer and the 10:00 Publisher behavior.
- Use the repository CLI from an exact deployed commit.
- Verify the deployed prompt SHA before enabling the changed command.

If Hermes cannot read the repository prompt file directly, deployment may
render or copy the prompt from the exact reviewed commit and verify its SHA.
That copied file is an operational artifact, not a second authoritative prompt.

Do not create `job-08h-monitor.json`. The raw live cron-job record remains
external/local-only. The captured production prompt is versioned in
`prompts/weekly-monitor-v1.prompt.md`; redacted snapshots are provenance only,
not active executable config.

## Rollback

Before cutover, record the previous live Hermes command/config and the reviewed
repository commit used for the candidate. Rollback is to restore the previous
Hermes command/prompt source or redeploy the previous reviewed repository
state, then verify that no duplicate generator or schedule was introduced.

Do not delete or rewrite historical source reports, semantic sidecars, delivery
artifacts, Registry data, or weekly-run-ledger attempts as part of application
rollback unless a separate audited runbook explicitly authorizes that action.

## Completion Criteria

Do not claim production migration complete until all of the following are true:

- Controlled parity passed against captured inputs.
- A controlled exact-date run produced validated report and sidecar hashes.
- The live Hermes job still has ID `f5259a8ec2d9` and Monday 08:00 UTC cadence.
- The 09:00 email/PDF producer and 10:00 Publisher still consume the same
  canonical report identity.
- One normal scheduled Monday run has been observed with the expected
  repository commit and prompt SHA.
- No invalid output was silently dropped, repaired, or hidden.
