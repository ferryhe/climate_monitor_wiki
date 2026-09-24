"""Validation-only canary for the exact PR #162 image or native CI checkout.

This script lives only in the validation branch, outside the unmodified candidate.
It performs no monitoring, browser launch or inference. Native host mode uses
a private installed runtime; unlike the image modes, OS egress is not disabled.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import subprocess
import sys
from urllib.parse import unquote, urlparse

EXPECTED_COMMIT = "82f930cf01b8b90cc2ebc0b702bf4a0db6f55d5f"
native_host = os.environ.get("ISSUE161_NATIVE_HOST") == "1"
base = Path(tempfile.mkdtemp(prefix="issue161-installed-managed-"))
phase = "setup"
try:
    if native_host:
        phase = "native_host_location"
        assert sys.version_info[:2] == (3, 12)
        app = Path(os.environ["ISSUE161_SOURCE_ROOT"])
        private = Path(os.environ["TMPDIR"])
        assert app.is_absolute() and app.resolve() == app
        assert private.parent == Path("/tmp") and private.stat().st_mode & 0o777 == 0o700
        assert private.stat().st_uid == os.getuid()
        assert base.is_relative_to(private)
        assert not Path(__file__).resolve().is_relative_to(app)
        assert not Path.cwd().is_relative_to(app)

        def git_output(root, *args):
            return subprocess.check_output(
                ["git", "-C", str(root), *args], stderr=subprocess.DEVNULL, text=True
            ).strip()

        phase = "native_host_checkout_identity"
        assert git_output(app, "rev-parse", "HEAD") == EXPECTED_COMMIT
        assert not git_output(app, "status", "--porcelain", "--untracked-files=no")
        phase = "native_host_checkout_parent_modes"
        for parent in app.parents:
            assert parent.stat().st_mode & 0o022 == 0
            assert parent.stat().st_uid in {0, os.getuid()}
        group = app.stat().st_gid
        assert group in {os.getegid(), *os.getgroups()}

        phase = "native_host_hermes_binding"
        hermes = private / "hermes-agent"
        assert git_output(hermes, "rev-parse", "HEAD") == "5538bd1f933be2e94aca9755deca5cc59cccc553"
        assert git_output(hermes, "remote", "get-url", "origin") == "https://github.com/NousResearch/hermes-agent.git"
        assert not git_output(hermes, "status", "--porcelain", "--untracked-files=no")
        origin = json.loads(importlib.metadata.distribution("hermes-agent").read_text("direct_url.json"))
        assert origin["dir_info"] == {"editable": True}
        assert urlparse(origin["url"]).scheme == "file"
        assert Path(unquote(urlparse(origin["url"]).path)).resolve() == hermes
        phase = "native_host_reader_binding"
        origin = json.loads(importlib.metadata.distribution("web-listening").read_text("direct_url.json"))
        assert origin["url"] == "https://github.com/ferryhe/web_listening_new.git"
        assert origin["vcs_info"]["vcs"] == "git"
        assert origin["vcs_info"]["commit_id"] == "ac2343f89bc7939736d85f049ebe2beac571034a"
        assert origin["vcs_info"]["requested_revision"] == origin["vcs_info"]["commit_id"]
        phase = "native_host_runtime_identity"
        assert Path(sys.prefix) == private / "venv"
        assert Path(shutil.which("hermes")) == private / "venv/bin/hermes"
        reader_root = Path(os.environ["CLIMATE_WEB_LISTENING_DATA_DIR"])
        assert reader_root == private / "reader"

        def runtime_modes():
            result = {}
            for category, root in (("venv", private / "venv"),
                                   ("hermes", hermes), ("reader", reader_root)):
                root_mode = stat.S_IMODE(root.stat().st_mode)
                if root.is_relative_to(app) or root_mode != 0o700:
                    print(json.dumps({"result": "INFO", "phase": phase,
                                      "runtime_category": category, "reason": "root_mode",
                                      "mode": oct(root_mode)}, sort_keys=True))
                    raise AssertionError()
                for path in (root, *root.rglob("*")):
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode):
                        target_mode = stat.S_IMODE(path.resolve().stat().st_mode)
                        if target_mode & 0o022:
                            print(json.dumps({"result": "INFO", "phase": phase,
                                              "runtime_category": category,
                                              "reason": "symlink_target_write",
                                              "mode": oct(target_mode)}, sort_keys=True))
                            raise AssertionError()
                        continue
                    if info.st_uid != os.getuid() or info.st_mode & 0o022:
                        print(json.dumps({"result": "INFO", "phase": phase,
                                          "runtime_category": category,
                                          "reason": "runtime_owner_or_write",
                                          "uid_matches": info.st_uid == os.getuid(),
                                          "mode": oct(stat.S_IMODE(info.st_mode))}, sort_keys=True))
                        raise AssertionError()
                    result[path] = stat.S_IMODE(info.st_mode)
            return result

        phase = "native_host_runtime_modes_baseline"
        strict_modes = runtime_modes()
        # Only this disposable checkout's application sources gain group write.
        phase = "native_host_collaborative_source_modes"
        for root in (app, app / "climate_monitor", app / "climate_registry", app / "scripts"):
            paths = (root,) if root == app else (root, *root.rglob("*"))
            for path in paths:
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                    continue
                assert info.st_uid == os.getuid() and info.st_gid == group
                assert info.st_mode & 0o002 == 0
                if stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode):
                    path.chmod(stat.S_IMODE(info.st_mode) | 0o020)
                    assert path.stat().st_mode & 0o020
            assert root.stat().st_mode & 0o777 == 0o775
        phase = "native_host_runtime_modes_preserved"
        assert runtime_modes() == strict_modes
        sys.path.insert(0, str(app))

    phase = "imports"
    from climate_monitor.hermes_identity import SNAPSHOT, create_snapshot, load_snapshot
    from climate_monitor.management import ManagementService, TaskDefinitionStore, default_task_definition
    from climate_registry.persistent import initialize_registry
    if native_host:
        import climate_monitor.hermes_identity as identity
        assert Path(identity.__file__) == app / "climate_monitor/hermes_identity.py"
        assert Path(identity.__file__).stat().st_mode & 0o777 == 0o664
        phase = "private_input_negative"
        negative = base / "synthetic-private-input"
        negative.write_text("synthetic marker")
        negative.chmod(0o600)
        identity.secure_read(negative, private=True)
        negative.chmod(0o620)
        try:
            identity.secure_read(negative, private=True)
        except ValueError:
            pass
        else:
            raise AssertionError("private group write accepted")
        negative.unlink()

    phase = "setup"
    assert os.environ.get("CLIMATE_REPOSITORY_COMMIT_SHA") == EXPECTED_COMMIT
    assert importlib.metadata.version("hermes-agent") == "0.20.5"
    assert importlib.metadata.version("web-listening") == "0.1.0"
    shared_source = native_host or os.environ.get("ISSUE161_TEST_SHARED_SOURCE") == "1"
    if shared_source and not native_host:
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
    source = (app if native_host else Path("/app")) / "climate_monitor/hermes_identity.py"
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
    # Synthetic sentinel only. Image modes have --network none; the native
    # job supplies an empty environment/private HOME without real credentials.
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
    if native_host:
        phase = "native_host_postflight"
        snapshots = (direct / SNAPSHOT, *(runs / result_id / SNAPSHOT for result_id in os.listdir(runs)
                                         if (runs / result_id / SNAPSHOT).is_dir()))
        assert len(snapshots) == 2
        for snapshot in snapshots:
            for path in (snapshot, *snapshot.rglob("*")):
                info = path.lstat()
                assert info.st_uid == os.getuid()
                assert stat.S_IMODE(info.st_mode) == (0o700 if stat.S_ISDIR(info.st_mode) else 0o600)
        assert runtime_modes() == strict_modes
        print(json.dumps({
            "native_host": True, "os_egress_disabled": False,
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "playwright_version": "1.62.0",
            "hermes_commit": "5538bd1f933be2e94aca9755deca5cc59cccc553",
            "reader_commit": "ac2343f89bc7939736d85f049ebe2beac571034a",
            "private_group_write_rejected": True, "private_snapshot_modes": True,
            "runtime_modes_unchanged": True, "checkout_owner_matches_uid": True,
            "checkout_gid_is_member": True, "source_root_mode": "0775",
            "source_file_mode": "0664", "snapshot_count": len(snapshots),
        }, sort_keys=True))
    print(json.dumps({
        **({"native_host": True} if native_host else {}),
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
        "error_type": type(exc).__name__,
        **({"native_host": True} if native_host else {"error": str(exc)[:240]}),
    }, sort_keys=True))
    raise SystemExit(1)
finally:
    shutil.rmtree(base)
