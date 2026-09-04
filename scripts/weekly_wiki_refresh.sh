#!/usr/bin/env bash
# Hermes weekly publisher. It never changes the production checkout: the Python
# publisher clones origin/main into a temporary directory and updates one rolling
# pull-request branch from there.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
PY="${PYTHON:-$REPO/.venv/bin/python}"
REPORT_DIR="${REPORT_DIR:-${CLIMATE_REPORTS_DIR:-/home/ubuntu/web_listening/data/reports}}"
LOCK_FILE="${CLIMATE_PUBLISH_LOCK:-/tmp/climate-monitor-weekly-publisher.lock}"
LEDGER_DIR="${CLIMATE_RUN_LEDGER_DIR:-/var/lib/climate-monitor/weekly-run-ledger}"

ARGS=(
  --production-repo "$REPO"
  --report-dir "$REPORT_DIR"
  --ledger-dir "$LEDGER_DIR"
)
if [[ -n "${CLIMATE_PUBLISH_REGISTRY_DB:-}" ]]; then
  ARGS+=(--registry-database "$CLIMATE_PUBLISH_REGISTRY_DB")
fi
if [[ "${CLIMATE_PUBLISH_ALLOW_OFFCYCLE:-0}" == "1" ]]; then
  ARGS+=(--allow-offcycle)
fi
if [[ -n "${CLIMATE_PUBLISH_REPORT_DATE:-}" ]]; then
  ARGS+=(--date "$CLIMATE_PUBLISH_REPORT_DATE")
fi

exec flock --nonblock "$LOCK_FILE" \
  "$PY" "$REPO/scripts/publish_weekly_reports.py" \
  "${ARGS[@]}"
