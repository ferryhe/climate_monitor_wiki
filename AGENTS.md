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

**Branch from `main`.** PR #24 (weekly cadence + Docker/Caddy HTTPS) was
squash-merged into `main` as `0587228`, so `main` is now the complete, current
state. The old `feat/weekly-cadence-and-https-deployment` branch is superseded —
do not branch from it.

`.env` (gitignored, `chmod 600`, repo root) holds `RELOAD_TOKEN`,
`OPENAI_API_KEY`, and `SITE_HOST` — the host address used in the health-check
URLs below. Read it from there rather than hardcoding an IP.

> **`OPENAI_API_KEY` is currently empty**, so the service runs in offline
> extractive mode (`/api/config` reports `agent_mode: "offline"`,
> `model: "offline-extractive"`). Answers are real and cited but not
> LLM-synthesised. This is expected, not a bug — set the key and re-run
> `docker compose up -d` to enable synthesis.

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
scripts/sync_source_wiki.py        # regenerate wiki/ pages + index
scripts/weekly_wiki_refresh.sh     # full pipeline: ingest→sync→commit→reload→verify
scripts/reload_and_smoke_test.py   # post-deploy verification
```

`weekly_wiki_refresh.sh` is the one the weekly cron runs. Prefer it over calling
the steps by hand — it has the correct ordering and health checks.

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
.venv/bin/python -m pytest -q          # expect: 66 passed
node --check showcase/app.js           # frontend has no build step
```

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
- **`/api/reload` is localhost-only unless `RELOAD_TOKEN` is set.** The refresh
  script sources `.env` and falls back to a container restart.

`weekly_wiki_refresh.sh` loads `.env` **before** resolving `BASE_URL` (precedence:
`BASE_URL` > `SITE_HOST` > loopback). Resolving first would pin a stale IP and
silently health-check the wrong host while reporting success.

See `docs/deployment.md`.

---

## Scheduled jobs

| Job | ID | Schedule (UTC) |
|---|---|---|
| Weekly Climate & Actuarial Monitor | `f5259a8ec2d9` | Mon 08:00 |
| Weekly Climate Wiki Rebuild | `dccb79cd69bc` | Mon 10:00 |

The rebuild runs 2h after the monitor so the report exists before ingest. If you
change one schedule, preserve that gap.

These are **Hermes cron jobs** (this host's scheduler), not GitHub Actions.
Inspect with the Hermes `cronjob` tool: `cronjob action=list`.

### GitHub Actions — already aligned

`.github/workflows/climate-monitor.yml` uses `cron: "30 10 * * 1"` (Mondays
only) and passes `--cadence weekly`. It is **not** a competing weekday
generator — that was resolved in commit `1707f94`. Do not "fix" it as if it were
still Mon–Fri.

---

## Remaining work (the next agent's backlog)

`docs/weekly-migration-remaining-work.md` is the backlog, but **verify before
trusting it** — it lists phases that have since landed. Re-check each against
the code rather than assuming it is current.

Confirmed still open (verified against the live corpus):

- **Stale hardcoded dates** (`Phase 4`): `QUERY_ALIASES["latest"]` still pins
  `2026-04-20` while the corpus latest is `2026-08-10`, injecting a four-month-old
  date into retrieval. Should derive from `kb.latest_date` at runtime.
- **Prompt starters assume daily density** (`Phase 5`): "Summarize the past 14
  days" spans ~2 reports under a weekly cadence.

Already landed — do **not** redo:

- **Week-window parsing** (`Phase 2`) is **fixed**. Verified: `past 2 weeks` →
  14 dates, `last 3 weeks` → 21, `past 4 weeks` → 28. Earlier docs describing
  these as returning 0 dates are stale.
- **GitHub Actions alignment** (`Phase 7`) — see above.

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
