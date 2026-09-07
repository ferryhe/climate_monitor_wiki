#!/usr/bin/env bash
# Hermеs cron wrapper for the 09:00 UTC Monday email/PDF slot.
#
# Reads env: REPORT_DATE, CLIMATE_SOURCE_DIR, CLIMATE_DELIVERY_OUTPUT_DIR,
# CLIMATE_JOB_STATUS_DIR, CLIMATE_DELIV_RE_SUMMARY_OUT (path).
# Writes the scheduler-status snapshot via scripts/scheduler_status.py.
# Does NOT push, reload the API, or send live email in default mode.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
PY="${PYTHON:-$REPO/.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python)"
fi

: "${REPORT_DATE:?REPORT_DATE is required (YYYY-MM-DD)}"
: "${CLIMATE_SOURCE_DIR:?CLIMATE_SOURCE_DIR is required}"
: "${CLIMATE_DELIVERY_OUTPUT_DIR:?CLIMATE_DELIVERY_OUTPUT_DIR is required}"
: "${CLIMATE_JOB_STATUS_DIR:?CLIMATE_JOB_STATUS_DIR is required}"

scheduled_for="${REPORT_DATE}T09:00:00Z"

"$PY" "$REPO/scripts/scheduler_status.py" \
  --name email --state running \
  --scheduled-for "$scheduled_for" \
  --claimed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --status-dir "$CLIMATE_JOB_STATUS_DIR"

set +e
"$PY" -m climate_delivery.cli \
  --report-date "$REPORT_DATE" \
  --source-dir "$CLIMATE_SOURCE_DIR" \
  --output-dir "$CLIMATE_DELIVERY_OUTPUT_DIR"
rc=$?
set -e

if [[ $rc -eq 0 ]]; then
  "$PY" "$REPO/scripts/scheduler_status.py" \
    --name email --state completed \
    --scheduled-for "$scheduled_for" \
    --claimed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --started-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --finished-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --status-dir "$CLIMATE_JOB_STATUS_DIR"
  exit 0
fi

"$PY" "$REPO/scripts/scheduler_status.py" \
  --name email --state failed \
  --scheduled-for "$scheduled_for" \
  --claimed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --started-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --finished-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --status-dir "$CLIMATE_JOB_STATUS_DIR"
exit "$rc"