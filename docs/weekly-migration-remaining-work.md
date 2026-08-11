# Weekly migration: what still needs changing

**Status of the daily → weekly switch.** Phases 1, 2, 4, 5, and the PR review
follow-ups are implemented. This document records both the remaining work and
the reasoning behind completed migration steps.

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
| Isolated weekly publication (temporary clone → rolling PR) | `scripts/publish_weekly_reports.py`, `scripts/weekly_wiki_refresh.sh` |
| Fixed a test that asserted the ingest schedule, not retrieval | `tests/test_agentic_wiki.py` |
| Docker + Caddy HTTPS on the host IP | `Dockerfile`, `Caddyfile`, `docker-compose.yml` |
| Week-based date windows and window-scoped citations | `agentic_wiki/wiki_agent.py`, `tests/test_agentic_wiki.py` |
| Removed the competing GitHub Actions generator | `.github/workflows/climate-monitor.yml` (deleted) |

Result: **24 pages / 24 sources / 0 missing** (was 74 pages with 50 phantoms).
Current suite: **120 passed**.

---

## Phase 2 — landed after review: week-based date windows

This was the highest-value correctness follow-up and is now implemented on this
branch.

`_date_range_days()` in `agentic_wiki/wiki_agent.py` understands *days* and
*months*, and now also understands *weeks*. Verified against the live corpus:

| Question | days parsed | dates returned |
|---|---|---|
| `Summarize the past 7 days of reports` | 7 | 7 dates ✅ |
| `summarize the last week` | 7 | 7 dates ✅ |
| `Give me a report for this month` | — | 10 dates ✅ |
| `past 2 weeks` | 14 | 14 dates ✅ |
| `last 3 weeks of reports` | 21 | 21 dates ✅ |
| `Summarize the past 4 weeks` | 28 | 28 dates ✅ |
| `recent 6 weeks trends` | 42 | 42 dates ✅ |

Under a weekly cadence, "past N weeks" is *the* natural way to ask for a range —
and it now returns a real date window. The answer source list is also scoped to
that window so old April reports do not appear as citations for an August window.

The parser still represents a rolling period as calendar dates for query
scoping. Retrieval and displayed coverage now intersect that window with real
corpus report dates, so a 4-week question reports the available weekly reports
rather than claiming most days are missing.

---

## Phase 3 — the `"daily"` document type is now a misnomer

`_detect_type()` labels any `climate-monitor-YYYY-MM-DD` page as type
**`"daily"`**. That string is load-bearing across the whole stack:

- `agentic_wiki/wiki_agent.py` — ranking boosts (`chunk.type == "daily"`),
  `_asks_report_summary()`, `_daily_page_path()`, source-link mapping
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

## Phase 4 — landed: runtime latest-date aliases

The former `QUERY_ALIASES` entries hardcoded April dates:

```python
"latest": "latest 2026-04-20 climate monitor update current summary",
"today":  "2026-04-20 latest daily report climate monitor",
```

The corpus latest is now **2026-08-10**. Asking "what's the latest?" injects the
string `2026-04-20` into the retrieval query, actively biasing results toward a
four-month-old report. Verified: the query returns `2026-08-10` **and**
`2026-04-20` among its sources — the stale bias is measurable.

`_expand_query()` now receives `kb.latest_date` at runtime and builds `latest`
and `today` expansions from that value using cadence-neutral report wording.
Verified against the real corpus: a latest-report query cites 2026-08-10 and no
longer cites 2026-04-20.

---

## Phase 5 — landed: weekly-density prompt starters

The former prompt starters included:

- "30-day change" → ~4 weekly reports
- "14-day themes" → ~2 weekly reports, described as "uses daily coverage as
  supporting context"

"Summarize the past 14 days by theme, not by day" is close to meaningless when
that window holds two reports.

The API and frontend fallback now offer `Last 4 weeks`, `Last 12 weeks`,
`Insurer implications`, `Pricing explainer`, and `Latest report`. Their prompts
and descriptions use weekly-report semantics and are regression-tested to stay
in sync.

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

## Phase 7 — superseded: one automatic generator

Hermes is now the only report generator. The competing GitHub Actions workflow
was deleted entirely. Emergency manual generation runs only on the controlled
server through the same monitor and rolling-PR publisher.

Hermes publication no longer commits on the production checkout. The publisher
starts from the latest `origin/main` in a temporary clone, validates and imports
all unpublished Monday reports, runs the complete checks, and updates
`codex/hermes-weekly-monitor` through an unconnected temporary candidate ref.
The publisher checks `main` before exact-lease promotion and immediately after.
The second window cannot be eliminated with ordinary Git pushes: if `main`
moves there, the publisher CAS-restores the previous good rolling ref (or
deletes a newly created one), removes the candidate, and retries before any PR
operation. Human review and merge are the final safety boundary. The operational
flow is **generate → rolling PR → human merge → server deploy**.

---

## Suggested order

1. **Phase 4** (stale dates) — tiny, measurable bias fix
2. **Phase 5** (prompt starters) — now that week windows work
3. **Phase 6** (docs), **Phase 3** (labels) — cosmetic cleanup

Phase 4 is contained to `agentic_wiki/wiki_agent.py` and is covered by the
existing test suite; it is the natural next correctness PR.
