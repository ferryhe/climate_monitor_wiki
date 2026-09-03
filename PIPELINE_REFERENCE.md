# Climate Monitor Pipeline — Complete Reference

## Pipeline Architecture

```
Weekly cron jobs (every Monday):

08:00  Step 1: Pillar A Site Check          → article_changes_{DATE}.json
08:15  Step 2: Pillar B Web Search          → pillar_b_{DATE}.json
       Step 2b: Save Pillar B URLs         → article_state.json (dedup baseline)
08:30  Step 3: Aggregate + Dedup           → aggregated_{DATE}.json
08:32  Step 4: Extract Conferences         → conferences_{DATE}.json
08:35  Step 5: Hermes LLM Classification  → hermes_assessments_{DATE}.json
       Step 5b: Apply Assessments         → filtered_{DATE}.json
09:00  Step 6: Build Markdown Report     → climate-monitor-{DATE}.md
09:15  Step 7: Render PDF                → climate_delivery_artifacts/{DATE}/{SHA}/climate-monitor-{DATE}.pdf
09:30  Step 8: Send Email                → email sent
09:45  Step 9: Sync Registry            → article-registry.sqlite3
10:00  Step 10: Update Website          → wiki + container reload
```

## Steps Detail

| # | Time | Name | Type | Script/Cron | Output |
|---|---|---|---|---|---|
| 1 | 08:00 | Pillar A Site Check | Script | `scripts/step1_pillar_a.py` | article_changes JSON |
| 2 | 08:15 | Pillar B Web Search | LLM | Cron: Step 2 | pillar_b JSON |
| 2b | 08:15 | Save Pillar B State | Script | `scripts/step2b_save_state.py` | article_state.json |
| 3 | 08:30 | Aggregate Report | Script | `scripts/step3_aggregate.py` | aggregated JSON |
| 4 | 08:32 | Extract Conferences | Script | `scripts/step7b_extract_conferences.py` | conferences JSON |
| 5 | 08:35 | Hermes Classification | LLM | Cron: Step 3b | hermes_assessments JSON |
| 5b | 08:35 | Apply Filter | Script | `scripts/step3_filter.py` | filtered JSON |
| 6 | 09:00 | Build Markdown | Script | `scripts/step5_build_md.py` | climate-monitor MD |
| 7 | 09:15 | Render PDF | Script | `scripts/step6_render_pdf.py` | PDF artifact |
| 8 | 09:30 | Send Email | LLM | Cron: Step 7 | Email |
| 9 | 09:45 | Sync Registry | Script | `scripts/step8_sync_registry.py` | Registry DB |
| 10 | 10:00 | Update Website | Script | `scripts/step9_update_website.py` | Wiki + RAG |

## Date Logic

All scripts use `last_monday()` — the most recent Monday. If today is Monday, use today.

## Dedup Mechanism

### article_state.json
- Stores all previously seen URLs (Pillar A + B)
- Pillar A: URLs from web_listening changes
- Pillar B: URLs from web_search (under `__pillar_b__` key)
- Step 1 filters new articles against this baseline

### Registry DB
- Stores Monday reports only
- 9 reports currently (latest 2026-09-07)

## Data Flow

```
article_state.json (dedup baseline)
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
        ┌─────────────┐     ┌─────────────┐
        │  Step 4     │     │  Step 5     │
        │  Conference │     │  Hermes LLM │
        │  Extract    │     │  Classify   │
        └──────┬──────┘     └──────┬──────┘
               │                   │
               └─────────┬─────────┘
                         ▼
                ┌─────────────┐
                │  Step 5b   │
                │  Filter     │
                │  Relevant?  │
                └──────┬──────┘
                       │
                       ▼
                ┌─────────────┐
                │  Step 6     │
                │  Build MD   │
                │  (SINGLE   │
                │  SOURCE)   │
                └──────┬──────┘
                       │
         ┌─────────────┼─────────────┐
         │             │             │
         ▼             ▼             ▼
    ┌──────────┐ ┌──────────┐ ┌──────────┐
    │  Step 7  │ │  Step 8  │ │  Step 9  │
    │  PDF     │ │  Email   │ │  Sync DB │
    └──────────┘ └──────────┘ └──────────┘
                                     │
                                     ▼
                            ┌─────────────┐
                            │  Step 10    │
                            │  Update Web  │
                            │  wiki + RAG │
                            └─────────────┘
```

## MD Report Structure (Single Source of Truth)

```
# 🌡️ Weekly Climate & Actuarial Monitor (Supranational Orgs)

**Report Date:** {DATE}
**Generated:** {TIMESTAMP}
**Scope:** 57 supranational organization sites monitored

---

## 📋 Executive Summary

- Sites checked: **57**, succeeded: **57**, failed: **0**
- Monitored window: last 7 days
- Pillar B search window: last 3 months
- Total detected changes: **N** → After relevance filter: **M**

{4-paragraph executive summary from Hermes LLM}

---

## Pillar A — Climate & Actuarial Site Changes

### {Category} ({count})

- **{Title}**
  - **Categories:** {Category}
  - {Summary (2-4 sentences)}
  - **Keywords:** {keyword1}, {keyword2}, ...
  🔗 {URL}

---

## Pillar B — Climate & Actuarial Intelligence (last 3 months)

### {Category} ({count})

- **{Title}**
  - **Categories:** {Category}
  - {Summary}
  - **Keywords:** ...
  🔗 {URL}

---

## 🔗 Original Links

- {URL1}
- {URL2}
...
```

## Web Interface

The web interface (wiki) displays the full MD content for each report:
- Full executive summary
- All articles grouped by category
- Each article shows: title, categories, summary, keywords, URL
- Tags for search/filtering

The RAG system uses wiki pages as context for answering questions about reports.

## Prompt Configuration

All LLM prompts are stored in `PIPELINE_CONFIG.md` for easy modification without code changes.
