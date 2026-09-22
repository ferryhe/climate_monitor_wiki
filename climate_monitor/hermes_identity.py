"""Private, durable execution inputs for managed Hermes runs.

The public reference contains only a schema and digest. Never put source values
or parser exceptions in diagnostics: config, environment and auth are secrets.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import tempfile

import yaml

SCHEMA = 'climate-hermes-execution-snapshot.v1'
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PRIVATE_BYTES = 4 * 1024 * 1024
MAX_TREE_BYTES = 128 * 1024 * 1024
MAX_TREE_FILES = 10000
SNAPSHOT = 'hermes-private'
ROUTE_KEYS = ('model_aliases', 'model', 'providers', 'custom_providers', 'fallback_model', 'fallback_providers',
              'credential_pool_strategies', 'smart_model_routing', 'bedrock', 'vertex')
BASE_ENV = {
    'PATH', 'HOME', 'LANG', 'LC_ALL', 'HERMES_INFERENCE_PROVIDER', 'HERMES_INFERENCE_MODEL',
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'HERMES_CA_BUNDLE', 'AWS_CA_BUNDLE',
    'AWS_ENDPOINT_URL', 'AWS_ENDPOINT_URL_BEDROCK_RUNTIME',
}
CA_FILES = {'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'HERMES_CA_BUNDLE', 'AWS_CA_BUNDLE'}
# Provider names from Hermes' built-in auth registry; configured key_env and
# ${NAME} references are added below, without copying unrelated environment.
CREDENTIAL_ENV = {
    'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY',
    'ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'GOOGLE_API_KEY', 'GEMINI_API_KEY',
    'DEEPSEEK_API_KEY', 'XAI_API_KEY', 'GROQ_API_KEY', 'CEREBRAS_API_KEY',
    'MISTRAL_API_KEY', 'TOGETHER_API_KEY', 'FIREWORKS_API_KEY', 'COPILOT_GITHUB_TOKEN',
    'GITHUB_TOKEN', 'ZAI_API_KEY', 'KIMI_API_KEY', 'MINIMAX_API_KEY', 'QWEN_API_KEY',
    'AI_GATEWAY_API_KEY', 'AZURE_API_KEY', 'AZURE_OPENAI_API_KEY',
    'AZURE_OPENAI_ENDPOINT', 'AZURE_OPENAI_API_VERSION', 'AZURE_FOUNDRY_API_KEY',
    'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
    'AWS_REGION', 'AWS_DEFAULT_REGION', 'AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_ROLE_ARN', 'AWS_ROLE_SESSION_NAME',
    'AWS_CONFIG_FILE', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_WEB_IDENTITY_TOKEN_FILE',
    'GOOGLE_APPLICATION_CREDENTIALS', 'GOOGLE_CLOUD_PROJECT', 'GOOGLE_CLOUD_LOCATION',
    'VERTEX_PROJECT_ID', 'VERTEX_LOCATION', 'VERTEX_CREDENTIALS_PATH',
}


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _signature(st):
    return [st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
            st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def _check(st, private, directory=False):
    valid_kind = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
    if (not valid_kind or st.st_uid not in ({os.getuid()} if private else {0, os.getuid()})
            or st.st_mode & (0o077 if private else 0o022)):
        raise ValueError('unsafe Hermes input type, ownership or permissions')
    if not directory and st.st_size > (MAX_PRIVATE_BYTES if private else MAX_FILE_BYTES):
        raise ValueError('Hermes input size limit exceeded')


def _open_parent(path):
    # Walk directory descriptors so a swapped ancestor cannot redirect a read.
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in path.parts[1:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            st = os.fstat(fd)
            if (st.st_uid not in {0, os.getuid()} or
                    (st.st_mode & 0o022 and not (st.st_uid == 0 and st.st_mode & stat.S_ISVTX))):
                raise ValueError('unsafe Hermes input parent')
        return fd, path.name
    except BaseException:
        os.close(fd)
        raise


def secure_read(path, *, private=False):
    """Reject special files before reading; bound memory even during mutation."""
    path = Path(path)
    try:
        before = path.lstat()
        _check(before, private)
        parent, name = _open_parent(path)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            opened = os.fstat(fd)
            _check(opened, private)
            if _signature(before) != _signature(opened):
                raise ValueError('Hermes input changed during collection')
            def bounded_read():
                chunks = []
                limit = MAX_PRIVATE_BYTES if private else MAX_FILE_BYTES
                remaining = limit + 1
                while remaining:
                    part = os.read(fd, min(remaining, 65536))
                    if not part:
                        break
                    chunks.append(part)
                    remaining -= len(part)
                raw = b''.join(chunks)
                if len(raw) > limit:
                    raise ValueError('Hermes input size limit exceeded')
                return raw
            raw = bounded_read()
            os.lseek(fd, 0, os.SEEK_SET)
            if bounded_read() != raw:
                raise ValueError('Hermes input changed during collection')
            if (_signature(opened) != _signature(os.fstat(fd))
                    or _signature(opened) != _signature(path.lstat()) or len(raw) != opened.st_size):
                raise ValueError('Hermes input changed during collection')
            return raw, {'identity': _signature(opened), 'sha256': _digest(raw)}
        finally:
            os.close(fd)
    except OSError:
        raise ValueError('unsafe or unavailable Hermes input') from None


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir_private(path):
    path = Path(path)
    if path.exists():
        _check(path.lstat(), True, directory=True)
        return
    if not path.parent.exists():
        _mkdir_private(path.parent)
    path.mkdir(mode=0o700)
    _sync_dir(path.parent)


def _write(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _directory_names(path):
    path = Path(path)
    before = path.lstat()
    _check(before, False, directory=True)
    parent, name = _open_parent(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        if _signature(before) != _signature(os.fstat(fd)):
            raise ValueError('Hermes directory changed during collection')
        names = []
        with os.scandir(fd) as entries:
            for entry in entries:
                if len(names) >= MAX_TREE_FILES:
                    raise ValueError('Hermes tree entry limit exceeded')
                names.append(entry.name)
        if (_signature(before) != _signature(os.fstat(fd))
                or _signature(before) != _signature(path.lstat())):
            raise ValueError('Hermes directory changed during collection')
        return sorted(names)
    finally:
        os.close(fd)


def _tree(root, *, package=False):
    """Bounded deterministic inventory. Reject symlinks, including directories."""
    result = {}
    total = 0
    entry_count = 0
    skipped = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', 'tests', 'docs', 'apps'} if package else set()
    def walk(directory, depth=0):
        nonlocal total, entry_count
        if depth > 32:
            raise ValueError("Hermes tree depth limit exceeded")
        names = _directory_names(directory)
        entry_count += len(names)
        if entry_count > MAX_TREE_FILES:
            raise ValueError('Hermes tree entry limit exceeded')
        for name in names:
            if name in skipped or (package and name.startswith('.')):
                continue
            path = directory / name
            st = path.lstat()
            if stat.S_ISDIR(st.st_mode):
                walk(path, depth + 1)
            elif not package or path.suffix in {'.py', '.toml', '.json', '.yaml', '.yml', '.txt', '.pth', '.so', '.pem', '.crt'}:
                raw, metadata = secure_read(path)
                total += len(raw)
                if len(result) >= MAX_TREE_FILES or total > MAX_TREE_BYTES:
                    raise ValueError('Hermes tree size limit exceeded')
                result[str(path)] = (raw, metadata)
            elif stat.S_ISLNK(st.st_mode):
                raise ValueError('unsafe Hermes package symlink')
    walk(Path(root))
    return result


def _runtime_paths(interpreter, executable, package):
    paths = [str(package)]
    for prefix in (Path(interpreter).absolute().parent.parent, Path(executable).parent.parent):
        for site in sorted(prefix.glob('lib/python*/site-packages')):
            if str(site) not in paths:
                paths.append(str(site))
    return paths


def launch_command(home, *arguments):
    """Every managed Python child bypasses site startup, including probes/hooks."""
    root = Path(home).parent
    payload = json.loads(secure_read(root / 'manifest.json', private=True)[0])
    return [payload['interpreter'], '-I', '-S', str(root / 'bootstrap/launcher.py'), *arguments]


def _editable_package_root(executable, interpreter):
    """Resolve pip's PEP 610 editable installation without site/.pth/imports."""
    from urllib.parse import urlsplit, unquote
    from email.parser import BytesParser
    import tomllib
    candidates = []
    evidence = {}
    sites = _runtime_paths(interpreter, executable, '')[1:]
    if len(sites) > MAX_TREE_FILES:
        raise ValueError('Hermes runtime entry limit exceeded')
    for site in sites:
        for name in _directory_names(Path(site)):
            if not re.fullmatch(r'hermes[-_]agent-[^/]+\.dist-info', name, re.I):
                continue
            if candidates:
                raise ValueError('ambiguous Hermes editable package root')
            directory = Path(site) / name
            raw, meta = secure_read(directory / 'METADATA')
            metadata = BytesParser().parsebytes(raw)
            if (re.sub(r'[-_.]+', '-', metadata.get('Name', '')).lower() != 'hermes-agent'
                    or len(metadata.get_all('Name', [])) != 1 or len(metadata.get_all('Version', [])) != 1):
                raise ValueError('invalid Hermes distribution metadata')
            evidence[str(directory / 'METADATA')] = (raw, meta)
            raw, meta = secure_read(directory / 'direct_url.json')
            evidence[str(directory / 'direct_url.json')] = (raw, meta)
            direct = json.loads(raw)
            if not isinstance(direct, dict) or direct.get('dir_info') != {'editable': True}:
                raise ValueError('unsupported Hermes editable metadata')
            url = urlsplit(direct['url'])
            path = Path(unquote(url.path))
            if (url.scheme != 'file' or url.netloc or url.query or url.fragment or
                    not path.is_absolute() or '..' in path.parts):
                raise ValueError('unsafe Hermes editable path')
            raw, meta = secure_read(path / 'pyproject.toml')
            project = tomllib.loads(raw.decode())['project']
            if not isinstance(project, dict) or project.get('name') != 'hermes-agent' or project.get('version') != metadata['Version']:
                raise ValueError('inconsistent Hermes editable project')
            _directory_names(path / 'hermes_cli')
            evidence[str(path / 'pyproject.toml')] = (raw, meta)
            candidates.append(path)
    if len(candidates) != 1:
        raise ValueError('missing or ambiguous Hermes editable package root')
    return candidates[0], evidence


