# Climate Monitor Wiki

A structured, interlinked knowledge base on climate risk, natural catastrophe insurance, and actuarial research, compiled weekly from automated monitoring.

## Web + Obsidian Surfaces

This repo exposes the monitoring corpus through three web tabs plus an Obsidian plugin:

- `Historical Reports` is the default operator archive for weekly narrative
  briefings, monitoring snapshots, PDFs, and their source articles.
- `Chat` uses a minimal single-column conversation layout inspired by `ferryhe/c-ross-2`, but recolored to match the Obsidian workspace.
- `Obsidian` restores the earlier browsing workspace with `Dataview`, `Note Detail`, and `Graph View` for selecting the active retrieval context.
  The page order is now `Dataview + Note Detail` first, then `Graph View`.
  The graph supports `Notes` and `Keywords` modes so you can switch between file links and a source-backed concept map.
  Both graph modes are precomputed by the API so the workspace can render quickly without rebuilding the graph client-side.
- `.obsidian/plugins/climate-agent-chat/` adds an Obsidian side-panel chat plugin that calls the same local API.

See [docs/project-closeout.md](docs/project-closeout.md) for the operator guide,
module map, API/CLI audit, scheduled-job boundaries, and closeout record.

The active note chosen in the web Obsidian tab or the Obsidian plugin is sent as `contextPath`, so retrieval can prioritize the current page during chat.
Chat now also exposes three answer modes:

- `Brief`: faster, tighter synthesis
- `Detailed`: richer answers that pull more aggressively from `sources/` raw reports
- `Report`: a theme-clustered, date-coverage-aware report mode tuned for prompts such as `Summarize the past 4 weeks`

## Runtime

- `api_server.py` serves the Codespaces demo and the `/api/*` API routes.
- `agentic_wiki/` loads both `wiki/*.md` and `sources/*.md`, chunks notes and raw reports, plans retrieval, ranks evidence, and synthesizes cited answers.
- `climate_registry/` owns the historical SQLite Registry, DB-first Article
  Detail enrichment, weekly candidate transaction, and exact restore.
- `climate_delivery/` owns the retained 09:00 summary/PDF/manifest and email
  delivery pipeline.
- `showcase/` is a static frontend with the shared chat and wiki workspace.

Range-style weekly-report questions such as `Summarize the past 4 weeks`, `Give me an executive report for the past 12 weeks`, or `Summarize reports from 2026-07-27 to 2026-08-10` are anchored to the latest available corpus date. Chat covers the real reports found inside that calendar window and does not treat intervening non-report days as missing updates.

The chatbot can run in two modes:

- **OpenAI mode**: set `OPENAI_API_KEY` in your local `.env` or in your host's environment variables; answers are synthesized by `OPENAI_MODEL`.
- **Offline demo mode**: no key required; the app still demonstrates retrieval and cited extractive answers.

## Setup

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Optional model-backed configuration:

```bash
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.4-mini
OPENAI_TEMPERATURE=0.2
SOURCE_DIR=sources
RELOAD_TOKEN=your-shared-secret
```

In GitHub Codespaces, prefer storing `OPENAI_API_KEY` as a Codespaces secret for this repository, then restart the Codespace so the variable is injected into the terminal and API process.

Your local `.env` is for development convenience only. Keep using `.env.example` as the template, and do not commit a real `.env` file.

## Run

```bash
source .venv/bin/activate
uvicorn api_server:app --host 0.0.0.0 --port 8501
```

Open the forwarded Codespaces port `8501`.

- `/` serves the web workspace.
- `GET /api/config` returns wiki metadata, retrieval corpus stats, answer mode defaults, prompt starters, and precomputed graph payloads for the Obsidian workspace.
- `POST /api/chat` runs retrieval + answering.
- `POST /api/reload` reloads the wiki files from disk.

Example API call:

```bash
curl -s http://localhost:8501/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"What are the latest Climate Monitor highlights?","language":"en","answerMode":"detailed"}'
```

