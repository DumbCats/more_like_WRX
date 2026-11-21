"""Re-import only the "cone" of sqlglot modules a patch affects, keeping the rest.

`record_imports()` takes an inventory of every object reachable from the imported
sqlglot modules (namespaces, classes, functions, containers, objects) together
with the references they hold to named sqlglot modules/classes/functions.
`reimport(cone, install)` drops the cone modules from sys.modules, imports them
again through the patched overlay, then checks that nothing outside the cone
changed except references that now point to the re-imported objects. Any
surprise raises ConeRedo, and the caller falls back to a full fresh import.
"""
from __future__ import annotations

import ast
import enum
import importlib
import marshal
import operator
import sys
import types

_IS = operator.is_
_LEAF_TYPES = (str, int, float, bool, bytes, complex, type(None), types.ModuleType, types.CodeType, types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType)
_MISSING = object()


def _is_sqlglot(name):
    return name == 'sqlglot' or name.startswith('sqlglot.')


# id -> named() result while record_imports() runs (None otherwise).
_NAMED_MEMO = None


def named(obj):
    """The sqlglot module owning `obj` when it is reachable there by name, else None."""
    memo = _NAMED_MEMO
    if memo is not None:
        owner = memo.get(id(obj), _MISSING)
        if owner is _MISSING:
            owner = memo[id(obj)] = _named(obj)
        return owner
    return _named(obj)


def _named(obj):
    if type(obj) is types.ModuleType:
        return obj.__name__ if _is_sqlglot(obj.__name__) else None
    if not (isinstance(obj, type) or type(obj) is types.FunctionType):
        return None
    module_name = getattr(obj, '__module__', None)
    if not isinstance(module_name, str) or not _is_sqlglot(module_name):
        return None
    module = sys.modules.get(module_name)
    qualname = getattr(obj, '__qualname__', '')
    if module is None or '<' in qualname:
        return None
    found = module
    for part in qualname.split('.'):
        namespace = vars(found) if type(found) is types.ModuleType else found.__dict__
        found = namespace.get(part, None)
        if isinstance(found, (staticmethod, classmethod)):
            found = found.__func__
        if found is None:
            return None
    return module_name if found is obj else None


