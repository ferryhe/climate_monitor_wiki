"""Install only the mandatory hooks in an isolated acquisition subprocess home."""
from __future__ import annotations
import json
import os
from pathlib import Path
from climate_monitor.managed_runtime import run_managed

from climate_monitor.hermes_identity import prepare_home, SNAPSHOT

from climate_monitor.request_budget import (
    RequestBudget,
    candidate_handle_protocol,
    candidate_handle_suffix,
    ledger_path,
    original_candidate_search_result,
    original_search_tool_result,
    provider_native_unbounded_search,
    search_identity_suffix,
)

SEARCH_IDENTITY_PLUGIN_ID = "climate-acquisition-search-identity"


def attempt_home(binding):
    return Path(binding["checkpoint_dir"]).parent / SNAPSHOT / f"attempt-{binding['attempt']}"


def _write_immutable(path, raw, message):
    from climate_monitor.hermes_identity import secure_read, _write, _sync_dir, _mkdir_private
    _mkdir_private(path.parent)
    try:
        _write(path, raw.encode())
    except FileExistsError:
        if secure_read(path, private=True)[0] != raw.encode():
            raise ValueError(message)
    _sync_dir(path.parent)
    _sync_dir(path.parent.parent)


def _attempt_binding(path):
    if os.environ.get('CLIMATE_ACQUISITION_ATTEMPT'):
        from climate_monitor.hermes_attempt_policy import verified_binding
        return verified_binding(path)[1]
    return json.loads(Path(path).read_text(encoding='utf-8'))


def transform_search_tool_result(
    binding_path, *, tool_name, args, result, session_id, tool_call_id,
    status=None, **_ignored,
):
    """Append the durable raw search-call identity to its original tool result."""
    if tool_name != "web_search" or status != "ok" or not all(
        isinstance(value, str) and value.strip()
        for value in (result, session_id, tool_call_id)
    ) or not isinstance(args, dict):
        return None
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    binding = _attempt_binding(binding_path)
    if not provider_native_unbounded_search(binding):
        return None
    raw_session = session_id.strip()
    raw_call = tool_call_id.strip()
    budget = RequestBudget(ledger_path(binding), binding)
    call_id = f"{int(binding['attempt'])}:{raw_session}:{raw_call}"
    if candidate_handle_protocol(binding):
        existing = [
            row for row in budget.result_handles()
            if row["attempt"] == int(binding["attempt"])
            and row["session_id"] == raw_session
            and row["tool_call_id"] == raw_call
        ]
        existing.sort(key=lambda row: row["ordinal"])
        handles = [row["handle"] for row in existing]
        original = (
            original_candidate_search_result(result, handles)
            if handles else result
        )
        try:
            budget.complete_tool(call_id, original, status)
            minted = budget.register_search_result_handles(
                raw_session, raw_call, original,
            )
        except ValueError:
            return None
        handles = [row["handle"] for row in minted]
        suffix = candidate_handle_suffix(handles)
        return original + suffix
    suffix = search_identity_suffix(raw_call, query)
    original = original_search_tool_result(result, raw_call, query)
    # The direct Hermes dispatcher emits post_tool_call before this seam. Its
    # AIAgent executor owns post-tool emission and suppresses that inner event,
    # so this post-dispatch transform may run first. Completing the exact
    # pre-admitted call here is idempotent; the later shell post hook verifies
    # the same result and does not add another event or unit.
    try:
        budget.complete_tool(call_id, original, status)
    except ValueError:
        return None
    event = budget.tool_event(call_id)
    if not event or any((
        event.get("tool") != "web_search",
        event.get("url") != query,
        event.get("status") != "ok",
        event.get("completed") is not True,
        event.get("result") != original,
    )):
        return None
    return result if result.endswith(suffix) else result + suffix


def install_hooks(command, binding_path, binding, environment):
    from climate_monitor.hermes_identity import secure_read, _digest
    binding_path = Path(binding_path).absolute()
    root = binding_path.parent / SNAPSHOT
    raw = secure_read(binding_path)[0]
    if (json.loads(raw) != json.loads(json.dumps(binding)) or Path(binding['checkpoint_dir']).parent != root.parent
            or type(binding['attempt']) is not int or binding['attempt'] < 1):
        raise ValueError('inconsistent acquisition attempt; start a fresh run')
    home = root / f"attempt-{binding['attempt']}"
    payload, config, environment = prepare_home(
        binding_path.parent, binding.get('hermes_snapshot'), home,
        source=f"climate-acquisition-{binding['run_id']}",
    )
    # Runtime controls and already-rewritten inference/CA inputs win overlaps.
    environment.update({k: v for k, v in payload['web_environment'].items() if k not in environment})
    seal = {'schema_version': 'climate-acquisition-attempt-policy.v1',
            'binding_path': str(Path(binding_path).absolute()), 'binding_sha256': _digest(raw),
            'snapshot': binding['hermes_snapshot'], 'run_id': binding['run_id'], 'attempt': binding['attempt']}
    seal_path = home / 'attempt-policy.json'
    environment['CLIMATE_ACQUISITION_ATTEMPT'] = str(seal_path)
    environment['CLIMATE_WEB_LISTENING_DATA_DIR'] = payload['reader_runtime']['root']
    environment['PYTHONPATH'] += os.pathsep + str(root / 'acquisition')
    from climate_monitor.hermes_attempt_policy import acquisition_configuration
    config = acquisition_configuration(root, payload, binding, binding_path)
    from climate_monitor.hermes_identity import launch_command
    command[:1] = launch_command(home)
    search_plugin = json.loads(secure_read(root / 'policy/search-plugin.json', private=True)[0])['name']
    if search_plugin in config['plugins']['enabled']:
        plugin = home / 'plugins' / search_plugin
        for name, frozen in [('__init__.py', 'policy/search-plugin.py'), ('plugin.yaml', 'policy/search-plugin.json')]:
            content = secure_read(root / frozen, private=True)[0]
            if payload['policy'][frozen] != {'size': len(content), 'sha256': _digest(content)}:
                raise ValueError('frozen acquisition policy changed; start a fresh run')
            _write_immutable(plugin / name, content.decode(), 'immutable acquisition plugin changed; start a fresh run')
    raw = json.dumps(config, sort_keys=True)
    seal['configuration_sha256'] = _digest(raw.encode())
    _write_immutable(seal_path, json.dumps(seal, sort_keys=True), 'immutable acquisition attempt changed; start a fresh run')
    config_path = home / "config.yaml"  # JSON is a supported YAML subset.
    _write_immutable(
        config_path, raw, "immutable attempt Hermes hook configuration differs",
    )
    env = environment
    interpreter = launch_command(home, '--budget-hook')
    from climate_monitor.hermes_runtime_inventory import VERIFY_TIMEOUT
    verified = run_managed([*interpreter,
        "--binding", str(binding_path), "--verify-runtime"], cwd=home, env=env, state_dir=home,
        capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
    from climate_monitor.hermes_auth_state import verify_auth
    verify_auth(home)
    if verified.returncode or "climate acquisition hooks verified" not in verified.stdout:
        raise ValueError("Hermes acquisition hook installation incompatible: " +
                         "runtime verification failed")
    return env, home
