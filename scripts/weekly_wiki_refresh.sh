#!/usr/bin/env bash
# Hermes weekly publisher. It never changes the production checkout: the Python
# publisher clones origin/main into a temporary directory and updates one rolling
# pull-request branch from there.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
PY="${PYTHON:-$REPO/.venv/bin/python}"
REPORT_DIR="${REPORT_DIR:-/home/ubuntu/web_listening/data/reports}"
LOCK_FILE="${CLIMATE_PUBLISH_LOCK:-/tmp/climate-monitor-weekly-publisher.lock}"

ARGS=(
  --production-repo "$REPO"
  --report-dir "$REPORT_DIR"
)
if [[ -n "${CLIMATE_PUBLISH_REGISTRY_DB:-}" ]]; then
  ARGS+=(--registry-database "$CLIMATE_PUBLISH_REGISTRY_DB")
fi

exec flock --nonblock "$LOCK_FILE" \
  "$PY" "$REPO/scripts/publish_weekly_reports.py" \
  "${ARGS[@]}"
