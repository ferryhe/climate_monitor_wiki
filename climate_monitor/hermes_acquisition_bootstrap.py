"""Import and execution origin boundary for the private acquisition bundle."""
import builtins
import importlib
import importlib.abc
import importlib.machinery
from pathlib import Path
import sys
import sysconfig

_installed = None


def install(root, runtime_root=None):
    global _installed
    root = Path(root).absolute()
    if _installed == root:
        return
    if _installed is not None:
        raise ValueError('acquisition import root changed')
    prefix = str(root) + '/'
    stdlib = Path(sysconfig.get_path('stdlib')).resolve()
    names = {p.name.split('.')[0] for p in root.iterdir() if not p.name.endswith(('.dist-info', '.libs'))}
    def private(origin):
        return str(origin).startswith(prefix) and root in Path(origin).resolve().parents
    for name, prior in tuple(sys.modules.items()):
        if name.split('.')[0] in names and prior is not None and not private(getattr(prior, '__file__', '')):
            raise ValueError('unbound acquisition import already loaded')
    sys.path.insert(0, str(root))
    def caller():
        frame = sys._getframe(1)
        runtime_handoff = False
        while frame:
            filename = frame.f_code.co_filename
            if private(filename):
                if runtime_handoff and filename == str(root / 'scripts/acquisition_budget_hook.py'):
                    return ''
                return filename
            if runtime_root and filename.startswith(str(runtime_root) + '/'):
                runtime_handoff = True
            # A deliberate handoff from the frozen verifier to Hermes may load
            # Hermes' own runtime. It is not an acquisition dependency import.
            if filename.endswith('/scripts/acquisition_budget_hook.py'):
                return filename
            frame = frame.f_back
        return ''
    def permitted(name, origin):
        if origin:
            top = name.split('.')[0]
            hermes = origin == str(root / 'scripts/acquisition_budget_hook.py') and top in {'agent', 'hermes_cli', 'model_tools'}
            if top not in names and top not in sys.stdlib_module_names and not hermes:
                raise ModuleNotFoundError('unbound acquisition dependency')
    class FrozenFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split('.')[0] not in names:
                # Do not fall through to ambient finders for acquisition code.
                permitted(fullname, caller())
                return None
            locations = [str(root)] if path is None else list(path)
            if any(not (Path(p) == root or root in Path(p).resolve().parents) for p in locations):
                raise ValueError('unbound acquisition import path')
            spec = importlib.machinery.PathFinder.find_spec(fullname, locations)
            if spec is None or not spec.origin or not private(spec.origin) or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                raise ModuleNotFoundError('frozen acquisition dependency unavailable')
            return spec
    sys.meta_path.insert(0, FrozenFinder())
    def audit(event, arguments):
        if event != 'exec':
            return
        origin = caller()
        if not origin or origin == str(root / 'scripts/acquisition_budget_hook.py'):
            return
        filename = arguments[0].co_filename
        if filename.startswith('<frozen ') or private(filename):
            return
        generator = Path(sys._getframe(1).f_code.co_filename)
        if filename.startswith('<') and generator in {stdlib / 'collections/__init__.py', stdlib / 'dataclasses.py', stdlib / 'typing.py'}:
            return
        path = Path(filename).absolute()
        if stdlib in path.parents and 'site-packages' not in path.parts:
            return
        raise ValueError('unbound acquisition execution origin')
    sys.addaudithook(audit)
    original = builtins.__import__
    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        origin = str((globals or {}).get('__file__') or '')
        if not level and private(origin):
            permitted(name, origin)
        result = original(name, globals, locals, fromlist, level)
        if name.split('.')[0] in names:
            for key, module in tuple(sys.modules.items()):
                if (key == name or key.startswith(name + '.')) and module is not None and not private(getattr(module, '__file__', '')):
                    raise ValueError('unbound acquisition module origin')
        return result
    builtins.__import__ = guarded_import
    original_module = importlib.import_module
    def guarded_module(name, package=None):
        if not name.startswith('.'):
            permitted(name, caller())
        return original_module(name, package)
    importlib.import_module = guarded_module
    _installed = root
