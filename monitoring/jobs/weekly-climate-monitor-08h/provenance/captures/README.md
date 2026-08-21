# Redacted Capture Provenance

This directory is reserved for redacted provenance captures that document what
was observed during controlled parity or cutover work.

`hermes-job-f5259a8ec2d9.redacted.json` records sanitized metadata from the
externally verified live Hermes cron-job JSON: public job identity, UTC cron
expression, file size/hash/counts, safe runtime shape, and prompt
hashes/lengths. The capture file does not embed the prompt body; the exact
captured prompt text is versioned separately at
`../../prompts/weekly-monitor-v1.prompt.md`.

The raw `job-08h-monitor.json` was provided as a local attachment for
inspection, but it is intentionally not committed. This remains separate from
the earlier `monitor-08h-package.tar.gz` omission: that tarball's README
claimed `job-08h-monitor.json` was included while the tarball did not contain
it.

No raw production capture is committed by this repository change. Do not add
raw live Hermes payloads, credentials, host paths, origin or delivery
identifiers, unredacted logs, raw job JSON, or invented snapshots. The only
committed prompt body should be the reviewed prompt artifact under
`prompts/`. Redacted snapshots, when separately authorized and real, should
use:

```text
snapshots/<capture-id>.snapshot.redacted.json
```

Files here are provenance only. They are not active executable config, and this
directory must not contain `job-08h-monitor.json`.
