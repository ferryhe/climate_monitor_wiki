"""Non-executing web-search input inventory for Hermes 0.20.5 and 0.21.3.

Only web providers/dispatch and their shared credential helpers are inspected.
Generic env-name forwarding functions are pinned by AST digest: a changed or
new dynamic declaration requires an audited contract, never a guessed name.
"""
import ast
import hashlib
import re
from pathlib import Path

SOURCES = frozenset({
    'tools/web_tools.py', 'tools/managed_tool_gateway.py',
    'tools/tool_backend_helpers.py', 'tools/xai_http.py',
    'agent/web_search_registry.py', 'agent/web_search_provider.py',
})
ENV_CALLS = frozenset({'getenv', 'get_env_value', 'get_provider_env', 'provider_env',
    'get_secret', '_env', '_env_value', '_has_env', '_scoped_credential',
    '_dotenv_value', 'env_getter', 'resolve_provider_secret'})

# Full function ASTs inspected at fcbd1076 (0.20.5) and 1ad89ac0 (0.21.3).
# Also audited at Docker pin 5538bd1f under Python 3.12 (empty type_params AST field).
# These forward literal profile/call-site names or the web vendor gateway name.
DYNAMIC_HELPERS = {
    "tools/xai_http.py:get_env_value": [
        "dc5f210a69fed26b9217594e0b54e9f0c4753bbfa072a62d7d813b270e2153a1",
        "ab2e4e36abde4ae43a993797b64396e20041e5723813b19ef7f528c4a4282156"
    ],
    "agent/web_search_provider.py:get_provider_env": [
        "5f85f8a3beea7a24126b872337409a60ce7e2c36b6f20c462a39aeefd3b9d706",
        "7e032806cafd2b8227ddbb5fb3ab3a5c8ae550dfe4e0c87d2253f8856480341f",
        "cd44de6c536d89d7fa6edc57a78329c2194175e08199a4cbc8c312cc6b29e5bf"
    ],
    "plugins/web/_common.py:cached_sdk_client": [
        "b7a0428a5ea90b6a7027a4969b24a30129f4c7fafda22967e834c1a28a86f6e7"
    ],
    "plugins/web/_common.py:is_available": [
        "3345c200784cc5f4d910baba1ffa36269f68c2f7cb9217717f098054f960aa62"
    ],
    "plugins/web/_common.py:provider_env": [
        "5a33afb5b38c3790dfb1532e17063b0fb85c6d00b37062b3b298da6561fcd263"
    ],
    "plugins/web/firecrawl/provider.py:_env": [
        "d8169be81ad14f7fd034b87704fa47e88ba2c54f8a26d1c5952305f7e288da51"
    ],
    "tools/managed_tool_gateway.py:build_vendor_gateway_url": [
        "6246e435806165a3d22dd5438c7444ed5bf206d16de1ddc250bca1e0e3b29bf2",
        "3d57b05adfd6454a6b49109add5b466a3a1b395bbe6b924095289e4e44caee8c",
        "da2a71ff0681e017cf3d2ee99f8744f7aa69834819cf422ac6685603c39a0403"
    ],
    "tools/web_tools.py:_env_value": [
        "f4c76d716dd6b3280105c9dbaf8013e59fd6074e7853903ee24fbb7bc8cb89a7",
        "55d4a5970ed8c05d96ae16ef84f1408b7a398326534ed66cd1ef867b86b3eb00",
        "abb1cab77267e3e9262c4765331ab534ffb7da34316061ba51a0d6786b9ecaed"
    ],
    "tools/web_tools.py:_has_env": [
        "d2cf256b7050751854d58ac70a2fa1d89ba09601427a9692e369ea94e67b3557",
        "1c363b932da8e3a3e68b13f6f55b687902c7b62442b51839a528debfeece2f25"
    ],
    "tools/web_tools.py:_rescue_eligible": [
        "d29371a1144b098e189e8da8b4a70db602ee6a3756f5d7547847b6619f4a644a",
        "de395c54eb835046d76c9bc9d27c30e22c1c4449f6a653229bbff06cc6bc9d20"
    ],
    "plugins/web/_common.py:setup_schema": [
        "7f1362d4ba6246c4345753c85ea2d9de347217192b8486a1fe580c976a6dbc32"
    ],
    "plugins/web/_common.py:keyless_variant_schema": [
        "57510af75826e2e661fcec0a693945039a17043fd3580f2e562faa08f626415a"
    ]
}


