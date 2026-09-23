"""Frozen execution inputs and exclusive effective identity publication."""
import concurrent.futures
import json
import os
from pathlib import Path
import threading

import pytest

from climate_monitor import management
from test_issue94_management_console import _definition, _store


@pytest.fixture
def runtime(tmp_path, monkeypatch, safe_managed_interpreter):
    for key in ('SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE'):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / 'hermes-package'
    (root / 'venv/bin').mkdir(parents=True)
    (root / 'hermes_cli').mkdir()
    from hermes_offline_runtime import install_plugin_loader
    install_plugin_loader(root)
    (root / 'hermes_cli/env_loader.py').write_text('def load_hermes_dotenv(**kwargs): return []\n')
    (root / 'pyproject.toml').write_text('[project]\nversion="0.20.5"\n')
    (root / 'hermes_cli/main.py').write_text('def main(): pass\n')
    (root / 'run_agent.py').write_text('# runtime\n')
    executable = root / 'venv/bin/hermes'
    executable.write_text('#!' + str(safe_managed_interpreter) + '\nfrom hermes_cli.main import main\nmain()\n')
    executable.chmod(0o700)
    home = tmp_path / 'ambient'
    home.mkdir(mode=0o700)
    for name, value in [('config.yaml', '{"model":{"default":"route-A","provider":"provider-A"}}'), ('auth.json', '{}')]:
        (home / name).write_text(value)
        (home / name).chmod(0o600)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_EXECUTABLE', str(executable))
    return home, executable


def test_manager_freezes_before_launch(tmp_path, monkeypatch, runtime):
    home, _ = runtime
    store = _store(tmp_path)
    store.save(_definition(tmp_path), actor='test')
    captured = []
    service = management.ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda b: captured.append(b) or 123)
    started = service.start()
    binding = captured[0]
    assert set(binding['hermes_snapshot']) == {'schema_version', 'sha256'}
    from climate_monitor.hermes_identity import load_snapshot
    (home / 'config.yaml').write_text('{"model":{"default":"route-B"}}')
    frozen = load_snapshot(tmp_path / 'runs' / started['run_id'], binding['hermes_snapshot'])
    assert frozen['config']['model']['default'] == 'route-A'
    assert 'route-A' not in json.dumps(binding)


def test_identity_concurrent_writers_never_overwrite(tmp_path):
    from climate_monitor.hermes_identity import publish_identity
    barrier = threading.Barrier(2)
    def write(model):
        barrier.wait()
        try:
            return publish_identity(tmp_path, {'provider': 'test', 'model': model, 'source': 'run'})
        except ValueError:
            return None
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, ['a', 'b']))
    assert sum(value is not None for value in results) == 1
    winner = json.loads((tmp_path / 'effective-identity.json').read_text())
    assert winner == next(value for value in results if value is not None)


@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'permissions', 'oversized', 'device'])
def test_unsafe_read_rejected_without_blocking(tmp_path, kind):
    from climate_monitor.hermes_identity import secure_read, MAX_FILE_BYTES
    path = tmp_path / 'input'
    if kind == 'symlink':
        path.symlink_to(tmp_path / 'absent')
    elif kind == 'fifo':
        os.mkfifo(path, 0o600)
    elif kind == 'device':
        path = Path('/dev/null')
    else:
        path.write_bytes(b'x')
        path.chmod(0o644 if kind == 'permissions' else 0o600)
        if kind == 'oversized':
            with path.open('r+b') as stream:
                stream.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(ValueError, match='unsafe|limit'):
        secure_read(path, private=True)


def test_collection_mutation_never_publishes(tmp_path, monkeypatch, runtime):
    from climate_monitor import hermes_identity as identity
    home, _ = runtime
    original = identity.secure_read
    changed = False
    def read(path, **kwargs):
        nonlocal changed
        result = original(path, **kwargs)
        if Path(path).name == 'auth.json' and not changed:
            changed = True
            (home / 'config.yaml').write_text('{"model":"changed"}')
        return result
    monkeypatch.setattr(identity, 'secure_read', read)
    run = tmp_path / 'run'
    run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='changed'):
        identity.create_snapshot(run, source='run')
    assert not (run / identity.SNAPSHOT).exists()


@pytest.mark.parametrize('damage', ['complete', 'manifest', 'package', 'executable'])
def test_incomplete_or_drift_fails_closed(tmp_path, runtime, damage):
    from climate_monitor import hermes_identity as identity
    _, executable = runtime
    run = tmp_path / 'run'
    run.mkdir(mode=0o700)
    reference = identity.create_snapshot(run, source='run')
    if damage == 'complete':
        (run / identity.SNAPSHOT / 'complete').unlink()
    elif damage == 'manifest':
        (run / identity.SNAPSHOT / 'manifest.json').write_text('{}')
    elif damage == 'package':
        (executable.parents[2] / 'hermes_cli/main.py').write_text('# changed')
    else:
        executable.write_text('# changed')
    with pytest.raises(ValueError, match='start a fresh run'):
        identity.load_snapshot(run, reference)


def test_snapshotless_binding_is_readable_but_cannot_resume(tmp_path, runtime):
    store = _store(tmp_path)
    store.save(_definition(tmp_path), actor='test')
    service = management.ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda b: 123)
    started = service.start()
    run = tmp_path / 'runs' / started['run_id']
    for name in ('binding.json', 'attempt-1.json'):
        value = json.loads((run / name).read_text())
        value.pop('hermes_snapshot')
        (run / name).write_text(json.dumps(value))
    assert service.binding(started['run_id'])['run_id'] == started['run_id']
    (run / 'attempt-1-result.json').write_text('{"exit_code":75,"retryable":true}')
    with pytest.raises(ValueError, match='start a fresh run'):
        service.resume(started['run_id'])


def test_environment_ca_and_custom_route_frozen(tmp_path, monkeypatch, runtime):
    from climate_monitor import hermes_identity as identity
    home, executable = runtime
    ca = tmp_path / 'ca.pem'
    ca.write_text('test certificate')
    ca.chmod(0o644)
    ca_dir = tmp_path / 'certs'
    ca_dir.mkdir()
    (ca_dir / 'root.pem').write_text('test root')
    config = {'model': {'default': 'route-A', 'provider': 'custom'},
              'providers': {'custom': {'base_url': 'https://example.invalid/v1', 'key_env': 'CUSTOM_TEST_KEY', 'ssl_ca_cert': str(ca)}},
              'mcp_servers': {'unrelated': {}}, 'plugins': {'enabled': ['unrelated']}}
    (home / 'config.yaml').write_text(json.dumps(config))
    monkeypatch.setenv('CUSTOM_TEST_KEY', 'synthetic-test-value')
    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid')
    monkeypatch.setenv('SSL_CERT_FILE', str(ca))
    monkeypatch.setenv('SSL_CERT_DIR', str(ca_dir))
    run = tmp_path / 'run'
    run.mkdir(mode=0o700)
    ref = identity.create_snapshot(run, source='run')
    monkeypatch.setenv('CUSTOM_TEST_KEY', 'changed')
    monkeypatch.setenv('PATH', '/changed')
    monkeypatch.setenv('HTTP_PROXY', 'changed')
    ca.write_text('changed')
    (home / 'auth.json').write_text('{"changed":true}')
    exe, env, child = identity.inference_runtime(run, ref, purpose='report', source='run')
    assert exe == identity.launch_command(child)
    assert exe[1:3] == ['-I', '-S']
    assert identity.load_snapshot(run, ref)['executable'] == str(executable)
    assert env['CUSTOM_TEST_KEY'] == 'synthetic-test-value'
    assert env['HTTP_PROXY'] == 'http://proxy.invalid'
    assert env['PATH'] != '/changed'
    assert Path(env['SSL_CERT_FILE']).read_text() == 'test certificate'
    assert (Path(env['SSL_CERT_DIR']) / 'root.pem').read_text() == 'test root'
    assert json.loads((child / 'auth.json').read_text()) == {}
    projected = json.loads((child / 'config.yaml').read_text())
    assert projected['providers']['custom']['base_url'] == 'https://example.invalid/v1'
    assert projected['mcp_servers'] == {}
    assert 'synthetic-test-value' not in json.dumps(ref)
    for path in (run / identity.SNAPSHOT).rglob('*'):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)


def test_installer_ignores_ambient_after_start(tmp_path, monkeypatch, runtime):
    from types import SimpleNamespace
    from climate_monitor import hermes_acquisition_hooks as hooks
    home, executable = runtime
    store = _store(tmp_path)
    store.save(_definition(tmp_path), actor='test')
    service = management.ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda b: 123)
    started = service.start()
    binding = service.binding(started['run_id'])
    (home / 'config.yaml').unlink()
    os.mkfifo(home / 'config.yaml', 0o600)
    monkeypatch.setattr(hooks.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='climate acquisition hooks verified', stderr=''))
    command = ['ambient-must-not-resolve']
    env, child = hooks.install_hooks(command, tmp_path / 'runs' / started['run_id'] / 'attempt-1.json', binding, {})
    from climate_monitor import hermes_identity as h
    assert command[:3] == [h.load_snapshot(child.parent.parent, binding['hermes_snapshot'])['interpreter'], '-I', '-S']
    assert Path(command[3]) == child.parent / 'bootstrap/launcher.py'
    assert json.loads((child / 'config.yaml').read_text())['model']['default'] == 'route-A'


def test_identical_identity_writers_and_fsync(tmp_path, monkeypatch):
    from climate_monitor import hermes_identity as identity
    calls = []
    original = os.fsync
    monkeypatch.setattr(os, 'fsync', lambda fd: (calls.append(os.fstat(fd).st_mode), original(fd))[-1])
    value = {'provider': 'provider', 'model': 'model', 'source': 'run'}
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        assert list(pool.map(lambda _: identity.publish_identity(tmp_path, value), range(4))) == [value] * 4
    import stat
    assert any(stat.S_ISDIR(mode) for mode in calls)
    assert any(stat.S_ISREG(mode) for mode in calls)
    assert not list(tmp_path.glob('.identity-*'))


def test_unsafe_parent_and_tree_limits(tmp_path, monkeypatch):
    from climate_monitor import hermes_identity as identity
    actual = tmp_path / 'actual'
    actual.mkdir()
    (actual / 'file').write_text('x')
    link = tmp_path / 'link'
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match='unsafe'):
        identity.secure_read(link / 'file')
    monkeypatch.setattr(identity, 'MAX_TREE_FILES', 0)
    with pytest.raises(ValueError, match='limit'):
        identity._tree(actual)


def test_resume_uses_snapshot_after_ambient_inputs_disappear(tmp_path, runtime):
    home, _ = runtime
    store = _store(tmp_path)
    store.save(_definition(tmp_path), actor='test')
    launched = []
    service = management.ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda b: launched.append(b) or 123)
    started = service.start()
    original = launched[0]['hermes_snapshot']
    for path in home.iterdir():
        path.unlink()
    home.rmdir()
    run = tmp_path / 'runs' / started['run_id']
    (run / 'attempt-1-result.json').write_text('{"exit_code":75,"retryable":true}')
    service.resume(started['run_id'])
    assert launched[-1]['hermes_snapshot'] == original
    assert launched[-1]['attempt'] == 2


def test_success_hook_publishes_only_identity_and_checks_source(tmp_path, monkeypatch):
    from climate_monitor import hermes_identity as identity
    monkeypatch.setenv('HERMES_SESSION_SOURCE', 'run')
    identity.observe_hook(tmp_path, 'run', successful=True, provider='provider', model='model',
                          base_url='https://example.invalid', response={'ignored': True})
    assert identity.load_identity(tmp_path) == {'provider': 'provider', 'model': 'model', 'source': 'run'}
    class Stopped(Exception):
        pass
    def stop(code):
        assert code == 65
        raise Stopped()
    monkeypatch.setattr(os, '_exit', stop)
    with pytest.raises(Stopped):
        identity.observe_hook(tmp_path, 'run', successful=False, provider='provider', model='different')
    monkeypatch.setenv('HERMES_SESSION_SOURCE', 'changed')
    with pytest.raises(Stopped):
        identity.observe_hook(tmp_path, 'run', successful=False, provider='provider', model='model')


def test_snapshot_fsync_failure_does_not_publish(tmp_path, monkeypatch, runtime):
    from climate_monitor import hermes_identity as identity
    run = tmp_path / 'run'
    run.mkdir(mode=0o700)
    def fail(fd):
        raise OSError('simulated durability failure')
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError, match='durability'):
        identity.create_snapshot(run, source='run')
    assert not (run / identity.SNAPSHOT).exists()
    assert not list(run.glob('.hermes-stage-*'))


def test_source_change_during_descriptor_read_rejected(tmp_path, monkeypatch):
    from climate_monitor import hermes_identity as identity
    path = tmp_path / 'input'
    path.write_text('initial')
    path.chmod(0o600)
    original = os.read
    changed = False
    target_inode = path.stat().st_ino
    def read(fd, size):
        nonlocal changed
        raw = original(fd, size)
        if not changed and os.fstat(fd).st_ino == target_inode:
            changed = True
            path.write_text('mutated')
        return raw
    monkeypatch.setattr(os, 'read', read)
    with pytest.raises(ValueError, match='changed'):
        identity.secure_read(path, private=True)


def test_owner_and_socket_rejected(tmp_path, monkeypatch):
    import socket
    from types import SimpleNamespace
    from climate_monitor import hermes_identity as identity
    import stat
    with pytest.raises(ValueError, match='ownership'):
        identity._check(SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid() + 100), True)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(tmp_path / 'socket'))
        with pytest.raises(ValueError, match='unsafe'):
            identity.secure_read(tmp_path / 'socket')


def test_dotenv_precedence_and_referenced_credential_file(tmp_path, monkeypatch, runtime):
    from climate_monitor import hermes_identity as identity
    home, _ = runtime
    credential = tmp_path / 'credential.json'
    credential.write_text('{}')
    credential.chmod(0o600)
    (home / '.env').write_text('OPENAI_API_KEY=synthetic-dotenv-value\n')
    (home / '.env').chmod(0o600)
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-parent-value')
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS', str(credential))
    run = tmp_path / 'run'
    run.mkdir(mode=0o700)
    ref = identity.create_snapshot(run, source='run')
    credential.unlink()
    _, env, _ = identity.inference_runtime(run, ref, purpose='report', source='run')
    assert env['OPENAI_API_KEY'] == 'synthetic-dotenv-value'
    assert Path(env['GOOGLE_APPLICATION_CREDENTIALS']).read_text() == '{}'
    assert Path(env['HERMES_MANAGED_DIR']).parent == run / identity.SNAPSHOT


def test_package_dotenv_is_frozen_and_child_loader_cannot_resample(tmp_path, monkeypatch, runtime):
    from climate_monitor import hermes_identity as identity
    import subprocess
    home, executable = runtime
    package = executable.parents[2]
    project_env = package / '.env'
    project_env.write_text('OPENAI_API_KEY=synthetic-package-value\n')
    project_env.chmod(0o600)
    run = tmp_path / 'run'
    run.mkdir(mode=0o700)
    ref = identity.create_snapshot(run, source='run')
    project_env.unlink()
    os.mkfifo(project_env, 0o600)
    _, env, child = identity.inference_runtime(run, ref, purpose='report', source='run')
    assert env['OPENAI_API_KEY'] == 'synthetic-package-value'
    result = subprocess.run([os.sys.executable, '-c',
        'from hermes_cli.env_loader import load_hermes_dotenv; '
        'assert load_hermes_dotenv(project_env="unused") == []; '
        'assert load_hermes_dotenv.__name__ == "frozen_environment"'],
        cwd=child, env=env, capture_output=True, timeout=10)
    assert result.returncode == 0


def test_recursive_yaml_is_rejected_boundedly(tmp_path, runtime):
    from climate_monitor import hermes_identity as identity
    home, _ = runtime
    (home / 'config.yaml').write_text('model: &loop {default: *loop}\n')
    run = tmp_path / 'run'
    run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='invalid private'):
        identity.create_snapshot(run, source='run')
    assert not (run / identity.SNAPSHOT).exists()


def test_bootstrap_uses_the_verified_package_root(tmp_path, runtime):
    from climate_monitor import hermes_identity as identity
    _, executable = runtime
    # An unrelated namespace beside the launcher must not become PYTHONPATH.
    (executable.parent / 'hermes_cli').mkdir()
    run = tmp_path / 'run'
    run.mkdir(mode=0o700)
    ref = identity.create_snapshot(run, source='run')
    payload = identity.load_snapshot(run, ref)
    assert payload['package_root'] == str(executable.parents[2])


def test_review1_effective_route_survives_resume(tmp_path, runtime):
    from climate_monitor import hermes_identity as identity
    home, _ = runtime
    route = {'model': {'name': 'primary', 'api_base': 'https://nested.invalid'},
             'provider': 'custom', 'base_url': 'https://primary.invalid', 'context_length': 8192,
             'fallback_providers': [{'provider': 'commandcode', 'model': 'backup', 'base_url': 'https://backup.invalid'}],
             'fallback_model': {'provider': 'legacy', 'model': 'last'},
             'credential_pool_strategies': {'custom': 'round_robin'},
             'providers': {'custom': {'base_url': 'https://registry.invalid'}},
             'custom_providers': [{'name': 'custom', 'base_url': 'https://custom.invalid'}],
             'smart_model_routing': {'enabled': True}, 'gateway': {'enabled': True}}
    (home / 'config.yaml').write_text(json.dumps(route))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = identity.create_snapshot(run, source='run')
    (home / 'config.yaml').unlink()
    for purpose in ('first', 'resume'):
        _, _, child = identity.inference_runtime(run, ref, purpose=purpose, source='run')
        cfg = json.loads((child / 'config.yaml').read_text())
        assert cfg['model'] == {'default': 'primary', 'provider': 'custom', 'base_url': 'https://primary.invalid', 'context_length': 8192}
        for key in ('fallback_providers', 'fallback_model', 'credential_pool_strategies', 'providers', 'custom_providers', 'smart_model_routing'):
            assert cfg[key] == route[key]
        assert 'gateway' not in cfg