def _runtime(executable, *, commitments=None):
    executable = Path(executable)
    launcher, meta = secure_read(executable)
    first = launcher.splitlines()[0].decode('utf-8')
    if first.startswith('#!') and 'python' in first:
        parts = shlex.split(first[2:])
        if len(parts) != 1 or not Path(parts[0]).is_absolute():
            raise ValueError('Hermes runtime needs an absolute Python interpreter')
        interpreter = parts[0]
    elif (first == '#!/bin/sh' and b"'python3'" in launcher[:200]
          and b'realpath -- "$0"' in launcher[:200]):
        interpreter = str(executable.parent / 'python3')
    else:
        raise ValueError('unsupported Hermes launcher runtime')
    root = next((p for p in executable.parents if (p / 'pyproject.toml').is_file()
                 and (p / 'hermes_cli').is_dir()), None)
    resolution = {}
    if root is None:
        try:
            root, resolution = _editable_package_root(executable, interpreter)
        except (ValueError, OSError, KeyError, TypeError, UnicodeError):
            raise ValueError('cannot bind Hermes editable package root; start a fresh run') from None
    inventory = _tree(root, package=True)
    total = sum(len(raw) for raw, _ in inventory.values())
    def add(path, value=None):
        nonlocal total
        value = secure_read(path) if value is None else value
        prior = inventory.get(str(path))
        total += len(value[0]) - (len(prior[0]) if prior else 0)
        if total > MAX_TREE_BYTES or (str(path) not in inventory and len(inventory) >= MAX_TREE_FILES):
            raise ValueError('Hermes runtime inventory limit exceeded')
        inventory[str(path)] = value
    for path, value in resolution.items():
        add(Path(path), value)
    add(executable, (launcher, meta))
    resolved_interpreter = str(Path(interpreter).resolve(strict=True))
    add(resolved_interpreter)
    # Bind virtualenv resolution and installed distribution manifests too.
    distribution_paths = set()
    venvs = {executable.parent.parent, Path(interpreter).absolute().parent.parent}
    for venv in sorted(venvs):
        library = venv / 'lib'
        if os.path.lexists(library):
            for name in _directory_names(library):
                packages = library / name / 'site-packages'
                if name.startswith('python') and packages.exists():
                    for entry in _directory_names(packages):
                        distribution_paths.add(packages / entry)
                        if len(distribution_paths) > MAX_TREE_FILES:
                            raise ValueError('Hermes runtime entry limit exceeded')
        if (venv / 'pyvenv.cfg').exists():
            add(venv / 'pyvenv.cfg')
    for path in sorted(distribution_paths):
        if path.name.endswith('.dist-info'):
            for name in ('METADATA', 'RECORD', 'direct_url.json'):
                if (path / name).exists():
                    if str(path / name) in resolution and secure_read(path / name) != resolution[str(path / name)]:
                        raise ValueError('Hermes editable metadata changed')
                    add(path / name)
        elif path.suffix == '.pth':
            add(path)
    metadata = {name: meta for name, (_, meta) in inventory.items()}
    from climate_monitor.hermes_runtime_inventory import inventory as runtime_inventory
    metadata['site_packages'] = runtime_inventory(_runtime_paths(interpreter, executable, root)[1:], commitments=commitments)
    metadata['package_root'] = {'path': str(root)}
    metadata['interpreter_link'] = {'path': interpreter, 'target': resolved_interpreter,
                                    'identity': _signature(Path(interpreter).lstat())}
    return interpreter, metadata


def _check_document(value):
    # Count aliases each time they are encountered: recursive or exponentially
    # shared YAML graphs cannot turn a bounded file into unbounded work.
    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > 10000 or depth > 32:
            raise ValueError('Hermes document structure limit exceeded')
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError('invalid Hermes document key')
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError('invalid Hermes document value')


def _env_reference(body):
    name = body.strip()
    if name.startswith('env:'):
        name = name[4:].strip()
    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', name):
        raise ValueError('unsupported Hermes environment reference; start a fresh run')
    return name


