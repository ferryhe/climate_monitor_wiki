# Climate Monitor Wiki — Repo Study & Weekly Integration Report

> **Historical snapshot:** This report records the earlier local-commit/reload
> design and is not current operating guidance. The live workflow is documented
> in `docs/weekly-cadence.md`: Hermes generate → rolling PR → human merge →
> separate server deploy. The GitHub report-generator workflow described below
> has been deleted.

**Prepared:** 2026-08-10 · **Repo:** github.com/ferryhe/climate_monitor_wiki
**Live service:** https://172.31.10.77

---

## 1. What this repo does

`climate_monitor_wiki` is a **self-updating knowledge base on climate risk,
nat-cat insurance, and actuarial research**. It is not just a document store —
it is a retrieval-augmented (RAG) question-answering system with an Obsidian-style
browsing surface.

Three things happen in one repo:

**a) Automated collection.** A scheduled monitor reads
`monitoring/supranational_sources.yaml`, uses the external `web_listening` tool
to crawl supranational organizations (IAIS, ISSB, EIOPA, Swiss Re, World Bank,
WHO, IPCC…), filters for climate/actuarial relevance, and writes one immutable
raw report per run into `sources/`.

**b) Wiki generation.** `scripts/sync_source_wiki.py` turns each raw report into
a curated page in `wiki/` and rebuilds `wiki/index.md`. Alongside the dated
report pages sit 12 hand-curated **topic pages** (`secondary-perils`,
`parametric-insurance`, `nat-cat-protection-gap`, `isbb-ifrs-s2`,
`actuaries-climate-index`, …) interlinked with Obsidian `[[wikilinks]]`.

**c) Agentic retrieval + UI.** `agentic_wiki/` indexes **both** the curated
`wiki/` pages and the raw `sources/` reports, chunks them, plans retrieval,
ranks evidence, and synthesizes **cited** answers. `api_server.py` (FastAPI)
serves this plus a static frontend in `showcase/`.

### Architecture

- `sources/` — raw monitoring reports, immutable, one `.md` per report date
- `wiki/` — generated dated pages + curated topic pages (Obsidian vault)
- `agentic_wiki/` — mixed-corpus retrieval and answer synthesis
- `showcase/` — two-tab web workspace: **Chat** and **Obsidian**
  (Dataview + Note Detail, then Graph View with Notes/Keywords modes)
- `api_server.py` — `GET /api/config`, `POST /api/chat`, `POST /api/reload`
- `.obsidian/plugins/climate-agent-chat/` — side-panel chat plugin hitting the same API
- `tests/` — 62 regression tests over indexing, retrieval, answer modes, API contract

### Notable design points

- **Answer modes:** `Brief` (tight), `Detailed` (pulls harder from raw sources),
  `Report` (theme-clustered, date-coverage-aware).
- **Context-aware retrieval:** the note you have open is passed as `contextPath`
  so retrieval prioritizes the page you are reading.
- **Date-range expansion:** "past 7 days" / "this month" are expanded into exact
  report dates against the real corpus, rather than returning one stray report.
- **Graceful degradation:** with no `OPENAI_API_KEY` it still runs, returning
  extractive cited answers instead of synthesized prose.

---

## 2. Weekly reports now feed the repo

Cloned to `/home/ubuntu/climate_monitor_wiki`. New script
**`scripts/ingest_weekly_reports.py`** copies reports from the weekly monitoring
job's output (`/home/ubuntu/web_listening/data/reports/`) into `sources/` and
regenerates the wiki.

One judgement call worth flagging: the report directory holds **7** files, but
only **3** are real weekly runs. The others (Sun/Tue/Fri dates) are manual
re-runs of the same monitoring week. Ingesting all of them would create several
"weekly" pages per week, so the script takes **Monday-dated reports only** by
default (`--allow-offcycle` / `--date` to override).

Ingested: `2026-07-27`, `2026-08-03`, `2026-08-10`.

---

## 3. Daily → weekly adaptation

**The core problem.** The sync script expanded a *contiguous date range*: a page
for every calendar day between the first and last report, with placeholders for
missing days. Correct for a daily cadence — a missing weekday is a real gap.
Under a weekly cadence it manufactures **six phantom pages per week**. Before
the fix the index carried ~50 bogus "No report" rows and the retrieval corpus
was full of empty documents.

**Changes made:**

1. **Cadence flag** — `--cadence daily|weekly` (also `CLIMATE_WIKI_CADENCE`).
   Library default stays `daily`, so nothing existing changed behaviour.
2. **Weekly = only real dates.** No grid at all. A 7-day grid was tried and
   rejected: the corpus mixes April's daily run with the current weekly run, so
   any synthetic grid is wrong at the boundary.
