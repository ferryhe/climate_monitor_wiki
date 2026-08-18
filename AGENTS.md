# AGENTS.md — climate_monitor_wiki

Operating instructions for any agent working in this repository.

**Scope boundary — read first.** This agent owns the application only:
the Python packages, `scripts/`, `wiki/`, `sources/`, `showcase/`, tests, and
the Docker/Caddy compose stack **as declared in this repo** (`Dockerfile`,
`Caddyfile`, `docker-compose.yml`).

### Fail2Ban and host security are OUT OF SCOPE for this repository

fail2ban, `iptables`/`nftables` (including the `DOCKER-USER` chain), SSH
hardening, and anything under `/etc/` are **host infrastructure**, not
application code. They belong to the host security agent
(`~/.hermes/agents/host-security/AGENTS.md`).

Hard rules:

- **Never add fail2ban or firewall config to this repository** — no
  `deploy/fail2ban/`, no `jail.d/` drop-ins, no mirrored `/etc` files.
- **Never edit `/etc/fail2ban` from an application task.**
- The live and only authoritative config is `/etc/fail2ban`. A repo copy would
  drift silently and be mistaken for the real thing.
- If a task touches host firewall or intrusion prevention, **stop and hand off**.

> A `deploy/fail2ban/` directory briefly existed here and was **removed** before
> the #24 merge. `deploy/` no longer exists in this repo. Do not recreate it for
> security config.

Some host-security work does require an application change — for example a
Caddy log-format change, or setting `trusted_proxies` if a CDN is ever placed in
front of the site (jails currently key on `remote_ip`). Those arrive as a
**request from the security agent** and are made here, in the repo.

---

## Setup

```bash
cd ~/climate_monitor_wiki
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # if absent
.venv/bin/python -m pytest -q
```

**Branch from current `origin/main`.** The verified application baseline is
`6a4b359`; final production completion is still gated on the unconfigured 10:30
Registry/`article_metadata` weekly task. PR #24 (`0587228`) and the old
`feat/weekly-cadence-and-https-deployment` branch are historical and must not be
used as branch bases.

`.env` (gitignored, `chmod 600`, repo root) holds `RELOAD_TOKEN`,
`OPENAI_API_KEY`, and `SITE_HOST` — the host address used in the health-check
URLs below. Read it from there rather than hardcoding an IP.

If **`OPENAI_API_KEY` is empty**, the service runs in offline extractive mode
(`/api/config` reports `agent_mode: "offline"`,
`model: "offline-extractive"`). Answers remain real and cited but are not
LLM-synthesised. This is expected, not a bug. Never infer the current production
mode from this document; check the sanitized `/api/config` response.

---

## What this project is

A weekly climate-risk / actuarial monitoring pipeline that publishes an
interlinked wiki plus a retrieval-augmented chat UI over the reports.

Flow: monitor job writes a dated markdown report → ingest into `sources/` →
generate `wiki/` pages + index → FastAPI serves the wiki and a RAG chat
endpoint → Caddy fronts it over HTTPS.

| Path | Role |
|---|---|
| `climate_monitor/` | Report generation: research, dedupe, report writer |
| `agentic_wiki/` | RAG retrieval and answer synthesis (`wiki_agent.py` is the core) |
| `api_server.py` | FastAPI: `/api/chat`, `/api/config`, `/api/reload` |
| `scripts/` | Operational entrypoints (see below) |
| `showcase/` | Frontend (vanilla JS, no build step) |
| `sources/` | Raw ingested reports — **the source of truth** |
| `wiki/` | Generated pages — derived, safe to regenerate |
| `Dockerfile`, `Caddyfile`, `docker-compose.yml` | Deployment stack (repo root) |

### Entrypoints

```bash
scripts/run_climate_monitor.py     # generate a new report (calls out to LLM/web)
scripts/ingest_weekly_reports.py   # sources/ <- monitoring output, then sync
scripts/publish_weekly_reports.py  # isolated-clone rolling-PR publisher
scripts/sync_source_wiki.py        # regenerate wiki/ pages + index
scripts/weekly_wiki_refresh.sh     # locked Hermes wrapper around the publisher
scripts/reload_and_smoke_test.py   # post-deploy verification
```

