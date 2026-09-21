# Managed Hermes defaults: controlled validation

Managed tasks do not select a provider or model. The first acquisition turn uses
the executing user's ordinary `HERMES_HOME/config.yaml`, `.env`, authentication
state, and credential environment. The run then records the successful
provider/model route from Hermes' own session usage database and resumes that
same session for later acquisition turns. Article authoring, executive authoring,
report handoff, and meeting extraction reuse the run-private `HERMES_HOME`.
The observed route remains sanitized evidence and a continuity check; it is not
converted back into a provider/model command-line override.

The repository test environment does not contain the pinned real Hermes binary.
Before production use, validate commit `5538bd1` in the controlled scheduler
environment:

1. Configure the ordinary Hermes chat default without task-level CLI overrides.
2. Start a new managed run and confirm the first `hermes chat` command contains
   neither `--provider` nor `--model`.
3. Confirm `hermes-effective-identity.json` contains the actual successful route
   and session ID from the latest `session_model_usage.last_seen` row for the
   bound session; it must contain no credential.
4. Trigger feedback or resume and confirm it uses `--resume` with that session.
5. Change the ambient Hermes default, then confirm the existing run retains its
   frozen run-local configuration and observed identity while a new run uses the
   new default.
6. Exercise report handoff, article and executive authoring, plus automatic and
   manual meeting extraction. Confirm each process uses the same run-private
   `HERMES_HOME`, each Hermes command omits provider/model flags, and every
   recorded provider/model matches the observed run evidence.
7. Remove or invalidate the ordinary Hermes default and confirm the managed run
   reports a default-configuration error, distinct from an acquisition/content
   failure.

Mocked session databases prove the repository contract only; they are not
evidence that the real provider route was observed.