def web_environment_names(package, runtime, inputs):
    from climate_monitor.hermes_identity import secure_read, MAX_PRIVATE_BYTES
    names = set()
    for filename, expected in runtime.items():
        path = Path(filename)
        if path.suffix != '.py' or not path.is_relative_to(package):
            continue
        relative = path.relative_to(package).as_posix()
        if relative not in SOURCES and not relative.startswith('plugins/web/'):
            continue
        try:
            raw, metadata = secure_read(path)
            if metadata != expected or len(raw) > MAX_PRIVATE_BYTES:
                raise ValueError()
            inputs[filename] = metadata
            tree = ast.parse(raw)
            if relative == 'tools/tool_backend_helpers.py':
                # Only web-selection/account helpers, never browser/voice/image credentials.
                tree.body = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) or
                             n.name in {'managed_nous_tools_enabled', 'nous_tool_gateway_unavailable_message',
                                        '_raw_section', 'read_selection', 'selection_exists', 'selection_error', 'prefers_gateway'}]
            nodes = list(ast.walk(tree))
            if len(nodes) > 100000:
                raise ValueError()
            parents = {child: node for node in nodes for child in ast.iter_child_nodes(node)}
            aliases = {alias.asname: alias.name for node in nodes if isinstance(node, ast.ImportFrom)
                       for alias in node.names if alias.asname}
            def audited_forwarder(node):
                function = node
                while function in parents and not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    function = parents[function]
                key = relative + ':' + getattr(function, 'name', '')
                digest = hashlib.sha256(ast.dump(function, include_attributes=False).encode()).hexdigest()
                if digest not in DYNAMIC_HELPERS.get(key, ()):
                    raise ValueError()
            for node in nodes:
                if relative.startswith('plugins/web/') and isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value in {'key', 'env_vars'}:
                            valid = (isinstance(value, ast.Constant) and isinstance(value.value, str)) if key.value == 'key' else isinstance(value, (ast.List, ast.Tuple))
                            if not valid:
                                audited_forwarder(node)
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if any(isinstance(t, ast.Name) and t.id == 'KEY_ENV' for t in targets):
                        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                            raise ValueError()
                    if node.value is not None and ast.unparse(node.value).endswith('.environ'):
                        raise ValueError()
                # Literal registry names, KEY_ENV fields, helper call arguments,
                # and fallback maps are all within this web-only source scope.
                ancestor = node
                while ancestor in parents and not isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    ancestor = parents[ancestor]
                profile_literal = relative.startswith('plugins/web/') or (relative == 'tools/web_tools.py' and
                                  getattr(ancestor, 'name', '') == '_rescue_eligible')
                if profile_literal and isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(
                        r'[A-Z][A-Z0-9]*_[A-Z_0-9]+', node.value):
                    names.add(node.value)
                reference = None
                if isinstance(node, ast.Call):
                    name = ast.unparse(node.func).split('.')[-1]
                    name = aliases.get(name, name)
                    positions = {'cached_sdk_client': 1, 'setup_schema': 3, 'keyless_variant_schema': 1}
                    if name in positions and relative.startswith('plugins/web/'):
                        supplied = ([node.args[positions[name]]] if len(node.args) > positions[name] else []) + [k.value for k in node.keywords if k.arg in {'env_var', 'key_env'}]
                        for value in supplied:
                            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                                audited_forwarder(node)
                    if name in {'build_vendor_gateway_url', 'resolve_managed_tool_gateway'} and relative.startswith('plugins/web/'):
                        if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                            raise ValueError()
                        names.add(node.args[0].value.upper().replace('-', '_') + '_GATEWAY_URL')
                    if name in ENV_CALLS or ast.unparse(node.func).endswith('.environ.get'):
                        if not node.args:
                            raise ValueError()
                        reference = node.args[0]
                elif isinstance(node, ast.Subscript) and ast.unparse(node.value).endswith('.environ'):
                    reference = node.slice
                if reference is not None:
                    if isinstance(reference, ast.Constant) and isinstance(reference.value, str):
                        if not re.fullmatch(r'[A-Z][A-Z_0-9]*', reference.value):
                            raise ValueError()
                        names.add(reference.value)
                    else:
                        audited_forwarder(node)
            if relative == 'tools/managed_tool_gateway.py':
                for statement in tree.body:
                    if isinstance(statement, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_MANAGED_GATEWAY_VENDOR' for t in statement.targets):
                        vendor = ast.literal_eval(statement.value)
                        if not isinstance(vendor, str):
                            raise ValueError()
                        names.add(vendor.upper().replace('-', '_') + '_GATEWAY_URL')
        except (ValueError, SyntaxError, TypeError, OSError):
            raise ValueError('unsupported Hermes web input; start a fresh run') from None
    # A subprocess testing escape hatch is never an execution input.
    names.discard('HERMES_DDGS_ALLOW_TEST_HOOKS')
    return names


def web_plugin_denials(config, package, runtime, inputs):
    """Preserve only bundled web deny keys (Hermes matches key OR manifest name)."""
    from climate_monitor.hermes_identity import secure_read, _check_document, IDENTITY_PLUGIN
    import yaml
    try:
        plugins = config.get('plugins', {})
        if not isinstance(plugins, dict):
            raise ValueError()
        disabled, enabled = plugins.get('disabled', []), plugins.get('enabled', [])
        for values in (disabled, enabled):
            if (not isinstance(values, list) or len(values) != len(set(values)) or
                    any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', v) for v in values)):
                raise ValueError()
        aliases = {}
        canonical = set()
        for filename, expected in runtime.items():
            path = Path(filename)
            if not path.is_relative_to(package / 'plugins/web') or path.name not in {'plugin.yaml', 'plugin.yml'}:
                continue
            raw, metadata = secure_read(path)
            if metadata != expected:
                raise ValueError()
            inputs[filename] = metadata
            manifest = yaml.safe_load(raw)
            _check_document(manifest)
            if (not isinstance(manifest, dict) or manifest.get('kind') != 'backend' or
                    not isinstance(manifest.get('name'), str) or
                    not isinstance(manifest.get('provides_web_providers'), list) or
                    not manifest['provides_web_providers'] or
                    any(not isinstance(p, str) or not p for p in manifest['provides_web_providers'])):
                raise ValueError()
            key = path.parent.relative_to(package / 'plugins').as_posix()
            if key in canonical:
                raise ValueError()
            canonical.add(key)
            for alias in {key, manifest['name']}:
                if alias in aliases and aliases[alias] != key:
                    raise ValueError()
                aliases[alias] = key
        result = []
        for value in disabled:
            if value in {IDENTITY_PLUGIN, 'climate-acquisition-search-identity'}:
                raise ValueError()
            if value in aliases:
                key = aliases[value]
                if key in result or any(aliases.get(v) == key for v in enabled):
                    raise ValueError()
                result.append(key)
            elif value.startswith(('web/', 'web-')):
                raise ValueError()
        return sorted(result)
    except (ValueError, TypeError, KeyError, OSError, yaml.YAMLError):
        raise ValueError('unsupported Hermes web plugin deny input; start a fresh run') from None
