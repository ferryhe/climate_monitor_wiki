# Production readiness and operator guide

**Status:** `PRODUCTION READINESS PENDING — REGISTRY SYNC NOT DEPLOYED OR SCHEDULED`

**Repository baseline:** `main` at `14904db` (weekly Registry implementation merged)

**Verified production deployment:** `main` at `6a4b359`

**Audit date:** 2026-08-18

This is the current handoff document for the application. Historical design
reports remain useful background, but they are not the operating authority.
The source-of-truth runbooks are `docs/deployment.md`,
`docs/weekly-cadence.md`, and the module-specific documents linked below.

## 1. What the project does

Climate Monitor Wiki turns weekly climate-risk and actuarial monitoring into a
source-backed website:

1. an external Hermes monitor produces a canonical Monday Markdown report;
2. the retained 09:00 Weekly Climate Email job is the only delivery-artifact
   producer: it creates the summary/PDF/manifest and sends the PDF highlights
   to the existing four recipients;
3. the isolated publisher imports the report into append-mostly `sources/`,
   regenerates `wiki/`, and updates one rolling pull request;
4. a human merges the reviewed content and a separate server deployment updates
   the application;
5. FastAPI serves the web application, cited retrieval answers, the historical
   article Registry, and validated report artifacts; and
6. Caddy provides public HTTPS in front of the application container.

The Monday 10:30 Weekly Registry Sync implementation and tested runner are now
in `main`, but they have not been deployed, configured in Hermes, or observed in
a normal weekly run. DB-first Article Detail uses Registry enrichment; tracked
`article_metadata/*.json` remains a compatibility fallback and is not a weekly
generated artifact. Until deployment and the first observed run complete, this
project must not be described as `PRODUCTION COMPLETE`.

The website is both a weekly-report archive and a retrieval interface. It lets
an operator verify the published briefing/PDF, trace every report back to its
articles and raw Markdown, ask cited questions across the corpus, and browse the
wiki as an Obsidian-style knowledge graph.

## 2. The three operator interfaces

### Historical Reports

This is the default tab and the primary production-check surface.

1. Select a week in the newest-first list.
2. Confirm the narrative `Executive Summary` and `Monitoring Snapshot` render.
3. Download the PDF and confirm the selected date is correct.
4. Review the report's article list; select an article to inspect provenance and
   captured content.
5. Switch to `Article Database` to search the complete Registry or filter it by
   publisher, pillar, and report date.

This interface reads the external Registry database and delivery artifacts
through read-only mounts. If either optional mount is unavailable, the archive
reports an unavailable state while Chat and Obsidian remain usable.

### Chat

Use Chat for questions that require synthesis across several reports or topic
pages.

- `Brief` gives the shortest cited answer.
- `Detailed` pulls more evidence from raw `sources/` reports.
- `Report` produces an executive-style period summary with themes and report
  coverage. Prompts such as `Summarize the past 4 weeks` include only real
  report dates; they do not invent missing daily pages.

Expand an answer's evidence drawer to audit its sources. A note selected in the
Obsidian tab becomes the active Chat context. The application still returns a
cited extractive answer if model synthesis is unavailable.

### Obsidian

Use Obsidian for corpus exploration and source inspection.

- `Dataview` searches and filters page metadata.
- `Note Detail` previews the selected Markdown and links a weekly note to its
  historical report.
- `Graph View / Notes` shows wiki links.
- `Graph View / Keywords` connects notes to source-backed concepts.
- `Use in Chat` transfers the selected note into Chat as prioritized context.

The tracked Obsidian plugin is a separate desktop integration that calls the
same Chat API; it is not a fourth web tab.

## 3. End-to-end code and data graph

