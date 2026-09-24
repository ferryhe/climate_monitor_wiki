"""Offline canary for the exact PR #162 image on a disposable CI runner.

This script lives only in the validation branch; the image is built from the
unmodified PR #162 commit. It performs no monitoring, browser launch or inference.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from climate_monitor.hermes_identity import SNAPSHOT, create_snapshot, load_snapshot
from climate_monitor.management import ManagementService, TaskDefinitionStore, default_task_definition
from climate_registry.persistent import initialize_registry

EXPECTED_COMMIT = "82f930cf01b8b90cc2ebc0b702bf4a0db6f55d5f"
base = Path(tempfile.mkdtemp(prefix="issue161-installed-managed-"))
phase = "setup"
try:
    assert os.environ.get("CLIMATE_REPOSITORY_COMMIT_SHA") == EXPECTED_COMMIT
    assert importlib.metadata.version("hermes-agent") == "0.20.5"
    assert importlib.metadata.version("web-listening") == "0.1.0"
    shared_source = os.environ.get("ISSUE161_TEST_SHARED_SOURCE") == "1"
    if shared_source:
        # Disposable container copy-on-write only: reproduce collaborative
        # application source modes without touching runtime/private inputs.
        app = Path("/app")
        app_stat = app.lstat()
        assert stat.S_ISDIR(app_stat.st_mode)
        app.chmod(stat.S_IMODE(app_stat.st_mode) | 0o020)
        for root in map(Path, ("/app/climate_monitor", "/app/climate_registry", "/app/scripts")):
            assert stat.S_ISDIR(root.lstat().st_mode)
            for path in (root, *root.rglob("*")):
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    assert metadata.st_nlink == 1
                if stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
                    path.chmod(stat.S_IMODE(metadata.st_mode) | 0o020)
    source = Path("/app/climate_monitor/hermes_identity.py")
    assert source.is_file() and source.stat().st_mode & 0o022 == (0o020 if shared_source else 0)
    executable = shutil.which("hermes")
    if not executable:
        raise RuntimeError("missing installed Hermes executable")
    home = base / "hermes-home"
    home.mkdir(mode=0o700)
    runs = base / "runs"
    runs.mkdir(mode=0o700)
    state = base / "state"
    state.mkdir(mode=0o700)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_EXECUTABLE"] = executable
    os.environ["CLIMATE_MANAGED_STATE_DIR"] = str(state)
    # Synthetic sentinel only. The container has --network none and no secrets.
    os.environ["OPENAI_API_KEY"] = "test-only-not-a-real-key"
    direct = base / "direct"
    direct.mkdir(mode=0o700)
    phase = "create_snapshot"
    reference = create_snapshot(direct, source="issue161-canary")
    phase = "load_snapshot"
    direct_payload = load_snapshot(direct, reference)
    assert (direct / SNAPSHOT).is_dir()
    phase = "reader_preflight"
    reader = direct_payload["reader_runtime"]
    runtime_configs = sorted(Path(reader["root"]).glob("tools/*/*/*/runtime.json"))
    browser_python = Path(reader["root"]) / "browser-runtimes/playwright/bin/python"
    diagnostics = {
        "root_matches_env": reader["root"] == os.environ["CLIMATE_WEB_LISTENING_DATA_DIR"],
        "absent": reader["absent"],
        "inventory_count": len(reader["inventory"]),
        "runtime_config_count": len(runtime_configs),
        "browser_runtime_present": browser_python.is_file(),
    }
    print(json.dumps({"result": "INFO", "phase": phase, **diagnostics}, sort_keys=True))
    assert diagnostics["root_matches_env"] and diagnostics["inventory_count"]
    assert diagnostics["browser_runtime_present"] and "browser-runtimes" not in diagnostics["absent"]
    phase = "definition"
    definition = default_task_definition()
    definition["runtime"].update(
        registry_database=str(base / "registry.sqlite3"), run_root=str(runs)
    )
    initialize_registry(base / "registry.sqlite3")
    store = TaskDefinitionStore(base / "task.json", base / "versions")
    store.save(definition, actor="issue161-canary")
    launcher_called = []

    def controlled_launcher(binding):
        directory = runs / binding["run_id"]
        stored = json.loads((directory / "binding.json").read_text())
        assert stored == json.loads(json.dumps(binding))
        assert (directory / SNAPSHOT).is_dir()
        load_snapshot(directory, binding["hermes_snapshot"])
        launcher_called.append(True)
        return 123

    phase = "managed_start"
    result = ManagementService(
        store=store, runtime_root=runs, launcher=controlled_launcher
    ).start(trigger="scheduled")
    assert result["accepted"] and launcher_called
    print(json.dumps({
        "result": "PASS", "candidate_commit": EXPECTED_COMMIT,
        "collaborative_source_modes": shared_source,
        "installed_hermes": importlib.metadata.version("hermes-agent"),
        "installed_reader": importlib.metadata.version("web-listening"),
        "reader_runtime_inventory": "nonempty",
        "reader_runtime_configs": len(runtime_configs),
        "reader_tools_absent": "tools" in reader["absent"],
        "snapshot_policy_members": len(direct_payload["policy"]),
        "managed_start_accepted": True,
        "controlled_launcher_checked_binding": True,
        "outbound_inference": False,
    }, sort_keys=True))
except Exception as exc:
    print(json.dumps({
        "result": "FAIL", "phase": phase,
        "error_type": type(exc).__name__, "error": str(exc)[:240],
    }, sort_keys=True))
    raise SystemExit(1)
finally:
    shutil.rmtree(base)
