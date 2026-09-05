# Climate Monitor Pipeline — Reference

Complete reference for the weekly climate/actuarial monitoring pipeline. For
editable LLM prompts, see [PIPELINE_CONFIG.md](PIPELINE_CONFIG.md).

## Pipeline Architecture

```
Weekly cron jobs (every Monday):

08:00  Step 1: Pillar A Site Check          → article_changes_{DATE}.json
08:15  Step 2: Pillar B Web Search          → pillar_b_{DATE}.json
08:30  Step 3: Aggregate + Dedup           → aggregated + combined candidates
       Step 3 stages a date-bound URL delta (canonical state is unchanged)
08:32  Step 4: Extract Conferences         → conferences_{DATE}.json
08:35  Step 5: Hermes LLM Classification   → hermes_assessments_{DATE}.json
08:37  Step 5b: Apply Assessments          → filtered_{DATE}.json
09:00  Step 6: Commit Report Bundle        → Markdown + report JSON + combined candidates
09:01  Post-report URL-state commit         → article_state.json
09:15  Step 7: Render PDF                  → climate_delivery_artifacts/{DATE}/{SHA}/climate-monitor-{DATE}.pdf
09:30  Step 8: Send Email                  → email sent
10:00  Step 10: Publish rolling PR          → codex/hermes-weekly-monitor
       Human review + merge                → GitHub main
       Server + Render deploy              → reviewed sources + wiki
       Step 9: Sync deployed Registry      → article-registry.sqlite3
```

## Steps Detail

| # | Time | Name | Type | Script/Cron | Output |
|---|---|---|---|---|---|
| 1 | 08:00 | Pillar A Site Check | Script | `scripts/step1_pillar_a.py` | article_changes JSON |
| 2 | 08:15 | Pillar B Web Search | LLM | Cron: Step 2 | pillar_b JSON |
| 2b | 09:01 | Commit URL State (historical filename) | Script | `scripts/step2_save_state.py --commit-pending` | article_state.json |
| 3 | 08:30 | Aggregate Report | Script | `scripts/step3_aggregate.py` | aggregated JSON |
| 4 | 08:32 | Extract Conferences | Script | `scripts/step7b_extract_conferences.py` | conferences JSON |
| 5 | 08:35 | Hermes Classification | LLM | Cron: Step 3b | hermes_assessments JSON |
| 5b | 08:37 | Apply Filter | Script | `scripts/step3_filter.py` | filtered JSON |
| 6 | 09:00 | Commit Report Bundle | Script | `scripts/step5_build_md.py` | Markdown + report JSON + combined candidates |
| 7 | 09:15 | Render PDF | Script | `scripts/step6_render_pdf.py` | PDF artifact |
| 8 | 09:30 | Send Email | LLM | Cron: Step 7 | Email |
| 10 | 10:00 | Publish Wiki PR | Script | `scripts/step9_update_website.py` | Rolling PR |
| 9 | Post-deploy | Sync Registry | Script | `scripts/step8_sync_registry.py` | Registry DB |

## Key Files

| File | Location | Purpose |
|---|---|---|
| `step1_pillar_a.py` | `scripts/` | Extract all valid current articles from SQLite changes |
| `step2_save_state.py` | `scripts/` | Verify the final bundle and explicitly commit its pending URL delta |
| `step3_aggregate.py` | `scripts/` | Merge Pillar A + B by canonical URL, apply history, and stage the pending delta |
| `step7b_extract_conferences.py` | `scripts/` | Pre-extract conference articles from aggregated JSON |
| `step3b_generate_assessments.py` | `scripts/` | Store LLM assessments (fallback: keyword classification) |
| `step3b_hermes_filter.py` | `scripts/` | Generate the Hermes prompt for classification |
| `step3_filter.py` | `scripts/` | Apply Hermes LLM assessments (or keyword fallback) |
| `step5_build_md.py` | `scripts/` | Recoverably commit Markdown, report evidence, and staged combined candidates |
| `step6_render_pdf.py` | `scripts/` | Render PDF via climate_delivery |
| `step8_sync_registry.py` | `scripts/` | Sync a deployed source to article-registry.sqlite3 |
| `step9_update_website.py` | `scripts/` | Delegate publication to the isolated rolling-PR publisher |
| `PIPELINE_CONFIG.md` | repo root | Cron schedule + Hermes prompt templates (modifiable) |

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

```
┌─────────────┐     ┌─────────────┐
│  SQLite     │     │  web_search │
│  changes    │     │  (Hermes)   │
│  table      │     │             │
└──────┬──────┘     └──────┬──────┘
       │                   │
       ▼                   ▼
┌─────────────┐     ┌─────────────┐
│  Step 1     │     │  Step 2     │
│  Pillar A   │     │  Pillar B   │
│  (collect)  │     │  (search)   │
└──────┬──────┘     └──────┬──────┘
       │                   │
       └─────────┬─────────┘
                 ▼
        ┌─────────────┐
        │  Step 3     │
        │  Aggregate  │
        │  + Dedup    │
        └──────┬──────┘
               │ stages date/digest-bound pending URL delta
               │
               ▼
        ┌─────────────┐     ┌─────────────┐
        │  Step 4     │     │  Step 5     │
        │  Conference │     │  Hermes LLM │
        │  Extract    │     │  Classify   │
        └──────┬──────┘     └──────┬──────┘
               │                   │
               └─────────┬─────────┘
                         ▼
                ┌─────────────┐
                │  Step 5b    │
                │  Filter     │
                │  Relevant?  │
                └──────┬──────┘
                       │
                       ▼
                ┌─────────────┐
                │  Step 6     │
                │  Build MD   │
                │  (SINGLE    │
                │  SOURCE)    │
                └──────┬──────┘
                       │
                       ▼
              explicit state commit
              (`step2_save_state.py
                 --commit-pending`)
                       │
         ┌─────────────┼─────────────┐
         │             │             │
         ▼             ▼             ▼
    ┌──────────┐ ┌──────────┐ ┌──────────────┐
    │  Step 7  │ │  Step 8  │ │   Step 10    │
    │  PDF     │ │  Email   │ │ Publish PR   │
    └──────────┘ └──────────┘ └──────┬───────┘
                                     ▼
                              review + merge
                                     │
                      ┌──────────────┴──────────────┐
                      ▼                             ▼
                server deploy                 Render deploy
                      │
                      ▼
                ┌──────────┐
                │  Step 9  │
                │ Sync DB  │
                └──────────┘
```

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
