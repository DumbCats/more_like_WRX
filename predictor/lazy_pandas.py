"""Defer the (expensive) import of numpy and pandas until a test really uses them.

`install()` puts placeholder modules for numpy, pandas and pandas.testing into
sys.modules; the first attribute access runs the real import in place. Other
modules that imported `pandas.testing.assert_frame_equal` early get the real
function patched in once pandas is loaded.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import types

DEFERRED_MODULES = ('numpy', 'pandas')
_INSTALLED = {'installed': False}


class _DeferredModule(types.ModuleType):
    """A module whose body has not run yet; any attribute access runs it."""

    def __getattr__(self, name):
        _execute(self)
        return getattr(self, name)


def _execute(module):
    """Run the real module body of a deferred module (no-op if already run)."""
    if type(module) is not _DeferredModule:
        return
    spec = module.__dict__['__spec__']
    module.__class__ = types.ModuleType
    if spec.submodule_search_locations is not None:
        module.__path__ = spec.submodule_search_locations
    if spec.name == 'pandas' and isinstance(sys.modules.get('pandas.testing'), _TestingStandIn):
        del sys.modules['pandas.testing']
    spec._initializing = True
    try:
        spec.loader.exec_module(module)
    finally:
        spec._initializing = False
    if spec.name == 'pandas':
        real_testing = sys.modules.get('pandas.testing')
        if real_testing is not None and (not isinstance(real_testing, _TestingStandIn)):
            for other in list(sys.modules.values()):
                namespace = getattr(other, '__dict__', None)
                if type(namespace) is dict and namespace.get('assert_frame_equal') is assert_frame_equal and (other is not _THIS_MODULE):
                    namespace['assert_frame_equal'] = real_testing.assert_frame_equal


def _real_testing():
    load()
    return sys.modules['pandas.testing']


def assert_frame_equal(*args, **kwargs):
    """Stand-in for pandas.testing.assert_frame_equal that loads pandas on first call."""
    return _real_testing().assert_frame_equal(*args, **kwargs)


class _TestingStandIn(types.ModuleType):
    """Placeholder for pandas.testing; forwards attribute reads to the real module."""

    def __getattr__(self, name):
        if name.startswith('__') and name.endswith('__'):
            raise AttributeError(name)
        return getattr(_real_testing(), name)


def install() -> bool:
    """Install the placeholders; False when numpy/pandas are already imported or not found."""
    if _INSTALLED['installed']:
        return True
    if any((name in sys.modules for name in DEFERRED_MODULES + ('pandas.testing',))):
        return False
    specs = {}
    for name in DEFERRED_MODULES:
        spec = importlib.machinery.PathFinder.find_spec(name)
        if spec is None or spec.loader is None or (not hasattr(spec.loader, 'exec_module')):
            return False
        specs[name] = spec
    for name, spec in specs.items():
        module = importlib.util.module_from_spec(spec)
        module.__dict__.pop('__path__', None)
        module.__class__ = _DeferredModule
        sys.modules[name] = module
    testing = _TestingStandIn('pandas.testing')
    testing.assert_frame_equal = assert_frame_equal
    sys.modules['pandas.testing'] = testing
    _INSTALLED['installed'] = True
    return True


def loaded(name) -> bool:
    """True when module `name` is imported and its body has run."""
    module = sys.modules.get(name)
    return module is not None and type(module) is not _DeferredModule


def load() -> None:
    """Run the real imports of every still-deferred module now."""
    for name in DEFERRED_MODULES:
        module = sys.modules.get(name)
        if type(module) is _DeferredModule:
            _execute(module)


_THIS_MODULE = sys.modules[__name__]
