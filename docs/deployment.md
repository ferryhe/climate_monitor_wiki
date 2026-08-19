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

`wiki/`, `sources/`, and `article_metadata/` are bind-mounted read-only into the
app container. A host-side report ingest is visible after `POST /api/reload`;
article annotations are read per Registry request. Neither content-only change
requires an image rebuild.

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

The wrapper writes the successful Publisher attempt to the external weekly-run
ledger while it still holds the Publisher lock. Configure the same explicit
ledger and lock paths used by Registry validation; do not add a second external
flat-record writer:

```bash
export CLIMATE_RUN_LEDGER_DIR=/var/lib/climate-monitor/weekly-run-ledger
export CLIMATE_PUBLISH_LOCK=/tmp/climate-monitor-weekly-publisher.lock
bash scripts/weekly_wiki_refresh.sh
```

This is a publication command, not a deployment command. It validates and
imports reports in a temporary clone of the latest `origin/main`, regenerates
the weekly wiki, runs the full checks, and updates the fixed
`codex/hermes-weekly-monitor` pull request. It never changes the production
checkout, reads `.env`, reloads the API, or restarts a container.

Every newly introduced authoritative report is parsed before it is copied.
Each recognized Pillar item must have exactly one attached explicit HTTP(S)
source-link marker; missing, orphaned, multiple, malformed, or ambiguous link
forms fail closed. The copied bytes must retain the SHA validated by that gate.
Candidate and report source URLs must be ASCII HTTP(S) URIs: producers
IDNA-encode Unicode hostnames and percent-encode Unicode path/query/fragment
data with uppercase triplets and no encoded ASCII unreserved characters. Port
tokens use canonical decimal without leading zeroes. Empty or scheme-default
ports, dot segments, noncanonical IPv4/DNS/IDNA 2008 labels, and noncanonical
bracketed IP-literals fail closed. Raw square brackets are reserved for a
canonical IPv6 or valid IPvFuture authority rather than path, query, or
fragment text. The gate deliberately preserves internal path slashes, path
case, path reserved-encoding, transport, `www`, and non-default-port
distinctions. Query ordering remains distinct, but the inherited query
parse/re-encode treats `%2F` and `/`, `%20` and `+`, and `?flag` and `?flag=`
as equivalent.
The Publisher rejects same-pillar or cross-pillar canonical URL duplicates,
exact normalized-title duplicates, and publication-ineligible root/topic
pages. Multiple pending reports share an in-memory history overlay. Existing
reports already in `main` are not revalidated, so a clean no-op remains a
clean no-op.

Registry-backed history checks are opt-in and use a separate host-process
variable (not the web container variable):

```bash
export CLIMATE_PUBLISH_REGISTRY_DB=/external/path/article-registry.sqlite3
bash scripts/weekly_wiki_refresh.sh
```

The wrapper passes `--registry-database` only when that value is non-empty. It
does not source `.env`, guess or print the path, or use `CLIMATE_REGISTRY_DB`.
The configured exact schema-v3 or schema-v4 database must be an immutable, sidecar-free snapshot
whose report filename/SHA identities exactly match `origin/main`'s `sources/`.
It is opened with SQLite read-only URI and `query_only`; a missing, corrupt,
wrong-schema, contract-broken, or out-of-sync snapshot stops publication before
copy, commit, push, or PR mutation.

This application release does not set that host variable or edit the Hermes
prompt. Server integration remains a separate owner-approved change. The
intended operational order is: read the current Registry while selecting and
publishing candidates, merge/deploy the accepted report, then run the existing
`plan-update`/`update` procedure to atomically advance the Registry. A changed
report title or summary is not treated as proof that an external article body
changed; only separately captured external content supports that conclusion.

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
It must be a complete exact schema-v3 or schema-v4 main database with no dependency on WAL, SHM,
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
| Valid exact schema v3 or v4, including an empty Registry | 200 | `available: true` and actual version |
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

## Optional read-only delivery artifacts

Historical Reports can consume an operator-managed delivery artifact tree with
the independent `docker-compose.delivery.yml` override. Set the host path
explicitly; Compose will fail instead of creating a missing directory:

```bash
export CLIMATE_DELIVERY_ARTIFACTS_HOST_DIR=/external/climate-delivery-output

docker compose \
  -f docker-compose.yml \
  -f docker-compose.registry.yml \
  -f docker-compose.delivery.yml \
  config --quiet
```

