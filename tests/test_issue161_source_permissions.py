"""Application source permissions must not relax private/external input readers."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def source_copy(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    root = tmp_path / 'source'
    # Full application source, including default configuration used by managed start.
    for name in ('climate_monitor', 'climate_registry', 'climate_delivery', 'agentic_wiki',
                 'scripts', 'monitoring', 'tests'):
        shutil.copytree(checkout / name, root / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    return root


@pytest.mark.parametrize('directories,files', [(0o755, 0o644), (0o755, 0o664),
                                              (0o775, 0o644), (0o775, 0o664)])
def test_snapshot_and_managed_start_from_source_modes(source_copy, tmp_path, directories, files):
    for path in [source_copy, *source_copy.rglob('*')]:
        path.chmod(directories if path.is_dir() else files)
    assert source_copy.stat().st_mode & 0o777 == directories
    assert (source_copy / 'climate_monitor/hermes_identity.py').stat().st_mode & 0o777 == files
    code = '''
import json, sys
from pathlib import Path
from climate_monitor import hermes_identity as h
from climate_monitor.management import ManagementService
sys.path.insert(0, 'tests')
from test_issue94_management_console import _definition, _store
root = Path(sys.argv[1])
run = root / 'direct'; run.mkdir(mode=0o700)
ref = h.create_snapshot(run)
h.load_snapshot(run, ref)
for p in (run / h.SNAPSHOT).rglob('*'):
    assert p.stat().st_mode & 0o777 == (0o700 if p.is_dir() else 0o600)
frozen = (run / h.SNAPSHOT / 'acquisition/climate_monitor/hermes_identity.py').read_text()
assert '_read_application_source' not in frozen
store = _store(root)
store.save(_definition(root), actor='test')
def launch(binding):
    directory = root / 'runs' / binding['run_id']
    assert json.loads((directory / 'binding.json').read_text()) == json.loads(json.dumps(binding))
    h.load_snapshot(directory, binding['hermes_snapshot'])
    return 123
result = ManagementService(store=store, runtime_root=root / 'runs', launcher=launch).start(trigger='scheduled')
assert result['accepted'] and result['pid'] == 123
print('snapshot and managed start verified')
'''
    # Synthetic provenance label for this disposable, modified source fixture.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
               CLIMATE_REPOSITORY_COMMIT_SHA='a' * 40)
    env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-B', '-c', code, str(tmp_path)],
                            cwd=source_copy, env=env, text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'snapshot and managed start verified'


@pytest.fixture
def copied_identity(source_copy, monkeypatch):
    from climate_monitor import hermes_identity as h, hermes_acquisition_policy as bundle
    monkeypatch.setattr(h, '__file__', str(source_copy / 'climate_monitor/hermes_identity.py'))
    monkeypatch.setattr(bundle, '__file__', str(source_copy / 'climate_monitor/hermes_acquisition_policy.py'))
    return h


@pytest.mark.parametrize('damage', ['symlink', 'parent_symlink', 'root_symlink', 'fifo',
                                    'world_file', 'world_parent', 'world_root', 'outside_parent',
                                    'outside', 'traversal', 'oversized', 'untrusted_writers',
                                    'sticky_root', 'nontraversable_root'])
def test_source_reader_rejects_unsafe_paths(source_copy, copied_identity, tmp_path, damage):
    h = copied_identity
    path = source_copy / 'climate_monitor/hermes_frozen_policy.py'
    if damage == 'symlink':
        path.unlink(); path.symlink_to(source_copy / 'climate_monitor/models.py')
    elif damage in {'parent_symlink', 'root_symlink'}:
        parent = path.parent if damage == 'parent_symlink' else source_copy
        real = parent.with_name(parent.name + '-real')
        parent.rename(real); parent.symlink_to(real, target_is_directory=True)
    elif damage == 'fifo':
        path.unlink(); os.mkfifo(path, 0o600)
    elif damage == 'world_file':
        path.chmod(0o666)
    elif damage == 'world_parent':
        path.parent.chmod(0o777)
    elif damage == 'world_root':
        source_copy.chmod(0o777)
    elif damage in {'sticky_root', 'nontraversable_root'}:
        source_copy.chmod(0o1775 if damage == 'sticky_root' else 0o765)
        path.chmod(0o664)
    elif damage == 'outside_parent':
        tmp_path.chmod(0o775)
    elif damage == 'outside':
        path = tmp_path / 'outside'; path.write_bytes(b'x')
    elif damage == 'traversal':
        path = source_copy / 'climate_monitor/../climate_monitor/hermes_frozen_policy.py'
    elif damage == 'oversized':
        with path.open('wb') as stream:
            stream.truncate(h.MAX_FILE_BYTES + 1)
    elif damage == 'untrusted_writers':
        # This group can edit a template, but cannot edit/replace imported identity code.
        path.chmod(0o664)
    with pytest.raises(ValueError, match='unsafe|untrusted|limit'):
        h._read_application_source(path)


@pytest.mark.parametrize('field', ['st_uid', 'st_gid'])
def test_source_reader_rejects_foreign_identity(source_copy, copied_identity, monkeypatch, field):
    from types import SimpleNamespace
    path = source_copy / 'climate_monitor/hermes_frozen_policy.py'
    inode = path.stat().st_ino
    real = os.fstat
    def changed(fd):
        st = real(fd)
        if st.st_ino != inode:
            return st
        values = {name: getattr(st, name) for name in dir(st) if name.startswith('st_')}
        values[field] = 987654
        return SimpleNamespace(**values)
    monkeypatch.setattr(os, 'fstat', changed)
    with pytest.raises(ValueError, match='ownership'):
        copied_identity._read_application_source(path)


@pytest.mark.parametrize('damage', ['write', 'replace', 'parent', 'root'])
def test_source_change_during_read_rejected(source_copy, copied_identity, monkeypatch, damage):
    path = source_copy / 'climate_monitor/hermes_frozen_policy.py'
    inode = path.stat().st_ino
    real = os.read
    changed = False
    def read(fd, size):
        nonlocal changed
        raw = real(fd, size)
        if not changed and os.fstat(fd).st_ino == inode:
            changed = True
            if damage == 'write':
                path.write_bytes(b'changed')
            elif damage == 'replace':
                other = path.with_suffix('.new'); other.write_bytes(raw); other.replace(path)
            else:
                parent = path.parent if damage == 'parent' else source_copy
                parent.rename(parent.with_name(parent.name + '-old'))
                parent.mkdir()
        return raw
    monkeypatch.setattr(os, 'read', read)
    with pytest.raises(ValueError, match='changed|unavailable'):
        copied_identity._read_application_source(path)


@pytest.mark.parametrize('damage', ['symlink', 'fifo', 'world', 'between_collections'])
def test_invalid_source_never_launches(source_copy, copied_identity, tmp_path, monkeypatch, damage):
    from climate_monitor import management
    from test_issue94_management_console import _definition, _store
    path = source_copy / 'climate_monitor/hermes_frozen_policy.py'
    if damage == 'between_collections':
        original = copied_identity._collect
        def collect(*args, **kwargs):
            value = original(*args, **kwargs)
            path.write_bytes(path.read_bytes() + b'\n# changed\n')
            return value
        monkeypatch.setattr(copied_identity, '_collect', collect)
    elif damage == 'world':
        path.chmod(0o666)
    else:
        path.unlink()
        if damage == 'fifo': os.mkfifo(path, 0o600)
        else: path.symlink_to(source_copy / 'climate_monitor/models.py')
    monkeypatch.setenv('CLIMATE_MANAGED_STATE_DIR', str(tmp_path / 'state'))
    store = _store(tmp_path); store.save(_definition(tmp_path), actor='test')
    launched = []
    service = management.ManagementService(store=store, runtime_root=tmp_path / 'runs',
                                           launcher=lambda binding: launched.append(binding))
    with pytest.raises(ValueError):
        service.start()
    assert not launched
    assert not list((tmp_path / 'runs').glob('*/binding.json'))
    assert not list((tmp_path / 'runs').glob('*/attempt-*.json'))
    assert not list((tmp_path / 'runs').glob('*/hermes-private/complete'))


@pytest.mark.parametrize('private', [False, True])
@pytest.mark.parametrize('damage', ['file', 'parent'])
def test_external_and_private_reads_stay_strict(tmp_path, private, damage):
    from climate_monitor import hermes_identity as h, hermes_runtime_inventory as inventory
    parent = tmp_path / 'external'; parent.mkdir(mode=0o700)
    path = parent / 'input'; path.write_bytes(b'x'); path.chmod(0o600)
    (path if damage == 'file' else parent).chmod(0o664 if damage == 'file' else 0o775)
    with pytest.raises(ValueError, match='unsafe'):
        h.secure_read(path, private=private)
    with pytest.raises(ValueError, match='unsafe'):
        inventory.file_digest(path, inventory.signature(path.stat()))


def test_host_exec_still_refuses_writable_temp_ancestor(tmp_path):
    from climate_monitor import host_exec
    ancestor = tmp_path / 'shared'; ancestor.mkdir(mode=0o775); ancestor.chmod(0o775)
    private = ancestor / 'private'; private.mkdir(mode=0o700)
    with pytest.raises(host_exec.ProtocolError):
        host_exec._directory(str(private), os.getuid())


@pytest.mark.parametrize('target', ['launcher', 'package', 'config', 'auth', 'home_parent'])
def test_snapshot_still_refuses_unsafe_external_inputs(source_copy, copied_identity, tmp_path, target):
    source_copy.chmod(0o775)
    executable = Path(os.environ['HERMES_EXECUTABLE'])
    home = Path(os.environ['HERMES_HOME'])
    path = {'launcher': executable, 'package': executable.parents[2] / 'run_agent.py',
            'config': home / 'config.yaml', 'auth': home / 'auth.json', 'home_parent': home}[target]
    path.chmod(0o775 if path.is_dir() else 0o664)
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='unsafe'):
        copied_identity.create_snapshot(run)
    assert not (run / copied_identity.SNAPSHOT).exists()


def test_source_replace_between_stat_and_open(source_copy, copied_identity, monkeypatch):
    path = source_copy / 'climate_monitor/hermes_frozen_policy.py'
    real = os.open
    changed = False
    def open_file(name, flags, *args, **kwargs):
        nonlocal changed
        if name == path.name and not changed:
            changed = True
            other = path.with_suffix('.new'); other.write_bytes(path.read_bytes()); other.replace(path)
        return real(name, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', open_file)
    with pytest.raises(ValueError, match='changed'):
        copied_identity._read_application_source(path)


def test_source_group_requires_membership(source_copy, copied_identity, monkeypatch):
    monkeypatch.setattr(os, 'getegid', lambda: 987654)
    monkeypatch.setattr(os, 'getgroups', lambda: [])
    with pytest.raises(ValueError, match='group'):
        copied_identity._read_application_source(source_copy / 'climate_monitor/hermes_frozen_policy.py')


@pytest.mark.parametrize('root_mode,package_mode,accepted', [
    (0o765, 0o755, False),
    (0o755, 0o765, False),
    (0o765, 0o775, False),
    (0o775, 0o765, False),
    (0o775, 0o775, True),
])
def test_group_writable_identity_requires_source_traversal(
    source_copy, copied_identity, root_mode, package_mode, accepted,
):
    package = source_copy / 'climate_monitor'
    module = package / 'hermes_identity.py'
    target = package / 'hermes_frozen_policy.py'
    source_copy.chmod(root_mode)
    package.chmod(package_mode)
    module.chmod(0o664)
    target.chmod(0o664)
    assert source_copy.stat().st_mode & 0o777 == root_mode
    assert package.stat().st_mode & 0o777 == package_mode
    assert module.stat().st_mode & 0o777 == 0o664
    assert target.stat().st_mode & 0o777 == 0o664
    # Owner access lets the collector read; authority must reflect group access.
    if accepted:
        assert copied_identity._read_application_source(target)[0] == target.read_bytes()
    else:
        with pytest.raises(ValueError, match='untrusted application source writers'):
            copied_identity._read_application_source(target)
