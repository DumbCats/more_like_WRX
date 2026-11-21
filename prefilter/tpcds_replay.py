"""Replaying TPC-DS executor queries against a patched copy of the repository.

Imported only when the prefilter has a TPC-DS budget to spend (see refinement.drop_tpcds).
"""
import json
import multiprocessing
import os
import pickle
import signal
import sys
import tempfile
from pathlib import Path

from replay import overlay

RESULT_FILE = 'tpcds-result.json'


def passes(source, files, queries, reached, profile, seconds):
    """Whether the given queries still pass on the patched tree (False when unsure)."""
    try:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            overlay(source, root, {path: revised for path, _base, revised, *_lines in files})
            order = sorted(queries, key=lambda query: profile['cpu'][query])
            limits = [3 * profile['cpu'][query] + 1.0 for query in order]
            python_paths = [path for path, *_rest in files if path.endswith('.py')]
            child = multiprocessing.get_context('fork').Process(target=run_queries, args=(root, python_paths, sorted(reached), order, limits))
            child.start()
            child.join(seconds)
            if child.is_alive():
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    child.kill()
                child.join()
            if child.exitcode != 0:
                return False
            result = json.loads((root / RESULT_FILE).read_text())
            return result == {'done': sorted(order), 'failed': False}
    except (OSError, ValueError, KeyError, TypeError):
        return False


class Timeout(Exception):
    pass


def loaded_from(root, path):
    """Whether the module for path was imported from root (None when not imported)."""
    module = sys.modules.get(path[:-3].replace('/', '.').removesuffix('.__init__'))
    if module is None:
        return None
    filename = getattr(module, '__file__', None)
    return bool(filename) and Path(filename).resolve() == (root / path).resolve()


def run_queries(root, paths, reached, order, limits):
    """Child process: run TPC-DS queries like TestExecutor.test_execute_tpcds, recording results."""
    os.setsid()
    with open(os.devnull, 'wb') as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    os.chdir(root)
    for name in [module for module in sys.modules if module.split('.')[0] in ('sqlglot', 'tests')]:
        del sys.modules[name]
    sys.path.insert(0, str(root))
    sys.dont_write_bytecode = True
    os.environ.update(PYTHONPATH=str(root), SKIP_INTEGRATION='0', PYTHONDONTWRITEBYTECODE='1')

    def expire(*_args):
        raise Timeout()

    signal.signal(signal.SIGALRM, expire)
    import tests.test_executor as test_executor

    cls = test_executor.TestExecutor
    if getattr(cls, '__unittest_skip__', False):
        sys.exit(1)
    cls.setUpClass()
    test_executor.initializer(test_executor.TPCDS_SCHEMA, pickle.loads(pickle.dumps(cls.tpcds_tables)))
    case = cls('test_execute_tpcds')
    if any(loaded_from(root, path) is False for path in paths):
        sys.exit(1)
    done, failed = [], False
    for query, limit in zip(order, limits):
        args, sql, _expected = cls.tpcds_sqls[query]
        signal.setitimer(signal.ITIMER_REAL, limit)
        try:
            expression = test_executor.parse_one(sql)
            expression, args = pickle.loads(pickle.dumps((expression, args)))
            table = test_executor.mp_execute(expression, args)
            table = pickle.loads(pickle.dumps(table))
            if table is not None:
                case.subtestHelper(query, table, tpch=False)
        except BaseException:
            failed = True
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        done.append(query)
        if failed:
            break
    if any(loaded_from(root, path) is False for path in paths):
        sys.exit(1)
    if not failed and any(loaded_from(root, path) is not True for path in reached):
        sys.exit(1)
    (root / RESULT_FILE).write_text(json.dumps({'done': sorted(done), 'failed': failed}))
