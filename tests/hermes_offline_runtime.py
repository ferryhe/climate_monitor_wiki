"""Small offline implementation of Hermes' plugin registration contract.

No inference or user code discovery. Generated managed plugins register through
ctx.register_hook, as in Hermes 0.20.5 and 0.21.3. The verifier runs in a real
subprocess; missing/broken registration is observable without a Hermes install.
"""
import subprocess
_POPEN = subprocess.Popen

PLUGIN_LOADER = '''
import importlib.util
import json
import os
from pathlib import Path

class Manager:
    def __init__(self):
        self.plugins = []
        self.hooks = {}
        self.tools = {}
    def list_plugins(self):
        return self.plugins
    def emit(self, event, **kwargs):
        for callback in self.hooks.get(event, []):
            try:
                callback(**kwargs)
            except Exception:
                pass  # Hermes lifecycle dispatch suppresses plugin exceptions.

_manager = Manager()
def get_plugin_manager():
    return _manager

def discover_entrypoint_manifests():
    return []

def discover_plugins(force=False):
    global _manager
    _manager = Manager()
    discover_entrypoint_manifests()
    home = Path(os.environ['HERMES_HOME'])
    config = json.loads((home / 'config.yaml').read_text())
    for name in config['plugins']['enabled']:
        plugin = home / 'plugins' / name
        manifest = json.loads((plugin / 'plugin.yaml').read_text())
        assert manifest['name'] == name
        assert config.get('hooks_auto_accept') is True
        hooks = []
        class Context:
            def register_tool(self, **kwargs):
                assert kwargs.get("name") and callable(kwargs.get("handler"))
                _manager.tools[kwargs["name"]] = kwargs
            def register_hook(self, event, callback):
                assert event in manifest['hooks']
                hooks.append(event)
                _manager.hooks.setdefault(event, []).append(callback)
        spec = importlib.util.spec_from_file_location(name, plugin / '__init__.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.register(Context())
        _manager.plugins.append({'key': name, 'enabled': True, 'hooks': len(hooks)})
'''


def install_plugin_loader(root):
    (root / 'hermes_cli/plugins.py').write_text(PLUGIN_LOADER)
    (root / 'hermes_cli/config.py').write_text("import os,json\nfrom pathlib import Path\ndef load_config(): return json.loads((Path(os.environ['HERMES_HOME'])/'config.yaml').read_text())\n")
    (root / 'agent').mkdir(exist_ok=True)
    (root / 'agent/__init__.py').write_text('')
    (root / 'agent/shell_hooks.py').write_text(SHELL_HOOKS)
    (root / 'model_tools.py').write_text("from hermes_cli.plugins import get_plugin_manager\ndef get_tool_definitions(**kwargs): return [{'function': v['schema']} for v in get_plugin_manager().tools.values()]\n")
    (root / 'tools').mkdir(exist_ok=True)
    (root / 'tools/__init__.py').write_text('')
    (root / 'tools/skills_sync.py').write_text('def sync_skills(quiet=False): return {}\n')


def successful_api_lifecycle(home, environment):
    """Simulated successful provider call through real frozen plugin discovery.

    Used only by explicit inference subprocess doubles. No publication helper or
    verifier is patched: the generated plugin owns pre/post and identity writes.
    """
    import json
    from pathlib import Path
    root = Path(environment['HERMES_HOME']).parent
    payload = json.loads((root / 'manifest.json').read_text())
    code = ("from hermes_cli.plugins import discover_plugins,get_plugin_manager; "
            "discover_plugins(force=True); m=get_plugin_manager(); "
            "m.emit('pre_api_request',provider='offline',model='offline-model'); "
            "m.emit('post_api_request',provider='offline',model='offline-model')")
    with _POPEN([payload['interpreter'], '-I', '-S', str(root / 'bootstrap/launcher.py'), '-c', code], cwd=home, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        process.communicate(timeout=30)
    if process.returncode:
        raise ValueError('offline frozen plugin lifecycle failed')


SHELL_HOOKS = '''
import json, os, shlex, subprocess
from types import SimpleNamespace
def iter_configured_hooks(config):
    return [SimpleNamespace(event=event, command=row['command'], fail_closed=row.get('fail_closed', False))
            for event, rows in config.get('hooks', {}).items() for row in rows]
def register_from_config(config, accept_hooks=False):
    assert accept_hooks
    return iter_configured_hooks(config)
def run_once(spec, payload):
    result = subprocess.run(shlex.split(spec.command), input=json.dumps(payload), text=True,
                            capture_output=True, env=dict(os.environ), timeout=15)
    return {'parsed': json.loads(result.stdout) if result.stdout else None, 'returncode': result.returncode}
'''
