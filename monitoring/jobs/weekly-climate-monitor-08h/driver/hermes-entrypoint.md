# Hermes Entrypoint Notes

Hermes remains the live scheduler and runtime owner. This file documents how a
separately authorized Hermes command may call the repository-owned strict
driver from an exact deployed commit.

Repository CLI shape:

```bash
python scripts/run_climate_monitor.py \
  --production-weekly \
  --authoring-response authoring-response.json \
  --date YYYY-MM-DD \
  --json
```

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
