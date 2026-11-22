"""Build generated/tpcds-stages.json, the profile behind the TPC-DS stage gate (../tpcds_stages.py).

Usage: tpcds_stages.py <repo> <output.json>

Records, on the pinned tree:
  digests     per TPC-DS fixture query, the digests of its parsed tree and (for executed queries)
              of its optimized tree and its duckdb transpilation
  rows        per executed query, the digest of the executor's result (columns and rows)
  cpu         per query, the CPU seconds of pickling, planning and executing it
  downstream  file -> first lines of every sqlglot function that runs while sqlglot and the test
              modules are imported, during TestExecutor.setUpClass, and while the queries are
              pickled, planned and executed (everything except parse/optimize/transpile)

The digests are then recomputed in a fresh interpreter; if they differ (nondeterminism), no
profile is written and the gate stays off. Exits 0 either way.
"""
import importlib
import json
import os
import pickle
import pkgutil
import subprocess
import sys
import time
from pathlib import Path

PREFILTER_DIR = Path(__file__).resolve().parent.parent


def main(repo, output):
    repo = str(Path(repo).resolve())
    os.chdir(repo)
    os.environ['SKIP_INTEGRATION'] = '0'
    sys.path[:0] = [repo, str(PREFILTER_DIR)]
    import tpcds_stages

    if len(sys.argv) > 3 and sys.argv[3] == '--digests':
        from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

        print(json.dumps(tpcds_stages.stage_digests(list(load_sql_fixture_pairs(tpcds_stages.FIXTURE)), TPCDS_SCHEMA)))
        return

    monitoring = sys.monitoring
    tool = monitoring.PROFILER_ID
    monitoring.use_tool_id(tool, 'tpcds-stages')
    seen = set()

    def started(code, _offset):
        seen.add((code.co_filename, code.co_firstlineno))
        return monitoring.DISABLE

    monitoring.register_callback(tool, monitoring.events.PY_START, started)

    # downstream, part 1: import time of every sqlglot module and of the test modules
    monitoring.set_events(tool, monitoring.events.PY_START)
    import sqlglot

    for module in pkgutil.walk_packages(sqlglot.__path__, 'sqlglot.'):
        try:
            importlib.import_module(module.name)
        except Exception:
            pass
    import tests.test_executor as test_executor

    monitoring.set_events(tool, 0)

    # upstream (not traced): parse, optimize and transpile every query
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema

    cls = test_executor.TestExecutor
    schema = test_executor.TPCDS_SCHEMA
    sqls = list(test_executor.load_sql_fixture_pairs(tpcds_stages.FIXTURE))
    digests = tpcds_stages.stage_digests(sqls, schema)
    parsed = [test_executor.parse_one(sql) for _meta, sql, _expected in sqls]
    optimized = [
        optimize(pickle.loads(pickle.dumps(tree)), ensure_schema(schema, dialect=None), leave_tables_isolated=True, dialect=None) if meta.get('execute') else None
        for tree, (meta, _sql, _expected) in zip(parsed, sqls)
    ]

    # downstream, part 2: the test's own work around the stages
    executor = sys.modules['sqlglot.executor']
    pending = []
    executor.optimize = lambda *_args, **_kwargs: pending.pop()
    monitoring.set_events(tool, monitoring.events.PY_START)
    cls.setUpClass()
    test_executor.initializer(schema, pickle.loads(pickle.dumps(cls.tpcds_tables)))
    rows, cpu = [], []
    for tree, tree_optimized, (meta, _sql, _expected) in zip(parsed, optimized, sqls):
        started = time.process_time()
        expression, args = pickle.loads(pickle.dumps((tree, meta)))
        if tree_optimized is not None:
            pending.append(tree_optimized)
        table = test_executor.mp_execute(expression, args)
        table = pickle.loads(pickle.dumps(table))
        rows.append(None if table is None else tpcds_stages.rows_digest(table))
        cpu.append(round(time.process_time() - started, 4))
    monitoring.set_events(tool, 0)
    monitoring.free_tool_id(tool)
    cls.tearDownClass()

    prefix = os.path.join(repo, 'sqlglot') + os.sep
    downstream = {}
    for filename, line in seen:
        if filename.startswith(prefix):
            downstream.setdefault(os.path.relpath(filename, repo), []).append(line)
    if any(row[0] == 'error' or 'error' in row for row in digests):
        print('tpcds stages: a query failed on the pinned tree; gate off', file=sys.stderr)
        return
    again = subprocess.run([sys.executable, __file__, repo, output, '--digests'], capture_output=True, text=True, check=False)
    if again.returncode != 0 or json.loads(again.stdout or 'null') != digests:
        print('tpcds stages: digests are not reproducible; gate off', file=sys.stderr)
        return
    tables_file = str(Path(output).with_name('tpcds-tables.pickle'))
    with open(tables_file, 'wb') as handle:
        pickle.dump(tpcds_stages.load_tables(os.path.join(test_executor.FIXTURES_DIR, 'optimizer/tpc-ds/'), schema), handle, protocol=pickle.HIGHEST_PROTOCOL)
    stages = {'digests': digests, 'rows': rows, 'cpu': cpu, 'tables': tables_file, 'downstream': {file: sorted(lines) for file, lines in sorted(downstream.items())}}
    # self-test: the full (execute everything) check must pass on the unpatched tree
    stages_loaded = json.loads(json.dumps(stages))
    stages_loaded['downstream'] = {file: set(lines) for file, lines in stages_loaded['downstream'].items()}
    started = time.monotonic()
    verdict, executed = tpcds_stages.still_passes(Path(repo), [], stages_loaded, False, [3 * seconds + 1 for seconds in cpu], 120, tables_file)
    if verdict != 'pass' or executed != sum(1 for row in rows if row is not None):
        print(f'tpcds stages: self-test failed ({verdict}, {executed}); gate off', file=sys.stderr)
        return
    print(f'tpcds stages: self-test executed {executed} queries in {time.monotonic() - started:.1f} s', file=sys.stderr)
    Path(output).write_text(json.dumps(stages))
    print(f'tpcds stages: {len(digests)} queries, {sum(map(len, downstream.values()))} downstream functions in {len(downstream)} files', file=sys.stderr)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
