# Weekly migration: what still needs changing

**Status of the daily → weekly switch.** Phase 1 is implemented on this branch.
This document is the research output for what remains.

Every claim below was verified by running the code against the real corpus
(`latest_date = 2026-08-10`), not by reading it.

---

## Phase 1 — landed in this branch (PR #24)

Status refers to this branch's contents, not to `main`.

| Change | File |
|---|---|
| `--cadence daily\|weekly` flag; weekly renders only real dates | `scripts/sync_source_wiki.py` |
| Prune legacy sourceless placeholder pages | `scripts/sync_source_wiki.py` |
| Weekly labels (`## Weekly Reports`, `#weekly-report`) | `scripts/sync_source_wiki.py` |
| Monday-only ingest from the monitoring job | `scripts/ingest_weekly_reports.py` |
| One-command refresh (ingest → sync → commit → reload → health check) | `scripts/weekly_wiki_refresh.sh` |
| Fixed a test that asserted the ingest schedule, not retrieval | `tests/test_agentic_wiki.py` |
| Docker + Caddy HTTPS on the host IP | `Dockerfile`, `Caddyfile`, `docker-compose.yml` |

Result: **24 pages / 24 sources / 0 missing** (was 74 pages with 50 phantoms).
Suite: **62 passed**.

---

## Phase 2 — the one that actually matters: week-based date windows

**This is the highest-value remaining change.** Everything else is cosmetic.

`_date_range_days()` in `agentic_wiki/wiki_agent.py` understands *days* and
*months*, but has **no concept of weeks**. Verified against the live corpus:

| Question | days parsed | dates returned |
|---|---|---|
| `Summarize the past 7 days of reports` | 7 | 7 dates ✅ |
| `summarize the last week` | 7 | 7 dates ✅ |
| `Give me a report for this month` | — | 10 dates ✅ |
| **`past 2 weeks`** | **None** | **0** ❌ |
| **`last 3 weeks of reports`** | **None** | **0** ❌ |
| **`Summarize the past 4 weeks`** | **None** | **0** ❌ |
| **`recent 6 weeks trends`** | **None** | **0** ❌ |

Under a weekly cadence, "past N weeks" is *the* natural way to ask for a range —
and it is exactly the phrasing that silently returns no date window. The query
still answers, but it falls back to generic relevance ranking with no date
coverage, so the user gets a plausible answer that quietly ignores the window
they asked for. **Silent wrong-scope is worse than an error.**

Three sub-issues:

1. **No `week(s)` pattern.** `DAY_RANGE_RE` only matches `day|days`. Needs a
   sibling `WEEK_RANGE_RE` matching `(?:past|last|recent)\s+(\d{1,2})\s+weeks?`,
   converted as `weeks * 7` days.
2. **The 30-day cap.** `_date_range_days` clamps to `min(n, 30)`. With weekly
   reports, 30 days is only ~4 data points; "past 6 months" of weekly reports is
   a perfectly reasonable ask. The cap should be raised (e.g. 180 days) or
   expressed as a max **number of reports** rather than a max number of days.
3. **`_window_dates` enumerates every calendar day.** For a 12-week window that
   builds an 84-element list where at most 12 can ever match. It works (the
   intersection is harmless) but it is wasteful and it makes the
   "coverage" reporting misleading — it counts 84 requested dates against 12
   possible hits. Better: intersect the window with the dates that actually
   exist in the corpus (`kb` already knows them) before reporting coverage.

**Recommended fix:** add week parsing, raise the cap, and intersect the window
with real corpus dates. Contained to ~3 functions in one file, plus tests.

---

## Phase 3 — the `"daily"` document type is now a misnomer

`_detect_type()` labels any `climate-monitor-YYYY-MM-DD` page as type
**`"daily"`**. That string is load-bearing across the whole stack:

- `agentic_wiki/wiki_agent.py` — ranking boosts (`chunk.type == "daily"`),
  `_asks_daily_summary()`, `_daily_page_path()`, source-link mapping
- `showcase/app.js` — `GRAPH_COLORS.daily`, graph legend, Dataview row rendering
  (`doc.type === "daily"`), status placeholders
