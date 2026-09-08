# Weekly Monitor Ownership Runbook

This runbook describes the repository-owned pieces of the Monday 08:00 UTC
Weekly Climate & Actuarial Monitor and the boundary with Hermes.

Status: repository support is implemented for prompt bytes, strict authoring
validation, fixtures, same-run preparation, a serial URL queue, and safe provenance.
Hermes cutover, controlled production execution, and one observed normal Monday
run are not complete in this repository change.
The current operational authorities are
[PIPELINE_REFERENCE.md](../../../../PIPELINE_REFERENCE.md) and
[PIPELINE_CONFIG.md](../../../../PIPELINE_CONFIG.md).

## Ownership Boundary

Repository owns:

- Versioned prompt bytes in
  `monitoring/jobs/weekly-climate-monitor-08h/prompts/weekly-monitor-v1.prompt.md`.
  These remain a captured legacy contract, not the live whole-week task.
- Editable `article-relevance-v1.prompt.md` and `pillar-b-search-v1.prompt.md`
  in the same directory, plus the existing CLI's URL/executive instructions.
- Prompt metadata in
  `monitoring/jobs/weekly-climate-monitor-08h/prompts/weekly-monitor-v1.meta.json`.
- Prompt loading and SHA-256 provenance in
  `climate_monitor.weekly_monitor.prompt_loader`.
- The strict weekly driver in `climate_monitor.weekly_monitor.driver`.
- Preparation, independent per-URL calls and resumable checkpoints in
  `scripts/run_climate_monitor.py`; one executive call follows all URL results.
- Authoring request/response validation in
  `climate_monitor.weekly_monitor.authoring_contract`.
- Safe provenance assembly in `climate_monitor.weekly_monitor.provenance`.
- Structural authoring/provenance schemas and offline fixtures under
  `monitoring/jobs/weekly-climate-monitor-08h/contracts/`.
- Canonical Markdown and semantic sidecar validation before mutation.
- Public, path-free run provenance returned by the CLI JSON result.

Hermes/runtime owns:

- The actual live job inventory, command and timezone readback. The target
  monitor cadence is Monday 08:00 UTC, or 16:00 Asia/Shanghai. Historical job
  ID `f5259a8ec2d9` is capture provenance, not a required current job identity.
- Provider credentials, secret injection, runtime limits, working directories,
  host paths, and authoritative execution logs/state.
- Any live scheduler command change, enablement, or rollback.

Do not add a GitHub Actions generator, application cron, second scheduler, or
host-specific secret-bearing configuration in this repository.
Do not create `job-08h-monitor.json`.

## Redacted Hermes Capture Reference

The raw `job-08h-monitor.json` has been provided as a local attachment and
verified externally, but it is intentionally not committed here. The earlier
tarball `monitor-08h-package.tar.gz` reportedly had a README claiming
`job-08h-monitor.json` was included, but controller inspection found that the
actual tarball did not contain that file.

The repository keeps only sanitized metadata in
`provenance/captures/hermes-job-f5259a8ec2d9.redacted.json`: public job
identity/schedule, raw file hash/counts, redaction flags, delivery channels as
channel names only, safe runtime shape, and prompt hashes/lengths. The capture
file omits the prompt body. The repository's pinned compatibility prompt is at
`prompts/weekly-monitor-v1.prompt.md`; later reviewed changes mean its current
hash differs from the original capture. Host paths, origin
identifiers, delivery identifiers, credentials, and raw logs remain absent from
metadata outside that exact prompt artifact.

## Pinned v1 Prompt Identity

The repository prompt is loaded as raw bytes and identified by:

- Prompt ID: `weekly_monitor`
- Prompt version: `v1`
- Prompt SHA-256:
  `53896231729799845b48e85eb5901cacce03aa26cec2199e2176dc254d5b7909`,
  computed from the exact bytes of
  `prompts/weekly-monitor-v1.prompt.md` and pinned in
  `prompts/weekly-monitor-v1.meta.json`

The historical capture's `543f8c5b…` hash remains provenance for the originally
observed job. It is not the current repository prompt hash. This documentation
update changes neither pinned prompt bytes nor their metadata.

The authoring contract requires one `article-semantic-bundle.v1` for every final article
identity. Missing, duplicate, unknown, extra, malformed, unknown-category, or
otherwise invalid authoring output fails closed before the canonical report,
semantic sidecar, wiki sync, or dedupe seen-state are updated.

## CLI and compatibility replay

For a live same-run invocation, use the `--production-weekly --authoring-mode run`
recipe in [PIPELINE_REFERENCE.md](../../../../PIPELINE_REFERENCE.md#run-and-resume).
It binds the upstream outcome, manifest, Pillar B input, selected history,
evidence and destinations. It runs one fresh context per unfinished URL, with
relevance and semantic authoring together, followed by executive authoring.

The controlled fixture/manual CLI behavior remains available without strict
authoring mode:

```bash
python scripts/run_climate_monitor.py --manifest-fixture sample.json --no-sync
```

The older supplied-response compatibility replay remains available:

```bash
python scripts/run_climate_monitor.py \
  --production-weekly \
  --authoring-response authoring-response.json \
  --date 2026-05-18 \
  --json
```

In that replay, `--authoring-response` points to an already-produced response. The
library driver validates it; it does not make an additional model/provider call
and does not read provider secrets. Optional public model metadata can be
recorded with `--model-provider`, `--model`, `--temperature`, and
`--max-output-tokens`. Secret values, environment names carrying secrets, host
paths, raw logs, and exception text must not be supplied as public metadata.

## Provenance

Successful strict weekly runs expose safe provenance in the JSON result:

- Repository commit SHA.
- Prompt ID, version, and raw prompt SHA-256.
- Driver and authoring contract versions.
- Taxonomy ID and SHA-256.
- Canonical report filename and SHA-256.
- Semantic sidecar filename and SHA-256.
- Final article identities and count.
- Safe model/provider/settings metadata.

The JSON result intentionally contains sanitized filenames instead of
host-specific absolute paths.

Controlled parity, cutover, rollback, and completion gates are documented in
[`parity-cutover-rollback.md`](parity-cutover-rollback.md).
