"""Read access to the profile tables built offline (the `tables/` directory).

The tables describe, for the pinned sqlglot checkout and its test suite, which
tests execute which lines, statements and code units, which tests read which
names, files and class attributes, and how long each test (and each part of a
test body) takes. Everything is loaded lazily and cached.

Test sets are stored once in `sets.bin` (arrays of uint16 test numbers indexed
by `sets.idx`); the other tables refer to them by set id (0 = empty set).
"""
import json
import marshal
import os
import zlib
from array import array

import settings

# A per-test unit entry of this value means "the whole test body depends on it".
BASE_PATTERN = 65535


def _load_compressed(path):
    with open(path, 'rb') as handle:
        return marshal.loads(zlib.decompress(handle.read()))


class ProfileTables:
    """Lazy reader of one tables directory.

    `meta` holds: 'files' (rel path -> file id), 'test_ids', 'opened' / 'listed'
    (rel path -> set id of tests reading that file / listing that directory),
    'importers', 'shapes' and 'pool_users'.

    A file record (`file(rel)`) is a list indexed as follows:
      0 code units [(first line, last line, class path, kind)] (kind 0 = function)
      1 compiler flags, 2 names bound by imports,
      3 per line: set id of the tests that execute it,
      4 per line: executed while the module is imported,
      5 / 6 per unit: set id of its tests / executed at import,
      7 / 8 set ids of the tests that need the module's import-time behaviour,
      9 / 10 per line: first / last line of the enclosing statement,
      11 / 12 / 13 import-time readers per line (set id, flag, (file id, line) pairs).
    """

    def __init__(self, path):
        self.path = path
        with open(os.path.join(path, 'meta.bin'), 'rb') as handle:
            self.meta = marshal.load(handle)
        with open(os.path.join(path, 'sets.idx'), 'rb') as handle:
            self.offsets = array('I')
            self.offsets.frombytes(handle.read())
        self.sets_file = open(os.path.join(path, 'sets.bin'), 'rb')
        self.file_cache = {}
        self.set_cache = {}
        self.unit_cache = {}
        self.test_cache = {}
        self._paths_by_id = None
        self._name_readers = None
        self._class_users = None
        self._class_map = None
        self._faithful = None
        self._tight = None
        try:
            with open(os.path.join(path, 'cost.bin'), 'rb') as handle:
                self.cost = marshal.load(handle)
        except OSError:
            self.cost = None

    def file(self, rel):
        """The record of source file `rel` (see the class docstring), or None."""
        record = self.file_cache.get(rel)
        if record is None:
            file_id = self.meta['files'].get(rel)
            if file_id is None:
                return None
            record = self.file_cache[rel] = _load_compressed(os.path.join(self.path, 'f', f'{file_id}.bin'))
        return record

    def unit_entries(self, rel, line):
        """{test number: pattern index (or BASE_PATTERN)} for the tests running line `line` of `rel`."""
        file_id = self.meta['files'].get(rel)
        if file_id is None:
            return {}
        chunk_key = (file_id, line // 256)
        chunk = self.unit_cache.get(chunk_key)
        if chunk is None:
            try:
                chunk = _load_compressed(os.path.join(self.path, 'u', '%d_%d.bin' % chunk_key))
            except OSError:
                chunk = {}
            self.unit_cache[chunk_key] = chunk
        entries = chunk.get(line)
        if entries is None:
            return {}
        if isinstance(entries, bytes):
            pairs = array('H')
            pairs.frombytes(entries)
            entries = chunk[line] = dict(zip(pairs[0::2], pairs[1::2]))
        return entries

    def test_units(self, number):
        """The body profile of test `number`, or None.

        Indexed as: 0 / 1 first and last line of the method, 2 statements
        [(first, last, required)], 3 tasks [(kind 's'|'f'|'t', fixture, index,
        statement)], 4 task patterns (tuples of task indices), 5 total cost,
        6 base cost, 7 cost per task, 8 classes used per task, 9 base classes.
        """
        record = self.test_cache.get(number, 0)
        if record == 0:
            try:
                record = _load_compressed(os.path.join(self.path, 't', '%d.bin' % number))
            except OSError:
                record = None
            self.test_cache[number] = record
        return record

    def readers(self, name):
        """[(rel path, line)] of test-code lines that read attribute/global `name`."""
        if self._name_readers is None:
            try:
                self._name_readers = _load_compressed(os.path.join(self.path, 'readers.bin'))
            except OSError:
                self._name_readers = {}
        packed = self._name_readers.get(name)
        if not packed:
            return ()
        values = array('I')
        values.frombytes(packed)
        paths = self.paths()
        return [(paths[values[position]], values[position + 1]) for position in range(0, len(values), 2)]

    def class_users(self, name):
        """Test numbers using class `module.Class`, or None when unknown."""
        if self._class_users is None:
            try:
                self._class_users = _load_compressed(os.path.join(self.path, 'classes.bin'))
            except OSError:
                self._class_users = {}
        return self._class_users.get(name)

    def classmap(self):
        """The class-attribute reader map (decoded), or None if it is missing or broken."""
        if self._class_map is None:
            try:
                class_map = _load_compressed(os.path.join(self.path, 'classmap.bin'))
                hooked = array('I')
                hooked.frombytes(class_map['hooked'])
                unknown = array('H')
                unknown.frombytes(class_map['unknown'])
                class_map['hooked'] = {class_map['classes'][class_index] for class_index in hooked}
                class_map['unknown'] = set(unknown)
                class_map['fnames'] = set(class_map['fnames'])
                self._class_map = class_map
            except (OSError, ValueError, KeyError, EOFError, zlib.error):
                self._class_map = False
        return self._class_map or None

    def tight_lines(self):
        """{test number: {rel path: frozenset of lines it executes}} for tests profiled per line."""
        if self._tight is None:
            self._tight = {}
            if settings.TIGHT_LINES:
                try:
                    packed_tests = _load_compressed(os.path.join(self.path, 'tight_lines.bin'))
                    for number, packed_files in packed_tests.items():
                        lines_by_file = self._tight[number] = {}
                        for rel, packed in packed_files.items():
                            lines = array('I')
                            lines.frombytes(packed)
                            lines_by_file[rel] = frozenset(lines)
                except (OSError, ValueError, EOFError, zlib.error):
                    self._tight = {}
        return self._tight

    def faithful(self):
        """Test numbers whose reduced runs may reuse the original method code."""
        if self._faithful is None:
            try:
                with open(os.path.join(self.path, 'faithful.json')) as handle:
                    self._faithful = frozenset(json.load(handle))
            except (OSError, ValueError):
                self._faithful = frozenset()
        return self._faithful

    def paths(self):
        """file id -> rel path."""
        if self._paths_by_id is None:
            self._paths_by_id = {file_id: rel for rel, file_id in self.meta['files'].items()}
        return self._paths_by_id

    def tests(self, set_id):
        """The test numbers of set `set_id` (an array, or () for set 0)."""
        if set_id == 0:
            return ()
        numbers = self.set_cache.get(set_id)
        if numbers is None:
            start, end = (self.offsets[set_id], self.offsets[set_id + 1])
            self.sets_file.seek(start)
            numbers = array('H')
            numbers.frombytes(self.sets_file.read(end - start))
            self.set_cache[set_id] = numbers
        return numbers