The web container sees only the artifact output at `/delivery-output`, mounted
read-only. Do not mount the delivery config, recipient list, SMTP environment,
or email state. Omit this override to disable the feature; the app continues to
start and Historical Reports keeps its Markdown fallback. Enabling the mount
does not run delivery, backfill, email, reload, deployment, or any scheduled job.

Roll forward or back by recreating only the application container with the same
set of approved overrides. For a rollback image that predates artifact support,
omit `docker-compose.delivery.yml`; never delete or rewrite the external
artifact tree as part of application rollback.

For local verification, `.venv/bin/python scripts/test_registry_container.py` builds the real
Dockerfile without a source checkout mount and exercises unconfigured, empty,
seeded, missing, corrupt, wrong-schema, read-only, and offline-chat cases.
`.venv/bin/python scripts/test_registry_browser.py` runs deterministic Archive UI states
in a real local Chrome/Chromium. The browser smoke intentionally avoids a
general frontend project: it needs the optional `playwright==1.62.1` Python
package installed into that environment and `SYSTEM_CHROME` when Chrome is not
in a documented default path.

## Optional read-only weekly run status

The weekly run ledger and `/api/update-status` are independent of the Article
Registry. They require a separate external directory and optional Compose
override:

```bash
export CLIMATE_UPDATE_STATUS_HOST_DIR=/external/weekly-run-ledger
docker compose \
  -f docker-compose.yml \
  -f docker-compose.update-status.yml \
  config --quiet
```

Add `-f docker-compose.registry.yml` when the separately configured Article
Registry is enabled; neither override depends on the other.

The override mounts the parent directory read-only at `/update-status` with
implicit host-path creation disabled. The app reads fresh immutable JSON files
only; it does not update the ledger. See
[`update-status.md`](update-status.md) for the attempt contract, permissions,
failure reasons, resource limits, writer verification, and app-only rollback.

Enabling this override does not create or change a Monitor, Email, Registry
updater, or Registry capture job. The repo-owned Publisher wrapper is the sole
supported Publisher ledger producer. The legacy repair is complete; any older
external flat-record step must remain disabled so it cannot append
a newer identity-less success. This phase does not change Caddy, Fail2Ban, the
firewall, or any scheduled workflow.

## Optional sanitized scheduler status

`/api/job-status` is a separate read-only projection of scheduler liveness. It
does not read the Hermes database and does not replace `/api/update-status`.
Enable its independent directory mount with:

```bash
export CLIMATE_JOB_STATUS_HOST_DIR=/external/sanitized-job-status
docker compose \
  -f docker-compose.yml \
  -f docker-compose.job-status.yml \
  config --quiet
```

The override mounts the external parent at `/job-status` read-only with host
path creation disabled. It composes independently with the Registry and update
status overrides. See [`job-status.md`](job-status.md) for the strict snapshot
contract, API responses, atomic replacement procedure, and security boundary.

This application phase creates no exporter, systemd timer, Hermes job, or
snapshot. App-only deployment or rollback rebuilds/recreates only Wiki; it does
not restart Caddy or alter the confirmed 08:00/09:00/10:00 jobs. It also does
not create or verify the 10:30 Weekly Registry Sync job, which remains a
separate production-completion gate.

## Future weekly Registry sync

The application now contains a supported exact-date Registry candidate
transaction and a tested 10:30 operations runner, but no scheduler entry is
created or enabled by this repository. See
[`weekly-registry-automation.md`](weekly-registry-automation.md) for the DB/JSON
precedence decision, explicit paths, dry-run, exit codes, exact backup/restore
boundary, API verification, and the disabled Hermes job draft. Deployment must
keep the enabled 09:00 Email/PDF producer and its four existing recipients
unchanged; the Registry runner never sends mail.

## Ledger-contract rollout and 10:30 gate

This is a server runbook only. Repository development must not execute these
steps against production. This task baseline is `origin/main` at `cf19da8`;
confirm and record the actual deployed commit at run time.

### Stage A — deploy validated fallback coverage

1. Confirm the production checkout is clean, fetch `origin`, and fast-forward
   only to the human-approved merge commit containing Registry schema v4 and
   validated fallback coverage.
2. Rebuild/recreate only the app with the already approved Registry, delivery,
   update-status, and job-status overrides. This application-deployment substep
   does not restart Caddy or alter any Hermes job.
