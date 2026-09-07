#!/usr/bin/env bash
# Hermеs cron wrapper for the 10:00 UTC Monday publisher slot.
#
# Calls scripts/weekly_wiki_refresh.sh (isolated clone + rolling PR).
# Reads env: REPORT_DATE, CLIMATE_RUN_LEDGER_DIR, CLIMATE_REPORTS_DIR,
# CLIMATE_JOB_STATUS_DIR.
# Does NOT auto-merge or deploy (the publisher updates the rolling branch;
# human review + merge + deploy are separate, post-merge gates).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
PY="${PYTHON:-$REPO/.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python)"
fi

: "${REPORT_DATE:?REPORT_DATE is required (YYYY-MM-DD)}"
: "${CLIMATE_RUN_LEDGER_DIR:?CLIMATE_RUN_LEDGER_DIR is required}"
: "${CLIMATE_REPORTS_DIR:?CLIMATE_REPORTS_DIR is required}"
: "${CLIMATE_JOB_STATUS_DIR:?CLIMATE_JOB_STATUS_DIR is required}"

scheduled_for="${REPORT_DATE}T10:00:00Z"

"$PY" "$REPO/scripts/scheduler_status.py" \
  --name publisher --state running \
  --scheduled-for "$scheduled_for" \
  --claimed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --status-dir "$CLIMATE_JOB_STATUS_DIR"

set +e
CLIMATE_RUN_LEDGER_DIR="$CLIMATE_RUN_LEDGER_DIR" \
CLIMATE_REPORTS_DIR="$CLIMATE_REPORTS_DIR" \
CLIMATE_PUBLISH_REPORT_DATE="$REPORT_DATE" \
  bash "$REPO/scripts/weekly_wiki_refresh.sh"
rc=$?
set -e

if [[ $rc -eq 0 ]]; then
  "$PY" "$REPO/scripts/scheduler_status.py" \
    --name publisher --state completed \
    --scheduled-for "$scheduled_for" \
    --claimed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --started-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --finished-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --status-dir "$CLIMATE_JOB_STATUS_DIR"
  exit 0
fi

"$PY" "$REPO/scripts/scheduler_status.py" \
  --name publisher --state failed \
  --scheduled-for "$scheduled_for" \
  --claimed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --started-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --finished-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --status-dir "$CLIMATE_JOB_STATUS_DIR"
exit "$rc"