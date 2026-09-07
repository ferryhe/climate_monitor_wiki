# Climate Monitor Pipeline — Reference

Complete reference for the weekly climate/actuarial monitoring pipeline. For
editable LLM prompts, see [PIPELINE_CONFIG.md](PIPELINE_CONFIG.md).

## Single production chain (authoritative)

The weekly pipeline is **one** Hermes-scheduled driver path. The four jobs
below run in order every Monday UTC and each writes a slot in
`scheduler-status.json` (read by `GET /api/job-status`):

| # | UTC | Slot            | Entry point                                              | What it does                                                                                                                          |
|---|-----|-----------------|----------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------|
| 1 | 08  | `monitor`       | `scripts/run_climate_monitor.py --production-weekly`     | Calls `climate_monitor.weekly_monitor.driver.run_weekly_monitor` → `climate_monitor.orchestrator.run_monitor`. Writes the Monday report, semantic sidecar, combined candidates, and article-evidence artifact; stages the pending URL delta. |
| 2 | 09  | `email`         | `climate_delivery` pipeline (`scripts/record_weekly_run.py` → email + PDF) | Renders the PDF, writes the manifest, and sends the retained weekly email to the existing four recipients. |
| 3 | 10  | `publisher`     | `scripts/weekly_wiki_refresh.sh` → `scripts/publish_weekly_reports.py` | Isolated-clone rolling-PR publisher. Never touches the production checkout. Updates the `codex/hermes-weekly-monitor` PR branch only. Human review + merge + server/Render deploy are separate, human-controlled steps. |
| 4 | 10:30 | `registry`     | `scripts/weekly_registry_refresh.py`                     | Draft, **disabled by default**. Scheduled only after the validated-fallback deployment and a controlled exact-sync round-trip complete. |

