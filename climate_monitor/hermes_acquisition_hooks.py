"""Install only the mandatory hooks in an isolated acquisition subprocess home."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def attempt_home(binding):
    return Path(binding["checkpoint_dir"]).parent / f"hermes-attempt-{binding['attempt']}"


def install_hooks(command, binding_path, binding, environment):
    home = attempt_home(binding)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    hook = shlex.join([sys.executable, str(ROOT / "scripts/acquisition_budget_hook.py"),
                       "--binding", str(binding_path)])
    config = {"hooks_auto_accept": True, "hooks": {
        "pre_tool_call": [{"command": hook, "timeout": 15, "fail_closed": True}],
        "post_tool_call": [{"command": hook, "timeout": 15}],
    }, "mcp_servers": {}, "memory": {"memory_enabled": False, "user_profile_enabled": False}}
    raw = json.dumps(config, sort_keys=True)
    config_path = home / "config.yaml"  # JSON is a supported YAML subset.
    if config_path.exists() and config_path.read_text() != raw:
        raise ValueError("immutable attempt Hermes hook configuration differs")
    config_path.write_text(raw)
    config_path.chmod(0o600)
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
