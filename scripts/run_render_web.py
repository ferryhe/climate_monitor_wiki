from __future__ import annotations

import os
import tempfile
from pathlib import Path

import uvicorn

from climate_registry.audit import build_audit_registry


ROOT = Path(__file__).resolve().parents[1]


def _run_app() -> None:
    port = int(os.getenv("PORT", "8501"))
    uvicorn.run("api_server:app", host="0.0.0.0", port=port)


def main() -> None:
    if os.getenv("CLIMATE_REGISTRY_DB", "").strip():
        _run_app()
        return

    source_dir = (ROOT / os.getenv("SOURCE_DIR", "sources")).resolve()
    with tempfile.TemporaryDirectory(prefix="climate-render-registry-") as workspace:
        workspace_path = Path(workspace)
        database = workspace_path / "article-registry.sqlite3"
        build_audit_registry(source_dir, database, workspace_path / "audit")
        os.environ["CLIMATE_REGISTRY_DB"] = str(database)
        try:
            _run_app()
        finally:
            os.environ.pop("CLIMATE_REGISTRY_DB", None)


if __name__ == "__main__":
    main()
