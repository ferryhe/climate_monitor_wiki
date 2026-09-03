# Weekly migration: remaining work

**Backlog of the daily → weekly switch.** All correctness phases (date
windows, runtime latest-date aliases, weekly-density prompt starters, single
generator, isolated publisher) are implemented on `main`. Only cosmetic
renames remain; none of them are functionally required.

---

## Remaining item 1 — user-visible "Daily" legend label

The UI legend still reads "Daily" while the index says "Weekly Reports".
The wire value `type: "daily"` is load-bearing across the whole stack
(`agentic_wiki/wiki_agent.py` ranking boosts, `showcase/app.js` graph
colours, `/api/config` payloads consumed by the Obsidian plugin), so do NOT
rename the internal type string. Rename the user-visible label only:

- `showcase/index.html:303` — legend text "Daily" → "Report"
- `showcase/app.js:49,59` — same legend text in JS-rendered spots

## Remaining item 2 — "Daily" wording in code defaults

The live weekly job supplies its own report title, so these are fallbacks
only, but they should not say Daily:

- `climate_monitor/config.py:125` — `report_title` default
  `"Daily Climate & Actuarial Monitor"` → weekly wording. Several tests
  (`tests/test_climate_monitor_config.py`, `test_climate_monitor_orchestrator.py`,
  `test_climate_monitor_report_writer.py`, `test_climate_monitor_research_search.py`)
  assert the current default and must be updated in the same PR.
- `scripts/reload_and_smoke_test.py:25` — `--date` help text "Daily report
  date" → "Report date".

---

## History (for context only — all completed)

- Phase 1 (PR #24): `--cadence daily|weekly`, weekly-only real dates,
  Monday-only ingest, isolated rolling-PR publisher, Docker + Caddy HTTPS.
  Result: 24 pages / 24 sources / 0 missing.
- Phase 2: week-based date windows in `_date_range_days()` (verified against
  the live corpus).
- Phase 4: runtime latest-date aliases — stale April dates no longer bias
  retrieval queries.
- Phase 5: weekly-density prompt starters (`Last 4 weeks`, `Insurer
  implications`, etc.) with regression tests.
- Phase 7: Hermes is the sole report generator; the GitHub Actions workflow
  was deleted. Operational flow: generate → rolling PR → human merge →
  server deploy.