The 2-hour gap between monitor (08:00) and publisher (10:00) is deliberate so
the report exists before ingest. Each slot writes its own state via
`scripts/hermes_job_<slot>.sh` (see [AC-10](#hermes-job-wrappers-ac-10)).

The repository no longer schedules a 10-step pipeline of independent cron
entries. Any pre-existing `step[1-9]*.py` script in `scripts/` is retained for
**compatibility** (referenced by `tests/test_step1_pillar_a_parser.py`,
`tests/test_pipeline_scripts.py`, and the legacy hermes fallback path) and is
**not part of the single production chain** invoked today.

### Pipeline architecture (single chain)

```
Weekly Monday UTC (Hermes cron, four slots)

08:00  monitor    run_climate_monitor.py --production-weekly
                  └── climate_monitor.weekly_monitor.driver.run_weekly_monitor
                          └── climate_monitor.orchestrator.run_monitor
                                  ├── Markdown + semantic sidecar + combined candidates
                                  ├── article-evidence.v1_{DATE}.json (AC-1 #93 path)
                                  └── pending-seen-url delta (atomic two-phase)

09:00  email      climate_delivery pipeline
                  ├── climate-monitor-{DATE}.pdf (manifest + briefing JSON)
                  └── email sent to the four retained recipients

10:00  publisher  weekly_wiki_refresh.sh  →  publish_weekly_reports.py
                  ├── isolated clone of origin/main
                  ├── wiki/ regenerated via sync_source_wiki
                  ├── codex/hermes-weekly-monitor rolling PR updated
                  └── CAS rollback if main moved during the lease

10:30  registry   weekly_registry_refresh.py  [DISABLED — awaiting merge + deploy gate]
```

## Steps Detail

The numbered `stepN_*.py` scripts are **compatibility fallbacks** (referenced
by `tests/test_step1_pillar_a_parser.py`, `tests/test_pipeline_scripts.py`,
and the legacy hermes fallback path). They are **not** scheduled and **not**
invoked by the single production chain above. They are retained so the
on-disk tests that import them continue to validate the legacy artifact
contract and so any future controlled run can reproduce the legacy flow.

| Legacy script               | Used by                                       | Notes |
|-----------------------------|-----------------------------------------------|-------|
| `scripts/step1_pillar_a.py` | `tests/test_step1_pillar_a_parser.py`         | Parses the legacy SQLite Pillar A changes table. |
| `scripts/step2_save_state.py` | `tests/test_pipeline_scripts.py`            | Commits the pending URL delta with explicit `--commit-pending`. |
| `scripts/step3_aggregate.py`  | `tests/test_pipeline_scripts.py`            | Pre-driver merge of Pillar A + Pillar B by canonical URL. |
| `scripts/step3_filter.py`     | `tests/test_pipeline_scripts.py`            | Applies the legacy Hermes assessments (or keyword fallback). |
| `scripts/step5_build_md.py`   | `tests/test_pipeline_scripts.py`            | Recoverably commits Markdown + combined-candidates evidence. |
| `scripts/step6_render_pdf.py` | (compatibility only)                         | Pre-`climate_delivery` PDF renderer. |
| `scripts/step7b_extract_conferences.py` | (compatibility only)              | Pre-extracts conference articles from the aggregated JSON. |
| `scripts/step8_sync_registry.py` | (compatibility only)                      | Pre-driver Registry DB sync. |
| `scripts/step9_update_website.py` | (compatibility only)                    | Pre-driver delegation entry point. |

Do not schedule these scripts. Do not assume any of them is running on the
controlled server. Their only producer of authoritative weekly output today
is the single production chain above.

## Key Files

| File | Location | Purpose |
|---|---|---|
| `run_climate_monitor.py` | `scripts/` | **Authoritative** entry point. `--production-weekly` invokes the single driver path. |
| `publish_weekly_reports.py` | `scripts/` | **Authoritative** publisher. Clones `origin/main` into a temp dir, regenerates wiki, updates the rolling PR branch only. |
| `weekly_wiki_refresh.sh` | `scripts/` | Hermes wrapper around `publish_weekly_reports.py`. The lock and isolated-clone are the load-bearing parts. |
| `record_weekly_run.py` | `scripts/` | 09:00 wrapper around the `climate_delivery` pipeline. |
| `weekly_registry_refresh.py` | `scripts/` | 10:30 draft (disabled by default). |
| `weekly_monitor/driver.py` | `climate_monitor/` | `run_weekly_monitor` — the single v1/v2 driver path. |
| `weekly_monitor/authoring_contract.py` | `climate_monitor/` | v1/v2 authoring request + response validation. v2 binds the response to the article-evidence envelope and exposes the 6-key stats (total/updated/unchanged/blocked/failed/unresolved) so the orchestrator and downstream scripts can report 57/42/15 splits. |
| `orchestrator.py` | `climate_monitor/` | `run_monitor` — the core report renderer, dedup, sidecar, and atomic URL-state transaction. |
| `seen_state.py` | `climate_monitor/` | Two-phase canonical URL history commit. The pending delta is staged before any artifact is written and committed only when the bundle validates. |
| `job_status.py` | `climate_monitor/` | Read-side: validates `scheduler-status.json` written by `climate_monitor/scheduler_status.py`. |
| `scheduler_status.py` | `climate_monitor/` | Write-side: `update_slot(name, state, …)` invoked by the four Hermes job wrappers. |
| `hermes_job_monitor.sh` … `hermes_job_registry.sh` | `scripts/` | Four thin Hermes wrappers; each writes a single slot to `scheduler-status.json`. |
| `step1_pillar_a.py` … `step9_update_website.py` | `scripts/` | **Compatibility only.** Not invoked by the single production chain. |
| `PIPELINE_CONFIG.md` | repo root | The four-job weekly schedule + retained LLM prompt templates. |

## Date Logic

Every script accepts `--date`. Steps 6/9 default to `last_monday()`; step 8
requires an explicit deployed report date, and the other steps default to
today's date. The cron jobs always pass the report date explicitly, and step1
anchors its query window on that date.

## Dedup Mechanism

### article_state.json
- Stores previously committed canonical URLs for Pillar A + B.
- Step 1 neither filters against nor writes this file; it collects all valid
  current discoveries in its unchanged artifact shape.
- Step 3 performs the canonical-URL history split after the A/B merge and
  stages a pending delta bound to the report date and combined-candidate digest.
  If a complete same-date report exists, its canonical combined evidence stays
  untouched and its validated candidate items are carried into the shared merge
  with incremental current input. The next complete candidate evidence is then
  staged for Step 5 promotion.
- Step 5 validates and recoverably promotes Markdown, report JSON, and the
  matching combined evidence as one bundle without changing Markdown format.
  A custom Step 3 `--combined-output PATH` is continued with Step 5 and Step 2
  by passing that same path as `--combined PATH`.
- Only `step2_save_state.py --commit-pending`, after the final Markdown and
  report evidence exist, verifies that exact bundle and updates this file.
- A pending delta for another date is never overwritten. Commit that bound
  date first; `--no-update-seen-state` leaves the pending and canonical files
  unchanged.

### Registry DB
- Stores Monday reports by default; non-Monday (offcycle) manual re-runs are
  accepted only with an explicit `--allow-offcycle` opt-in on the registry
  CLI (`plan-selection`, `weekly-sync`) and the step scripts
- Sync is append-only via `climate_registry plan-update`/`update` with SHA conflict checks

## Data Flow

The data flow today is **the single production chain** at the top of this
document. The diagram below is the historic pre-driver view retained for
compatibility; it does not run today.

```
  ┌───────────────────────────────────────────────────────────────┐
  │  compatibility-only legacy flow (not invoked in production)   │
  │                                                               │
  │  Step 1: Pillar A   ───→ article_changes_{DATE}.json          │
  │  Step 2: Pillar B   ───→ pillar_b_{DATE}.json                 │
  │  Step 2 (state)     ───→ article_state.json (dedup baseline)  │
  │  Step 3: Aggregate  ───→ aggregated_{DATE}.json               │
  │  Step 7b: Conferences ──→ conferences_{DATE}.json             │
  │  Step 3b: Hermes    ───→ hermes_assessments_{DATE}.json       │
  │  Step 3f: Filter    ───→ filtered_{DATE}.json                 │
  │  Step 5: Build MD   ───→ climate-monitor-{DATE}.md            │
  │         └──→ Step 6: Render PDF  ──→ climate-monitor-{DATE}.pdf│
  │         └──→ Step 7: Send Email                               │
  │         └──→ Step 9: Publish rolling PR                       │
  │                  └─→ review + merge into GitHub main          │
  │                       └─→ server deploy  /  Render deploy     │
  │                            └─→ Step 8: Sync deployed Registry │
  └───────────────────────────────────────────────────────────────┘
```

For the actual production flow see the **Single production chain
(authoritative)** section above. The numeric script names above are
retained for test compatibility; publication now precedes the post-deploy
Registry sync, and neither script writes generated report content directly
into the production checkout. GitHub `main` is the common content source for
the controlled server and Render.

## MD Report Structure (Single Source of Truth)

```
# 🌡️ Weekly Climate & Actuarial Monitor (Supranational Orgs)

**Report Date:** {DATE}
**Generated:** {TIMESTAMP}
**Scope:** 57 supranational organization sites monitored

---

## 📋 Executive Summary

- Sites checked: **{CHECKED}**, succeeded: **{SUCCEEDED}**, failed: **{FAILED}**
- Monitored window: last 7 days
- Pillar B search window: last 3 months
- Total detected changes: **N** → After relevance filter: **M**

{4-paragraph executive summary from Hermes LLM}

---

## Pillar A — Climate & Actuarial Site Changes

### {Category} ({count})

- **{Title}**
  - **Categories:** {Primary}, {Secondary}, ...
  - {Summary (2-4 sentences)}
  - **Keywords:** {keyword1}, {keyword2}, ...
  🔗 {URL}

---

## Pillar B — Climate & Actuarial Intelligence (last 3 months)

### {Category} ({count})

- **{Title}**
  - **Categories:** {Primary}, {Secondary}, ...
  - {Summary}
  - **Keywords:** ...
  🔗 {URL}

---

## 🔗 Original Links

- {URL1}
- {URL2}
...
```

### Categories contract

Every article carries an ordered `categories` list (first element = primary
display category) plus a derived `category` field equal to `categories[0]` for
compatibility. Sections group articles by the primary category only; the
`Categories:` line and the JSON sidecar emit the full ordered list.

## Web Interface

The web interface (wiki) displays the full MD content for each report:
- Full executive summary
- All articles grouped by category
- Each article shows: title, categories, summary, keywords, URL
- Tags for search/filtering

The RAG system uses wiki pages as context for answering questions about reports.

## Prompt Configuration

All LLM prompts are stored in `PIPELINE_CONFIG.md` for easy modification without code changes.

## Hermes job wrappers (AC-10)

Each of the four Monday UTC slots above is wrapped by a thin `hermes_job_*.sh`
script that:

1. Resolves inputs from explicit environment variables (`$CLIMATE_REPORTS_DIR`,
   `$CLIMATE_RUN_LEDGER_DIR`, `$CLIMATE_JOB_STATUS_DIR`, `$CLIMATE_SOURCE_DIR`,
   `$CLIMATE_WIKI_DIR`, `$CLIMATE_FIXTURE_DIR`). No path is hardcoded to
   `/home/ubuntu/*`; the production checkout at `/opt/climate_monitor_wiki` is
   the only filesystem anchor.
2. Invokes the slot's actual entry point (no inline logic).
3. Writes a strictly-validated `scheduler-status.json` slot via
   `climate_monitor/scheduler_status.py update_slot(name, state, …)` so
   `/api/job-status` reports the live state of every job.

| Wrapper                              | Slot      | Entry point invoked                                                    | Default state on success |
|--------------------------------------|-----------|------------------------------------------------------------------------|--------------------------|
| `scripts/hermes_job_monitor.sh`      | `monitor` | `python scripts/run_climate_monitor.py --production-weekly …`          | `completed`              |
| `scripts/hermes_job_email.sh`        | `email`   | `python scripts/record_weekly_run.py` (climate_delivery pipeline)      | `completed`              |
| `scripts/hermes_job_publisher.sh`    | `publisher` | `bash scripts/weekly_wiki_refresh.sh`                                 | `completed`              |
| `scripts/hermes_job_registry.sh`     | `registry` | (none — dry-run path)                                                 | `not_dispatched` (disabled gate) |

`scripts/hermes_job_monitor.sh` falls back to a pre-staged article-evidence +
stats fixture at `$CLIMATE_FIXTURE_DIR` when the live `web_listening` outcome
is not available (controlled dry-run path). It does **not** read `.env`, push
to git, or reload the API server.
