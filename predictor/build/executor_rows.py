"""Build the TestExecutor row cache: executor_rows.py <repo> <cache dir> [--check].

Runs TestExecutor.setUpClass of the pinned checkout once and stores, per
dataset (tpch, tpcds), the parsed rows of every table:
  executor-<dataset>.rows   marshalled row lists, back to back
  executor-<dataset>.index  table -> (source csv.gz size, offset, length)
With --check, every lazily built attribute (harness_env.EXEC_LAZY) is then
rebuilt through the cache and compared with what setUpClass produced; the exit
status is 1 when any differs (the image build then drops the cache).
"""
import marshal
import os
import sys

PREDICTOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PREDICTOR_DIR)


def typed(rows):
    """Rows with each value as (type, repr), for an exact comparison."""
    return [tuple(((type(value), repr(value)) for value in row)) for row in rows]


def write_cache(tables, schema, directory, out_dir, dataset):
    archive = os.path.join(out_dir, f'executor-{dataset}')
    entries = {}
    with open(archive + '.rows.tmp', 'wb') as row_file:
        for table_name in schema:
            source_size = os.path.getsize(f'{directory}{table_name}.csv.gz')
            offset = row_file.tell()
            marshal.dump(tables[table_name].rows, row_file)
            entries[table_name] = (source_size, offset, row_file.tell() - offset)
    with open(archive + '.index.tmp', 'wb') as index_file:
        marshal.dump(entries, index_file)
    for suffix in ('.rows', '.index'):
        os.replace(archive + suffix + '.tmp', archive + suffix)
    print(archive, os.path.getsize(archive + '.rows'), 'bytes', file=sys.stderr)


def same_tables(harness_env, module, name, old, new):
    assert harness_env.cached_exec_rows(module, name.split('_')[0]) is not None, 'cache not used'
    ok = list(new) == list(old)
    for table_name in old:
        a, b = (old[table_name], new[table_name])
        same = type(a) is type(b) and a.columns == b.columns and (type(a.rows) is type(b.rows)) and (len(a.rows) == len(b.rows)) and (typed(a.rows) == typed(b.rows)) and (a.column_range == b.column_range) and (a.reader.columns == b.reader.columns)
        if not same:
            print('DIFF table', name, table_name, file=sys.stderr)
        ok = ok and same
    return ok


def same_connections(old, new):
    query = 'SELECT table_name FROM information_schema.tables ORDER BY 1'
    ok = old.execute(query).fetchall() == new.execute(query).fetchall()
    for table_name, in old.execute(query).fetchall():
        ok = ok and old.execute(f'SELECT * FROM {table_name}').fetchall() == new.execute(f'SELECT * FROM {table_name}').fetchall()
        ok = ok and old.execute(f'DESCRIBE {table_name}').fetchall() == new.execute(f'DESCRIBE {table_name}').fetchall()
    return ok


def main():
    repo, out_dir = (sys.argv[1], sys.argv[2])
    check = '--check' in sys.argv
    import harness_env
    harness_env.install_environment(repo)
    harness_env.import_tests(['tests.test_executor'])
    module = sys.modules['tests.test_executor']
    cls = module.TestExecutor
    cls.setUpClass()
    os.makedirs(out_dir, exist_ok=True)
    for dataset, tables in (('tpch', cls.tpch_tables), ('tpcds', cls.tpcds_tables)):
        schema, directory = harness_env.exec_schema(module, dataset)
        assert list(tables) == list(schema)
        write_cache(tables, schema, directory, out_dir, dataset)
    if not check:
        return
    harness_env.EXEC_CACHE = out_dir
    failures = 0

    class StandIn:
        __module__ = module.__name__
    for name in harness_env.EXEC_LAZY:
        new = harness_env.build_exec_attribute(StandIn, name)
        old = getattr(cls, name)
        if name.endswith('_tables'):
            ok = same_tables(harness_env, module, name, old, new)
        elif name.endswith('_conn'):
            ok = same_connections(old, new)
        else:
            ok = new == old
        print('check', name, 'OK' if ok else 'DIFFERENT', file=sys.stderr)
        failures += not ok
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