class ModuleHeap:
    """Inventory of the objects owned by each imported sqlglot module.

    `records` holds tuples (kind, object, saved, owner module, marks):
      kind  'n' namespace (object = the module dict), 'c' class, 'd' dict,
            'l' list, 's' set, 'p' plain marshalable container, 'f' function,
            'o' other object with __dict__/__slots__;
      saved the state to compare against later (see _walk);
      marks (position, module) of values that are named sqlglot objects.
    """

    def __init__(self, walk_order=()):
        self.order = [name for name, module in sys.modules.items() if _is_sqlglot(name) and module is not None]
        self.index = {name: position for position, name in enumerate(self.order)}
        self.modules = {name: sys.modules[name] for name in self.order}
        self.records = []
        self._leaf = {}
        seen = set()
        first = [name for name in walk_order if name in self.modules]
        rest = [name for name in self.order if name not in set(first)]
        for name in first + rest:
            self._walk(self.modules[name], seen)
        self.by_owner = {}
        for record in self.records:
            self.by_owner.setdefault(record[3], []).append(record)

    def _is_leaf_type(self, value_type):
        """True for types whose instances are never walked into (memoised)."""
        leaf = self._leaf.get(value_type)
        if leaf is None:
            if value_type in _LEAF_TYPES or issubclass(value_type, enum.Enum):
                leaf = True
            elif value_type is types.FunctionType or value_type in (dict, list, set, tuple, frozenset, staticmethod, classmethod, property, types.MethodType) or issubclass(value_type, (type, dict)):
                leaf = False
            else:
                module_name = getattr(value_type, '__module__', '') or ''
                leaf = not (_is_sqlglot(module_name) or module_name == 'functools' or module_name == 'collections')
            self._leaf[value_type] = leaf
        return leaf

    def _push(self, stack, values):
        leaf_cache = self._leaf
        for value in values:
            value_type = type(value)
            leaf = leaf_cache.get(value_type)
            if leaf is None:
                leaf = self._is_leaf_type(value_type)
            if not leaf:
                stack.append(value)

    def _scan(self, stack, values, seen):
        """Queue walkable values; return marks for those that are named sqlglot objects."""
        leaf_cache = self._leaf
        marks = []
        push = stack.append
        for position, value in enumerate(values):
            value_type = type(value)
            leaf = leaf_cache.get(value_type)
            if leaf is None:
                leaf = self._is_leaf_type(value_type)
            if value_type is types.ModuleType or value_type is types.FunctionType or (not leaf and isinstance(value, type)):
                owner = named(value)
                if owner is not None:
                    marks.append((position, owner))
                    if value_type is types.ModuleType or owner != self._own:
                        continue
            if not leaf and id(value) not in seen:
                push(value)
        return marks

    def _walk(self, module, seen):
        """Record every object reachable from `module` that this module owns."""
        self._own = module.__name__
        own = module.__name__
        stack = [module]
        push = self._push
        records = self.records
        while stack:
            obj = stack.pop()
            key = id(obj)
            if key in seen:
                continue
            seen.add(key)
            obj_type = type(obj)
            children = None
            if obj_type is types.ModuleType:
                if obj is not module:
                    continue
                kind, children = ('n', vars(obj))
            elif isinstance(obj, type):
                if obj.__module__ != own and named(obj) is not None:
                    seen.discard(key)
                    continue
                if not _is_sqlglot(getattr(obj, '__module__', '') or ''):
                    continue
                kind, children = ('c', obj.__dict__)
            elif obj_type is types.FunctionType:
                if not _is_sqlglot(obj.__module__ or ''):
                    continue
                if obj.__module__ != own and named(obj) is not None:
                    seen.discard(key)
                    continue
                kind = 'f'
            elif obj_type is dict or obj_type is list or obj_type is set or (obj_type is tuple) or (obj_type is frozenset):
                plain = _plain_copy(obj)
                if plain is not None:
                    if obj_type is tuple or obj_type is frozenset:
                        continue
                    kind = 'p'
                elif obj_type is dict:
                    kind, children = ('d', obj)
                elif obj_type is list:
                    kind = 'l'
                elif obj_type is set:
                    kind = 's'
                else:
                    push(stack, obj)
                    continue
            elif isinstance(obj, dict):
                kind, children = ('d', obj)
            elif obj_type in (staticmethod, classmethod):
                stack.append(obj.__func__)
                continue
            elif obj_type is property:
                push(stack, (obj.fget, obj.fset, obj.fdel))
                continue
            elif obj_type is types.MethodType:
                push(stack, (obj.__func__, obj.__self__))
                continue
            elif self._is_leaf_type(obj_type):
                continue
            else:
                kind = 'o'
            if kind == 'p':
                saved, marks = (plain, ())
            elif children is not None:
                keys = tuple(children)
                values = tuple(children.values())
                saved = (keys, values, obj.__bases__ if kind == 'c' else None)
                marks = self._scan(stack, values, seen)
            elif kind in ('l', 's'):
                saved = tuple(obj)
                marks = self._scan(stack, saved, seen)
            elif kind == 'f':
                cells = obj.__closure__ or ()
                contents = tuple((_cell_contents(cell) for cell in cells))
                saved = (obj.__code__, obj.__defaults__, obj.__kwdefaults__, cells, contents)
                marks = ()
                push(stack, (obj.__defaults__, obj.__kwdefaults__) + contents)
                attributes = getattr(obj, '__dict__', None)
                if attributes:
                    stack.append(attributes)
            else:
                saved = _object_state(obj)
                if saved is None:
                    continue
                marks = ()
                push(stack, saved[1])
                if saved[0] is not None:
                    stack.append(saved[0])
            records.append((kind, obj if children is None or kind != 'n' else children, saved, own, tuple(marks)))


def _plain_copy(obj):
    """A marshal round-trip copy of `obj` when it holds only plain data, else None."""
    try:
        return marshal.loads(marshal.dumps(obj, 2))
    except (ValueError, TypeError):
        return None


