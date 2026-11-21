"""Fingerprints of sqlglot's import-time state, to see what a patch changes.

`snapshot()` walks every imported sqlglot module (globals and class attributes,
plus the dialect component classes) and maps a key per value to a hash:
  ('G', module, name)              module global
  ('A', module, class path, attr)  class attribute
  ('O', ...key[1:], field)         a field of a sqlglot object stored there
The base snapshot (pinned tree) is compared with one taken after the patched
modules are imported; changed keys tell which state a patch touched.

The hashes depend on PYTHONHASHSEED (fixed to 0) and on this file: the saved
base state's header includes a checksum of this file, so edit with care.
"""
from __future__ import annotations

import enum
import marshal
import re
import sys
import types

_PRIMITIVE = (str, int, float, bool, bytes, complex, type(None))
_MARSHALABLE = (dict, list, tuple, set, frozenset)
_PRIMITIVE_NAME = {primitive: primitive.__name__ for primitive in _PRIMITIVE}


class Fingerprinter:
    """Hashes values structurally, memoised by object id.

    `code_alias` maps new code hashes to old ones (functions the patch swapped
    in place count as unchanged); `base_codes` (from base_codes() of the base
    snapshot's fingerprinter) lets unchanged code objects reuse their base hash.
    `objects` and `function_keys` are filled by snapshot().
    """

    def __init__(self, code_alias=None, base_codes=None):
        self.memo = {}
        self.busy = set()
        self.code_memo = {}
        self.code_objects = []
        self.code_alias = code_alias or {}
        self.base = None
        self.objects = {}
        self.function_keys = set()
        if base_codes:
            base_hashes = base_codes[1]
            if not any((new_hash != old_hash and new_hash in base_hashes for new_hash, old_hash in self.code_alias.items())):
                self.base = base_codes[0]

    def base_codes(self):
        """(code id -> hash, all hashes, code objects kept alive) for later fingerprinters."""
        return (dict(self.code_memo), frozenset(self.code_memo.values()), list(self.code_objects))

    def code_hash(self, code):
        """Hash of a code object (bytecode, names, constants; not line numbers)."""
        digest = self.code_memo.get(id(code))
        if digest is None and self.base is not None:
            digest = self.base.get(id(code))
            if digest is not None:
                digest = self.code_alias.get(digest, digest)
                self.code_memo[id(code)] = digest
                return digest
        if digest is None:
            self.code_objects.append(code)
            parts = [code.co_code, code.co_names, code.co_varnames, code.co_freevars, code.co_argcount, code.co_flags]
            for const in code.co_consts:
                parts.append(self.code_hash(const) if isinstance(const, types.CodeType) else (type(const).__name__, repr(const)))
            digest = hash(tuple(parts))
            digest = self.code_alias.get(digest, digest)
            self.code_memo[id(code)] = digest
        return digest

    def fingerprint(self, value, depth=0):
        """Fingerprint of `value`: (type name, value) for primitives, else an int hash."""
        value_type = type(value)
        primitive_name = _PRIMITIVE_NAME.get(value_type)
        if primitive_name is not None:
            return (primitive_name, value)
        key = id(value)
        known = self.memo.get(key)
        if known is not None:
            return known
        if key in self.busy or depth > 12:
            return ('cycle', value_type.__name__)
        self.busy.add(key)
        try:
            description = self._describe(value, value_type, depth)
        finally:
            self.busy.discard(key)
        try:
            digest = hash(description)
        except TypeError:
            digest = hash(repr(description))
        self.memo[key] = digest
        return digest

    def _describe(self, value, value_type, depth):
        """A hashable structural description of a non-primitive value."""
        if value_type in _MARSHALABLE:
            try:
                return ('m', value_type.__name__, marshal.dumps(value, 2))
            except ValueError:
                pass
            if value_type is dict:
                return self._dict(value, depth)
            if value_type is tuple and value and all((isinstance(item, type) for item in value)):
                return ('tc', frozenset(self._sequence(value, depth + 1)))
            if value_type in (list, tuple):
                return (value_type.__name__, tuple(self._sequence(value, depth + 1)))
            return ('s', frozenset(self._sequence(value, depth + 1)))
        if isinstance(value, enum.Enum):
            return ('E', value_type.__module__, value_type.__qualname__, value.name, self.fingerprint(value.value, depth + 1))
        if isinstance(value, type):
            return ('C', value.__module__, value.__qualname__)
        if value_type is types.ModuleType:
            return ('M', value.__name__)
        if value_type is types.FunctionType:
            cells = ()
            if value.__closure__:
                cells = tuple((self._cell(cell, depth) for cell in value.__closure__))
            return ('F', value.__qualname__, self.code_hash(value.__code__), self.fingerprint(value.__defaults__, depth + 1), self.fingerprint(value.__kwdefaults__, depth + 1), cells)
        if value_type in (staticmethod, classmethod):
            return (value_type.__name__, self.fingerprint(value.__func__, depth + 1))
        if value_type is property:
            return ('prop', self.fingerprint(value.fget, depth + 1), self.fingerprint(value.fset, depth + 1), self.fingerprint(value.fdel, depth + 1))
        if value_type is types.MethodType:
            return ('meth', self.fingerprint(value.__func__, depth + 1), self.fingerprint(value.__self__, depth + 1))
        if value_type is types.BuiltinFunctionType or value_type is types.MethodDescriptorType or value_type is types.WrapperDescriptorType:
            return ('B', getattr(value, '__qualname__', repr(value)))
        if value_type is re.Pattern:
            return ('re', value.pattern, value.flags)
        if isinstance(value, dict):
            return ('dd', value_type.__qualname__, self._dict(value, depth))
        if isinstance(value, (list, tuple)):
            return ('ll', value_type.__qualname__, tuple((self.fingerprint(item, depth + 1) for item in value)))
        if isinstance(value, (set, frozenset)):
            return ('ss', value_type.__qualname__, frozenset((self.fingerprint(item, depth + 1) for item in value)))
        module_name = getattr(value_type, '__module__', '') or ''
        if module_name.startswith('functools') and hasattr(value, 'func'):
            return ('partial', self.fingerprint(value.func, depth + 1), self.fingerprint(value.args, depth + 1), self.fingerprint(value.keywords, depth + 1))
        if module_name.startswith('sqlglot'):
            fields = self.instance_fields(value, value_type)
            if fields is not None:
                return ('O', module_name, value_type.__qualname__, tuple(((name, self.fingerprint(field, depth + 1)) for name, field in fields)))
        wrapped = getattr(value, '__wrapped__', None)
        if wrapped is not None:
            return ('W', module_name, value_type.__qualname__, self.fingerprint(wrapped, depth + 1))
        try:
            text = repr(value)
        except Exception:
            text = ' at 0x'
        if ' at 0x' in text or len(text) > 4000:
            return ('T', module_name, value_type.__qualname__)
        return ('R', module_name, value_type.__qualname__, text)

    def _sequence(self, values, depth):
        # Hot path: locals bound once, memo consulted before recursing.
        primitive_name_of = _PRIMITIVE_NAME.get
        memo_get = self.memo.get
        fingerprint = self.fingerprint
        result = []
        append = result.append
        for item in values:
            primitive_name = primitive_name_of(type(item))
            if primitive_name is not None:
                append((primitive_name, item))
                continue
            known = memo_get(id(item))
            append(known if known is not None else fingerprint(item, depth))
        return result

    def _dict(self, mapping, depth):
        # Hot path, see _sequence. Dicts keyed by classes are order-insensitive.
        primitive_name_of = _PRIMITIVE_NAME.get
        memo_get = self.memo.get
        fingerprint = self.fingerprint
        child_depth = depth + 1
        items = []
        append = items.append
        keyed_by_class = False
        for key, value in mapping.items():
            primitive_name = primitive_name_of(type(key))
            if primitive_name is not None:
                key_print = (primitive_name, key)
            else:
                if not keyed_by_class and isinstance(key, type):
                    keyed_by_class = True
                key_print = memo_get(id(key))
                if key_print is None:
                    key_print = fingerprint(key, child_depth)
            primitive_name = primitive_name_of(type(value))
            if primitive_name is not None:
                append((key_print, (primitive_name, value)))
                continue
            known = memo_get(id(value))
            append((key_print, known if known is not None else fingerprint(value, child_depth)))
        if keyed_by_class:
            return ('d', frozenset(items))
        return ('d', tuple(items))

    def _cell(self, cell, depth):
        try:
            return self.fingerprint(cell.cell_contents, depth + 1)
        except ValueError:
            return ('empty',)

    @staticmethod
    def instance_fields(value, value_type):
        """Sorted (name, value) of an object's __dict__ and __slots__; None without either."""
        fields = []
        namespace = getattr(value, '__dict__', None)
        if isinstance(namespace, dict):
            fields.extend(namespace.items())
        for klass in value_type.__mro__:
            slots = klass.__dict__.get('__slots__')
            if slots:
                if isinstance(slots, str):
                    slots = (slots,)
                for slot in slots:
                    if slot in ('__dict__', '__weakref__'):
                        continue
                    try:
                        fields.append((slot, getattr(value, slot)))
                    except AttributeError:
                        pass
        if not fields and namespace is None:
            return None
        return sorted(fields, key=lambda item: item[0])


