"""Stage gate for the TPC-DS executor test (tests/test_executor.py::test_execute_tpcds).

The test parses each fixture query, and for the executed ones compares the rows of sqlglot's
Python executor (optimize -> plan -> execute) with DuckDB running the query transpiled to the
duckdb dialect. On the pinned tree it passes, so on a patched tree it still passes when, for
every executed query, the duckdb SQL is unchanged (DuckDB gives the same frame) and the
executor returns exactly the pinned rows; and when parsing every fixture query still works.

The build step (build/tpcds_stages.py) records per query the digests of the parsed tree, the
optimized tree, the duckdb SQL and the executor's rows, plus the sqlglot functions that run
outside the parse/optimize/transpile stages ("downstream": import time, table loading,
pickling, planning, executing). Two ways to check a patch, both in a forked child that imports
the patched tree:
  * safe edits (only bodies of functions that are never downstream): recompute the stage
    digests; queries whose parsed and optimized trees are unchanged need no execution.
  * other edits: execute the queries (no DuckDB, no pandas) and compare the rows' digests.
Any difference, error or timeout means "keep the test".
"""
import ast
import csv
import gzip
import hashlib
import json
import multiprocessing
import os
import pickle
import signal
import sys
import tempfile
from pathlib import Path

import tpcds_profile

FIXTURE = 'optimizer/tpc-ds/tpc-ds.sql'
RESULT_FILE = 'tpcds-stages-result.json'
TABLE_MODULE = 'sqlglot/executor/table.py'
_CLOSE = object()


def load(path):
    """The stage profile ({'downstream': {path: set of lines}, 'digests': [...]}) or None."""
    try:
        with open(path, encoding='utf8') as handle:
            stages = json.load(handle)
        stages['downstream'] = {file: set(lines) for file, lines in stages['downstream'].items()}
        if len(stages['digests']) != len(stages['rows']):
            return None
        return stages
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _hash(text):
    return hashlib.blake2b(text.encode('utf8', 'surrogatepass'), digest_size=16).hexdigest()


def tree_digest(root, node_type):
    """A digest of an expression tree: node classes, args, types, comments and metadata."""
    parts = []
    stack = [root]
    while stack:
        value = stack.pop()
        if value is _CLOSE:
            parts.append(')')
        elif isinstance(value, node_type):
            meta = value._meta
            parts.append('(%s|%r|%r' % (type(value).__name__, value.comments, sorted(meta.items(), key=repr) if meta else None))
            stack.append(_CLOSE)
            if value._type is not None:
                stack.append(value._type)
                stack.append('type')
            for key, child in reversed(list(value.args.items())):
                stack.append(child)
                stack.append('=' + key)
        elif type(value) is list:
            parts.append('[')
            stack.append(_CLOSE)
            stack.extend(reversed(value))
        else:
            parts.append(repr(value))
    return _hash('\x00'.join(parts))


def rows_digest(table):
    """A digest of an executor result as the test reads it (columns and row tuples)."""
    return _hash(repr((tuple(table.columns), [tuple(row) for row in table.rows])))


def stage_digests(sqls, schema, optimized=True):
    """Per query: [parsed digest] or [parsed, optimized, duckdb sql] digests for executed ones.

    Runs on whichever sqlglot is importable; a query whose stages raise gets 'error' entries.
    Without optimized, the optimized digest is None (the caller executes the query anyway).
    """
    from sqlglot import parse_one, transpile
    from sqlglot.expressions import Expr
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema

    rows = []
    for meta, sql, _expected in sqls:
        try:
            parsed = parse_one(sql)
            row = [tree_digest(parsed, Expr)]
            if meta.get('execute'):
                # as in mp_execute -> execute(): the tree arrives pickled, schema is a dict
                if optimized:
                    tree = optimize(pickle.loads(pickle.dumps(parsed)), ensure_schema(schema, dialect=None), leave_tables_isolated=True, dialect=None)
                    row.append(tree_digest(tree, Expr))
                else:
                    row.append(None)
                row.append(_hash(transpile(sql, write='duckdb')[0]))
        except Exception:
            row = ['error', 'error', 'error'] if meta.get('execute') else ['error']
        rows.append(row)
    return rows


