# Climate Monitor Wiki

A structured, interlinked knowledge base on climate risk, natural catastrophe insurance, and actuarial research, compiled weekly from automated monitoring.

**Current target:** independent PDF intake, daily five-site rotation and weekly
search feed activated knowledge; biweekly PDF generation, native Hermes review
and delayed exact-file delivery are separate tasks. Website, search and PDF candidates use the same Registry. T1/T10 improve and
review exact candidate snapshots; automatic final checks publish only unchanged
approved versions. `is_visible` controls their shared public projection. The target is ten cron jobs plus the existing
PDF writer. See [the current deployment contract](docs/biweekly-et-deployment.md).
Repo tests, native identity preflight, isolated real-site rehearsal and production
cycle evidence are separate gates; this description does not claim a cutover.

**Observed production baseline, 2026-10-08:** the public Registry is schema 19
and the legacy separate Runtime routing is still deployed. The schema 22
migration and single-Registry cutover are pending; follow the
[deployment runbook](docs/deployment.md).

**Historical audit, 2026-09-08:** the URL-by-URL monitor and editable discovery/relevance
prompts were implemented and tested in the SSH sandbox. At that audit, production
used the legacy Step jobs; the four-slot deployment below had not been switched on.
Issue #87 was closed by the owner; its closure is not deployment evidence.
Pillar B now validates dated search evidence, and the monitor wrapper records
the report identity required by email. A complete run over the configured sites
and controlled deployment/scheduler verification remain cutover gates. See
[PIPELINE_REFERENCE.md](PIPELINE_REFERENCE.md#verification-and-cutover).

## Web + Obsidian Surfaces

This repo exposes the monitoring corpus through three public web tabs, an
authenticated Hermes operations link, and an Obsidian plugin:

- `Historical Reports` is the default operator archive for weekly narrative
  briefings, monitoring snapshots, PDFs, and their source articles.
- `Chat` uses a minimal single-column conversation layout inspired by `ferryhe/c-ross-2`, but recolored to match the Obsidian workspace.
- `Obsidian` restores the earlier browsing workspace with `Dataview`, `Note Detail`, and `Graph View` for selecting the active retrieval context.
  The page order is now `Dataview + Note Detail` first, then `Graph View`.
  The graph supports `Notes` and `Keywords` modes so you can switch between file links and a source-backed concept map.
  Both graph modes are precomputed by the API so the workspace can render quickly without rebuilding the graph client-side.
- `Hermes` appears only when the existing `/manage` operator session is active.
  It opens the official Hermes Web Dashboard at `/hermes`; it is an operations
  UI and is separate from the public, corpus-grounded retrieval `Chat` tab.
- `.obsidian/plugins/climate-agent-chat/` adds an Obsidian side-panel chat plugin that calls the same local API.

See [docs/ui-surfaces.md](docs/ui-surfaces.md) for the operator interfaces and
[PIPELINE_REFERENCE.md](PIPELINE_REFERENCE.md) for the current module map,
entrypoints and scheduled-job boundaries.

Use current `origin/main` for development and inspect the server checkout and
runtime before deployment. Superseded handoffs remain available in Git history;
they are not a current job inventory or a deployment baseline.

The active note chosen in the web Obsidian tab or the Obsidian plugin is sent as `contextPath`, so retrieval can prioritize the current page during chat.
Chat now also exposes three answer modes:

- `Brief`: faster, tighter synthesis
- `Detailed`: richer answers with more supporting passages from the same public corpus
- `Report`: a theme-clustered, date-coverage-aware report mode tuned for prompts such as `Summarize the past 4 weeks`

The seven starters cover on-demand PDF, dates and opportunities, new articles,
insurance, regulation, physical risk and transition risk. Only an explicit PDF
creation/download request enters the existing on-demand report flow. That PDF
shows its actual date basis and citations; it does not create a formal T4→T5→T6
PASS, send mail or change approved report history.

Dates and opportunities use the website's effective public meeting records.
Publication dates remain separate from approved first-added/material-update
times. Recent additions use 14 New York calendar days ending at the query's
as-of time, or the requested range. Missing chronology, participation details
and partial/conflicting coverage stay explicit. With no model, Chat still
returns cited facts and extracts from the approved Registry or verified Git
export and Wiki. It does not claim autonomous live search.
Source-only mode answers the current question once; it does not resolve objects
or reuse page evidence from previous messages or context. Supply the exact
meeting/article name or URL again when asking another question.

With a configured model, Chat uses Wiki for topic findings, the approved meeting
index for events, and direct URL reads for identified current facts. Its short
`research_state` plan opens the evidence tools; its task/evidence checklist guides
read-only discovery, detail and URL tools using the current question and
server-held ordered identities. It reads results and can continue until the
requested parts have evidence or concrete gaps. Missing facts include manual
verification steps; ambiguous objects can prompt a clarification. Both providers
load the same [research operating manual](agentic_wiki/chat_instructions.md).
Directed URL reads use the governed `web_listening` reader;
search candidates become page evidence only after a successful read. Each turn
allows at most fourteen model calls, sixteen tool calls, four URL reads, two searches,
120 seconds including 20 seconds reserved for final synthesis, and 32,000 evidence characters. Failures return the confirmed part
and the remaining gaps. Response context is an opaque, bounded one-hour memory
handle; website and Obsidian echo it with the assistant message so truncated
history preserves the original meeting order. New chats have no handle, and
expiry/restart can require identifying or rereading a source. Web evidence stays
in that conversation and does not write the Registry or formal sources.

## Runtime

- `api_server.py` serves the Codespaces demo and the `/api/*` API routes.
- `agentic_wiki/` retrieves the shared approved Wiki projection for current
  Registry deployments. Raw reports and old runtime overlays remain legacy
  compatibility inputs; they cannot republish hidden or pending current items.
- `climate_registry/` owns article identity and evidence, intake activation,
  shared Wiki rendering, frozen range reports, historical enrichment and exact
  restore. `CLIMATE_REGISTRY_DB` selects one external business database; runtime
  storage retains queues, task state and derived artifacts.
- `climate_delivery/` owns shared rendering, immutable report revisions, native
  review receipts and exact approved-file delivery. The historical weekly
  summary/PDF/manifest contract remains readable.
- `showcase/` is a static frontend with the shared chat and wiki workspace.

Range-style weekly-report questions such as `Summarize the past 4 weeks`, `Give me an executive report for the past 12 weeks`, or `Summarize reports from 2026-07-27 to 2026-08-10` are anchored to the latest available corpus date. Chat covers the real reports found inside that calendar window and does not treat intervening non-report days as missing updates.

The chatbot can run in two modes:

- **OpenAI mode**: set `OPENAI_API_KEY` in your local `.env` or in your host's environment variables; answers are synthesized by `OPENAI_MODEL`.
- **Claude mode**: set `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`; set `CLIMATE_CHAT_PROVIDER=anthropic` to select it when OpenAI is also configured. OpenAI remains the compatible default when its key is present. Claude uses native Messages tool use and its bounded web-search tool; OpenAI uses Chat Completions tools and Responses search. Provider/search failures are reported and return cited extracts. Capability metadata means configured, not a successful live check. SDK `OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL` settings remain available for compatible deployments.
- **Source-only mode**: no key required; Chat returns approved facts and cited extracts.

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

`sources/` preserves published report history and citation evidence. Add new
reports without rewriting existing archive bytes. For a local rebuild:

1. Add the new report to `sources/`.
2. Regenerate the current weekly pages and `wiki/index.md`:

```bash
REPORT_DATE="<new Monday, YYYY-MM-DD>"
python scripts/sync_source_wiki.py --cadence weekly
python scripts/reload_and_smoke_test.py --date "$REPORT_DATE"
```

3. Confirm the reload and smoke test succeed for that same `REPORT_DATE`.

With `CLIMATE_REGISTRY_DB` configured, sync reads its approved public projection;
`--registry-database` can select an existing external rehearsal copy. New pending
items remain hidden until T1 improvement and T10 review complete. Without either
selector, the legacy sources-only rebuild remains available. Production Git
publication uses the isolated publisher, followed by review/merge and a separate
deployment; rebuilding or reloading is not an approval or cutover. See
[the source-update SOP](docs/source-update-sop.md) and
[deployment](docs/deployment.md).

## Importing PDF reports

The PDF intake adapter retains the original PDF bytes, extracted report text,
source metadata and embedded creation/modification times,
normalizes linked articles and calendar entries, and can add them to the
Registry without fabricating web-fetch or meeting-run records. Without
`--apply`, the CLI prints a dry-run summary and does not write files or change
the Registry. The Python API `climate_monitor.pdf_intake.import_pdf_reports()`
returns the full in-memory bundle. To persist the Registry records and JSON
bundle, create a pre-import backup and pass `--apply`:

```bash
python -m climate_monitor.pdf_intake \
  --input docs/input \
  --output output/pdf-intake.json \
  --registry-db "$CLIMATE_REGISTRY_DB" \
  --backup-dir output/registry-backups \
  --apply
```

Set `CLIMATE_REGISTRY_DB` to the existing external business database first;
current writes require the explicit schema 22 migration in the
[deployment runbook](docs/deployment.md). `--apply` is required for any CLI file
or database write. New automated records remain candidates until T1/T10 review.
The adapter records the PDF hash,
extracted page text, links, dates, summaries and TypeSafe classification
suggestions in PDF-specific Registry tables. Re-imports deduplicate the same
PDF and article/calendar occurrences. `TYPESAFE_API_KEY` is optional; without
it, records are still imported with verbatim extracted content and no AI suggestion.
Linked event identities use event type, title and canonical source URLs, so date
changes become new occurrences under the same identity. A renamed event without
a stable source event ID remains separate instead of being guessed as the same event.

## Modular weekly monitor

The canonical URL is the article identity. Pillar A and Pillar B describe how a
URL was discovered; the combined record retains every origin and source name.
Titles are metadata: different URLs with the same title remain separate articles.

| Module | Program/package | Responsibility |
|---|---|---|
| Site acquisition (Pillar A) | `web_listening` `RuntimeService.explore_site` / `refresh_site` | Governed site exploration, immutable continuation state, actual attempts and climate outcome/manifest bridge artifacts |
| Discovery (Pillar B) | Hermes tools + `weekly_monitor/prompt_loader.py`, `pillar_b_discovery.py` | Execute the editable search task; validate completed queries, report date and article publication evidence |
| Prepare and queue | `scripts/run_climate_monitor.py` | Validate inputs, merge URLs, freeze evidence, run/resume each URL serially |
| Candidate identity | `climate_monitor/article_candidate_contract.py`, `candidate_aggregation.py`, `dedupe.py` | Canonical URLs, merged origins and artifact identities |
| Evidence adapter | `climate_monitor/article_content_adapter.py` + public `Request`/`RuntimeService` | Resolve one prevalidated selected URL under an exact-path scope through the same Runtime root; verify derived content bytes and preserve the full job/result, source/derived identities, attempts and failures |
| Optional page titles | `climate_monitor/article_title.py` | Extract the current page H1/title offline, preserving capitalization |
| Authoring rules and validation | `climate_monitor/weekly_monitor/`, `climate_monitor/taxonomy.py` | Compose relevance rules, validate both relevance decisions, summary, categories and keywords |
| Report/state transaction | `climate_monitor/orchestrator.py`, `seen_state.py` | Finalize the validated Markdown, sidecar, candidate evidence and URL history |
| Delivery | `climate_delivery/` | Reuse the executive narrative, render PDF/manifest and perform retained email delivery |
| Publication | `scripts/publish_weekly_reports.py` | Regenerate wiki in an isolated clone and update the rolling content PR |
| Registry and knowledge | `climate_registry/`, `agentic_wiki/`, `api_server.py` | Pre-report article storage, shared approved projection, immutable report archives, retrieval and post-deploy report association |

### New flow

The article-first flow is documented in
[Article-first architecture](docs/article-first-architecture.md). Registry
acquisition writes happen before report authoring. A report is one output of
the article library; approved knowledge indexing does not require a new report
or a GitHub merge. The scheduler and deployment edges below require separate
live verification.

```mermaid
flowchart LR
    A["web_listening sites"] --> R
    B["Hermes search + governed extraction"] --> R
    P["Management PDF import"] --> R
    R["One CLIMATE_REGISTRY_DB<br/>pending candidates + immutable evidence"] --> I["Existing T1/T10<br/>information improvement + native review"]
    I --> K["Automatic exact snapshot checks<br/>published version + is_visible"]
    K --> C["Same public projection<br/>API + Wiki + RAG + Chat"]
    K --> Q["Frozen date-range report"]
    C --> Q
    Q --> F["HTML/PDF<br/>existing versioned renderer"]
    R --> M["Existing serial monitor<br/>article summaries + executive"]
    M --> S["sources/<br/>report + semantic sidecar"]
    S --> E["climate_delivery<br/>PDF/manifest + retained email"]
    S --> G["Isolated publisher<br/>rolling content PR"]
    K -->|Approved public snapshot| G
    G --> D["Human review + merge + deploy"]
    D --> V["Registry report association<br/>exact identity and coverage gates"]
    H["Management parameters/prompts + Hermes"] -. "independent jobs" .-> K
    H -.-> M
    H -.-> Q
```

`CLIMATE_REGISTRY_DB` selects the only business database. Existing acquisition,
PDF intake, T1 and T10 write there; queues and task files stay on runtime storage.
Public API connections are read-only. All current consumers read immutable
approved versions through the same projection. A later pending update preserves
the old public version. `is_visible=false` removes the canonical article across
all its sources. Archived reports and `sources/` remain immutable.

The future IAA CSC template module is tracked in [#189](https://github.com/ferryhe/climate_monitor_wiki/issues/189)
and follows the user's `docs/input` samples; this change keeps current PDF formats.

Each URL sees only its own evidence. The relevance rules are maintained
separately but included in the same invocation as summary, categories and
keywords. There is no article-count cap. A failed URL does not stop the queue;
it blocks finalization until repaired. Resume revalidates successful checkpoints
and reuses frozen evidence. The executive summary has a separate checkpoint.
See [the runtime limitation](PIPELINE_CONFIG.md#monitor-v2-evidence-authoring):
one Hermes invocation is not a guarantee of one provider API request after a
stream failure.

### Module configuration

- Managed acquisition/search wording: the versioned saved task in `/manage`; tracked prompt files seed the bootstrap. Explicit legacy prompt paths remain compatibility overrides.
- Relevance rules: the same directory's `article-relevance-v1.prompt.md`.
- Categories/semantic constraints: `monitoring/taxonomies/article_categories_v1.yaml`, validated against its versioned identity.
- Page titles: `python -m climate_monitor.article_title saved-page.html`; disable in the monitor with `--no-page-titles` on a fresh prepare.
- Article/executive instructions: the same saved task exposes `article_summary` and `executive_summary`, alongside acquisition, search and relevance prompts. Runs freeze these versions and hashes; see [PIPELINE_CONFIG.md](PIPELINE_CONFIG.md#prompt-templates).
  Not every prompt section has been externalized.

Keep these modules in the existing driver path. Reuse upstream public acquisition
APIs rather than copying reader policy into climate code. Remove imports, old
branches and configuration made unused by a replacement. Retire legacy Step
entrypoints only after their scheduler callers are disabled and equivalent
coverage is verified; the 2026-09-08 audit still found them enabled.

### Run and deployment references

[PIPELINE_REFERENCE.md](PIPELINE_REFERENCE.md) owns the detailed program/artifact
map, executable monitor command, resume behavior, compatibility boundary and
cutover evidence. [PIPELINE_CONFIG.md](PIPELINE_CONFIG.md) owns editable prompts,
runtime configuration and the biweekly Monday 08:00/09:00/10:00/10:30 ET schedule,
anchored to September 14, 2026. See [the ET deployment runbook](docs/biweekly-et-deployment.md).

The 09:00 delivery path owns the PDF/manifest and retained email. Publication
uses **generate → isolated rolling PR → review/merge → deploy**. Registry sync
requires that deployed report identity and explicit write gates. Generation
must use external state/output directories, leaving production `main` clean.
Hermes is the sole report generator; there is no GitHub Actions generator.

## Deploy on Render

This repo includes a [`render.yaml`](render.yaml) Blueprint and a [`.python-version`](.python-version) pin for Render.

If you deploy it as a Render web service, the relevant settings are:

- Build Command: `pip install -r requirements.txt`
- Start Command: `python -m scripts.run_render_web`
- Health Check Path: `/api/health`

The Render start command builds a temporary Registry for the immutable tracked
report history, then installs `wiki/public-registry.json` as the current approved
public snapshot, retaining committed Wiki bodies. If that artifact is absent,
the legacy sources/Wiki bootstrap remains available. This local Registry is
rebuilt on restart or deploy; no production database is committed to Git. If
`CLIMATE_REGISTRY_DB` is configured, the external database is used and bootstrap
is skipped.

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

This is the only Obsidian file shipped in the repo; personal vault
configuration and other community plugins are local-only (gitignored).

To use it:

1. Start the API server with `uvicorn api_server:app --host 0.0.0.0 --port 8501`.
2. Open this folder as an Obsidian vault.
3. Enable **Climate Agent Chat** under Community plugins.
4. Click the message icon or run **Open Climate Agent Chat**.

For the best vault experience, install the following community plugins from
your local Obsidian (they are not bundled with the repo):

- `Dataview`
- `Obsidian Git`

The web workspace mirrors the Dataview browsing model with a Dataview-style
table and graph explorer.
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
- approved current projection retrieval and legacy sources-only compatibility
- `contextPath` ranking behavior
- `brief` vs `detailed` answer-mode behavior
- rolling date-window summary coverage such as `past 7 days`
- `/api/config` metadata needed by graph/dataview
- showcase root HTML contract for the chat and Obsidian tabs
- canonical URL/history selection, frozen evidence and serial authoring recovery
- editable prompts, page titles, uncapped reports and bound delivery semantics

Manual QA notes live in [docs/testing.md](docs/testing.md). UI surface details live in [docs/ui-surfaces.md](docs/ui-surfaces.md).

## Structure

```text
.
├── sources/           # Immutable daily/weekly report archive and citation evidence
├── wiki/              # Approved public Git snapshot, derived pages, topics, and vault content
├── showcase/          # Three-tab static operator workspace
├── agentic_wiki/      # Approved Wiki retrieval with legacy read compatibility
├── climate_registry/  # Article storage, intake, Wiki projection, range snapshots and restore
├── climate_delivery/  # Summary/PDF/manifest and retained email delivery
├── scripts/           # Monitor, publisher, reload, Registry and QA entrypoints
├── tests/             # API, transaction, browser, and regression tests
└── .obsidian/         # Project Obsidian plugin (climate-agent-chat) only
```

## Reports

The report inventory is maintained in [wiki/index.md](wiki/index.md), with
original report content in [sources/](sources/). Weekly rendering
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

Pillar A consumes the configured `web_listening` sites and their reviewed
acquisition scopes. Pillar B uses the editable search prompt to discover
additional articles. See [PIPELINE_CONFIG.md](PIPELINE_CONFIG.md) for configuration
ownership. A report's checked-site count comes from its actual upstream outcome,
not a fixed inventory or the number of discovered articles.

Generated previews and audit output belong in the ignored `/output/` directory
or external runtime storage. Canonical `sources/`, derived `wiki/`, and intentional
test fixtures remain tracked.

### Authenticated acquisition management

The optional `/manage` console edits the single versioned acquisition task,
starts or resumes it through Hermes, and reads progress from the #112 Registry
contract. It is disabled for login until `CLIMATE_CONSOLE_USERNAME`,
`CLIMATE_CONSOLE_PASSWORD_HASH`, and a stable `CLIMATE_CONSOLE_SESSION_SECRET` are
provided server-side. The shipped Compose deployment forwards those values,
defaults `CLIMATE_CONSOLE_SESSION_SECONDS` to 1800, forces
`CLIMATE_CONSOLE_SECURE_COOKIE=true`, and fails at container startup if the
credentials/signing secret are empty or secure cookies are disabled. Generate the
Argon2 hash without putting the plaintext password in Compose or shell history:

```bash
python -c 'from fastapi_users.password import PasswordHelper; import getpass; print(PasswordHelper().hash(getpass.getpass()))'
```

Set the printed value as `CLIMATE_CONSOLE_PASSWORD_HASH`. Credentials are never
returned to the browser. Put
`CLIMATE_TASK_CONFIG`, `CLIMATE_TASK_VERSION_DIR`, and
`CLIMATE_ACQUISITION_RUN_DIR` on persistent external storage in an operated
deployment. The repository bootstrap marker imports the former five prompt
locations; the first save writes one self-contained, atomically replaced state.

```text
authenticated browser (/manage)
    │ config/version/preview/save/restore + start/resume/status
    ▼
FastAPI Users + signed JWT cookie ───► versioned task definition
    │                                      │ immutable version + hashes
    │ detached start                       ▼
    └──────────────────────────────► Hermes Agent (`hermes chat`)
                                           │ native search/browser tools
                                           ▼
existing web-listening adapter + climate_registry.acquisition (#112)
                                           │ exact batch/content versions
                                           ▼
frozen report input ──► existing monitor authoring/checkpoint/publish gates
```

#### Manual ingest-only activation

An authenticated operator can choose **Start ingest-only run** in `/manage`
(the equivalent API request is `POST /api/manage/runs` with
`{"mode":"ingest_only"}`). It uses the normal governed acquisition and frozen
batch to write pending candidates to the same Registry. T1 improvement and T10
review must complete before the approved projection is indexed and available in
Wiki/Chat. This mode creates no weekly report, PDF, email, or rolling PR.
The default manual request and every scheduled start
remain report mode; the delivery times and deployment boundary do not change.
The wiki-side producer retains its immutable request and validated snapshot in
the shared intake queue. The existing `pdf-intake-writer` handles both PDF and
web activation and still owns the writable runtime Wiki mount; that mount is
derived output, not a second business database.

Progress keeps `pending_review`, `acquisition_complete`, `indexed`, and
`chat_ready` distinct. Intake completion alone does not make a candidate public.
If indexing or Chat activation fails, **Resume frozen run** retries post-processing
from the saved batch without fetching it again. Current range reports and the
publisher use the shared approved Registry projection. Legacy active manifests
remain readable for historical contracts. The Render entrypoint imports the
committed `wiki/public-registry.json` public display snapshot into its existing
temporary Registry, retaining approved Git Wiki bodies without a production DB.
See [deployment](docs/deployment.md#explicit-management-pdf-imports).

There is no separate console queue, search wrapper, second executor, or report/publish
bypass. Concurrent start/resume requests for the shared
monitor state are rejected by an interprocess lock; the worker owns that lock
from collection through report finalization. The scheduler snapshot remains separate from live
acquisition status. The Python 3.12 image installs `web-listening` 0.1.0 from
`web_listening_new` revision `ac2343f89bc7939736d85f049ebe2beac571034a` and an
editable Hermes Agent 0.20.5 checkout from immutable source commits; Hermes is
MIT-licensed, while `web-listening` is an internal source dependency with no
license metadata declared at the pinned revision. The Compose runtime volume is
seeded with the task definition only when empty. A separate whole-root volume
persists upstream acquisition jobs, artifacts, lifecycle state and the qualified
browser runtime across container replacement. This diagram describes
implemented repository code, **not** evidence that production credentials,
storage, or schedules were deployed. See
[PIPELINE_CONFIG.md](PIPELINE_CONFIG.md) and [PIPELINE_REFERENCE.md](PIPELINE_REFERENCE.md)
for those operational gates.

### Authenticated Hermes operations UI

`/hermes` is a same-site reverse proxy to the **official** Hermes Web Dashboard,
not a replacement chat frontend. It reuses exactly the `/manage` FastAPI Users
login and `climate_console_session` cookie; there is no second account, login,
role, configuration form, or executor. Anonymous page requests redirect to
`/manage/login?next=/hermes`, while anonymous Dashboard HTTP and WebSocket
requests fail closed. Logging out revokes the presented session for subsequent
HTTP operations and closes its existing proxied WebSockets; token expiry is
also rechecked while a WebSocket is open.

Every successful login receives a cryptographically random JWT `jti`. Active
session IDs are allowlisted in `/app/output/console-sessions.sqlite3`, on the
same `climate_runtime` deployment volume. Logout deletes only the presented ID;
all workers and replacement containers consult the same SQLite store, so an old
cookie cannot be restored by an immediate relogin, process restart, or another
worker. Tokens issued by the earlier stateless implementation have no `jti` and
fail closed. The login page, cookie name, and `/manage` UX from #94 are unchanged.

The Compose image pins Hermes Agent commit
`5538bd1f933be2e94aca9755deca5cc59cccc553` (`pyproject.toml` version `0.20.5`)
and Node `22.22.0`. At image build time it installs the upstream `web` and `pty`
extras and builds both the official React Dashboard and TUI bundle. Dashboard
startup is deliberately opt-in so upgrading an existing Wiki/Chat deployment
without the new origin setting cannot stop the application. Provision both
values atomically in `.env` before recreating the service:

```text
HERMES_DASHBOARD_ENABLED=1
CLIMATE_PUBLIC_ORIGIN=https://climate.example
```

The base Compose service continues to set `HERMES_HOME=/app/output/hermes`, as it
did before the Dashboard integration. That directory is already inside the
persistent `climate_runtime` volume, so `/manage` jobs keep the same profiles and
sessions whether the Dashboard is disabled or enabled. No home migration is
required for an existing base Compose installation.

When enabled, the existing container entrypoint starts it with:

```text
HERMES_HOME=/app/output/hermes \
CLIMATE_PUBLIC_ORIGIN=https://climate.example \
python -m climate_monitor.hermes_dashboard_server
```

Only FastAPI can reach that loopback listener. Caddy exposes FastAPI on 80/443
and has no route or published port to 9119. FastAPI removes the `/hermes` prefix,
adds `X-Forwarded-Prefix: /hermes`, and proxies page assets, REST calls and all
Dashboard WebSockets. The pinned upstream explicitly supports that prefix and
provides Chat over PTY/WebSocket, structured tool events, recent/session lists,
and session continuation. Those are upstream capabilities rather than locally
duplicated forms.

`CLIMATE_PUBLIC_ORIGIN` is a required contract only when
`HERMES_DASHBOARD_ENABLED=1`: set it to the exact
browser-facing HTTPS origin (scheme plus authority, with no path, credentials,
query, or fragment). It is **not** inferred from `Host`, `Forwarded`, or
`X-Forwarded-*`. Hermes 0.20.5 otherwise derives an MCP OAuth redirect from
`request.base_url`; setting upstream `dashboard.public_url` would also engage a
second Hermes auth gate even though the service binds to loopback. The small
pinned-version adapter therefore overrides only Hermes' late-bound
`_mcp_oauth_callback_url` seam and produces
`$CLIMATE_PUBLIC_ORIGIN/hermes/api/mcp/oauth/callback/<server>`. Callback server
identifiers are limited to a single 1–128 character RFC 3986 unreserved ASCII
segment; dot segments, slashes, backslashes, percent escapes, and encoded or
double-encoded alternate forms fail closed before any loopback request. It
refuses any Hermes version other than 0.20.5. FastAPI exposes only that
state-protected GET callback without the Strict #94 cookie, because a cross-site
provider redirect does not carry a `SameSite=Strict` cookie; all Dashboard pages,
APIs, and WebSockets remain under the single #94 boundary. The upstream
callback still requires its per-flow opaque OAuth `state` before accepting a code.
Caddy suppresses access-log records for the complete callback path so OAuth
`code` and `state` query values are never persisted in its JSON URI field; other
site requests remain logged normally. Caddy's separate runtime/error logger can
also serialize a request when an upstream is unavailable, so its stdout/stderr
encoder removes the query string from every `request.uri` before Docker retains
it. Request paths and error details remain available, but query parameters are
intentionally unavailable in `docker compose logs caddy`; use the normal Caddy
access log for non-callback request diagnostics that require them. The shipped
Uvicorn process disables its duplicate request access log, so callback query
credentials also stay out of the application container's Docker logs. Uvicorn
lifecycle and error logs remain enabled.

The selected instance is the current/default profile under the dedicated
`HERMES_HOME=/app/output/hermes` directory in this deployment's named
`climate_runtime` volume. Configuration, memory and existing #94 session history
therefore remain in place and survive container/Dashboard restarts without
exposing a host Hermes home or unrelated profile tree. The official Dashboard is
machine-level within that home: profiles deliberately created under this
deployment directory appear in its switcher, but host profiles cannot. The same
home is used by `/manage`-started Hermes work, preserving the existing model
environment and session continuity while keeping this instance explicit.

When Dashboard enablement is omitted (the safe default), or if the enabled
loopback Dashboard is starting or unavailable, authenticated HTML gets a
clear `Hermes Dashboard unavailable` 503 page and API calls get a stable 503
JSON reason. The persistent volume is not modified, so restarting the Compose
service does not discard sessions. This repository change does **not** claim a
production deployment or a real model conversation. A safe runtime check is:

```bash
CLIMATE_REPOSITORY_COMMIT_SHA="$(git rev-parse --verify HEAD)" \
  docker compose build wiki
docker compose run --rm --no-deps --entrypoint sh wiki -c \
  'hermes --version && test -f /opt/hermes-agent/hermes_cli/web_dist/index.html && test -f /opt/hermes-agent/hermes_cli/tui_dist/entry.js && CLIMATE_PUBLIC_ORIGIN=https://climate.example python -c "from climate_monitor.hermes_dashboard_server import oauth_callback_url; assert oauth_callback_url(\"calendar\") == \"https://climate.example/hermes/api/mcp/oauth/callback/calendar\"" && python scripts/validate_pinned_hermes_oauth.py'
```

After an authorized deployment, run the deterministic, non-model access probe
with credentials supplied only through the environment:

```bash
CLIMATE_VALIDATION_USERNAME="$CLIMATE_CONSOLE_USERNAME" \
CLIMATE_VALIDATION_PASSWORD='<operator password>' \
python scripts/validate_issue114_access.py "$CLIMATE_PUBLIC_ORIGIN"
```

It verifies anonymous page/API/WebSocket rejection, shared login, official page
bootstrap, session-list HTTP, an authenticated WebSocket handshake, the
cookie-free callback path with deliberately invalid state, logout, and replay
rejection. It sends no prompt and reports `"model_prompt_sent": false`.

The authorized acceptance operator must still use the browser for the required
real-model check: open `/hermes`, send a harmless prompt such as “Reply with the
current session title and do not use tools,” record the answer; then run a
read-only tool such as listing the current working directory, record the tool
event/result; open Sessions, reopen that session, send “Continue with OK,” and
record the continuation. Do not use mail, publishing, scheduling, file-write, or
other side-effecting tools. Record Hermes `0.20.5`, profile, timestamp, and
session ID. This must be performed later in the authorized real environment;
the repository tests and Docker smoke below are not that evidence.

```mermaid
flowchart LR
    Public["Public browser"] --> Reports["Reports / Registry / Wiki"]
    Public --> RAG["Retrieval Chat<br/>climate corpus only"]
    Operator["Operator browser"] --> Login["Shared #94 /manage login"]
    Login --> Manage["/manage<br/>climate task forms"]
    Login --> Proxy["/hermes<br/>FastAPI HTTP + WS gate"]
    Proxy --> Dashboard["Official Hermes Dashboard<br/>127.0.0.1:9119"]
    Dashboard --> Store["Dedicated /app/output/hermes directory<br/>current profile + sessions"]
```

_Operational documentation updated: 2026-09-11_