`weekly_wiki_refresh.sh` is the one the weekly Hermes job runs. It does not
modify the production checkout or reload the app: it publishes generated files
from a temporary clone to the fixed `codex/hermes-weekly-monitor` pull-request
branch. Merge and deployment are separate, human-controlled steps.

Publication uses a unique temporary candidate ref that is never connected to a
PR. After checking `main`, the publisher promotes it to the rolling branch with
an exact lease and immediately checks `main` again. A race in that short window
is CAS-rolled back before any PR operation. Do not claim ordinary Git pushes can
make the `main` check and rolling update atomic; human review and merge remain
the final safety boundary.

---

## Cadence: weekly, and why that matters

The pipeline was originally **daily** and was migrated to **weekly**. Most bugs
in this repo trace back to leftover daily assumptions.

- The library default is still `cadence = daily`; weekly is opted into
  explicitly via `--cadence weekly` or `CLIMATE_WIKI_CADENCE`. **Do not flip the
  default** — existing tests encode daily behaviour deliberately.
- Weekly renders **only dates that have a real report**. Never reintroduce
  contiguous date-range filling: it manufactures ~6 phantom "No report" pages
  per week and pollutes the retrieval corpus. This was a real bug (74 pages / 50
  phantoms → 24 pages / 0 missing).
- Ingest takes **Monday-dated reports only** by default. The monitoring output
  directory also contains manual re-runs on other weekdays that duplicate the
  same week. `--allow-offcycle` overrides.

Read `docs/weekly-cadence.md` and `docs/weekly-migration-remaining-work.md`
before touching cadence logic.

---

## Non-negotiables

1. **Never fabricate report content.** Every wiki claim traces to a file in
   `sources/`. If a date has no report, it has no page — do not invent one, and
   do not synthesize a placeholder that reads like content.
2. **`sources/` is append-mostly.** Deleting or rewriting a source silently
   changes history and breaks citations. Regenerating `wiki/` is fine.
3. **Secrets stay out of git.** `.env` holds `RELOAD_TOKEN` and
   `OPENAI_API_KEY`; it is gitignored and `chmod 600`. Never commit it, never
   echo its values into logs, tool output, or a PR body.
4. **Do not weaken a test to make it pass.** If a test fails, first establish
   whether the test or the code encodes the wrong assumption, then say which.
5. **Do not push code directly to `main`.** Work on a branch and open a PR.
   Exception: this `AGENTS.md` is maintained directly on `main` by owner request.

---

## Verification — required before claiming done

```bash
.venv/bin/python -m pytest -q          # expect all applicable tests to pass
node --check showcase/app.js           # frontend has no build step
```

Closeout verification on 2026-08-18 collected 872 tests and completed with
859 passed / 13 environment-specific skips on Windows.

For anything touching the running service:

```bash
docker compose ps                       # climate-wiki-app + climate-wiki-caddy healthy
source .env && curl -sk -o /dev/null -w '%{http_code}\n' "https://$SITE_HOST/api/config"   # 200
```

**A change to retrieval logic is not verified by unit tests alone.** Query the
live corpus and inspect what comes back:

```python
from agentic_wiki.wiki_agent import AgenticWikiResponder
r = AgenticWikiResponder(); r.client = None      # offline extractive mode
print(r.kb.latest_date)
res = r.answer("...", language="en", answer_mode="brief")
print(sorted({s.get("date") for s in res["sources"]}, reverse=True))
```

This is how the "past N weeks returns 0 dates" class of bug is caught — the
query still *answers*, it just silently ignores the requested window.

### Frontend duplication trap

`PROMPT_STARTERS` (`wiki_agent.py:208`) is served via `/api/config`
(`wiki_agent.py:1295`). `showcase/app.js:3` defines **`DEFAULT_PROMPT_STARTERS`**
— a different identifier, used only as a *fallback* when `state.promptStarters`
from the API is absent or empty (`app.js:105/627/1263`).

So they diverge only in the offline/degraded path — but that path is what a
user sees when the API is down, so keep them consistent. Note
`tests/test_agentic_wiki.py:140` asserts the literal string
`"DEFAULT_PROMPT_STARTERS"` appears in the served body, so renaming the JS
identifier breaks a test.

---

## Deployment

Two containers: `climate-wiki-app` (uvicorn, internal) behind
`climate-wiki-caddy` (TLS on 443, HTTP→HTTPS on 80). `wiki/` and `sources/` are
bind-mounted read-only, so a host-side ingest is picked up via `POST /api/reload`
without an image rebuild.

