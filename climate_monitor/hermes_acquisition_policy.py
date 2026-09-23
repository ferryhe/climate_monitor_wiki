"""Build the narrow acquisition code bundle before publishing a snapshot.

Definitions have one authority: the normal application modules. Selection copies
whole AST definitions and their referenced module globals, never executes them.
The resulting modules have no import of the live checkout or package initializers.
"""
from __future__ import annotations

import ast
from functools import lru_cache
import sys
from pathlib import Path

# Explicit module boundary. Adding an application import outside this closure is
# a publication error, not an implicit dependency on the worker checkout.
MODULES = {
    'scripts/run_agent_acquisition.py': ('_stage_candidate_receipt', '_finalize_candidate_receipt'),
    'climate_monitor/hermes_acquisition_hooks.py': ('transform_search_tool_result', 'SEARCH_IDENTITY_PLUGIN_ID'),
    'climate_monitor/management.py': ('_atomic_write',),
    'climate_registry/acquisition.py': ('PublicationDatePolicy',),
    'climate_monitor/web_listening_adapter.py': ('_url_allowed',),
    'climate_monitor/article_content_adapter.py': ('fetch_article_content',),
    'climate_monitor/hermes_identity.py': ('secure_read',),
    'climate_monitor/request_budget.py': None,
    'climate_monitor/models.py': None,
    'climate_monitor/dedupe.py': None,
    'climate_monitor/hermes_attempt_policy.py': None,
    'climate_monitor/hermes_reader_runtime.py': None,
    'climate_monitor/hermes_runtime_inventory.py': ('inventory', 'decode_commitments', 'open_file', 'check', 'signature'),
    'scripts/acquisition_budget_hook.py': None,
}