def _cell_contents(cell):
    try:
        return cell.cell_contents
    except ValueError:
        return _MISSING


def _object_state(obj):
    """(__dict__ or None, tuple of slot values) of an object; None when it has neither."""
    namespace = getattr(obj, '__dict__', None)
    if not isinstance(namespace, dict):
        namespace = None
    slot_values = []
    for klass in type(obj).__mro__:
        slots = klass.__dict__.get('__slots__')
        if not slots:
            continue
        for slot in (slots,) if isinstance(slots, str) else slots:
            if slot in ('__dict__', '__weakref__'):
                continue
            try:
                slot_values.append(getattr(obj, slot))
            except AttributeError:
                slot_values.append(_MISSING)
    if namespace is None and (not slot_values):
        return None
    return (namespace, tuple(slot_values))


# The inventory taken by the last record_imports() call.
RECORDED_HEAP = None


def record_imports(walk_order=()):
    """Take the inventory of the imported sqlglot modules (walking `walk_order` first)."""
    global RECORDED_HEAP, _NAMED_MEMO
    _NAMED_MEMO = {}
    try:
        RECORDED_HEAP = ModuleHeap(walk_order)
    finally:
        _NAMED_MEMO = None
    return RECORDED_HEAP


def import_statements(text):
    """Sorted AST dumps of the import statements and import calls in a source text."""
    tree = ast.parse(text)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if name in ('import_module', '__import__', 'find_spec', 'reload'):
                found.append(ast.dump(node, include_attributes=False))
    return sorted(found)


class ConeRedo(Exception):
    """The cone re-import cannot be trusted; redo the commit with a full fresh import."""


def reimport(cone, install):
    """Re-import the `cone` modules after `install()` sets up the overlay.

    Returns the modules outside the cone that hold functions referring to
    re-imported objects. Raises ConeRedo when anything is not as recorded.
    """
    heap = RECORDED_HEAP
    cone_set = set(cone)
    for name in cone:
        if name not in heap.index:
            raise ConeRedo('not in the pinned import: ' + name)
    for name in heap.order:
        if sys.modules.get(name) is not heap.modules[name]:
            raise ConeRedo('module changed since the record: ' + name)
    for kind, obj, saved, owner, marks in heap.records:
        if kind == 'n' and marks and (owner not in cone_set):
            keys = saved[0]
            for position, target in marks:
                if target in cone_set:
                    obj.pop(keys[position], None)
    for name in cone:
        del sys.modules[name]
    install()
    for name in cone:
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
        except BaseException as error:
            if isinstance(error, KeyboardInterrupt):
                raise
            raise ConeRedo('import fails: %s %r' % (name, error)) from None
    imported = [name for name, module in sys.modules.items() if _is_sqlglot(name) and module is not None]
    if len(imported) != len(heap.order) or set(imported) != set(heap.order):
        raise ConeRedo('other sqlglot modules after the import')
    for name in heap.order:
        module = sys.modules[name]
        if (module is heap.modules[name]) == (name in cone_set):
            raise ConeRedo('module not imported again, or imported when it should not be: ' + name)
    touched = set()
    for owner, records in heap.by_owner.items():
        if owner in cone_set:
            continue
        for record in records:
            refers_to_cone = False
            for _, target in record[4]:
                if target in cone_set:
                    refers_to_cone = True
                    break
            if not refers_to_cone and _fast_same(record):
                continue
            if not _same(record[0], record[1], record[2], record[4], cone_set):
                raise ConeRedo('state outside the cone changed: %s %s' % (record[0], _describe(record[1])))
            values = record[2][1] if record[0] in ('n', 'c', 'd') else record[2]
            if any((type(values[position]) is types.FunctionType for position, target in record[4] if target in cone_set)):
                touched.add(owner)
    return touched


