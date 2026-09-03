# Climate Monitor Pipeline — 完整流程图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        每周一 08:00 UTC 自动触发                            │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 1: Pillar A Site Check (08:00)                                      │
│  Script: step1_pillar_a.py                                                 │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ 1. Load baseline: article_state.json (1338 URLs)                      │  │
│  │ 2. Query SQLite changes table (last 7 days)                           │  │
│  │ 3. Parse diff_snippet: extract #### [Title](URL) from new_content   │  │
│  │ 4. Extract URLs from new_links changes                                │  │
│  │ 5. Filter: is_junk_url, is_junk_title, baseline dedup, relevance    │  │
│  │ 6. Append new URLs back to article_state.json (org keys only)         │  │
│  │ 7. Output: article_changes_YYYY-MM-DD.json                           │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│  Output: data/reports/article_changes_YYYY-MM-DD.json                       │
│  Example: 11 orgs, 22 new articles (15 already in baseline)               │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 2: Pillar B Web Search (08:15)                                      │
│  Type: Hermes LLM (web_search skill)                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ 1. web_search: "climate change actuarial risk insurance 2026"          │  │
│  │ 2. web_search: "IFRS S2 ISSB climate disclosure actuary 2026"        │  │
│  │ 3. web_search: "parametric insurance climate adaptation 2026"          │  │
│  │ 4. web_search: "climate risk scenario actuarial 2026"                  │  │
│  │ 5. Dedup by URL                                                     │  │
│  │ 6. Output: pillar_b_YYYY-MM-DD.json                                 │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│  Output: data/reports/pillar_b_YYYY-MM-DD.json (5-10 items)               │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 3: Aggregate Report (08:30)                                         │
│  Script: step3_aggregate.py                                               │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ 1. Load article_changes + pillar_b JSON                              │  │
│  │ 2. Merge all items                                                   │  │
│  │ 3. Dedup by normalized URL (lowercase, strip query, trailing /)      │  │
│  │ 4. Output: aggregated_YYYY-MM-DD.json                               │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│  Output: data/reports/aggregated_YYYY-MM-DD.json (27 items)               │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 3b: Hermes Relevance Filter (08:35)                                 │
│  Type: Hermes LLM (web_search skill)                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ 1. Read aggregated JSON                                              │  │
│  │ 2. For each article, LLM assesses:                                   │  │
│  │    - relevant: true/false (climate + actuarial?)                     │  │
│  │    - category: climate_disclosure, scenario_analysis, etc.            │  │
│  │    - summary: 1-2 sentence summary                                   │  │
│  │    - keywords: 3-5 terms                                           │  │
│  │ 3. Output: hermes_assessments_YYYY-MM-DD.json                      │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│  Output: data/reports/hermes_assessments_YYYY-MM-DD.json                  │
│  Fallback: if Hermes unavailable, use keyword-based classification         │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 3 Filter: Apply Assessments (script: step3_filter.py)                 │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ 1. Load aggregated + hermes_assessments JSON                        │  │
│  │ 2. For each item, use Hermes assessment (or keyword fallback)      │  │
│  │ 3. Separate: relevant (23) vs non-relevant (0)                     │  │
│  │ 4. Output: filtered_YYYY-MM-DD.json                                 │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│  Output: data/reports/filtered_YYYY-MM-DD.json (23 items)                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 4: LLM Summary Generation (08:45)                                   │
│  Type: Hermes LLM (web_search skill)                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ 1. Read filtered articles                                            │  │
│  │ 2. For each article, generate 1-2 line summary using LLM             │  │
│  │ 3. Generate 4-paragraph Executive Summary                           │  │
│  │ 4. Output: executive_summary_YYYY-MM-DD.json                       │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│  Output: data/reports/executive_summary_YYYY-MM-DD.json                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 5: Build Markdown Report (09:00)                                     │
│  Script: step5_build_md.py                                                 │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ 1. Load filtered + executive_summary JSON                             │  │
│  │ 2. Build MD with: title, metadata, stats, Pillar A/B sections        │  │
│  │ 3. Include filtering stats (23 relevant, 0 non-relevant)            │  │
│  │ 4. Output: climate-monitor-YYYY-MM-DD.md                            │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│  Output: data/reports/climate-monitor-YYYY-MM-DD.md (7839 chars)          │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 6: Render PDF (09:15)                                              │
│  Script: step6_render_pdf.py                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ 1. Copy MD to /tmp (outside repo for climate_delivery)               │  │
│  │ 2. Run: climate_delivery summarize --report /tmp/xxx.md              │  │
│  │ 3. Run: climate_delivery render-pdf --summary /tmp/xxx.json          │  │
│  │ 4. Verify PDF (%PDF- header, fail closed)                            │  │
│  │ 5. Output to climate_delivery_artifacts/YYYY-MM-DD/SHA/              │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│  Output: climate_delivery_artifacts/2026-09-07/SHA/climate-monitor.pdf     │
│  Size: ~12 KB, 3 pages                                                   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 7: Send Email (09:30)                                              │
│  Type: Hermes LLM (email skill)                                           │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ 1. Read MD report                                                   │  │
│  │ 2. Locate PDF artifact                                              │  │
│  │ 3. Send email with PDF attachment to subscribers                     │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│  Output: Email sent (or fail-closed if no sidecar)                         │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 8: Sync Registry (09:45)                                            │
│  Script: step8_sync_registry.py                                            │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ 1. Require sources/climate-monitor-<date>.md                          │  │
│  │ 2. Run: climate_registry plan-update (append-only, SHA checks)        │  │
│  │ 3. Fail closed on any report identity conflict                        │  │
│  │ 4. Run: climate_registry update (lock, migrations, exact backup)      │  │
│  │ 5. Output: registry updated (imported report dates)                   │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│  Output: Registry DB updated (append-only; semantics via semantic-import)   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  STEP 9: Update Website (10:00)                                           │
│  Script: step9_update_website.py                                          │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ 1. Copy MD to sources/                                                │  │
│  │ 2. Run: sync_source_wiki.py --cadence weekly (fail closed)            │  │
│  │ 3. Run: reload_and_smoke_test.py --date <date> (RELOAD_TOKEN env)     │  │
│  │ 4. Verify /api/config serves the new date; chat returns sources       │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│  Output: wiki updated + API reloaded and smoke-tested                       │
└─────────────────────────────────────────────────────────────────────────────┘
```

# Pipeline Steps Summary Table

| Step | Time | Script/Job | Input | Output | Type |
|------|------|------------|-------|--------|------|
| **1** | 08:00 | `step1_pillar_a.py` | SQLite `changes` table (last 7 days) + `article_state.json` baseline | `article_changes_YYYY-MM-DD.json` | Script (deterministic) |
| **2** | 08:15 | Cron: Step 2 Pillar B | web search queries | `pillar_b_YYYY-MM-DD.json` | LLM (web_search) |
| **3** | 08:30 | `step3_aggregate.py` | `article_changes` + `pillar_b` JSON | `aggregated_YYYY-MM-DD.json` | Script (deterministic) |
| **3b** | 08:35 | Cron: Step 3b Hermes Filter | `aggregated` JSON | `hermes_assessments_YYYY-MM-DD.json` | LLM (web_search) |
| **3f** | — | `step3_filter.py` | `aggregated` + `hermes_assessments` JSON | `filtered_YYYY-MM-DD.json` | Script (deterministic) |
| **4** | 08:45 | Cron: Step 4 LLM Summary | `filtered` JSON | `executive_summary_YYYY-MM-DD.json` + per-article summaries | LLM (web_search) |
| **5** | 09:00 | `step5_build_md.py` | `filtered` + `executive_summary` JSON | `climate-monitor-YYYY-MM-DD.md` | Script (deterministic) |
| **6** | 09:15 | `step6_render_pdf.py` | `climate-monitor-YYYY-MM-DD.md` | `climate_delivery_artifacts/YYYY-MM-DD/SHA/climate-monitor.pdf` | Script (deterministic) |
| **7** | 09:30 | Cron: Step 7 Send Email | MD report + PDF | Email sent | LLM (email) |
| **8** | 09:45 | `step8_sync_registry.py` | `filtered` JSON | `article-registry.sqlite3` updated | Script (deterministic) |
| **9** | 10:00 | `step9_update_website.py` | Wiki sources | Website updated + container reloaded | Script (deterministic) |

# Data Flow Diagram

```
article_state.json (baseline)
       │
       ▼
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
│  (filter)   │     │  (search)   │
└──────┬──────┘     └──────┬──────┘
       │                   │
       └─────────┬─────────┘
                 ▼
        ┌─────────────┐
        │  Step 3     │
        │  Aggregate  │
        │  + Dedup    │
        └──────┬──────┘
               │
               ▼
        ┌─────────────┐
        │  Step 3b    │
        │  Hermes LLM │
        │  Classify   │
        └──────┬──────┘
               │
               ▼
        ┌─────────────┐
        │  Step 3f    │
        │  Filter     │
        │  Relevant?  │
        └──────┬──────┘
               │
               ▼
        ┌─────────────┐
        │  Step 4     │
        │  LLM        │
        │  Summaries  │
        └──────┬──────┘
               │
               ▼
        ┌─────────────┐
        │  Step 5     │
        │  Build MD   │
        └──────┬──────┘
               │
               ▼
        ┌─────────────┐     ┌─────────────┐
        │  Step 6     │     │  Step 7     │
        │  Render PDF │     │  Send Email │
        └──────┬──────┘     └──────┬──────┘
               │                   │
               ▼                   ▼
        ┌─────────────┐     ┌─────────────┐
        │  Step 8     │     │  Step 9     │
        │  Sync Reg   │     │  Update Web │
        └──────┬──────┘     └──────┬──────┘
               │                   │
               ▼                   ▼
        ┌─────────────┐     ┌─────────────┐
        │  Registry   │     │  Wiki       │
        │  SQLite DB  │     │  Container  │
        └─────────────┘     └─────────────┘
```

# Key Files

| File | Location | Purpose |
|------|----------|---------|
| `step1_pillar_a.py` | `scripts/` | Extract articles from SQLite changes + baseline dedup |
| `step3_aggregate.py` | `scripts/` | Merge Pillar A + B, dedup by URL |
| `step3_filter.py` | `scripts/` | Apply Hermes LLM assessments (or keyword fallback) |
| `step3b_hermes_filter.py` | `scripts/` | Generate Hermes prompt for classification |
| `step5_build_md.py` | `scripts/` | Build final markdown report |
| `step6_render_pdf.py` | `scripts/` | Render PDF via climate_delivery |
| `step8_sync_registry.py` | `scripts/` | Sync to article-registry.sqlite3 |
| `step9_update_website.py` | `scripts/` | Update wiki + container reload |
| `PIPELINE_CONFIG.md` | repo root | Hermes prompt templates (modifiable) |