```mermaid
flowchart LR
    MON[Hermes Monitor\nMon 08:00 UTC] -->|canonical Markdown| OUT[External report directory]
    OUT --> DEL[Weekly Climate Email\nMon 09:00 UTC\nretained]
    DEL -->|only artifact producer| ART[(Summary/PDF/manifest\nread-only artifacts)]
    DEL -->|PDF highlights| RECIP[Existing 4 recipients]
    OUT --> PUB[Hermes Publisher\nMon 10:00 UTC]
    PUB --> PLEDGER[(Publisher success ledger)]
    PUB -->|temporary clone| ING[ingest_weekly_reports]
    ING --> SRC[(sources/\nsource of truth)]
    SRC --> SYNC[sync_source_wiki]
    SYNC --> WIKI[(wiki/\nderived pages)]
    PUB --> PR[Rolling GitHub PR]
    PR --> MERGE[Human review and merge]
    MERGE --> DEPLOY[Separate server deploy]

    SRC --> RAG[agentic_wiki retrieval]
    WIKI --> RAG
    RAG --> CHAT[/api/chat]
    CHAT --> CHATUI[Chat]

    SRC --> REGJOB[Weekly Registry Sync\nMon 10:30 UTC\nIMPLEMENTED / NOT SCHEDULED]
    ART --> REGJOB
    PLEDGER[(Publisher success ledger)] --> REGJOB
    REGJOB -->|candidate + target enrichment\natomic promotion| REG
    REG[(External Registry DB\nread-only)] --> REGAPI[/api/registry/*]
    META[(article_metadata/\ncompatibility fallback)] -.-> REGAPI
    ART --> REGAPI
    REGAPI --> HISTORY[Historical Reports]

    WIKI --> CONFIG[/api/config]
    SRC --> CONFIG
    CONFIG --> OBS[Obsidian explorer]

    LEDGER[(Optional weekly-run ledger)] --> UPDATE[/api/update-status]
    SNAP[(Optional sanitized scheduler snapshot)] --> JOB[/api/job-status]

    CADDY[Caddy HTTPS] --> API[FastAPI api_server]
    API --> CHAT
    API --> REGAPI
    API --> CONFIG
    API --> UPDATE
    API --> JOB
```

An interactive implementation graph is generated locally in
`graphify-out/graph.html`; `graphify-out/graph.json` is the machine-readable
version and `graphify-out/GRAPH_REPORT.md` is its audit report. The directory is
intentionally ignored because it is a reproducible analysis artifact, not
runtime application state.

## 4. Module map

| Path | Responsibility | Normal production role |
|---|---|---|
| `api_server.py` | FastAPI routes, static mounts, reload authorization, Registry/artifact projection | Application entrypoint |
| `agentic_wiki/` | Load/chunk `wiki/` and `sources/`, plan and rank retrieval, answer with citations, precompute explorer graphs | Chat and Obsidian backend |
| `climate_monitor/` | Monitor configuration, collection adapter, relevance filtering, dedupe, report writing | Called by the external 08:00 monitor workflow |
| `climate_registry/` | Versioned SQLite schema, DB-first read API, source selection, capture/enrichment, exact-date candidate sync and restore | Historical report/article data layer and future 10:30 implementation |
| `climate_delivery/` | Parse report, build narrative summary, render PDF, email delivery, validate/backfill immutable artifacts | Sole production delivery-artifact producer in the retained 09:00 job; also sends PDF highlights to four recipients |
| `scripts/run_climate_monitor.py` | Monitor CLI adapter | Controlled fixture/manual runs; external Hermes owns production generation |
| `scripts/ingest_weekly_reports.py` | Validate and import Monday reports, then synchronize the wiki | Publisher sub-step |
| `scripts/publish_weekly_reports.py` | Isolated clone, validation, exact-lease rolling-branch promotion, PR management | Core 10:00 publisher logic |
| `scripts/weekly_wiki_refresh.sh` | Locked Hermes wrapper around the publisher | Scheduled publisher entrypoint |
| `scripts/sync_source_wiki.py` | Regenerate dated wiki pages and index from `sources/` | Derived-content builder |
| `scripts/reload_and_smoke_test.py` | Reload a deployed corpus and verify config, page, and Chat | Post-deploy operator check |
| `scripts/preflight_registry.py` | Validate an external Registry directory before read-only mounting | Deployment preflight |
| `scripts/weekly_registry_refresh.py` | Dry-run-gated sync, reload, SHA/membership binding, and API verification | Tested 10:30 runner draft; not scheduled |
| `scripts/record_weekly_run.py` | Append one sanitized immutable attempt to an external ledger | Available contract; producer integration is deferred |
| `scripts/test_registry_container.py` | Isolated Docker Registry integration smoke | Release/manual QA |
| `scripts/test_registry_browser.py` | Optional browser interaction smoke | Release/manual QA |
| `showcase/` | Three-tab, build-free HTML/CSS/JavaScript frontend | Browser UI |
| `sources/` | Canonical report Markdown | Append-mostly source of truth |
| `wiki/` | Generated report pages plus curated topic pages | Safe to regenerate |
| `article_metadata/` | Source-backed historical article annotations | Compatibility fallback; not regenerated by weekly sync |
| `monitoring/` | Source registry, site scopes, monitor configuration and fixtures | Monitor inputs |
| `Dockerfile`, `docker-compose*.yml`, `Caddyfile` | Application container, optional read-only mounts, and HTTPS proxy | Deployment declaration owned by this repo |

