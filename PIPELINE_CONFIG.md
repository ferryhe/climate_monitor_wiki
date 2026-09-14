# Pipeline Configuration

The repository defines a four-slot biweekly ET Hermes sequence anchored to the
single production driver path. This is the intended deployment, not proof
that the server has switched to it. The 2026-09-08 SSH audit still found
12 enabled legacy Step jobs and no installed four-slot sequence. Keep that
deployment distinction until the live chain passes and the scheduler is switched.

## Biweekly ET schedule (anchor: September 14, 2026)

| # | ET | Slot        | Hermes wrapper                            | Entry point invoked                                                  | Result                                                  |
|---|-----|-------------|--------------------------------------------|----------------------------------------------------------------------|---------------------------------------------------------|
| 1 | 08  | `monitor`   | `scripts/hermes_job_monitor.sh`           | `python scripts/hermes_job.py monitor --managed --scheduled`         | Monday report Markdown + sidecar + URL-state commit    |
| 2 | 09  | `email`     | `scripts/hermes_job_email.sh`             | `python -m climate_delivery.cli run`    | PDF + manifest + retained email to the four recipients  |
| 3 | 10  | `publisher` | `scripts/hermes_job_publisher.sh`         | `flock` + `python scripts/publish_weekly_reports.py`                   | Rolling `codex/hermes-weekly-monitor` PR update         |
| 4 | 10:30 | `registry` | `scripts/hermes_job_registry.sh`          | `scripts/weekly_registry_refresh.py` (explicit gates)                                                        | `not_dispatched` until merge + deploy gate is satisfied |

Each dispatched production wrapper writes local-only `scheduler-status.json` via `climate_monitor/scheduler_status.py
update_slot(name, state, …)`. The publisher slot is 2h after monitor so the
report exists before ingest; preserve that gap if you ever re-schedule.
`weekly_wiki_refresh.sh` remains a compatible direct Publisher wrapper; the
scheduled slot delegates directly to the same Python publisher under its lock.

Use the DST-aware UTC ticker checks in [the ET deployment runbook](docs/biweekly-et-deployment.md).
The shared guard rejects the wrong offset and alternate Mondays.
The manifest records this mapping; it is not an installed job inventory.
The 2026-09-08 server audit found no four-slot scheduler snapshot;
`/api/job-status` returned 503 `not_configured`.

