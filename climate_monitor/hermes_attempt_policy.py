"""Stdlib-only verifier for the private immutable acquisition attempt seal."""
import hashlib
import json
import os
from pathlib import Path

from climate_monitor.hermes_identity import secure_read, _verify_policy


def verified_binding(expected_path=None):
    try:
        seal_path = Path(os.environ['CLIMATE_ACQUISITION_ATTEMPT'])
        root = Path(__file__).resolve().parents[2]
        if (seal_path.parent.parent != root or seal_path.parent != Path(os.environ['HERMES_HOME'])
                or seal_path.name != 'attempt-policy.json' or not seal_path.parent.name.startswith('attempt-')):
            raise ValueError()
        seal = json.loads(secure_read(seal_path, private=True)[0])
        if (set(seal) != {'schema_version', 'snapshot', 'binding_path', 'binding_sha256', 'run_id', 'attempt', 'configuration_sha256'}
                or seal['schema_version'] != 'climate-acquisition-attempt-policy.v1'):
            raise ValueError()
        raw = secure_read(root / 'manifest.json', private=True)[0]
        digest = hashlib.sha256(raw).hexdigest()
        payload = json.loads(raw)
        if (seal['snapshot'] != {'schema_version': payload['schema_version'], 'sha256': digest}
                or secure_read(root / 'complete', private=True)[0] != digest.encode()):
            raise ValueError()
        _verify_policy(root, payload['policy'])
        if hashlib.sha256(secure_read(seal_path.parent / 'config.yaml', private=True)[0]).hexdigest() != seal['configuration_sha256']:
            raise ValueError()
        path = Path(seal['binding_path'])
        if path.parent != root.parent or (expected_path is not None and Path(expected_path).absolute() != path):
            raise ValueError()
        binding_raw = secure_read(path)[0]
        binding = json.loads(binding_raw)
        if (hashlib.sha256(binding_raw).hexdigest() != seal['binding_sha256']
                or binding['hermes_snapshot'] != seal['snapshot']
                or binding['run_id'] != seal['run_id'] or binding['attempt'] != seal['attempt']
                or seal_path.parent.name != f"attempt-{binding['attempt']}"
                or payload['source'] != 'climate-acquisition-' + binding['run_id']
                or os.environ.get('HERMES_SESSION_SOURCE') != payload['source']
                or Path(binding['checkpoint_dir']).parent != root.parent):
            raise ValueError()
        config = json.loads(secure_read(seal_path.parent / 'config.yaml', private=True)[0])
        if config != acquisition_configuration(root, payload, binding, path):
            raise ValueError()
        for manifest, implementation in [('policy/plugin.json', 'policy/identity.py'),
                                          ('policy/search-plugin.json', 'policy/search-plugin.py')]:
            name = json.loads(secure_read(root / manifest, private=True)[0])['name']
            if name not in config['plugins']['enabled']:
                continue
            for target, frozen in [('plugin.yaml', manifest), ('__init__.py', implementation)]:
                content = secure_read(seal_path.parent / 'plugins' / name / target, private=True)[0]
                if payload['policy'][frozen] != {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}:
                    raise ValueError()
        return path, binding
    except BaseException:
        # Plugin dispatch catches ordinary exceptions. No partial guard failure
        # may be turned into a successful tool response or expose source values.
        os._exit(65)


def acquisition_configuration(root, payload, binding, binding_path):
    """The sole attempt config recipe, also verified by its frozen child copy."""
    import shlex
    root = Path(root)
    def expand(value):
        if isinstance(value, str) and value.startswith('@snapshot/'):
            return str(root / value[len('@snapshot/'):])
        if isinstance(value, dict):
            return {key: expand(child) for key, child in value.items()}
        if isinstance(value, list):
            return [expand(child) for child in value]
        return value
    contract = json.loads(secure_read(root / 'policy/acquisition.json', private=True)[0])
    identity = json.loads(secure_read(root / 'policy/plugin.json', private=True)[0])['name']
    search = json.loads(secure_read(root / 'policy/search-plugin.json', private=True)[0])['name']
    command = shlex.join([payload['interpreter'], '-I', '-S', str(root / 'bootstrap/launcher.py'), '--budget-hook',
                          '--binding', str(binding_path)])
    config = expand(payload['config'])
    if payload['web_config']:
        config['web'] = expand(payload['web_config'])
    config.update({'hooks_auto_accept': True, 'mcp_servers': {},
                   'memory': {'memory_enabled': False, 'user_profile_enabled': False},
                   'plugins': {'enabled': [identity]},
                   'hooks': {'pre_tool_call': [{'command': command, 'timeout': 15, 'fail_closed': True}],
                             'post_tool_call': [{'command': command, 'timeout': 15}]}})
    if payload['web_plugins_disabled']:
        config['plugins']['disabled'] = payload['web_plugins_disabled']
    if binding.get('agent_protocol') in contract['native_protocols']:
        config['plugins']['enabled'].append(search)
    if binding.get('agent_protocol') == contract['candidate_protocol']:
        config['tools'] = {'tool_search': {'enabled': 'off'}}
    return config



def reader_context(binding_path, binding):
    """Explicit reader authority for worker-side and frozen candidate fetches."""
    import climate_monitor.hermes_reader_runtime as reader
    import climate_monitor.hermes_runtime_inventory as engine
    root = Path(binding_path).parent / 'hermes-private'
    raw = secure_read(root / 'manifest.json', private=True)[0]
    digest = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw)
    if (binding['hermes_snapshot'] != {'schema_version': payload['schema_version'], 'sha256': digest}
            or secure_read(root / 'complete', private=True)[0] != digest.encode()
            or payload['source'] != 'climate-acquisition-' + binding['run_id']):
        raise ValueError('reader runtime changed; start a fresh run')
    _verify_policy(root, payload['policy'])
    reader.verify(root, payload, lambda p: secure_read(p, private=True)[0], engine)
    return {'data_root': payload['reader_runtime']['root'],
            'reader_home': str(root / ('attempt-' + str(binding['attempt'])))}

def reader_service(runtime_type, reader_home=None):
    """Bind the reader's external Python tools to our verified no-site entrypoint."""
    from web_listening.tool_registry import lifecycle
    import climate_monitor.hermes_reader_runtime as hermes_reader_runtime
    import climate_monitor.hermes_runtime_inventory as hermes_runtime_inventory
    if reader_home is None:
        verified_binding()
    home = str(Path(reader_home or os.environ['HERMES_HOME']))
    root = Path(home).parent
    payload = json.loads(secure_read(root / 'manifest.json', private=True)[0])
    hermes_reader_runtime.verify(root, payload, lambda p: secure_read(p, private=True)[0], hermes_runtime_inventory)
    _verify_policy(root, payload['policy'])
    hermes_reader_runtime.verify_attempt(root, Path(home), payload, lambda p: secure_read(p, private=True)[0], hermes_runtime_inventory)
    def installed_command(installed):
        # Recheck at every actual command construction, not just Runtime.open.
        hermes_reader_runtime.verify(root, payload, lambda p: secure_read(p, private=True)[0], hermes_runtime_inventory)
        entry = installed.directory / installed.entrypoint
        if entry.suffix != '.py':
            raise ValueError('unsupported reader executable; start a fresh run')
        interpreter = payload['interpreter']
        config = entry.parent / 'runtime.json'
        if config.exists():
            interpreter = json.loads(secure_read(config)[0])['python']
        return (interpreter, '-I', '-S', str(root / 'bootstrap/launcher.py'), '--reader-tool', home, str(entry))
    lifecycle._installed_command = installed_command
    return runtime_type
