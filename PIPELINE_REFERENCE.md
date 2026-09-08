# Climate Monitor Pipeline — Reference

The program and artifact map for the modular weekly pipeline. See
[README.md](README.md#new-flow) for the flowchart and
[PIPELINE_CONFIG.md](PIPELINE_CONFIG.md) for editable prompts and scheduling.

## Implementation and deployment status

As of the 2026-09-08 SSH audit, the same-run prepare → serial URL authoring →
executive summary → finalize path is implemented and sandbox-tested. Production
still has 12 enabled legacy Step jobs. The four-slot wrappers are a deployment
target, not a live inventory. Issue #87 is owner-closed; historical handoffs that
say it must stay open do not override that decision.

The library entry `weekly_monitor.driver.run_weekly_monitor` validates a completed
response and delegates to the orchestrator. The production CLI owns preparation
and the serial model queue before invoking that library. These are layers of one
path, not competing report generators.

## Program and artifact map

| Stage | Owner / public entry | Input → output |
|---|---|---|
| Pillar A acquisition | External `web_listening` batch/export APIs | Configured sites → matching `acquisition-batch-result.v2` and `web-listening-manifest.v1` |
| Pillar B discovery | Hermes `web_search` / `web_extract`; `prompt_loader.load_pillar_b_search_prompt` | Explicit report date + editable template → candidate JSON array |
| Prepare | `scripts/run_climate_monitor.py --production-weekly --authoring-mode prepare` | Outcome + manifest + Pillar B → `bundle.json`, `combined.json`, `candidate_item_snapshot.json`, `article_evidence.json`, `stats.json`, `v2_authoring_request.json` in staging |
| Identity/merge | `article_candidate_contract.py`, `candidate_aggregation.py`, `dedupe.py` | URL-bearing records → one canonical identity, all origins, source metadata |
| Evidence | `article_content_adapter.build_article_evidence_artifact` | Public governed upstream result → content/ref, attempts, fetch status, hash and explicit snippet/none fallback |
| Optional title extraction | `article_title.extract_page_title` | Verified HTML → main/article H1, other H1, Open Graph or HTML title, preserving case |
| URL authoring | Existing CLI `--authoring-mode run` | One URL's frozen evidence → two booleans, summary, basis, evidence hash, categories, keywords; validated checkpoint in `url_authoring/` |
| Executive authoring | Same CLI after every URL completes | Qualified summaries → `executive_authoring.json`; no invocation for an empty qualified set |
| Finalize | Same CLI `--authoring-mode finalize`; `weekly_monitor.driver`, `orchestrator` | Prepared bundle + validated v2 response → report Markdown, semantic sidecar, candidates/evidence, URL history transaction |
| Delivery | `python -m climate_delivery.cli run` | Validated report → briefing, content-addressed PDF/manifest and retained email |
| Publication | `publish_weekly_reports.py`, `weekly_wiki_refresh.sh` | Unpublished reports → isolated clone, regenerated `sources/`/`wiki/`, rolling content PR |
| Deployment | `reload_and_smoke_test.py` and controlled deployment runbook | Reviewed/merged content and code → deployed corpus and verified API |
| Registry | `weekly_registry_refresh.py`, `climate_registry.weekly` | Exact deployed report + delivery identity → candidate sync, coverage checks, backup/promotion |
| Web / retrieval | `api_server.py`, `agentic_wiki/`, `showcase/` | Published corpus + Registry → historical reports, article details, cited chat |

Module paths without a directory prefix are under `climate_monitor/`. Upstream
reader policy belongs to `web_listening`; climate does not implement a second
HTTP/browser/stealth crawler. A policy refusal remains a terminal acquisition
outcome. Available snippets are explicitly labelled, and a URL with no evidence
cannot acquire an invented summary.

HTML is retained with its original content hash. The authoring view uses the
complete `web_listening.blocks.normalizer.normalize_html` Markdown result;
there is no arbitrary text cutoff. The offline title helper neither fetches nor
calls a model. `--no-page-titles` disables it on a fresh prepare.

## Run and resume

Provision the upstream artifacts and complete Pillar B search first. The monitor
consumes those files; `--print-pillar-b-prompt` only renders a task for Hermes.
Use one explicit Monday date and absolute external paths throughout a run:

```bash
python scripts/run_climate_monitor.py --production-weekly \
  --authoring-mode run --report-date "$REPORT_DATE" \
  --acquisition-batch "$CLIMATE_OUTCOME_ARTIFACT" \
  --web-listening-manifest "$CLIMATE_MANIFEST_ARTIFACT" \
  --pillar-b-artifact "$CLIMATE_PILLAR_B_ARTIFACT" \
  --staging-dir "$CLIMATE_STAGING_DIR" \
  --state-dir "$CLIMATE_STATE_DIR" --source-dir "$CLIMATE_SOURCE_DIR" \
  --wiki-dir "$CLIMATE_WIKI_DIR" \
  --source-config "$CLIMATE_SOURCE_CONFIG" --run-config "$CLIMATE_RUN_CONFIG" \
  --site-scopes "$CLIMATE_SITE_SCOPES" \
  --model "$MODEL" --model-provider "$MODEL_PROVIDER" --json
```

This is an executing command, not preflight. Sandbox runs must supply isolated
output/state paths. `--no-sync --no-update-seen-state` suppress wiki sync and URL
history promotion for monitor-only rehearsals; they do not disable network/model
calls. Explicit fixture runs require the dry-run controls below.

- Each URL has a fresh Hermes context, limited to its own evidence. Relevance
  rules are included in the same invocation as summary/categories/keywords.
- The application computes climate AND actuarial/insurance relevance. A false
  decision has empty summary fields. Finalize does not reapply the old keyword
  filter to validated v2 decisions.
- Each result is validated and atomically checkpointed. A failed URL is recorded
  and the queue continues; any unfinished URL blocks executive authoring and
  finalization. There is no article-count cap.
- Resume with the same command/staging. Completed results are revalidated and
  reused. Inputs, model/provider, prompt, taxonomy, title policy and destinations
  must still match; changed inputs require fresh staging.
- Prepare and finalize share history filtering and same-date carry-forward.
  Staging binds the effective candidate URL set and history destination; a
  history change that alters selection stops resume before model work. A
  completed run can replay its own same-date candidates, including rejected
  articles, without another model invocation. Pending report/history commits
  recover through the existing transaction before normal selection resumes.
- Executive authoring has its own checkpoint. Finalize reuses the prepared
  evidence and verifies date, URL set and hashes without recrawling.
  The executive response must be prose paragraphs; lists/headings are rejected
  before checkpoint completion so delivery cannot mistake findings for legacy
  monitoring bullets. Resume then repeats only executive authoring.
- Progress is stderr; `--json` stdout is one final result. Hermes runtime retry
  limitations and stdin compatibility are documented in PIPELINE_CONFIG.md.

## Identity, dates and report format

Pillar A/B are discovery origins, not separate namespaces. `source` is an
institution/website name; `pillar` carries A or B. Canonical URL merging keeps all
origins. Titles are never deduplication keys. URL history commits only with the
validated final report bundle; interrupted work cannot mark unfinished articles
as processed.

Site counts come from the upstream outcome, not the number of articles or the
configured source list. `total = updated + unchanged + blocked + failed +
unresolved`. The WRI sandbox input had **one** requested site and **one** unchanged
site; 164 discovery rows do not mean 164 checked sites. Never copy fixture counts
such as 57/42/15 into a live report.

Pillar B's prompt uses the three-calendar-month window ending on the explicit
report date. Its existing consumer still accepts only `title`, `url`, `source`,
`summary`; the discovery timestamp is not a publication date. The missing
machine-checked date/search-success contract remains a production cutover gate.

Markdown retains the existing weekly contract: report identity and monitoring
counts, executive narrative, Pillar A/B sections grouped by primary category,
article titles, ordered categories, summaries, keywords and original links.
The semantic sidecar binds structured metadata to that report. Delivery reuses
the narrative executive summary; old bullet-only reports retain their existing
extractive fallback. Taxonomy is owned by
`monitoring/taxonomies/article_categories_v1.yaml`, not a duplicate label list in
a cron prompt. The legacy `daily` document type and library cadence default remain
load-bearing; weekly operation is explicit.

## Legacy scripts and cleanup

`step1_pillar_a.py`, `step2_save_state.py`, `step3_aggregate.py`, the Step 3b/filter
scripts, `step5_build_md.py`, `step6_render_pdf.py`,
`step7b_extract_conferences.py`, `step8_sync_registry.py` and
`step9_update_website.py` remain compatibility/rollback entrypoints. The last SSH
inventory still found legacy jobs enabled, so they cannot yet be described as
unused or deleted safely.

After the unique new schedule is verified, disable old scheduler callers, check
remaining imports/tests and remove obsolete code/configuration made redundant by
the replacement. Keep historical reports and required compatibility contracts.
Do not add a parallel driver, copied reader stack or duplicate prompt definition.
Superseded issue handoffs, closeout audits and completed migration plans are
available in Git history. Keep current instructions in these module/runbook
documents rather than retaining another active-looking copy.

## Verification and cutover

Evidence as of 2026-09-08:

- Reviewed runtime implementation before documentation pruning, with the pinned
  upstream installed: full SSH
  sandbox pytest **1769 passed / 5 skipped**, with three existing warnings.
  Compilation, shell/JavaScript syntax and whitespace checks passed. Independent
  code and Markdown reviews passed after history/resume and executive-delivery
  regressions were fixed.
- After removing four unused Markdown files and their obsolete text-only test,
  a separate clean environment verified `web_listening` was absent: full suite
  **1695 passed / 78 skipped**, with three existing warnings. Dependency
  consistency passed. Environment-dependent skips are not live acquisition proof.
- The single-site WRI-derived input exercised 163 unique URLs and produced a
  22-article PDF preview. It did not cover the full configured site list; its
  older Pillar B input was reused, so it is not a freshness proof.
- Fresh Hermes Pillar B search completed all four required queries and returned
  four candidates dated June 8, June 9, June 30 and July 9 for the June 7–September
  7 window. Manual review retained three; the IAIS market article only briefly
  mentioned climate. This was a discovery test, not a new whole-report run.
- A first search run failed because the sandbox dependency cache was read-only,
  yet wrote `[]` with exit 0. A writable sandbox cache fixed tool execution. The
  prompt now distinguishes tool failure from zero results; deterministic success
  and date validation are still required.

Before production cutover: complete those input gates; pin both repos and a
compatible Hermes runtime; verify the monitor run-ledger producer required by
email; run all configured sites with real Pillar B discovery;
validate monitor → delivery dry-run → publisher no-push → Registry dry-run with
the same report identity. Then merge/deploy the reviewed code, switch to the unique
four-slot schedule, read back each command/timezone and observe a normal weekly
cycle. Only then retire old jobs and temporary worktrees, retaining a rollback
point. A passing fixture, owner-closed issue or scheduled reminder is not evidence
that this sequence has completed.

## Hermes job wrappers (AC-1/3/10)

The four `scripts/hermes_job_*.sh` wrappers resolve an absolute repository and
interpreter and invoke `scripts/hermes_job.py` from any working directory.
`--preflight` validates paths and contracts without dispatch or writes. All
runtime paths must be explicit, absolute and already provisioned. No wrapper
reads `.env`. Child output is suppressed to keep recipients and SMTP errors
out of scheduler logs.

The monitor requires `CLIMATE_OUTCOME_ARTIFACT`, `CLIMATE_MANIFEST_ARTIFACT`,
`CLIMATE_PILLAR_B_ARTIFACT` and `CLIMATE_STAGING_DIR`. The wrapper invokes the
existing CLI with `--authoring-mode run`: prepare, serial URL authoring,
executive authoring and finalize. It resolves model/provider through the existing
monitor configuration. Operators do not assemble unrelated response/evidence/stats
files. The previous `live_acquisition_contract_unavailable` placeholder has been
replaced by the executable same-run path; invalid or mismatched inputs still fail.

Fixtures require both `CLIMATE_DRY_RUN=1` and `CLIMATE_DRY_RUN_FIXTURE_DIR`.
Dry-run output/state paths must be within an explicit existing
`CLIMATE_DRY_RUN_ROOT` under `/tmp`. There is no automatic fixture fallback.
The monitor additionally requires existing `CLIMATE_STATE_DIR`,
`CLIMATE_SOURCE_DIR` and `CLIMATE_WIKI_DIR`, plus absolute existing
`CLIMATE_SOURCE_CONFIG`, `CLIMATE_RUN_CONFIG` (weekly report title), and
`CLIMATE_SITE_SCOPES` configuration files. Library daily defaults are unchanged.

Email uses `python -m climate_delivery.cli run --report PATH --output-dir PATH
--state-dir PATH --config PATH`, with absolute paths from `CLIMATE_REPORT_PATH`,
`CLIMATE_DELIVERY_OUTPUT_DIR`, `CLIMATE_DELIVERY_STATE_DIR` and
`CLIMATE_DELIVERY_CONFIG`. A completed same-date monitor slot and latest ledger
report SHA are required. `load_delivery_config` supplies exactly four recipients
and resolves required SMTP settings. The delivery pipeline validates the sidecar,
generates and validates its content-addressed PDF before SMTP dispatch.
The monitor CLI returns report/provenance data but does not append that monitor
ledger attempt itself. The runtime producer integration must be connected and
verified in the same-run rehearsal; a successful monitor scheduler snapshot alone
cannot satisfy the email gate.

Publisher requires `CLIMATE_REPORTS_DIR`, `CLIMATE_RUN_LEDGER_DIR`, and, for
production, `CLIMATE_PUBLISH_LOCK`. It validates the selected report before
invoking the absolute `scripts/publish_weekly_reports.py`. Its dry-run is the
existing pending-report validator's no-push plan, not publication.

Registry requires `CLIMATE_REGISTRY_ENABLE=1` and
`CLIMATE_HUMAN_MERGE_DEPLOY_VERIFIED=1`, plus `CLIMATE_EXPECTED_REPORT_SHA256`,
`CLIMATE_SOURCE_DIR`, `CLIMATE_REGISTRY_DB`, `CLIMATE_DELIVERY_OUTPUT_DIR`,
`CLIMATE_REGISTRY_BACKUP_DIR`, `CLIMATE_REGISTRY_LOCK`, and
`CLIMATE_RUN_LEDGER_DIR`. It invokes `scripts/weekly_registry_refresh.py`;
`CLIMATE_DRY_RUN=1` passes `--dry-run` and stops before capture, promotion or reload.
An eligible current-week dry-run records `registry_dry_run_exit_N` only in its
isolated status directory, with `not_dispatched`; pre-slot/historical rehearsals
return the code in stdout without falsifying a scheduled occurrence.
Production additionally requires `CLIMATE_REGISTRY_WRITE_ENABLE=1`, `API_BASE_URL`
and `SITE_HOST`. The existing runner verifies deployed corpus, publisher ledger,
artifact and DB identity. A blocked or dry-run Registry is never full completion.

All wrappers require `REPORT_DATE` (Monday, UTC semantics) and an external
`CLIMATE_JOB_STATUS_DIR`. These snapshots are local-only evidence. Render has
no shared source and `/api/job-status` remains 503 `not_configured`; see
[the status contract](docs/job-status.md). The 2026-09-08 SSH audit found 12 enabled legacy Step jobs on the real server;
the four-slot target was not installed. The intended schedule is not a provisioning claim.

Hermes local timezone is Asia/Shanghai: Monday 08/09/10/10:30 UTC maps to
16/17/18/18:30 CST (`0 16 * * 1`, `0 17 * * 1`, `0 18 * * 1`, `30 18 * * 1`).
Read back each job's timezone and command before scheduling. Do not change the
global timezone. Local 08:23 CST is 00:23 UTC, before the monitor window.
