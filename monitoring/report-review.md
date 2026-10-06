# Native Hermes T5 report review

Run as a native agent cron with `no_agent=false`, fresh context, no background
reviewer and no conversation carryover. Its read-only pre-script is
`python scripts/review_pipeline.py peek --kind report --root "$CLIMATE_REPORT_REVIEW_DIR"`.
An empty packet (`wakeAgent=false`) ends silently before model startup.

For the supplied exact target, use local terminal to claim:
`python scripts/review_pipeline.py claim --kind report --root "$CLIMATE_REPORT_REVIEW_DIR" --target DATE`.
Use the injected native session ID; never guess another session or use the latest
DB row. Save the returned token. Resolve and read the packet source, frozen
snapshot, previous source if present, and complete saved PDF full-text file.
Use native `read_file` with its real 1-based offset/limit. Follow actual
`next_offset` for byte truncation and cover every line without truncated lines.
Read every listed PNG using `vision_analyze(image_url=EXACT_LOCAL_PATH)`.
Image presence, extracted text alone, shell echoes and checked=true do not prove
review. Durable matching tool calls and successful responses are required.

Check the actual PDF text and all pages: period and selection, original dates,
late carryforward/coverage gaps, evidence/citations, section completeness,
garbled text/placeholders, tool logs/prompts, duplication/truncation/overflow,
headers/footers/page numbering. Compare with the frozen source and snapshot.
Write external result JSON with `status` and concrete `reason`, then submit:
`python scripts/review_pipeline.py submit --kind report --root "$CLIMATE_REPORT_REVIEW_DIR" --target DATE --token TOKEN --result EXTERNAL_JSON`.

Use `pass` only after all actual inspections succeed. `changes_requested` supports
`changes.title`, `changes.executive_summary` (text list), and `changes.updates`
(mapping frozen ordinal to title/paragraphs). Keep evidence/citations/identity
unchanged. Submission creates a new immutable revision; stop this context. The
next independent cron must read and inspect the new actual PDF again.

For a code/template defect that blocks this edition use `blocked_code_change`
with `proposal` containing reason, pdf_evidence, root_cause, files,
suggested_change, pr_title, pr_description and validation. Cite inspected page
and text evidence. Never change application/template code or source evidence,
commit/push/create a PR, or send mail. `review_failed` records actual tool or
inspection failure. Retain concrete progress/reason; never default to PASS.
Only new results or actionable blockers warrant a user-facing response; empty
or unchanged queues remain silent.

When a readable_text companion is supplied, read that exact frozen file. Its JSON
fragments concatenate losslessly to the original full text and avoid native
long-line clipping; all numbered lines still require actual complete coverage.
