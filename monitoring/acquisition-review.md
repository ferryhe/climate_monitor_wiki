# Native Hermes T10 acquisition review and recovery

Run one native agent cron with `no_agent=false`, fresh context, no background
reviewer. Read-only pre-script:
`python scripts/review_pipeline.py peek --kind acquisition --root "$CLIMATE_ACQUISITION_RUN_DIR"`.
Consume the returned `verification_report` JSON/English Markdown artifact paths,
including the automatically emitted inventory when no action is selected. The
complete inventory distinguishes unverified reasons and reader failures,
metadata-only report references, and older verified versions retained publicly.
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
categories and metadata. Read the packet's complete frozen `enrichment_contract`
alongside that snapshot: source/read evidence, full new body and summary,
each supported update note, dates and their precision, taxonomy bindings,
`content_comparison_baseline` and `approval_inheritance_baseline`. Check that
the candidate, body, summary, update note and date evidence describe the same
version. Keep unknown comparisons explicitly unknown. An explicit owner date
override retains its authority and reason; do not request source-date verification
or repeat owner approval for that date. New agent-authored summary/update notes
still need this independent full-content review.

Submit conclusions keyed by candidate SHA, each with `candidate_sha256`, `status`
and `reason`. For an enrichment scope other than `publication_date_only`, also
return the exact supplied `enrichment_contract_sha256` unchanged, confirming that
the complete frozen contract was reviewed. The existing `publication_date_only`
exception retains its narrow date scope and inherited approved content; it does
not require G7/G9 or a new source-fact review. The automatic final checks verify the
native session/read evidence and recheck the current candidate SHA under the
shared database lock before changing its public pointer. No generated receipt
can stand in for an actual native review.

Every item is independent. A changed or failed item stays pending while other
approved items can publish. An existing approved version remains readable during
later improvement. `is_visible=false` excludes that canonical identity across
all public consumers. The existing writer regenerates the same approved
projection after publication; historical sources and archived reports are fixed.

After submission, use the existing acquisition `activate` command with
`--target registry-review`. Its `approved_candidates` identifies only this
packet's successful current approvals; `public_revision` binds those results to
one exact Registry snapshot for G1/G5. A missing approval, old PASS or pending
candidate cannot authorize new public content. `downstream_sync_status=not_checked`
does not prove G5 completion. A display-refresh failure rolls back Registry
approval; after successful approval, retry downstream failures through the
existing writer without replaying approval. Recheck the current revision before
consuming the handoff if other publication work has since completed.


T10 approval does not substitute for effective source-bound T1 verification.
Only the exact verified published primary supplies public facts; unchecked sibling
observations keep metadata references only. Copying an original PDF Executive
Summary requires actual owner authorization and the exact original-adoption path,
with label `Adopted from the original PDF`. It is not independent fact verification or a native PASS.
See the existing deployment runbook for schema 23 and adoption commands.