def test_review1_plugin_provider_environment(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as identity
    home, executable = runtime
    plugin = executable.parents[2] / 'plugins/model-providers/commandcode'
    plugin.mkdir(parents=True)
    plugin.joinpath('__init__.py').write_text('_ENV = ("COMMANDCODE_API_KEY", "COMMANDCODE_BASE_URL", "COMMANDCODE_ANTHROPIC_BASE_URL")\n')
    import secrets
    marker = secrets.token_hex(24)
    names = ('COMMANDCODE_API_KEY', 'COMMANDCODE_BASE_URL', 'COMMANDCODE_ANTHROPIC_BASE_URL')
    for name in names:
        monkeypatch.setenv(name, marker)
    (home / 'config.yaml').write_text('{"model":{"provider":"auto"}}')
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = identity.create_snapshot(run, source='run')
    for name in names:
        monkeypatch.delenv(name)
    for purpose in ('first', 'resume'):
        _, env, _ = identity.inference_runtime(run, ref, purpose=purpose, source='run')
        assert all(env.get(name) == marker for name in names)


@pytest.mark.parametrize('source', ['op-env', 'secrets'])
def test_review1_external_sources_fail_before_publication(tmp_path, runtime, source):
    from climate_monitor import hermes_identity as identity
    home, _ = runtime
    if source == 'op-env':
        (home / '.op.env').write_text('')
        (home / '.op.env').chmod(0o600)
    else:
        (home / 'config.yaml').write_text('{"secrets":{"onepassword":{"enabled":true}}}')
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported|fresh'):
        identity.create_snapshot(run, source='run')
    assert not (run / identity.SNAPSHOT).exists()


def test_review1_final_environment_metadata(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as identity
    import hashlib
    ca = tmp_path / 'ca.pem'; ca.write_text('test roots')
    monkeypatch.setenv('SSL_CERT_FILE', str(ca))
    credential = tmp_path / 'credential.json'; credential.write_text('{}'); credential.chmod(0o600)
    monkeypatch.setenv('GOOGLE_APPLICATION_CREDENTIALS', str(credential))
    _, executable = runtime
    certifi = executable.parents[1] / 'lib/python3.11/site-packages/certifi'
    certifi.mkdir(parents=True); (certifi / 'cacert.pem').write_text('test roots')
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = identity.create_snapshot(run, source='run')
    payload = identity.load_snapshot(run, ref)
    assert payload['environment_metadata'] == {k: {'size': len(v.encode()), 'sha256': hashlib.sha256(v.encode()).hexdigest()} for k, v in payload['environment'].items()}


@pytest.mark.parametrize('marker_honored', [True, False])
def test_review1_skills_opt_out(tmp_path, runtime, marker_honored):
    from climate_monitor import hermes_identity as identity
    import subprocess
    _, executable = runtime
    package = executable.parents[2]
    bundled = package / 'skills/example'
    bundled.mkdir(parents=True)
    (bundled / 'SKILL.md').write_text('offline bundled skill')
    # Offline dependency contract for main.py's first-launch call to
    # tools.skills_sync.sync_skills (0.20.5 / 0.21.3): opt-out precedes
    # discovery/seeding. The negative control proves this fixture seeds without
    # the marker; the managed child must never see the bundled content.
    (package / 'hermes_cli/main.py').write_text(
        'from tools.skills_sync import sync_skills\n'
        'def main(): sync_skills(quiet=True)\n')
    (package / 'tools/skills_sync.py').write_text(
        'import os, pathlib, shutil\n'
        'def sync_skills(quiet=False):\n'
        '    home = pathlib.Path(os.environ["HERMES_HOME"])\n'
        + ('    if (home / ".no-bundled-skills").exists(): return {}\n' if marker_honored else '') +
        '    bundled = pathlib.Path(__file__).parents[1] / "skills"\n'
        '    shutil.copytree(bundled, home / "skills", dirs_exist_ok=True)\n'
        '    return {}\n')
    bare = tmp_path / 'unmanaged'; bare.mkdir(mode=0o700)
    subprocess.run([str(executable)], env={'HERMES_HOME': str(bare), 'PYTHONPATH': str(package), 'PYTHONDONTWRITEBYTECODE': '1'}, check=True, capture_output=True)
    assert (bare / 'skills/example/SKILL.md').is_file()
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = identity.create_snapshot(run, source='run')
    for purpose in ('first', 'resume', 'report', 'meeting'):
        (bundled / 'SKILL.md').write_text(purpose)
        exe, env, child = identity.inference_runtime(run, ref, purpose=purpose, source='run')
        subprocess.run(exe, env=env, cwd=child, check=True, capture_output=True)
        assert (child / '.no-bundled-skills').stat().st_mode & 0o777 == 0o600
        assert not (child / 'skills').exists()


def test_review1_generated_plugin_lifecycle(tmp_path, monkeypatch):
    """Offline loader executes the generated register/callback seam in a child."""
    from climate_monitor import hermes_identity as identity
    import subprocess
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = identity.create_snapshot(run, source='run')
    calls = []
    real_run = subprocess.run
    def record(*args, **kwargs):
        calls.append(True)
        return real_run(*args, **kwargs)
    monkeypatch.setattr(identity.subprocess, 'run', record)
    _, env, child = identity.inference_runtime(run, ref, purpose='test', source='run')
    assert calls, 'real identity runtime verification must execute'
    interpreter = identity.load_snapshot(run, ref)['interpreter']
    script = '''
from pathlib import Path
from hermes_cli.plugins import discover_plugins, get_plugin_manager
discover_plugins(force=True)
m = get_plugin_manager()
root = Path(__import__('os').environ['HERMES_HOME']).parent
m.emit('pre_api_request', provider='test', model='model')
assert not (root / 'effective-identity.json').exists()
m.emit('post_api_request', provider='test', model='model')
assert (root / 'effective-identity.json').exists()
m.emit('pre_api_request', provider='test', model='model')
m.emit('post_api_request', provider='test', model='model')
'''
    result = real_run(identity.launch_command(child, '-c', script), env=env, cwd=child, capture_output=True)
    assert result.returncode == 0
    for change in ('model', 'source'):
        divergent = dict(env)
        if change == 'source':
            divergent['HERMES_SESSION_SOURCE'] = 'other'
        code = "from hermes_cli.plugins import discover_plugins,get_plugin_manager; discover_plugins(force=True); get_plugin_manager().emit('pre_api_request',provider='test',model=" + repr('other' if change == 'model' else 'model') + ")"
        result = real_run(identity.launch_command(child, '-c', code), env=divergent, cwd=child, capture_output=True)
        assert result.returncode == 65
        assert not result.stdout and not result.stderr


@pytest.mark.parametrize('purpose', ['report', 'acquisition'])
def test_review1_private_marker_never_enters_public_artifacts(tmp_path, monkeypatch, capsys, caplog, purpose):
    from climate_monitor import hermes_identity as identity
    from scripts import run_agent_acquisition as runner
    import secrets
    marker = secrets.token_hex(32)
    monkeypatch.setenv('OPENAI_API_KEY', marker)
    ambient = Path(os.environ['HERMES_HOME'])
    (ambient / 'config.yaml').write_text(json.dumps({'model': {'api_key': marker, 'default': 'alias'},
        'model_aliases': {'alias': {'model': 'resolved', 'provider': 'custom', 'api_key': marker}}}))
    (ambient / 'auth.json').write_text(json.dumps({'credential': marker}))
    store = _store(tmp_path)
    store.save(_definition(tmp_path), actor='test')
    service = management.ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda binding: 123)
    started = service.start()
    binding = service.binding(started['run_id'])
    run = tmp_path / 'runs' / started['run_id']
    prompt = runner._prompt(run / 'attempt-1.json', binding)
    (run / 'test-prompt.txt').write_text(prompt)
    if purpose == 'report':
        _, env, child = identity.inference_runtime(run, binding['hermes_snapshot'], purpose='report', source=f"climate-acquisition-{started['run_id']}")
    else:
        from climate_monitor.hermes_acquisition_hooks import install_hooks
        env, child = install_hooks(['hermes'], run / 'attempt-1.json', binding, {})
    assert bool(env['OPENAI_API_KEY'] == marker)
    with identity.auth_execution(child, require_identity=True) as outcome:
        from hermes_offline_runtime import successful_api_lifecycle
        successful_api_lifecycle(child, env)
        (child / 'auth.json').write_text(json.dumps({'providers': {'test': {'refresh_token': marker}}}))
        outcome['returncode'] = 0
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err + caplog.text + json.dumps(binding)
    private_hits = []
    for path in run.rglob('*'):
        if path.is_file():
            found = marker.encode() in path.read_bytes()
            if found:
                assert path.is_relative_to(run / identity.SNAPSHOT)
                assert path.stat().st_mode & 0o777 == 0o600
                private_hits.append(path.name)
    assert sorted(private_hits) == ['000000.json', '000001.json', 'auth.json', 'config.yaml', 'manifest.json']


@pytest.mark.parametrize('model,root,expected', [
    ('primary', {'provider': 'custom', 'api_base': 'https://root.invalid'}, {'default': 'primary', 'provider': 'custom', 'base_url': 'https://root.invalid'}),
    ({'default': 'primary', 'provider': 'explicit', 'base_url': 'https://explicit.invalid', 'context_length': 42},
     {'provider': 'ignored', 'base_url': 'https://ignored.invalid', 'context_length': 99},
     {'default': 'primary', 'provider': 'explicit', 'base_url': 'https://explicit.invalid', 'context_length': 42}),
    ({'model': {'provider': 'nested', 'model': 'primary'}, 'provider': 'auto', 'api_base': 'https://nested.invalid'}, {},
     {'default': 'primary', 'provider': 'nested', 'base_url': 'https://nested.invalid'}),
    ({'default': 'primary', 'model': 'ignored', 'name': 'ignored', 'api_base': 'https://nested.invalid'}, {'api_base': 'https://root.invalid'},
     {'default': 'primary', 'base_url': 'https://root.invalid'}),
])
def test_review1_normalization_precedence(tmp_path, runtime, model, root, expected):
    from climate_monitor import hermes_identity as identity
    home, _ = runtime
    (home / 'config.yaml').write_text(json.dumps(dict(root, model=model)))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = identity.create_snapshot(run, source='run')
    (home / 'config.yaml').write_text('{}')
    _, _, child = identity.inference_runtime(run, ref, purpose='resume', source='run')
    assert json.loads((child / 'config.yaml').read_text())['model'] == expected


def test_review1_metadata_load_rejects_inconsistency(tmp_path, runtime):
    from climate_monitor import hermes_identity as identity
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = identity.create_snapshot(run, source='run')
    manifest = run / identity.SNAPSHOT / 'manifest.json'
    payload = json.loads(manifest.read_bytes())
    payload['environment_metadata'] = {}
    raw = identity._bytes(payload)
    manifest.write_bytes(raw)
    digest = identity._digest(raw)
    (run / identity.SNAPSHOT / 'complete').write_text(digest)
    with pytest.raises(ValueError, match='fresh'):
        identity.load_snapshot(run, dict(ref, sha256=digest))


@pytest.mark.parametrize('declaration', ['env_vars=compute()', 'env_vars=(123,)', 'env_vars=NAMES'])
def test_review1_dynamic_provider_declaration_rejected(tmp_path, runtime, declaration):
    from climate_monitor import hermes_identity as identity
    _, executable = runtime
    plugin = executable.parents[2] / 'plugins/model-providers/custom'
    plugin.mkdir(parents=True)
    (plugin / '__init__.py').write_text('profile = ProviderProfile(' + declaration + ')\n')
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported'):
        identity.create_snapshot(run, source='run')
    assert not (run / identity.SNAPSHOT).exists()


def test_review1_provider_literal_factory(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as identity
    _, executable = runtime
    plugin = executable.parents[2] / 'plugins/model-providers/factory'
    plugin.mkdir(parents=True)
    (plugin / '__init__.py').write_text('def profile(name, env_vars):\n    return ProviderProfile(name=name, env_vars=env_vars)\na = profile("a", ("TEST_FACTORY_KEY",))\n')
    monkeypatch.setenv('TEST_FACTORY_KEY', 'offline')
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = identity.create_snapshot(run, source='run')
    assert identity.load_snapshot(run, ref)['environment']['TEST_FACTORY_KEY'] == 'offline'


def test_review1_verifier_rejects_missing_registration(tmp_path, monkeypatch):
    from climate_monitor import hermes_identity as identity
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = identity.create_snapshot(run, source='run')
    original = identity.prepare_home
    def damage(*args, **kwargs):
        payload, config, env = original(*args, **kwargs)
        plugin = Path(env['HERMES_HOME']) / 'plugins' / identity.IDENTITY_PLUGIN / '__init__.py'
        plugin.write_text('def register(ctx): pass\n')
        return payload, config, env
    monkeypatch.setattr(identity, 'prepare_home', damage)
    with pytest.raises(ValueError, match='identity hooks unavailable'):
        identity.inference_runtime(run, ref, purpose='test', source='run')


@pytest.mark.parametrize('source', [
    'NAMES = ("TEST_KEY",)\nif flag:\n    NAMES = computed()\nprofile = ProviderProfile(env_vars=NAMES)\n',
    'def factory(env_vars):\n    env_vars = computed()\n    return ProviderProfile(env_vars=env_vars)\nprofile = factory(("TEST_KEY",))\n',
])
def test_review1_ambiguous_provider_rebinding_rejected(tmp_path, runtime, source):
    from climate_monitor import hermes_identity as identity
    _, executable = runtime
    plugin = executable.parents[2] / 'plugins/model-providers/ambiguous'
    plugin.mkdir(parents=True)
    (plugin / '__init__.py').write_text(source)
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported'):
        identity.create_snapshot(run, source='run')


def test_review2_policy_exists_at_publication_and_is_verified(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    payload = h.load_snapshot(run, ref)
    assert payload.get('policy'), 'policy must be part of published manifest'
    monkeypatch.setattr(h, 'FROZEN_STARTUP', 'raise RuntimeError()')
    _, _, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    path = run / h.SNAPSHOT / 'bootstrap/sitecustomize.py'
    path.write_bytes(path.read_bytes() + b'\n# changed\n')
    with pytest.raises(ValueError, match='fresh'):
        h.load_snapshot(run, ref)


def test_review2_env_references_and_adapter_credentials(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h
    import secrets
    home, executable = runtime
    adapter = executable.parents[2] / 'agent'; adapter.mkdir(exist_ok=True)
    (adapter / 'bedrock_adapter.py').write_text('import os\nvalue = os.getenv("AWS_BEARER_TOKEN_BEDROCK")\nother = os.environ["CUSTOM_ADAPTER_CREDENTIAL"]\n')
    value = secrets.token_hex(24)
    for key in ('CUSTOM_ROUTE_BASE', 'CUSTOM_ROUTE_KEY', 'AWS_BEARER_TOKEN_BEDROCK', 'CUSTOM_ADAPTER_CREDENTIAL'):
        monkeypatch.setenv(key, value)
    (home / 'config.yaml').write_text(json.dumps({'model': {'provider': 'custom', 'base_url': '${env:CUSTOM_ROUTE_BASE}', 'api_key': '${env:CUSTOM_ROUTE_KEY}'}, 'fallback_providers': [{'provider': 'custom', 'model': 'backup', 'base_url': '${env:CUSTOM_ROUTE_BASE}', 'api_key': '${env:CUSTOM_ROUTE_KEY}'}]}))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    payload = h.load_snapshot(run, ref)
    assert bool(payload['config']['model']['base_url'] == value)
    assert all(payload['environment'].get(k) == value for k in ('CUSTOM_ROUTE_KEY', 'AWS_BEARER_TOKEN_BEDROCK', 'CUSTOM_ADAPTER_CREDENTIAL'))
    (home / 'config.yaml').unlink()
    for key in ('CUSTOM_ROUTE_BASE', 'CUSTOM_ROUTE_KEY', 'AWS_BEARER_TOKEN_BEDROCK', 'CUSTOM_ADAPTER_CREDENTIAL'):
        monkeypatch.delenv(key)
    for purpose in ('report', 'meetings'):
        _, env, child = h.inference_runtime(run, ref, purpose=purpose, source='climate-acquisition-run')
        config = json.loads((child / 'config.yaml').read_text())
        assert all(route['base_url'] == value and route['api_key'] == value for route in (config['model'], *config['fallback_providers']))
        assert env['AWS_BEARER_TOKEN_BEDROCK'] == value


@pytest.mark.parametrize('provider', ['github-copilot', 'bedrock', 'vertex', 'azure'])
def test_review2_implicit_credentials_rejected(tmp_path, runtime, provider):
    from climate_monitor import hermes_identity as h
    home, _ = runtime
    (home / 'config.yaml').write_text(json.dumps({'model': {'provider': provider}}))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported|fresh'):
        h.create_snapshot(run)
    assert not (run / h.SNAPSHOT).exists()


def test_review2_auth_tamper_rejected(tmp_path, runtime):
    from climate_monitor import hermes_identity as h
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    _, _, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    (home / 'auth.json').write_text('{"changed":true}')
    with pytest.raises(ValueError, match='auth|fresh'):
        h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')


def test_review2_oauth_refresh_lifecycle(tmp_path, runtime):
    from climate_monitor import hermes_identity as h
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    _, _, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    with h.auth_execution(home) as result:
        import secrets
        (home / 'auth.json').write_text(json.dumps({'providers': {'test': {'refresh_token': secrets.token_hex(24)}}}))
        result['returncode'] = 0
    _, _, resumed = h.inference_runtime(run, ref, purpose='meetings', source='climate-acquisition-run')
    assert (resumed / 'auth.json').read_bytes() == (home / 'auth.json').read_bytes()
    with pytest.raises(ValueError, match='auth|fresh'):
        with h.auth_execution(resumed) as result:
            (resumed / 'auth.json').write_text('{"changed":true}')
            result['returncode'] = 1
    with pytest.raises(ValueError, match='auth|fresh'):
        h.inference_runtime(run, ref, purpose='meetings', source='climate-acquisition-run')


def test_review2_missing_auth_history_cannot_restart_generation_zero(tmp_path, runtime):
    from climate_monitor import hermes_identity as h
    import shutil
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    _, _, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    with h.auth_execution(home) as result:
        (home / 'auth.json').write_text('{"changed":true}')
        result['returncode'] = 0
    shutil.rmtree(run / h.SNAPSHOT / 'auth-generations')
    with pytest.raises(ValueError, match='auth|fresh'):
        h.inference_runtime(run, ref, purpose='meetings', source='climate-acquisition-run')


def test_review2_frozen_plugin_runs_without_live_checkout(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h
    import subprocess
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    original = (run / h.SNAPSHOT / 'policy/identity.py').read_bytes()
    monkeypatch.setattr(h, 'observe_hook', lambda *a, **k: pytest.fail('live observer used'))
    monkeypatch.setattr(h, 'FROZEN_STARTUP', 'raise RuntimeError("changed")')
    _, env, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    code = '''
import sys
class Poison:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('climate_monitor'):
            raise RuntimeError('live application import forbidden')
sys.meta_path.insert(0, Poison())
from hermes_cli.plugins import discover_plugins,get_plugin_manager
discover_plugins(force=True)
m=get_plugin_manager()
m.emit('pre_api_request',provider='offline',model='model')
m.emit('post_api_request',provider='offline',model='model')
m.emit('post_api_request',provider='offline',model='model')
'''
    interpreter = h.load_snapshot(run, ref)['interpreter']
    result = subprocess.run(h.launch_command(home, '-c', code), cwd=home, env=env, capture_output=True)
    assert result.returncode == 0 and not result.stdout and not result.stderr
    assert (home / 'plugins' / h.IDENTITY_PLUGIN / '__init__.py').read_bytes() == original
    assert h.load_identity(run / h.SNAPSHOT)['source'] == 'climate-acquisition-run'
    plugin = home / 'plugins' / h.IDENTITY_PLUGIN / '__init__.py'
    plugin.write_bytes(original + b'\n# corrupt\n')
    with pytest.raises(ValueError, match='plugin|policy'):
        h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')


def test_review2_standalone_identity_concurrent_processes(tmp_path, runtime):
    from climate_monitor import hermes_identity as h
    import subprocess
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    _, env, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    interpreter = h.load_snapshot(run, ref)['interpreter']
    code = "import sys; from hermes_cli.plugins import discover_plugins,get_plugin_manager; discover_plugins(force=True); sys.stdin.read(1); get_plugin_manager().emit('post_api_request',provider='offline',model=sys.argv[1])"
    children = [subprocess.Popen(h.launch_command(home, '-c', code, name), cwd=home, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for name in ('first', 'second')]
    for child in children:
        child.stdin.write(b'x'); child.stdin.flush()
    for child in children:
        out, err = child.communicate(timeout=10)
        assert not out and not err
    assert sorted(child.returncode for child in children) == [0, 65]


@pytest.mark.parametrize('value', ['${env:MISSING_ROUTE_INPUT}', '${vault:UNSUPPORTED}'])
def test_review2_unresolved_reference_fails_before_binding(tmp_path, runtime, value):
    from climate_monitor import hermes_identity as h
    home, _ = runtime
    (home / 'config.yaml').write_text(json.dumps({'model': {'base_url': value}}))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='reference|fresh'):
        h.create_snapshot(run)
    assert not (run / h.SNAPSHOT).exists()


@pytest.mark.parametrize('provider,credential', [('github-copilot','COPILOT_GITHUB_TOKEN'), ('bedrock','AWS_BEARER_TOKEN_BEDROCK'), ('azure','AZURE_FOUNDRY_API_KEY')])
def test_review2_explicit_credentials_and_fallbacks_freeze(tmp_path, runtime, monkeypatch, provider, credential):
    from climate_monitor import hermes_identity as h
    import secrets
    home, executable = runtime
    adapter = executable.parents[2] / 'agent'; adapter.mkdir(exist_ok=True)
    (adapter / 'bedrock_adapter.py').write_text('import os\nvalue=os.environ.get("AWS_BEARER_TOKEN_BEDROCK")\n')
    value = secrets.token_hex(24)
    monkeypatch.setenv(credential, value)
    (home / 'config.yaml').write_text(json.dumps({'model': {'provider': 'custom', 'base_url': '${env:ROUTE_POINTER}'}, 'fallback_providers': [{'provider': provider, 'model': 'backup'}]}))
    monkeypatch.setenv('ROUTE_POINTER', 'https://offline.invalid')
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    monkeypatch.delenv(credential); monkeypatch.delenv('ROUTE_POINTER')
    (home / 'config.yaml').unlink()
    for purpose in ('report', 'meetings'):
        _, env, child = h.inference_runtime(run, ref, purpose=purpose, source='climate-acquisition-run')
        assert bool(env[credential] == value)
        cfg = json.loads((child / 'config.yaml').read_bytes())
        assert cfg['model']['base_url'] == 'https://offline.invalid'
        assert cfg['fallback_providers'][0]['provider'] == provider


def test_review2_auto_does_not_probe_unrelated_cli(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h
    home, executable = runtime
    adapter = executable.parents[2] / 'agent'; adapter.mkdir(exist_ok=True)
    (adapter / 'bedrock_adapter.py').write_text('# SDK chain available\n')
    (home / 'config.yaml').write_text('{"model":{"provider":"auto"}}')
    monkeypatch.setenv('OPENAI_API_KEY', __import__('secrets').token_hex(24))
    monkeypatch.setenv('GITHUB_TOKEN', 'unrelated')
    monkeypatch.setattr(h.shutil, 'which', lambda *a, **k: pytest.fail('credential CLI probed'))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    h.create_snapshot(run)
    monkeypatch.delenv('OPENAI_API_KEY')
    other = tmp_path / 'other'; other.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported'):
        h.create_snapshot(other)


def test_review2_unchanged_auth_and_readonly_probe(tmp_path, runtime):
    from climate_monitor import hermes_identity as h
    from climate_monitor.hermes_auth_state import verify_auth
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    for _ in range(2):
        _, _, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
        with h.auth_execution(home) as result:
            result['returncode'] = 0
        verify_auth(home)
    assert len(list((run / h.SNAPSHOT / 'auth-generations').glob('*.json'))) == 1
    (home / 'auth.json').unlink()
    with pytest.raises(ValueError, match='auth'):
        h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')


def test_review2_acquisition_first_and_feedback_seal_auth(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from scripts import run_agent_acquisition as runner
    from types import SimpleNamespace
    import secrets
    store = _store(tmp_path); store.save(_definition(tmp_path), actor='test')
    service = management.ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda b: 123)
    started = service.start(); binding = service.binding(started['run_id'])
    run = tmp_path / 'runs' / started['run_id']
    monkeypatch.setattr(hooks.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='climate acquisition hooks verified'))
    monkeypatch.setattr(runner, 'RequestBudget', lambda *a: SimpleNamespace(remaining_seconds=lambda: 60))
    monkeypatch.setattr(runner, '_write_runtime', lambda *a, **k: None)
    observed = []
    class Process:
        pid = 123
        def __init__(self, *args, cwd, **kwargs):
            auth = Path(cwd) / 'auth.json'
            from hermes_offline_runtime import successful_api_lifecycle
            successful_api_lifecycle(Path(cwd), kwargs['env'])
            observed.append(h._digest(auth.read_bytes()))
            auth.write_text(json.dumps({'providers': {'test': {'refresh_token': secrets.token_hex(24)}}}))
        def poll(self): return 0
        def wait(self): return 0
    monkeypatch.setattr(runner.subprocess, 'Popen', Process)
    for turn in ('first', 'feedback'):
        assert runner._invoke_hermes(['hermes'], run / (turn + '.log'), run / 'attempt-1.json', binding, float('inf')) == 0
    assert observed[0] != observed[1]
    assert len(list((run / h.SNAPSHOT / 'auth-generations').glob('*.json'))) == 3
    assert all(not p.read_bytes() for p in run.glob('*.log'))


def test_review2_repeated_meeting_and_report_resume_share_refresh(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h
    from scripts import run_meeting_extraction as meetings
    from tests.test_issue87_post_pr106 import _help_with_query_file
    from types import SimpleNamespace
    import hashlib, secrets, subprocess
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    binding = run / 'attempt-1.json'
    binding.write_text(json.dumps({'run_id': 'run', 'hermes_snapshot': ref}))
    real_run = subprocess.run
    prior = []
    def process(command, **kwargs):
        if '-c' in command:
            return real_run(command, **kwargs)
        if '--help' in command:
            return SimpleNamespace(returncode=0, stdout=_help_with_query_file())
        path = Path(kwargs['cwd']) / 'auth.json'
        from hermes_offline_runtime import successful_api_lifecycle
        successful_api_lifecycle(Path(kwargs['cwd']), kwargs['env'])
        prior.append(h._digest(path.read_bytes()))
        path.write_text(json.dumps({'providers': {'test': {'refresh_token': secrets.token_hex(24)}}}))
        return SimpleNamespace(returncode=0, stdout='{}', stderr='session_id: 20260908_120234_3b2f4b')
    monkeypatch.setattr(subprocess, 'run', process)
    invoke = meetings._extractor('', '', binding)
    request = {'article_body': 'offline body', 'content_sha256': hashlib.sha256(b'offline body').hexdigest(),
               'content_version_id': 'test', 'source_url': 'https://offline.invalid', 'prompt': 'test'}
    invoke(request); invoke(request)
    assert prior[0] != prior[1]
    _, _, report = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    _, _, meeting = h.inference_runtime(run, ref, purpose='meetings', source='climate-acquisition-run')
    assert (report / 'auth.json').read_bytes() == (meeting / 'auth.json').read_bytes()
    assert len(list((run / h.SNAPSHOT / 'auth-generations').glob('*.json'))) == 3


def test_review2_only_effective_fallbacks_require_credentials(tmp_path, runtime):
    from climate_monitor import hermes_identity as h
    import secrets
    home, _ = runtime
    (home / 'config.yaml').write_text(json.dumps({
        'model': {'provider': 'custom', 'base_url': 'https://offline.invalid'},
        'fallback_providers': [{'provider': 'bedrock'},  # missing model: disabled by Hermes
                               {'provider': 'azure-foundry', 'model': 'backup', 'api_key': secrets.token_hex(24)}],
        'fallback_model': {'provider': 'azure-foundry', 'model': 'backup'},  # duplicate: first entry wins
    }))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    h.create_snapshot(run)


def test_review2_shared_auth_store_rejected_without_reading(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h
    home, _ = runtime
    path = tmp_path / 'external'; os.mkfifo(path, 0o600)
    monkeypatch.setenv('HERMES_SHARED_AUTH_DIR', str(path))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported|fresh'):
        h.create_snapshot(run)


def test_review2_truncated_auth_history_fails_closed(tmp_path, runtime):
    from climate_monitor import hermes_identity as h
    import secrets
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    _, _, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    with h.auth_execution(home) as result:
        (home / 'auth.json').write_text(json.dumps({'providers': {'test': {'refresh_token': secrets.token_hex(24)}}}))
        result['returncode'] = 0
    (run / h.SNAPSHOT / 'auth-generations' / '000001.json').unlink()
    with pytest.raises(ValueError, match='fresh'):
        h.inference_runtime(run, ref, purpose='meetings', source='climate-acquisition-run')


@pytest.mark.parametrize('provider,variable', [('bedrock', 'AWS_SHARED_CREDENTIALS_FILE'), ('vertex', 'VERTEX_CREDENTIALS_PATH')])
def test_review2_explicit_credential_files_survive_removal(tmp_path, runtime, monkeypatch, provider, variable):
    from climate_monitor import hermes_identity as h
    import secrets
    ambient, _ = runtime
    marker = secrets.token_hex(24)
    credentials = tmp_path / 'credential-input'
    raw = ('[default]\naws_access_key_id = ' + marker + '\naws_secret_access_key = ' + marker + '\n') if provider == 'bedrock' else json.dumps({
        'type': 'service_account', 'client_email': 'offline@example.invalid', 'private_key': marker, 'token_uri': 'https://offline.invalid'})
    credentials.write_text(raw); credentials.chmod(0o600)
    monkeypatch.setenv(variable, str(credentials))
    (ambient / 'config.yaml').write_text(json.dumps({'model': {'provider': provider, 'default': 'offline'}}))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    credentials.unlink(); monkeypatch.delenv(variable)
    for purpose in ('report', 'meetings'):
        _, env, _ = h.inference_runtime(run, ref, purpose=purpose, source='climate-acquisition-run')
        private = Path(env[variable])
        assert private.read_text() == raw
        assert private.stat().st_mode & 0o777 == 0o600
    assert marker not in json.dumps(ref)


@pytest.mark.parametrize('provider', ['vertex-ai', 'gcp-vertex', 'vertexai', 'aws', 'amazon', 'amazon-bedrock', 'github-model', 'github-copilot-acp', 'copilot-acp-agent', 'claude', 'claude-code', 'minimax-portal', 'qwen-cli', 'grok-oauth', 'azure-ai-foundry', 'azure-ai', 'claude-oauth', 'codex', 'openai_codex', 'nous-portal', 'nousresearch', 'minimax-oauth-io', 'qwen'])
def test_review2_provider_aliases_cannot_bypass_credential_contract(tmp_path, runtime, provider):
    from climate_monitor import hermes_identity as h
    ambient, executable = runtime
    (executable.parents[2] / 'hermes_cli/auth.py').write_text('# model auth registry\n')
    (ambient / 'config.yaml').write_text(json.dumps({'model': {'provider': provider, 'default': 'offline'}}))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported|fresh'):
        h.create_snapshot(run, environ={'HERMES_HOME': str(ambient), 'HERMES_EXECUTABLE': str(executable), 'PATH': '/usr/bin:/bin'})
    assert not (run / h.SNAPSHOT).exists()


@pytest.mark.parametrize('provider', ['anthropic', 'openai-codex'])
def test_review2_frozen_oauth_state_remains_supported(tmp_path, runtime, provider):
    from climate_monitor import hermes_identity as h
    import secrets
    ambient, executable = runtime
    (executable.parents[2] / 'hermes_cli/auth.py').write_text('# model auth registry\n')
    (ambient / 'config.yaml').write_text(json.dumps({'model': {'provider': provider, 'default': 'offline'}}))
    initial = {'active_provider': provider, 'providers': {provider: {'access_token': secrets.token_hex(24), 'refresh_token': secrets.token_hex(24)}}}
    (ambient / 'auth.json').write_text(json.dumps(initial))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run, environ={'HERMES_HOME': str(ambient), 'HERMES_EXECUTABLE': str(executable), 'PATH': '/usr/bin:/bin'})
    (ambient / 'auth.json').unlink()
    _, _, child = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    assert bool(json.loads((child / 'auth.json').read_bytes()) == initial)


def test_review2_sdk_default_profile_is_not_silently_dropped(tmp_path, runtime):
    from climate_monitor import hermes_identity as h
    import secrets
    ambient, executable = runtime
    (ambient / 'config.yaml').write_text('{"model":{"provider":"bedrock"}}')
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported|fresh'):
        h.create_snapshot(run, environ={'HERMES_HOME': str(ambient), 'HERMES_EXECUTABLE': str(executable), 'PATH': '/usr/bin:/bin',
                                       'AWS_DEFAULT_PROFILE': 'offline', 'AWS_ACCESS_KEY_ID': secrets.token_hex(24), 'AWS_SECRET_ACCESS_KEY': secrets.token_hex(24)})
    assert not (run / h.SNAPSHOT).exists()


def test_review2_auto_does_not_treat_oauth_profile_names_as_api_keys(tmp_path, runtime):
    from climate_monitor import hermes_identity as h
    import secrets
    ambient, executable = runtime
    package = executable.parents[2]
    plugin = package / 'plugins/model-providers/qwen'; plugin.mkdir(parents=True)
    (plugin / '__init__.py').write_text('profile = ProviderProfile(name="qwen-oauth", env_vars=("QWEN_API_KEY",), auth_type="oauth_external")\n')
    (package / 'agent').mkdir(exist_ok=True)
    (package / 'agent/bedrock_adapter.py').write_text('# SDK fallback available\n')
    (ambient / 'config.yaml').write_text('{"model":{"provider":"auto"}}')
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported|fresh'):
        h.create_snapshot(run, environ={'HERMES_HOME': str(ambient), 'HERMES_EXECUTABLE': str(executable), 'PATH': '/usr/bin:/bin', 'QWEN_API_KEY': secrets.token_hex(24)})


@pytest.mark.parametrize('kind', ['unknown-active', 'external-sdk-config'])
def test_review2_auto_auth_state_cannot_bypass_sdk_gate(tmp_path, runtime, kind):
    from climate_monitor import hermes_identity as h
    import secrets
    ambient, executable = runtime
    package = executable.parents[2]
    (package / 'agent').mkdir(exist_ok=True)
    (package / 'agent/bedrock_adapter.py').write_text('# SDK fallback available\n')
    (ambient / 'config.yaml').write_text('{"model":{"provider":"auto"}}')
    provider = 'missing-provider' if kind == 'unknown-active' else 'openai-codex'
    (ambient / 'auth.json').write_text(json.dumps({'active_provider': provider, 'providers': {provider: {'refresh_token': secrets.token_hex(24)}}}))
    environment = {'HERMES_HOME': str(ambient), 'HERMES_EXECUTABLE': str(executable), 'PATH': '/usr/bin:/bin'}
    if kind == 'external-sdk-config':
        config = tmp_path / 'aws-input'; config.write_text('[default]\ncredential_process = offline-command\n'); config.chmod(0o600)
        environment['AWS_CONFIG_FILE'] = str(config)
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported|fresh'):
        h.create_snapshot(run, environ=environment)
    assert not (run / h.SNAPSHOT).exists()


@pytest.mark.parametrize('kind', ['failed', 'crashed'])
def test_review2_unsealed_auth_blocks_other_child_homes(tmp_path, runtime, kind):
    from climate_monitor import hermes_identity as h
    import subprocess, sys
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    _, _, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    if kind == 'failed':
        with pytest.raises(ValueError, match='auth'):
            with h.auth_execution(home) as result:
                (home / 'auth.json').write_text('{"changed":true}')
                result['returncode'] = 1
    else:
        code = "import os,sys; from pathlib import Path; from climate_monitor.hermes_identity import auth_execution\nwith auth_execution(Path(sys.argv[1])):\n    (Path(sys.argv[1]) / 'auth.json').write_text('{\"changed\":true}')\n    os._exit(2)\n"
        process = subprocess.run([sys.executable, '-c', code, str(home)], env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(Path.cwd())}, capture_output=True)
        assert process.returncode == 2
        assert not process.stdout and not process.stderr
    with pytest.raises(ValueError, match='fresh'):
        h.inference_runtime(run, ref, purpose='meetings', source='climate-acquisition-run')


@pytest.mark.parametrize('site', ['primary', 'environment', 'fallback', 'smart'])
@pytest.mark.parametrize('credential', ['inline', 'key_env', 'reference'])
def test_review3_alias_route_frozen(tmp_path, runtime, monkeypatch, site, credential):
    from climate_monitor import hermes_identity as h
    import secrets, yaml
    ambient, _ = runtime
    marker = secrets.token_hex(24)
    monkeypatch.setenv('ALIAS_ROUTE_KEY', marker)
    monkeypatch.setenv('ALIAS_ROUTE_BASE', 'https://offline.invalid/v1')
    alias = {'model': 'resolved-model', 'provider': 'custom:Private Route',
             'base_url': '${env:ALIAS_ROUTE_BASE}', 'context_length': 8192}
    alias.update({'api_key': marker} if credential == 'inline' else
                 {'key_env': 'ALIAS_ROUTE_KEY'} if credential == 'key_env' else
                 {'api_key': '${env:ALIAS_ROUTE_KEY}'})
    config = {'model': {'default': 'active-alias', 'provider': 'custom'},
              'model_aliases': {'active-alias': alias}, 'ui': {'unrelated': True}}
    if site == 'environment':
        config['model']['default'] = 'other'
        monkeypatch.setenv('HERMES_INFERENCE_MODEL', 'active-alias')
    if site in ('fallback', 'smart'):
        config['model']['default'] = 'other'
        config['fallback_providers' if site == 'fallback' else 'smart_model_routing'] = [
            {'provider': 'custom', 'model': 'active-alias'}]
    (ambient / 'config.yaml').write_text(json.dumps(config))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    (ambient / 'config.yaml').unlink()
    monkeypatch.delenv('ALIAS_ROUTE_KEY'); monkeypatch.delenv('ALIAS_ROUTE_BASE')
    for purpose in ('report', 'meetings', 'report'):
        _, env, home = h.inference_runtime(run, ref, purpose=purpose, source='climate-acquisition-run')
        frozen = yaml.safe_load((home / 'config.yaml').read_text())
        actual = frozen['model_aliases']['active-alias']
        assert actual['model'] == 'resolved-model'
        assert actual['provider'] == alias['provider']
        assert actual['base_url'] == 'https://offline.invalid/v1'
        assert bool((env['ALIAS_ROUTE_KEY'] if credential == 'key_env' else actual['api_key']) == marker)
        assert 'ui' not in frozen


@pytest.mark.parametrize('site', ['primary', 'fallback', 'smart', 'environment'])
def test_review3_alias_missing_key_rejected(tmp_path, runtime, monkeypatch, site):
    from climate_monitor import hermes_identity as h
    ambient, _ = runtime
    monkeypatch.delenv('MISSING_ALIAS_KEY', raising=False)
    config = {'model': {'default': 'alias', 'provider': 'custom'},
              'model_aliases': {'alias': {'model': 'actual', 'provider': 'custom', 'key_env': 'MISSING_ALIAS_KEY'}}}
    if site == 'environment':
        config['model']['default'] = 'other'; monkeypatch.setenv('HERMES_INFERENCE_MODEL', 'alias')
    elif site != 'primary':
        config['model']['default'] = 'other'
        config['fallback_providers' if site == 'fallback' else 'smart_model_routing'] = [{'provider': 'custom', 'model': 'alias'}]
    (ambient / 'config.yaml').write_text(json.dumps(config))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='fresh|unsupported'):
        h.create_snapshot(run)
    assert not (run / h.SNAPSHOT).exists()


@pytest.mark.parametrize('schema', ['providers', 'custom_providers'])
@pytest.mark.parametrize('site', ['primary', 'fallback', 'smart', 'alias'])
def test_review3_named_custom_command_rejected(tmp_path, runtime, schema, site):
    from climate_monitor import hermes_identity as h
    ambient, _ = runtime
    sentinel = tmp_path / 'must-not-execute'
    entry = {'name': 'Private Route', 'provider_key': 'private-route',
             'base_url': 'https://offline.invalid', 'key_cmd': 'touch ' + str(sentinel)}
    route = {'provider': 'custom:private-route', 'model': 'offline'}
    config = {'model': dict(route, default='offline'),
              schema: {'private-route': entry} if schema == 'providers' else [entry]}
    if site != 'primary':
        config['model'] = {'provider': 'custom', 'default': 'other', 'base_url': 'https://other.invalid'}
        if site == 'alias':
            config['model']['default'] = 'alias'; config['model_aliases'] = {'alias': route}
        else:
            config['fallback_providers' if site == 'fallback' else 'smart_model_routing'] = [route]
    (ambient / 'config.yaml').write_text(json.dumps(config))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported|fresh'):
        h.create_snapshot(run)
    assert not sentinel.exists()
    assert not (run / h.SNAPSHOT).exists()


def test_review3_success_requires_identity_after_auth_sealing(tmp_path, runtime):
    from climate_monitor import hermes_identity as h
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    _, _, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    with pytest.raises(ValueError, match='identity|fresh'):
        with h.auth_execution(home, require_identity=True) as result:
            (home / 'auth.json').write_text('{"providers":{}}')
            result['returncode'] = 0
    assert not (run / h.SNAPSHOT / 'auth-inflight.json').exists()
    from climate_monitor.hermes_auth_state import verify_auth
    verify_auth(home)


@pytest.mark.parametrize('kind', ['acquisition', 'feedback', 'report', 'report-resume', 'meeting', 'meeting-retry'])
@pytest.mark.parametrize('publish', [False, True])
def test_review3_wrapper_requires_plugin_success(tmp_path, runtime, monkeypatch, kind, publish):
    from climate_monitor import hermes_identity as h
    from scripts import run_agent_acquisition as acquisition, run_climate_monitor as report, run_meeting_extraction as meetings
    from types import SimpleNamespace
    from hermes_offline_runtime import successful_api_lifecycle
    from tests.test_issue87_post_pr106 import _help_with_query_file
    import subprocess, hashlib
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    binding = {'run_id': 'run', 'hermes_snapshot': ref, 'checkpoint_dir': str(run / 'checkpoint')}
    path = run / 'attempt-1.json'; path.write_text(json.dumps(binding))
    _, env, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    calls = []
    def infer():
        calls.append(True)
        (home / 'auth.json').write_text('{"providers":{}}')
        if publish:
            successful_api_lifecycle(home, env)
    original_run = subprocess.run
    def process(command, **kwargs):
        if '-c' in command:
            return original_run(command, **kwargs)
        if '--help' in command:
            return SimpleNamespace(returncode=0, stdout=_help_with_query_file())
        infer()
        return SimpleNamespace(returncode=0, stdout='{}', stderr='session_id: 20260908_120234_3b2f4b')
    monkeypatch.setattr(subprocess, 'run', process)
    if kind in ('acquisition', 'feedback'):
        monkeypatch.setattr(acquisition, 'install_hooks', lambda *a: (env, home))
        monkeypatch.setattr(acquisition, 'RequestBudget', lambda *a: SimpleNamespace(remaining_seconds=lambda: 60))
        monkeypatch.setattr(acquisition, '_write_runtime', lambda *a, **k: None)
        class Process:
            pid = 123
            def __init__(self, *a, **kw): infer()
            def poll(self): return 0
            def wait(self): return 0
        monkeypatch.setattr(subprocess, 'Popen', Process)
        invoke = lambda: acquisition._invoke_hermes(['hermes'], run / 'response.log', path, binding, float('inf'))
    elif kind.startswith('report'):
        monkeypatch.setattr(report, '_managed_inference_runtime', lambda args: (str(runtime[1]), env, home))
        args = SimpleNamespace(task_binding=str(path), model='', model_provider='', authoring_timeout=5)
        checkpoint = run / 'checkpoint.json'
        if kind == 'report-resume':
            checkpoint.write_text(json.dumps({'input_sha256': report._canonical_digest({'instruction': 'offline', 'model': '', 'provider': ''}), 'status': 'failed', 'attempt': 1}))
        invoke = lambda: report._checkpointed_authoring(checkpoint, 'offline', args=args, help_stdout=_help_with_query_file(), validate=lambda value: value)
    else:
        monkeypatch.setattr(h, 'inference_runtime', lambda *a, **k: (str(runtime[1]), env, home))
        extractor = meetings._extractor('', '', path)
        request = {'article_body': 'offline', 'content_sha256': hashlib.sha256(b'offline').hexdigest(),
                   'content_version_id': 'test', 'source_url': 'https://offline.invalid', 'prompt': 'offline'}
        invoke = lambda: extractor(request)
    if publish:
        invoke(); invoke()  # equal later successes reuse the create-once identity
        assert h.require_effective_identity(home.parent, 'climate-acquisition-run')
    else:
        with pytest.raises((ValueError, SystemExit), match='identity|fresh|authoring'):
            invoke()
    assert calls
    from climate_monitor.hermes_auth_state import verify_auth
    verify_auth(home)  # even an identity failure must leave refresh state sealed
    assert not (home.parent / 'auth-inflight.json').exists()


@pytest.mark.parametrize('variable', ['OPENAI_API_KEY', 'ANTHROPIC_AUTH_TOKEN'])
def test_review3_nonascii_credentials_fail_closed(tmp_path, runtime, monkeypatch, variable):
    from climate_monitor import hermes_identity as h
    monkeypatch.setenv(variable, __import__('secrets').token_hex(16) + chr(0x200b))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported|fresh'):
        h.create_snapshot(run)


@pytest.mark.parametrize('tamper', ['missing-link', 'run_id', 'acquisition_batch_id', 'task_version', 'hermes_snapshot', 'valid'])
def test_review3_meeting_binding_link(tmp_path, runtime, monkeypatch, tamper):
    from climate_monitor import hermes_identity as h
    from scripts import run_meeting_extraction as worker
    import hashlib
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    acquisition = {'run_id': 'run', 'acquisition_batch_id': 'batch', 'task_version': 1,
                   'registry_database': str(tmp_path / 'registry'), 'hermes_snapshot': ref}
    path = run / 'attempt-1.json'; path.write_text(json.dumps(acquisition))
    binding = {'schema_version': 'climate-meeting-worker-binding.v1', 'acquisition_binding_path': str(path),
               'acquisition_run_id': 'run', 'acquisition_batch_id': 'batch', 'task_version': 1,
               'registry_database': acquisition['registry_database'], 'hermes_snapshot': ref,
               'meeting_attempt': 1, 'retry_failed': False, 'retry_meeting_run_id': None,
               'prompt_version': 1, 'prompt_text': 'offline', 'prompt_sha256': hashlib.sha256(b'offline').hexdigest()}
    if tamper == 'missing-link':
        binding.pop('acquisition_binding_path')
    elif tamper != 'valid':
        acquisition[tamper] = 'changed'
        path.write_text(json.dumps(acquisition))
    calls = []
    monkeypatch.setattr(worker, 'process_batch', lambda *a, **k: calls.append(True) or {'status': 'succeeded'})
    if tamper == 'valid':
        assert worker.run(binding)['status'] == 'succeeded'
        assert calls
    else:
        with pytest.raises(ValueError, match='fresh'):
            worker.run(binding)
        assert not calls


@pytest.mark.parametrize('provider', ['Private Route', 'private-route', 'custom:private-route', 'CUSTOM:PRIVATE-ROUTE'])
@pytest.mark.parametrize('schema', ['providers', 'custom_providers'])
@pytest.mark.parametrize('credential', ['key_env', 'inline'])
def test_review3_named_custom_frozen_credentials(tmp_path, runtime, monkeypatch, provider, schema, credential):
    from climate_monitor import hermes_identity as h
    import secrets
    ambient, _ = runtime
    marker = secrets.token_hex(24)
    monkeypatch.setenv('CUSTOM_NAMED_KEY', marker)
    entry = {'name': 'Private Route', 'provider_key': 'private-route', 'base_url': 'https://offline.invalid',
             **({'key_env': 'CUSTOM_NAMED_KEY'} if credential == 'key_env' else {'api_key': marker})}
    config = {'model': {'provider': provider, 'default': 'offline'},
              schema: {'private-route': entry} if schema == 'providers' else [entry]}
    (ambient / 'config.yaml').write_text(json.dumps(config))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    (ambient / 'config.yaml').unlink(); monkeypatch.delenv('CUSTOM_NAMED_KEY')
    for purpose in ('report', 'meetings'):
        _, env, home = h.inference_runtime(run, ref, purpose=purpose, source='climate-acquisition-run')
        actual = json.loads((home / 'config.yaml').read_text())[schema]
        actual = actual['private-route'] if schema == 'providers' else actual[0]
        assert bool((env[actual['key_env']] if credential == 'key_env' else actual['api_key']) == marker)


@pytest.mark.parametrize('field,value', [('credential_process', 'never-execute'), ('auth_mode', 'managed_identity'), ('auth_type', 'default')])
def test_review3_custom_nested_external_credentials_rejected(tmp_path, runtime, field, value):
    from climate_monitor import hermes_identity as h
    ambient, _ = runtime
    config = {'model': {'provider': 'custom:route', 'default': 'offline'},
              'providers': {'route': {'base_url': 'https://offline.invalid', 'auth': {field: value}}}}
    (ambient / 'config.yaml').write_text(json.dumps(config))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported|fresh'):
        h.create_snapshot(run)


@pytest.mark.parametrize('provider', ['bedrock', 'github-copilot', 'vertex', 'azure'])
def test_review3_alias_implicit_credentials_rejected(tmp_path, runtime, provider):
    from climate_monitor import hermes_identity as h
    ambient, _ = runtime
    (ambient / 'config.yaml').write_text(json.dumps({'model': {'provider': 'custom', 'default': 'alias'},
        'model_aliases': {'alias': {'model': 'resolved', 'provider': provider}}}))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported|fresh'):
        h.create_snapshot(run)


def test_review3_completed_report_requires_bound_identity(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h
    from scripts import run_climate_monitor as report
    from types import SimpleNamespace
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    _, env, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    monkeypatch.setattr(report, '_managed_inference_runtime', lambda args: (str(runtime[1]), env, home))
    path = run / 'complete.json'
    path.write_text(json.dumps({'input_sha256': report._canonical_digest({'instruction': 'offline', 'model': '', 'provider': ''}),
                               'status': 'completed', 'response': {}}))
    args = SimpleNamespace(task_binding='bound', model='', model_provider='')
    with pytest.raises(ValueError, match='identity|fresh'):
        report._checkpointed_authoring(path, 'offline', args=args, help_stdout='', validate=lambda value: value)
    from hermes_offline_runtime import successful_api_lifecycle
    successful_api_lifecycle(home, env)
    assert report._checkpointed_authoring(path, 'offline', args=args, help_stdout='', validate=lambda value: value) == {}


def test_review3_environment_alias_supersedes_configured_provider(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h
    ambient, _ = runtime
    (ambient / 'config.yaml').write_text(json.dumps({'model': {'provider': 'bedrock', 'default': 'stale'},
        'model_aliases': {'alias': {'model': 'resolved', 'provider': 'custom', 'base_url': 'https://offline.invalid'}}}))
    monkeypatch.setenv('HERMES_INFERENCE_MODEL', 'alias')
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    assert h.load_snapshot(run, ref)['environment']['HERMES_INFERENCE_MODEL'] == 'alias'


@pytest.mark.parametrize('change', ['missing', 'source', 'permissions', 'malformed'])
def test_review3_required_identity_validation(tmp_path, change):
    from climate_monitor import hermes_identity as h
    root = tmp_path / 'private'; root.mkdir(mode=0o700)
    if change != 'missing':
        h.publish_identity(root, {'provider': 'offline', 'model': 'offline-model', 'source': 'expected'})
        path = root / 'effective-identity.json'
        if change == 'permissions': path.chmod(0o644)
        if change == 'malformed': path.write_text('{"source":"expected"}')
    with pytest.raises(ValueError, match='identity|fresh'):
        h.require_effective_identity(root, 'other' if change == 'source' else 'expected')


def test_review3_identity_private_directory_required(tmp_path):
    from climate_monitor import hermes_identity as h
    root = tmp_path / 'identity'; root.mkdir(mode=0o700)
    h.publish_identity(root, {'provider': 'offline', 'model': 'offline', 'source': 'run'})
    root.chmod(0o755)
    with pytest.raises(ValueError, match='unsafe|identity|fresh'):
        h.require_effective_identity(root, 'run')


def test_review3_trimmed_alias_key_env(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h
    ambient, _ = runtime
    import secrets
    value = secrets.token_hex(24)
    monkeypatch.setenv('TRIMMED_ALIAS_KEY', value)
    (ambient / 'config.yaml').write_text(json.dumps({'model': {'default': 'alias'},
        'model_aliases': {'alias': {'model': 'actual', 'provider': 'custom', 'key_env': ' TRIMMED_ALIAS_KEY '}}}))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    assert bool(h.load_snapshot(run, ref)['environment']['TRIMMED_ALIAS_KEY'] == value)


def test_review3_meeting_link_rechecked_before_each_inference(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h
    from scripts import run_meeting_extraction as worker
    import hashlib
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    acquisition = {'run_id': 'run', 'acquisition_batch_id': 'batch', 'task_version': 1,
                   'registry_database': str(tmp_path / 'registry'), 'hermes_snapshot': ref}
    path = run / 'attempt-1.json'; path.write_text(json.dumps(acquisition))
    binding = {'schema_version': 'climate-meeting-worker-binding.v1', 'acquisition_binding_path': str(path),
               'acquisition_run_id': 'run', 'acquisition_batch_id': 'batch', 'task_version': 1,
               'registry_database': acquisition['registry_database'], 'hermes_snapshot': ref,
               'meeting_attempt': 1, 'retry_failed': False, 'retry_meeting_run_id': None,
               'prompt_version': 1, 'prompt_text': 'offline', 'prompt_sha256': hashlib.sha256(b'offline').hexdigest()}
    monkeypatch.setattr(h, 'inference_runtime', lambda *a, **kw: pytest.fail('changed link reached inference'))
    def process(*args, extractor, **kwargs):
        acquisition['task_version'] = 2
        path.write_text(json.dumps(acquisition))
        return extractor({})
    monkeypatch.setattr(worker, 'process_batch', process)
    with pytest.raises(ValueError, match='fresh'):
        worker.run(binding)


@pytest.mark.parametrize('kind', ['root', 'simple'])
def test_review3_ambiguous_alias_names_rejected(tmp_path, runtime, kind):
    from climate_monitor import hermes_identity as h
    ambient, _ = runtime
    # Canonical manifest sorting must not reverse case-insensitive alias
    # precedence and expose a route that collection never checked.
    config = {'model': {'default': 'alias', 'provider': 'custom', 'base_url': 'https://offline.invalid'}}
    if kind == 'root':
        config['model_aliases'] = {'alias': {'model': 'external', 'provider': 'bedrock'},
                                  'ALIAS': {'model': 'offline', 'provider': 'custom'}}
    else:
        config['model']['aliases'] = {'alias': 'custom/offline', 'ALIAS': 'bedrock/external'}
    (ambient / 'config.yaml').write_text(json.dumps(config))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsupported|ambiguous|fresh'):
        h.create_snapshot(run)
    assert not (run / h.SNAPSHOT).exists()


@pytest.mark.parametrize('member', [
    'acquisition/scripts/acquisition_budget_hook.py',
    'policy/search-plugin.py', 'policy/search-plugin.json',
    'acquisition/climate_monitor/request_budget.py',
    'acquisition/scripts/run_agent_acquisition.py',
    'acquisition/climate_monitor/hermes_acquisition_hooks.py',
    'acquisition/climate_monitor/article_content_adapter.py',
])
def test_review4_acquisition_policy_frozen_and_verified(tmp_path, runtime, member):
    from climate_monitor import hermes_identity as h
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    payload = h.load_snapshot(run, ref)
    assert member in payload['policy']
    path = run / h.SNAPSHOT / member
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_bytes(path.read_bytes() + b'\n# altered\n')
    with pytest.raises(ValueError, match='fresh'):
        h.load_snapshot(run, ref)


def test_review4_hook_uses_bound_interpreter_and_static_plugin(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from test_issue117_request_boundaries import new_protocol_binding
    from types import SimpleNamespace
    import shlex
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    binding = new_protocol_binding(run)
    ref = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id'])
    binding['hermes_snapshot'] = ref
    frozen = h.load_snapshot(run, ref)
    monkeypatch.setattr(os.sys, 'executable', '/must-not-be-executed')
    monkeypatch.setattr(hooks.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='climate acquisition hooks verified'))
    sources = []
    for attempt in (1, 2):
        binding['attempt'] = attempt
        path = run / f'attempt-{attempt}.json'; path.write_text(json.dumps(binding))
        env, home = hooks.install_hooks(['hermes'], path, binding, {})
        config = json.loads((home / 'config.yaml').read_bytes())
        command = shlex.split(config['hooks']['pre_tool_call'][0]['command'])
        assert command[0] == frozen['interpreter']
        assert command[1:3] == ['-I', '-S']
        assert Path(command[3]).is_relative_to(run / h.SNAPSHOT)
        sources.append((home / 'plugins' / hooks.SEARCH_IDENTITY_PLUGIN_ID / '__init__.py').read_bytes())
        assert env['CLIMATE_ACQUISITION_ATTEMPT']
    assert sources[0] == sources[1]


@pytest.mark.parametrize('tamper', ['budget', 'run_id', 'snapshot', 'attempt', 'home', 'config'])
def test_review4_frozen_candidate_modules_execute_without_checkout(tmp_path, runtime, monkeypatch, tamper):
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from test_issue117_request_boundaries import new_protocol_binding, V3_AGENT_PROTOCOL
    from types import SimpleNamespace
    import subprocess
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    binding = new_protocol_binding(run); binding['agent_protocol'] = V3_AGENT_PROTOCOL
    binding['hermes_snapshot'] = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id'])
    path = run / 'attempt-1.json'; path.write_text(json.dumps(binding))
    original = subprocess.run
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='climate acquisition hooks verified'))
    env, home = hooks.install_hooks(['hermes'], path, binding, {})
    monkeypatch.setattr(subprocess, 'run', original)
    payload = h.load_snapshot(run, binding['hermes_snapshot'])
    code = '''
from hermes_cli.plugins import discover_plugins, get_plugin_manager
discover_plugins(force=True)
assert len(get_plugin_manager().list_plugins()) == 2
from scripts import run_agent_acquisition as candidate
from pathlib import Path
assert 'hermes-private/acquisition' in str(Path(candidate.__file__))
assert candidate._bounded_annotation('safe', 'title') == 'safe'
print('frozen candidate registration verified')
'''
    result = original(h.launch_command(home, '-c', code), cwd=home, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'frozen candidate registration verified'
    # The same immutable binding cannot silently be changed between registration
    # and dispatch, even while preserving its run/snapshot identifiers.
    if tamper == 'budget': binding['budgets']['fetch_attempts'] += 1
    if tamper == 'run_id': binding['run_id'] = 'another'
    if tamper == 'snapshot': binding['hermes_snapshot']['sha256'] = '0' * 64
    if tamper == 'attempt': binding['attempt'] = 2
    if tamper == 'home': env['HERMES_HOME'] = str(home.parent / 'attempt-2')
    if tamper == 'config': (home / 'config.yaml').write_text('{}')
    path.write_text(json.dumps(binding))
    result = original(h.launch_command(home, '-c', code), cwd=home, env=env, capture_output=True, text=True)
    assert result.returncode == 65
    assert not result.stdout and not result.stderr


@pytest.mark.parametrize('protocol', ['v2', 'v3', 'v3_body', 'legacy'])
def test_review4_live_poison_and_attempt_resume(tmp_path, runtime, monkeypatch, protocol):
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks, hermes_acquisition_policy as bundle
    from scripts import run_agent_acquisition as live_runner
    from test_issue117_request_boundaries import _v3_binding, NEW_AGENT_PROTOCOL
    from climate_monitor.request_budget import RequestBudget, ledger_path
    import subprocess, shutil
    # Mutate only a disposable copy of the creation-time source, never the
    # shared checkout. Poison loaded worker helpers independently after binding.
    source = tmp_path / 'source'; checkout = Path(bundle.__file__).parents[1]
    for name in [*bundle.MODULES, 'climate_monitor/hermes_search_policy.py', 'climate_monitor/hermes_acquisition_bootstrap.py']:
        target = source / name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checkout / name, target)
    if protocol == 'v3_body':
        # An offline reader source fixture is frozen before publication, rather
        # than monkeypatching a live handler after the binding exists.
        import ast
        adapter = source / 'climate_monitor/article_content_adapter.py'
        raw = adapter.read_text(); lines = raw.splitlines(keepends=True)
        function = next(n for n in ast.parse(raw).body if isinstance(n, ast.FunctionDef) and n.name == '_default_providers')
        replacement = '''def _default_providers(*, data_root=None, reader_home=None, site_key=None, site_scope=None, budget=None):
    assert Path(reader_home).name.startswith('attempt-')
    assert Path(data_root) == Path(reader_home).parents[1] / 'managed/web-listening-runtime'
    def reader(article_id, url):
        assert site_scope['source_key'] == site_key
        budget.claim('http', url, retry_key='article:' + url)
        body = 'Offline verified article'
        return {'status': 'present', 'article_id': article_id, 'requested_url': url,
                'final_url': url, 'selected_method': 'public_reader', 'full_text': body,
                'sha256': hashlib.sha256(body.encode()).hexdigest(), 'content_type': 'text/html',
                'attempts': [{'engine': 'public_reader', 'status': 'success', 'http_status': 200}],
                'extraction_metadata': {'http_status': 200}}
    return (reader,)
'''
        adapter.write_text(''.join(lines[:function.lineno-1]) + replacement + ''.join(lines[function.end_lineno:]))
    monkeypatch.setattr(bundle, '__file__', str(source / 'climate_monitor/hermes_acquisition_policy.py'))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    binding = _v3_binding(tmp_path, mode='unlimited' if protocol == 'v3_body' else 'recent')
    binding['checkpoint_dir'] = str(run / 'checkpoint')
    if protocol == 'v2': binding['agent_protocol'] = NEW_AGENT_PROTOCOL
    if protocol == 'legacy': binding.pop('agent_protocol')
    ref = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id'])
    binding['hermes_snapshot'] = ref
    expected = h.load_snapshot(run, ref)
    for target in source.rglob('*.py'):
        target.write_text('raise RuntimeError("poisoned live source")\n')
    monkeypatch.setattr(hooks, 'transform_search_tool_result', lambda *a, **k: pytest.fail('live transform executed'))
    monkeypatch.setattr(live_runner, '_stage_candidate_receipt', lambda *a, **k: pytest.fail('live candidate executed'))
    monkeypatch.setattr(live_runner, '_FINAL_ANNOTATION_LIMITS', {})
    monkeypatch.setattr(os.sys, 'executable', '/never-execute-worker-python')
    plugin_name = hooks.SEARCH_IDENTITY_PLUGIN_ID
    monkeypatch.setattr(hooks, 'SEARCH_IDENTITY_PLUGIN_ID', 'poisoned-live-name')
    monkeypatch.setattr(hooks, 'candidate_handle_protocol', lambda binding: False)
    monkeypatch.setattr(hooks, 'provider_native_unbounded_search', lambda binding: False)
    plugin_bytes = []
    for attempt in (1, 2):
        binding['attempt'] = attempt
        path = run / f'attempt-{attempt}.json'; path.write_text(json.dumps(binding))
        ledger = RequestBudget(ledger_path(binding), binding)
        ledger.claim('web_search', 'q', call_id=f'{attempt}:session:call', results=5, session_id='session', tool_call_id='call')
        env, home = hooks.install_hooks(['hermes'], path, binding, {})  # real frozen verifier
        code = '''
import json, os
from pathlib import Path
from hermes_cli.plugins import discover_plugins, get_plugin_manager
from climate_monitor.hermes_attempt_policy import verified_binding
discover_plugins(force=True)
m = get_plugin_manager(); path, binding = verified_binding()
protocol = binding.get('agent_protocol', {}).get('version')
expected = {'climate_stage_candidate', 'climate_finalize_candidate'} if protocol == 'trusted-candidate-handles.v3' else set()
assert set(m.tools) == expected
assert len(m.plugins) == (1 if protocol is None else 2)
if protocol:
    raw = json.dumps({'data': {'web': [{'url': 'https://wmo.int/article-' + str(binding['attempt']), 'title': 'WMO', 'description': '16 Apr 2025'}]}})
    transformed = m.hooks['transform_tool_result'][0](tool_name='web_search', args={'query':'q'}, result=raw, session_id='session', tool_call_id='call', status='ok')
    assert transformed and transformed != raw
    if expected:
        from climate_monitor.request_budget import RequestBudget, ledger_path
        budget = RequestBudget(ledger_path(binding), binding)
        handle = next(r['handle'] for r in budget.result_handles() if r['attempt'] == binding['attempt'])
        args = {'result_handle': handle, 'source_key': 'wmo'}
        stage = json.loads(m.tools['climate_stage_candidate']['handler'](args, session_id='session'))
        eligible = binding['date_policy']['mode'] == 'unlimited'
        assert stage['date_status'] == ('eligible' if eligible else 'outside_window')
        if eligible:
            assert stage['content_preview'] == 'Offline verified article'
            assert stage['body_status'] == 'ok'
        final = json.loads(m.tools['climate_finalize_candidate']['handler']({'candidate_handle': stage['candidate_handle'], 'selected': eligible, 'title': 'WMO', 'summary': '', 'selection_reason': 'date policy'}, session_id='session'))
        assert final['status'] == ('finalized' if eligible else 'rejected')
print('frozen acquisition lifecycle verified')
'''
        result = subprocess.run(h.launch_command(home, '-c', code), cwd=home, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'frozen acquisition lifecycle verified'
        if protocol != 'legacy':
            plugin_bytes.append((home / 'plugins' / plugin_name / '__init__.py').read_bytes())
        assert h.load_snapshot(run, ref)['policy'] == expected['policy']
    assert not plugin_bytes or plugin_bytes[0] == plugin_bytes[1]


def test_review4_all_policy_members_reject_corruption_before_launch(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from test_issue117_request_boundaries import new_protocol_binding
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    binding = new_protocol_binding(run)
    binding['hermes_snapshot'] = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id'])
    path = run / 'attempt-1.json'; path.write_text(json.dumps(binding))
    payload = h.load_snapshot(run, binding['hermes_snapshot'])
    monkeypatch.setattr(hooks.subprocess, 'run', lambda *a, **k: pytest.fail('process launched with corrupt policy'))
    for member in payload['policy']:
        target = run / h.SNAPSHOT / member
        original = target.read_bytes(); target.write_bytes(original + b'\n')
        with pytest.raises(ValueError, match='fresh'):
            hooks.install_hooks(['hermes'], path, binding, {})
        target.write_bytes(original)


@pytest.mark.parametrize('kind', ['file', 'empty-directory'])
def test_review4_unlisted_policy_module_rejected(tmp_path, runtime, kind):
    from climate_monitor import hermes_identity as h
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    extra = run / h.SNAPSHOT / 'acquisition/scripts/unbound_helper.py'
    if kind == 'file':
        extra.write_text('raise RuntimeError()\n'); extra.chmod(0o600)
    else:
        extra.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='fresh'):
        h.load_snapshot(run, ref)


def test_review4_reader_dependency_source_closure(tmp_path):
    from climate_monitor.hermes_acquisition_policy import provider_dependency_policy
    venv = tmp_path / 'venv'
    site = venv / 'lib/python3.11/site-packages'; site.mkdir(parents=True)
    reader = site / 'web_listening'; reader.mkdir()
    (reader / '__init__.py').write_text('from helper_dependency import value\n')
    (reader / 'template.txt').write_text('template')
    helper = site / 'helper_dependency.py'; helper.write_text('value = 42\n')
    policy = provider_dependency_policy(venv / 'bin/python')
    assert policy['acquisition/helper_dependency.py'] == b'value = 42\n'
    assert policy['acquisition/web_listening/template.txt'] == b'template'
    helper.unlink(); helper.symlink_to(reader / 'template.txt')
    with pytest.raises(ValueError, match='unsafe'):
        provider_dependency_policy(venv / 'bin/python')


def test_review4_dependency_mutation_during_collection_fails(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h, hermes_acquisition_policy as bundle
    venv = tmp_path / 'reader-venv'
    package = venv / 'lib/python3.11/site-packages/web_listening'; package.mkdir(parents=True)
    module = package / '__init__.py'; module.write_text('version = 1\n')
    collect = bundle.provider_dependency_policy
    calls = []
    def changing(interpreter, package_root=None):
        value = collect(venv / 'bin/python')
        calls.append(True)
        if len(calls) == 1:
            module.write_text('version = 2\n')
        return value
    monkeypatch.setattr(bundle, 'provider_dependency_policy', changing)
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='changed|fresh'):
        h.create_snapshot(run)
    assert len(calls) == 2
    assert not (run / h.SNAPSHOT).exists()


@pytest.mark.parametrize('change', ['identity-copy', 'search-copy', 'config-recipe'])
def test_review4_child_verifies_materialized_policy(tmp_path, runtime, change):
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from test_issue117_request_boundaries import new_protocol_binding
    import subprocess, hashlib
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    binding = new_protocol_binding(run)
    binding['hermes_snapshot'] = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id'])
    path = run / 'attempt-1.json'; path.write_text(json.dumps(binding))
    env, home = hooks.install_hooks(['hermes'], path, binding, {})
    if change == 'config-recipe':
        config = json.loads((home / 'config.yaml').read_text())
        config['hooks']['pre_tool_call'][0]['fail_closed'] = False
        raw = json.dumps(config).encode(); (home / 'config.yaml').write_bytes(raw)
        seal_path = home / 'attempt-policy.json'
        seal = json.loads(seal_path.read_text()); seal['configuration_sha256'] = hashlib.sha256(raw).hexdigest()
        seal_path.write_text(json.dumps(seal))
    else:
        name = 'climate-frozen-identity' if change == 'identity-copy' else hooks.SEARCH_IDENTITY_PLUGIN_ID
        events = ['pre_api_request', 'post_api_request'] if change == 'identity-copy' else ['transform_tool_result']
        replacement = 'def register(ctx):\n' + ''.join('    ctx.register_hook(' + repr(event) + ', lambda **kw: None)\n' for event in events)
        (home / 'plugins' / name / '__init__.py').write_text(replacement)
    payload = h.load_snapshot(run, binding['hermes_snapshot'])
    result = subprocess.run(h.launch_command(home, '-c', 'from hermes_cli.plugins import discover_plugins; discover_plugins(force=True)'),
                            cwd=home, env=env, capture_output=True)
    assert result.returncode == 65
    assert not result.stdout and not result.stderr


def test_review4_reader_tree_counts_empty_directories(tmp_path, monkeypatch):
    from climate_monitor import hermes_identity as h
    from climate_monitor.hermes_acquisition_policy import provider_dependency_policy
    venv = tmp_path / 'venv'
    package = venv / 'lib/python3.11/site-packages/web_listening'; package.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    directory = package
    for _ in range(25):
        directory = directory / 'nested'; directory.mkdir()
    monkeypatch.setattr(h, 'MAX_TREE_FILES', 20)
    with pytest.raises(ValueError, match='limit'):
        provider_dependency_policy(venv / 'bin/python')


def test_review4_new_unbound_application_import_rejected_before_publish(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h, hermes_acquisition_policy as bundle
    import shutil
    source = tmp_path / 'source'; checkout = Path(bundle.__file__).parents[1]
    for name in [*bundle.MODULES, 'climate_monitor/hermes_search_policy.py', 'climate_monitor/hermes_acquisition_bootstrap.py']:
        target = source / name; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checkout / name, target)
    runner = source / 'scripts/run_agent_acquisition.py'
    raw = runner.read_text().replace('    binding = _attempt_binding(binding_path)', '    import future_dependency_unbound\n    binding = _attempt_binding(binding_path)')
    runner.write_text(raw)
    monkeypatch.setattr(bundle, '__file__', str(source / 'climate_monitor/hermes_acquisition_policy.py'))
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unbound|unsupported'):
        h.create_snapshot(run)
    assert not (run / h.SNAPSHOT).exists()


@pytest.mark.parametrize('purpose', ['acquisition', 'report', 'meetings'])
@pytest.mark.parametrize('helper_name', ['startup_helper', '__editable___offline_finder'])
def test_review5_pth_helper_never_executes(tmp_path, runtime, purpose, helper_name, safe_managed_interpreter):
    import subprocess, sys
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from test_issue117_request_boundaries import new_protocol_binding
    ambient, executable = runtime
    package = executable.parents[2]
    (package / 'hermes_cli/main.py').write_text("def main():\n from hermes_cli.plugins import discover_plugins,get_plugin_manager\n discover_plugins(force=True)\n m=get_plugin_manager()\n m.emit('pre_api_request',provider='offline',model='offline')\n m.emit('post_api_request',provider='offline',model='offline')\n")
    venv = executable.parent.parent
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(venv)], check=True)
    executable.write_text('#!' + str(venv / 'bin/python') + '\nfrom hermes_cli.main import main\nmain()\n')
    (venv / 'bin/python').unlink()
    os.link(safe_managed_interpreter, venv / 'bin/python')
    (venv / 'pyvenv.cfg').write_bytes((safe_managed_interpreter.parent.parent / 'pyvenv.cfg').read_bytes())
    site = next((venv / 'lib').glob('python*/site-packages'))
    sentinel = tmp_path / 'startup-executed'
    helper = site / (helper_name + '.py')
    helper.write_text('# original\n')
    (site / 'startup.pth').write_text('import ' + helper_name + '\n')
    source = site / 'editable_source.py'; source.write_text('# original\n')
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    binding = new_protocol_binding(run)
    ref = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id'])
    binding['hermes_snapshot'] = ref
    launches = []
    for attempt in (1, 2):
        if purpose == 'acquisition':
            binding['attempt'] = attempt
            path = run / f'attempt-{attempt}.json'; path.write_text(json.dumps(binding))
            command = ['hermes']
            env, home = hooks.install_hooks(command, path, binding, {})
        else:
            command, env, home = h.inference_runtime(run, ref, purpose=purpose, source='climate-acquisition-' + binding['run_id'])
        launches.append((list(command), dict(env), home))
        assert command[1:3] == ['-I', '-S']
        with h.auth_execution(home, require_identity=True) as auth:
            result = subprocess.run(command, env=env, cwd=home, capture_output=True)
            auth['returncode'] = result.returncode
        assert result.returncode == 0
        assert not sentinel.exists(), 'unfrozen startup helper executed before policy'
        assert h.load_snapshot(run, ref)
        assert binding['hermes_snapshot'] == ref


    # Runtime files are now bound: changed helpers must fail before execution.
    for attempt, (command, env, home) in enumerate(launches, 1):
        helper.write_text('import editable_source\n')
        source.write_text('from pathlib import Path\nPath(' + repr(str(sentinel)) + ').touch()\n# attempt ' + str(attempt))
        with pytest.raises(ValueError, match='start a fresh run'):
            h.load_snapshot(run, ref)
        result = subprocess.run(command, env=env, cwd=home, capture_output=True)
        assert result.returncode == 65
        assert not result.stdout and not result.stderr
        assert not sentinel.exists(), 'unfrozen startup helper executed before policy'
        assert binding['hermes_snapshot'] == ref


@pytest.mark.parametrize('loader', ['util', 'pathfinder'])
def test_review5_dynamic_loader_rejected_before_execution(tmp_path, loader):
    import subprocess, sys
    from climate_monitor.hermes_acquisition_policy import provider_dependency_policy
    venv = tmp_path / 'venv'; site = venv / 'lib/python3.11/site-packages'
    package = site / 'web_listening'; package.mkdir(parents=True)
    sentinel = tmp_path / 'ambient-executed'
    (site / 'ambient_dynamic.py').write_text('from pathlib import Path\nPath(' + repr(str(sentinel)) + ').touch()\n')
    find = "importlib.util.find_spec('ambient_dynamic')" if loader == 'util' else "importlib.machinery.PathFinder.find_spec('ambient_dynamic')"
    (package / '__init__.py').write_text('import importlib.util, importlib.machinery\nspec = ' + find + '\nmodule = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n')
    try:
        policy = provider_dependency_policy(venv / 'bin/python')
    except ValueError:
        assert not sentinel.exists()
        return
    private = tmp_path / 'private'
    for name, raw in policy.items():
        path = private / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    bootstrap = Path(__file__).parents[1] / 'climate_monitor/hermes_acquisition_bootstrap.py'
    code = 'import runpy; m=runpy.run_path(' + repr(str(bootstrap)) + '); m["install"](' + repr(str(private / 'acquisition')) + '); import web_listening'
    result = subprocess.run([sys.executable, '-S', '-c', code], env={'PYTHONPATH': str(site)}, capture_output=True)
    assert result.returncode != 0, 'uncollected ambient loader accepted'
    assert not sentinel.exists(), 'ambient loader executed unbound bytes'


@pytest.mark.parametrize('kind', ['native', 'modules', 'dynamic_import', 'namespace'])
def test_review5_unsupported_dependency_rejected(tmp_path, kind):
    from climate_monitor.hermes_acquisition_policy import provider_dependency_policy
    site = tmp_path / 'venv/lib/python3.11/site-packages'
    package = site / 'web_listening'; package.mkdir(parents=True)
    bodies = {'native': '', 'modules': 'import sys\nx=sys.modules["ambient_dynamic"]\n',
              'dynamic_import': 'x=__import__("ambient_dynamic")\n',
              'namespace': 'import sys\nsys.path.append("ambient")\n'}
    (package / '__init__.py').write_text(bodies[kind])
    if kind == 'native':
        (package / 'extension.so').write_bytes(b'not executed')
    with pytest.raises(ValueError, match='unsupported'):
        provider_dependency_policy(tmp_path / 'venv/bin/python')


@pytest.mark.parametrize('name', ['web_listening', 'web_listening.nested'])
def test_review5_preloaded_collision_rejected(tmp_path, name):
    import subprocess, sys
    root = tmp_path / 'private'; (root / 'web_listening').mkdir(parents=True)
    (root / 'web_listening/__init__.py').write_text('')
    bootstrap = Path(__file__).parents[1] / 'climate_monitor/hermes_acquisition_bootstrap.py'
    code = 'import sys,types,runpy; m=types.ModuleType(' + repr(name) + '); m.__file__="/unbound/ambient.py"; sys.modules[' + repr(name) + ']=m; runpy.run_path(' + repr(str(bootstrap)) + ')["install"](' + repr(str(root)) + ')'
    result = subprocess.run([sys.executable, '-I', '-S', '-c', code], capture_output=True)
    assert result.returncode != 0
    assert b'unbound acquisition import already loaded' in result.stderr


def test_review5_editable_dependency_is_private(tmp_path):
    from climate_monitor.hermes_acquisition_policy import provider_dependency_policy
    site = tmp_path / 'venv/lib/python3.11/site-packages'; site.mkdir(parents=True)
    metadata = site / 'web_listening-1.dist-info'; metadata.mkdir()
    source = tmp_path / 'editable/src/web_listening'; source.mkdir(parents=True)
    (source / '__init__.py').write_text('VALUE="original"\n')
    (metadata / 'direct_url.json').write_text(json.dumps({'url': (tmp_path / 'editable').as_uri(), 'dir_info': {'editable': True}}))
    (metadata / 'top_level.txt').write_text('web_listening\n')
    policy = provider_dependency_policy(tmp_path / 'venv/bin/python')
    (source / '__init__.py').write_text('raise RuntimeError("ambient")\n')
    assert policy['acquisition/web_listening/__init__.py'] == b'VALUE="original"\n'


@pytest.mark.parametrize('source', [
    'VALUE=1\nVALUE=2\ndef selected(): return VALUE\n',
    'VALUE=1\ndel VALUE\ndef selected(): return VALUE\n',
    'VALUE=1\nVALUE+=1\ndef selected(): return VALUE\n',
])
def test_review5_ambiguous_selected_globals_rejected(source):
    from climate_monitor.hermes_acquisition_policy import select_definitions
    with pytest.raises(ValueError, match='unsupported|ambiguous'):
        select_definitions(source.encode(), ('selected',))


def test_review5_async_definition_preserved():
    import ast
    from climate_monitor.hermes_acquisition_policy import select_definitions
    raw = select_definitions(b'VALUE=1\nasync def selected(): return VALUE\n', ('selected',))
    tree = ast.parse(raw)
    assert isinstance(tree.body[1], ast.AsyncFunctionDef)
    assert tree.body[0].targets[0].id == 'VALUE'


@pytest.mark.parametrize('source', [
    'import sys as runtime\nruntime.path.append("ambient")\n',
    'from sys import modules as cache\nvalue=cache["ambient_dynamic"]\n',
    '__path__=["ambient"]\n',
])
def test_review5_aliased_namespace_escape_rejected(tmp_path, source):
    from climate_monitor.hermes_acquisition_policy import provider_dependency_policy
    package = tmp_path / 'venv/lib/python3.11/site-packages/web_listening'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text(source)
    with pytest.raises(ValueError, match='unsupported'):
        provider_dependency_policy(tmp_path / 'venv/bin/python')


@pytest.mark.parametrize('loader', ['util', 'pathfinder'])
def test_review5_execution_origin_guard_stops_loader(tmp_path, loader):
    import subprocess, sys
    private = tmp_path / 'private'; (private / 'web_listening').mkdir(parents=True)
    ambient = tmp_path / 'ambient'; ambient.mkdir()
    sentinel = tmp_path / 'executed'
    (ambient / 'ambient_dynamic.py').write_text('from pathlib import Path\nPath(' + repr(str(sentinel)) + ').touch()\n')
    find = "importlib.util.find_spec('ambient_dynamic')" if loader == 'util' else "importlib.machinery.PathFinder.find_spec('ambient_dynamic')"
    (private / 'web_listening/__init__.py').write_text('import importlib.util,importlib.machinery\nspec=' + find + '\nmodule=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n')
    bootstrap = Path(__file__).parents[1] / 'climate_monitor/hermes_acquisition_bootstrap.py'
    code = 'import sys,runpy;sys.path.append(' + repr(str(ambient)) + ');runpy.run_path(' + repr(str(bootstrap)) + ')["install"](' + repr(str(private)) + ');import web_listening'
    for attempt in (1, 2):
        result = subprocess.run([sys.executable, '-I', '-S', '-c', code], capture_output=True)
        assert result.returncode != 0
        assert not sentinel.exists()


def test_review5_entrypoint_discovery_cannot_override_policy(tmp_path, runtime):
    from climate_monitor import hermes_identity as h
    _, executable = runtime
    sentinel = tmp_path / 'entrypoint-ran'
    plugins = executable.parents[2] / 'hermes_cli/plugins.py'
    with plugins.open('a') as out:
        out.write('\ndef discover_entrypoint_manifests():\n    Path(' + repr(str(sentinel)) + ').touch()\n    return []\n')
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    for attempt in (1, 2):
        h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
        assert not sentinel.exists()


@pytest.mark.parametrize('extra', ['another-plugin', 'climate-frozen-identity/extra.py', 'climate-frozen-identity/empty'])
def test_review5_plugin_home_exact_membership(tmp_path, runtime, extra):
    from climate_monitor import hermes_identity as h
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    _, _, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    path = home / 'plugins' / extra
    if extra.endswith('.py'):
        path.write_text('# extra\n'); path.chmod(0o600)
    else:
        path.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='fresh|plugin'):
        h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')


def test_review5_interpreter_runtime_path_metadata_is_bound(tmp_path, runtime, safe_managed_interpreter):
    from climate_monitor import hermes_identity as h
    import sys
    _, executable = runtime
    interpreter = tmp_path / 'other-venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(safe_managed_interpreter)
    metadata = tmp_path / 'other-venv/lib/python3.11/site-packages/offline-1.dist-info/METADATA'
    metadata.parent.mkdir(parents=True); metadata.write_text('Name: offline\nVersion: 1\n')
    executable.write_text('#!' + str(interpreter) + '\nfrom hermes_cli.main import main\nmain()\n')
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    ref = h.create_snapshot(run)
    metadata.write_text('Name: offline\nVersion: 2\n')
    with pytest.raises(ValueError, match='fresh'):
        h.load_snapshot(run, ref)


@pytest.mark.parametrize('module', ['_imp', '_ctypes', '_frozen_importlib', '_frozen_importlib_external'])
def test_review5_low_level_loader_modules_rejected(tmp_path, module):
    from climate_monitor.hermes_acquisition_policy import provider_dependency_policy
    package = tmp_path / 'venv/lib/python3.11/site-packages/web_listening'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('import ' + module + '\n')
    with pytest.raises(ValueError, match='unsupported'):
        provider_dependency_policy(tmp_path / 'venv/bin/python')

@pytest.mark.parametrize('dotenv', ['home', 'project', 'process'])
def test_review6_web_channel_first_resume(tmp_path, monkeypatch, runtime, capsys, dotenv):
    import hashlib
    import secrets
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    home, executable = runtime
    package = executable.parents[2]
    (package / 'tools').mkdir(exist_ok=True)
    # Contract shared by both supported web families, with a future literal name.
    names = ['TAVILY_API_KEY', 'TAVILY_BASE_URL', 'FIRECRAWL_API_URL', 'BRAVE_SEARCH_API_KEY',
             'PARALLEL_SEARCH_MODE', 'SEARXNG_URL', 'NEW_WEB_SETTING']
    (package / 'tools/web_tools.py').write_text('import os\n' + '\n'.join(
        f'os.getenv({name!r})' for name in names))
    values = {name: secrets.token_hex(24) for name in names}
    web = {'backend': 'tavily', 'search_backend': 'tavily', 'extract_backend': 'firecrawl',
           'providers': {'tavily': {'tier': 'paid', 'base_url': '${env:TAVILY_BASE_URL}'}}}
    (home / 'config.yaml').write_bytes(h._bytes({'model': {'default': 'route-A', 'provider': 'provider-A'}, 'web': web}))
    if dotenv == 'process':
        for name, value in values.items(): monkeypatch.setenv(name, value)
    else:
        h._write((home if dotenv == 'home' else package) / '.env',
                 ''.join(f'{k}={v}\n' for k, v in values.items()).encode())
    run = tmp_path / 'run'; run.mkdir()
    reference = h.create_snapshot(run, source='climate-acquisition-web')
    payload = h.load_snapshot(run, reference)
    matches = payload.get('web_environment') == values
    assert matches
    assert payload['web_environment_metadata'] == h._environment_metadata(values)
    assert not set(values) & set(payload['environment'])
    (home / 'config.yaml').unlink()
    for path in (home / '.env', package / '.env'):
        if path.exists(): path.unlink()
    for name in names: monkeypatch.delenv(name, raising=False)
    binding = {'run_id': 'web', 'checkpoint_dir': str(run / 'checkpoint'), 'hermes_snapshot': reference,
               'agent_protocol': {'version': 'trusted-candidate-handles.v3', 'search_policy': 'provider-native-unbounded.v1', 'candidate_policy': 'trusted-tool-receipts.v1'}}
    for attempt in (1, 2):
        binding['attempt'] = attempt
        path = run / f'attempt-{attempt}.json'; path.write_text(json.dumps(binding))
        env, child = hooks.install_hooks(['hermes'], path, binding, {})
        assert all(env.get(k) == v for k, v in values.items())
        config = json.loads((child / 'config.yaml').read_text())
        assert config['web']['search_backend'] == 'tavily'
        assert config['web']['providers']['tavily']['base_url'] == values['TAVILY_BASE_URL']
    for purpose in ('report', 'meetings'):
        _, env, child = h.inference_runtime(run, reference, purpose=purpose, source='climate-acquisition-web')
        assert not set(values) & set(env)
        assert 'web' not in json.loads((child / 'config.yaml').read_text())
    public = json.dumps(binding) + str(capsys.readouterr())
    assert all(value not in public for value in values.values())
    for path in run.rglob('*'):
        if path.is_file() and any(value.encode() in path.read_bytes() for value in values.values()):
            assert h.SNAPSHOT in path.parts and path.stat().st_mode & 0o777 == 0o600


def test_review6_dynamic_web_name_rejected(tmp_path, runtime):
    from climate_monitor import hermes_identity as h
    _, executable = runtime
    tools = executable.parents[2] / 'tools'; tools.mkdir(exist_ok=True)
    (tools / 'web_tools.py').write_text('import os\nos.getenv("KEY_" + input())\n')
    with pytest.raises(ValueError, match='unsupported.*web'):
        h.create_snapshot(tmp_path / 'run')

@pytest.mark.parametrize('tamper', ['metadata', 'value', 'config'])
def test_review6_web_manifest_integrity(tmp_path, runtime, tamper):
    from climate_monitor import hermes_identity as h
    run = tmp_path / 'run'; run.mkdir()
    ref = h.create_snapshot(run)
    manifest = run / h.SNAPSHOT / 'manifest.json'
    payload = json.loads(manifest.read_bytes())
    if tamper == 'metadata':
        payload['web_environment_metadata'] = {'unexpected': {}}
    elif tamper == 'value':
        payload['web_environment']['TAVILY_API_KEY'] = __import__('secrets').token_hex(24)
    else:
        payload['web_config'] = {'backend': 'changed'}
    raw = h._bytes(payload); manifest.write_bytes(raw)
    # Relation validation must also reject a self-consistent outer digest.
    if tamper == 'metadata':
        ref = dict(ref, sha256=h._digest(raw))
        (run / h.SNAPSHOT / 'complete').write_text(ref['sha256'])
    with pytest.raises(ValueError, match='fresh'):
        h.load_snapshot(run, ref)


@pytest.mark.parametrize('source', [
    'import os as process\nprocess.environ.get("KEY_" + input())',
    'import os\nenv = os.environ\nenv.get("KEY_" + input())',
    'class Provider:\n KEY_ENV = "KEY_" + input()',
])
def test_review6_dynamic_web_alias_rejected(tmp_path, runtime, source):
    from climate_monitor import hermes_identity as h
    _, exe = runtime
    provider = exe.parents[2] / 'plugins/web/new/provider.py'
    provider.parent.mkdir(parents=True); provider.write_text(source)
    with pytest.raises(ValueError, match='unsupported.*web'):
        h.create_snapshot(tmp_path / 'run')


def test_review6_web_marker_public_artifacts(tmp_path, runtime, capsys, caplog):
    import secrets
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from scripts import run_agent_acquisition as runner
    from hermes_offline_runtime import successful_api_lifecycle
    home, exe = runtime
    tools = exe.parents[2] / 'tools'; tools.mkdir(exist_ok=True)
    (tools / 'web_tools.py').write_text('import os\nos.getenv("TAVILY_API_KEY")\n')
    marker = secrets.token_hex(32)
    h._write(home / '.env', ('TAVILY_API_KEY=' + marker).encode())
    (home / 'config.yaml').write_text(json.dumps({'web': {'backend': 'tavily', 'api_key': marker}}))
    store = _store(tmp_path); store.save(_definition(tmp_path), actor='test')
    service = management.ManagementService(store=store, runtime_root=tmp_path / 'runs', launcher=lambda binding: 123)
    started = service.start(); binding = service.binding(started['run_id'])
    run = tmp_path / 'runs' / started['run_id']
    (run / 'test-prompt.txt').write_text(runner._prompt(run / 'attempt-1.json', binding))
    (home / '.env').unlink(); (home / 'config.yaml').unlink()
    env, child = hooks.install_hooks(['hermes'], run / 'attempt-1.json', binding, {})
    matches = env['TAVILY_API_KEY'] == marker
    assert matches
    with h.auth_execution(child, require_identity=True) as state:
        successful_api_lifecycle(child, env)
        state['returncode'] = 0
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err + caplog.text + json.dumps(binding)
    hits = []
    for path in run.rglob('*'):
        if path.is_file() and marker.encode() in path.read_bytes():
            assert path.is_relative_to(run / h.SNAPSHOT)
            assert path.stat().st_mode & 0o777 == 0o600
            hits.append(path.name)
    assert sorted(hits) == ['config.yaml', 'manifest.json']

@pytest.mark.parametrize('source', [
    'schema = {"env_vars": compute()}',
    'schema = {"env_vars": [{"key": compute()}]}',
    'setup_schema("new", "paid", "new", key_env=compute())',
    'cached_sdk_client("slot", compute(), "missing", "feature", factory)',
])
def test_review6_dynamic_web_profile_rejected(tmp_path, runtime, source):
    from climate_monitor import hermes_identity as h
    _, exe = runtime
    provider = exe.parents[2] / 'plugins/web/new/provider.py'
    provider.parent.mkdir(parents=True); provider.write_text(source)
    with pytest.raises(ValueError, match='unsupported.*web'):
        h.create_snapshot(tmp_path / 'run')


def _review7_editable(runtime, tmp_path, monkeypatch):
    home, old = runtime
    package = old.parents[2]
    (package / 'pyproject.toml').write_text('[project]\nname="hermes-agent"\nversion="0.20.5"\n')
    prefix = tmp_path / 'usr/local'
    executable = prefix / 'bin/hermes'; executable.parent.mkdir(parents=True)
    executable.write_bytes(old.read_bytes()); executable.chmod(0o700)
    dist = prefix / 'lib/python3.11/site-packages/hermes_agent-0.20.5.dist-info'
    dist.mkdir(parents=True)
    (dist / 'METADATA').write_text('Name: hermes-agent\nVersion: 0.20.5\n')
    (dist / 'direct_url.json').write_text(json.dumps({'url': package.as_uri(), 'dir_info': {'editable': True}}))
    sentinel = tmp_path / 'startup-executed'
    (dist.parent / '__editable__.hermes.pth').write_text(f'import pathlib; pathlib.Path({str(sentinel)!r}).touch()\n')
    monkeypatch.setenv('HERMES_EXECUTABLE', str(executable))
    return package, dist, sentinel


def test_review7_docker_editable_nonancestor(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h
    package, dist, sentinel = _review7_editable(runtime, tmp_path, monkeypatch)
    run = tmp_path / 'run'; run.mkdir()
    ref = h.create_snapshot(run)
    payload = h.load_snapshot(run, ref)
    assert payload['package_root'] == str(package)
    assert str(dist / 'direct_url.json') in payload['runtime']
    for attempt in (1, 2):
        cmd, env, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
        assert cmd[1:3] == ['-I', '-S']
    assert not sentinel.exists()
    (dist / 'direct_url.json').write_text('{}')
    with pytest.raises(ValueError, match='fresh'):
        h.load_snapshot(run, ref)


def test_review7_clean_sibling_auth_serializes(tmp_path, runtime):
    from climate_monitor import hermes_identity as h
    from hermes_offline_runtime import successful_api_lifecycle
    run = tmp_path / 'run'; run.mkdir()
    ref = h.create_snapshot(run)
    _, env, meeting = h.inference_runtime(run, ref, purpose='meetings', source='climate-acquisition-run')
    started, release = threading.Event(), threading.Event()
    def first():
        with h.auth_execution(meeting, require_identity=True) as state:
            successful_api_lifecycle(meeting, env)
            (meeting / 'auth.json').write_text('{"providers":{"offline":{"generation":1}}}')
            started.set()
            assert release.wait(10)
            state['returncode'] = 0
    def second():
        _, env2, report = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
        with h.auth_execution(report, require_identity=True) as state:
            assert json.loads((report / 'auth.json').read_bytes())['providers']['offline']['generation'] == 1
            successful_api_lifecycle(report, env2)
            state['returncode'] = 0
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        a = pool.submit(first)
        assert started.wait(10)
        b = pool.submit(second)
        try:
            with pytest.raises(concurrent.futures.TimeoutError): b.result(timeout=0.25)
        finally:
            release.set()
        a.result(timeout=10); b.result(timeout=10)
    assert not (run / h.SNAPSHOT / 'auth-inflight.json').exists()


@pytest.mark.parametrize('deny', ['web/tavily', 'web-tavily'])
def test_review7_web_deny_first_resume(tmp_path, runtime, deny):
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    home, exe = runtime
    manifest = exe.parents[2] / 'plugins/web/tavily/plugin.yaml'
    manifest.parent.mkdir(parents=True)
    manifest.write_text('name: web-tavily\nkind: backend\nprovides_web_providers: [tavily]\n')
    (home / 'config.yaml').write_text(json.dumps({'web': {'backend':'tavily'}, 'plugins': {'disabled':[deny, 'unrelated-plugin']}}))
    run = tmp_path / 'run'; run.mkdir()
    ref = h.create_snapshot(run)
    (home / 'config.yaml').unlink()
    for attempt in (1, 2):
        binding = {'run_id':'run','attempt':attempt,'hermes_snapshot':ref,'checkpoint_dir':str(run/'checkpoint')}
        path = run / f'attempt-{attempt}.json'; path.write_text(json.dumps(binding))
        _, child = hooks.install_hooks(['hermes'], path, binding, {})
        plugins = json.loads((child/'config.yaml').read_bytes())['plugins']
        assert plugins.get('disabled') == ['web/tavily']
        assert h.IDENTITY_PLUGIN in plugins['enabled']
    for purpose in ('report','meetings'):
        _, _, child = h.inference_runtime(run,ref,purpose=purpose,source='climate-acquisition-run')
        assert 'disabled' not in json.loads((child/'config.yaml').read_bytes())['plugins']


@pytest.mark.parametrize('kind', ['missing', 'ambiguous', 'metadata_symlink', 'metadata_fifo', 'metadata_mode',
                                  'root_symlink', 'root_mode', 'wrong_project', 'wrong_version', 'noneditable', 'remote', 'null_metadata', 'bad_project'])
def test_review7_editable_unsafe_rejected(tmp_path, runtime, monkeypatch, kind):
    import shutil
    from climate_monitor import hermes_identity as h
    package, dist, sentinel = _review7_editable(runtime, tmp_path, monkeypatch)
    direct = dist / 'direct_url.json'
    if kind == 'null_metadata': direct.write_text('null')
    elif kind == 'bad_project': (package/'pyproject.toml').write_text('project = 1')
    elif kind == 'missing': direct.unlink()
    elif kind == 'ambiguous': shutil.copytree(dist, dist.with_name('hermes_agent-other.dist-info'))
    elif kind in {'metadata_symlink', 'metadata_fifo'}:
        raw = direct.read_bytes(); direct.unlink()
        if kind == 'metadata_fifo': os.mkfifo(direct, 0o600)
        else:
            target = tmp_path / 'metadata.json'; target.write_bytes(raw); direct.symlink_to(target)
    elif kind == 'metadata_mode': direct.chmod(0o666)
    elif kind == 'root_mode': package.chmod(0o777)
    elif kind == 'root_symlink':
        alias = tmp_path / 'package-link'; alias.symlink_to(package, target_is_directory=True)
        direct.write_text(json.dumps({'url':alias.as_uri(),'dir_info':{'editable':True}}))
    elif kind == 'wrong_project': (package/'pyproject.toml').write_text('[project]\nname="other"\nversion="0.20.5"')
    elif kind == 'wrong_version': (dist/'METADATA').write_text('Name: hermes-agent\nVersion: 9.0\n')
    elif kind == 'noneditable': direct.write_text(json.dumps({'url':package.as_uri(),'dir_info':{'editable':False}}))
    elif kind == 'remote': direct.write_text(json.dumps({'url':'https://offline.invalid/pkg','dir_info':{'editable':True}}))
    with pytest.raises((ValueError, OSError)):
        h.create_snapshot(tmp_path/'run')
    assert not sentinel.exists()
    assert not (tmp_path/'run'/h.SNAPSHOT).exists()


@pytest.mark.parametrize('crash', [False, True])
def test_review7_process_siblings_and_abandonment(tmp_path, runtime, crash):
    import multiprocessing
    from climate_monitor import hermes_identity as h
    run = tmp_path/'run';run.mkdir()
    ref = h.create_snapshot(run)
    _,_,meeting = h.inference_runtime(run,ref,purpose='meetings',source='climate-acquisition-run')
    context = multiprocessing.get_context('fork')
    entered, release = context.Event(), context.Event()
    def worker():
        with h.auth_execution(meeting) as state:
            entered.set()
            if not release.wait(10): os._exit(91)
            if crash: os._exit(92)
            (meeting/'auth.json').write_text('{"providers":{"offline":{"generation":1}}}')
            state['returncode'] = 0
    process=context.Process(target=worker);process.start()
    assert entered.wait(10)
    def sibling():
        return h.inference_runtime(run,ref,purpose='report',source='climate-acquisition-run')
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future=pool.submit(sibling)
        try:
            with pytest.raises(concurrent.futures.TimeoutError):future.result(timeout=0.2)
        finally:
            release.set();process.join(10)
        if crash:
            with pytest.raises(ValueError,match='fresh'):future.result(timeout=10)
        else:
            _,_,report=future.result(timeout=10)
            assert json.loads((report/'auth.json').read_bytes())['providers']['offline']['generation']==1
    assert process.exitcode == (92 if crash else 0)
    assert (run/h.SNAPSHOT/'auth-inflight.json').exists() == crash


def test_review7_auth_wait_is_bounded(tmp_path, runtime, monkeypatch):
    from climate_monitor import hermes_identity as h, hermes_auth_state as auth
    run=tmp_path/'run';run.mkdir();ref=h.create_snapshot(run)
    _,_,home=h.inference_runtime(run,ref,purpose='meetings',source='climate-acquisition-run')
    monkeypatch.setattr(auth,'AUTH_LOCK_TIMEOUT',0.1)
    with h.auth_execution(home) as state:
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future=pool.submit(h.load_snapshot,run,ref)
            with pytest.raises(ValueError,match='fresh'):future.result(timeout=2)
        assert (home.parent/'auth-inflight.json').exists()
        state['returncode']=0
    h.load_snapshot(run,ref)


@pytest.mark.parametrize('plugins', [
    {'disabled':'web/tavily'}, {'disabled':[{}]}, {'disabled':['web/*']},
    {'disabled':['web/missing']}, {'disabled':['web/tavily','web-tavily']},
    {'disabled':['web/tavily'],'enabled':['web-tavily']},
    {'disabled':['climate-frozen-identity']}, {'disabled':['climate-acquisition-search-identity']},
])
def test_review7_invalid_web_denials(tmp_path,runtime,plugins):
    from climate_monitor import hermes_identity as h
    home,exe=runtime
    manifest=exe.parents[2]/'plugins/web/tavily/plugin.yaml';manifest.parent.mkdir(parents=True)
    manifest.write_text('name: web-tavily\nkind: backend\nprovides_web_providers: [tavily]\n')
    (home/'config.yaml').write_text(json.dumps({'plugins':plugins}))
    with pytest.raises(ValueError,match='fresh'):h.create_snapshot(tmp_path/'run')
    assert not (tmp_path/'run'/h.SNAPSHOT).exists()


def test_review8_exact_pinned_reader(tmp_path):
    import hashlib
    from climate_monitor.hermes_acquisition_policy import validate_dependency_source, provider_dependency_policy
    raw = (Path(__file__).parent / 'fixtures/issue140-reader/budgets.py').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == '15e3b825e370da5a9ddfebf1b8ebe40169c580446e1a66288aa3e80711beb343'
    validate_dependency_source(raw)
    site = tmp_path / 'usr/local/lib/python3.12/site-packages/web_listening/request'
    site.mkdir(parents=True)
    (site / 'budgets.py').write_bytes(raw)
    (site / 'model.py').write_text('class Budgets: pass\nclass RequestValidationError(ValueError): pass\n')
    policy = provider_dependency_policy(tmp_path / 'usr/local/bin/python')
    assert policy['acquisition/web_listening/request/budgets.py'] == raw


@pytest.mark.parametrize('attempt', [1, 2])
@pytest.mark.parametrize('change', ['mutate', 'shadow'])
def test_review8_runtime_bytes_bound(tmp_path, runtime, attempt, change):
    from climate_monitor import hermes_identity as h
    _, executable = runtime
    site = executable.parent.parent / 'lib/python3.12/site-packages'
    (site / 'driftdep').mkdir(parents=True)
    module = site / 'driftdep/__init__.py'
    module.write_text('value = 1\n')
    dist = site / 'driftdep-1.dist-info'; dist.mkdir()
    (dist / 'METADATA').write_text('Name: driftdep\nVersion: 1\n')
    (dist / 'RECORD').write_text('driftdep/__init__.py,,\n')
    run = tmp_path / 'run'; run.mkdir()
    ref = h.create_snapshot(run)
    if attempt == 2:
        h.load_snapshot(run, ref)
    if change == 'mutate':
        module.write_text('value = 2\n')
    else:
        (site / 'unowned_shadow.py').write_text('value = 2\n')
    with pytest.raises(ValueError, match='drift|fresh'):
        h.load_snapshot(run, ref)


@pytest.mark.parametrize('existing', [False, True])
def test_review8_entrypoint_private_home(tmp_path, runtime, existing):
    import subprocess
    from climate_monitor import hermes_identity as h
    home = tmp_path / 'volume/hermes'
    if existing:
        home.mkdir(parents=True, mode=0o755)
    env = dict(os.environ, HERMES_HOME=str(home), HERMES_DASHBOARD_ENABLED='0')
    for name in list(env):
        if name.startswith('CLIMATE_'):
            env.pop(name)
    result = subprocess.run(['sh', 'scripts/docker_entrypoint.sh', 'true'], env=env, capture_output=True)
    assert result.returncode == 0
    assert home.is_dir() and home.stat().st_mode & 0o777 == 0o700
    run = tmp_path / 'run'; run.mkdir()
    reference = h.create_snapshot(run, environ=env)
    payload = h.load_snapshot(run, reference)
    assert not (home / 'config.yaml').exists() and not (home / 'auth.json').exists()
    assert payload['auth'] == {}


@pytest.mark.parametrize('purpose', ['acquisition', 'report', 'meetings'])
@pytest.mark.parametrize('attempt', [1, 2])
def test_review8_child_rechecks_runtime_before_import(tmp_path, runtime, purpose, attempt):
    import subprocess
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from test_issue117_request_boundaries import new_protocol_binding
    _, executable = runtime
    site = executable.parent.parent / 'lib/python3.12/site-packages'
    site.mkdir(parents=True)
    module = site / 'driftdep.py'; module.write_text('value = 1\n')
    sentinel = tmp_path / 'unbound-executed'
    run = tmp_path / 'run'; run.mkdir()
    binding = new_protocol_binding(run)
    binding['hermes_snapshot'] = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id'])
    for turn in range(1, attempt + 1):
        if purpose == 'acquisition':
            binding['attempt'] = turn
            path = run / f'attempt-{turn}.json'; path.write_text(json.dumps(binding))
            command = ['hermes']
            env, home = hooks.install_hooks(command, path, binding, {})
        else:
            command, env, home = h.inference_runtime(run, binding['hermes_snapshot'], purpose=purpose,
                                                    source='climate-acquisition-' + binding['run_id'])
        command = h.launch_command(home, '-c', 'import driftdep; assert driftdep.value == 1')
        result = subprocess.run(command, env=env, cwd=home, capture_output=True)
        assert result.returncode == 0
    module.write_text('from pathlib import Path\nPath(' + repr(str(sentinel)) + ').touch()\n')
    # Use the already-prepared command to exercise the child boundary itself.
    result = subprocess.run(command, env=env, cwd=home, capture_output=True)
    assert result.returncode == 65 and not result.stdout and not result.stderr
    assert not sentinel.exists()


@pytest.mark.parametrize('kind', ['traversal', 'absolute', 'duplicate', 'symlink', 'fifo', 'permissions', 'oversized', 'native', 'data'])
def test_review8_record_and_runtime_security(tmp_path, kind):
    from climate_monitor import hermes_runtime_inventory as inventory
    site = tmp_path / 'prefix/lib/python3.12/site-packages'; site.mkdir(parents=True)
    dist = site / 'example-1.dist-info'; dist.mkdir()
    module = site / ('module.so' if kind == 'native' else 'data.bin' if kind == 'data' else 'module.py')
    module.write_bytes(b'original')
    record = dist / 'RECORD'; record.write_text(module.name + ',,\n')
    if kind in {'native', 'data'}:
        first = inventory.inventory([str(site)])
        module.write_bytes(b'changed')
        assert inventory.inventory([str(site)]) != first
        return
    if kind == 'traversal': record.write_text('../../../../escape,,\n')
    if kind == 'absolute': record.write_text('/tmp/escape,,\n')
    if kind == 'duplicate': record.write_text('module.py,,\nmodule.py,,\n')
    if kind == 'symlink':
        module.unlink(); module.symlink_to(record)
    if kind == 'fifo':
        module.unlink(); os.mkfifo(module)
    if kind == 'permissions': module.chmod(0o666)
    if kind == 'oversized':
        with module.open('wb') as stream: stream.truncate(inventory.MAX_FILE + 1)
    with pytest.raises((ValueError, OSError)):
        inventory.inventory([str(site)])


@pytest.mark.parametrize('kind', ['symlink', 'file', 'relative'])
def test_review8_entrypoint_rejects_unsafe_home(tmp_path, kind):
    import subprocess
    target = tmp_path / 'target'; target.mkdir(mode=0o755)
    home = tmp_path / 'home'
    if kind == 'symlink': home.symlink_to(target)
    elif kind == 'file': home.write_text('not a directory')
    else: home = Path('relative-private-home')
    env = dict(os.environ, HERMES_HOME=str(home), HERMES_DASHBOARD_ENABLED='0')
    for name in list(env):
        if name.startswith('CLIMATE_'): env.pop(name)
    result = subprocess.run(['sh', 'scripts/docker_entrypoint.sh', 'true'], env=env, capture_output=True)
    assert result.returncode == 78
    assert result.stderr.strip() == b'cannot establish private Hermes home'
    assert target.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize('body', [
    'getter = getattr\ngetter(object(), "find_spec")',
    'import sys as runtime\ngetattr(runtime, input())',
    'from importlib.metadata import distribution',
    'getattr(value, "__dict__")',
    '__import__(input())',
])
def test_review8_attribute_access_does_not_enable_loader_aliases(body):
    from climate_monitor.hermes_acquisition_policy import validate_dependency_source
    with pytest.raises(ValueError, match='unsupported'):
        validate_dependency_source(body.encode())


def test_review8_unmanifested_package_origin_rejected(tmp_path, runtime):
    import py_compile, subprocess
    from climate_monitor import hermes_identity as h
    _, executable = runtime
    sentinel = tmp_path / 'executed'
    source = tmp_path / 'source.py'
    source.write_text('from pathlib import Path\nPath(' + repr(str(sentinel)) + ').touch()\n')
    # Sourceless import is outside the package-source inventory, unlike site-packages.
    py_compile.compile(str(source), cfile=str(executable.parents[2] / 'unbound.pyc'), doraise=True)
    run = tmp_path / 'run'; run.mkdir()
    ref = h.create_snapshot(run)
    _, env, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    result = subprocess.run(h.launch_command(home, '-c', 'import unbound'), env=env, cwd=home, capture_output=True)
    assert result.returncode == 65
    assert not sentinel.exists()


def test_review8_vendored_record_is_bound_data(tmp_path):
    from climate_monitor.hermes_runtime_inventory import inventory
    site = tmp_path / 'lib/python3.12/site-packages'; site.mkdir(parents=True)
    vendor = site / 'package/_vendor/copied-1.dist-info'; vendor.mkdir(parents=True)
    record = vendor / 'RECORD'; record.write_text('../../../bin/not-installed,,\n')
    first = inventory([str(site)])
    record.write_text('../../../bin/changed,,\n')
    assert inventory([str(site)]) != first


def test_review8_bound_finder_preserves_distribution_metadata(tmp_path):
    import subprocess, sys
    site = tmp_path / 'site-packages'
    dist = site / 'offline-1.dist-info'; dist.mkdir(parents=True)
    (dist / 'METADATA').write_text('Name: offline\nVersion: 1\n')
    helper = Path(__file__).parents[1] / 'climate_monitor/hermes_runtime_inventory.py'
    code = 'import runpy,sys; m=runpy.run_path(' + repr(str(helper)) + '); m["install_origin_guard"](set(),sys.path); sys.path.append(' + repr(str(site)) + '); from importlib.metadata import version; assert version("offline")=="1"'
    result = subprocess.run([sys.executable, '-I', '-S', '-c', code], capture_output=True)
    assert result.returncode == 0


def test_review8_private_cache_cannot_supply_unbound_code(tmp_path, runtime, monkeypatch):
    import py_compile, subprocess, sys, importlib.util
    from climate_monitor import hermes_identity as h
    _, executable = runtime
    sentinel = tmp_path / 'cache-executed'
    poison = 'from pathlib import Path; Path(' + repr(str(sentinel)) + ').touch(); value = 2\n'
    source = executable.parents[2] / 'cacheprobe.py'
    source.write_text('value = 1\n#' + ' ' * (len(poison) - len('value = 1\n#\n')) + '\n')
    run = tmp_path / 'run'; run.mkdir()
    ref = h.create_snapshot(run)
    _, env, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    forged = tmp_path / 'forged.py'; forged.write_text(poison)
    os.utime(forged, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns))
    with monkeypatch.context() as context:
        context.setattr(sys, 'pycache_prefix', str(run / h.SNAPSHOT / 'inactive-bytecode'))
        cache = Path(importlib.util.cache_from_source(str(source)))
        cache.parent.mkdir(parents=True)
        py_compile.compile(str(forged), cfile=str(cache), dfile=str(source), doraise=True)
    result = subprocess.run(h.launch_command(home, '-c', 'import cacheprobe; assert cacheprobe.value == 1'),
                            env=env, cwd=home, capture_output=True)
    assert result.returncode == 0
    assert not sentinel.exists()


def test_review8_runtime_commitments_skip_content_reads(tmp_path, monkeypatch):
    from climate_monitor import hermes_runtime_inventory as r
    site = tmp_path / 'lib/python3.12/site-packages'; site.mkdir(parents=True)
    (site / 'module.py').write_text('value = 1\n')
    import time
    time.sleep(1.1)  # Move past the filesystem's coalesced timestamp tick.
    commitments = {}
    expected = r.inventory([str(site)], capture=commitments)
    encoded = r.encode_commitments(commitments)
    commitments = r.decode_commitments(encoded)
    r._DIGEST_CACHE.clear()  # A new hook process has no in-memory cache.
    def forbidden(*args, **kwargs):
        pytest.fail('unchanged hook runtime must not rehash file contents')
    monkeypatch.setattr(r, 'file_digest', forbidden)
    for attempt in (1, 2):
        files = set()
        assert r.inventory([str(site)], commitments=commitments, bound_files=files) == expected
        assert files == {str(site / 'module.py')}


@pytest.mark.parametrize('mutation', ['content', 'restored_mtime', 'shadow', 'replacement', 'missing'])
def test_review8_runtime_commitments_reject_drift(tmp_path, mutation):
    from climate_monitor import hermes_runtime_inventory as r
    site = tmp_path / 'site-packages'; site.mkdir()
    module = site / 'module.py'; module.write_text('value = 1\n')
    commitments = {}; r.inventory([str(site)], capture=commitments)
    before = module.stat()
    if mutation in {'content', 'restored_mtime'}:
        module.write_text('value = 2\n')
        if mutation == 'restored_mtime': os.utime(module, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif mutation == 'shadow': (site / 'shadow.py').write_text('value = 3\n')
    elif mutation == 'replacement':
        module.unlink(); module.write_text('value = 1\n')
    else: module.unlink()
    with pytest.raises(ValueError, match='runtime'):
        r.inventory([str(site)], commitments=commitments)


def test_review8_runtime_commitment_corruption_fails_before_child(tmp_path, runtime):
    import subprocess
    from climate_monitor import hermes_identity as h
    run = tmp_path / 'run'; run.mkdir()
    ref = h.create_snapshot(run)
    _, env, home = h.inference_runtime(run, ref, purpose='report', source='climate-acquisition-run')
    command = h.launch_command(home, '-c', 'raise AssertionError("must not run")')
    index = run / h.SNAPSHOT / 'bootstrap/runtime-commitments.zlib'
    index.write_bytes(index.read_bytes() + b'x')
    with pytest.raises(ValueError, match='start a fresh run'):
        h.load_snapshot(run, ref)
    result = subprocess.run(command, env=env, cwd=home, capture_output=True)
    assert result.returncode == 65 and not result.stdout and not result.stderr


def test_review8_hook_verifier_rejects_timeout_block(tmp_path, runtime):
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from test_issue117_request_boundaries import new_protocol_binding
    _, executable = runtime
    shell = executable.parents[2] / 'agent/shell_hooks.py'
    shell.write_text(shell.read_text() + '\ndef run_once(spec, payload):\n return {"parsed": {"action": "block", "message": "timed out"}, "returncode": None, "timed_out": True}\n')
    run = tmp_path / 'run'; run.mkdir()
    binding = new_protocol_binding(run)
    binding['hermes_snapshot'] = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id'])
    path = run / 'attempt-1.json'; path.write_text(json.dumps(binding))
    with pytest.raises(ValueError, match='runtime verification failed'):
        hooks.install_hooks(['hermes'], path, binding, {})


@pytest.mark.parametrize('attempt', [1, 2])
def test_review9_deployed_reader_root_survives_install(tmp_path, runtime, monkeypatch, attempt):
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from test_issue117_request_boundaries import new_protocol_binding
    deployed = tmp_path / 'opt/web-listening-data'
    browser = deployed / 'browser-runtimes/playwright/browsers/chromium-test/chrome-linux64/chrome'
    browser.parent.mkdir(parents=True)
    browser.write_bytes(b'offline browser fixture\n')
    monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(deployed))
    run = tmp_path / 'run'; run.mkdir()
    binding = new_protocol_binding(run)
    binding['hermes_snapshot'] = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id'])
    monkeypatch.delenv('CLIMATE_WEB_LISTENING_DATA_DIR')
    for number in range(1, attempt + 1):
        binding['attempt'] = number
        path = run / f'attempt-{number}.json'; path.write_text(json.dumps(binding))
        env, home = hooks.install_hooks(['hermes'], path, binding, {})
        child = Path(env['CLIMATE_WEB_LISTENING_DATA_DIR'])
        assert child == deployed
        assert (child / browser.relative_to(deployed)).is_file()


def _review9_browser(tmp_path, interpreter=None):
    import ast, hashlib, os, shutil, sys
    fixture = Path(__file__).parent / 'fixtures/issue140-reader/browser'
    source = fixture / 'browser_acquisition.py'
    tree = ast.parse(source.read_bytes())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {'host_runtime', 'tree_digest'}]
    namespace = {'Path': Path, 'hashlib': hashlib}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    root = tmp_path / 'deployed'; root.mkdir()
    venv = root / 'browser-runtimes/playwright'
    (venv / 'bin').mkdir(parents=True)
    if interpreter is None:
        (venv / 'bin/python').symlink_to(Path(sys.executable).resolve())
    else:
        # Share bytes without relocating Python through a second venv symlink.
        os.link(interpreter, venv / 'bin/python')
    package = venv / 'lib/python3.12/site-packages/playwright'; package.mkdir(parents=True)
    (package / '__init__.py').write_text('# offline SDK fixture\n')
    (venv / 'lib64').symlink_to('lib')
    if interpreter is not None:
        (venv / 'pyvenv.cfg').write_bytes((interpreter.parent.parent / 'pyvenv.cfg').read_bytes())
    else:
        (venv / 'pyvenv.cfg').write_text('include-system-site-packages = false\n')
    browser = venv / 'browsers/chromium-1234/chrome-linux64/chrome'
    browser.parent.mkdir(parents=True); browser.write_bytes(b'offline browser binary\n'); browser.chmod(0o700)
    entry = json.loads((fixture / 'runtime-lock.json').read_bytes())['tools']['playwright']
    # Synthetic runtime assets use their measured hashes; adapter bytes are exact.
    entry['sdk_tree_sha256'] = namespace['tree_digest'](package)
    entry['browser_sha256'] = hashlib.sha256(browser.read_bytes()).hexdigest()
    config = namespace['host_runtime'](venv, root, entry)
    installed = root / 'tools/acquisition/acquisition.playwright/1.0.0'; installed.mkdir(parents=True)
    for name in ('tool.py', 'tool.json', 'runtime-lock.json'): shutil.copyfile(fixture / name, installed / name)
    (installed / 'runtime.json').write_text(json.dumps(config))
    (installed / 'state.json').write_text(json.dumps({'schema_version':'web-listening-tool-state.v1','qualified':True,'disabled':False,'broken':False,'failure_code':None}))
    (installed.parent / 'active.json').write_text(json.dumps({'schema_version':'web-listening-tool-active.v1','version':'1.0.0'}))
    return root, venv, installed, config


def test_review9_actual_adapter_no_site_and_mutable_state(tmp_path, runtime, monkeypatch, safe_managed_interpreter):
    import subprocess
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from test_issue117_request_boundaries import new_protocol_binding
    root, venv, installed, config = _review9_browser(tmp_path, safe_managed_interpreter)
    sentinel = tmp_path / 'startup-ran'
    site = venv / 'lib/python3.12/site-packages'
    (site / 'unsafe.pth').write_text('import startup_helper\n')
    (site / 'startup_helper.py').write_text('from pathlib import Path; Path(' + repr(str(sentinel)) + ').touch()\n')
    monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(root))
    run = tmp_path / 'run'; run.mkdir()
    binding = new_protocol_binding(run)
    ref = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id']); binding['hermes_snapshot'] = ref
    monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(tmp_path / 'different'))
    for attempt in (1, 2):
        (root / 'artifacts').mkdir(exist_ok=True)
        (root / 'artifacts' / str(attempt)).write_text('mutable output')
        (root / 'jobs.sqlite3').write_bytes(bytes([attempt]))
        (root / 'history').mkdir(exist_ok=True)
        binding['attempt'] = attempt
        path = run / f'attempt-{attempt}.json'; path.write_text(json.dumps(binding))
        env, home = hooks.install_hooks(['hermes'], path, binding, {})
        assert env['CLIMATE_WEB_LISTENING_DATA_DIR'] == str(root)
        command = [config['python'], '-I', '-S', str(home.parent / 'bootstrap/launcher.py'), '--reader-tool', str(home), str(installed / 'tool.py')]
        request = {'protocol_version':'web-listening-tool-qualification.v1','operation':'describe','tool_id':'acquisition.playwright','version':'1.0.0','category':'acquisition'}
        result = subprocess.run(command, input=json.dumps(request), text=True, capture_output=True, env={'PATH':env['PATH']})
        assert result.returncode == 0 and json.loads(result.stdout)['status'] == 'ok'
        assert not sentinel.exists()
        assert h.load_snapshot(run, ref)
    for purpose in ('report', 'meetings'):
        _, env, home = h.inference_runtime(run, ref, purpose=purpose, source='climate-acquisition-' + binding['run_id'])
        assert 'CLIMATE_WEB_LISTENING_DATA_DIR' not in env
        assert str(root) not in (home / 'config.yaml').read_text()


@pytest.mark.parametrize('damage', ['binary', 'sdk', 'pth_helper', 'member', 'mode', 'link', 'active', 'config'])
def test_review9_reader_drift_rejected(tmp_path, runtime, monkeypatch, damage, safe_managed_interpreter):
    from climate_monitor import hermes_identity as h
    root, venv, installed, _ = _review9_browser(tmp_path, safe_managed_interpreter)
    helper = venv / 'lib/python3.12/site-packages/startup_helper.py'; helper.write_text('# frozen\n')
    monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(root))
    run = tmp_path / 'run'; run.mkdir()
    ref = h.create_snapshot(run)
    target = {'binary':venv/'browsers/chromium-1234/chrome-linux64/chrome',
              'sdk':venv/'lib/python3.12/site-packages/playwright/__init__.py',
              'pth_helper':helper, 'active':installed.parent/'active.json', 'config':installed/'runtime.json'}.get(damage)
    if target: target.write_bytes(target.read_bytes() + b'changed')
    elif damage == 'member': (installed / 'new.py').write_text('# shadow\n')
    elif damage == 'mode': installed.chmod(0o777)
    else:
        (venv / 'bin/python').unlink(); (venv / 'bin/python').symlink_to('/bin/false')
    with pytest.raises(ValueError, match='start a fresh run'):
        h.load_snapshot(run, ref)