3. **Pruning** of legacy sourceless placeholder pages (`--keep-sourceless` opts out).
4. **Labels** — `## Weekly Reports`, "weekly report pages", `#weekly-report` tags.

**Result:** 24 pages / 24 sources / **0 missing** (was 74 pages / 50 phantoms).

**One pre-existing test had to be corrected.**
`test_past_week_daily_summary_covers_requested_window_offline` asserted all 7
days of a "past 7 days" window are in the corpus — an assertion about the
*ingest schedule*, not about retrieval, and false by construction under a weekly
cadence. It now asserts the real contract: every report that *exists* in the
window is covered, and the latest is included. Four new tests cover weekly
behaviour, including a regression guard that daily cadence still fills gaps.

**Test suite: 62 passed.**

---

## 4. Scheduled wiki generation

Two **separate** jobs, so a monitoring failure cannot corrupt the wiki:

- **Weekly Climate & Actuarial Monitor** (`f5259a8ec2d9`) — Mon 08:00 UTC —
  crawls 57 sites, writes the raw report.
- **Weekly Climate Wiki Rebuild** (`dccb79cd69bc`) — Mon 10:00 UTC — ingests
  that report, regenerates the wiki, commits, reloads the live service, health-checks.

The 2-hour offset covers the monitor's ~6 min of crawling plus retries. If no
new Monday report exists, the rebuild is a clean no-op rather than a failure.
The job is pinned to `nous/tencent/hy3` to avoid the scheduler's spend-guard
drift-skip.

Both the job and manual use run one entry point:
`bash scripts/weekly_wiki_refresh.sh`.

---

## 5. Docker + Caddy HTTPS deployment

Docker 29.7.2 installed; Caddy runs as a container (no host package needed).

- `climate-wiki-app` — built from `Dockerfile`, uvicorn on 8501, internal only
- `climate-wiki-caddy` — `caddy:2-alpine`, TLS on 443, HTTP→HTTPS on 80

`wiki/` and `sources/` are bind-mounted read-only, so a host-side ingest is
picked up via `POST /api/reload` with no image rebuild.

**Verified live:**
- `https://172.31.10.77/api/config` → **200**
- `https://172.31.10.77/` → **200**
- `http://172.31.10.77/` → **301** → https
- Chat API returns cited answers sourced from the new `2026-08-10` weekly report

**Two real problems hit and fixed:**

1. **Bare-IP TLS failed.** RFC 6066 forbids IP literals in SNI, so clients
   connecting to `https://172.31.10.77` send *no* SNI; Caddy could not match a
   site block and aborted with `tlsv1 alert internal error`. Fixed with
   `default_sni` in the global options block.
2. **`/api/reload` returned 403** — it is localhost-only unless `RELOAD_TOKEN`
   is set. Generated a token into a gitignored, `chmod 600` `.env`; the refresh
   script now sources it and falls back to a container restart if reload fails.

**Certificate:** issued by Caddy's internal CA, so browsers warn until the root
is imported:
`sudo docker cp climate-wiki-caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt`
The TLS itself is genuine. Swap in a real domain + email in the `Caddyfile` for
public exposure.

---

## 6. End-to-end verification

Rather than testing stages in isolation, a simulated `2026-08-17` report was
dropped into the monitoring output directory and the pipeline run for real:

```
ingest: copied=1  + climate-monitor-2026-08-17.md
sync:   latest=2026-08-17 pages=25 sources=25 created=1 missing=0
commit: docs: weekly climate monitor update (2026-08-17)
reload ok
api/config -> 200
```

A live HTTPS chat query then returned `2026-08-17` among its cited sources —
confirming the whole chain: **new report → sources → wiki page → git commit →
service reload → live query**. The fixture was then removed and the repo
returned to a clean state (62 tests still passing).

---

## 7. Open items

- **Not pushed to GitHub.** All work is committed locally on `main` in
  `/home/ubuntu/climate_monitor_wiki`. Push needs your credentials / a decision
  on branch-vs-PR.
- **Feishu doc creation still blocked.** The Hermes app
  (`cli_aac5056da878dbcb`) lacks `docx:document` + `docx:document:create`, so
  this report is delivered as a file rather than a native Feishu doc. Grant at:
  `open.feishu.cn/page/scope-apply?clientID=cli_aac5056da878dbcb&scopes=docx%3Adocument%2Cdocx%3Adocument%3Acreate`
- **GitHub Actions workflow untouched** — still on the daily-ish
  `30 10 * * 1-5`. Independent of the local pipeline; change to `30 10 * * 1`
  if you want CI aligned to weekly.
- **HTTPS is on the private IP** `172.31.10.77`. For access beyond this LAN you
  need a domain or tunnel, at which point Caddy can get a real Let's Encrypt cert.
