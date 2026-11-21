"""The process environment sqlglot's test suite runs in, and shared class fixtures.

- `install_environment` prepares a process to import and run sqlglot's tests
  (cwd, sys.path, inline process pool, strict logging).
- Import helpers report modules that fail to import.
- Shared class fixtures: TestOptimizer / TestExecutor do expensive work in
  setUpClass. `build_class_fixtures` runs it once in the coordinator (or, for
  TestExecutor, installs lazily built attributes backed by a row cache built at
  image build time, see build/executor_rows.py) and records which fixture files
  it read, so children can refresh only what a patch changes.
"""
from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import sys


class _StrictLogHandler(logging.Handler):
    """Format every record (as a real handler would) and let formatting errors raise."""

    def emit(self, record):
        record.getMessage()

    def handleError(self, record):
        raise


def force_single_thread_duckdb() -> None:
    """Make duckdb.connect default to one thread (idempotent; no-op without duckdb)."""
    try:
        import duckdb
    except ImportError:
        return
    real_connect = duckdb.connect
    if getattr(real_connect, '_blast', False):
        return

    def connect(*args, **kwargs):
        config = dict(kwargs.pop('config', None) or {})
        config.setdefault('threads', 1)
        return real_connect(*args, config=config, **kwargs)
    connect._blast = True
    duckdb.connect = connect


def install_environment(root: str) -> None:
    """Prepare this process to import and run the tests of the checkout at `root`."""
    import unittest
    import inline_pool
    os.environ['SKIP_INTEGRATION'] = '0'
    os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)
    inline_pool.install()
    logging.getLogger().addHandler(_StrictLogHandler(level=0))


def import_sqlglot() -> None:
    """Import every sqlglot module (except __main__)."""
    import sqlglot
    for info in pkgutil.walk_packages(sqlglot.__path__, 'sqlglot.'):
        if info.name.endswith('__main__'):
            continue
        importlib.import_module(info.name)


def broken_modules(test_modules=()) -> list:
    """Sorted names of sqlglot modules and `test_modules` that fail to import."""
    broken = []
    try:
        import sqlglot
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt):
            raise
        return ['sqlglot']
    for info in pkgutil.walk_packages(sqlglot.__path__, 'sqlglot.', onerror=lambda name: broken.append(name)):
        if info.name.endswith('__main__'):
            continue
        try:
            importlib.import_module(info.name)
        except BaseException as error:
            if isinstance(error, KeyboardInterrupt):
                raise
            broken.append(info.name)
    broken.extend(broken_test_modules(test_modules))
    return sorted(set(broken))


def broken_test_modules(names) -> list:
    """Test modules among `names` that fail to import (a SkipTest is not a failure)."""
    import unittest
    broken = []
    for name in names:
        try:
            importlib.import_module(name)
        except unittest.SkipTest:
            pass
        except BaseException as error:
            if isinstance(error, KeyboardInterrupt):
                raise
            broken.append(name)
    return broken


def import_tests(modules) -> None:
    """Import the given test modules, then pin duckdb to one thread."""
    for name in modules:
        importlib.import_module(name)
    force_single_thread_duckdb()


# --- TestExecutor class fixtures ----------------------------------------------------

SHARED_FIXTURES = (('tests.test_optimizer', 'TestOptimizer'), ('tests.test_executor', 'TestExecutor'))
EXEC_KEY = ('tests.test_executor', 'TestExecutor')
EXEC_DATA_PREFIXES = ('tests/fixtures/optimizer/tpc-h/', 'tests/fixtures/optimizer/tpc-ds/')
# Directory of the executor row cache; read at call time (build/executor_rows.py reassigns it).
EXEC_CACHE = os.environ.get('BLAST_EXEC_CACHE', '/predictor/cache')
EXEC_STATS = {'built': []}
EXEC_SQL_FIXTURES = {'tpch': 'optimizer/tpc-h/tpc-h.sql', 'tpcds': 'optimizer/tpc-ds/tpc-ds.sql'}
# TestExecutor class attributes that setUpClass builds (made lazy by setup_exec_lazy).
EXEC_LAZY = ('tpch_conn', 'tpcds_conn', 'tpch_tables', 'tpcds_tables', 'tpch_sqls', 'tpcds_sqls')


def exec_rows_from_csv(module, file_name):
    """Rows of a TPC csv.gz exactly as TestExecutor.setUpClass parses them."""
    import ast
    import csv
    reader = csv.reader(module.open_file(file_name), delimiter='|')
    rows = []
    column_types = []
    next(reader)
    for row in reader:
        if not column_types:
            for value in row:
                try:
                    column_types.append(type(ast.literal_eval(value)))
                except (ValueError, SyntaxError):
                    column_types.append(str)
        rows.append(tuple((None if column_type is not str and value == '' else column_type(value) for column_type, value in zip(column_types, row))))
    return rows


