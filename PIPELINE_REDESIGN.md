# Climate Monitor Wiki — 9-Step Pipeline Redesign

## Current Problems
1. 08:00 Monitor prompt too complex (10+ steps), LLM skips deterministic site checks
2. Registry Sync broken (model config drift + non-Monday blocked)
3. 09:00 Email fail-closed (missing sidecar)
4. Two competing collection paths (old weekly_driver + new Agentic)

## Target Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Step 1: Pillar A — Deterministic Site Check (script)      │
│  scripts/run_pillar_a.py                                     │
│  Input: 57 org sites (SQLite: web_listening/data/sites.db)     │
│  Output: article_changes_<DATE>.json (new articles per org)    │
├─────────────────────────────────────────────────────────────┤
│  Step 2: Pillar B — Web Search (Hermes LLM)                │
│  cron job → hermes web_search                                │
│  Input: search queries (climate actuarial, last 3 months)      │
│  Output: pillar_b_<DATE>.json (deduped web items)           │
├─────────────────────────────────────────────────────────────┤
│  Step 3: Aggregate + Dedup (script)                         │
│  scripts/aggregate_report.py                                 │
│  Input: article_changes_<DATE>.json + pillar_b_<DATE>.json    │
│  Output: aggregated_<DATE>.json (unified candidate list)     │
├─────────────────────────────────────────────────────────────┤
│  Step 4: LLM Summary (Hermes LLM)                           │
│  cron job → hermes chat (per-article + executive prompts)    │
│  Input: aggregated_<DATE>.json                               │
│  Output: summaries_<DATE>.json (per-article + executive)    │
├─────────────────────────────────────────────────────────────┤
│  Step 5: Build Final MD (script)                             │
│  scripts/build_markdown.py                                   │
│  Input: aggregated_<DATE>.json + summaries_<DATE>.json       │
│  Output: climate-monitor-<DATE>.md (final report)            │
├─────────────────────────────────────────────────────────────┤
│  Step 6: Render PDF (script)                                │
│  scripts/render_pdf.py (calls climate_delivery render-pdf)   │
│  Input: climate-monitor-<DATE>.md                            │
│  Output: delivery_artifacts/<DATE>/<SHA>/pdf                │
├─────────────────────────────────────────────────────────────┤
│  Step 7: Send Email (Hermes LLM)                            │
│  cron job → hermes email tool                               │
│  Input: climate-monitor-<DATE>.md + PDF path                │
│  Output: email sent to approved recipients                   │
├─────────────────────────────────────────────────────────────┤
│  Step 8: Sync Registry (script)                             │
│  scripts/sync_registry.py                                   │
│  Input: climate-monitor-<DATE>.md                            │
│  Output: Registry DB updated (reports + articles)           │
├─────────────────────────────────────────────────────────────┤
│  Step 9: Website Display (script + reload)                   │
│  scripts/update_website.py                                  │
│  Input: Registry DB + wiki pages                            │
│  Output: Website updated (wiki + history_report)            │
└─────────────────────────────────────────────────────────────┘
```

## Implementation Plan

### Phase 1: Scripts (deterministic, no LLM)
- [ ] scripts/run_pillar_a.py — site check via web_listening
- [ ] scripts/aggregate_report.py — merge + dedup
- [ ] scripts/build_markdown.py — generate final MD
- [ ] scripts/render_pdf.py — call climate_delivery render-pdf
- [ ] scripts/sync_registry.py — insert into SQLite
- [ ] scripts/update_website.py — reload + verify

### Phase 2: Hermes Jobs (LLM-powered)
- [ ] 08:00 Pillar A check (script only)
- [ ] 08:15 Pillar B search (hermes web_search)
- [ ] 08:30 Aggregate + Summaries (hermes LLM)
- [ ] 09:00 Build MD + Render PDF (script)
- [ ] 09:30 Send Email (hermes email tool)
- [ ] 10:00 Sync Registry (script)
- [ ] 10:30 Update Website (script)

### Phase 3: Testing
- [ ] Test each script independently
- [ ] Test full pipeline end-to-end
- [ ] Verify website display

## Key Design Decisions
1. **Scripts are deterministic** — no LLM, no randomness
2. **Hermes jobs only for LLM tasks** — web search, summarization, email
3. **Each step is independently testable**
4. **Intermediate files are saved** — for debugging and retry
5. **No preflight gates** — if a step fails, fix and re-run
