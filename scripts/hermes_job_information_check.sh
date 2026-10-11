#!/usr/bin/env bash
# Hermes ticks at both possible UTC hours; only 05:00 America/New_York runs.
set -euo pipefail
if [[ "${1:-}" != "--preflight" && "$(TZ=America/New_York date +%H)" != "05" ]]; then
  exit 0
fi
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
env_file="${CLIMATE_WIKI_ENV_FILE:-$repo/.env}"
if [[ ! -f "$env_file" ]]; then
  printf 'Information check requires the configured env file: %s\n' "$env_file" >&2
  exit 2
fi
env_file="$(cd -- "$(dirname -- "$env_file")" && pwd -P)/$(basename -- "$env_file")"
compose=(--project-directory "$repo" --env-file "$env_file")
# Resolve Compose selections in the caller environment before sudo strips it.
selection="$(docker compose "${compose[@]}" config --environment | sed -n '/^COMPOSE_FILE=/p; /^COMPOSE_PATH_SEPARATOR=/p; /^COMPOSE_PROJECT_NAME=/p')"
file_list=""
separator=""
project=""
while IFS= read -r value; do
  case "$value" in
    COMPOSE_FILE=*) file_list="${value#*=}" ;;
    COMPOSE_PATH_SEPARATOR=*) separator="${value#*=}" ;;
    COMPOSE_PROJECT_NAME=*) project="${value#*=}" ;;
  esac
done <<< "$selection"
if [[ -n "$file_list" ]]; then
  IFS="${separator:-:}" read -r -a compose_files <<< "$file_list"
  for file in "${compose_files[@]}"; do compose+=(-f "$file"); done
fi
if [[ -n "$project" ]]; then
  compose+=(-p "$project")
fi
cd -- "$repo"
sudo -n docker compose "${compose[@]}" exec -T pdf-intake-writer python -c '
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
    print(json.dumps({"status": "ready", "timezone": "America/New_York", "hour": 5}), flush=True)
    raise SystemExit(0)

from pathlib import Path
from scripts.check_information import run_daily_checks
raise SystemExit(run_daily_checks([
    "--database", os.environ["CLIMATE_REGISTRY_DB"],
    "--backup-dir", os.environ["CLIMATE_REGISTRY_BACKUP_DIR"], "--refresh-chat",
    "--data-root", os.getenv("CLIMATE_WEB_LISTENING_DATA_DIR", "/pipeline/web-listening"),
], result_dir=Path("/pipeline")))
' "${1:-}" < "$env_file"
