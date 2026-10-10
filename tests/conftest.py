"""Shared governed Runtime fixture for tests replacing per-source acquisition."""

from collections import defaultdict

import pytest

from fixture_modes import remove_shared_write


@pytest.fixture(autouse=True)
def isolated_chat_allowance(tmp_path, monkeypatch):
    """API tests represent independent networks unless a test shares its store."""
    import sys
    monkeypatch.setenv("CLIMATE_CHAT_ACCESS_DB", str(tmp_path / "chat-access.sqlite3"))
    if "api_server" in sys.modules:
        monkeypatch.setattr(sys.modules["api_server"], "chat_access_store", None)


def _partition_collected_items(items, shard_count):
    files = defaultdict(list)
    positions = {id(item): position for position, item in enumerate(items)}
    for item in items:
        files[str(item.path)].append(item)

    shards = [[] for _ in range(shard_count)]
    weights = [0] * shard_count
    for path, file_items in sorted(files.items(), key=lambda entry: (-len(entry[1]), entry[0])):
        shard = min(range(shard_count), key=lambda index: (weights[index], index))
        shards[shard].extend(file_items)
        weights[shard] += len(file_items)

    return tuple(tuple(sorted(shard, key=lambda item: positions[id(item)])) for shard in shards)


def pytest_addoption(parser):
    parser.addoption(
        "--ci-shard",
        choices=("1/2", "2/2"),
        help="run one deterministic whole-file CI test shard",
    )


def pytest_collection_modifyitems(config, items):
    selected = config.getoption("--ci-shard")
    if selected is None:
        return
    config._ci_original_items = tuple(items)
    shard = int(selected.split("/", 1)[0]) - 1
    items[:] = _partition_collected_items(items, 2)[shard]


@pytest.fixture
def governed_adapter_runtime(monkeypatch):
    from climate_monitor import web_listening_adapter as adapter

    class Runtime:
        @classmethod
        def open(cls, _root):
            return cls()

        def close(self):
            return None

    monkeypatch.setenv("CLIMATE_MONITOR_ENABLE_LIVE_WEB_LISTENING", "1")
    monkeypatch.setattr(adapter, "_runtime_service_type", lambda: Runtime)


@pytest.fixture(scope="session")
def safe_managed_interpreter(tmp_path_factory):
    """One private executable copy; hosted CI Python may itself be mode 0777."""
    import shutil
    import sys
    from pathlib import Path

    root = tmp_path_factory.mktemp('managed-python')
    root.chmod(0o700)
    binary = root / 'bin/python'
    binary.parent.mkdir(mode=0o700)
    shutil.copyfile(Path(sys.executable).resolve(), binary)
    binary.chmod(0o700)
    # Preserve stdlib discovery for relocatable Python, including -I -S launches.
    config = root / 'pyvenv.cfg'
    config.write_text('home = ' + str(Path(sys._base_executable).resolve().parent)
                      + '\ninclude-system-site-packages = false\n')
    config.chmod(0o600)
    return binary


@pytest.fixture(autouse=True)
def isolated_managed_hermes_inputs(tmp_path_factory, monkeypatch, safe_managed_interpreter):
    """Managed starts must never snapshot a test runner's real credentials."""
    from climate_monitor.hermes_identity import BASE_ENV, CREDENTIAL_ENV
    monkeypatch.delenv("CLIMATE_REGISTRY_DB", raising=False)

    for key in BASE_ENV | CREDENTIAL_ENV:
        if key not in {'PATH', 'HOME', 'LANG', 'LC_ALL'}:
            monkeypatch.delenv(key, raising=False)
    fixture_root = tmp_path_factory.mktemp('hermes-input')
    root = fixture_root / 'managed-hermes-package'
    (root / 'venv/bin').mkdir(parents=True)
    (root / 'hermes_cli').mkdir()
    from hermes_offline_runtime import install_plugin_loader
    install_plugin_loader(root)
    (root / 'hermes_cli/env_loader.py').write_text('def load_hermes_dotenv(**kwargs): return []\n')
    (root / 'pyproject.toml').write_text('[project]\nversion="0.20.5"\n')
    (root / 'hermes_cli/main.py').write_text('def main(): pass\n')
    (root / 'run_agent.py').write_text('# isolated fixture\n')
    executable = root / 'venv/bin/hermes'
    executable.write_text('#!' + str(safe_managed_interpreter) + '\n# fixture\n')
    executable.chmod(0o700)
    # Positive fixture baseline only; preserve private/execute/special bits.
    for path in (root, *root.rglob('*')):
        remove_shared_write(path)
    home = fixture_root / 'managed-hermes-home'
    home.mkdir(mode=0o700)
    for name in ('config.yaml', 'auth.json'):
        (home / name).write_text('{}')
        (home / name).chmod(0o600)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_EXECUTABLE', str(executable))
