#!/usr/bin/env bash
# Hermes ticks at both possible UTC hours; only 05:00 America/New_York runs.
set -euo pipefail
if [[ "${1:-}" != "--preflight" && "$(TZ=America/New_York date +%H)" != "05" ]]; then
  exit 0
fi
sudo -n docker exec -i climate-pdf-intake-writer python -c '
import json
import os
import sys
from io import StringIO
from dotenv import dotenv_values

configuration = dotenv_values(stream=StringIO(sys.stdin.read()), interpolate=False)
os.environ["TYPESAFE_API_KEY"] = configuration.get("TYPESAFE_API_KEY") or os.getenv("TYPESAFE_API_KEY", "")

required = ("TYPESAFE_API_KEY", "CLIMATE_REGISTRY_WRITER_DB", "CLIMATE_REGISTRY_BACKUP_DIR",
    "CLIMATE_PDF_INTAKE_QUEUE_DIR", "CLIMATE_PDF_RUNTIME_WIKI_DIR", "CLIMATE_PDF_RELOAD_URL", "RELOAD_TOKEN")
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

from scripts.check_information import main
result = 0
for kind in ("articles", "meetings"):
    try:
        code = main(["--kind", kind, "--database", os.environ["CLIMATE_REGISTRY_WRITER_DB"],
            "--backup-dir", os.environ["CLIMATE_REGISTRY_BACKUP_DIR"], "--refresh-chat",
            "--data-root", os.getenv("CLIMATE_WEB_LISTENING_DATA_DIR", "/pipeline/web-listening"),
            "--result", "/pipeline/" + kind + "-daily-check.json"])
    except Exception as exc:
        print(json.dumps({"kind": kind, "status": "failed", "error": type(exc).__name__}), flush=True)
        code = 2
    result = max(result, code)
raise SystemExit(result)
' "${1:-}" < "${CLIMATE_WIKI_ENV_FILE:-$HOME/climate_monitor_wiki/.env}"