def _references(value):
    names = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {'key_env', 'api_key_env', 'api_key_env_var', 'base_url_env_var'} and isinstance(child, str):
                names.add(child.strip())
            names.update(_references(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_references(child))
    elif isinstance(value, str):
        names.update(_env_reference(match) for match in re.findall(r'\$\{([^}]+)\}', value))
    return names


def _route_config(config):
    """Hermes 0.20.5/0.21.3 _normalize_root_model_keys precedence.

    Normalize before projecting: root aliases otherwise disappear before the
    child's config loader can migrate them. Do not materialize task overrides.
    """
    result = {key: config[key] for key in ROUTE_KEYS if key in config}
    alias_map = config.get('model_aliases', {})
    if (not isinstance(alias_map, dict) or len(alias_map) > 1024 or any(
            not isinstance(name, str) or not name.strip() or not isinstance(entry, dict) or
            not isinstance(entry.get('model'), str) or not entry['model'].strip() or any(
                field in entry and not isinstance(entry[field], str)
                for field in ('provider', 'base_url', 'api_base', 'api_key', 'key_env'))
            for name, entry in alias_map.items())):
        raise ValueError('unsupported Hermes alias input; start a fresh run')
    original = config.get('model')
    simple = original.get('aliases') if isinstance(original, dict) else None
    for names in (alias_map, simple if isinstance(simple, dict) else {}):
        # Hermes folds names case-insensitively; canonical JSON key sorting
        # must never reverse the winner among colliding source definitions.
        if len({name.strip().lower() for name in names}) != len(names):
            raise ValueError('ambiguous Hermes aliases; start a fresh run')
    needs = any(config.get(k) for k in ('provider', 'base_url', 'context_length', 'api_base'))
    needs = needs or (isinstance(original, dict) and (
        any(original.get(k) for k in ('api_base', 'model', 'name')) or
        any(isinstance(original.get(k), dict) for k in ('default', 'model', 'name'))))
    if not needs:
        return result
    model = dict(original) if isinstance(original, dict) else {'default': original} if original else {}
    for key in ('default', 'model', 'name'):
        nested = model.get(key)
        if isinstance(nested, dict):
            model[key] = str(nested.get('model') or nested.get('default') or '').strip()
            provider = str(nested.get('provider') or '').strip()
            if provider and str(model.get('provider') or '').strip() in ('', 'auto'):
                model['provider'] = provider
    for key in ('provider', 'base_url', 'context_length'):
        if config.get(key) and not model.get(key):
            model[key] = config[key]
    for alias in (config.get('api_base'), model.get('api_base')):
        if alias and not model.get('base_url'):
            model['base_url'] = alias
    model.pop('api_base', None)
    if not model.get('default') and (model.get('model') or model.get('name')):
        model['default'] = model.get('model') or model.get('name')
    if model.get('default'):
        model.pop('model', None)
        model.pop('name', None)
    result['model'] = model
    return result


def _provider_environment(package, runtime, inputs, auto_keys=None):
    """Read only inventoried model-provider sources; never import plugin code.

    Auto selection can consult every model provider. Capture that conservative
    registry even for explicit routes, including fallback/smart routing.
    Literal profile env_vars may reference a module tuple constant. Dynamic or
    multiply assigned declarations are unsupported rather than guessed.
    """
    keys = set()
    for filename, expected in runtime.items():
        path = Path(filename)
        if not path.is_absolute() or path.suffix != '.py':
            continue
        relative = path.relative_to(package) if path.is_relative_to(package) else None
        if relative is None:
            continue
        parts = relative.parts
        provider_source = (parts[:2] == ('plugins', 'model-providers') or parts[0] == 'providers')
        builtin = parts[0] == 'hermes_cli' and (path.name in ('auth.py', 'providers.py', 'copilot_auth.py', 'auth_constants.py',
                            'auth_nous.py', 'auth_codex.py', 'auth_xai.py', 'auth_qwen.py',
                            'auth_minimax.py', 'auth_oauth_grants.py', 'auth_device_flow.py',
                            'auth_zai_kimi.py', 'auth_openrouter.py') or path.name.startswith('runtime_provider'))
        adapter = parts[0] == 'agent' and (path.name.endswith('_adapter.py') or
                   path.name.startswith('credential_') or path.name == 'anthropic_credentials.py')
        if not (provider_source or builtin or adapter):
            continue
        raw, metadata = secure_read(path)
        if metadata != expected or len(raw) > MAX_PRIVATE_BYTES:
            raise ValueError('Hermes provider source changed or exceeds limit')
        inputs[filename] = metadata
        try:
            tree = ast.parse(raw)
            nodes = list(ast.walk(tree))
            if len(nodes) > 100000:
                raise ValueError()
            constants = {}
            for statement in tree.body:
                if isinstance(statement, ast.Assign):
                    for target in statement.targets:
                        if isinstance(target, ast.Name):
                            constants.setdefault(target.id, []).append(statement.value)
            def literal(node):
                if isinstance(node, ast.Name):
                    values = constants.get(node.id, [])
                    writes = sum(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
                                 and n.id == node.id for n in nodes)
                    if len(values) != 1 or writes != 1:
                        raise ValueError()
                    return ast.literal_eval(values[0])
                return ast.literal_eval(node)
            if auto_keys is not None and relative.as_posix() == 'hermes_cli/auth.py':
                for statement in tree.body:
                    if isinstance(statement, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == '_REGISTRY_ROWS' for t in statement.targets):
                        if not isinstance(statement.value, (ast.List, ast.Tuple)):
                            raise ValueError()
                        for row in statement.value.elts:
                            if isinstance(row, ast.Call):
                                continue  # OAuth ProviderConfig entries handled separately.
                            if not isinstance(row, ast.Tuple) or len(row.elts) < 4:
                                raise ValueError()
                            name, names = literal(row.elts[0]), literal(row.elts[3])
                            if not isinstance(names, (tuple, list)) or any(not isinstance(n, str) for n in names):
                                raise ValueError()
                            keys.update(names)
                            if name not in {'copilot', 'lmstudio'}:
                                auto_keys.update(names)
            parents = {child: parent for parent in nodes for child in ast.iter_child_nodes(parent)}
            def profile_names(keyword):
                try:
                    return literal(keyword.value)
                except ValueError:
                    # 0.21.3 Kimi profiles use a module-local factory with an
                    # env_vars parameter and literal tuples at every call site.
                    if not isinstance(keyword.value, ast.Name):
                        raise
                    function = parents.get(keyword)
                    while function is not None and not isinstance(function, ast.FunctionDef):
                        function = parents.get(function)
                    if function not in tree.body or function.args.vararg or function.args.kwarg:
                        raise ValueError()
                    body = function.body
                    if (body and isinstance(body[0], ast.Expr) and
                            isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)):
                        body = body[1:]
                    if (function.decorator_list or len(body) != 1 or not isinstance(body[0], ast.Return)
                            or not isinstance(body[0].value, ast.Call)):
                        raise ValueError()
                    args = [arg.arg for arg in function.args.args]
                    index = args.index(keyword.value.id)
                    calls = [n for n in nodes if isinstance(n, ast.Call) and
                             isinstance(n.func, ast.Name) and n.func.id == function.name]
                    if not calls:
                        raise ValueError()
                    names = []
                    for call in calls:
                        supplied = ([call.args[index]] if len(call.args) > index else []) + [
                            k.value for k in call.keywords if k.arg == keyword.value.id]
                        if len(supplied) != 1:
                            raise ValueError()
                        value = literal(supplied[0])
                        if not isinstance(value, (tuple, list)):
                            raise ValueError()
                        names.extend(value)
                    return names
            for node in nodes:
                reference = None
                if isinstance(node, ast.Call) and node.args and ast.unparse(node.func) in {
                        'os.getenv', 'os.environ.get', 'env.get', 'get_secret', '_get_secret', '_scoped_key_env'}:
                    reference = node.args[0]
                elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == 'os.environ':
                    reference = node.slice
                if isinstance(reference, ast.Constant) and isinstance(reference.value, str):
                    if re.fullmatch(r'[A-Z][A-Z_0-9]*', reference.value):
                        keys.add(reference.value)
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if re.fullmatch(r'[A-Z][A-Z_0-9]*(?:KEY|TOKEN|BASE_URL|ENDPOINT)', node.value):
                        keys.add(node.value)
                if provider_source and isinstance(node, ast.keyword) and node.arg == 'env_vars':
                    names = profile_names(node)
                    if not isinstance(names, (tuple, list)) or any(
                            not isinstance(n, str) or not re.fullmatch(r'[A-Z][A-Z_0-9]*', n) for n in names):
                        raise ValueError()
                    keys.update(names)
                    call = parents[node]
                    fields = {k.arg: k.value for k in call.keywords}
                    name = fields.get('name')
                    if (auto_keys is not None and isinstance(name, ast.Constant) and
                            name.value not in {'copilot', 'lmstudio', 'custom', 'openrouter'} and
                            ('auth_type' not in fields or literal(fields['auth_type']) == 'api_key')):
                        auto_keys.update(n for n in names if not n.endswith(('_URL', '_BASE_URL')))
                if (auto_keys is not None and isinstance(node, ast.Call) and
                        isinstance(node.func, ast.Name) and node.func.id == 'ProviderConfig'):
                    fields = {k.arg: k.value for k in node.keywords}
                    if (isinstance(fields.get('id'), ast.Constant) and
                            isinstance(fields.get('auth_type'), ast.Constant) and
                            fields['auth_type'].value == 'api_key' and
                            fields['id'].value not in {'copilot', 'lmstudio'}):
                        names = literal(fields.get('api_key_env_vars', ast.Tuple(elts=[])))
                        if not isinstance(names, (list, tuple)) or any(not isinstance(n, str) for n in names):
                            raise ValueError()
                        auto_keys.update(names)
                        keys.update(names)
        except (SyntaxError, ValueError, TypeError, AttributeError, RecursionError):
            raise ValueError('unsupported Hermes provider environment declaration; start a fresh run') from None
    return {key for key in keys if not key.startswith('HERMES_SPOTIFY_') and key != 'HERMES_OAUTH_TRACE'}


