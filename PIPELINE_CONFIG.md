# Pipeline Configuration

The repository runs **one** weekly pipeline today: a four-slot Hermеs cron
sequence anchored to the single production driver path. The numbered
`stepN_*.py` scripts are kept on disk for test compatibility only
(`tests/test_step1_pillar_a_parser.py`, `tests/test_pipeline_scripts.py`)
and are **not** scheduled.

## Weekly schedule (authoritative)

| # | UTC | Slot        | Hermеs wrapper                            | Entry point invoked                                                  | Result                                                  |
|---|-----|-------------|--------------------------------------------|----------------------------------------------------------------------|---------------------------------------------------------|
| 1 | 08  | `monitor`   | `scripts/hermes_job_monitor.sh`           | `python scripts/run_climate_monitor.py --production-weekly …`        | Monday report Markdown + sidecar + URL-state commit    |
| 2 | 09  | `email`     | `scripts/hermes_job_email.sh`             | `python scripts/record_weekly_run.py` (climate_delivery pipeline)    | PDF + manifest + retained email to the four recipients  |
| 3 | 10  | `publisher` | `scripts/hermes_job_publisher.sh`         | `bash scripts/weekly_wiki_refresh.sh`                                 | Rolling `codex/hermes-weekly-monitor` PR update         |
| 4 | 10:30 | `registry` | `scripts/hermes_job_registry.sh`          | (dry-run only)                                                        | `not_dispatched` until merge + deploy gate is satisfied |

Each Hermes wrapper writes one slot of `scheduler-status.json` (read by
`GET /api/job-status`) via `climate_monitor/scheduler_status.py
update_slot(name, state, …)`. The publisher slot is 2h after monitor so the
report exists before ingest; preserve that gap if you ever re-schedule.

## Data Flow (single chain)

```
Hermes cron
   │
   ├─ 08:00  scripts/hermes_job_monitor.sh
   │         └─ scripts/run_climate_monitor.py --production-weekly
   │              └─ climate_monitor.weekly_monitor.driver.run_weekly_monitor
   │                   └─ climate_monitor.orchestrator.run_monitor
   │                         ├── climate-monitor-{DATE}.md          (Markdown + sidecar)
   │                         ├── climate-monitor-{DATE}.json       (combined candidates)
   │                         ├── article-evidence.v1_{DATE}.json   (AC-1 #93 path)
   │                         └── pending-seen-url delta            (atomic two-phase)
   │
   ├─ 09:00  scripts/hermes_job_email.sh
   │         └─ climate_delivery pipeline
   │              ├── climate-monitor-{DATE}.pdf
   │              ├── manifest + briefing JSON
   │              └── email to the four retained recipients
   │
   ├─ 10:00  scripts/hermes_job_publisher.sh
   │         └─ scripts/weekly_wiki_refresh.sh
   │              └─ scripts/publish_weekly_reports.py
   │                   ├── isolated clone of origin/main
   │                   ├── wiki/ regenerated via sync_source_wiki
   │                   └── codex/hermes-weekly-monitor rolling PR update (CAS rollback)
   │
   └─ 10:30  scripts/hermes_job_registry.sh  [DISABLED — log only]
```

The numeric script names are retained for compatibility, but publication
now precedes the post-deploy Registry sync. Neither script writes
generated report content directly into the production checkout. GitHub
`main` is the common content source for the controlled server and Render.

## Prompt Templates

Hermes LLM prompts that remain in active use are listed below. The legacy
"Step 2 / Step 3b / Step 7" prompt blocks are retained for compatibility
with the still-on-disk step scripts but are **not** invoked by the single
production chain above.

### Monitor (v2 evidence authoring)

