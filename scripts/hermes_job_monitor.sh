#!/usr/bin/env bash
# Hermes cron wrapper for the 08:00 UTC Monday monitor slot.
#
# Reads env: REPORT_DATE, CLIMATE_SITE_SCOPES (yaml), CLIMATE_STATE_DIR,
# CLIMATE_SOURCE_DIR, CLIMATE_WIKI_DIR, CLIMATE_JOB_STATUS_DIR, optionally
# CLIMATE_FIXT_RE_DIR (pre-staged article-evidence + stats for dry-run).
# Does NOT push, send email, reload the API, or read .env.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
PY="${PYTHON:-$REPO/.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python)"
fi

: "${REPORT_DATE:?REPORT_DATE is required (YYYY-MM-DD)}"
: "${CLIMATE_STATE_DIR:?CLIMATE_STATE_DIR is required (state directory)}"
: "${CLIMATE_SOURCE_DIR:?CLIMATE_SOURCE_DIR is required}"
: "${CLIMATE_WIKI_DIR:?CLIMATE_WIKI_DIR is required}"
: "${CLIMATE_JOB_STATUS_DIR:?CLIMATE_JOB_STATUS_DIR is required}"

# Compute Monday 08:00:00Z for the given report date.
report_iso="$REPORT_DATE"
scheduled_for="${report_iso}T08:00:00Z"

# Locate authoring response + article evidence + stats. Prefer pre-staged
# web_listening #67 outcome files when available; otherwise fall back to the
# canonical 57-record fixture under CLIMATE_FIXT_RE_DIR/fixtures/issue87.
AUTO_FIXT="$REPO/tests/fixtures/issue87/57_record_fixture.json"
authoring_response=""
article_evidence=""
stats=""
if [[ -n "${CLIMATE_FIXT_RE_DIR:-}" && -f "$CLIMATE_FIXT_RE_DIR/fixtures/issue87/57_record_fixture.json" ]]; then
  authoring_response="$CLIMATE_FIXT_RE_DIR/fixtures/issue87/57_record_fixture.json"
elif [[ -f "$AUTO_FIXT" ]]; then
  authoring_response="$AUTO_FIXT"
fi
if [[ -n "${ARTICLE_EVIDENCE:-}" && -f "${ARTICLE_EVIDENCE}" ]]; then
  article_evidence="${ARTICLE_EVIDENCE}"
elif [[ -n "${CLIMATE_FIXT_RE_DIR:-}" && -f "$CLIMATE_FIXT_RE_DIR/fixtures/issue87/57_article_evidence.json" ]]; then
  article_evidence="$CLIMATE_FIXT_RE_DIR/fixtures/issue87/57_article_evidence.json"
fi
if [[ -n "${STATS_JSON:-}" ]]; then
  stats="${STATS_JSON}"
elif [[ -n "${CLIMATE_FIXT_RE_DIR:-}" && -f "$CLIMATE_FIXT_RE_DIR/fixtures/issue87/57_stats.json" ]]; then
  stats="$(cat "$CLIMATE_FIXT_RE_DIR/fixtures/issue87/57_stats.json")"
fi

# Write scheduler-status: scheduled -> running
"$PY" "$REPO/scripts/scheduler_status.py" \
  --name monitor --state running \
  --scheduled-for "$scheduled_for" \
  --claimed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --status-dir "$CLIMATE_JOB_STATUS_DIR"

args=(
  scripts/run_climate_monitor.py
  --production-weekly
  --date "$REPORT_DATE"
  --state-dir "$CLIMATE_STATE_DIR"
  --source-dir "$CLIMATE_SOURCE_DIR"
  --wiki-dir "$CLIMATE_WIKI_DIR"
  --authoring-response "$authoring_response"
)
if [[ -n "$article_evidence" ]]; then
  args+=(--article-evidence "$article_evidence")
fi
if [[ -n "$stats" ]]; then
  args+=(--stats "$stats")
fi

set +e
"$PY" "${args[@]}"
rc=$?
set -e

if [[ $rc -eq 0 ]]; then
  "$PY" "$REPO/scripts/scheduler_status.py" \
    --name monitor --state completed \
    --scheduled-for "$scheduled_for" \
    --claimed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --started-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --finished-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --status-dir "$CLIMATE_JOB_STATUS_DIR"
  exit 0
fi

"$PY" "$REPO/scripts/scheduler_status.py" \
  --name monitor --state failed \
  --scheduled-for "$scheduled_for" \
  --claimed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --started-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --finished-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --status-dir "$CLIMATE_JOB_STATUS_DIR"
exit "$rc"