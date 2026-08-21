# Weekly Climate Monitor 08h

This directory contains repository-owned management artifacts for the Monday
08:00 UTC Weekly Climate & Actuarial Monitor.

The active runtime remains Hermes-owned. Files here are prompt, contract,
driver, documentation, and provenance-capture references for a future web
backend management project. They are not a live scheduler config.

Key files:

- `manifest.json` describes the repo-owned artifact set.
- `prompts/weekly-monitor-v1.prompt.md` is the exact captured production
  prompt body.
- `prompts/weekly-monitor-v1.meta.json` pins prompt identity and SHA-256.
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
