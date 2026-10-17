#!/usr/bin/env bash
# Each five-minute tick admits two bounded incremental batches.
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
env_file="${CLIMATE_WIKI_ENV_FILE:-$repo/.env}"
if [[ ! -f "$env_file" ]]; then
  printf 'Information check requires the configured env file: %s\n' "$env_file" >&2
  exit 2
fi
env_file="$(cd -- "$(dirname -- "$env_file")" && pwd -P)/$(basename -- "$env_file")"
cd -- "$repo"
PY="${PYTHON:-$repo/.venv/bin/python}"
[[ "$PY" = /* && -x "$PY" ]] || exit 2
# Resolve the full caller model once; sudo must not parse its dotenv again.
project="$(docker compose --project-directory "$repo" --env-file "$env_file" config --format json | "$PY" -c '
import json, sys
model = json.load(sys.stdin)
if "pdf-intake-writer" not in model.get("services", {}):
    raise SystemExit("Information check requires pdf-intake-writer in the selected Compose model")
print(model["name"])
')"
ids="$(sudo -n docker ps --quiet --no-trunc --filter "label=com.docker.compose.project=$project" --filter label=com.docker.compose.service=pdf-intake-writer --filter label=com.docker.compose.oneoff=False)"
if [[ -z "$ids" ]]; then
  printf 'Information check requires a running writer for the selected Compose project\n' >&2
  exit 2
fi
mapfile -t writer_ids <<< "$ids"
if [[ "${#writer_ids[@]}" -ne 1 ]]; then
  printf 'Information check requires exactly one running writer for the selected Compose project\n' >&2
  exit 2
fi
sudo -n docker exec -i "${writer_ids[0]}" /usr/local/bin/python3.12 -c '
import json
import os
import sys
from io import StringIO
from dotenv import dotenv_values

configuration = dotenv_values(stream=StringIO(sys.stdin.read()), interpolate=False)
os.environ["TYPESAFE_API_KEY"] = configuration.get("TYPESAFE_API_KEY") or ""

required = ("TYPESAFE_API_KEY", "CLIMATE_REGISTRY_DB", "CLIMATE_REGISTRY_BACKUP_DIR",
    "CLIMATE_PDF_INTAKE_QUEUE_DIR", "CLIMATE_PDF_RUNTIME_WIKI_DIR", "CLIMATE_PDF_RELOAD_URL", "CLIMATE_ACQUISITION_RUN_DIR", "RELOAD_TOKEN")
missing = [name for name in required if not os.getenv(name, "").strip()]
if missing:
    print(json.dumps({"status": "not_configured", "missing": missing}), flush=True)
    raise SystemExit(2)
from climate_monitor.article_content_adapter import check_dependencies
if check_dependencies() != "available":
    print(json.dumps({"status": "not_configured", "reason": "governed_reader_unavailable"}), flush=True)
    raise SystemExit(2)
if sys.argv[1] == "--preflight":
    print(json.dumps({"status": "ready", "timezone": "America/New_York", "interval_minutes": 5,
        "batch_size_per_kind": int(configuration.get("CLIMATE_CHECK_BATCH_SIZE") or 10),
        "retry_after_minutes": int(configuration.get("CLIMATE_CHECK_RETRY_AFTER_MINUTES") or 60)}), flush=True)
    raise SystemExit(0)

from pathlib import Path
from scripts.check_information import run_daily_checks
raise SystemExit(run_daily_checks([
    "--database", os.environ["CLIMATE_REGISTRY_DB"],
    "--backup-dir", os.environ["CLIMATE_REGISTRY_BACKUP_DIR"], "--refresh-chat",
    "--data-root", os.getenv("CLIMATE_WEB_LISTENING_DATA_DIR", "/pipeline/web-listening"),
    "--limit", configuration.get("CLIMATE_CHECK_BATCH_SIZE") or "10",
    "--retry-after-minutes", configuration.get("CLIMATE_CHECK_RETRY_AFTER_MINUTES") or "60",
], result_dir=Path("/pipeline")))
' "${1:-}" < "$env_file"