def test_review9_tool_file_symlink_rejected_before_publication(tmp_path, runtime, monkeypatch, safe_managed_interpreter):
    from climate_monitor import hermes_identity as h
    root, _, installed, _ = _review9_browser(tmp_path, safe_managed_interpreter)
    real = installed / 'implementation.py'; (installed / 'tool.py').rename(real)
    (installed / 'tool.py').symlink_to(real.name)
    monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(root))
    run = tmp_path / 'run'; run.mkdir()
    with pytest.raises(ValueError):
        h.create_snapshot(run)
    assert not (run / h.SNAPSHOT).exists()


def test_review9_pinned_browser_discovery_qualification(tmp_path, safe_managed_interpreter):
    import ast, hashlib, subprocess
    from types import SimpleNamespace
    root, _, installed, config = _review9_browser(tmp_path, safe_managed_interpreter)
    fixture = Path(__file__).parent / 'fixtures/issue140-reader/browser'
    tree = ast.parse((fixture / 'browser_acquisition.py').read_bytes())
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {'host_runtime','tree_digest','_runtime_identity_matches'}]
    lock = json.loads((installed / 'runtime-lock.json').read_bytes())
    # As in upstream offline tests, small fixture assets use measured test pins.
    # The qualification function and adapter bytes remain exact upstream source.
    lock['tools']['playwright']['sdk_tree_sha256'] = config['sdk_tree_sha256']
    lock['tools']['playwright']['browser_sha256'] = config['browser_sha256']
    raw = json.dumps(lock).encode(); (installed / 'runtime-lock.json').write_bytes(raw)
    namespace = {'Path':Path,'hashlib':hashlib,'json':json,'subprocess':subprocess,
                 'FROZEN_RUNTIME_LOCK_SHA256':hashlib.sha256(raw).hexdigest()}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(fixture / 'browser_acquisition.py'), 'exec'), namespace)
    manifest = SimpleNamespace(tool_id='acquisition.playwright', version='1.0.0')
    assert namespace['_runtime_identity_matches'](root, manifest)
    Path(config['browser']).write_bytes(b'changed browser')
    assert not namespace['_runtime_identity_matches'](root, manifest)


