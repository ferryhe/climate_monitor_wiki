"""Install governed hooks in a run-private copy of the user's Hermes home."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import yaml

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

ROOT = Path(__file__).resolve().parents[1]
SEARCH_IDENTITY_PLUGIN_ID = "climate-acquisition-search-identity"
CREDENTIAL_INPUTS_STATE = ".managed-credential-inputs.json"
MANAGED_ENVIRONMENT_STATE = ".managed-provider-environment.json"
MANAGED_INPUTS_COMPLETE = ".managed-inputs-complete.json"
MANAGED_SOURCE_HOME = ".managed-source"
_CREDENTIAL_INPUTS_SCHEMA = "managed-hermes-inputs.v3"
_MANAGED_ENVIRONMENT_SCHEMA = "managed-hermes-provider-environment.v1"
_MANAGED_INPUTS_COMPLETE_SCHEMA = "managed-hermes-inputs-complete.v1"
_CREDENTIAL_INPUTS = (".env", ".op.env", "auth.json")
_INPUT_FILES = ("config.yaml", *_CREDENTIAL_INPUTS)


def attempt_home(binding):
    # A managed run needs one stable Hermes session store across feedback and
    # explicit resume attempts.  The home is private to the run, not the task.
    if "provider" in binding and "model" in binding:
        return Path(binding["checkpoint_dir"]).parent / f"hermes-attempt-{binding['attempt']}"
    return Path(binding["checkpoint_dir"]).parent / "hermes-runtime"


def _write_immutable(path, raw, message):
    if path.exists() and path.read_text() != raw:
        raise ValueError(message)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw)
    path.chmod(0o600)


def _create_once(path, raw):
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        else:
            path.chmod(0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _copy_frozen_credential(raw, destination):
    try:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
        )
    except FileExistsError:
        return
    with os.fdopen(descriptor, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def _read_frozen_inputs(home):
    home = Path(home)
    state_path = home / CREDENTIAL_INPUTS_STATE
    environment_path = home / MANAGED_ENVIRONMENT_STATE
    complete_path = home / MANAGED_INPUTS_COMPLETE
    source_home = home / MANAGED_SOURCE_HOME
    if (home / f"{CREDENTIAL_INPUTS_STATE}.initializing").exists():
        raise ValueError("managed Hermes input snapshot initialization is incomplete")
    try:
        snapshot = json.loads(state_path.read_text(encoding="utf-8"))
        environment_snapshot = json.loads(environment_path.read_text(encoding="utf-8"))
        complete = json.loads(complete_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("managed Hermes input snapshot is unreadable") from exc
    files = snapshot.get("files") if isinstance(snapshot, dict) else None
    values = (
        environment_snapshot.get("values")
        if isinstance(environment_snapshot, dict) else None
    )
    if (
        not isinstance(files, dict)
        or snapshot.get("schema_version") != _CREDENTIAL_INPUTS_SCHEMA
        or set(snapshot) != {"schema_version", "files"}
        or set(files) != set(_INPUT_FILES)
        or not all(isinstance(value, bool) for value in files.values())
        or not isinstance(values, dict)
        or environment_snapshot.get("schema_version") != _MANAGED_ENVIRONMENT_SCHEMA
        or set(environment_snapshot) != {"schema_version", "values"}
        or not all(
            isinstance(name, str) and name
            and isinstance(value, str) and value
            for name, value in values.items()
        )
        or not isinstance(complete, dict)
        or complete.get("schema_version") != _MANAGED_INPUTS_COMPLETE_SCHEMA
        or set(complete) != {"schema_version"}
        or not source_home.is_dir()
        or files["config.yaml"] != (source_home / "config.yaml").is_file()
    ):
        raise ValueError("managed Hermes input snapshot is incompatible")
    if any(files[name] and not (home / name).is_file() for name in _CREDENTIAL_INPUTS):
        raise ValueError("managed Hermes input snapshot initialization is incomplete")
    return values


def _apply_frozen_environment(environment, names, values):
    frozen = dict(environment)
    for name in set(names) | set(values):
        frozen.pop(name, None)
    frozen.update(values)
    return frozen


def _freeze_credential_inputs(
    source_home, home, environment=None, managed_environment_names=(),
):
    source_home = Path(source_home)
    home = Path(home)
    state_path = home / CREDENTIAL_INPUTS_STATE
    environment_path = home / MANAGED_ENVIRONMENT_STATE
    complete_path = home / MANAGED_INPUTS_COMPLETE
    initializing = home / f"{CREDENTIAL_INPUTS_STATE}.initializing"
    supplied = dict(environment or {})
    names = set(managed_environment_names)
    if complete_path.exists():
        values = _read_frozen_inputs(home)
    else:
        if any(home.iterdir()):
            raise ValueError("managed Hermes input snapshot initialization is incomplete")
        try:
            descriptor = os.open(
                initializing, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
            )
        except FileExistsError as exc:
            raise ValueError(
                "managed Hermes input snapshot initialization is incomplete"
            ) from exc
        os.close(descriptor)
        complete = False
        try:
            frozen = {}
            for name in _INPUT_FILES:
                try:
                    frozen[name] = (source_home / name).read_bytes()
                except FileNotFoundError:
                    frozen[name] = None
            frozen_environment = {
                name: supplied[name]
                for name in sorted(names)
                if supplied.get(name)
            }
            snapshot = {
                "schema_version": _CREDENTIAL_INPUTS_SCHEMA,
                "files": {name: raw is not None for name, raw in frozen.items()},
            }
            environment_snapshot = {
                "schema_version": _MANAGED_ENVIRONMENT_SCHEMA,
                "values": frozen_environment,
            }
            frozen_source_home = home / MANAGED_SOURCE_HOME
            frozen_source_home.mkdir(mode=0o700)
            if frozen["config.yaml"] is not None:
                _copy_frozen_credential(
                    frozen["config.yaml"], frozen_source_home / "config.yaml",
                )
            for name in _CREDENTIAL_INPUTS:
                raw = frozen[name]
                if raw is not None:
                    _copy_frozen_credential(raw, home / name)
            _create_once(
                state_path,
                json.dumps(snapshot, sort_keys=True).encode("utf-8") + b"\n",
            )
            _create_once(
                environment_path,
                json.dumps(environment_snapshot, sort_keys=True).encode("utf-8") + b"\n",
            )
            _create_once(
                complete_path,
                json.dumps({
                    "schema_version": _MANAGED_INPUTS_COMPLETE_SCHEMA,
                }, sort_keys=True).encode("utf-8") + b"\n",
            )
            complete = True
        finally:
            if complete:
                initializing.unlink(missing_ok=True)
        values = _read_frozen_inputs(home)
    return _apply_frozen_environment(supplied, names, values)


def load_frozen_managed_environment(home, environment, managed_environment_names):
    values = _read_frozen_inputs(Path(home))
    return _apply_frozen_environment(
        environment, managed_environment_names, values,
    )


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


def _install_search_identity_plugin(home, binding_path):
    plugin = home / "plugins" / SEARCH_IDENTITY_PLUGIN_ID
    binding = json.loads(Path(binding_path).read_text())
    v3 = candidate_handle_protocol(binding)
    manifest = json.dumps({
        "name": SEARCH_IDENTITY_PLUGIN_ID,
        "version": "1.0.0",
        "description": "Expose durable completed search identity to the acquisition turn.",
        "hooks": ["transform_tool_result"],
    }, sort_keys=True)
    stage_schema = {
        "name": "climate_stage_candidate",
        "description": (
            "Resolve one trusted search result handle, apply the frozen date policy, "
            "and conditionally obtain its governed article body."
        ),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "result_handle": {"type": "string"},
                "source_key": {"type": "string"},
            },
            "required": ["result_handle", "source_key"],
        },
    }
    finalize_schema = {
        "name": "climate_finalize_candidate",
        "description": "Attach bounded relevance annotations to one staged candidate receipt.",
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "candidate_handle": {"type": "string"},
                "selected": {"type": "boolean"},
                "title": {"type": "string", "maxLength": 500},
                "summary": {"type": "string", "maxLength": 4000},
                "selection_reason": {"type": "string", "maxLength": 2000},
            },
            "required": [
                "candidate_handle", "selected", "title", "summary",
                "selection_reason",
            ],
        },
    }
    source = (
        "import sys\n"
        "import json\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from climate_monitor.hermes_acquisition_hooks import transform_search_tool_result\n"
        f"_BINDING_PATH = {str(Path(binding_path).resolve())!r}\n\n"
        "def register(ctx):\n"
        "    def transform(**kwargs):\n"
        "        return transform_search_tool_result(_BINDING_PATH, **kwargs)\n"
        "    ctx.register_hook(\"transform_tool_result\", transform)\n"
    )
    if v3:
        source += (
            "    from scripts.run_agent_acquisition import "
            "_stage_candidate_receipt, _finalize_candidate_receipt\n"
            "    def stage(args, session_id=None, **_kwargs):\n"
            "        return json.dumps(_stage_candidate_receipt(_BINDING_PATH, "
            "session_id=session_id, **args), sort_keys=True, separators=(',', ':'))\n"
            "    def finalize(args, session_id=None, **_kwargs):\n"
            "        return json.dumps(_finalize_candidate_receipt(_BINDING_PATH, "
            "session_id=session_id, **args), sort_keys=True, separators=(',', ':'))\n"
            f"    ctx.register_tool(name='climate_stage_candidate', toolset='climate_acquisition', schema={stage_schema!r}, handler=stage)\n"
            f"    ctx.register_tool(name='climate_finalize_candidate', toolset='climate_acquisition', schema={finalize_schema!r}, handler=finalize)\n"
        )
    _write_immutable(
        plugin / "plugin.yaml", manifest,
        "immutable attempt Hermes plugin manifest differs",
    )
    _write_immutable(
        plugin / "__init__.py", source,
        "immutable attempt Hermes plugin implementation differs",
    )


def install_hooks(
    command, binding_path, binding, environment, *, managed_environment_names=(),
):
    home = attempt_home(binding)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_home = Path(environment.get("HERMES_HOME") or Path.home() / ".hermes")
    # Freeze presence before any later installation step can fail. Private
    # auth.json updates (including OAuth refresh) are never overwritten.
    environment = _freeze_credential_inputs(
        source_home, home, environment, managed_environment_names,
    )
    runtime_binding = home / "managed-binding.json"
    runtime_binding.write_text(
        json.dumps(binding, ensure_ascii=False, sort_keys=True), encoding="utf-8",
    )
    runtime_binding.chmod(0o600)
    hook = shlex.join([sys.executable, str(ROOT / "scripts/acquisition_budget_hook.py"),
                       "--binding", str(runtime_binding)])
    source_config = home / MANAGED_SOURCE_HOME / "config.yaml"
    config_path = home / "config.yaml"
    if source_config.is_file():
        loaded_config = yaml.safe_load(source_config.read_text(encoding="utf-8"))
        if loaded_config is not None and not isinstance(loaded_config, dict):
            raise ValueError("Hermes default configuration is not an object")
        config = dict(loaded_config or {})
    else:
        config = {}
    # Preserve the ordinary Hermes model/provider defaults while replacing the
    # mutable extension surfaces with the governed acquisition hooks.
    config.update({"hooks_auto_accept": True, "hooks": {
        "pre_tool_call": [{"command": hook, "timeout": 15, "fail_closed": True}],
        "post_tool_call": [{"command": hook, "timeout": 15}],
    }, "mcp_servers": {}, "memory": {"memory_enabled": False, "user_profile_enabled": False}})
    if candidate_handle_protocol(binding):
        config["tools"] = {"tool_search": {"enabled": "off"}}
    if provider_native_unbounded_search(binding):
        _install_search_identity_plugin(home, runtime_binding)
        config["plugins"] = {"enabled": [SEARCH_IDENTITY_PLUGIN_ID]}
    raw = json.dumps(config, sort_keys=True)
    # JSON is a supported YAML subset.
    _write_immutable(
        config_path, raw, "immutable attempt Hermes hook configuration differs",
    )
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
