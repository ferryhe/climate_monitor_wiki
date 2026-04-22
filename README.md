# Climate Monitor Wiki

A structured, interlinked knowledge base on climate risk, natural catastrophe insurance, and actuarial research, compiled daily from automated monitoring.

## Web + Obsidian Surfaces

This repo now exposes the same wiki through three aligned surfaces:

- `showcase/` serves a two-tab web workspace.
- `Chat` uses a minimal single-column conversation layout inspired by `ferryhe/c-ross-2`, but recolored to match the Obsidian workspace.
- `Obsidian` restores the earlier browsing workspace with `Dataview`, `Note Detail`, and `Graph View` for selecting the active retrieval context.
  The page order is now `Dataview + Note Detail` first, then `Graph View`.
  The graph supports `Notes` and `Keywords` modes so you can switch between file links and a source-backed concept map.
  Both graph modes are precomputed by the API so the workspace can render quickly without rebuilding the graph client-side.
- `.obsidian/plugins/climate-agent-chat/` adds an Obsidian side-panel chat plugin that calls the same local API.

The active note chosen in the web Obsidian tab or the Obsidian plugin is sent as `contextPath`, so retrieval can prioritize the current page during chat.
Chat now also exposes three answer modes:

- `Brief`: faster, tighter synthesis
- `Detailed`: richer answers that pull more aggressively from `sources/` raw reports
- `Report`: a theme-clustered, date-coverage-aware report mode that works better for prompts such as `Give me a report for this month`

## Runtime

- `api_server.py` serves the Codespaces demo and the `/api/*` API routes.
- `agentic_wiki/` loads both `wiki/*.md` and `sources/*.md`, chunks notes and raw reports, plans retrieval, ranks evidence, and synthesizes cited answers.
- `showcase/` is a static frontend with the shared chat and wiki workspace.

Range-style daily-report questions such as `Summarize the past 7 days of reports`, `Give me a report for this month`, or `Summarize reports from 2026-04-14 to 2026-04-21` are expanded into exact report dates based on the latest available corpus day, so the assistant can cover the requested window more deliberately instead of returning only one or two standout reports.

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
2. If it is a new date, create the matching `wiki/climate-monitor-YYYY-MM-DD.md`.
3. Update `wiki/index.md`.
4. Reload the API and run the smoke test:

```bash
python scripts/reload_and_smoke_test.py --date 2026-04-22
```

The detailed step-by-step workflow lives in [docs/source-update-sop.md](docs/source-update-sop.md).

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
├── sources/           # Raw daily monitoring reports (immutable, one .md per date)
├── wiki/              # Curated topic + daily report pages and Obsidian vault content
├── showcase/          # Static web workspace (chat + graph/dataview explorer)
├── agentic_wiki/      # Mixed-corpus retrieval over wiki + raw sources
├── tests/             # API and retrieval regression tests
└── .obsidian/         # Vault config + local plugin
```

## Daily Reports

22 daily report pages in `wiki/` covering **2026-04-01 through 2026-04-22**.
Source files in `sources/` contain the original report content.

Missing dates: `04-11`, `04-12`, `04-13`, `04-15`, `04-19`

## Key Topics

- [[secondary-perils]] — 92% of nat-cat losses now come from secondary perils
- [[swiss-re-sigma]] — 2025 losses reached $107B; 2026 forecast $148B to $320B
- [[isbb-ifrs-s2]] — IFRS S2 becomes effective in January 2027
- [[parametric-insurance]] — +38% growth; 58% EU protection gap
- [[actuaries-climate-index]] — ACI extended to weather derivatives
- [[nat-cat-protection-gap]] — 49% gap concentrating risk on sovereigns
- [[iais-climate-risk]] — IAIS Holistic Framework + CLIMADA tool
- [[cas-soa-climate-research]] — CAS $75K RFP; SOA research

## Data Sources

Daily reports are sourced from 14 high-priority organizations such as IAIS, ISSB, EIOPA, and Swiss Re, plus 5 rotating normal-priority organizations via automated monitoring.

_Last updated: 2026-04-22_