## 5. API completeness audit

| Route | Consumer and purpose | Closeout result |
|---|---|---|
| `GET /` | Loads the three-tab web application | Active and tested |
| `GET /api/health` | Lightweight liveness check and alternate-host health contract | Active and tested |
| `GET /api/config` | Chat/Obsidian bootstrap plus the container's deeper readiness check | Active and tested |
| `POST /api/chat` | Web and Obsidian-plugin retrieval/answering | Active and tested |
| `POST /api/reload` | Localhost or token-authorized corpus reload after deployment | Active and tested; intentionally not a publisher action |
| `GET /api/registry/status` | Historical Reports availability/counts | Active when Registry is mounted; tested |
| `GET /api/registry/reports` | Paginated weekly archive | Active and tested |
| `GET /api/registry/reports/{date}` | Report detail plus validated briefing/PDF projection | Active and tested |
| `GET /api/registry/reports/{date}/pdf` | Validated PDF download | Active and tested |
| `GET /api/registry/publishers` | Article filter choices | Active and tested |
| `GET /api/registry/articles` | Search/filter/paginate articles | Active and tested |
| `GET /api/registry/articles/{id}` | Article content, provenance, enrichment, and appearances | Active and tested |
| `GET /api/update-status` | Read-only sanitized attempt ledger | Contract complete; no producer is wired and no current web tab consumes it |
| `GET /api/job-status` | Read-only sanitized scheduler observer snapshot | Contract complete; exporter is deferred and no current web tab consumes it |
| `/wiki`, `/sources`, `/showcase` | Static content mounts | Active; `/sources` remains raw evidence |

FastAPI also exposes its default `/docs`, `/redoc`, and `/openapi.json` routes.
They are not used by the frontend or normal operator workflow. They can be
disabled in a later hardening-only change if no external API consumer relies on
them; that decision is deliberately not mixed into closeout.

A read-only production check on 2026-08-18 returned HTTP 200 for the homepage,
health, config, all Registry list/detail routes, the four historical report
details/PDFs, and the static wiki/source/frontend files. `/api/update-status`
and `/api/job-status` returned their documented 503 `not_configured` state.
The three default API-documentation routes were publicly reachable with HTTP
200. Production Chat and reload were not invoked during this read-only audit;
their behavior is covered by the automated suite, and reload remains a
separately authorized operation.

The apparent overlap between `/api/health` and `/api/config` is intentional:
the former is cheap liveness, while the latter proves that the retrieval corpus
can be constructed and is therefore used as readiness by Docker/Caddy.

## 6. CLI completeness audit

All documented command parsers and subcommand help paths exit successfully when
invoked through their supported module/script form.