- `showcase/styles.css` — `--daily` colour token, `.dot-daily`
- `showcase/index.html` — legend text reading "Daily"
- `/api/config` graph payloads consumed by the Obsidian plugin

It is **functionally correct** — the reports are still dated pages and
everything works. It is a naming/UX problem: the UI legend says "Daily" while
the index says "Weekly Reports".

**Recommendation: rename the user-visible label only, keep the internal type
string.** Change the legend/label to "Report" (cadence-neutral), leave
`type: "daily"` as the wire value. Renaming the type across the API payload,
the frontend, and the Obsidian plugin is a wide, breaking, low-reward change —
it would need a coordinated plugin update for a cosmetic gain. If a rename is
wanted later, do it as its own PR with a compatibility alias.

---

## Phase 4 — stale hardcoded dates (small but real)

`QUERY_ALIASES` in `wiki_agent.py` hardcodes April dates:

```python
"latest": "latest 2026-04-20 climate monitor update current summary",
"today":  "2026-04-20 latest daily report climate monitor",
```

The corpus latest is now **2026-08-10**. Asking "what's the latest?" injects the
string `2026-04-20` into the retrieval query, actively biasing results toward a
four-month-old report. Verified: the query returns `2026-08-10` **and**
`2026-04-20` among its sources — the stale bias is measurable.

**Fix:** build these aliases from `kb.latest_date` at runtime instead of
hardcoding. Also drop the word "daily" from the alias text.

---

## Phase 5 — prompt starters assume daily density

`PROMPT_STARTERS` (duplicated in `wiki_agent.py` **and** `showcase/app.js` —
they must be kept in sync) offers:

- "30-day change" → ~4 weekly reports
- "14-day themes" → ~2 weekly reports, described as "uses daily coverage as
  supporting context"

"Summarize the past 14 days by theme, not by day" is close to meaningless when
that window holds two reports.

**Fix:** retune to the new cadence — e.g. "Last 4 weeks", "Last quarter",
"Latest report" — and drop "daily" from the descriptions. **Note:** this depends
on Phase 2; changing the starter text to "past 4 weeks" *before* week-parsing
exists would ship a built-in button that silently returns no date window.

---

## Phase 6 — docs and cosmetics

- `README.md` line 3: "compiled **daily** from automated monitoring" → weekly.
  Also the "Daily Reports" section header and the "25 daily report pages
  covering 2026-04-01 through 2026-04-25 / Missing dates: …" block, all stale.
- `climate_monitor/config.py`: `report_title` default is
  `"Daily Climate & Actuarial Monitor"`. Only a fallback (the live weekly job
  supplies its own title), but it should not say Daily.
- `scripts/reload_and_smoke_test.py`: `--date` help text says "Daily report
  date". Cosmetic.

---

## Phase 7 — GitHub Actions still on a weekday schedule

`.github/workflows/climate-monitor.yml` runs `cron: "30 10 * * 1-5"` —
**Mon–Fri**, i.e. still the daily model, and it calls
`scripts/run_climate_monitor.py` directly (not the new weekly path).

This workflow is **independent of the local pipeline** built in Phase 1. Today
it would open a PR with a new dated report five times a week, competing with the
local weekly cadence.

Two coherent options — the choice is yours:

- **(a) Align it:** `cron: "30 10 * * 1"` and pass `--cadence weekly` to the
  sync step. CI becomes the weekly generator.
- **(b) Disable it:** drop the `schedule:` trigger, keep `workflow_dispatch:`
  for manual runs, and let the local cron own the cadence.

**I recommend (b)** while the local Docker service is the delivery surface —
having two independent generators writing dated reports into the same repo is a
recipe for conflicting commits. Revisit if the service ever moves off this host.

---

## Suggested order

1. **Phase 2** (week windows) — the only change with real correctness impact
2. **Phase 4** (stale dates) — tiny, measurable bias fix
3. **Phase 7** (decide CI) — needs your call, prevents duplicate generators
4. **Phase 5** (prompt starters) — after Phase 2 lands
5. **Phase 6** (docs), **Phase 3** (labels) — cosmetic cleanup

Phases 2 and 4 are contained to `agentic_wiki/wiki_agent.py` and are covered by
the existing test suite; they are the natural next PR.