@pytest.mark.parametrize('value', [None, ''])
def test_review9_absent_root_is_private_empty_runtime(tmp_path, runtime, monkeypatch, value):
    from climate_monitor import hermes_identity as h
    if value is None: monkeypatch.delenv('CLIMATE_WEB_LISTENING_DATA_DIR', raising=False)
    else: monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', value)
    run = tmp_path / 'run'; run.mkdir()
    ref = h.create_snapshot(run)
    payload = h.load_snapshot(run, ref)
    root = Path(payload['reader_runtime']['root'])
    assert root == run / 'managed/web-listening-runtime'
    assert root.stat().st_mode & 0o777 == 0o700
    assert payload['reader_runtime']['absent'] == ['tools', 'browser-runtimes']
    (root / 'artifacts').mkdir()
    assert h.load_snapshot(run, ref)
    (root / 'tools').mkdir()
    with pytest.raises(ValueError, match='start a fresh run'):
        h.load_snapshot(run, ref)


@pytest.mark.parametrize('attempt', [1, 2])
def test_review9_reader_child_refuses_changed_runtime(tmp_path, runtime, monkeypatch, attempt, safe_managed_interpreter):
    import subprocess, secrets
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from test_issue117_request_boundaries import new_protocol_binding
    root, venv, installed, config = _review9_browser(tmp_path, safe_managed_interpreter)
    marker = secrets.token_hex(24)
    (installed / 'private-marker.txt').write_text(marker); (installed / 'private-marker.txt').chmod(0o600)
    monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(root))
    run = tmp_path / 'run'; run.mkdir()
    binding = new_protocol_binding(run)
    ref = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id']); binding['hermes_snapshot'] = ref
    for number in range(1, attempt + 1):
        binding['attempt'] = number
        path = run / f'attempt-{number}.json'; path.write_text(json.dumps(binding))
        env, home = hooks.install_hooks(['hermes'], path, binding, {})
    command = [config['python'], '-I', '-S', str(home.parent / 'bootstrap/launcher.py'), '--reader-tool', str(home), str(installed / 'tool.py')]
    Path(config['browser']).write_bytes(b'mutated runtime')
    result = subprocess.run(command, env={'PATH':env['PATH']}, capture_output=True)
    assert result.returncode == 65 and not result.stdout and not result.stderr
    assert marker not in path.read_text() and str(root) not in path.read_text()
    assert marker not in json.dumps(ref)