3. Verify `/api/health`, `/api/config`, Registry status, Article Detail DB-first
   and JSON-fallback behavior, and Historical Report detail/PDF responses for
   `2026-07-27`, `2026-08-03`, `2026-08-10`, and `2026-08-17`.
4. Before the next 10:00 run and under separate scheduler-change authorization,
   inspect its Hermes command, deploy the repo wrapper as the sole Publisher
   recorder, and remove or disable any external flat ledger assembler. Do not
   create or enable the 10:30 job in this stage.
5. From the verified production checkout, bind the runbook paths to that exact
   checkout and the configured Publisher lock; do not copy a path from an older
   `/srv` or `/home/ubuntu` example:

   ```bash
   CLIMATE_REPO="$(pwd -P)"
   CLIMATE_SOURCE_DIR="$CLIMATE_REPO/sources"
   CLIMATE_PUBLISH_LOCK="${CLIMATE_PUBLISH_LOCK:-/tmp/climate-monitor-weekly-publisher.lock}"
   export CLIMATE_PUBLISH_LOCK
   test -d "$CLIMATE_SOURCE_DIR" && test -f "$CLIMATE_PUBLISH_LOCK"
   ```

### Stage B — completed legacy-ledger validation (historical evidence)

The exact repair has already been applied and validates. Do not repeat it as a
deployment step. Retain the following command and SHA only as the audit recipe
for checking the completed overlay and untouched original attempt/claim.

Run the repair without `--apply`; dry-run is the default:

```bash
.venv/bin/python scripts/repair_publisher_ledger.py \
  --date 2026-08-17 \
  --ledger-dir /var/lib/climate-monitor/weekly-run-ledger \
  --source-dir "$CLIMATE_SOURCE_DIR" \
  --registry-database /var/lib/climate-registry/article-registry.sqlite3 \
  --artifact-root /var/lib/climate-delivery/output \
  --lock-file "$CLIMATE_PUBLISH_LOCK"
```

The expected status is now `already_valid`. Source raw bytes, the Registry report row, the exact artifact
directory, and the manifest report identity must all resolve to:

```text
ed19d7b8c8fbe99a5f66b333b5e2d5fbee63c3f41cf927d79d812888fc333972
```

Dry-run must create no repair overlay, backup, temporary file, or lock. The
pre-existing configured Publisher lock file must remain byte-for-byte unchanged;
the command must not read delivery configuration/state or modify any input.

### Stage C — completed repair invariants (do not reapply)

The raw-hash-bound overlay under `.attempt-repairs` has already been created and
validated without changing the original attempt or private claim. The steps
below describe retained audit/rollback evidence, not pending production work.

Retained evidence records the ledger tree hash, Registry DB hash, artifact
inventory, delivery state, and checkout status from before and after the repair.
It proves that only one raw-hash-bound overlay was added and that the legacy
attempt and private identity hard link were unchanged. Validation must return
`already_valid` without another write.

Rollback is correspondingly narrow: under separate audited authorization,
remove only that exact repair overlay and revalidate the untouched legacy
attempt/claim. Do not edit the attempt, source, Registry DB, artifact, or
delivery state to roll back this projection.

### Stage D — validate weekly-sync

Run `python -m climate_registry weekly-sync` for exact date `2026-08-17` with
the explicit source, Registry, artifact, backup, standard DB-lock, and Publisher
ledger paths documented in
[`weekly-registry-automation.md`](weekly-registry-automation.md). Dry-run must
pass identity preflight. The two prior controlled candidates each observed 21
capture successes and four deterministic publisher-wall 403 failures, then
stopped before promotion with the live DB unchanged. With validated fallback
deployed, expect 21 captured / 4 failed / 4 validated fallback / 0 unresolved
and atomic promotion, or a safe no-op (exit `6`) if those exact resolutions are
already current. Verify DB hashes, fetch/resolution audit rows, API provenance,
Historical Report/PDF views, and absence of email or delivery-state access.

### Stage E — create disabled, then authorize enablement

Only after Stages A–D pass may the server agent create `Weekly Climate Registry
Sync` at `30 10 * * 1`. It must initially remain disabled and must not be run.
Validate the disabled command, working directory, explicit paths, environment,
and alerting. Enablement and the first real execution require separate owner
authorization. Observe at least one normal Monday cycle before declaring
`PRODUCTION COMPLETE`.
