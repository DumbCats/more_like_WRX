"""Snapshots that detect tests leaking state into later tests.

- `interpreter_settings` / `shared_marks`: interpreter-wide settings and the
  shallow state of sqlglot and test modules, compared before and after tests.
- watches: shared containers a patch may now fill differently, compared after
  every test (`resolve_watches` / `changed_watches`).
- `picture_of_tests` / `restore_tests`: the state of test classes and modules,
  restored between the commits of a fixture batch.
"""
import gc
import os
import sys
import types

import settings

_PRIMITIVE = (bool, int, float, str, bytes, type(None))
_FUNCTION_TYPES = (types.FunctionType, staticmethod, classmethod, property, types.MethodType, types.BuiltinFunctionType)
_CONTAINER_TYPES = (dict, set, list)
_MISSING = object()

# class attributes unittest and our harness set routinely (still restored, but not reported)
ROUTINE_CLASS_STATE = frozenset(('_classSetupFailed', '_class_cleanups', 'tearDown_exceptions', '_blast_parent_state'))
# InlinePool instance attributes that change whenever a pool is used
ROUTINE_POOL_STATE = frozenset(('_started', '_shutdown'))


def _decimal_settings(context):
    return (context.prec, context.rounding, context.Emin, context.Emax, context.capitals, context.clamp, tuple(sorted((signal.__name__ for signal, enabled in context.traps.items() if enabled))))


def _environ_snapshot():
    data = getattr(os.environ, '_data', None)
    if type(data) is dict:
        return dict(data)
    return hash(frozenset(os.environ.items()))


def interpreter_settings():
    """A comparable tuple of the interpreter-wide settings a test could change."""
    import decimal
    import logging
    root_logger = logging.getLogger()
    return (sys.getrecursionlimit(), sys.getswitchinterval(), os.getcwd(), len(sys.path), sys.path[:3], _environ_snapshot(), _decimal_settings(decimal.getcontext()), gc.get_threshold(), root_logger.level, len(root_logger.handlers), logging.root.manager.disable, sys.stdout is sys.__stdout__, sys.stderr is sys.__stderr__, sys.excepthook, sys.gettrace() is None)


def shared_marks():
    """Primitive values and object identities of sqlglot/test module globals and sqlglot class attributes."""
    marks = []
    seen = set()

    def class_marks(module_name, path, cls):
        if id(cls) in seen:
            return
        seen.add(id(cls))
        for name, value in list(vars(cls).items()):
            value_type = type(value)
            if value_type in _PRIMITIVE:
                marks.append((module_name, path, name, value))
            elif value_type in _FUNCTION_TYPES:
                marks.append((module_name, path, name, id(value)))
            elif isinstance(value, type) and value.__module__ == module_name:
                class_marks(module_name, path + '.' + name, value)
    for module_name, module in sorted(sys.modules.items()):
        if module is None or not (module_name == 'sqlglot' or module_name.startswith(('sqlglot.', 'tests.'))):
            continue
        for name, value in list(vars(module).items()):
            value_type = type(value)
            if value_type in _PRIMITIVE:
                marks.append((module_name, name, value))
            elif value_type in _FUNCTION_TYPES or value_type is types.ModuleType or isinstance(value, type):
                marks.append((module_name, name, id(value)))
                if isinstance(value, type) and value.__module__ == module_name and module_name.startswith('sqlglot'):
                    class_marks(module_name, name, value)
    return marks


# --- watched shared containers ---------------------------------------------------

def _container_copy(value):
    value_type = type(value)
    return value_type(value) if value_type in _CONTAINER_TYPES else value


def resolve_watches(specs):
    """Turn (module, container description) specs into live watches.

    A watch is [key, getter, current object, copy of its contents]. Attribute
    specs ('A', name) watch that attribute on every sqlglot class having it.
    """
    watches = []
    classes = None
    for module_name, container in specs:
        if container[0] == 'A':
            if classes is None:
                classes, seen = ([], set())

                def walk(cls, path):
                    if id(cls) in seen:
                        return
                    seen.add(id(cls))
                    classes.append((cls, path))
                    for name, value in list(vars(cls).items()):
                        if isinstance(value, type) and value.__module__ == cls.__module__:
                            walk(value, path + '.' + name)
                for loaded_name, module in sorted(sys.modules.items()):
                    if module is None or not (loaded_name == 'sqlglot' or loaded_name.startswith('sqlglot.')):
                        continue
                    for value in list(vars(module).values()):
                        if isinstance(value, type) and value.__module__ == loaded_name:
                            walk(value, value.__qualname__)
            for cls, path in classes:
                value = vars(cls).get(container[1])
                if type(value) in _CONTAINER_TYPES:
                    watches.append([('A', cls.__module__, path, container[1]), lambda owner=cls, attribute=container[1]: vars(owner).get(attribute), value, _container_copy(value)])
        else:
            module = sys.modules.get(module_name)
            if module is None:
                continue
            if len(container) == 2:
                key, getter = (('G', module_name, container[1]), lambda owner=module, attribute=container[1]: vars(owner).get(attribute))
            else:
                owner = vars(module).get(container[1])
                for attribute in container[2:-1]:
                    owner = getattr(owner, attribute, None)
                if owner is None:
                    continue
                key = None
                if isinstance(owner, type):
                    key = ('A', owner.__module__, owner.__qualname__, container[-1])
                elif isinstance(owner, types.ModuleType):
                    key = ('G', owner.__name__, container[-1])
                getter = lambda owner=owner, attribute=container[-1]: getattr(owner, attribute, None)
            value = getter()
            watches.append([key, getter, value, _container_copy(value)])
    return watches