def test_review9_adapter_checks_attempt_seal(tmp_path, runtime, monkeypatch, safe_managed_interpreter):
    import subprocess
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from test_issue117_request_boundaries import new_protocol_binding
    root, _, installed, config = _review9_browser(tmp_path, safe_managed_interpreter)
    monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(root))
    run = tmp_path / 'run'; run.mkdir()
    binding = new_protocol_binding(run)
    binding['hermes_snapshot'] = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id'])
    path = run / 'attempt-1.json'; path.write_text(json.dumps(binding))
    env, home = hooks.install_hooks(['hermes'], path, binding, {})
    seal = home / 'attempt-policy.json'; value = json.loads(seal.read_bytes()); value['binding_sha256'] = '0'*64; seal.write_text(json.dumps(value))
    command = [config['python'],'-I','-S',str(home.parent/'bootstrap/launcher.py'),'--reader-tool',str(home),str(installed/'tool.py')]
    request = {'protocol_version':'web-listening-tool-qualification.v1','operation':'describe','tool_id':'acquisition.playwright','version':'1.0.0','category':'acquisition'}
    result = subprocess.run(command,input=json.dumps(request),text=True,capture_output=True,env={'PATH':env['PATH']})
    assert result.returncode == 65 and not result.stdout and not result.stderr


