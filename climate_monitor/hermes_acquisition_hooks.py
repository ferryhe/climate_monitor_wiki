"""Install only the mandatory hooks in an isolated acquisition subprocess home."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from climate_monitor.request_budget import (
    RequestBudget,
    ledger_path,
    original_search_tool_result,
    provider_native_unbounded_search,
    search_identity_suffix,
)

ROOT = Path(__file__).resolve().parents[1]
SEARCH_IDENTITY_PLUGIN_ID = "climate-acquisition-search-identity"


def attempt_home(binding):
    return Path(binding["checkpoint_dir"]).parent / f"hermes-attempt-{binding['attempt']}"


def _write_immutable(path, raw, message):
    if path.exists() and path.read_text() != raw:
        raise ValueError(message)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw)
    path.chmod(0o600)


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
    binding = json.loads(Path(binding_path).read_text())
    if not provider_native_unbounded_search(binding):
        return None
    raw_session = session_id.strip()
    raw_call = tool_call_id.strip()
    suffix = search_identity_suffix(raw_call, query)
    original = original_search_tool_result(result, raw_call, query)
    budget = RequestBudget(ledger_path(binding), binding)
    call_id = f"{int(binding['attempt'])}:{raw_session}:{raw_call}"
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


def _install_search_identity_plugin(home, binding_path):
    plugin = home / "plugins" / SEARCH_IDENTITY_PLUGIN_ID
    manifest = json.dumps({
        "name": SEARCH_IDENTITY_PLUGIN_ID,
        "version": "1.0.0",
        "description": "Expose durable completed search identity to the acquisition turn.",
        "hooks": ["transform_tool_result"],
    }, sort_keys=True)
    source = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from climate_monitor.hermes_acquisition_hooks import transform_search_tool_result\n"
        f"_BINDING_PATH = {str(Path(binding_path).resolve())!r}\n\n"
        "def register(ctx):\n"
        "    def transform(**kwargs):\n"
        "        return transform_search_tool_result(_BINDING_PATH, **kwargs)\n"
        "    ctx.register_hook(\"transform_tool_result\", transform)\n"
    )
    _write_immutable(
        plugin / "plugin.yaml", manifest,
        "immutable attempt Hermes plugin manifest differs",
    )
    _write_immutable(
        plugin / "__init__.py", source,
        "immutable attempt Hermes plugin implementation differs",
    )


def install_hooks(command, binding_path, binding, environment):
    home = attempt_home(binding)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    hook = shlex.join([sys.executable, str(ROOT / "scripts/acquisition_budget_hook.py"),
                       "--binding", str(binding_path)])
    config = {"hooks_auto_accept": True, "hooks": {
        "pre_tool_call": [{"command": hook, "timeout": 15, "fail_closed": True}],
        "post_tool_call": [{"command": hook, "timeout": 15}],
    }, "mcp_servers": {}, "memory": {"memory_enabled": False, "user_profile_enabled": False}}
    if provider_native_unbounded_search(binding):
        _install_search_identity_plugin(home, binding_path)
        config["plugins"] = {"enabled": [SEARCH_IDENTITY_PLUGIN_ID]}
    raw = json.dumps(config, sort_keys=True)
    config_path = home / "config.yaml"  # JSON is a supported YAML subset.
    _write_immutable(
        config_path, raw, "immutable attempt Hermes hook configuration differs",
    )
    # OAuth refreshes are confined to this attempt's private copy. Never copy
    # global hooks/plugins/MCP/environment configuration into the subprocess.
    auth = Path(environment.get("HERMES_HOME") or Path.home() / ".hermes") / "auth.json"
    if auth.is_file() and not (home / "auth.json").exists():
        fd = os.open(home / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as destination:
            destination.write(auth.read_bytes())
    env = {**environment, "HERMES_HOME": str(home), "HERMES_ACCEPT_HOOKS": "1"}
    env.pop("HERMES_SAFE_MODE", None)
    env.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)
    executable = Path(command[0])
    if not executable.is_absolute():
        executable = Path(shutil.which(command[0]) or command[0])
    shebang = executable.open("rb").readline(4096).decode(errors="replace").strip()
    if not shebang.startswith("#!"):
        raise ValueError("cannot verify Hermes Python shell-hook runtime")
    interpreter = shlex.split(shebang[2:])
    if interpreter and Path(interpreter[0]).name == "env":
        interpreter = interpreter[1:]
    if not interpreter or "python" not in Path(interpreter[0]).name:
        raise ValueError("Hermes executable does not expose a verifiable Python runtime")
    verified = subprocess.run([*interpreter, str(ROOT / "scripts/acquisition_budget_hook.py"),
        "--binding", str(binding_path), "--verify-runtime"], cwd=home, env=env,
        capture_output=True, text=True, timeout=30)
    if verified.returncode or "climate acquisition hooks verified" not in verified.stdout:
        raise ValueError("Hermes acquisition hook installation incompatible: " +
                         (verified.stderr or verified.stdout)[-2000:])
    return env, home
