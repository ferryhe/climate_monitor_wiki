# Weekly cadence: how the daily pipeline was adapted

The repo was originally built around a **daily** monitor (April 2026,
`sources/climate-monitor-YYYY-MM-DD.md`, one file per calendar day). The live
monitoring job now runs **weekly** (Mondays 08:00 UTC). This document records
what changed and why.

## What "daily" assumed

`scripts/sync_source_wiki.py` expanded a *contiguous date range*: it took the
earliest and latest known report and generated a page for **every calendar day**
in between. Days with no source file became placeholder pages reading
"No report - source file missing for this date", plus a `⚠️ No report` row in
`wiki/index.md`. Under a daily cadence that is correct — a missing weekday is a
genuine gap worth flagging.

Under a weekly cadence it breaks badly: two reports 7 days apart manufacture
**six phantom pages** per week. Left unchanged, the index filled with ~50 bogus
"No report" rows and the retrieval corpus was polluted with empty documents.

## Changes

### 1. Cadence flag on the sync script

`sync_source_wiki(..., cadence="daily"|"weekly")`, also exposed as
`--cadence` and the `CLIMATE_WIKI_CADENCE` env var. The library default stays
`daily` so existing behaviour and tests are untouched; the weekly path is opted
into explicitly.

- `daily` — unchanged: fill the full date range, emit gap placeholders.
- `weekly` — render **only dates that actually have a report**. No grid, no
  phantoms. A 7-day grid was tried first and rejected: the corpus mixes the
  historical April daily run with the current weekly run, so any synthetic grid
  is wrong at the boundary.

### 2. Pruning legacy placeholders

Weekly mode deletes report pages that have no matching `sources/` file
(`prune_sourceless=True`, disable with `--keep-sourceless`). This cleared the 50
inherited placeholder pages. Result: **24 pages / 24 sources / 0 missing**.

### 3. Labels

Under weekly cadence the index heading becomes `## Weekly Reports`, the page
count reads "weekly report pages", and page tags are `#weekly-report` instead of
`#daily-report`.

### 4. Ingest script

`scripts/ingest_weekly_reports.py` copies reports from the monitoring job's
output directory (`/home/ubuntu/web_listening/data/reports/`) into `sources/`,
then runs the weekly sync.

**Only Monday-dated reports are ingested by default.** The report directory also
contains manual re-runs and debugging passes (Sun/Tue/Fri files) that are
duplicates of the same monitoring week; ingesting them would create several
"weekly" pages per week. Override with `--allow-offcycle`, or pin one file with
`--date YYYY-MM-DD`.

```bash
python scripts/ingest_weekly_reports.py --dry-run     # preview
python scripts/ingest_weekly_reports.py               # ingest + sync only
```

The ingest command has no commit or push mode. Automated publication is owned
by `scripts/publish_weekly_reports.py`, which works only in a temporary clone of
the latest `origin/main` and updates the fixed rolling branch
`codex/hermes-weekly-monitor`. It first pushes a unique temporary candidate ref
that is never connected to a PR, rechecks `main`, then promotes the candidate to
the rolling ref with an exact lease. A final `main` check happens immediately
after promotion. There is an unavoidable very short window in which rolling can
be based on a just-superseded `main`; the publisher detects that before any PR
operation and uses an exact lease to restore the previous good ref (or delete a
new ref) before retrying. Human review and merge remain the safety boundary.

### 5. Test suite

One pre-existing test (`test_past_week_daily_summary_covers_requested_window_offline`)
hard-asserted that all 7 days in a "past 7 days" window are present in the
corpus. That is an assertion about the *ingest schedule*, not about retrieval,
and it is false by construction under weekly cadence. It now asserts the real
contract: every report that **exists** in the window is covered, and the
window's latest report is included.

Four tests were added for weekly behaviour: no gap-filling, placeholder pruning,
`--keep-sourceless`, and a regression guard that daily cadence still fills gaps.

Suite: **112 passed**.

## Scheduling

Two jobs, deliberately separated so a monitoring failure cannot corrupt the wiki:

| Job | Schedule | Does |
|---|---|---|
| Weekly Climate & Actuarial Monitor (`f5259a8ec2d9`) | Mon 08:00 UTC | crawls 57 sites, writes `data/reports/climate-monitor-<date>.md` |
| Weekly Climate Wiki Publisher (`dccb79cd69bc`) | Mon 10:00 UTC | validates/imports reports in a temporary clone and updates a rolling PR |

The 2-hour offset gives the monitor room to finish (~6 min of crawling, plus
retries) before the publisher reads its output. If `main` already contains all
eligible Monday reports, publication is a no-op.

The publisher runs `scripts/weekly_wiki_refresh.sh`. A host `flock` prevents
overlap. The wrapper does not source `.env`, alter the production checkout,
reload the app, restart containers, or deploy anything.

The complete production flow is: **Hermes generate → rolling PR → human merge
→ separate server deploy**. The production checkout remains clean and tracks
`origin/main` throughout generation.

## No GitHub report generator

The competing GitHub Actions generator was deleted. Hermes is the only report
generator. Emergency manual generation is performed only on the controlled
server with the existing monitor and rolling-PR publisher; do not recreate a
scheduled or `workflow_dispatch` generator.