def _environment_metadata(environment):
    if not isinstance(environment, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in environment.items()):
        raise ValueError('invalid frozen Hermes environment')
    return {key: {'size': len(value.encode()), 'sha256': _digest(value.encode())}
            for key, value in environment.items()}


def _expand_route(value, environment):
    if isinstance(value, dict):
        return {key: _expand_route(child, environment) for key, child in value.items()}
    if isinstance(value, list):
        return [_expand_route(child, environment) for child in value]
    if isinstance(value, str):
        def resolve(match):
            name = _env_reference(match.group(1))
            if name not in environment:
                raise ValueError('missing Hermes environment reference; start a fresh run')
            return environment[name]
        return re.sub(r'\$\{([^}]+)\}', resolve, value)
    return value

def _collect(environ, executable, reader_default):
    environment = dict(environ)
    home = Path(environment.get('HERMES_HOME') or Path(environment.get('HOME', str(Path.home()))) / '.hermes')
    _check(home.lstat(), True, directory=True)
    inputs = {}
    data = {}
    # External hydration cannot be proven complete from the process environment.
    if os.path.lexists(home / '.op.env'):
        raise ValueError('unsupported Hermes external secret input; start a fresh run')
    inputs[str(home / '.op.env')] = None
    interpreter, runtime = _runtime(executable)
    package = Path(runtime['package_root']['path'])
    for name in ('config.yaml', 'auth.json', '.env'):
        path = home / name
        try:
            path.lstat()
        except FileNotFoundError:
            inputs[str(path)] = None
            continue
        raw, metadata = secure_read(path, private=True)
        inputs[str(path)] = metadata
        data[name] = raw
    project_env = package / '.env' if package else None
    if project_env is not None:
        try:
            project_env.lstat()
        except FileNotFoundError:
            inputs[str(project_env)] = None
        else:
            raw, metadata = secure_read(project_env, private=True)
            inputs[str(project_env)] = metadata
            data['project.env'] = raw
    try:
        config = yaml.safe_load(data.get('config.yaml', b'{}')) or {}
        auth = json.loads(data.get('auth.json', b'{}'))
        if not isinstance(config, dict) or not isinstance(auth, dict):
            raise ValueError()
        _check_document(config)
        _check_document(auth)
        secrets = config.get('secrets') or {}
        if not isinstance(secrets, dict) or any(
                not isinstance(value, dict) or value.get('enabled') not in (None, False)
                for value in secrets.values()):
            raise ValueError()
        web_config = config.get("web") or {}
        if not isinstance(web_config, dict):
            raise ValueError()
        from climate_monitor.hermes_web_inputs import web_plugin_denials
        web_disabled = web_plugin_denials(config, package, runtime, inputs)
        config = _route_config(config)
    except Exception:
        raise ValueError('invalid private Hermes route or auth document, or unsupported external input; start a fresh run') from None
    # Hermes loads its home .env. Use the same dotenv grammar without importing
    # Hermes or executing any user code; never project unrelated variables.
    from dotenv import dotenv_values
    from dotenv.variables import parse_variables
    from io import StringIO
    for name in ('.env', 'project.env'):
        if name in data:
            values = dotenv_values(stream=StringIO(data[name].decode('utf-8-sig')), interpolate=False)
            for key, value in values.items():
                # Pinned Hermes: user file overrides exports; the project file
                # overrides only if there was no user file, otherwise fills gaps.
                if value is not None and (name == '.env' or '.env' not in data or key not in environment):
                    environment[key] = ''.join(atom.resolve(environment) for atom in parse_variables(value))
    if environment.get('HERMES_SHARED_AUTH_DIR'):
        raise ValueError('unsupported external Hermes auth store; start a fresh run')
    references = _references(config)
    config = _expand_route(config, environment)
    keys = BASE_ENV | CREDENTIAL_ENV | references | _references(config)
    auto_keys = {'OPENAI_API_KEY', 'OPENROUTER_API_KEY'}
    if package is not None:
        keys |= _provider_environment(package, runtime, inputs, auto_keys)
    from climate_monitor.hermes_web_inputs import web_environment_names
    web_keys = web_environment_names(package, runtime, inputs) if package is not None else set()
    web_keys |= _references(web_config)
    web_config = _expand_route(web_config, environment)
    web_env = {key: environment[key] for key in sorted(web_keys) if key in environment and key not in keys}
    frozen_env = {key: environment[key] for key in sorted(keys) if key in environment}
    # Both env_loader versions strip non-ASCII for these suffixes. Reject
    # instead of silently changing credentials while disabling that loader.
    if any(key.endswith(('_API_KEY', '_TOKEN', '_SECRET', '_KEY')) and not value.isascii()
           for key, value in (frozen_env | web_env).items()):
        raise ValueError('unsupported Hermes credential encoding; start a fresh run')
    blobs = {}
    def capture_ca(path, *, private=False):
        raw, meta = secure_read(Path(path), private=private)
        inputs[str(path)] = meta
        name = 'ca-' + _digest(str(path).encode())
        blobs[name] = raw.hex()
        return '@snapshot/' + name
    for key in ('AWS_CONFIG_FILE', 'AWS_SHARED_CREDENTIALS_FILE', 'AWS_WEB_IDENTITY_TOKEN_FILE',
                'GOOGLE_APPLICATION_CREDENTIALS', 'VERTEX_CREDENTIALS_PATH'):
        if frozen_env.get(key):
            frozen_env[key] = capture_ca(frozen_env[key], private=True)
    # httpx/requests normally obtain their default roots from this runtime's
    # certifi distribution. Freeze those bytes rather than re-reading them later.
    default_ca = []
    library = Path(executable).parent.parent / 'lib'
    if os.path.lexists(library):
        for name in _directory_names(library):
            candidate = library / name / 'site-packages/certifi/cacert.pem'
            if name.startswith('python') and os.path.lexists(candidate):
                default_ca.append(candidate)
    if len(default_ca) > 1:
        raise ValueError('ambiguous Hermes default CA input')
    if default_ca:
        for key in ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE'):
            frozen_env.setdefault(key, str(default_ca[0]))
    for key in CA_FILES:
        if frozen_env.get(key):
            frozen_env[key] = capture_ca(frozen_env[key])
    if frozen_env.get('SSL_CERT_DIR'):
        ca_root = Path(frozen_env['SSL_CERT_DIR'])
        inputs[str(ca_root)] = {'identity': _signature(ca_root.lstat())}
        tree = _tree(ca_root)
        for path, (raw, meta) in tree.items():
            inputs[path] = meta
            blobs['ca-dir/' + str(Path(path).relative_to(frozen_env['SSL_CERT_DIR']))] = raw.hex()
        frozen_env['SSL_CERT_DIR'] = '@snapshot/ca-dir'
    def project_ca(value):
        if isinstance(value, dict):
            return {k: capture_ca(v) if k == 'ssl_ca_cert' and isinstance(v, str) and v else project_ca(v)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [project_ca(v) for v in value]
        return value
    config = project_ca(config)
    web_config = project_ca(web_config)
    _credential_contract(config, auth, frozen_env, blobs, runtime, auto_keys)
    # Supported credentials never need IMDS. Prevent a later auto/OAuth failure
    # from discovering host credentials through the SDK's final fallback.
    frozen_env['AWS_EC2_METADATA_DISABLED'] = 'true'
    env_metadata = _environment_metadata(frozen_env)
    from climate_monitor import hermes_reader_runtime, hermes_runtime_inventory
    reader_root = environment.get('CLIMATE_WEB_LISTENING_DATA_DIR')
    if not reader_root:
        _mkdir_private(reader_default)
        reader_root = str(reader_default)
    reader = hermes_reader_runtime.collect(reader_root, hermes_runtime_inventory)
    return {'reader_runtime': reader, 'config': config, 'auth': auth, 'environment': frozen_env, 'blobs': blobs,
            'web_config': web_config, 'web_environment': web_env, 'web_plugins_disabled': web_disabled,
            'web_environment_metadata': _environment_metadata(web_env),
            'inputs': inputs, 'environment_metadata': env_metadata, 'runtime': runtime, 'executable': executable, 'interpreter': interpreter, 'package_root': str(package), 'runtime_paths': _runtime_paths(interpreter, executable, package)}


def create_snapshot(run_dir, *, environ=None, source=None):
    """Called while the manager owns both creation locks, before any binding."""
    run_dir = Path(run_dir)
    original_environment = dict(os.environ if environ is None else environ)
    environment = dict(original_environment)
    # Apply the supported empty-root fallback only after Hermes dotenv precedence.
    default_reader = run_dir.absolute() / 'managed/web-listening-runtime'
    candidate = environment.get('HERMES_EXECUTABLE') or shutil.which('hermes', path=environment.get('PATH', ''))
    if not candidate:
        raise ValueError('Hermes executable unavailable; start a fresh run')
    executable = str(Path(candidate).resolve(strict=True))
    managed = Path(environment.get('HERMES_MANAGED_DIR') or '/etc/hermes')
    if managed.exists():
        raise ValueError('external managed Hermes configuration cannot be frozen; start a fresh run with supported inputs')
    source = source or f'climate-acquisition-{run_dir.name}'
    _validate_identity({'provider': 'bound', 'model': 'bound', 'source': source})
    policy = _policy_bytes(run_dir / SNAPSHOT, source)
    first = _collect(environment, executable, default_reader)
    from climate_monitor.hermes_acquisition_policy import provider_dependency_policy
    dependencies = provider_dependency_policy(first['interpreter'], first['package_root'])
    second = _collect(environment, executable, default_reader)
    if first != second or (dict(os.environ if environ is None else environ) != original_environment):
        raise ValueError('Hermes inputs changed during collection; start a fresh run')
    if (policy != _policy_bytes(run_dir / SNAPSHOT, source)
            or dependencies != provider_dependency_policy(second['interpreter'], second['package_root'])):
        raise ValueError('Hermes policy changed during collection; start a fresh run')
    policy.update(dependencies)
    from climate_monitor.hermes_runtime_inventory import inventory, encode_commitments
    commitments = {}
    if inventory(first['runtime_paths'][1:], capture=commitments) != first['runtime']['site_packages']:
        raise ValueError('Hermes runtime changed during collection; start a fresh run')
    policy['bootstrap/runtime-commitments.zlib'] = encode_commitments(commitments)
    from climate_monitor import hermes_reader_runtime, hermes_runtime_inventory
    reader_commitments = {}
    if hermes_reader_runtime.collect(first['reader_runtime']['root'], hermes_runtime_inventory, capture=reader_commitments) != first['reader_runtime']:
        raise ValueError('reader runtime changed during collection; start a fresh run')
    policy['bootstrap/reader-commitments.zlib'] = encode_commitments(reader_commitments)
    if (len(policy) > MAX_TREE_FILES or sum(map(len, policy.values())) > MAX_TREE_BYTES
            or any(len(raw) > MAX_PRIVATE_BYTES for raw in policy.values())):
        raise ValueError('acquisition policy size limit exceeded')
    payload = {'schema_version': SCHEMA, **first, 'source': source,
               'policy': {name: {'size': len(raw), 'sha256': _digest(raw)} for name, raw in policy.items()}}
    raw = _bytes(payload)
    if len(raw) > MAX_PRIVATE_BYTES:
        raise ValueError('Hermes snapshot size limit exceeded')
    digest = _digest(raw)
    stage = Path(tempfile.mkdtemp(prefix='.hermes-stage-', dir=run_dir))
    try:
        for name, content in policy.items():
            path = stage / name
            _mkdir_private(path.parent)
            _write(path, content)
            _sync_dir(path.parent)
        if payload['environment'].get('SSL_CERT_DIR') == '@snapshot/ca-dir':
            _mkdir_private(stage / 'ca-dir')
        for name, encoded in payload['blobs'].items():
            path = stage / name
            _mkdir_private(path.parent)
            _write(path, bytes.fromhex(encoded))
            _sync_dir(path.parent)
        from climate_monitor.hermes_auth_state import initialize_auth
        initialize_auth(stage, {'schema_version': SCHEMA, 'sha256': digest}, payload['auth'])
        _write(stage / 'manifest.json', raw)
        _write(stage / 'complete', digest.encode())
        _sync_dir(stage)
        os.rename(stage, run_dir / SNAPSHOT)
        _sync_dir(run_dir)
        _sync_dir(run_dir.parent)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'schema_version': SCHEMA, 'sha256': digest}