## When Sources Update

If `sources/` changes, do the following:

1. Add or update the raw file in `sources/`.
2. Regenerate the weekly report pages and `wiki/index.md`:

```bash
REPORT_DATE="<new Monday, YYYY-MM-DD>"
python scripts/sync_source_wiki.py --cadence weekly
python scripts/reload_and_smoke_test.py --date "$REPORT_DATE"
```

3. Confirm the reload and smoke test succeed for that same `REPORT_DATE`.

The detailed step-by-step workflow lives in [docs/source-update-sop.md](docs/source-update-sop.md).

## Automated Climate Monitor

The scheduled Hermes monitor reads `monitoring/supranational_sources.yaml`, uses `web_listening` as the external acquisition layer, filters climate-related and actuarial-relevant items, and writes a Monday-dated report to its authoritative report directory. At 09:00 UTC, the retained Weekly Climate Email (PDF highlights) job is the only delivery-artifact producer and sends the result to the existing four recipients. At 10:00 UTC, `scripts/weekly_wiki_refresh.sh` invokes the isolated publisher: it clones the latest `origin/main` into a temporary directory, imports all unpublished weekly reports, regenerates the wiki, validates the result, and updates the fixed `codex/hermes-weekly-monitor` pull-request branch.

The application now includes a tested Monday 10:30 Weekly Registry Sync runner,
DB-first Article Detail enrichment, and exact backup/restore. The Hermes task is
still unconfigured and unverified. The tracked `article_metadata/` JSON remains
a compatibility fallback rather than a weekly generated artifact. The system
must not be called `PRODUCTION COMPLETE` until current `main` is deployed and
the 10:30 task has completed a normal weekly run.

The production checkout is never used as a generation workspace. Publication is deliberately split into **generate → rolling PR → human merge → server deploy** so production `main` stays clean and can be fast-forwarded safely.

Isolated local fixture run (all generated state, sources, and wiki pages stay
outside the checkout):

```bash
DRY_RUN_DIR="$(mktemp -d)"
CLIMATE_WIKI_CADENCE=weekly \
python scripts/run_climate_monitor.py \
  --date 2026-05-18 \
  --manifest-fixture monitoring/fixtures/web_listening_manifest_sample.json \
  --research-fixture monitoring/fixtures/research_results_sample.json \
  --state-dir "$DRY_RUN_DIR/state" \
  --source-dir "$DRY_RUN_DIR/sources" \
  --wiki-dir "$DRY_RUN_DIR/wiki" \
  --no-update-seen-state
echo "Fixture outputs: $DRY_RUN_DIR"
```

For an intentional live, mutating run on the controlled server, choose the
Monday report date, install or point to `web_listening`, then opt in explicitly:

```bash
REPORT_DATE="<new Monday, YYYY-MM-DD>"
WEB_LISTENING_PROJECT_PATH=../web_listening \
CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING=1 \
CLIMATE_MONITOR_ENABLE_LIVE_RESEARCH=1 \
CLIMATE_WIKI_CADENCE=weekly \
python scripts/run_climate_monitor.py --date "$REPORT_DATE"
```

Hermes is the sole report generator. There is no GitHub Actions generator. An emergency manual run is performed only on the controlled server with the existing monitor and rolling-PR publisher.

## Deploy on Render

This repo includes a [`render.yaml`](render.yaml) Blueprint and a [`.python-version`](.python-version) pin for Render.

If you deploy it as a Render web service, the relevant settings are:

- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn api_server:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/api/health`

The Blueprint also sets:

- `PYTHON_VERSION=3.12.1`
- `OPENAI_MODEL=gpt-5.4-mini`
- `OPENAI_TEMPERATURE=0.2`
- `WIKI_DIR=wiki`
- `SOURCE_DIR=sources`
- `RELOAD_TOKEN` as a generated secret
- `OPENAI_API_KEY` as a placeholder secret (`sync: false`)

For secrets on Render:

- add `OPENAI_API_KEY` in the Render Environment page, or provide it during the initial Blueprint creation flow
- do not commit a real `.env` file to the repo
- if your key currently exists only as a GitHub or Codespaces secret, add the same value to Render separately

Render's environment variable docs describe setting secrets in the Render Dashboard, bulk-importing them from a local `.env`, or declaring placeholders in `render.yaml`.

## Obsidian Integration

The plugin already lives at:

```text
.obsidian/plugins/climate-agent-chat/
```

To use it:

1. Start the API server with `uvicorn api_server:app --host 0.0.0.0 --port 8501`.
2. Open this folder as an Obsidian vault.
3. Enable **Climate Agent Chat** under Community plugins.
4. Click the message icon or run **Open Climate Agent Chat**.

For the best vault experience, keep these Obsidian plugins enabled:

- `Dataview`
- `Obsidian Git`

The vault already includes `Dataview`, and the web workspace now mirrors that browsing model with a Dataview-style table and graph explorer.
The Obsidian plugin now also lets you switch between `Brief`, `Detailed`, and `Report` answers before sending.
For daily report notes, the detail panel's `Source` link opens the matching raw Markdown file under the GitHub repo's `main` branch `sources/` directory.

## Testing

Automated checks:

```bash
source .venv/bin/activate
python -m pytest
node --check showcase/app.js
```

Coverage today focuses on:

- wiki indexing and chunking
- raw `sources/` ingestion into retrieval
- `contextPath` ranking behavior
- `brief` vs `detailed` answer-mode behavior
- rolling date-window summary coverage such as `past 7 days`
- `/api/config` metadata needed by graph/dataview
- showcase root HTML contract for the chat and Obsidian tabs

Manual QA notes live in [docs/testing.md](docs/testing.md). UI surface details live in [docs/ui-surfaces.md](docs/ui-surfaces.md).

## Structure

```text
.
├── sources/           # Canonical daily/weekly reports; append-mostly source of truth
├── wiki/              # Derived report pages, topics, and Obsidian vault content
├── showcase/          # Three-tab static operator workspace
├── agentic_wiki/      # Mixed-corpus retrieval over wiki + raw sources
├── climate_registry/  # Historical Registry, enrichment, weekly sync and restore
├── climate_delivery/  # Summary/PDF/manifest and retained email delivery
├── scripts/           # Monitor, publisher, reload, Registry and QA entrypoints
├── tests/             # API, transaction, browser, and regression tests
└── .obsidian/         # Vault config + local plugin
```

## Reports

25 source-backed report pages are present: 20 legacy daily reports from April,
one June report, and four weekly reports from **2026-07-27 through 2026-08-17**.
Source files in `sources/` contain the original report content. Weekly rendering
shows only dates with a real source report and never manufactures gap pages.

## Key Topics

- [[secondary-perils]] — 92% of nat-cat losses now come from secondary perils
- [[swiss-re-sigma]] — 2025 losses reached $107B; 2026 forecast $148B to $320B
- [[isbb-ifrs-s2]] — IFRS S2 implementation now spans industry updates and practical audit guidance ahead of 2027
- [[parametric-insurance]] — parametric cover is expanding from sovereign flood and cat bonds into retail heatwave and data-center climate-stress use cases
- [[climate-finance]] — 2026 focus has shifted from headline targets to implementing the $1.3T climate-finance pathway while adaptation gaps stay large
- [[actuaries-climate-index]] — ACI is increasingly used for insurance balance-sheet measurement as well as weather-derivatives work
- [[nat-cat-protection-gap]] — 49% gap concentrating risk on sovereigns
- [[iais-climate-risk]] — IAIS Holistic Framework + CLIMADA tool
- [[cas-soa-climate-research]] — CAS $75K RFP; SOA research

## Data Sources

Monitoring reports are sourced from 14 high-priority organizations such as
IAIS, ISSB, EIOPA, and Swiss Re, plus 5 rotating normal-priority organizations
per run via automated monitoring.

_Last updated: 2026-08-17_