| CLI | Commands / role | Keep or simplify |
|---|---|---|
| `scripts/run_climate_monitor.py` | Generate a fixture or controlled report | Keep; production ownership is external but fixtures and emergency runs need it |
| `scripts/ingest_weekly_reports.py` | Ingest Monday reports; optional date/off-cycle/dry-run controls | Keep; publisher dependency |
| `scripts/publish_weekly_reports.py` | Isolated rolling-PR publication | Keep; scheduled core |
| `scripts/sync_source_wiki.py` | Daily/weekly regeneration | Keep both cadences; daily is a compatibility contract |
| `scripts/reload_and_smoke_test.py` | Reload and post-deploy verification | Keep; operator recovery/check |
| `python -m climate_registry` | `audit-history`, `plan-update`, `update`, `plan-selection`, `capture-enrich`, `weekly-sync`, `restore-backup` | Keep; audit, planning, capture, atomic weekly promotion, and exact restore are distinct operations |
| `python -m climate_delivery` | `summarize`, `render-pdf`, `send-email`, `run`, `backfill` | Keep; composable operations plus one full pipeline and audited recovery |
| `python -m scripts.preflight_registry` | Registry mount validation | Keep; deployment safety gate |
| `python -m scripts.record_weekly_run` | Append sanitized attempt | Keep while the optional status contract remains; producer wiring is deferred |
| `scripts/weekly_registry_refresh.py` | Run 10:30 dry-run → formal sync → reload → API verification | Keep as a tested disabled draft until separately scheduled |
| Registry container/browser smoke scripts | Release verification | Keep outside the normal scheduler path |

`send-email` is active through the retained 09:00 Weekly Climate Email job. That
job is both the only delivery-artifact producer and the intentional email
delivery path for the existing four recipients. `backfill` is no longer needed
for the completed historical repair, but remains the deterministic, dry-runnable
recovery tool and must not be replaced with manual artifact edits.

## 7. Server agent jobs

A server agent job is an external Hermes-scheduled process. It is not a FastAPI
route, browser background task, GitHub Action, or container cron process. The
application repository supplies validated entrypoints and contracts; the
server scheduler owns dispatch, credentials, and authoritative execution state.

| Time (UTC) | Job | Status | Responsibility |
|---|---|---|---|
| Monday 08:00 | Weekly Climate & Actuarial Monitor (`f5259a8ec2d9`) | Confirmed | Collect sources and write the canonical Monday report |
| Monday 09:00 | Weekly Climate Email (PDF highlights) | Confirmed; retained | Be the only delivery-artifact producer and intentionally email the existing four recipients |
| Monday 10:00 | Weekly Climate Wiki Publisher (`dccb79cd69bc`) | Confirmed | Import through an isolated clone and update the rolling PR |
| Monday 10:30 | Weekly Registry Sync | Implementation merged; job not configured | Atomically update/enrich the external Registry, reload, and verify; deploy, schedule, and observe before claiming production completion |

These are three distinct operational facts:

- **Delivery artifact automation:** confirmed at 09:00 and retained; no other
  automation should produce the summary/PDF/manifest artifacts.
- **Email delivery:** intentionally retained in that same 09:00 job for the
  existing four recipients.
- **Registry weekly automation:** DB-first implementation and disabled runner are
  merged; deployment, Hermes configuration, and one observed normal run remain.
  There is intentionally no weekly `article_metadata` JSON producer.

The two-hour Monitor→Publisher gap is required. The application does not read
raw Hermes state. `/api/job-status` accepts only a separately produced,
sanitized observer snapshot; `/api/update-status` reads a producer-written,
append-only public ledger. Neither endpoint schedules or proves dispatch by
itself.

## 8. Simplification decisions

Completed at closeout:

- removed one uncalled Publisher helper, `_remote_main_sha`;
- removed one uncalled frontend helper, `titleFromPath`.

Reviewed and deliberately retained:

- the load-bearing `daily` document type and daily cadence default;
- offline extractive answering and frontend prompt-starter fallback;
- Registry audit/update/capture, weekly sync/exact restore, and delivery backfill commands;
- optional job/update status read contracts;
- the Obsidian plugin and bundled vault integration;
- separate liveness and readiness endpoints.

Possible later cleanups, each requiring its own decision and verification:

- disable FastAPI's public interactive API documentation if no external
  consumer needs it;
- retire the alternative `render.yaml` deployment only after confirming it is
  no longer an accepted recovery/deployment target;
- split test-only dependencies from the shared environment if image size becomes
  an objective (the publisher currently benefits from one reproducible set);
- move shared report parsing into a neutral contract module to remove the
  package-level `climate_registry` ↔ `climate_delivery` dependency cycle;
- move wiki synchronization out of `scripts/` into a library module so
  `climate_monitor` no longer imports an operational script.

None of those optional cleanups is part of deploying and scheduling the 10:30
Weekly Registry Sync. They should remain separate so the
production-completion gate stays narrow and verifiable.

Registry-automation verification ran all 913 collected tests (`899 passed`,
`14` platform or optional-environment skips), validated `showcase/app.js` with Node, and
confirmed every supported CLI help path exits successfully. The base Compose
file and the combined Registry, delivery-artifact, update-status, and job-status
overrides also passed `docker compose config --quiet`.

## 9. Accepted production verification record

The owner-provided production verification predates the Registry implementation
merge and records:

- `main 6a4b359` deployed and healthy;
- 2026-07-27, 2026-08-03, 2026-08-10, and 2026-08-17 show narrative Executive
  Summary, Monitoring Snapshot, and PDF download;
- Articles, Detail, report switching, and mobile behavior are normal;
- the old 2026-08-10 artifact is quarantined intact, not deleted;
- the 2026-08-17 artifact, Registry database, delivery state, source code, and
  scheduled tasks were not changed by the historical backfill;
- the historical backfill itself sent no email; this does not pause or disable
  the retained 09:00 weekly email delivery; and
- the post-backfill dry-run reports all target dates as valid.

These checks establish that the deployed website and historical artifact repair
are healthy. Repository `main 14904db` now contains the 10:30 implementation,
but that code has not been deployed and its Hermes task remains unconfigured.
`PRODUCTION COMPLETE` may be claimed only after current `main` is deployed and
the task is created, retained, and observed completing a normal weekly cycle.

## 10. Remaining completion gate and operations

1. Keep the old 2026-08-10 quarantine at mode `700`; an old manifest may contain
   recipient metadata.
2. After the next normal Monday run, verify that the new report automatically
   receives a narrative summary, Monitoring Snapshot, and PDF from the 09:00
   job, and that the PDF highlights email is delivered to the four recipients.
3. Deploy current `main`, confirm the explicit external Registry/artifact/
   ledger/backup paths and permissions, and archive a successful exact-date
   `weekly-sync --dry-run` result.
4. Under separate authorization, configure the Monday 10:30 Weekly Registry
   Sync from the tested runner draft, then verify its Registry, briefing/PDF,
   ordered article membership, and sample Article Detail after a normal run.
   This is required before changing the status to `PRODUCTION COMPLETE`.
5. Wait for at least one successful weekly cycle before requesting separate,
   audited authorization to remove the old quarantine. Do not perform an
   unaudited deletion.

The intended production chain, with the remaining gate shown explicitly, is:

```text
08:00 weekly report
      -> 09:00 summary/PDF/manifest artifact (confirmed, only producer)
      -> 09:00 PDF highlights email (confirmed, 4 recipients)
      -> read-only artifact mount -> Historical Reports
      -> 10:00 rolling Wiki PR
      -> 10:30 Weekly Registry Sync (IMPLEMENTED / NOT DEPLOYED OR SCHEDULED)
      -> Executive Summary + Snapshot + PDF + Articles + Detail
```
