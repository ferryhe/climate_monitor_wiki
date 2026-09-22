#!/usr/bin/env python3
"""Attempt-scoped Hermes shell hook. No credentials or model output in stdout."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from climate_monitor.request_budget import RequestBudget, hook_decision, ledger_path
from climate_monitor.hermes_acquisition_hooks import SEARCH_IDENTITY_PLUGIN_ID


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binding", required=True)
    parser.add_argument("--verify-runtime", action="store_true")
    args = parser.parse_args()
    from climate_monitor.hermes_attempt_policy import verified_binding
    _, binding = verified_binding(args.binding)
    if args.verify_runtime:
        from agent.shell_hooks import iter_configured_hooks, register_from_config, run_once
        from hermes_cli.config import load_config
        config = load_config()
        specs = iter_configured_hooks(config)
        if sorted(s.event for s in specs) != ["post_tool_call", "pre_tool_call"] or not any(s.event == "pre_tool_call" and s.fail_closed for s in specs):
            raise ValueError("incompatible attempt shell-hook configuration")
        registered = register_from_config(config, accept_hooks=True)
        if len(registered) != 2:
            raise ValueError("attempt shell hooks were not registered")
        pre = next(s for s in specs if s.event == "pre_tool_call")
        result = run_once(pre, {"hook_event_name": "pre_tool_call", "tool_name": "__climate_guard_probe__"})
        # run_once exposes both the subprocess result and the parsed directive.
        if (result.get("timed_out") or result.get("error") or result.get("returncode") != 2
                or result.get("parsed") != {"action": "block", "message": "climate acquisition guard probe"}):
            raise ValueError("budget hook probe did not complete successfully")
        from climate_monitor.request_budget import (
            candidate_handle_protocol, provider_native_unbounded_search,
        )
        IDENTITY_PLUGIN = "climate-frozen-identity"
        from hermes_cli.plugins import discover_plugins, get_plugin_manager
        discover_plugins(force=True)
        identity_plugin = next((p for p in get_plugin_manager().list_plugins()
                                if p['key'] == IDENTITY_PLUGIN), None)
        if not identity_plugin or not identity_plugin['enabled'] or identity_plugin['hooks'] != 2:
            raise ValueError("mandatory effective identity hooks missing")
        expected_plugins = {IDENTITY_PLUGIN}
        if provider_native_unbounded_search(binding):
            expected_plugins.add(SEARCH_IDENTITY_PLUGIN_ID)
        if set(config.get('plugins', {}).get('enabled', [])) != expected_plugins:
            raise ValueError('unexpected acquisition plugin configuration')
        if provider_native_unbounded_search(binding):
            from hermes_cli.plugins import discover_plugins, get_plugin_manager
            discover_plugins(force=True)
            plugins = {
                plugin["key"]: plugin for plugin in get_plugin_manager().list_plugins()
            }
            installed = plugins.get(SEARCH_IDENTITY_PLUGIN_ID)
            if not installed or not installed["enabled"] or installed["hooks"] != 1:
                raise ValueError("attempt search-identity plugin was not registered")
            if candidate_handle_protocol(binding):
                from model_tools import get_tool_definitions
                definitions = get_tool_definitions(
                    enabled_toolsets=["climate_acquisition"], quiet_mode=True,
                    skip_tool_search_assembly=True,
                )
                names = {
                    row.get("function", {}).get("name") for row in definitions
                }
                expected = {
                    "climate_stage_candidate", "climate_finalize_candidate",
                }
                if names != expected:
                    raise ValueError(
                        "attempt candidate tools were not registered exactly"
                    )
        print("climate acquisition hooks verified")
        return 0
    try:
        payload = json.load(sys.stdin)
        if payload.get("tool_name") == "__climate_guard_probe__":
            result = {"action": "block", "message": "climate acquisition guard probe"}
        else:
            path = ledger_path(binding)
            if not path.is_file():
                raise ValueError("durable request ledger missing before tool dispatch")
            result = hook_decision(RequestBudget(path, binding), payload)
    except Exception:
        result = {"action": "block", "message": "acquisition guard failed closed"}
    print(json.dumps(result))
    return 2 if result.get("action") == "block" else 0


if __name__ == "__main__":
    raise SystemExit(main())