def _describe(obj):
    """Short text naming a changed object, for ConeRedo messages."""
    if isinstance(obj, type) and RECORDED_HEAP is not None:
        for _, recorded, saved, _, _ in RECORDED_HEAP.records:
            if recorded is obj:
                now = dict(obj.__dict__)
                before = dict(zip(saved[0], saved[1]))
                differing = [name for name in set(now) | set(before) if now.get(name, _MISSING) is not before.get(name, _MISSING)]
                return 'class %s.%s %s' % (obj.__module__, obj.__qualname__, differing[:8])
    if type(obj) is dict and '__name__' in obj:
        return 'namespace ' + str(obj.get('__name__'))
    if isinstance(obj, type):
        return 'class ' + obj.__module__ + '.' + obj.__qualname__
    return type(obj).__name__ + ' ' + repr(obj)[:80]


def _fast_same(record):
    """Cheap check that a record is unchanged (False means: check properly)."""
    kind, obj, saved = (record[0], record[1], record[2])
    if kind == 'd' or kind == 'n':
        return tuple(obj.values()) == saved[1] and tuple(obj) == saved[0]
    if kind == 'p':
        return obj == saved
    if kind == 'c':
        namespace = obj.__dict__
        return tuple(namespace.values()) == saved[1] and tuple(namespace) == saved[0] and (obj.__bases__ is saved[2])
    if kind == 'f':
        return obj.__code__ is saved[0] and obj.__defaults__ is saved[1] and (obj.__kwdefaults__ is saved[2]) and (not saved[3] or tuple(map(_cell_contents, saved[3])) == saved[4]) and ((obj.__closure__ or ()) == saved[3])
    if kind == 'l' or kind == 's':
        return tuple(obj) == saved
    return False


def _same(kind, obj, saved, marks, cone_set):
    """True when a record is unchanged, allowing marked references into the cone to be replaced.

    A namespace whose key order changed only because cone names were popped
    and re-added is put back into the recorded order.
    """
    if kind == 'p':
        return type(obj) is type(saved) and obj == saved
    cone_marks = [mark for mark in marks if mark[1] in cone_set]
    if kind in ('n', 'c', 'd'):
        saved_keys, saved_values, saved_bases = saved
        namespace = obj.__dict__ if kind == 'c' else obj
        if kind == 'c' and obj.__bases__ is not saved_bases:
            return False
        if len(namespace) != len(saved_keys):
            return False
        if kind == 'n' and cone_marks and (not all(map(_IS, namespace, saved_keys))):
            if set(namespace) != set(saved_keys):
                return False
            reordered = [(key, namespace[key]) for key in saved_keys]
            namespace.clear()
            namespace.update(reordered)
        if not all(map(_IS, namespace, saved_keys)) and tuple(namespace) != saved_keys:
            return False
        values = tuple(namespace.values())
        if not cone_marks:
            return all(map(_IS, values, saved_values))
        return _same_marked(values, saved_values, cone_marks)
    if kind in ('l', 's'):
        values = tuple(obj)
        if len(values) != len(saved):
            return False
        if not cone_marks:
            if all(map(_IS, values, saved)):
                return True
            return kind == 's' and set(values) == set(saved) and (len(set(map(id, values)) ^ set(map(id, saved))) == 0)
        return kind == 'l' and _same_marked(values, saved, cone_marks)
    if kind == 'f':
        closure = obj.__closure__ or ()
        return obj.__code__ is saved[0] and obj.__defaults__ is saved[1] and (obj.__kwdefaults__ is saved[2]) and (len(closure) == len(saved[3])) and all(map(_IS, closure, saved[3])) and all(map(_IS, map(_cell_contents, closure), saved[4]))
    state = _object_state(obj)
    return state is not None and state[0] is saved[0] and (len(state[1]) == len(saved[1])) and all(map(_IS, state[1], saved[1]))


def _same_marked(now, before, cone_marks):
    """Values are identical except marked positions, which must now hold the re-imported
    object of the same name."""
    owner_by_position = dict(cone_marks)
    for position, (current, previous) in enumerate(zip(now, before)):
        if position in owner_by_position:
            if current is previous or type(current) is not type(previous):
                return False
            if type(current) is types.ModuleType:
                if current.__name__ != previous.__name__ or sys.modules.get(current.__name__) is not current:
                    return False
            elif named(current) != owner_by_position[position] or current.__qualname__ != previous.__qualname__:
                return False
        elif current is not previous:
            return False
    return True
