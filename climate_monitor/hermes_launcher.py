"""Private managed entrypoint. Invoked only by the bound Python with -I -S.

Never calls site.main/addsitedir or the console script. Interpreter startup has
only the standard library; .pth, editable finders and usercustomize never run.
"""
import sys
import os
import json
import hashlib
import stat
from pathlib import Path


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o777 != 0o600 or before.st_size > 4 * 1024 * 1024:
            raise ValueError()
        raw = os.read(fd, before.st_size + 1)
        after = os.fstat(fd)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if identity(after) != identity(before) or len(raw) != before.st_size:
            raise ValueError()
        return raw
    finally:
        os.close(fd)


def main():
    if not sys.flags.no_site or not sys.flags.isolated:
        raise ValueError()
    root = Path(__file__).parent.parent
    raw = read_private(root / 'manifest.json')
    digest = hashlib.sha256(raw).hexdigest()
    if read_private(root / 'complete').decode().strip() != digest:
        raise ValueError()
    payload = json.loads(raw)
    # Verify every private executable byte before enabling any runtime path.
    for name, metadata in payload['policy'].items():
        if Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError()
        content = read_private(root / name)
        if metadata != {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}:
            raise ValueError()
    args = sys.argv[1:]
    reader_tool = args[:1] == ['--reader-tool']
    if reader_tool:
        os.environ['HERMES_HOME'] = args[1]
    home = Path(os.environ['HERMES_HOME'])
    if home.parent != root:
        raise ValueError()
    config = json.loads(read_private(home / 'config.yaml'))
    enabled = config['plugins']['enabled']
    plugins = home / 'plugins'
    if set(os.listdir(plugins)) != set(enabled):
        raise ValueError()
    known = {}
    for manifest, implementation in [('policy/plugin.json', 'policy/identity.py'),
                                     ('policy/search-plugin.json', 'policy/search-plugin.py')]:
        name = json.loads(read_private(root / manifest))['name']
        known[name] = {'plugin.yaml': manifest, '__init__.py': implementation}
    for name in enabled:
        if name not in known or set(os.listdir(plugins / name)) != set(known[name]):
            raise ValueError()
        for filename, frozen in known[name].items():
            if read_private(plugins / name / filename) != read_private(root / frozen):
                raise ValueError()
    sys.dont_write_bytecode = True
    # A verified regular file cannot contain bytecode-cache descendants.
    # Every cache lookup fails ENOTDIR; even a populated sibling cache is inert.
    sys.pycache_prefix = str(root / 'complete')
    # -I has already removed cwd, PYTHONPATH and user site. These are the only
    # additional import paths; no .pth text or editable finder is evaluated.
    stdlib_paths = list(sys.path)
    sys.path[:0] = [str(root / 'bootstrap')]
    import runtime_inventory
    runtime_files = set()
    if reader_tool or os.environ.get('CLIMATE_ACQUISITION_ATTEMPT'):
        import reader_runtime
        reader_runtime.verify(root, payload, read_private, runtime_inventory, bound_files=runtime_files)
    if reader_tool:
        reader_runtime.verify_attempt(root, home, payload, read_private, runtime_inventory)
        import runpy
        tool = Path(args[2])
        if (str(tool) not in runtime_files or tool.suffix != '.py'
                or not tool.is_relative_to(Path(payload['reader_runtime']['root']) / 'tools')
                or not home.name.startswith('attempt-')):
            raise ValueError()
        configuration = tool.parent / 'runtime.json'
        if configuration.exists():
            fd = runtime_inventory.open_file(configuration)
            try:
                config = json.loads(os.read(fd, 65537))
            finally:
                os.close(fd)
            if os.path.abspath(sys.executable) != config['python']:
                raise ValueError()
            sys.path.append(str(Path(config['python']).parent.parent / 'lib/python3.12/site-packages'))
        else:
            if runtime_inventory.inventory(payload['runtime_paths'][1:], bound_files=runtime_files,
                    commitments=runtime_inventory.decode_commitments(read_private(root / 'bootstrap/runtime-commitments.zlib'))) != payload['runtime']['site_packages']:
                raise ValueError()
            sys.path.extend(payload['runtime_paths'])
        runtime_inventory.install_origin_guard(runtime_files, stdlib_paths)
        # Match Python script imports, but only from the inventoried tool tree.
        sys.path.insert(0, str(tool.parent))
        sys.argv = [str(tool), *args[3:]]
        runpy.run_path(str(tool), run_name='__main__')
        return

    if runtime_inventory.inventory(payload['runtime_paths'][1:], bound_files=runtime_files,
                                   commitments=runtime_inventory.decode_commitments(read_private(root / 'bootstrap/runtime-commitments.zlib'))) != payload['runtime']['site_packages']:
        raise ValueError()
    runtime_files.update(name for name, value in payload['runtime'].items() if 'sha256' in value and name.startswith('/'))
    runtime_files.update(str(root / name) for name in payload['policy'])
    for name in enabled:
        runtime_files.update(str(plugins / name / filename) for filename in known[name])
    runtime_inventory.install_origin_guard(runtime_files, stdlib_paths)
    if os.environ.get('CLIMATE_ACQUISITION_ATTEMPT'):
        import acquisition_imports
        acquisition_imports.install(root / 'acquisition', payload['package_root'])
    sys.path.extend(payload['runtime_paths'])
    startup = root / 'bootstrap/sitecustomize.py'
    exec(compile(read_private(startup), str(startup), 'exec'), {'__file__': str(startup)})
    args = sys.argv[1:]
    if args[:1] == ['--budget-hook']:
        import runpy
        path = root / 'acquisition/scripts/acquisition_budget_hook.py'
        sys.argv = [str(path), *args[1:]]
        runpy.run_path(str(path), run_name='__main__')
    elif args[:1] == ['-c'] and len(args) >= 2:
        sys.argv = ['-c', *args[2:]]
        exec(compile(args[1], '<managed-probe>', 'exec'), {'__name__': '__main__'})
    else:
        from hermes_cli.main import main as cli
        sys.argv = [payload['executable'], *args]
        return cli()


if __name__ == '__main__':
    try:
        result = main()
    except SystemExit:
        raise
    except BaseException:
        os._exit(65)
    sys.exit(result)