def validate_dependency_source(raw, *, application=False):
    """Conservative, non-executing contract for optional Python dependencies.

    Dynamic loaders, interpreter namespace mutation and native code cannot be
    reduced to this literal import closure. Reject rather than run a finder.
    """
    tree = ast.parse(raw)
    forbidden_modules = {'importlib', 'runpy', 'ctypes', 'cffi', 'marshal', 'pickle', 'builtins', 'site', 'pkgutil', 'imp',
                         '_imp', '_ctypes', '_frozen_importlib', '_frozen_importlib_external'}
    forbidden_names = {'__import__', 'eval', 'exec', 'compile', 'globals', 'locals',
                       'setattr', 'delattr', '__builtins__', '__path__', '__loader__', '__spec__'}
    if application:
        forbidden_names -= {'getattr', 'setattr', 'delattr'}
    sys_names = {'sys'}
    module_names = {'sys'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names.update(a.asname or a.name.split('.')[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_names.update(a.asname or a.name for a in node.names)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            sys_names.update(a.asname or a.name for a in node.names if a.name == 'sys')
    forbidden_attributes = {'find_spec', 'PathFinder', 'module_from_spec', 'exec_module',
                            'load_module', 'spec_from_file_location', 'SourceFileLoader',
                            '__loader__', '__spec__', '__path__', 'modules', 'meta_path',
                            'path_hooks', 'path_importer_cache', 'extend_path', '__dict__', '__class__', '__globals__', '__getattribute__'}
    for node in ast.walk(tree):
        if not application and isinstance(node, ast.Name) and node.id == 'getattr':
            call = parents.get(node)
            if isinstance(call, ast.Call) and call.args and isinstance(call.args[0], ast.Attribute):
                base = call.args[0]
                while isinstance(base, ast.Attribute):
                    base = base.value
                if isinstance(base, ast.Name) and base.id in module_names:
                    raise ValueError('unsupported acquisition dependency loader')
            if (not isinstance(call, ast.Call) or call.func is not node or len(call.args) not in (2, 3)
                    or not isinstance(call.args[0], (ast.Name, ast.Attribute))
                    or (isinstance(call.args[0], ast.Name) and call.args[0].id in module_names
                        and not (call.args[0].id in {'os', 'stat'} and isinstance(call.args[1], ast.Constant)
                                 and isinstance(call.args[1].value, str) and call.args[1].value.isupper()))):
                raise ValueError('unsupported acquisition dependency loader')
        if not application and isinstance(node, ast.Constant) and isinstance(node.value, str) and (
                node.value in forbidden_attributes | {'__dict__', '__builtins__', '__class__', '__globals__', '__getattribute__'}):
            raise ValueError('unsupported acquisition dependency loader')
        if isinstance(node, ast.Import) and any(a.name.split('.')[0] in forbidden_modules for a in node.names):
            raise ValueError('unsupported acquisition dependency loader')
        if (isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] in forbidden_modules
                and not (node.module == 'importlib.metadata' and all(a.name == 'version' for a in node.names))):
            raise ValueError('unsupported acquisition dependency loader')
        if isinstance(node, ast.ImportFrom) and node.module == 'sys' and any(a.name in {'path', 'modules', 'meta_path', 'path_hooks', 'path_importer_cache'} for a in node.names):
            raise ValueError('unsupported acquisition dependency loader')
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            call = parents.get(node)
            # Exact pinned reader artifact hashing, not a dynamic loader.
            if (node.id == '__import__' and isinstance(call, ast.Call) and call.func is node
                    and len(call.args) == 1 and not call.keywords and isinstance(call.args[0], ast.Constant)
                    and call.args[0].value == 'hashlib'):
                continue
            raise ValueError('unsupported acquisition dependency loader')
        if isinstance(node, ast.Attribute) and (node.attr in forbidden_attributes or
                (node.attr == 'path' and isinstance(node.value, ast.Name) and node.value.id in sys_names)):
            raise ValueError('unsupported acquisition dependency loader')
    return tree


@lru_cache(maxsize=16)
def select_definitions(raw, names):
    """Copy a bounded, explicit module-global closure without importing code."""
    tree = ast.parse(raw)
    definitions = {}
    imports = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in definitions or node.name in imports:
                raise ValueError('ambiguous acquisition policy definition')
            definitions[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if not isinstance(target, ast.Name):
                    raise ValueError('unsupported acquisition policy definition')
                if target.id in definitions or target.id in imports:
                    raise ValueError('ambiguous acquisition policy definition')
                definitions[target.id] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imports[alias.asname or (alias.name.split('.')[0] if isinstance(node, ast.Import) else alias.name)] = node
    needed = set()
    pending = list(names)
    while pending:
        name = pending.pop()
        if name in needed:
            continue
        needed.add(name)
        if name in definitions:
            pending.extend(n.id for n in ast.walk(definitions[name]) if isinstance(n, ast.Name)
                           and isinstance(n.ctx, ast.Load) and (n.id in definitions or n.id in imports))
        elif name not in imports:
            raise ValueError('missing acquisition policy definition')
    for node in tree.body:
        if isinstance(node, (ast.Delete, ast.AugAssign)) and any(
                isinstance(n, ast.Name) and n.id in needed for n in ast.walk(node)):
            raise ValueError('unsupported acquisition policy global mutation')
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            aliases = [a for a in node.names if (a.asname or (a.name.split('.')[0] if isinstance(node, ast.Import) else a.name)) in needed]
            if isinstance(node, ast.ImportFrom) and node.module == '__future__':
                aliases = node.names
            if aliases:
                selected.append(ast.Import(names=aliases) if isinstance(node, ast.Import) else
                                ast.ImportFrom(module=node.module, names=aliases, level=node.level))
        elif any(definitions.get(name) is node for name in needed):
            selected.append(node)
    return (ast.unparse(ast.Module(body=selected, type_ignores=[])) + '\n').encode()


def acquisition_policy():
    from climate_monitor.hermes_identity import _read_application_source, MAX_TREE_BYTES, MAX_TREE_FILES, MAX_PRIVATE_BYTES
    root = Path(__file__).absolute().parents[1]
    sources = {}
    source_bytes = 0
    for name in MODULES:
        raw = _read_application_source(root / name)[0]
        source_bytes += len(raw)
        if len(raw) > MAX_PRIVATE_BYTES or source_bytes > MAX_TREE_BYTES:
            raise ValueError('acquisition source limit exceeded')
        sources[name] = raw
    selected = {name: set(names) if names is not None else None for name, names in MODULES.items()}
    result = {}
    while True:
        changed = False
        for name, names in selected.items():
            raw = sources[name] if names is None else select_definitions(sources[name], tuple(sorted(names)))
            result['acquisition/' + name] = raw
            module = name.removesuffix('.py').replace('/', '.')
            for node in ast.walk(ast.parse(raw)):
                if not isinstance(node, ast.ImportFrom):
                    continue
                target = node.module or ''
                if node.level:
                    target = '.'.join(module.split('.')[:-node.level] + ([target] if target else []))
                if target.split('.')[0] not in {'climate_monitor', 'climate_registry', 'scripts'}:
                    continue
                key = target.replace('.', '/') + '.py'
                if key not in selected or any(a.name == '*' for a in node.names):
                    raise ValueError('unbound acquisition application dependency')
                if selected[key] is not None:
                    required = {a.name for a in node.names} - selected[key]
                    if required:
                        selected[key].update(required)
                        changed = True
        if not changed:
            break
    for package in ('climate_monitor', 'climate_registry', 'scripts'):
        result[f'acquisition/{package}/__init__.py'] = b'# Private acquisition namespace; no application entrypoint side effects.\n'
    result['bootstrap/acquisition_imports.py'] = _read_application_source(root / 'climate_monitor/hermes_acquisition_bootstrap.py')[0]
    result['policy/search-plugin.py'] = _read_application_source(root / 'climate_monitor/hermes_search_policy.py')[0]
    import json
    constants = {}
    for node in ast.parse(sources['climate_monitor/request_budget.py']).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in {
                'AGENT_PROTOCOL_VERSION', 'V2_AGENT_PROTOCOL_VERSION', 'PROVIDER_NATIVE_SEARCH_POLICY', 'CANDIDATE_RECEIPT_POLICY'}:
            constants[node.targets[0].id] = ast.literal_eval(node.value)
    v2 = {'version': constants['V2_AGENT_PROTOCOL_VERSION'], 'search_policy': constants['PROVIDER_NATIVE_SEARCH_POLICY']}
    v3 = {'version': constants['AGENT_PROTOCOL_VERSION'], 'search_policy': constants['PROVIDER_NATIVE_SEARCH_POLICY'],
          'candidate_policy': constants['CANDIDATE_RECEIPT_POLICY']}
    result['policy/acquisition.json'] = json.dumps({'native_protocols': [v2, v3], 'candidate_protocol': v3}, sort_keys=True).encode()
    result['policy/search-plugin.json'] = b'{"hooks":["transform_tool_result"],"name":"climate-acquisition-search-identity","version":"1.0.0"}'
    # Reject accidental growth of the executable application import boundary.
    available = {name[len('acquisition/'):].removesuffix('.py').replace('/', '.') for name in result if name.startswith('acquisition/')}
    for name, raw in result.items():
        if not name.endswith('.py'):
            continue
        if name.startswith('acquisition/') or name == 'policy/search-plugin.py':
            validate_dependency_source(raw, application=True)
        module = name.removeprefix('acquisition/').removesuffix('.py').replace('/', '.')
        for node in ast.walk(ast.parse(raw)):
            targets = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                target = node.module or ''
                if node.level:
                    target = '.'.join(module.split('.')[:-node.level] + ([target] if target else []))
                targets = [target]
            for target in targets:
                top = target.split('.')[0]
                if top in {'climate_monitor', 'climate_registry', 'scripts'}:
                    if target not in available:
                        raise ValueError('unbound acquisition application dependency')
                elif top not in sys.stdlib_module_names and top != 'web_listening':
                    if not (module == 'scripts.acquisition_budget_hook' and top in {'agent', 'hermes_cli', 'model_tools'}):
                        raise ValueError('unbound acquisition external dependency')
    if len(result) > MAX_TREE_FILES or sum(map(len, result.values())) > MAX_TREE_BYTES:
        raise ValueError('acquisition policy limit exceeded')
    return result


def provider_dependency_policy(interpreter, package_root=None):
    """Copy the governed reader's installed source/data import closure.

    Resolve files without importing packages or executing editable finders. An
    absent optional reader is an explicit frozen ImportError, never an ambient
    fallback. Unsupported/oversized closures fail publication.
    """
    import json
    import sys
    import csv
    import io
    from email.parser import BytesParser
    from urllib.parse import unquote, urlparse
    from climate_monitor.hermes_identity import secure_read, _directory_names, _check, MAX_TREE_BYTES, MAX_TREE_FILES
    import stat
    prefix = Path(interpreter).absolute().parent.parent
    sites = sorted(prefix.glob('lib/python*/site-packages'))
    search = ([Path(package_root)] if package_root is not None else []) + list(sites)
    metadata = []
    for site in sites:
        for name in _directory_names(site):
            path = site / name
            if name.endswith('.pth'):
                for line in secure_read(path)[0].decode().splitlines():
                    if line.strip() and not line.startswith(('#', 'import ')):
                        candidate = site / line.strip()
                        if candidate.is_dir():
                            search.append(candidate.absolute())
            if name.endswith('.dist-info'):
                top = path / 'top_level.txt'
                names = secure_read(top)[0].decode().splitlines() if top.exists() else []
                record = path / 'RECORD'
                if record.exists():
                    for row in csv.reader(io.StringIO(secure_read(record)[0].decode())):
                        if row:
                            candidate = row[0].split('/')[0].split('.')[0]
                            if candidate.isidentifier():
                                names.append(candidate)
                info = path / 'METADATA'
                if info.exists():
                    distribution = BytesParser().parsebytes(secure_read(info)[0]).get('Name', '')
                    if distribution.lower().replace('-', '_') == 'web_listening':
                        names.append('web_listening')
                direct = path / 'direct_url.json'
                if direct.exists():
                    value = json.loads(secure_read(direct)[0])
                    if value.get('dir_info', {}).get('editable'):
                        url = urlparse(value.get('url', ''))
                        if url.scheme != 'file' or url.netloc:
                            raise ValueError('unsupported acquisition dependency source')
                        editable = Path(unquote(url.path))
                        search.extend([editable / 'src', editable])
                metadata.append((path, names))
    result = {}
    completed = set()
    pending = ['web_listening']
    total = 0
    entries = 0
    def add(name, path=None, raw=None):
        nonlocal total
        if name.endswith(('.so', '.pyd', '.dll', '.dylib')):
            raise ValueError('unsupported native acquisition dependency')
        raw = secure_read(path)[0] if raw is None else raw
        total += len(raw)
        if total > MAX_TREE_BYTES or len(result) >= MAX_TREE_FILES:
            raise ValueError('acquisition dependency limit exceeded')
        result['acquisition/' + name] = raw
        if name.endswith('.py'):
            for node in ast.walk(validate_dependency_source(raw)):
                if ((isinstance(node, ast.ImportFrom) and (node.module or '').startswith('web_listening.interfaces'))
                        or (isinstance(node, ast.Import) and any(a.name.startswith('web_listening.interfaces') for a in node.names))):
                    raise ValueError('unsupported acquisition server dependency')
                if isinstance(node, ast.Import):
                    pending.extend(a.name.split('.')[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    pending.append(node.module.split('.')[0])
    def walk(path, name, depth=0):
        nonlocal entries
        if depth > 32:
            raise ValueError('acquisition dependency depth limit exceeded')
        for child in _directory_names(path):
            entries += 1
            if entries > MAX_TREE_FILES:
                raise ValueError('acquisition dependency entry limit exceeded')
            if child in {'__pycache__', '.git', 'tests', 'test', 'docs'} or child.endswith(('.pyc', '.pyo')):
                continue
            # The governed in-process reader does not run its optional CLI/MCP/HTTP servers.
            if name == 'web_listening' and child == 'interfaces':
                continue
            source = path / child
            st = source.lstat()
            if stat.S_ISDIR(st.st_mode):
                _check(st, False, directory=True)
                walk(source, name + '/' + child, depth + 1)
            else:
                add(name + '/' + child, source)
    while pending:
        name = pending.pop()
        if name in completed or name in sys.stdlib_module_names or name in {'climate_monitor', 'climate_registry', 'scripts'}:
            continue
        if not name.isidentifier():
            raise ValueError('unsupported acquisition dependency name')
        completed.add(name)
        found = None
        for parent in search:
            if (parent / name).is_dir():
                found = parent / name
                walk(found, name)
                for libs in sorted(parent.glob(name + '*.libs')):
                    walk(libs, libs.name)
                break
            if (parent / (name + '.py')).exists():
                found = parent / (name + '.py'); add(name + '.py', found); break
            native = list(parent.glob(name + '.*.so'))
            if len(native) > 1:
                raise ValueError('ambiguous acquisition dependency')
            if native:
                found = native[0]; add(found.name, found); break
        if found is None:
            if name == 'web_listening' and any(name in names for _, names in metadata):
                raise ValueError('unsupported acquisition dependency source')
            add(name + '/__init__.py', raw=b'raise ModuleNotFoundError("optional frozen acquisition dependency unavailable")\n')
        for directory, names in metadata:
            if name in names:
                walk(directory, directory.name)
    return result
