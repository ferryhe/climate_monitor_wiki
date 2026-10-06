# Native Hermes T10 acquisition review and recovery

Run one native agent cron with `no_agent=false`, fresh context, no background
reviewer. Read-only pre-script:
`python scripts/review_pipeline.py peek --kind acquisition --root "$CLIMATE_ACQUISITION_RUN_DIR"`.
`wakeAgent=false` skips the model and ends silently. Claim the exact supplied run:
`python scripts/review_pipeline.py claim --kind acquisition --root "$CLIMATE_ACQUISITION_RUN_DIR" --target RUN`.
Use injected current session identity, exact token and immutable packet.

Review every source outcome, real artifact/attempt/tool provenance and candidate
body. Read full evidence with native `read_file`, following actual numbered-line
coverage and byte-budget next_offset. For candidates check complete body, title,
publisher/date evidence, faithful summary, climate AND actuarial/insurance
relevance, deduplication, coverage and source identity.

Submit external JSON with `items` keyed by acquisition_item_id, each containing
exact candidate_sha256, status (`pass`, `needs_correction`, `rejected`) and reason.
`sources` are keyed by configured source key with exact evidence_sha256, reason
and status (`passed_success`, `verified_no_new`, `needs_correction`, `recovering`,
`restricted`, `unresolved`). No-new requires real completed governed attempts
and verified full source artifact, not an empty list or exit zero. Failure and
access refusal stay visible. Submit via `review_pipeline.py submit --kind
acquisition --root "$CLIMATE_ACQUISITION_RUN_DIR" --target RUN --token TOKEN --result EXTERNAL_JSON`.

Use existing governed recovery only: `review_pipeline.py recover` with the same
root/run/token. It resumes the original frozen binding, completed evidence and
cumulative budget. Do not create another task, change credentials/prompts/skills/
model/budget or bypass access refusal. Review actual restored results later;
recovery success is not approval. Derived candidate corrections use `correct`
with result `{item_id,changes:{title/summary},reason}`; this revokes only that
candidate's PASS and requires the next independent context. Raw evidence stays.

After submission, `activate --kind acquisition --root "$CLIMATE_ACQUISITION_RUN_DIR"
--target RUN --queue-dir "$CLIMATE_INTAKE_QUEUE_DIR"`
queues only the exact current PASS subset to the existing writer. Partial
activation preserves other old active versions; unreviewed candidates stay private.

Proposals are records only, never automatic development tasks. Every proposal
requires kind (skill/tool/application_code), source_key, cause, reason, evidence,
verified_result, reproduction, future_version and actual successful tool_call_ids
whose results contain verified_result. Skill proposals additionally require
official_entry, steps, scope, before, after; tool proposals require owner,
affected_sources, suggested_change, validation; application code proposals
require root_cause, files, suggested_change, pr_title, pr_description, validation.
Reader/tool changes belong to web_listening; do not duplicate a crawler.
Temporary/no-new/restricted outcomes are not development proposals. Never modify
production skills/tools/code, commit/push/create a PR or merge. Respond only to
new results or actionable blockers; unchanged/empty work remains silent.

When a readable_text companion is supplied, read that exact frozen file. Its JSON
fragments concatenate losslessly to the original full text and avoid native
long-line clipping; all numbered lines still require actual complete coverage.
