#!/usr/bin/env bash
# Weekly wiki refresh: ingest the newest Climate & Actuarial Monitor report into
# the wiki repo, regenerate pages under the WEEKLY cadence, commit, and tell the
# running container to reload its corpus.
#
# Intended to run from cron ~1h after the monitor job (which fires Mon 08:00 UTC).
set -euo pipefail

REPO="/home/ubuntu/climate_monitor_wiki"
PY="$REPO/.venv/bin/python"
BASE_URL="${BASE_URL:-https://172.31.10.77}"

cd "$REPO"

# RELOAD_TOKEN lives in .env (gitignored, chmod 600). /api/reload rejects
# non-localhost callers unless the token is presented.
if [ -f "$REPO/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO/.env"
  set +a
fi

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
     ${RELOAD_TOKEN:+-H "X-Reload-Token: $RELOAD_TOKEN"} -o /tmp/reload.json -w '%{http_code}\n' \
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
