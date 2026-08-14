# Deployment: Docker + Caddy HTTPS

The wiki runs as two containers behind Caddy. The public site is available at
`https://climate.aiinforsearch.com` with Caddy-managed public-CA TLS. The host's
private IP remains available for internal health checks with Caddy's internal
CA.

Current host IP: **172.31.10.77**

## Stack

| Container | Image | Role |
|---|---|---|
| `climate-wiki-app` | built from `Dockerfile` | uvicorn serving `api_server:app` on 8501 (internal only) |
| `climate-wiki-caddy` | `caddy:2-alpine` | TLS termination on 443, HTTP→HTTPS redirect on 80, reverse proxy to the app |

`wiki/` and `sources/` are bind-mounted read-only into the app container, so a
host-side ingest is visible to the service immediately after `POST /api/reload`
— no image rebuild required.

## First run

```bash
cd /home/ubuntu/climate_monitor_wiki

# Generate the reload token + pin the host IP (file is gitignored, chmod 600).
printf 'SITE_HOST=172.31.10.77\nRELOAD_TOKEN=%s\n' "$(openssl rand -hex 24)" > .env
chmod 600 .env

sudo docker compose build
sudo docker compose up -d
sudo docker compose ps      # both containers should be Up, app "(healthy)"
```

Verify:

```bash
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' http://climate.aiinforsearch.com/  # 301 -> HTTPS
curl -s -o /dev/null -w '%{http_code}\n' https://climate.aiinforsearch.com/api/config  # 200
curl -sk -o /dev/null -w '%{http_code}\n' https://172.31.10.77/api/config   # 200
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' http://172.31.10.77/  # 301 -> https
```

## Certificate trust

Caddy automatically obtains and renews a publicly trusted certificate for
`climate.aiinforsearch.com`; DNS must resolve to this host and ports 80/443 must
be reachable for issuance and renewal.

The private-IP site uses Caddy's local CA, so clients connecting to the IP show
a trust warning until its root is imported. Export it with:

```bash
sudo docker cp climate-wiki-caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

Then import `caddy-root.crt` into the OS/browser trust store. `curl -k` skips
this only for private-IP scripted checks. Public checks should not use `-k`.

## Gotcha: bare-IP TLS needs `default_sni`

RFC 6066 forbids IP literals in the TLS SNI extension, so a client connecting to
`https://172.31.10.77` sends **no** SNI. Without a `default_sni` in the global
options block, Caddy cannot match a site block and aborts the handshake with:

```
TLS connect error: error:0A000438:SSL routines::tlsv1 alert internal error
```

The `Caddyfile` sets `default_sni {$SITE_HOST}` to fix this. If you change the
host IP, update `SITE_HOST` in `.env` and `docker compose up -d`.

## Operations

```bash
sudo docker compose logs -f caddy      # TLS / proxy logs
sudo docker compose logs -f wiki       # app logs
sudo docker compose restart wiki       # restart just the app
sudo docker compose down               # stop everything (volumes persist)
```

Access logs are written as JSON to the `caddy_logs` volume at
`/var/log/caddy/access.log`.

## Publishing and deploying weekly content

The Hermes schedule invokes the locked publisher wrapper:

```bash
bash scripts/weekly_wiki_refresh.sh
```

This is a publication command, not a deployment command. It validates and
imports reports in a temporary clone of the latest `origin/main`, regenerates
the weekly wiki, runs the full checks, and updates the fixed
`codex/hermes-weekly-monitor` pull request. It never changes the production
checkout, reads `.env`, reloads the API, or restarts a container.

After human review and merge, the server deployment process may fast-forward a
clean production checkout to `origin/main` and reload content. Code or
dependency changes require rebuilding only the application container. Those
deployment actions are intentionally separate from weekly generation.

Flow: **Hermes generate → rolling PR → human merge → server deploy**. See
[weekly-cadence.md](weekly-cadence.md).

## Optional read-only Article Registry

The base Compose file remains valid without a Registry. In that state the app,
Chat, and Wiki start normally; `/api/registry/status` returns HTTP 503 with
`{"available":false,"reason":"not_configured"}` and the Archive shows a clear
unavailable state.

