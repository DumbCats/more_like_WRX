"""Entry-level diffs of sqlglot dict/set registries and the tests that read those entries.

When a patch changes a module-level dict or set (a "holder", e.g. a dialect's
function table), the key map (tables/keymap.bin) tells which tests read which
entries of it. A holder is named by its state-fingerprint key joined with '|';
its entries by `stable_key()`, plus WHOLE_CONTAINER for "reads the whole thing",
'#len' and '#keys' for length and key-order changes.
"""
from __future__ import annotations

import array
import collections
import enum
import marshal
import numbers
import os
import types
import zlib

WHOLE_CONTAINER = '*'
LENGTH_ENTRY = '#len'
KEY_ORDER_ENTRY = '#keys'


def stable_key(key):
    """A process-independent string naming a dict key or set member."""
    try:
        if isinstance(key, str):
            return 's:' + str.__add__(key, '')
        if isinstance(key, numbers.Number):
            try:
                as_int = int(key)
                if as_int == key:
                    return 'n:%d' % as_int
            except (TypeError, ValueError, OverflowError):
                pass
            return 'n:' + repr(complex(key) if isinstance(key, complex) else float(key))
        if key is None:
            return 'N'
        if isinstance(key, bytes):
            return 'b:' + repr(bytes(key))
        if isinstance(key, tuple):
            return '(' + ','.join((stable_key(item) for item in key)) + ')'
        if isinstance(key, (frozenset, set)):
            return '{' + ','.join(sorted((stable_key(item) for item in key))) + '}'
        if isinstance(key, type):
            return 'C:%s.%s' % (key.__module__, key.__qualname__)
        if isinstance(key, enum.Enum):
            enum_class = type(key)
            return 'E:%s.%s.%s' % (enum_class.__module__, enum_class.__qualname__, key.name)
        if isinstance(key, types.FunctionType):
            return 'F:%s.%s' % (key.__module__, key.__qualname__)
    except Exception:
        pass
    key_type = type(key)
    return 'T:%s.%s' % (getattr(key_type, '__module__', '?'), getattr(key_type, '__qualname__', '?'))


def holder_name(key):
    """The key-map name of a holder given its state-fingerprint key tuple."""
    return '|'.join((str(part) for part in key))


def changed_container_keys(old, new, fingerprint_old, fingerprint_new):
    """Stable keys whose entries differ between two dicts/sets (None for other types)."""
    if type(old) is not type(new) or type(old) not in (dict, set, frozenset):
        return None
    changed = set()
    if type(old) is dict:
        old_counts, new_counts = (collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter))
        old_order, new_order = ([], [])
        for key, value in old.items():
            name = stable_key(key)
            old_order.append(name)
            old_counts[name][fingerprint_old(value)] += 1
        for key, value in new.items():
            name = stable_key(key)
            new_order.append(name)
            new_counts[name][fingerprint_new(value)] += 1
        for name in set(old_counts) | set(new_counts):
            if old_counts.get(name) != new_counts.get(name):
                changed.add(name)
        if old_order != new_order:
            changed.add(KEY_ORDER_ENTRY)
    else:
        old_counts, new_counts = (collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter))
        for member in old:
            old_counts[stable_key(member)][fingerprint_old(member)] += 1
        for member in new:
            new_counts[stable_key(member)][fingerprint_new(member)] += 1
        for name in set(old_counts) | set(new_counts):
            if old_counts.get(name) != new_counts.get(name):
                changed.add(name)
        if changed:
            changed.add(KEY_ORDER_ENTRY)
    if len(old) != len(new):
        changed.add(LENGTH_ENTRY)
    return changed


def changed_entry_rows(changed, base_objects, base_state, new_objects, new_fingerprinter, make_fingerprinter):
    """For each changed holder key, the sorted list of its changed entries.

    Holders whose base object no longer matches the saved base fingerprint, or
    whose type changed, are left out.
    """
    rows = {}
    base_fingerprinter = None
    for key in changed:
        base_object = base_objects.get(key)
        new_object = new_objects.get(key)
        if base_object is None or new_object is None or type(base_object) is not type(new_object):
            continue
        if base_fingerprinter is None:
            base_fingerprinter = make_fingerprinter()
        try:
            if base_fingerprinter.fingerprint(base_object) != base_state.get(key):
                continue
            entries = changed_container_keys(base_object, new_object, base_fingerprinter.fingerprint, new_fingerprinter.fingerprint)
        except Exception:
            continue
        if entries is not None:
            rows[key] = sorted(entries)
    return rows


class KeyMap:
    """The decoded keymap.bin: holder name -> entry table -> test-number sets."""

    def __init__(self, path):
        with open(path, 'rb') as handle:
            data = marshal.loads(zlib.decompress(handle.read()))
        self.holders = data['holders']
        self.tables = data['tables']
        self.sets = data['sets']
        unknown = array.array('H')
        unknown.frombytes(data['unknown'])
        self.unknown = set(unknown)

    def tests(self, set_id):
        """The test numbers of one stored set."""
        numbers = array.array('H')
        numbers.frombytes(self.sets[set_id])
        return numbers

    def readers(self, rows):
        """Tests reading any of the (holder key, entries) rows; None if a holder is unmapped."""
        readers = set(self.unknown)
        for holder_key, entries in rows:
            if entries is None:
                return None
            table_id = self.holders.get(holder_name(holder_key))
            if table_id is None:
                return None
            table = self.tables[table_id]
            for entry in [WHOLE_CONTAINER] + list(entries):
                set_id = table.get(entry)
                if set_id is not None:
                    readers.update(self.tests(set_id))
        return readers


_KEY_MAP = [None]


def load_key_map(tables_dir):
    """The KeyMap of `tables_dir` (loaded once), or None when it is missing or broken."""
    if _KEY_MAP[0] is None:
        try:
            _KEY_MAP[0] = KeyMap(os.path.join(tables_dir, 'keymap.bin'))
        except (OSError, ValueError, KeyError, EOFError, zlib.error):
            _KEY_MAP[0] = False
    return _KEY_MAP[0] or None


def _only_registry_edits(plan, root):
    """True when the plan changes only data (no code swaps, statements, removals, def lines)."""
    if plan.fpairs or plan.stmt_lines or plan.gone or plan.work:
        return False
    for rel_path, line_numbers in plan.def_units.items():
        try:
            with open(os.path.join(root, rel_path), encoding='utf-8') as handle:
                lines = handle.read().split('\n')
        except OSError:
            return False
        for line_number in line_numbers:
            if 0 < line_number <= len(lines) and lines[line_number - 1].lstrip().startswith(('def ', 'async def ', '@', 'class ')):
                return False
    return True


def tests_reading_changed_entries(plan, rows, tables_dir, root):
    """Tests that read the changed registry entries of `rows`, or None when unknown.

    `rows` are [holder key, is_function, entries] as sent by the fresh child.
    """
    if not rows or not _only_registry_edits(plan, root):
        return None
    if any((len(row) < 3 or row[1] or row[2] is None for row in rows)):
        return None
    key_map = load_key_map(tables_dir)
    if key_map is None:
        return None
    return key_map.readers([(row[0], row[2]) for row in rows])
