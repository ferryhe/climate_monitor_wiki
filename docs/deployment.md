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
curl -s  -o /dev/null -w '%{http_code} %{redirect_url}\n' http://climate.aiinforsearch.com/  # 301 -> HTTPS
curl -s  -o /dev/null -w '%{http_code}\n' https://climate.aiinforsearch.com/api/config  # 200
curl -sk -o /dev/null -w '%{http_code}\n' https://172.31.10.77/api/config   # 200
curl -s  -o /dev/null -w '%{http_code} %{redirect_url}\n' http://172.31.10.77/  # 301 -> https
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