def load_tables(directory, schema):
    """The executor tables of a TPC fixture directory, read like TestExecutor.setUpClass does."""
    from sqlglot.executor.table import Table

    tables = {}
    for table, columns in schema.items():
        file_name = f'{directory}{table}.csv.gz'
        with open(file_name, 'rb') as handle:
            gzipped = handle.read(2) == b'\x1f\x8b'
        source = gzip.open(file_name, 'rt', newline='') if gzipped else open(file_name, encoding='utf-8', newline='')
        with source:
            reader = csv.reader(source, delimiter='|')
            rows = []
            ctypes = []
            next(reader)
            for row in reader:
                if not ctypes:
                    for value in row:
                        try:
                            ctypes.append(type(ast.literal_eval(value)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(tuple(None if (kind is not str and value == '') else kind(value) for kind, value in zip(ctypes, row)))
        tables[table] = Table(columns=columns, rows=rows)
    return tables


def execute_query(expression, meta, schema, tables):
    """mp_execute of the test: the executor's result table for one query (None if not executed)."""
    from sqlglot import find_tables
    from sqlglot.executor import execute

    if not meta.get('execute'):
        return None
    return execute(expression, schema=schema, tables={found.name: tables[found.name] for found in find_tables(expression)})


def downstream_safe(files, stages):
    """Whether every edit is confined to bodies of functions that do not run downstream."""
    downstream = stages['downstream']
    for path, base, revised, *_lines in files:
        if not (path.startswith('sqlglot/') and path.endswith('.py')):
            return False
        edited = tpcds_profile.edited_bodies(base, revised)
        if edited is None:
            return False
        lines = downstream.get(path, ())
        for func in edited:
            first = min([func.lineno] + [decorator.lineno for decorator in func.decorator_list])
            if any(first <= number <= func.end_lineno for number in lines):
                return False
    return True


def still_passes(source, files, stages, safe, cpu, seconds, tables_file=None):
    """Whether the test provably still passes on the patched tree.

    cpu: per-query executor seconds (for the per-query time limits); seconds: overall limit;
    tables_file: the TPC-DS tables pickled at build time (rebuilt from the csv files if None).
    Returns (verdict, number of queries executed); verdict is 'pass', 'fail' (or unsure) or
    'duckdb' (the duckdb SQL of an executed query changed, which this check cannot decide).
    """
    from replay import overlay

    try:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            overlay(source, root, {path: revised for path, _base, revised, *_lines in files})
            paths = [path for path, *_rest in files]
            child = multiprocessing.get_context('fork').Process(target=_check_in_child, args=(root, paths, stages, safe, cpu, tables_file))
            child.start()
            child.join(seconds)
            if child.is_alive():
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    child.kill()
                child.join()
            if child.exitcode != 0:
                return 'fail', -1
            result = json.loads((root / RESULT_FILE).read_text())
            return result['verdict'], result['executed']
    except (OSError, ValueError, KeyError):
        return 'fail', -1


class _Timeout(Exception):
    pass


def _check_in_child(root, paths, stages, safe, cpu, tables_file):
    """Child process: import the patched tree from root, check, write the verdict."""
    os.setsid()
    with open(os.devnull, 'wb') as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    os.chdir(root)
    for name in [module for module in sys.modules if module.split('.')[0] in ('sqlglot', 'tests')]:
        del sys.modules[name]
    sys.path.insert(0, str(root))
    sys.dont_write_bytecode = True
    os.environ.update(SKIP_INTEGRATION='0', PYTHONDONTWRITEBYTECODE='1')
    verdict, executed = _check(root, paths, stages, safe, cpu, tables_file)
    for path in paths:
        module = sys.modules.get(path[:-3].replace('/', '.').removesuffix('.__init__'))
        filename = getattr(module, '__file__', None) if module is not None else None
        if filename and Path(filename).resolve() != (root / path).resolve():
            os._exit(1)  # the original module was loaded instead of the patched one
    (root / RESULT_FILE).write_text(json.dumps({'verdict': verdict, 'executed': executed}))
    os._exit(0)


def _check(root, paths, stages, safe, cpu, tables_file):
    from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    sqls = list(load_sql_fixture_pairs(FIXTURE))
    if len(sqls) != len(stages['digests']):
        return 'fail', 0
    digests = stage_digests(sqls, TPCDS_SCHEMA, optimized=safe)
    pending = []
    for query, ((meta, _sql, _expected), old, new) in enumerate(zip(sqls, stages['digests'], digests)):
        if 'error' in new or len(old) != len(new):
            return 'fail', 0
        if meta.get('execute'):
            if old[2] != new[2]:
                return 'duckdb', 0  # the expected frame changes: needs DuckDB, not checked here
            if not safe or old[:2] != new[:2]:  # (new[1] is None when not safe)
                pending.append(query)
    if not pending:
        return 'pass', 0
    from sqlglot import parse_one

    def expire(*_args):
        raise _Timeout()

    signal.signal(signal.SIGALRM, expire)
    try:
        signal.setitimer(signal.ITIMER_REAL, 30)
        if TABLE_MODULE in paths or not tables_file:
            # the table class changed: build the tables like setUpClass (which also loads TPC-H)
            load_tables(os.path.join(FIXTURES_DIR, 'optimizer/tpc-h/'), TPCH_SCHEMA)
            tables = pickle.loads(pickle.dumps(load_tables(os.path.join(FIXTURES_DIR, 'optimizer/tpc-ds/'), TPCDS_SCHEMA)))
        else:
            with open(tables_file, 'rb') as handle:
                tables = pickle.load(handle)
        signal.setitimer(signal.ITIMER_REAL, 0)
        pending.sort(key=lambda query: cpu[query])
        for count, query in enumerate(pending, 1):
            meta, sql, _expected = sqls[query]
            signal.setitimer(signal.ITIMER_REAL, 3 * cpu[query] + 1.0)
            expression, meta = pickle.loads(pickle.dumps((parse_one(sql), meta)))
            table = pickle.loads(pickle.dumps(execute_query(expression, meta, TPCDS_SCHEMA, tables)))
            signal.setitimer(signal.ITIMER_REAL, 0)
            if rows_digest(table) != stages['rows'][query]:
                return 'fail', count
    except BaseException:
        return 'fail', -1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    return 'pass', len(pending)