Every wrapper supports read-only `--preflight`. Explicit paths, dry-run isolation,
and the same-run input and remaining cutover gates are documented in
[PIPELINE_REFERENCE.md](PIPELINE_REFERENCE.md#hermes-job-wrappers-ac-1310).
Fixtures require `CLIMATE_DRY_RUN=1` plus `CLIMATE_DRY_RUN_FIXTURE_DIR` and isolated
`CLIMATE_DRY_RUN_ROOT`; production never falls back to a fixture.

## Configuration ownership

Use the [flowchart](README.md#new-flow) and
[program/artifact map](PIPELINE_REFERENCE.md#program-and-artifact-map) for the
single monitor path. `scripts/hermes_job.py` only resolves runtime paths and
delegates to the existing public entrypoints.

| Configuration | Owner / edit location |
|---|---|
| Acquisition task parameters and five business prompts | The authenticated `/manage` API and one `climate-acquisition-task-state.v1` at `CLIMATE_TASK_CONFIG` |
| Source inventory and reviewed site scopes | Existing `monitoring/supranational_sources.yaml` and `monitoring/site_scopes.yaml`, referenced by configured source keys |
| Categories and semantic limits | Structured/versioned `monitoring/taxonomies/article_categories_v1.yaml`, whose normalized hash is bound to each effective task |
| Model/provider | Effective task parameters; GPT-5.6 Luna default with credentials outside the repository |
| Runtime Registry and run paths | Effective task parameters, placed on external persistent storage by the operator |
| Live schedule | Hermes on the controlled server; `scripts/run_agent_acquisition.py --scheduled-start` consumes the same task definition as manual start |

Do not duplicate these definitions in new workflow scripts or cron prompt text.
The old numeric Step callers remain until the verified scheduler cutover.

## Prompt Templates

The one versioned state exposes exactly `acquisition_task`, `search_guidance`,
`relevance`, `article_summary`, and `executive_summary`. The tracked prompt files
seed the explicit bootstrap marker only. After the first authenticated save,
`prompt_loader.py`, the acquisition launcher, and serial report authoring all
consume the saved state. Explicit `path=` loader arguments remain test/legacy
compatibility overrides and never silently replace a bound run.

### Monitor (v2 evidence authoring)

Per-URL relevance rules live in
`monitoring/jobs/weekly-climate-monitor-08h/prompts/article-relevance-v1.prompt.md`.
The driver embeds them in the same request that produces the summary, categories
and keywords. An excluded URL returns empty summary fields; there is no separate
classifier request. Rules are included in each checkpoint's input hash. Use fresh
staging after changing rules, so completed results are not reused under new rules.

Page titles are extracted offline from the verified HTML during prepare by
`climate_monitor.article_title.extract_page_title`: article/main H1, other visible
H1, Open Graph title, then HTML title. Original capitalization is preserved. When
none exists, retain the discovery title (or the honest URL fallback). The adapter
records the title source and original body hash without changing discovery origins.
Finalize uses the immutable request title; the model cannot replace it.

The helper is optional: pass `--no-page-titles` to the existing monitor to disable
it on a fresh prepare/run. Library callers can pass a pure `title_extractor` to
`build_article_evidence_artifact(..., include_verified_content=True)` or omit it.
For standalone use: `python -m climate_monitor.article_title saved-page.html`.
It neither fetches URLs nor invokes a model.

The monitor's `hermes` on `PATH` must support `chat --query-file -` for UTF-8
stdin. Hermes `v2026.9.7` (`2237be355906fbe6065ce1815711eee52b2d646e`)
has been checked with a 9.6 MB stdin payload in the server sandbox. The older
`03fa32c` runtime does not support this channel: `--query -` submits a literal
dash and leaves stdin unread. The monitor rejects that runtime before authoring;
it never puts the full evidence in argv. Validate the selected runtime in isolation
before changing the production job's environment.

HTML evidence is retained unchanged and checked against its source content hash.
Current governed URL retrieval selects the upstream Markdown derivative and
verifies its bytes through `RuntimeService.open_owned_artifact`. The evidence
record retains the original source artifact, selected derivative, resolution,
attempts and hashes; it does not impose a text cutoff. Historical retained HTML
is passed through byte-identically. Each article invocation receives only one
URL's evidence.

`--authoring-mode run` prepares once, then processes URLs serially. Each URL
returns two relevance decisions plus summary, summary basis, evidence hash,
categories and keywords. The application computes climate AND actuarial/insurance
relevance and validates each result before atomically saving it in
`STAGING/url_authoring/`. The existing v2 validator and final response shape remain
authoritative; immutable article identities and provenance are bound by code.
Finalization uses the validated v2 model relevance decision without applying the
legacy keyword filter again. Every qualifying article is rendered; there is no
report article cap. The executive summary covers all qualifying article summaries.
PDF delivery reuses the monitor's narrative executive summary. Older reports
with monitoring bullets only retain the existing deterministic summary fallback.

Rerun the same command with the same staging directory to resume. Completed URL
results are revalidated and reused; failed or interrupted URLs are attempted
again. Prepare does not reacquire evidence on resume.
Finalize validates and reuses that same prepared evidence artifact (date, URL
set, record hashes and content hashes); it does not crawl the articles again.
With `--json`, stdout contains one final result object. Per-URL progress goes to
stderr, so wrappers can parse the result without mixing it with progress lines.
For a parse or validation failure, the fresh request includes only that item's
previous validation error alongside its original task and evidence. The base
input identity stays pinned, and the exact attempt request has its own SHA-256.
Changed source files, report date, model/provider, prompt, taxonomy or output locations require fresh
staging. A failed URL does not prevent the remaining URLs from running, but any
unfinished URL prevents executive authoring and finalization.

After every URL completes, a separate call receives only the verified summaries
of relevant articles and produces the executive summary. With no qualifying
summaries, it remains empty and no summary call is made. Its result is checkpointed
too, so summary failure does not rerun articles. The final v2 response, report and
seen-state transaction are committed only through the existing finalize path.

Each invocation uses `--max-turns 1 --reasoning none --ignore-rules` and
`HERMES_STREAM_RETRIES=0`; its dedicated Hermes configuration should use
`agent.api_max_retries: 1`. These controls are **not proof of one API call**:
the pinned runtime still requests a final summary after a partial stream exhausts
the iteration budget. An isolated runtime reproduction made three API calls.
The application bounds each invocation with `--authoring-timeout` (default 180
seconds) and records failure per URL. This is N independent URL invocations plus
one summary invocation, not a whole-week single model call. Raw attempts are
retained beside checkpoints. Only one complete JSON code fence is accepted as
response framing; malformed JSON, extra prose and invalid semantics still fail.
Production cutover requires a complete real run and verified runtime behavior.
Hermes must be able to renew credentials through its normal locked auth store.
A read-only auth mount can run until the current token expires, then fail while
saving renewal state; this occurred during the server sandbox test. Keep auth
renewal in Hermes rather than adding a second mechanism to the monitor driver.

The 08:00 CLI prepares frozen `article_evidence.json` and `stats.json` in its
staging directory before URL authoring. Its v2 request binds the response to
that retained evidence and the deterministic
stats dict `{"total": N, "updated": …, "unchanged": …, "blocked": …,
"failed": …, "unresolved": …}` (N must equal `updated + unchanged +
blocked + failed + unresolved`). The driver validates the mapping before
the orchestrator writes any artifact; `MonitorRunResult.stats` exposes
the validated counts. The total is the actual upstream site count; the
single-site WRI sandbox showed `1 requested / 1 unchanged`, not 57 sites.

### Email (09:00 ET, climate_delivery pipeline)

Use the email wrapper with four absolute paths and a verified monitor identity.
Configuration and preflight are documented in PIPELINE_REFERENCE.md.

### Current agent-guided acquisition and Pillar B v3

The acquisition-task component directs Hermes to choose native search only in
response to observed coverage gaps. The `search_guidance` component renders the
bound unlimited/recent/custom publication-date policy. Under
`trusted-candidate-handles.v3` lets Hermes select from public search candidates
and call the attempt-scoped stage/finalize tools. The runner persists the real
search events and governed article-read receipts, applies bound date eligibility,
and assembles the existing #112 Registry batch; model prose never supplies those
facts. New task definitions bind `acquisition-task-v2`; the v1 component remains
unchanged for explicit legacy/v2 frozen runs. A failed search is not a zero-result
success, and zero candidates is not zero searches. Explicit v2 and legacy runs
keep their frozen behavior and cannot resume as v3.

Each start writes an immutable task version, effective/component hashes, exact
batch, resolved report date/range, budgets and checkpoint paths. Resume creates
a new immutable attempt file from that original binding; a later config save
cannot alter it. Completion status requires Registry readback plus byte-equivalent
`freeze_acquisition_for_report` output, not agent prose. The current launcher is
implemented and tested but has not been installed in production.

### Managed acquisition capacity and incomplete coverage

New tasks use the provider-native unbounded search policy: there is no
application limit on search calls, cumulative results, results per call or
tokens. Provider schema validation still applies. Hermes chooses searches
adaptively; the application retains actual calls and results as evidence, never
as a pass/fail threshold. The legacy `search_attempts` and `search_results`
definition fields remain only for schema and frozen-run compatibility and are
not presented as active v3 controls. A binding with no `agent_protocol` is an
exact legacy bounded run, and resume rejects any legacy/v2/v3 protocol change.

Controlled acquisition still defaults to 5,000 fetch units, two retries per
item and 3,600 cumulative seconds. That finite capacity covers the frozen 116
seed inventory and substantial article follow-up, but it is not a promise of
content or of fitting an arbitrary number of candidates or redirect hops.
Explicit lower overrides remain authoritative. Each new run freezes those
limits, its protocol, source/scope inventory and governed HTTP identity.

A durable run ledger reserves capacity before every governed target send,
including redirects, and before each Hermes `web_extract` and `browser_exec`
dispatch. Failed or interrupted controlled-fetch reservations remain spent or
uncertain. `web_search` admission/completion is also recorded durably, but v2
does not reserve or enforce search/result capacity. Target-send reservations,
native fetch-tool units, source outcomes, policy refusals and precheck blocks are
distinct evidence. Resumes verify successful seed receipts and reuse them,
charging only remaining controlled operations under the same cumulative limits;
they retain all completed searches as evidence. A missing or changed ledger
fails closed. Successful seed receipts are independent of report checkpoint
promotion.

Only the acquisition subprocess receives the mandatory Hermes shell hooks in
its isolated attempt home. Its Python runtime must load and execute the public
hook contract before acquisition starts; incompatible configuration stops the
attempt. Global hooks, plugins and MCP configuration are not inherited. Provider
environment and an optional private copy of OAuth credentials supply identity.

The one installed distribution is `web-listening` 0.1.0 from
`web_listening_new` revision `ac2343f89bc7939736d85f049ebe2beac571034a`.
Site acquisition uses public explore/refresh requests; selected-URL retrieval
uses the public targeted `Request`/`RuntimeService.retrieve` operation. Article
retrieval uses the prevalidated candidate origin and exact canonical path and
records HTML-navigation and cross-path/cross-origin redirect gaps. Both open the same
persistent Runtime root. Requests allow only their frozen origins and paths and
carry finite request, byte, time and tool-attempt budgets. The Runtime alone
selects installed, enabled, healthy, qualified tools and records every attempt,
policy rejection and exclusion. Climate reserves the maximum possible network
requests before dispatch and reconciles only from returned measured usage;
unknown failures retain their reservation. HTTP evidence is never labelled as
browser execution. No private upstream patch or alternate crawler is used.

The bridge exports one disposition and, when any seed succeeds, one manifest
for each configured source. All individual seed outcomes, attempts and warnings
remain inside that source evidence; partial seed coverage therefore stays
partial even when its usable source export is updated or unchanged. When two
same-source discoveries name the same canonical URL and verified body, the
later discovery is retained as an unselected reuse origin without another
fetch. A rejected unknown candidate handle remains an auditable rejected call
and does not discard independently verified candidate receipts.

An attempt can finish as `completed_with_gaps`. All selected source outcomes and
artifacts survive Registry payload verification, report-input projection and
management readback. If at least one eligible exact content version was selected,
the partial projection is frozen immutably and authoring continues with the
English coverage limitations retained in the report, sidecar and delivery
artifacts. A selected-content integrity failure still blocks that content and
the report. Zero selected eligible records completes as
`no_eligible_information` without creating a report; a systemic reader stop is
`systemic_failure` and remains retryable. A truthful `no_search` retains its
reason. Local checks do not verify a production deployment, import, site run,
browser capability or full production coverage.

### Legacy Step 2: Pillar B Web Search (historical compatibility only)

Edit the query list, source preferences and selection wording in
`monitoring/jobs/weekly-climate-monitor-08h/prompts/pillar-b-search-v1.prompt.md`.
Keep its `${report_date}`, `${window_start}`, `${search_start}`, `${search_end}`
and `${output_path_json}` placeholders. Literal dollar signs use `$$`.
The renderer computes three calendar months from the explicit report date;
it does not use the machine's current date or a copied year.

View the exact task through the existing driver (no search or writes):

```bash
python scripts/run_climate_monitor.py --print-pillar-b-prompt \
  --report-date "$REPORT_DATE" --pillar-b-artifact "$CLIMATE_PILLAR_B_ARTIFACT"
```

The report date and absolute output path must come from the current run's
configuration. Add `--json` to inspect the rendered prompt, source path and SHA.

At deployment, the Hermes search task should retain only this fixed instruction:
resolve the current run's explicit report date/output path; run the command
above; read its stdout in full and perform that search task using Hermes tools.
Do not copy the rendered query list back into cron. This keeps changes to the
template effective on the next invocation without editing cron again.

The command renders instructions; Hermes owns search and saving the
`pillar-b-discovery.v1` envelope. The target monitor consumes it through
`--pillar-b-artifact`; this helper does not introduce a second search service.
Production cron has **not** been switched to this loader. Its current task still
contains the old year-only prompt; change it during the controlled deployment.

The production consumer validates the report date, exact completed query set,
three-month publication window and non-empty date evidence for the same article.
The legacy four-field array is not sufficient for production. Keep the
`## Search queries` heading and its unique query bullets: the validator reads
that section directly, so there is no second query list to maintain.
The date excerpt must come from that article or its source-backed search result;
passing the schema alone does not prove the publisher's claim.

### Serial authoring prompt boundaries

The article/executive response instructions now load the bound
`article_summary` and `executive_summary` components from the same task state in
`scripts/run_climate_monitor.py`. Their structured output remains validated by
`weekly_monitor/authoring_contract.py` and `taxonomy.py`. The pinned
`weekly-monitor-v1.prompt.md` remains a versioned contract/provenance artifact;
it is not a second whole-week request in the serial authoring path.

Search discovers candidates and retains factual excerpts. It does not create
final report summaries, classifications or keywords. Relevance rules are separate
configuration but run in the same per-URL request as those outputs. No classifier
agent or second classifier call is needed. Further prompt extraction belongs to
one focused change in this existing loader, not a new parallel service.

### Latest Pillar B verification

The 2026-09-08 SSH sandbox run used the rendered prompt for the 2026-09-07 report:
four required queries succeeded, four recent candidates were emitted, and three
remained after manual topic review. The output passed the existing four-field
consumer. This verifies a real search, not automated publication-date enforcement.

Search errors must not overwrite results with an empty array. The first rehearsal
exposed a read-only dependency-cache failure and an incorrect empty result; the
prompt was strengthened and the sandbox cache fixed before the successful rerun.
The new consumer adds the machine-checked query/date-evidence contract. Its live
test rejected a result that borrowed a date from a different publisher page.
The selected Hermes search backend also needs its pinned optional dependency
installed before running in a read-only sandbox. Detailed evidence is listed in
[PIPELINE_REFERENCE.md](PIPELINE_REFERENCE.md#verification-and-cutover).
