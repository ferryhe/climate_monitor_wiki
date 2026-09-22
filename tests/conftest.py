"""Shared governed Runtime fixture for tests replacing per-source acquisition."""

import pytest


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


@pytest.fixture(autouse=True)
def isolated_managed_hermes_inputs(tmp_path_factory, monkeypatch):
    """Managed starts must never snapshot a test runner's real credentials."""
    import os
    from pathlib import Path
    from climate_monitor.hermes_identity import BASE_ENV, CREDENTIAL_ENV

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
    executable.write_text('#!' + str(Path(os.sys.executable).absolute()) + '\n# fixture\n')
    executable.chmod(0o700)
    home = fixture_root / 'managed-hermes-home'
    home.mkdir(mode=0o700)
    for name in ('config.yaml', 'auth.json'):
        (home / name).write_text('{}')
        (home / name).chmod(0o600)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_EXECUTABLE', str(executable))