Registry adoption uses the separate `docker-compose.registry.yml` override.
It mounts one operator-managed directory read-only at `/registry` (outside the
application root `/app`) and
sets the fixed in-container database path. The host directory is configuration,
not repository content:

```bash
export CLIMATE_REGISTRY_HOST_DIR=/home/ubuntu/climate_monitor_data/registry

docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.registry.yml config --quiet
```

Before enabling it, prepare `article-registry.sqlite3` outside the checkout.
It must be a complete schema-v3 main database with no dependency on WAL, SHM,
or rollback-journal sidecars. Perform the publisher/copyright review first and
set articles without public full-text rights to `metadata_only` in the offline
candidate database. The deterministic preflight rejects relative, missing, or
in-repository directories, invalid schemas, corrupt databases, failed SQLite
checks, and any sidecar without creating or changing a database:

```bash
.venv/bin/python -m scripts.preflight_registry --host-dir "$CLIMATE_REGISTRY_HOST_DIR"

find "$CLIMATE_REGISTRY_HOST_DIR" -maxdepth 1 -type f \
  \( -name '*-wal' -o -name '*-shm' -o -name '*-journal' \) -print
chmod 0750 "$CLIMATE_REGISTRY_HOST_DIR"
chmod 0640 "$CLIMATE_REGISTRY_HOST_DIR/article-registry.sqlite3"
```

Expected results are user version `3`, `ok` from both checks, no rows from
`foreign_key_check`, and no sidecar files. Keep the host directory and file
owner-writable by the approved standalone update/capture operator; align the
group/read bits with the container's read identity and do not make them public.
The directory read-only bind is the web application's enforced boundary; the
API additionally uses fresh short-lived SQLite `mode=ro&immutable=1`
connections with `query_only` and never migrates, repairs, or writes the
Registry.

Before building, retain the currently deployed app image under a unique rollback
tag. Then deploy or roll forward only the app container; Caddy and scheduled
jobs do not change:

```bash
ROLLBACK_TAG="climate-monitor-wiki:pre-registry-$(date -u +%Y%m%dT%H%M%SZ)"
docker image tag climate-monitor-wiki:local "$ROLLBACK_TAG"

docker compose -f docker-compose.yml -f docker-compose.registry.yml \
  up -d --build --no-deps wiki

curl --fail-with-body -sS https://climate.aiinforsearch.com/api/registry/status \
  | python3 -c 'import json,sys; data=json.load(sys.stdin); assert data.get("available") is True; print(data)'
```

Status contract:

| Condition | HTTP | Safe reason |
|---|---:|---|
| Valid schema v3, including an empty Registry | 200 | `available: true` |
| No Registry configured | 503 | `not_configured` |
| Missing, unreadable, or corrupt main database | 503 | `database_unavailable` |
| Path inside the checkout or not absolute | 503 | `invalid_location` |
| Wrong/incomplete schema or invalid relationships | 503 | `invalid_schema` |

Responses never contain host paths, SQL, or exception text. `/api/health`, the
home page, and offline Chat remain available when Registry status is 503.

Rollback is app-only. Retag the saved image as the Compose image and recreate
only `wiki`; omit the Registry override if the rollback version predates this
wiring:

```bash
docker image tag "$ROLLBACK_TAG" climate-monitor-wiki:local
docker compose -f docker-compose.yml -f docker-compose.registry.yml \
  up -d --no-build --no-deps --force-recreate wiki
# For a pre-Registry image, use only: docker compose up -d --no-build --no-deps --force-recreate wiki
```

Do not delete or modify the external database during application rollback. This
wiring adds no Hermes job and does not schedule `update` or `capture-enrich`;
those remain explicit, separately reviewed server operations.

For local verification, `.venv/bin/python scripts/test_registry_container.py` builds the real
Dockerfile without a source checkout mount and exercises unconfigured, empty,
seeded, missing, corrupt, wrong-schema, read-only, and offline-chat cases.
`.venv/bin/python scripts/test_registry_browser.py` runs deterministic Archive UI states
in a real local Chrome/Chromium. The browser smoke intentionally avoids a
general frontend project: it needs the optional `playwright==1.62.1` Python
package installed into that environment and `SYSTEM_CHROME` when Chrome is not
in a documented default path.