def exec_schema(module, dataset):
    """(schema dict, csv directory) of 'tpch' or 'tpcds' in tests.test_executor."""
    if dataset == 'tpch':
        return (module.TPCH_SCHEMA, module.DIR_TPCH)
    return (module.TPCDS_SCHEMA, module.DIR_TPCDS)


def cached_exec_rows(module, dataset):
    """A `read_rows(table)` function backed by the row cache, or None when it is stale.

    The cache is valid when it lists the schema's tables in order and every
    source csv.gz still has the recorded size.
    """
    import marshal
    archive = os.path.join(EXEC_CACHE, f'executor-{dataset}')
    schema, directory = exec_schema(module, dataset)
    try:
        with open(archive + '.index', 'rb') as index_file:
            entries = marshal.load(index_file)
        blob_size = os.path.getsize(archive + '.rows')
        if list(entries) != list(schema):
            return None
        for table, (source_size, offset, length) in entries.items():
            if offset < 0 or length <= 0 or offset + length > blob_size:
                return None
            if os.path.getsize(f'{directory}{table}.csv.gz') != source_size:
                return None
    except (OSError, ValueError, EOFError, TypeError):
        return None

    def read_rows(table):
        _, offset, length = entries[table]
        try:
            with open(archive + '.rows', 'rb') as rows_file:
                rows_file.seek(offset)
                rows = marshal.loads(rows_file.read(length))
            if type(rows) is list:
                return rows
        except (OSError, ValueError, EOFError, TypeError):
            pass
        return exec_rows_from_csv(module, f'{directory}{table}.csv.gz')
    return read_rows


def build_exec_attribute(cls, name):
    """Build one EXEC_LAZY attribute of TestExecutor the way setUpClass would."""
    module = sys.modules[cls.__module__]
    dataset = name.split('_', 1)[0]
    schema, directory = exec_schema(module, dataset)
    if name.endswith('_conn'):
        connection = module.duckdb.connect()
        for table, columns in schema.items():
            file_name = f'{directory}{table}.csv.gz'
            connection.execute(f"\n                CREATE VIEW {table} AS\n                SELECT *\n                FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})\n                ")
        return connection
    if name.endswith('_tables'):
        cached = cached_exec_rows(module, dataset)
        if cached is not None:
            lazy = _LazyTables(schema, lambda table: module.Table(columns=schema[table], rows=cached(table)))
            if EXEC_TABLES_STRICT:
                lazy.load_all()
                return dict(lazy)
            return lazy
        tables = {}
        for table, columns in schema.items():
            tables[table] = module.Table(columns=columns, rows=exec_rows_from_csv(module, f'{directory}{table}.csv.gz'))
        return tables
    fixture = EXEC_SQL_FIXTURES[dataset]
    try:
        from reduction import STATE
    except ImportError:
        STATE = None
    saved_in_test = STATE.in_test if STATE is not None else None
    if STATE is not None:
        STATE.in_test = False
    try:
        return list(module.load_sql_fixture_pairs(fixture))
    finally:
        if STATE is not None:
            STATE.in_test = saved_in_test


def _loading_first(method_name):
    """A dict method for _LazyTables that builds every table before delegating."""
    plain_method = getattr(dict, method_name)

    def method(self, *args, **kwargs):
        self.load_all()
        return plain_method(self, *args, **kwargs)
    method.__name__ = method_name
    return method


_WHOLE_DICT_METHODS = ('__iter__', '__len__', '__contains__', '__eq__', '__ne__', '__repr__', '__reversed__', '__or__', '__ror__', '__ior__', '__setitem__', '__delitem__', '__sizeof__', 'keys', 'values', 'items', 'get', 'copy', 'pop', 'popitem', 'setdefault', 'update', 'clear')


class _LazyTables(dict):
    """A table-name -> Table dict that builds each table on first `[name]` access.

    Any operation that needs the whole dict builds every table first; pickling
    produces a plain dict.
    """
    __slots__ = ('_names', '_build', '_full')

    def __init__(self, names, build):
        super().__init__()
        self._names = tuple(names)
        self._build = build
        self._full = False

    def __missing__(self, key):
        if self._full or key not in self._names:
            raise KeyError(key)
        table = self._build(key)
        dict.__setitem__(self, key, table)
        return table

    def load_all(self):
        if not self._full:
            built = {name: dict.__getitem__(self, name) for name in dict.keys(self)}
            dict.clear(self)
            for name in self._names:
                dict.__setitem__(self, name, built[name] if name in built else self._build(name))
            self._full = True
        return self
    for _method_name in _WHOLE_DICT_METHODS:
        locals()[_method_name] = _loading_first(_method_name)
    del _method_name

    def __reduce_ex__(self, protocol):
        return (dict, (dict(self.load_all()),))