def _verify_policy(root, policy):
    """Exact private file membership as well as bytes; no import shadow files."""
    actual = set()
    directories = {str(parent) for name in policy for parent in Path(name).parents if str(parent) != '.'}
    total = 0
    entries = 0
    def walk(directory, depth=0):
        nonlocal total, entries
        if str(directory.relative_to(root)) not in directories:
            raise ValueError('unbound policy directory')
        if depth > 32:
            raise ValueError('policy depth limit exceeded')
        _check(directory.lstat(), True, directory=True)
        for name in _directory_names(directory):
            entries += 1
            if entries > MAX_TREE_FILES:
                raise ValueError('policy entry limit exceeded')
            path = directory / name
            if stat.S_ISDIR(path.lstat().st_mode):
                walk(path, depth + 1)
                continue
            member = str(path.relative_to(root))
            if member not in policy or len(actual) >= MAX_TREE_FILES:
                raise ValueError('unbound policy file')
            raw = secure_read(path, private=True)[0]
            total += len(raw)
            if total > MAX_TREE_BYTES or policy[member] != {'size': len(raw), 'sha256': _digest(raw)}:
                raise ValueError('policy bytes changed')
            actual.add(member)
    for name in ('bootstrap', 'policy', 'acquisition'):
        walk(Path(root) / name)
    if actual != set(policy):
        raise ValueError('incomplete policy files')


