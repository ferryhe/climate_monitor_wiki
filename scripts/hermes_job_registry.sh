#!/usr/bin/env bash
# Explicit configuration only; --preflight validates without dispatch or writes.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="${REPO:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
[[ "$REPO" = /* && -d "$REPO" ]] || exit 2
PY="${PYTHON:-$(command -v python3)}"
[[ "$PY" = /* && -x "$PY" ]] || exit 2
export PYTHONDONTWRITEBYTECODE=1
exec "$PY" "$REPO/scripts/hermes_job.py" registry "$@"