SKIP_ATTRS = frozenset(('__module__', '__qualname__', '__doc__', '__dict__', '__weakref__', '__firstlineno__', '__static_attributes__', '__annotations__', '__parameters__', '__orig_bases__', '__abstractmethods__', '_abc_impl', '__builtins__', '__cached__', '__file__', '__loader__', '__spec__', '__name__', '__package__', '__path__', '__annotate__'))
ROOT_CLASSES = (('sqlglot.tokens', '_TokenizerBase'), ('sqlglot.parser', 'Parser'), ('sqlglot.generator', 'Generator'), ('sqlglot.dialects.dialect', 'Dialect'))
OWNER_ATTRS = ('tokenizer_class', 'jsonpath_tokenizer_class', 'parser_class', 'generator_class')


def _resolves(cls):
    """True when `cls` is reachable from its module by its qualified name."""
    module = sys.modules.get(cls.__module__)
    if module is None:
        return False
    namespace = vars(module)
    found = None
    for part in cls.__qualname__.split('.'):
        found = namespace.get(part)
        if not isinstance(found, type):
            return False
        namespace = found.__dict__
    return found is cls


def root_classes():
    """The imported tokenizer/parser/generator/dialect base classes."""
    roots = []
    for module_name, class_name in ROOT_CLASSES:
        module = sys.modules.get(module_name)
        cls = vars(module).get(class_name) if module is not None else None
        if isinstance(cls, type):
            roots.append(cls)
    return roots