def load_snapshot(run_dir, reference):
    try:
        if (not isinstance(reference, dict) or set(reference) != {'schema_version', 'sha256'}
                or reference['schema_version'] != SCHEMA):
            raise ValueError()
        root = Path(run_dir) / SNAPSHOT
        _check(root.lstat(), True, directory=True)
        raw, _ = secure_read(root / 'manifest.json', private=True)
        complete, _ = secure_read(root / 'complete', private=True)
        if _digest(raw) != reference['sha256'] or complete.decode() != reference['sha256']:
            raise ValueError()
        payload = json.loads(raw)
        if payload['schema_version'] != SCHEMA:
            raise ValueError()
        if not {'bootstrap/runtime-commitments.zlib', 'bootstrap/runtime_inventory.py', 'bootstrap/launcher.py', 'bootstrap/sitecustomize.py', 'policy/identity.py', 'policy/plugin.json', 'policy/search-plugin.py', 'policy/search-plugin.json', 'acquisition/scripts/acquisition_budget_hook.py'} <= set(payload['policy']):
            raise ValueError()
        _verify_policy(root, payload['policy'])
        from climate_monitor import hermes_reader_runtime, hermes_runtime_inventory
        hermes_reader_runtime.verify(root, payload, lambda p: secure_read(p, private=True)[0], hermes_runtime_inventory)
        if payload['environment_metadata'] != _environment_metadata(payload['environment']):
            raise ValueError()
        if (not isinstance(payload['web_plugins_disabled'], list) or
                any(not isinstance(v, str) or not re.fullmatch(r'web/[A-Za-z0-9_-]+', v) for v in payload['web_plugins_disabled']) or
                len(set(payload['web_plugins_disabled'])) != len(payload['web_plugins_disabled']) or
                not isinstance(payload['web_config'], dict) or
                payload['web_environment_metadata'] != _environment_metadata(payload['web_environment'])):
            raise ValueError()
        from climate_monitor.hermes_runtime_inventory import decode_commitments
        commitments = decode_commitments(secure_read(root / 'bootstrap/runtime-commitments.zlib', private=True)[0])
        interpreter, runtime = _runtime(payload['executable'], commitments=commitments)
        if (runtime != payload['runtime'] or interpreter != payload['interpreter']
                or runtime['package_root']['path'] != payload['package_root']
                or payload['runtime_paths'] != _runtime_paths(interpreter, payload['executable'], payload['package_root'])):
            raise ValueError()
        if payload['environment'].get('SSL_CERT_DIR') == '@snapshot/ca-dir':
            _check((root / 'ca-dir').lstat(), True, directory=True)
        for name, encoded in payload['blobs'].items():
            if secure_read(root / name, private=True)[0] != bytes.fromhex(encoded):
                raise ValueError()
        load_identity(root)
        from climate_monitor.hermes_auth_state import verify_history
        verify_history(root, reference)
        return payload
    except (ValueError, OSError, KeyError, TypeError):
        raise ValueError('Hermes snapshot missing, incomplete or runtime drifted; start a fresh run') from None


def _validate_identity(value):
    if (not isinstance(value, dict) or set(value) != {'provider', 'model', 'source'}
            or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', v)
                   or '://' in v for v in value.values())):
        raise ValueError('invalid sanitized Hermes identity')
    return value


def load_identity(root):
    path = Path(root) / 'effective-identity.json'
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    try:
        return _validate_identity(json.loads(secure_read(path, private=True)[0]))
    except (ValueError, OSError):
        raise ValueError('invalid Hermes identity; start a fresh run') from None


def require_effective_identity(root, expected_source):
    """A successful managed inference must have observed a real API response."""
    _check(Path(root).lstat(), True, directory=True)
    identity = load_identity(root)
    if identity is None or identity['source'] != expected_source:
        raise ValueError('Hermes effective identity missing or inconsistent; start a fresh run')
    return identity