The 08:00 monitor runs the v2 authoring path when the orchestrator has
staged an `article-evidence.v1_{DATE}.json` artifact. The driver emits
the v2 authoring request that binds the response to the deterministic
stats dict `{"total": N, "updated": …, "unchanged": …, "blocked": …,
"failed": …, "unresolved": …}` (N must equal `updated + unchanged +
blocked + failed + unresolved`). The driver validates the mapping before
the orchestrator writes any artifact; `MonitorRunResult.stats` exposes
the validated counts (the canonical `57/42/15` split).

### Email (09:00, climate_delivery pipeline)

```

### Step 2: Pillar B Web Search

```
Run Pillar B web search for climate-actuarial intelligence.

Use web_search tool with these queries (run all):
1. "climate change actuarial risk insurance disclosure {YEAR}"
2. "IFRS S2 ISSB climate disclosure actuary {YEAR}"
3. "parametric insurance climate adaptation {YEAR}"
4. "climate risk scenario actuarial {YEAR}"

Base each summary strictly on the search result snippet for that URL; if the
snippet is empty or uninformative, leave summary as "".

Save to: data/reports/pillar_b_{REPORT_DATE}.json
Format: [{{"title":"...","url":"...","source":"web","summary":"..."}}]
```

### Step 3b: Hermes Relevance Filter + Classification

```
Step 3b: Hermes LLM relevance filter + classification + summary generation.

Read: data/reports/aggregated_{REPORT_DATE}.json
Also read: data/reports/conferences_{REPORT_DATE}.json (if exists, pre-extracted conference articles)

For each article, assess if it is TRULY relevant to BOTH climate change AND actuarial risk.

Use web_search to verify articles if needed.

For each article, provide:
- relevant: true/false (must be about BOTH climate AND actuarial/insurance topics)
- categories: an ordered list of one or more of the categories below. The
  FIRST element is the primary category used for report sectioning; later
  elements are secondary themes. Order by relevance, most relevant first.
  * climate_disclosure (reporting standards, ISSB, IFRS S2, TCFD)
  * scenario_analysis (stress testing, ORSA, modelling)
  * catastrophe_natcat (natural disasters, floods, droughts, storms)
  * adaptation_resilience (adaptation, resilience, protection gap)
  * mitigation_energy (renewable, decarbonization, net zero)
  * parametric_insurance (index insurance, cat bonds, weather derivatives)
  * financial_risk (solvency, banking stability, systemic risk)
  * health_mortality (mortality, morbidity, longevity)
  * regulation_standards (regulation, supervision, compliance)
  * biodiversity_nature (biodiversity, nature, ecosystem)
  * conference (conference, meeting, workshop, seminar, event)
  * general (climate-related but not specific)
- category: must equal categories[0] (kept for compatibility)
- summary: 2-4 sentences explaining the article's key points for actuaries
- keywords: 3-5 specific terms from article content

INTEGRITY RULES (mandatory):
1. summary MUST be "" (empty) unless you actually fetched the article content
   with web_search / web_extract and the summary is grounded in that fetched
   content. Never summarize from the title alone.
2. Do not invent keywords that do not appear in the fetched content or the
   title. If you did not fetch the article, limit keywords to terms present
   in the title.
3. If you cannot verify relevance from the title alone, fetch the article
   before marking it relevant.

Also generate a 4-paragraph executive summary STRUCTURED BY CATEGORY:
1. Overall findings (total articles, key themes)
2. Category analysis (for each category with articles: category name, what issues are covered)
3. Actuarial implications (what this means for actuaries)
4. Recommendations for the working group

Save results to: data/reports/hermes_assessments_{REPORT_DATE}.json
```

### Step 7: Email

```
Send the weekly climate monitoring email.

Read: data/reports/climate-monitor-{REPORT_DATE}.md
PDF: climate_delivery_artifacts/{REPORT_DATE}/{SHA}/climate-monitor-{REPORT_DATE}.pdf

Send email with:
- Subject: Weekly Climate & Actuarial Monitor — {REPORT_DATE}
- Body: Executive Summary from MD + link to PDF
- Attachment: PDF
```