def test_review9_reader_mutation_during_collection_never_publishes(tmp_path, runtime, monkeypatch, safe_managed_interpreter):
    from climate_monitor import hermes_identity as h, hermes_reader_runtime as reader
    root, _, _, config = _review9_browser(tmp_path, safe_managed_interpreter)
    monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(root))
    original = reader.collect
    calls = 0
    def changing(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1: Path(config['browser']).write_bytes(b'collection-window change')
        return result
    monkeypatch.setattr(reader, 'collect', changing)
    run = tmp_path / 'run'; run.mkdir()
    with pytest.raises(ValueError, match='changed during collection'):
        h.create_snapshot(run)
    assert not (run / h.SNAPSHOT).exists()


def test_review9_worker_controlled_fetch_uses_frozen_reader(tmp_path, runtime, monkeypatch, safe_managed_interpreter):
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks, article_content_adapter as article
    import scripts.run_agent_acquisition as runner
    from test_issue117_request_boundaries import new_protocol_binding
    root, _, _, _ = _review9_browser(tmp_path, safe_managed_interpreter)
    monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(root))
    run = tmp_path / 'run'; run.mkdir()
    binding = new_protocol_binding(run)
    binding['hermes_snapshot'] = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id'])
    path = run / 'attempt-1.json'; path.write_text(json.dumps(binding))
    _, home = hooks.install_hooks(['hermes'], path, binding, {})
    monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(tmp_path / 'ambient-change'))
    monkeypatch.delenv('CLIMATE_ACQUISITION_ATTEMPT', raising=False)
    monkeypatch.setattr(runner, '_bound_source_scope', lambda *a, **kw: ('site', {}))
    seen = {}
    def fetch(*args, **kwargs):
        seen.update(kwargs)
        return {'status':'unavailable','failure_reason':'offline fixture','attempts':[]}
    monkeypatch.setattr(article, 'fetch_article_content', fetch)
    runner._controlled_fetch_payload(path, binding, {'items':[{'url':'https://offline.invalid/article','source':'site'}]})
    assert seen['data_root'] == str(root)
    assert seen['reader_home'] == str(home)


