# Native Hermes T10 acquisition review and recovery

Run one native agent cron with `no_agent=false`, fresh context, no background
reviewer. Read-only pre-script:
`python scripts/review_pipeline.py peek --kind acquisition --root "$CLIMATE_ACQUISITION_RUN_DIR"`.
`wakeAgent=false` skips the model and ends silently. Claim the exact supplied run:
`python scripts/review_pipeline.py claim --kind acquisition --root "$CLIMATE_ACQUISITION_RUN_DIR" --target RUN`.
Use injected current session identity, exact token and immutable packet.

Review every source outcome, real artifact/attempt/tool provenance and candidate
body. Read full evidence with native `read_file`, following actual numbered-line
coverage and byte-budget next_offset. For candidates check the available evidence, title, publisher/date basis, faithful
summary, climate AND actuarial/insurance relevance, deduplication, coverage and
source identity. Missing body or classification is not a reason to invent it.
Observed report/PDF summaries remain clearly attributed; unknown dates retain
their actual source/report date basis. A governed website success still requires
its existing complete capture and date eligibility contract.

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

## One Registry and exact automatic publication

Website rotation, weekly search and PDF intake all write pending candidates to
`CLIMATE_REGISTRY_DB`. T1 improves article/meeting facts; this existing T10 native
context reviews their exact evidence. There is no additional review service or
cron. T5 reviews generated report PDFs downstream, not intake PDFs.

When peek selects `target=registry-review`, claim and submit with the same
acquisition commands. Read each `snapshot_path` or supplied `snapshot_view.path`
in full. The snapshot includes exact body, enrichment, source identity, dates,
categories and metadata. Submit conclusions keyed by candidate SHA, each with
`candidate_sha256`, `status` and `reason`. The automatic final checks verify the
native session/read evidence and recheck the current candidate SHA under the
shared database lock before changing its public pointer. No generated receipt
can stand in for an actual native review.

Every item is independent. A changed or failed item stays pending while other
approved items can publish. An existing approved version remains readable during
later improvement. `is_visible=false` excludes that canonical identity across
all public consumers. The existing writer regenerates the same approved
projection after publication; historical sources and archived reports are fixed.
