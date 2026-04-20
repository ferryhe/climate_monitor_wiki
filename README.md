# Climate Monitor Wiki

A structured, interlinked knowledge base on climate risk, natural catastrophe insurance, and actuarial research — compiled daily by an AI agent from automated monitoring.

## Agentic AI Chatbot

This branch adds a lightweight agentic RAG layer on top of the existing wiki:

- `api_server.py` serves the Codespace web demo and `/api/chat`.
- `agentic_wiki/` loads `wiki/*.md`, chunks pages, plans retrieval queries, searches evidence, reflects once, and synthesizes a cited answer.
- `showcase/` is now a chat-first UI with source inspection and active wiki context.
- `.obsidian/plugins/climate-agent-chat/` adds an Obsidian side-panel chat plugin that calls the same local API.

The chatbot can run in two modes:

- **OpenAI mode**: set `OPENAI_API_KEY` in `.env`; answers are synthesized by `OPENAI_MODEL`.
- **Offline demo mode**: no key required; the app still demonstrates wiki retrieval and cited extractive answers.

### Python virtual environment

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Required Python packages are listed in `requirements.txt`:

- `fastapi`
- `uvicorn[standard]`
- `openai`
- `python-dotenv`
- `pydantic`
- `pytest`

To enable model-backed replies, edit `.env`:

```bash
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.4-mini
```

In GitHub Codespaces, the safer option is to add `OPENAI_API_KEY` as a
**Codespaces secret** with access to this repository. After adding or changing a
Codespaces secret, stop and restart the codespace so the environment variable is
injected into the terminal and API server process.

### Codespace demo

```bash
source .venv/bin/activate
uvicorn api_server:app --host 0.0.0.0 --port 8501
```

Open the forwarded Codespaces port `8501`. The root page serves the chat UI, and the API is available at:

- `GET /api/config`
- `POST /api/chat`
- `POST /api/reload`

Example API call:

```bash
curl -s http://localhost:8501/api/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"最新的气候风险监测有什么重点？","language":"zh"}'
```

### Obsidian integration

The plugin is already placed under:

```text
.obsidian/plugins/climate-agent-chat/
```

To use it:

1. Start the API server with `uvicorn api_server:app --host 0.0.0.0 --port 8501`.
2. Open this folder as an Obsidian vault.
3. Enable **Climate Agent Chat** under Community plugins.
4. Click the message icon or run the command **Open Climate Agent Chat**.

The plugin sends the active note path as `contextPath`, so if a wiki page is open, the agent prioritizes that page during retrieval. The API key stays in the backend `.env`; it is not stored in Obsidian.

## Structure

```
.
├── sources/           # Raw daily monitoring reports (immutable, one .md per date)
└── wiki/              # Curated topic + daily report pages (Obsidian vault)
    ├── index.md       # Master catalog — daily reports + concept pages
    ├── log.md         # Operation history
    └── *.md           # Topic/entity/daily report pages
```

## Daily Reports

20 daily report pages in `wiki/` covering **2026-03-31 through 2026-04-20**.
Source files in `sources/` with full original report content.

**Missing dates** (no cron run / no doc found): 04-11, 04-12, 04-13, 04-15, 04-19

## For Obsidian Users

Open this folder as a vault in Obsidian. The `wiki/` directory is your Obsidian vault. Install the **Dataview** and **Obsidian Git** plugins for the best experience:
- **Dataview**: query pages by tags, dates, links
- **Obsidian Git**: auto-sync changes back to GitHub

## Key Topics

- [[secondary-perils]] — 92% of nat cat losses now from secondary perils
- [[swiss-re-sigma]] — 2025 losses $107B; 2026 forecast $148B–$320B
- [[isbb-ifrs-s2]] — IFRS S2 effective Jan 2027
- [[parametric-insurance]] — +38% growth; 58% EU protection gap
- [[actuaries-climate-index]] — ACI extended to weather derivatives
- [[nat-cat-protection-gap]] — 49% gap concentrating risk on sovereigns
- [[iais-climate-risk]] — IAIS Holistic Framework + CLIMADA tool
- [[cas-soa-climate-research]] — CAS $75K RFP; SOA research

## Data Sources

Daily reports sourced from 14 high-priority orgs (IAIS, ISSB, EIOPA, Swiss Re, etc.) and 5 rotating normal-priority orgs via automated monitoring.

_Last updated: 2026-04-20_