def test_review9_installed_tool_bound_sibling(tmp_path, runtime, monkeypatch, safe_managed_interpreter):
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from test_issue117_request_boundaries import new_protocol_binding
    import subprocess
    root, _, _, _ = _review9_browser(tmp_path, safe_managed_interpreter)
    tool = root / 'tools/transform/offline/1.0.0/tool.py'
    tool.parent.mkdir(parents=True)
    tool.write_text('import bound_reader_helper\nprint(bound_reader_helper.VALUE)\n')
    helper = tool.with_name('bound_reader_helper.py')
    helper.write_text('VALUE = "bound helper"\n')
    monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(root))
    run = tmp_path / 'run'; run.mkdir()
    binding = new_protocol_binding(run)
    ref = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id'])
    binding['hermes_snapshot'] = ref
    for attempt in (1, 2):
        binding['attempt'] = attempt
        path = run / f'attempt-{attempt}.json'; path.write_text(json.dumps(binding))
        env, home = hooks.install_hooks(['hermes'], path, binding, {})
        command = h.launch_command(home, '--reader-tool', str(home), str(tool))
        child = subprocess.run(command, env=env, cwd=home, capture_output=True, text=True)
        assert child.returncode == 0 and child.stdout.strip() == 'bound helper'
        assert binding['hermes_snapshot'] == ref
    helper.write_text('raise RuntimeError("changed bound helper")\n')
    with pytest.raises(ValueError, match='fresh'):
        h.load_snapshot(run, ref)
    child = subprocess.run(command, env=env, cwd=home, capture_output=True, text=True)
    assert child.returncode == 65 and not child.stdout and not child.stderr