def publish_identity(root, identity):
    """Fsynced temp + exclusive link: a loser can only compare the winner."""
    identity = _validate_identity(identity)
    root = Path(root)
    _check(root.lstat(), True, directory=True)
    fd, temporary = tempfile.mkstemp(prefix='.identity-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(_bytes(identity))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, root / 'effective-identity.json', follow_symlinks=False)
        except FileExistsError:
            if load_identity(root) != identity:
                raise ValueError('Hermes effective identity changed; start a fresh run')
        _sync_dir(root)
    finally:
        os.unlink(temporary)
        _sync_dir(root)
    return identity


IDENTITY_PLUGIN = 'climate-frozen-identity'


def observe_hook(root, source, *, successful, provider=None, model=None, **_ignored):
    """Lifecycle hook; Hermes swallows plugin errors, so terminate on mismatch."""
    value = {'source': source, 'provider': provider, 'model': model}
    try:
        _validate_identity(value)
        if os.environ.get('HERMES_SESSION_SOURCE') != source:
            raise ValueError('Hermes session source changed')
        if successful:
            publish_identity(root, value)
        else:
            prior = load_identity(root)
            if prior is not None and prior != value:
                raise ValueError('Hermes effective identity changed')
    except Exception:
        # Never print the hook payload (contains credentials, URLs and content).
        os._exit(65)


FROZEN_STARTUP = """import os
try:
    if os.environ.get('CLIMATE_ACQUISITION_ATTEMPT'):
        from pathlib import Path
        import acquisition_imports
        acquisition_imports.install(Path(__file__).parent.parent / 'acquisition')
        from climate_monitor.hermes_attempt_policy import verified_binding
        verified_binding()
    from hermes_cli import env_loader
    def frozen_environment(**kwargs):
        return []
    env_loader.load_hermes_dotenv = frozen_environment
    from tools import skills_sync
    def frozen_skills(quiet=False):
        return {'copied': [], 'updated': [], 'skipped': 0, 'user_modified': [],
                'cleaned': [], 'total_bundled': 0, 'optional_provenance_backfilled': [],
                'skipped_opt_out': True}
    skills_sync.sync_skills = frozen_skills
    from hermes_cli import plugins
    def frozen_entrypoints():
        return []
    plugins.discover_entrypoint_manifests = frozen_entrypoints
except BaseException:
    os._exit(65)
"""


def prepare_home(run_dir, reference, home, *, source):
    """Materialize only route/auth and controlled identity hooks, never ambient hooks."""
    payload = load_snapshot(run_dir, reference)
    root = Path(run_dir) / SNAPSHOT
    home = Path(home)
    _mkdir_private(home)
    _check(home.lstat(), True, directory=True)
    def expand(value):
        if isinstance(value, str) and value.startswith('@snapshot/'):
            return str(root / value[len('@snapshot/'):])
        if isinstance(value, dict):
            return {key: expand(child) for key, child in value.items()}
        if isinstance(value, list):
            return [expand(child) for child in value]
        return value
    bootstrap = root / 'bootstrap'
    if source != payload['source']:
        raise ValueError('Hermes session source changed; start a fresh run')
    marker = home / '.no-bundled-skills'
    if os.path.lexists(marker):
        if secure_read(marker, private=True)[0] != b'':
            raise ValueError('immutable Hermes skills opt-out changed')
    else:
        _write(marker, b'')
    config = expand(payload['config'])
    env = expand(payload['environment'])
    config.update({'hooks_auto_accept': True, 'mcp_servers': {},
                   'memory': {'memory_enabled': False, 'user_profile_enabled': False},
                   'plugins': {'enabled': [IDENTITY_PLUGIN]}})
    plugin = home / 'plugins' / IDENTITY_PLUGIN
    _mkdir_private(plugin)
    for target, frozen in (('plugin.yaml', 'policy/plugin.json'), ('__init__.py', 'policy/identity.py')):
        path = plugin / target
        raw, _ = secure_read(root / frozen, private=True)
        if payload['policy'][frozen] != {'size': len(raw), 'sha256': _digest(raw)}:
            raise ValueError('frozen Hermes policy changed; start a fresh run')
        if path.exists():
            if secure_read(path, private=True)[0] != raw:
                raise ValueError('controlled Hermes identity plugin changed')
        else:
            _write(path, raw)
    _sync_dir(plugin)
    _sync_dir(plugin.parent)
    from climate_monitor.hermes_auth_state import prepare_auth
    prepare_auth(home, reference)
    env.update({'HOME': str(home), 'HERMES_HOME': str(home), 'HERMES_ACCEPT_HOOKS': '1',
                'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'HERMES_REDACT_SECRETS': 'true',
                'HERMES_ENABLE_PROJECT_PLUGINS': '0', 'HERMES_SESSION_SOURCE': source,
                'HERMES_MANAGED_DIR': str(root / 'no-managed-overlay'),
                'PYTHONPATH': str(bootstrap) + os.pathsep + payload['package_root']})
    _sync_dir(home)
    return payload, config, env


def verify_identity_runtime(interpreter, environment, home):
    probe = (
        'from hermes_cli.plugins import discover_plugins,get_plugin_manager; '
        'discover_plugins(force=True); '
        f'p=next((p for p in get_plugin_manager().list_plugins() if p["key"]=={IDENTITY_PLUGIN!r}), None); '
        'assert p and p["enabled"] and p["hooks"]==2'
    )
    from climate_monitor.hermes_auth_state import verify_auth
    verify_auth(home)
    from climate_monitor.hermes_runtime_inventory import VERIFY_TIMEOUT
    result = subprocess.run(launch_command(home, '-c', probe), cwd=home, env=environment,
                            capture_output=True, timeout=VERIFY_TIMEOUT)
    verify_auth(home)
    if result.returncode:
        raise ValueError('mandatory Hermes identity hooks unavailable; start a fresh run')


def inference_runtime(run_dir, reference, *, purpose, source):
    """Shared frozen route for managed report and meeting inference (no tools)."""
    home = Path(run_dir) / SNAPSHOT / purpose
    payload, config, environment = prepare_home(run_dir, reference, home, source=source)
    raw = _bytes(config)
    path = home / 'config.yaml'
    if path.exists():
        if secure_read(path, private=True)[0] != raw:
            raise ValueError('immutable Hermes inference configuration changed')
    else:
        _write(path, raw)
    _sync_dir(home)
    _sync_dir(home.parent)
    verify_identity_runtime(payload['interpreter'], environment, home)
    return launch_command(home), environment, home


def _policy_bytes(root, source):
    template, _ = secure_read(Path(__file__).with_name('hermes_frozen_policy.py'))
    prefix = f'from pathlib import Path\nROOT = Path({str(root.absolute())!r})\nSOURCE = {source!r}\n'.encode()
    from climate_monitor.hermes_acquisition_policy import acquisition_policy
    return {**acquisition_policy(), 'bootstrap/sitecustomize.py': FROZEN_STARTUP.encode(),
            'bootstrap/reader_runtime.py': secure_read(Path(__file__).with_name('hermes_reader_runtime.py'))[0],
            'bootstrap/runtime_inventory.py': secure_read(Path(__file__).with_name('hermes_runtime_inventory.py'))[0],
            'bootstrap/launcher.py': secure_read(Path(__file__).with_name('hermes_launcher.py'))[0],
            'policy/identity.py': prefix + template,
            'policy/plugin.json': _bytes({'name': IDENTITY_PLUGIN, 'version': '1.0.0',
                                         'hooks': ['pre_api_request', 'post_api_request']})}


def auth_execution(home, *, enabled=True, require_identity=False):
    from climate_monitor.hermes_auth_state import execution
    return execution(home, enabled=enabled, require_identity=require_identity)


def _credential_contract(config, auth, environment, blobs, runtime, auto_keys):
    """Refuse routes requiring unfrozen SDK/CLI stores; never probe credentials.

    auth.resolve_provider in both releases excludes Copilot and LM Studio from
    auto detection, uses explicit keys/pools/OAuth first, then the AWS SDK chain.
    Explicit SDK routes require self-contained credentials. Arbitrary credential
    commands, workload federation and default/managed identity are unsupported.
    """
    def reject():
        raise ValueError('unsupported implicit Hermes credential input; start a fresh run')
    def usable(value):
        text = str(value or '').strip()
        return len(text) >= 4 and text.lower() not in {
            '*', '**', '***', 'changeme', 'your_api_key', 'your_api_key_here',
            'your-api-key', 'placeholder', 'example', 'dummy', 'null', 'none'}
    def nonempty(*names):
        return any(str(environment.get(name) or '').strip() for name in names)
    if (not isinstance(auth.get('providers', {}), dict) or
            not isinstance(auth.get('credential_pool', {}), dict) or
            not isinstance(auth.get('active_provider') or '', str)):
        reject()
    def contains_token(value):
        if isinstance(value, dict):
            return any((k in {'access_token', 'refresh_token', 'api_key', 'agent_key', 'id_token'} and
                        isinstance(v, str) and v.strip()) or contains_token(v) for k, v in value.items())
        return isinstance(value, list) and any(contains_token(v) for v in value)
    def provider_auth(provider):
        return bool(contains_token((auth.get('providers') or {}).get(provider)) or
                    contains_token((auth.get('credential_pool') or {}).get(provider)))
    def file_bytes(name):
        path = environment.get(name, '')
        if not path.startswith('@snapshot/'):
            reject()
        return bytes.fromhex(blobs[path[len('@snapshot/'):]])
    def bedrock():
        if nonempty('AWS_BEARER_TOKEN_BEDROCK'):
            return
        if (nonempty('AWS_ACCESS_KEY_ID') and nonempty('AWS_SECRET_ACCESS_KEY') and
                not nonempty('AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_CONFIG_FILE')):
            return
        if nonempty('AWS_SHARED_CREDENTIALS_FILE') and not nonempty('AWS_CONFIG_FILE', 'AWS_WEB_IDENTITY_TOKEN_FILE'):
            import configparser
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read_string(file_bytes('AWS_SHARED_CREDENTIALS_FILE').decode())
                profile = environment.get('AWS_DEFAULT_PROFILE') or environment.get('AWS_PROFILE') or 'default'
                entry = dict(parser[profile])
                if (set(entry) <= {'aws_access_key_id', 'aws_secret_access_key', 'aws_session_token', 'region'} and
                        entry.get('aws_access_key_id') and entry.get('aws_secret_access_key')):
                    return
            except (ValueError, KeyError, configparser.Error):
                pass
        reject()
    aliases = {'github-copilot': 'copilot', 'github': 'copilot', 'github-models': 'copilot',
               'google-vertex': 'vertex', 'google-vertex-ai': 'vertex', 'aws-bedrock': 'bedrock',
               'azure': 'azure-foundry', 'azure-openai': 'azure-foundry'}
    # auth.resolve_provider aliases plus runtime_provider's Vertex shortcuts
    # shared by the pinned/current releases. Normalize before security checks.
    for canonical, names in {
        'bedrock': ('aws', 'amazon', 'amazon-bedrock'),
        'vertex': ('vertex-ai', 'gcp-vertex', 'vertexai'),
        'copilot': ('github-model',),
        'copilot-acp': ('github-copilot-acp', 'copilot-acp-agent'),
        'anthropic': ('claude', 'claude-code', 'claude-oauth'),
        'azure-foundry': ('azure-ai-foundry', 'azure-ai'),
        'openai-codex': ('codex', 'openai_codex'),
        'nous': ('nous-portal', 'nousresearch'),
        'minimax-oauth': ('minimax-portal', 'minimax-global', 'minimax_oauth', 'minimax-oauth-io'),
        'qwen-oauth': ('qwen', 'qwen-portal', 'qwen-cli'),
        'xai-oauth': ('x-ai-oauth', 'grok-oauth', 'xai-grok-oauth'),
    }.items():
        aliases.update(dict.fromkeys(names, canonical))
    routes = _reachable_routes(config, environment)
    for route in routes:
        provider = str(route.get('provider') or environment.get('HERMES_INFERENCE_PROVIDER') or 'auto').strip().lower()
        provider = aliases.get(provider, provider)
        applicable = [route, *_custom_entries(config, str(route.get('provider') or provider), route)]
        for entry in applicable:
            _check_route_credentials(entry, environment)
        if provider == 'auto':
            # Endpoint intent resolves a custom/aggregator route before auth auto.
            if route.get('base_url') or route.get('api_key'):
                continue
            if any(usable(environment.get(key)) for key in auto_keys):
                continue
            # OAuth status/pool usability can expire. Any configured SDK
            # fallback must also be self-contained; never retain a command or
            # external profile merely because the initial OAuth store exists.
            if nonempty('AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_CONFIG_FILE',
                        'AWS_SHARED_CREDENTIALS_FILE', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
                        'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'AWS_WEB_IDENTITY_TOKEN_FILE'):
                bedrock()
            active = auth.get('active_provider')
            if contains_token((auth.get('credential_pool') or {}).get('openrouter')):
                continue
            if active in {'anthropic', 'openai-codex', 'nous', 'minimax-oauth', 'xai-oauth', 'qwen-oauth'} and provider_auth(active):
                provider = active
            elif any(Path(name).name == 'bedrock_adapter.py' for name in runtime):
                bedrock()
                continue
            else:
                continue  # no SDK adapter in this bound runtime
        if provider == 'copilot':
            # Pinned/current resolve_copilot_token refuses CLI fallback whenever
            # any explicit token variable is set, including invalid token values.
            if not nonempty('COPILOT_GITHUB_TOKEN', 'GH_TOKEN', 'GITHUB_TOKEN'):
                reject()
        elif provider in ('copilot-acp',):
            reject()  # separate CLI credential/runtime state is outside snapshot
        elif provider == 'bedrock':
            bedrock()
        elif provider == 'vertex':
            name = next((n for n in ('VERTEX_CREDENTIALS_PATH', 'GOOGLE_APPLICATION_CREDENTIALS') if environment.get(n)), None)
            if name is None:
                reject()
            try:
                credential = json.loads(file_bytes(name))
                if not isinstance(credential, dict) or credential.get('type') != 'service_account' or not all(
                        isinstance(credential.get(k), str) and credential[k]
                        for k in ('client_email', 'private_key', 'token_uri')):
                    reject()
            except (ValueError, TypeError):
                reject()
        elif provider == 'azure-foundry':
            if not route.get('api_key') and not nonempty('AZURE_FOUNDRY_API_KEY', 'AZURE_OPENAI_API_KEY', 'AZURE_API_KEY'):
                reject()
        elif provider in ('anthropic', 'openai-codex', 'nous', 'minimax-oauth', 'xai-oauth', 'qwen-oauth'):
            if any(Path(name).name == 'auth.py' for name in runtime):
                explicit = route.get('api_key') or (provider == 'anthropic' and nonempty(
                    'ANTHROPIC_API_KEY', 'ANTHROPIC_TOKEN', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN'))
                if not explicit and not provider_auth(provider):
                    reject()


def _effective_fallbacks(config):
    # hermes_cli.fallback_config.get_fallback_chain: skip incomplete entries,
    # modern order first, then legacy, first identical route wins.
    result, seen = [], set()
    for key in ('fallback_providers', 'fallback_model'):
        entries = config.get(key)
        entries = [entries] if isinstance(entries, dict) else entries if isinstance(entries, list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            provider, model = str(entry.get('provider') or '').strip(), str(entry.get('model') or '').strip()
            if not provider or not model:
                continue
            base = entry.get('base_url')
            base = base.strip().rstrip('/') if isinstance(base, str) else ''
            identity = provider.lower(), model.lower(), base.lower()
            if identity not in seen:
                seen.add(identity)
                result.append(dict(entry, provider=provider, model=model))
    return result


def _custom_entries(config, provider, route):
    # providers.custom_provider_aliases in both supported releases. Check every
    # matching entry, including legacy names and endpoint matches, before Hermes
    # can select one. Never evaluate token commands.
    def identities(*values):
        result = set()
        for value in values:
            raw = str(value or '').strip().lower()
            if not raw:
                continue
            normalized = raw.replace(' ', '-')
            result.update((raw, normalized, normalized if normalized.startswith('custom:') else 'custom:' + normalized))
            if normalized.startswith('custom:'):
                result.update((normalized.split(':', 1)[1], 'custom:' + normalized))
        return result
    requested = provider.strip().lower().replace(' ', '-')
    entries = []
    registry = config.get('providers', {})
    if isinstance(registry, dict):
        entries.extend((name, entry) for name, entry in registry.items() if isinstance(entry, dict))
    legacy = config.get('custom_providers', [])
    if isinstance(legacy, list):
        entries.extend((entry.get('provider_key'), entry) for entry in legacy if isinstance(entry, dict))
    def endpoint(value):
        return str(value or '').strip().rstrip('/').lower()
    base = endpoint(route.get('base_url') or route.get('api_base'))
    return [entry for key, entry in entries if requested in identities(key, entry.get('name')) or
            (requested == 'custom' and not base) or
            (base and base == endpoint(entry.get('api') or entry.get('url') or entry.get('base_url')))]


def _check_route_credentials(value, environment):
    if isinstance(value, dict):
        for key, child in value.items():
            if ((key in {'key_cmd', 'credential_process', 'token_command', 'credential_command'} and child) or
                    (key in {'auth_mode', 'auth_type'} and str(child).strip().lower() in
                     {'entra_id', 'managed_identity', 'default', 'command', 'adc', 'default_credentials'})):
                raise ValueError('unsupported implicit Hermes credential input; start a fresh run')
            if key in {'key_env', 'api_key_env', 'api_key_env_var'} and child:
                if not isinstance(child, str) or not environment.get(child.strip(), '').strip():
                    raise ValueError('unsupported missing Hermes credential input; start a fresh run')
            _check_route_credentials(child, environment)
    elif isinstance(value, list):
        for child in value:
            _check_route_credentials(child, environment)


def _reachable_routes(config, environment):
    model = config.get('model')
    primary = dict(model) if isinstance(model, dict) else {'default': model}
    primary['model'] = environment.get('HERMES_INFERENCE_MODEL', '').strip() or primary.get('default') or primary.get('model')
    routes = [primary, *_effective_fallbacks(config)]
    def scan(value):
        if isinstance(value, dict):
            if any(key in value for key in ('provider', 'model', 'default')):
                routes.append(value)
            for child in value.values():
                scan(child)
        elif isinstance(value, list):
            for child in value:
                scan(child)
    scan(config.get('smart_model_routing', {}))
    # model_switch._load_direct_aliases: root definitions win over model.aliases;
    # names are case insensitive. Keep original maps unchanged for child loading.
    aliases = {name.strip().lower(): dict(entry, provider=entry.get('provider', 'custom'))
               for name, entry in config.get('model_aliases', {}).items()}
    simple = primary.get('aliases', {})
    for name, entry in simple.items() if isinstance(simple, dict) else []:
        if isinstance(entry, str):
            provider, actual = entry.split('/', 1) if '/' in entry else (primary.get('provider', ''), entry)
            entry = {'model': actual.strip(), 'provider': provider.strip()}
        if isinstance(entry, dict):
            aliases.setdefault(name.strip().lower(), dict(entry, provider=entry.get('provider') or primary.get('provider') or 'custom'))
    explicit = environment.get('HERMES_INFERENCE_MODEL', '').strip().lower()
    if explicit in aliases:
        # oneshot resolves an explicit/env model alias before the configured
        # provider; the displaced provider must not trigger an unrelated SDK.
        routes[0] = aliases[explicit]
    seen = set()
    for route in routes:
        name = str(route.get('model') or route.get('default') or '').strip().lower()
        if name in aliases and name not in seen:
            seen.add(name)
            routes.append(aliases[name])
    return routes
