#!/usr/bin/env bash
# Hermes cron wrapper for the 10:30 UTC Monday Registry slot.
#
# The Registry run only fires after a successful human merge + deploy of
# the publisher's rolling branch. Until that gate clears, this wrapper
# writes scheduler-status with state=not_dispatched and
# result_code=awaiting_human_merge_deploy so /api/job-status reflects the
# real gate without lying that the job ran.
#
# Reads env: REPORT_DATE, CLIMATE_JOB_STATUS_DIR, optionally
# CLIMATE_REGISTRY_ENABLE=1 to opt into the dry-run-gated sync (still does
# NOT write to the production Registry DB without an explicit operator
# override).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
PY="${PYTHON:-$REPO/.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python)"
fi

: "${REPORT_DATE:?REPORT_DATE is required (YYYY-MM-DD)}"
: "${CLIMATE_JOB_STATUS_DIR:?CLIMATE_JOB_STATUS_DIR is required}"

scheduled_for="${REPORT_DATE}T10:30:00Z"

"$PY" "$REPO/scripts/scheduler_status.py" \
  --name registry --state not_dispatched \
  --scheduled-for "$scheduled_for" \
  --result-code awaiting_human_merge_deploy \
  --status-dir "$CLIMATE_JOB_STATUS_DIR"

# Dry-run-only opt-in. Real Registry writes still require a separate
# operator authorization; see docs/weekly-registry-automation.md.
if [[ "${CLIMATE_REGISTRY_ENABLE:-0}" == "1" ]]; then
  echo "Registry dry-run-gated: env CLIMATE_REGISTRY_ENABLE=1 is set but" >&2
  echo "Registry writes require an explicit operator override that has not" >&2
  echo "been authorised for this run. Exiting without writing." >&2
  exit 2
fi

exit 0