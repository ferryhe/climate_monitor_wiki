#!/usr/bin/env python3
"""Attempt-scoped Hermes shell hook. No credentials or model output in stdout."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from climate_monitor.request_budget import RequestBudget, hook_decision, ledger_path
from climate_monitor.hermes_acquisition_hooks import SEARCH_IDENTITY_PLUGIN_ID


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binding", required=True)
    parser.add_argument("--verify-runtime", action="store_true")
    args = parser.parse_args()
    if args.verify_runtime:
        from agent.shell_hooks import iter_configured_hooks, register_from_config, run_once
        from hermes_cli.config import load_config
        config = load_config()
        specs = iter_configured_hooks(config)
        if len(specs) != 2 or not any(s.event == "pre_tool_call" and s.fail_closed for s in specs):
            raise ValueError("incompatible attempt shell-hook configuration")
        registered = register_from_config(config, accept_hooks=True)
        if len(registered) != 2:
            raise ValueError("attempt shell hooks were not registered")
        pre = next(s for s in specs if s.event == "pre_tool_call")
        result = run_once(pre, {"hook_event_name": "pre_tool_call", "tool_name": "__climate_guard_probe__"})
        # run_once exposes both the subprocess result and the parsed directive.
        if (result.get("parsed") or {}).get("action") != "block":
            raise ValueError(f"budget hook probe did not block: {result}")
        binding = json.loads(Path(args.binding).read_text())
        from climate_monitor.request_budget import provider_native_unbounded_search
        if provider_native_unbounded_search(binding):
            from hermes_cli.plugins import discover_plugins, get_plugin_manager
            discover_plugins(force=True)
            plugins = {
                plugin["key"]: plugin for plugin in get_plugin_manager().list_plugins()
            }
            installed = plugins.get(SEARCH_IDENTITY_PLUGIN_ID)
            if not installed or not installed["enabled"] or installed["hooks"] != 1:
                raise ValueError("attempt search-identity plugin was not registered")
        print("climate acquisition hooks verified")
        return 0
    try:
        payload = json.load(sys.stdin)
        if payload.get("tool_name") == "__climate_guard_probe__":
            result = {"action": "block", "message": "climate acquisition guard probe"}
        else:
            binding = json.loads(Path(args.binding).read_text())
            path = ledger_path(binding)
            if not path.is_file():
                raise ValueError("durable request ledger missing before tool dispatch")
            result = hook_decision(RequestBudget(path, binding), payload)
    except Exception as exc:
        result = {"action": "block", "message": f"acquisition guard failed closed: {type(exc).__name__}: {exc}"}
    print(json.dumps(result))
    return 2 if result.get("action") == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())