Two traps already solved — do not "fix" them back:

- **Bare-IP TLS needs `default_sni`.** RFC 6066 forbids IP literals in SNI, so
  clients send none, Caddy cannot match a site block, and the handshake fails
  with `tlsv1 alert internal error`.
- **`/api/reload` is localhost-only unless `RELOAD_TOKEN` is set.** Deployment
  tooling may call it after a merged content update; the weekly publisher must
  never read `.env`, reload the app, or restart a container.

The production checkout must remain clean and track `origin/main`. Weekly
generation never commits there. The publication path is: Hermes generates a
report → the publisher rebuilds in a temporary clone → rolling PR → human merge
→ a separate server deployment updates and reloads the service.

See `docs/deployment.md`.

---

## Scheduled jobs

| Job | ID | Schedule (UTC) | Status |
|---|---|---|---|
| Weekly Climate & Actuarial Monitor | `f5259a8ec2d9` | Mon 08:00 | Confirmed |
| Weekly Climate Email (PDF highlights) | Not recorded here | Mon 09:00 | Enabled and retained |
| Weekly Climate Wiki Publisher | `dccb79cd69bc` | Mon 10:00 | Confirmed |
| Registry/`article_metadata` weekly task | Not configured | Mon 10:30 | Pending confirmation/configuration |

The publisher runs 2h after the monitor so the report exists before ingest. If
you change one schedule, preserve that gap. It updates one rolling PR; it does
not deploy or write to the production checkout.

These are **Hermes cron jobs** (this host's scheduler), not GitHub Actions.
Inspect with the Hermes `cronjob` tool: `cronjob action=list`.

The 09:00 job is the only delivery-artifact producer. Its email delivery is also
intentionally retained for the existing four recipients. Do not confuse the
historical backfill's no-email guarantee with the normal scheduled email path.

The distinct 10:30 Registry/`article_metadata` weekly automation is not yet
configured or verified. Do not claim `PRODUCTION COMPLETE` until that task has
been created and observed completing a normal weekly run. Do not invent its job
ID or treat the current `/api/job-status` v1 contract as proof that it exists.

### No GitHub report generator

Hermes is the only report generator. The competing GitHub Actions workflow was
deleted; do not recreate a scheduled or manually dispatched generator. An
emergency manual run happens only on the controlled server using the existing
monitor and rolling-PR publisher, with the same Monday report validation.

---

## Remaining work (the next agent's backlog)

`docs/weekly-migration-remaining-work.md` is the backlog, but **verify before
trusting it** — it lists phases that have since landed. Re-check each against
the code rather than assuming it is current.

Already landed — do **not** redo:

- **Week-window parsing** (`Phase 2`) is **fixed**. Verified: `past 2 weeks` →
  14 dates, `last 3 weeks` → 21, `past 4 weeks` → 28. Earlier docs describing
  these as returning 0 dates are stale.
- **Single automated generator and rolling-PR publication** — see above.
- **Runtime latest-date aliases** (`Phase 4`) — `latest` and `today` expand from
  `kb.latest_date`; no corpus date is hardcoded into retrieval aliases.
- **Weekly Chat coverage and prompt starters** (`Phase 5`) — rolling windows
  report only real corpus dates, and the API/frontend presets use 4-week,
  12-week, insurer-implication, and latest-report wording.

Deliberately **not** done: renaming the `"daily"` document type — it is
load-bearing across ranking, `app.js`, CSS, and the Obsidian plugin contract, so
a rename is wide, breaking, and low-reward (Phase 3).

---

## Working style

- **Verify, don't assert.** Run the thing. Paste real output. "Should work" is
  not a result.
- **Report blockers honestly.** A missing credential or failing install is
  reportable as-is; never substitute plausible-looking invented output.
- **Distinguish evidence from inference.** Say which claims you tested and which
  you reasoned about.
- **Push back when the request is wrong.** If an instruction would introduce a
  regression documented here, say so before complying.
- **Prefer the smallest correct change.** This repo has had wide cosmetic
  refactors proposed that would break the Obsidian plugin's API payload; the
  `"daily"` document type is a known misnomer that is deliberately **not**
  renamed because it is load-bearing across ranking, `app.js`, CSS, and the
  plugin contract.