# Set by make_exec_tables_strict(): build plain dicts instead of _LazyTables.
EXEC_TABLES_STRICT = False


def make_exec_tables_strict():
    """Switch to fully built table dicts (for children whose patch may touch the data)."""
    global EXEC_TABLES_STRICT
    EXEC_TABLES_STRICT = True
    cls = getattr(sys.modules.get(EXEC_KEY[0]), EXEC_KEY[1], None)
    for name in ('tpch_tables', 'tpcds_tables'):
        value = None if cls is None else cls.__dict__.get(name)
        if isinstance(value, _LazyTables):
            setattr(cls, name, dict(value.load_all()))


class _LazyClassAttribute:
    """Descriptor that builds an EXEC_LAZY attribute on first access and caches it."""

    def __init__(self, name):
        self.name = name

    def __get__(self, obj, owner):
        import time
        started = time.process_time()
        value = build_exec_attribute(owner, self.name)
        setattr(owner, self.name, value)
        EXEC_STATS['built'].append('%s:%.3f' % (self.name, time.process_time() - started))
        return value


def exec_cache_present() -> bool:
    """True when the executor row cache files exist for both datasets."""
    return all((os.path.isfile(os.path.join(EXEC_CACHE, f'executor-{dataset}.{suffix}')) for dataset in EXEC_SQL_FIXTURES for suffix in ('index', 'rows')))


def setup_exec_lazy(cls, prebuild=()):
    """Replace TestExecutor's setUpClass products with lazy attributes (some prebuilt)."""
    module = sys.modules[cls.__module__]
    cls.cache = {}
    for name in EXEC_LAZY:
        setattr(cls, name, _LazyClassAttribute(name))
    for name in prebuild:
        setattr(cls, name, build_exec_attribute(cls, name))
    reads = set()
    fixture_attributes = {}
    for dataset, fixture in EXEC_SQL_FIXTURES.items():
        schema, directory = exec_schema(module, dataset)
        reads.update((os.path.abspath(f'{directory}{table}.csv.gz') for table in schema))
        reads.add(os.path.abspath(os.path.join(module.FIXTURES_DIR, fixture)))
        fixture_attributes[dataset + '_sqls'] = (fixture, 'load_sql_fixture_pairs')
    cls._blast_reads = reads
    cls._blast_attrs = fixture_attributes


def exec_attribute_users(cls) -> dict:
    """Test method name -> the EXEC_LAZY attributes its code mentions."""
    users = {}
    for method_name, function in vars(cls).items():
        code = getattr(function, '__code__', None)
        if not method_name.startswith('test') or code is None:
            continue
        names = set()
        stack = [code]
        while stack:
            current = stack.pop()
            names.update(current.co_names)
            stack.extend((const for const in current.co_consts if isinstance(const, type(code))))
        used = names.intersection(EXEC_LAZY)
        if used:
            users[method_name] = used
    return users


def exec_fresh_ok(changed_paths) -> bool:
    """True when a fresh child may use the lazy executor attributes for this patch."""
    if not exec_cache_present():
        return False
    for path in changed_paths:
        if path.startswith('tests/') and path.endswith('.py'):
            return False
        if path.startswith(EXEC_DATA_PREFIXES) or path.startswith('sqlglot/executor/'):
            return False
    return True


def exec_fresh_hook(module):
    """Import hook for tests.test_executor in fresh children: lazy attributes, no setUpClass."""
    cls = getattr(module, EXEC_KEY[1], None)
    if cls is None or 'setUpClass' not in cls.__dict__:
        return
    setup_exec_lazy(cls)
    cls.setUpClass = classmethod(lambda c: None)
    cls.tearDownClass = classmethod(lambda c: None)


# --- shared class fixtures ------------------------------------------------------------

def _reads_during(function) -> set:
    """Absolute paths opened (via builtins.open/io.open) while calling `function`."""
    import builtins
    import io
    real_open = builtins.open
    seen = set()

    def recording_open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            seen.add(os.path.abspath(os.fspath(file)))
        return real_open(file, *args, **kwargs)
    builtins.open = io.open = recording_open
    try:
        function()
    finally:
        builtins.open = io.open = real_open
    return seen


