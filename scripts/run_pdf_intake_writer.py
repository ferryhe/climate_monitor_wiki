"""Run the independent PDF and web activation intake writer."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from urllib import request
from urllib.parse import urlencode

from climate_registry.pdf_pipeline import PdfIntakePipeline


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _reload_chat(expected_generation_id: str) -> None:
    call = request.Request(
        _required("CLIMATE_PDF_RELOAD_URL") + "?" + urlencode({"generation_id": expected_generation_id}),
        method="POST",
        headers={"X-Reload-Token": _required("RELOAD_TOKEN")},
    )
    with request.urlopen(call, timeout=60) as response:
        if response.status != 200:
            raise RuntimeError(f"Chat reload returned HTTP {response.status}")
        payload = json.load(response)
    projection = payload.get("pdf_projection") if isinstance(payload, dict) else None
    if not isinstance(projection, dict) or projection.get("generation_id") != expected_generation_id:
        raise RuntimeError("Chat reload did not activate the requested PDF Wiki projection")


def _pipeline() -> PdfIntakePipeline:
    from climate_registry.publication import resolve_database
    database = resolve_database()
    return PdfIntakePipeline(
        queue_dir=Path(_required("CLIMATE_PDF_INTAKE_QUEUE_DIR")),
        database=database,
        backup_dir=Path(_required("CLIMATE_REGISTRY_BACKUP_DIR")),
        runtime_wiki_dir=Path(_required("CLIMATE_PDF_RUNTIME_WIKI_DIR")),
        reload_chat=_reload_chat,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Process durable PDF and web activation batches.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    pipeline = _pipeline()
    while True:
        result = pipeline.process_next()
        if result is not None:
            print(f"{result['batch_id']} {result['stage']}", flush=True)
        if args.once:
            return 0 if result is None or result.get("chat_ready") else 1
        time.sleep(max(args.poll_seconds, 0.1))


if __name__ == "__main__":
    raise SystemExit(main())