def tree_classes(roots=None):
    """The roots and all their subclasses, depth first in definition order."""
    classes, seen = ([], set())
    stack = list(reversed(roots if roots is not None else root_classes()))
    while stack:
        cls = stack.pop()
        if id(cls) in seen:
            continue
        seen.add(id(cls))
        classes.append(cls)
        stack.extend(reversed(type.__subclasses__(cls)))
    return classes


def class_paths():
    """id -> (class, module, path) for the dialect class tree.

    Classes reachable by name keep their own path; component classes generated
    per dialect (e.g. Dialect.tokenizer_class) are named '<dialect path>.<attr>'.
    """
    paths = {}
    for cls in tree_classes():
        if _resolves(cls):
            paths[id(cls)] = (cls, cls.__module__, cls.__qualname__)
    owners = sorted(((entry[1], entry[2], entry[0]) for entry in paths.values() if hasattr(entry[0], 'tokenizer_class')))
    for module_name, path, owner in owners:
        for attr in OWNER_ATTRS:
            cls = owner.__dict__.get(attr)
            if isinstance(cls, type) and id(cls) not in paths:
                paths[id(cls)] = (cls, module_name, path + '.' + attr)
    return paths


def snapshot(code_alias=None, base_codes=None, only=None):
    """(state key -> fingerprint, the Fingerprinter) for the imported sqlglot modules.

    `only` restricts the walk to those module names. The Fingerprinter's
    `objects` holds the dict/set values by key and `function_keys` the keys
    holding functions.
    """
    fingerprinter = Fingerprinter(code_alias, base_codes)
    state = {}
    fingerprinter.objects = {}
    seen_classes = set()
    fingerprinter.function_keys = set()

    def add_value(key, value):
        value_type = type(value)
        if value_type in (types.FunctionType, classmethod, staticmethod, property, types.MethodType):
            fingerprinter.function_keys.add(key)
        module_name = getattr(value_type, '__module__', '') or ''
        if module_name.startswith('sqlglot') and (not isinstance(value, (type, enum.Enum))) and (value_type is not types.FunctionType):
            fields = fingerprinter.instance_fields(value, value_type)
            if fields:
                for name, field in fields:
                    state[('O',) + key[1:] + (name,)] = fingerprinter.fingerprint(field)
                return
        if value_type is dict or value_type is set or value_type is frozenset:
            fingerprinter.objects[key] = value
        state[key] = fingerprinter.fingerprint(value)

    def add_class(cls, module_name=None, path=None):
        if cls in seen_classes:
            return
        seen_classes.add(cls)
        module_name = module_name or cls.__module__
        path = path or cls.__qualname__
        for attr, value in list(cls.__dict__.items()):
            if attr in SKIP_ATTRS:
                continue
            if isinstance(value, type) and value.__module__ == cls.__module__ and value.__qualname__.startswith(cls.__qualname__ + '.'):
                add_class(value)
                continue
            add_value(('A', module_name, path, attr), value)
        state['A', module_name, path, '__bases__'] = fingerprinter.fingerprint(cls.__bases__)
    for module_name, module in sorted(sys.modules.items()):
        if module is None or not (module_name == 'sqlglot' or module_name.startswith('sqlglot.')):
            continue
        if only is not None and module_name not in only:
            continue
        for name, value in list(vars(module).items()):
            if name in SKIP_ATTRS:
                continue
            if isinstance(value, type):
                if value.__module__ == module_name:
                    add_class(value)
                else:
                    state['G', module_name, name] = fingerprinter.fingerprint(value)
                continue
            if isinstance(value, types.ModuleType):
                state['G', module_name, name] = ('M', value.__name__)
                continue
            add_value(('G', module_name, name), value)
    for cls, module_name, path in class_paths().values():
        if only is not None and module_name not in only:
            continue
        if cls not in seen_classes:
            add_class(cls, module_name, path)
    return (state, fingerprinter)


_MISSING = object()


def changed_keys(base, new):
    """Keys whose fingerprint differs, then keys missing from `new`."""
    changed = [key for key, value in new.items() if base.get(key, _MISSING) != value]
    changed += [key for key in base if key not in new]
    return changed


def changed_keys_within(base, new, only):
    """changed_keys, counting removed keys only for modules in `only`."""
    changed = [key for key, value in new.items() if base.get(key, _MISSING) != value]
    changed += [key for key in base if key[1] in only and key not in new]
    return changed
