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
    source = Path("/app/climate_monitor/hermes_identity.py")
    assert source.is_file() and source.stat().st_mode & 0o022 == 0
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
    direct_payload = load_snapshot(direct, reference)
    assert (direct / SNAPSHOT).is_dir()
    reader = direct_payload["reader_runtime"]
    assert reader["root"] == os.environ["CLIMATE_WEB_LISTENING_DATA_DIR"]
    assert reader["absent"] == []
    assert reader["inventory"]
    runtime_configs = sorted(Path(reader["root"]).glob("tools/*/*/*/runtime.json"))
    assert runtime_configs
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
        "installed_hermes": importlib.metadata.version("hermes-agent"),
        "installed_reader": importlib.metadata.version("web-listening"),
        "reader_runtime_inventory": "nonempty",
        "reader_runtime_configs": len(runtime_configs),
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