def _fixture_attributes(cls, reads) -> dict:
    """Class attribute -> (fixture name, loader) for list attributes equal to a loaded fixture."""
    helpers = sys.modules.get('tests.helpers')
    if helpers is None:
        return {}
    fixtures_dir = os.path.abspath(helpers.FIXTURES_DIR) + os.sep
    fixture_names = [path[len(fixtures_dir):] for path in reads if path.startswith(fixtures_dir) and path.endswith('.sql')]
    found = {}
    for attribute, value in list(cls.__dict__.items()):
        if not isinstance(value, list) or not value:
            continue
        for fixture_name in fixture_names:
            for loader in ('load_sql_fixture_pairs', 'load_sql_fixtures'):
                try:
                    if list(getattr(helpers, loader)(fixture_name)) == value:
                        found[attribute] = (fixture_name, loader)
                except Exception:
                    pass
    return found


def refresh_class_fixtures(root: str, files) -> list:
    """Reload the shared fixture attributes whose files the patch changes.

    Returns [(module name, failed)] for each shared class affected. A class
    whose setUpClass read some other changed file gets a failing setUpClass.
    The class state before the refresh is kept in `_blast_parent_state`.
    """
    changed = {os.path.join(root, rel) for rel in files}
    helpers = sys.modules.get('tests.helpers')
    refreshed = []
    for module_name, class_name in SHARED_FIXTURES:
        module = sys.modules.get(module_name)
        cls = getattr(module, class_name, None) if module else None
        reads = getattr(cls, '_blast_reads', None) if cls is not None else None
        if not reads or not reads & changed:
            continue
        attributes = cls._blast_attrs
        fixtures_dir = os.path.abspath(helpers.FIXTURES_DIR) + os.sep
        known = {fixtures_dir + fixture_name for fixture_name, _ in attributes.values()}
        error = None
        if (reads & changed) - known:
            error = RuntimeError('class fixture read a changed file: ' + ', '.join(sorted((reads & changed) - known)))
        cls._blast_parent_state = dict(vars(cls))
        for attribute, (fixture_name, loader) in attributes.items():
            if fixtures_dir + fixture_name in changed and error is None:
                try:
                    setattr(cls, attribute, list(getattr(helpers, loader)(fixture_name)))
                except Exception as load_error:
                    error = load_error
        if error is not None:

            def fail(c, error=error):
                raise error
            cls.setUpClass = classmethod(fail)
        refreshed.append((module_name, error is not None))
    return refreshed


def reduce_class_fixtures(changed) -> None:
    """Keep only the changed entries (reduction.changed_fixture_entries) in shared fixture lists."""
    for module_name, class_name in SHARED_FIXTURES:
        module = sys.modules.get(module_name)
        cls = getattr(module, class_name, None) if module else None
        attributes = getattr(cls, '_blast_attrs', None) if cls is not None else None
        for attribute, (fixture_name, loader) in (attributes or {}).items():
            keep = changed.get(('p' if loader == 'load_sql_fixture_pairs' else 'x', fixture_name))
            if keep is not None:
                setattr(cls, attribute, [entry for index, entry in enumerate(getattr(cls, attribute)) if index in keep])


def shared_class_info(root: str) -> list:
    """[(test id prefix, class, {fixture rel path: attribute}, other read rel paths)]."""
    info = []
    helpers = sys.modules.get('tests.helpers')
    for module_name, class_name in SHARED_FIXTURES:
        module = sys.modules.get(module_name)
        cls = getattr(module, class_name, None) if module else None
        reads = getattr(cls, '_blast_reads', None) if cls is not None else None
        if not reads or helpers is None:
            continue
        fixtures_dir = os.path.abspath(helpers.FIXTURES_DIR) + os.sep
        attribute_by_path = {os.path.relpath(fixtures_dir + fixture_name, root): attribute for attribute, (fixture_name, _) in cls._blast_attrs.items()}
        other_reads = {os.path.relpath(path, root) for path in reads} - set(attribute_by_path)
        info.append((module_name.replace('.', '/') + '.py::' + class_name + '::', cls, attribute_by_path, other_reads))
    return info


def build_class_fixtures(wanted, exec_lazy=False, exec_prebuild=()) -> list:
    """Run setUpClass once for the `wanted` shared classes and neutralise it.

    With `exec_lazy` and a present row cache, TestExecutor gets lazy attributes
    instead (building `exec_prebuild` now). Returns the (module, class) pairs done.
    """
    done = []
    for module_name, class_name in SHARED_FIXTURES:
        if (module_name, class_name) not in wanted:
            continue
        module = sys.modules.get(module_name)
        cls = getattr(module, class_name, None) if module else None
        if cls is None:
            continue
        if exec_lazy and (module_name, class_name) == EXEC_KEY and exec_cache_present():
            setup_exec_lazy(cls, exec_prebuild)
        else:
            reads = _reads_during(cls.setUpClass)
            cls._blast_reads = reads
            cls._blast_attrs = _fixture_attributes(cls, reads)
        cls.setUpClass = classmethod(lambda c: None)
        cls.tearDownClass = classmethod(lambda c: None)
        done.append((module_name, class_name))
    return done
