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
status="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 "$BASE_URL/api/config")"
echo "api/config -> $status"
[ "$status" = "200" ] || exit 1
echo "weekly wiki refresh complete"