@pytest.mark.parametrize('selection', ['project', 'empty_home', 'home'])
def test_review9_reader_dotenv_precedence(tmp_path, runtime, monkeypatch, selection, safe_managed_interpreter):
    from climate_monitor import hermes_identity as h, hermes_acquisition_hooks as hooks
    from test_issue117_request_boundaries import new_protocol_binding
    ambient, executable = runtime
    root, _, _, _ = _review9_browser(tmp_path, safe_managed_interpreter)
    home_env = ambient / '.env'
    project_env = executable.parents[2] / '.env'
    home_env.write_text('UNRELATED_SETTING=offline\n')
    project_env.write_text('CLIMATE_WEB_LISTENING_DATA_DIR=' + str(root) + '\n')
    for path in (home_env, project_env): path.chmod(0o600)
    monkeypatch.delenv('CLIMATE_WEB_LISTENING_DATA_DIR', raising=False)
    if selection == 'empty_home':
        monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(root))
        home_env.write_text('CLIMATE_WEB_LISTENING_DATA_DIR=\n')
    elif selection == 'home':
        monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(tmp_path / 'unselected'))
        home_env.write_text('CLIMATE_WEB_LISTENING_DATA_DIR=' + str(root) + '\n')
    run = tmp_path / 'run'; run.mkdir()
    binding = new_protocol_binding(run)
    ref = h.create_snapshot(run, source='climate-acquisition-' + binding['run_id'])
    binding['hermes_snapshot'] = ref
    expected = run / 'managed/web-listening-runtime' if selection == 'empty_home' else root
    home_env.unlink(); project_env.unlink()
    monkeypatch.setenv('CLIMATE_WEB_LISTENING_DATA_DIR', str(tmp_path / 'changed'))
    for attempt in (1, 2):
        binding['attempt'] = attempt
        path = run / f'attempt-{attempt}.json'; path.write_text(json.dumps(binding))
        env, _ = hooks.install_hooks(['hermes'], path, binding, {})
        assert env['CLIMATE_WEB_LISTENING_DATA_DIR'] == str(expected)
        assert h.load_snapshot(run, ref)['reader_runtime']['root'] == str(expected)


@pytest.mark.parametrize('size', [290_614_600, 320 * 1024 ** 2])
def test_review9_declared_chromium_runtime_file_size(size):
    """Actual declared Chromium size and inclusive ceiling, without allocating bytes."""
    import stat
    from types import SimpleNamespace
    from climate_monitor import hermes_runtime_inventory

    hermes_runtime_inventory.check(SimpleNamespace(
        st_mode=stat.S_IFREG | 0o755, st_uid=0, st_size=size))


def test_review9_runtime_file_above_chromium_ceiling_rejected():
    import stat
    from types import SimpleNamespace
    from climate_monitor import hermes_runtime_inventory

    with pytest.raises(ValueError, match='unsafe Hermes runtime file'):
        hermes_runtime_inventory.check(SimpleNamespace(
            st_mode=stat.S_IFREG | 0o755, st_uid=0,
            st_size=320 * 1024 ** 2 + 1))


_REVIEW9_DOCKER_WEB = json.loads((Path(__file__).parent / 'fixtures/issue140-hermes-web/5538bd-helpers.json').read_text())['helpers']


@pytest.mark.parametrize('helper', _REVIEW9_DOCKER_WEB, ids=lambda row: row['key'])
@pytest.mark.parametrize('drift', [False, 'name_byte', 'return_semantics'])
def test_review9_docker_python312_web_helper_contract(tmp_path, monkeypatch, helper, drift):
    import ast
    import hashlib
    from climate_monitor import hermes_identity as h, hermes_web_inputs as web
    source = helper['source']
    assert hashlib.sha256(source.encode()).hexdigest() == helper['source_sha256']
    original_parse = ast.parse

    def python312_parse(*args, **kwargs):
        tree = original_parse(*args, **kwargs)
        # Python 3.12 adds this empty field. Exercise its exact AST on 3.11 CI too.
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and 'type_params' not in node._fields:
                node._fields = (*node._fields, 'type_params')
                node.type_params = []
        return tree

    tree = python312_parse(source)
    assert hashlib.sha256(ast.dump(tree.body[0], include_attributes=False).encode()).hexdigest() == helper['ast312']
    if drift == 'return_semantics':
        # Keep the audited helper name and all dynamic reads, but alter its result.
        source = source.replace('return ', 'return not ', 1)
    elif drift == 'name_byte':
        # One-character semantic change in the function name keeps its dynamic reads.
        source = source.replace('def ', 'def x', 1)
    monkeypatch.setattr(ast, 'parse', python312_parse)
    path = tmp_path / helper['key'].split(':')[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    _, metadata = h.secure_read(path)
    if drift:
        with pytest.raises(ValueError, match='unsupported Hermes web input'):
            web.web_environment_names(tmp_path, {str(path): metadata}, {})
    else:
        inputs = {}
        web.web_environment_names(tmp_path, {str(path): metadata}, inputs)
        assert inputs == {str(path): metadata}


def _ci_identity_backend(root, backend):
    if backend == 'host':
        from climate_monitor.hermes_identity import publish_identity, load_identity
        return lambda value: publish_identity(root, value), lambda: load_identity(root)
    import runpy
    from climate_monitor import hermes_frozen_policy
    policy = runpy.run_path(hermes_frozen_policy.__file__, init_globals={'ROOT': root, 'SOURCE': 'run'})
    return policy['_publish'], policy['_read']


@pytest.mark.parametrize('backend', ['host', 'frozen'])
@pytest.mark.parametrize('operation', ['identical_writer', 'reader'])
def test_ci_identity_alias_cleanup_read_race(tmp_path, monkeypatch, backend, operation):
    """Force alias unlink during an unlocked read, or observe it blocked by flock."""
    import fcntl
    publish, read = _ci_identity_backend(tmp_path, backend)
    value = {'provider': 'provider', 'model': 'model', 'source': 'run'}
    cleanup_ready, allow_cleanup, cleaned = (threading.Event() for _ in range(3))
    contender_ready, allow_read = threading.Event(), threading.Event()
    role = threading.local()
    original_unlink, original_read, original_flock = os.unlink, os.read, fcntl.flock
    path = tmp_path / 'effective-identity.json'
    link_counts = []
    identity_inode = None
    cleanup_epoch = 0
    original_fstat, original_lstat = os.fstat, Path.lstat

    def metadata(st):
        # Model the inode ctime event even on filesystems that coalesce clock ticks.
        # The hard-link removal itself and nlink 2 -> 1 transition remain real.
        if st.st_ino != identity_inode:
            return st
        from types import SimpleNamespace
        fields = {name: getattr(st, name) for name in dir(st) if name.startswith('st_')}
        fields['st_ctime_ns'] += cleanup_epoch
        return SimpleNamespace(**fields)

    def unlink(name, *args, **kwargs):
        nonlocal identity_inode, cleanup_epoch
        if getattr(role, 'name', '') == 'winner' and Path(name).name.startswith('.identity-'):
            identity_inode = path.stat().st_ino
            cleanup_ready.set()
            assert allow_cleanup.wait(10)
            link_counts.append(path.stat().st_nlink)
            result = original_unlink(name, *args, **kwargs)
            cleanup_epoch += 1
            link_counts.append(path.stat().st_nlink)
            cleaned.set()
            return result
        return original_unlink(name, *args, **kwargs)

    def bounded_read(fd, size):
        if (getattr(role, 'name', '') == 'contender' and
                os.fstat(fd).st_ino == path.stat().st_ino):
            contender_ready.set()
            assert allow_read.wait(10)
        return original_read(fd, size)

    def flock(fd, flags):
        if getattr(role, 'name', '') == 'contender' and flags & (fcntl.LOCK_SH | fcntl.LOCK_EX):
            try:
                return original_flock(fd, flags | fcntl.LOCK_NB)
            except BlockingIOError:
                contender_ready.set()
        return original_flock(fd, flags)

    def winner():
        role.name = 'winner'
        publish(value)

    def contender():
        role.name = 'contender'
        if operation == 'identical_writer':
            publish(value)
        return read()

    monkeypatch.setattr(os, 'fstat', lambda fd: metadata(original_fstat(fd)))
    monkeypatch.setattr(Path, 'lstat', lambda path, *a, **kw: metadata(original_lstat(path, *a, **kw)))
    monkeypatch.setattr(os, 'unlink', unlink)
    monkeypatch.setattr(os, 'read', bounded_read)
    monkeypatch.setattr(fcntl, 'flock', flock)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first = pool.submit(winner)
        second = None
        try:
            assert cleanup_ready.wait(10)
            second = pool.submit(contender)
            assert contender_ready.wait(10)
            allow_cleanup.set()
            assert cleaned.wait(10)
            allow_read.set()
            first.result(timeout=10)
            assert second.result(timeout=10) == value
        finally:
            allow_cleanup.set()
            allow_read.set()
    assert link_counts == [2, 1]
    assert read() == value
    assert not list(tmp_path.glob('.identity-*'))


def _ci_identity_process(root, backend, value, barrier, results):
    publish, read = _ci_identity_backend(root, backend)
    barrier.wait(timeout=10)
    try:
        publish(value)
        results.put(('accepted', read()))
    except ValueError:
        results.put(('conflict', None))


@pytest.mark.parametrize('conflicting', [False, True])
@pytest.mark.parametrize('round_number', range(3))
def test_ci_identity_mixed_process_writers(tmp_path, conflicting, round_number):
    import multiprocessing
    from climate_monitor.hermes_identity import load_identity
    ctx = multiprocessing.get_context('fork')
    barrier, results = ctx.Barrier(6), ctx.Queue()
    values = [{'provider': 'provider', 'model': 'other' if conflicting and i % 2 else 'model',
               'source': 'run'} for i in range(6)]
    children = [ctx.Process(target=_ci_identity_process,
                           args=(tmp_path, 'host' if i % 2 else 'frozen', value, barrier, results))
                for i, value in enumerate(values)]
    try:
        for child in children:
            child.start()
        outcomes = [results.get(timeout=15) for _ in children]
        for child in children:
            child.join(timeout=15)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
        results.close()
        results.join_thread()
    accepted = [value for status, value in outcomes if status == 'accepted']
    assert len(accepted) == (3 if conflicting else 6)
    assert all(value == load_identity(tmp_path) for value in accepted)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['effective-identity.json']


@pytest.mark.parametrize('backend', ['host', 'frozen'])
@pytest.mark.parametrize('failure_call', [1, 2, 3])
def test_ci_identity_fsync_failure_releases_lock(tmp_path, monkeypatch, backend, failure_call):
    publish, read = _ci_identity_backend(tmp_path, backend)
    value = {'provider': 'provider', 'model': 'model', 'source': 'run'}
    original = os.fsync
    calls = 0

    def fail_once(fd):
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError('synthetic fsync failure')
        return original(fd)

    monkeypatch.setattr(os, 'fsync', fail_once)
    with pytest.raises(OSError, match='synthetic fsync failure'):
        publish(value)
    assert not list(tmp_path.glob('.identity-*'))
    if failure_call == 1:
        assert read() is None
    monkeypatch.setattr(os, 'fsync', original)
    # A separate open must acquire the directory lock after the failed publisher.
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        pool.submit(publish, value).result(timeout=10)
    assert read() == value
    assert sorted(p.name for p in tmp_path.iterdir()) == ['effective-identity.json']


@pytest.mark.parametrize('backend', ['host', 'frozen'])
@pytest.mark.parametrize('damage', ['symlink', 'fifo', 'mode'])
def test_ci_identity_unsafe_winner_still_rejected(tmp_path, backend, damage):
    publish, read = _ci_identity_backend(tmp_path, backend)
    value = {'provider': 'provider', 'model': 'model', 'source': 'run'}
    path = tmp_path / 'effective-identity.json'
    if damage == 'symlink':
        path.symlink_to(tmp_path / 'missing')
    elif damage == 'fifo':
        os.mkfifo(path, 0o600)
    else:
        path.write_text(json.dumps(value))
        path.chmod(0o644)
    with pytest.raises(ValueError):
        read()
    with pytest.raises(ValueError):
        publish(value)
    assert not list(tmp_path.glob('.identity-*'))


def _ci_identity_hold_directory(root, ready):
    from climate_monitor.hermes_frozen_policy import _identity_lock
    with _identity_lock(root, exclusive=True):
        ready.send('locked')
        ready.recv()


def test_ci_identity_process_exit_releases_directory_lock(tmp_path):
    import multiprocessing
    from climate_monitor.hermes_identity import publish_identity, load_identity
    ctx = multiprocessing.get_context('fork')
    parent, child = ctx.Pipe()
    holder = ctx.Process(target=_ci_identity_hold_directory, args=(tmp_path, child))
    holder.start()
    try:
        assert parent.poll(10) and parent.recv() == 'locked'
        holder.terminate()
        holder.join(timeout=10)
        assert holder.exitcode is not None
        value = {'provider': 'provider', 'model': 'model', 'source': 'run'}
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            assert pool.submit(publish_identity, tmp_path, value).result(timeout=10) == value
        assert load_identity(tmp_path) == value
        assert sorted(p.name for p in tmp_path.iterdir()) == ['effective-identity.json']
    finally:
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)
        parent.close()
        child.close()


def _ci_identity_fork_check(child_action, parent_action):
    import select
    import signal
    gate_read, gate_write = os.pipe()
    result_read, result_write = os.pipe()
    owned = {gate_read, gate_write, result_read, result_write}
    pid = None
    reaped = False
    try:
        pid = os.fork()
        if pid == 0:
            try:
                os.close(gate_write)
                os.close(result_read)
                assert os.read(gate_read, 1) == b'g'
                child_action()
                os.write(result_write, b'ok')
                os._exit(0)
            except BaseException:
                os._exit(1)
        for fd in (gate_read, result_write):
            os.close(fd)
            owned.remove(fd)
        parent_action()
        os.write(gate_write, b'g')
        assert select.select([result_read], [], [], 3)[0], 'CHILD_BLOCKED_ON_INHERITED_FLOCK'
        assert os.read(result_read, 2) == b'ok'
        _, status = os.waitpid(pid, 0)
        reaped = True
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        if pid and not reaped:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        for fd in owned:
            os.close(fd)


@pytest.mark.parametrize('holder_backend', ['host', 'frozen'])
@pytest.mark.parametrize('child_backend', ['host', 'frozen'])
@pytest.mark.parametrize('operation', ['read', 'publish'])
def test_ci_identity_fork_from_other_thread(tmp_path, holder_backend, child_backend, operation):
    """Release the parent's lock, then require the child not to retain its alias."""
    import runpy
    from climate_monitor import hermes_frozen_policy
    holder_lock = hermes_frozen_policy._identity_lock
    if holder_backend == 'frozen':
        holder_lock = runpy.run_path(hermes_frozen_policy.__file__)['_identity_lock']
    publish, read = _ci_identity_backend(tmp_path, child_backend)
    value = {'provider': 'provider', 'model': 'model', 'source': 'run'}
    publish(value)
    held, release = threading.Event(), threading.Event()

    def hold():
        with holder_lock(tmp_path, exclusive=True):
            held.set()
            assert release.wait(10)

    def parent():
        release.set()
        holder.join(timeout=10)
        assert not holder.is_alive()

    def child():
        if operation == 'publish':
            publish(value)
        assert read() == value

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(10)
        _ci_identity_fork_check(child, parent)
    finally:
        release.set()
        holder.join(timeout=10)
    assert read() == value


def test_ci_identity_fork_open_registration_window(tmp_path, monkeypatch):
    """Fork's before callback waits while a real fd is open but not yet tracked."""
    import runpy
    from climate_monitor import hermes_frozen_policy
    opened, resume_open, fork_started, release = (threading.Event() for _ in range(4))
    register, original_open, original_close = os.register_at_fork, os.open, os.close
    callbacks = {}
    captured_fd = []
    close_guarded = []

    def capture(**kwargs):
        callbacks.update(kwargs)
        def before():
            fork_started.set()
            kwargs['before']()
        register(**dict(kwargs, before=before))

    monkeypatch.setattr(os, 'register_at_fork', capture)
    policy = runpy.run_path(hermes_frozen_policy.__file__, init_globals={'ROOT': tmp_path, 'SOURCE': 'run'})
    guard = callbacks['before'].__self__

    def opening(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if threading.current_thread() is holder and Path(path) == tmp_path:
            assert guard.locked()
            captured_fd.append(fd)
            opened.set()
            assert resume_open.wait(10)
        return fd

    def closing(fd):
        if threading.current_thread() is holder and fd in captured_fd:
            close_guarded.append(guard.locked())
        return original_close(fd)

    def hold():
        with policy['_identity_lock'](tmp_path, exclusive=True):
            assert release.wait(10)

    def resume():
        assert fork_started.wait(10)
        assert guard.locked()
        resume_open.set()

    def parent():
        release.set()
        holder.join(timeout=10)
        coordinator.join(timeout=10)
        assert not holder.is_alive() and not coordinator.is_alive()

    def child():
        value = {'provider': 'provider', 'model': 'model', 'source': 'run'}
        policy['_publish'](value)
        assert policy['_read']() == value

    holder = threading.Thread(target=hold)
    coordinator = threading.Thread(target=resume)
    monkeypatch.setattr(os, 'open', opening)
    monkeypatch.setattr(os, 'close', closing)
    holder.start()
    try:
        assert opened.wait(10)
        coordinator.start()
        _ci_identity_fork_check(child, parent)
    finally:
        resume_open.set()
        release.set()
        holder.join(timeout=10)
        if coordinator.ident is not None:
            coordinator.join(timeout=10)
    assert close_guarded == [True]


def test_ci_identity_fork_duplicate_imports_and_fd_reuse(tmp_path):
    import fcntl
    import runpy
    from climate_monitor import hermes_frozen_policy as host
    copies = [runpy.run_path(host.__file__) for _ in range(2)]
    factories = [host._identity_directory, *(copy['_identity_directory'] for copy in copies)]
    roots = [tmp_path / str(i) for i in range(3)]
    for root in roots:
        root.mkdir(mode=0o700)
    contexts = [factory(root) for factory, root in zip(factories, roots)]
    old_fds = [context.__enter__() for context in contexts]
    for fd in old_fds:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def parent():
        for context in reversed(contexts):
            context.__exit__(None, None, None)

    def child():
        for fd in old_fds:
            with pytest.raises(OSError):
                os.fstat(fd)
        fresh = [factory(root) for factory, root in zip(factories, roots)]
        new_fds = [context.__enter__() for context in fresh]
        assert new_fds == old_fds  # Linux reuses the lowest free descriptor numbers.
        for context in contexts:
            context.__exit__(None, None, None)
        for fd, root in zip(new_fds, roots):
            assert os.fstat(fd).st_ino == root.stat().st_ino
        for context in reversed(fresh):
            context.__exit__(None, None, None)

    try:
        _ci_identity_fork_check(child, parent)
    finally:
        parent()