def changed_watches(watches):
    """[(key, sorted changed entries or None)] for the watches that changed; re-arms them."""
    registry_keys = settings.registry_keys
    changes = []
    for watch in watches:
        value = watch[1]()
        if value is watch[2] and (type(value) not in _CONTAINER_TYPES or value == watch[3]):
            continue
        changed_keys = None
        if registry_keys is not None and type(value) is type(watch[3]) and (type(value) in (dict, set)):
            try:
                keys = registry_keys.changed_container_keys(watch[3], value, id, id)
                changed_keys = sorted(keys) if keys is not None else None
            except Exception:
                changed_keys = None
        changes.append((watch[0], changed_keys))
        watch[2], watch[3] = (value, _container_copy(value))
    return changes


# --- test classes between the commits of a fixture batch ------------------------------

def _same_items(first, second):
    if type(first) is not type(second) or len(first) != len(second):
        return False
    if type(first) is list:
        return all((a is b for a, b in zip(first, second)))
    if type(first) is dict:
        return all((second.get(key, _MISSING) is value for key, value in first.items()))
    return first == second


def _same_value(first, second):
    return first is second or (type(first) is type(second) and type(first) in _PRIMITIVE and (first == second))


def picture_of_tests(test_ids):
    """Copies of the namespaces of the tests' modules and classes (with container contents
    and InlinePool states), or None if one is not loaded."""
    import inline_pool
    classes, modules = ([], [])
    seen = set()
    keys = sorted({(test_id.split('::')[0][:-3].replace('/', '.'), test_id.split('::')[1]) for test_id in test_ids})
    for module_name in [name for name, _ in keys] + ['tests.helpers', 'sqlglot']:
        module = sys.modules.get(module_name)
        if module is None:
            return None
        if id(module) not in seen:
            seen.add(id(module))
            modules.append((module, dict(vars(module))))
    for module_name, class_name in keys:
        cls = getattr(sys.modules[module_name], class_name, None)
        if not isinstance(cls, type):
            return None
        for base in cls.__mro__:
            if base is object or base.__module__.partition('.')[0] == 'unittest' or id(base) in seen:
                continue
            seen.add(id(base))
            attributes = dict(vars(base))
            contents = {}
            for name, value in attributes.items():
                if type(value) in _CONTAINER_TYPES:
                    contents[name] = type(value)(value)
                elif isinstance(value, inline_pool.InlinePool):
                    contents[name] = dict(vars(value))
            classes.append((base, attributes, contents))
    return (classes, modules)


def restore_tests(picture, graveyard):
    """Put test classes back as `picture` saw them; return descriptions of unusual changes.

    Replaced objects go to `graveyard` so they are not freed (and their ids not
    reused) while the batch runs.
    """
    classes, modules = picture
    problems = []
    for cls, attributes, contents in classes:
        current = vars(cls)
        for name in [name for name in current if name not in attributes]:
            graveyard.append(current[name])
            delattr(cls, name)
            if name not in ROUTINE_CLASS_STATE:
                problems.append('%s.%s' % (cls.__name__, name))
        for name, value in attributes.items():
            now = current.get(name, _MISSING)
            if now is not value:
                graveyard.append(now)
                setattr(cls, name, value)
                if name not in ROUTINE_CLASS_STATE:
                    problems.append('%s.%s' % (cls.__name__, name))
        for name, saved in contents.items():
            value = attributes[name]
            if type(value) in _CONTAINER_TYPES:
                if not _same_items(value, saved):
                    graveyard.append(type(value)(value))
                    value.clear()
                    (value.extend if type(value) is list else value.update)(saved)
                    if name not in ROUTINE_CLASS_STATE:
                        problems.append('%s.%s items' % (cls.__name__, name))
            else:
                pool_state = vars(value)
                if not _same_items(pool_state, saved):
                    if any((not _same_value(pool_state.get(field, _MISSING), saved.get(field, _MISSING)) for field in set(pool_state) | set(saved) if field not in ROUTINE_POOL_STATE)):
                        problems.append('%s.%s pool' % (cls.__name__, name))
                    pool_state.clear()
                    pool_state.update(saved)
    for module, saved_globals in modules:
        current = vars(module)
        if len(current) != len(saved_globals) or any((not _same_value(current.get(name, _MISSING), value) for name, value in saved_globals.items())):
            problems.append('globals of %s' % module.__name__)
    return problems
