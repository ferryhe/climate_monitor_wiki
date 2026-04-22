from __future__ import annotations

import argparse
import json
import os
import sys
from urllib import error, request


def _request_json(url: str, method: str = "GET", data: dict | None = None, headers: dict | None = None) -> dict:
    body = None if data is None else json.dumps(data).encode("utf-8")
    final_headers = {"Content-Type": "application/json"}
    if headers:
        final_headers.update(headers)
    req = request.Request(url, data=body, headers=final_headers, method=method)
    with request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reload the Climate Monitor API corpus and run a small smoke test."
    )
    parser.add_argument("--base-url", default=os.getenv("API_BASE_URL", "http://localhost:8501"))
    parser.add_argument("--date", help="Daily report date to verify, for example 2026-04-21.")
    parser.add_argument("--reload-token", default=os.getenv("RELOAD_TOKEN", ""))
    parser.add_argument(
        "--message",
        default="What are the latest Climate Monitor highlights?",
        help="Chat prompt used for the smoke test.",
    )
    args = parser.parse_args()

    headers = {}
    if args.reload_token:
        headers["x-reload-token"] = args.reload_token

    try:
        reload_payload = _request_json(f"{args.base_url}/api/reload", method="POST", headers=headers)
        print(
            "Reloaded corpus:",
            f"wiki_documents={reload_payload['wiki']['documents']}",
            f"source_documents={reload_payload['wiki']['source_documents']}",
        )

        config_payload = _request_json(f"{args.base_url}/api/config")
        print("Fetched config successfully.")

        if args.date:
            expected_path = f"wiki/climate-monitor-{args.date}.md"
            expected_source_path = f"sources/climate-monitor-{args.date}.md"
            doc = next((item for item in config_payload["documents"] if item["path"] == expected_path), None)
            if doc is None:
                raise RuntimeError(f"Missing daily wiki page in /api/config: {expected_path}")
            if doc.get("source_path") != expected_source_path:
                raise RuntimeError(
                    f"Unexpected source mapping for {expected_path}: {doc.get('source_path')} != {expected_source_path}"
                )
            print(f"Verified daily page mapping for {args.date}.")

        chat_payload = _request_json(
            f"{args.base_url}/api/chat",
            method="POST",
            data={
                "message": args.message,
                "language": "en",
                "answerMode": "detailed",
            },
        )
        if not chat_payload.get("text", "").strip():
            raise RuntimeError("Smoke chat returned an empty response.")
        if not chat_payload.get("sources"):
            raise RuntimeError("Smoke chat returned no evidence sources.")

        print(
            "Smoke chat passed:",
            f"answer_mode={chat_payload.get('answer_mode')}",
            f"sources={len(chat_payload.get('sources', []))}",
        )
        return 0
    except (error.URLError, error.HTTPError, RuntimeError, KeyError, ValueError) as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
