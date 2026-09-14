#!/usr/bin/env python3
"""Install and qualify the frozen Playwright adapter in one fresh Runtime root."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="/opt/web-listening-data")
    parser.add_argument("--upstream-source", default="/opt/web-listening-source")
    parser.add_argument("--authorization-window", required=True)
    args = parser.parse_args()
    source = Path(args.upstream_source).resolve()
    data = Path(args.data_dir).resolve()
    sys.path.insert(0, str(source))

    from tests.fixtures.browser_chain.server import fixture_server
    from tools.browser.install import install
    from web_listening.request.model import Budgets, ContentType, Request, Scope
    from web_listening.tool_registry.runners import in_process

    production_predicate = in_process._is_public_address
    try:
        # Exact temporary allowance from upstream's controlled live test. It is
        # process-local and restored before any public acquisition can run.
        in_process._is_public_address = (
            lambda ip: ip == "127.0.0.1" or production_predicate(ip)
        )
        with fixture_server() as (origin, _reads), tempfile.TemporaryDirectory() as temporary:
            request = Request(
                Scope((origin + "/qualification",), (origin,), ("/**",),
                      (ContentType.HTML, ContentType.FILE)),
                None, True, Budgets(24, 8 * 1024 * 1024, 60, 3),
            )
            request_path = Path(temporary) / "request.json"
            request_path.write_text(json.dumps(asdict(request)), encoding="utf-8")
            evidence = install(SimpleNamespace(
                action="install", tool="playwright", data_dir=str(data),
                runtime_root=str(data / "browser-runtimes/playwright"), docker="docker",
                version=None, request=str(request_path),
                authorization_window=args.authorization_window,
            ))
    finally:
        in_process._is_public_address = production_predicate
    print(json.dumps(evidence, sort_keys=True))
    return 0 if evidence["qualified"] and evidence["active"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
