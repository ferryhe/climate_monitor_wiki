#!/usr/bin/env bash
# Weekly wiki refresh: ingest the newest Climate & Actuarial Monitor report into
# the wiki repo, regenerate pages under the WEEKLY cadence, commit, and tell the
# running container to reload its corpus.
#
# Intended to run from cron ~1h after the monitor job (which fires Mon 08:00 UTC).
set -euo pipefail

# Resolve the repo from this script's own location so the script is portable.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
PY="$REPO/.venv/bin/python"

cd "$REPO"

# Load .env BEFORE resolving BASE_URL: it carries SITE_HOST (the address Caddy
# serves on) and RELOAD_TOKEN (/api/reload rejects non-localhost callers
# without it). Resolving BASE_URL first would pin a stale IP and silently send
# reloads and health checks to the wrong host after an address change.
if [ -f "$REPO/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO/.env"
  set +a
fi

# Precedence: explicit BASE_URL > SITE_HOST from .env > loopback fallback.
BASE_URL="${BASE_URL:-https://${SITE_HOST:-127.0.0.1}}"
echo "target: $BASE_URL"

wait_healthy() {
  for _ in $(seq 1 30); do
    if [ "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/api/config")" = "200" ]; then
      return 0
    fi
    sleep 2
  done
  return 1
}

echo "== ingest + sync =="
"$PY" scripts/ingest_weekly_reports.py --commit

echo "== reload running service =="
if curl -sk --max-time 15 -X POST "$BASE_URL/api/reload" \
     ${RELOAD_TOKEN:+-H "X-Reload-Token: $RELOAD_TOKEN"} -o /dev/null -w '%{http_code}\n' \
   | grep -q '^2'; then
  echo "reload ok"
else
  echo "reload failed; restarting container instead"
  sudo docker compose restart wiki
fi

echo "== verify =="
if wait_healthy; then
  echo "api/config -> 200"
else
  echo "api/config never returned 200 after restart" >&2
  exit 1
fi
echo "weekly wiki refresh complete"
