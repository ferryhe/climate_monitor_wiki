# Weekly Climate Monitor 08h

This directory contains repository-owned management artifacts for the Monday
08:00 UTC Weekly Climate & Actuarial Monitor.

The repository owns the production-capable prepare/serial-authoring/finalize
CLI, editable prompts and strict contracts. Hermes supplies model execution and
host scheduling; `web_listening` owns acquisition. These files are not live
scheduler configuration. Current operations are documented in
[PIPELINE_REFERENCE.md](../../../PIPELINE_REFERENCE.md) and
[PIPELINE_CONFIG.md](../../../PIPELINE_CONFIG.md).

The 2026-09-08 SSH audit found 12 legacy Step jobs; the new four-slot schedule
had not been installed. Historical captures below do not prove current job IDs.

Key files:

- `manifest.json` describes the repo-owned artifact set.
- `task-definition.json` is the explicit legacy-bootstrap marker for the one
  versioned task consumed by the management console, acquisition launcher,
  search/relevance loaders and existing serial authoring driver. After the first
  authenticated save its configured external path is a self-contained state
  containing effective run parameters and exactly five prompt components.
- `scripts/run_agent_acquisition.py` is the common manual/scheduled launcher. It
  starts `hermes chat` detached through `ManagementService`; it is not an agent,
  queue, worker service, or search wrapper.
- `prompts/weekly-monitor-v1.prompt.md` is the pinned legacy compatibility
  contract, derived from the captured prompt with later reviewed changes. Its
  current hash is pinned in metadata; the original capture retains its own hash.
  Its old quota, host paths and tools
  are historical bytes, not the executing serial queue's instructions.
- `prompts/weekly-monitor-v1.meta.json` pins prompt identity and SHA-256.
- `prompts/article-relevance-v1.prompt.md` supplies relevance rules included in
  each URL's joint decision/summary/categories/keywords request.
- `prompts/pillar-b-search-v1.prompt.md` is the editable discovery task, rendered
  for an explicit report date through the existing prompt loader and CLI.
- `scripts/run_climate_monitor.py` at the repository root owns the independent
  URL queue, checkpoints, executive invocation and finalize handoff. The library
  driver validates completed responses; it does not launch a competing queue.
- `contracts/*.schema.json` define portable request, response, and provenance
  shapes. Runtime Python validation is stricter where identity binding matters.
- `driver/driver.v1.json` describes the repo-owned CLI driver metadata, not a
  Hermes job payload.
- `provenance/captures/hermes-job-f5259a8ec2d9.redacted.json` records safe
  metadata from the externally verified local job attachment. The raw
  `job-08h-monitor.json` is not committed, and the earlier tarball omission
  remains documented there.
- `docs/ownership-runbook.md` separates repository ownership from Hermes
  runtime ownership.
- `docs/parity-cutover-rollback.md` describes controlled parity, cutover, and
  rollback without claiming completion.

The console uses FastAPI Users 15.x (MIT), a maintained authentication component
compatible with FastAPI, for Argon2 password verification and signed JWT cookie
login/logout/expiry. SlowAPI 0.1.x (MIT) throttles the public login endpoint to
five attempts per client address per minute. The deployment exposes no
registration or account-management API: operators rotate the Argon2 credential
hash and high-entropy JWT secret through deployment configuration. Production
must also enable TLS-only cookie mode. No deployment is claimed here.
