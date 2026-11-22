"""The predictor: for every commit of $BLAST_DATA, select the tests whose outcome it can change.

Flow (`main` -> `run`):
  1. plan every commit from its patch and the profile tables (planner);
  2. prepare the shared parent process: import sqlglot and the tests, build the
     shared class fixtures, load the base import state, pre-import libraries;
  3. run each commit's candidates in a forked child against the patched code
     (cone re-imports first, then hot commits and fixture batches, then fresh
     imports), see scheduling / child_runs / fixture_batches;
  4. print one JSON line per commit: {"commit": ..., "workers": [[test numbers], ...]}.

Importing this module installs the code cache (code_cache) before anything else.
build/base_state.py imports it for `base_state_header` and `resolve_base_objects`.
"""
import gc
import json
import marshal
import os
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import code_cache  # noqa: E402  (must come first: wraps SourceFileLoader.get_code)
import patching  # noqa: E402
import patch_heuristics  # noqa: E402,F401
from packing import pack  # noqa: E402

patching.KEEP_CODE = code_cache.INSTALLED
import settings  # noqa: E402  (imports the optional registry_keys / reimport_cone)
from planner import Plan, cone_plan, make_plan, registry_clear_at  # noqa: E402
from profile_tables import ProfileTables  # noqa: E402
from refinement import compute_reductions  # noqa: E402
from scheduling import Scheduler  # noqa: E402

log = settings.log


def main():
    """Entry point: run, then always print an answer for every commit, then exit."""
    started_at = time.monotonic()
    deadline = started_at + settings.WALL_LIMIT
    answer_fd = settings.ANSWER_FD = os.dup(1)
    # anything sqlglot or its tests print must not reach the answers
    os.dup2(2, 1)
    outputs = {'answers': {}, 'commits': [], 'tests': None}
    try:
        run(started_at, deadline, outputs)
    except BaseException as error:
        log('predictor failed:', repr(error))
        import traceback
        traceback.print_exc(file=sys.stderr)
    tests = outputs['tests']
    commits = outputs['commits']
    if tests is None:
        with open(os.path.join(settings.DATA, 'tests.json')) as handle:
            tests = json.load(handle)['tests']
    if not commits:
        commits = _commit_names()
    seconds = [test['seconds'] for test in tests]
    numbering = outputs.get('number')
    if numbering is None and any((test.get('number', index) != index for index, test in enumerate(tests))):
        numbering = [test.get('number', index) for index, test in enumerate(tests)]
    extra_tests = outputs.get('extra') or []
    every_test = None
    lines = []
    final = {}
    for commit in commits:
        workers = outputs['answers'].get(commit)
        if workers is None:
            if every_test is None:
                every_test = pack(list(range(len(tests))), seconds)
            workers = every_test
        prefilter_tests = settings.RESTRICT.get(commit) if settings.RESTRICT is not None else None
        if prefilter_tests is not None:
            chosen = [number for worker in workers for number in worker]
            kept = sorted((number for number in chosen if (number if numbering is None else numbering[number]) in prefilter_tests))
            if len(kept) < len(chosen):
                workers = pack(kept, seconds)
        if numbering is not None:
            workers = [[numbering[number] for number in worker if numbering[number] is not None] for worker in workers]
            workers = [worker for worker in workers if worker]
        if extra_tests:
            workers = [workers[0] + extra_tests] + workers[1:] if workers else [list(extra_tests)]
        final[commit] = workers
    dropped = tpcds_gate(final, tests, deadline)
    if dropped:
        tpcds_number = commit_test_number(tests)
        by_number = {test['number']: test['seconds'] for test in tests}
        for commit in dropped:
            final[commit] = pack(sorted(number for worker in final[commit] for number in worker if number != tpcds_number), by_number)
        log('tpcds gate dropped the TPC-DS test from', len(dropped), 'commits')
    for commit in commits:
        lines.append(json.dumps({'commit': commit, 'workers': final[commit]}))
    data = ('\n'.join(lines) + '\n').encode() if lines else b''
    while data:
        written = os.write(answer_fd, data)
        data = data[written:]
    cgroup_cpu = ''
    try:
        with open('/sys/fs/cgroup/cpu.stat') as handle:
            cgroup_cpu = ' '.join(handle.read().split('\n')[:3])
    except OSError:
        pass
    log('done in', round(time.monotonic() - started_at, 2), 's wall; cpu', round(sum(os.times()[:4]), 2), cgroup_cpu)
    sys.stderr.flush()
    os._exit(0)


TPCDS_TEST = 'tests/test_executor.py::TestExecutor::test_execute_tpcds'


def commit_test_number(tests):
    """The number of the TPC-DS executor test in tests.json (None when absent)."""
    return next((test['number'] for test in tests if test['id'] == TPCDS_TEST), None)


def tpcds_gate(final, tests, deadline):
    """Commits whose final answer may drop the TPC-DS executor test (the prefilter's stage gate,
    run as a fresh process on what is left of the wall budget, says it still passes)."""
    number = commit_test_number(tests)
    budget = min(settings.TPCDS_GATE_SECONDS, deadline - time.monotonic() - 10.0)
    gate = os.path.join(os.path.dirname(os.environ.get('BLAST_PREFILTER', '/prefilter/main.py')), 'tpcds_gate.py')
    if number is None or budget < 3.0 or not os.path.exists(gate):
        return []
    seconds = {test['number']: test['seconds'] for test in tests}

    def cost(workers):
        return max((sum(seconds[test] for test in worker) for worker in workers), default=0.0) + settings.WORKER_COST * len(workers)

    # Other executor tests in the answer (failing or held) almost always mean the TPC-DS test
    # fails too: the TPC-H ones, or two or more of the rest. The gate is not worth its CPU there.
    executor_ids = {test['number']: test['id'] for test in tests if test['id'].startswith(TPCDS_TEST.rsplit('::', 1)[0] + '::')}
    savings = {}
    for commit, workers in final.items():
        chosen = [test for worker in workers for test in worker]
        others = [executor_ids[test] for test in chosen if test in executor_ids and test != number]
        if any(test_id.endswith(('::test_execute_tpch', '::test_optimized_tpch')) for test_id in others) or len(others) >= 2:
            continue
        if number in chosen:
            savings[commit] = round(cost(workers) - cost(pack(sorted(test for test in chosen if test != number), seconds)), 3)
    savings = {commit: saving for commit, saving in savings.items() if saving > 5.0}
    if not savings:
        return []
    import subprocess

    environment = dict(os.environ, BLAST_REPO=settings.REPO, BLAST_DATA=settings.DATA)
    try:
        completed = subprocess.run([sys.executable, gate, str(budget)], input=json.dumps(savings).encode(), stdout=subprocess.PIPE, timeout=budget + 15.0, env=environment)
        verdicts = [json.loads(line) for line in completed.stdout.decode().splitlines() if line.strip()]
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        log('tpcds gate failed:', repr(error))
        return []
    return [row['commit'] for row in verdicts if row.get('verdict') == 'pass' and row.get('commit') in savings]


def _commit_names():
    return sorted((name[:-6] for name in os.listdir(os.path.join(settings.DATA, 'commits')) if name.endswith('.patch')))


# --- the saved base state ------------------------------------------------------------

def base_state_header():
    """What a saved base state must match: interpreter, hash seed, hashing, fingerprint code."""
    with open(os.path.join(HERE, 'state_fingerprint.py'), 'rb') as handle:
        code_version = zlib.crc32(handle.read())
    return (sys.version, os.environ.get('PYTHONHASHSEED'), hash('blast radius base state probe'), hash(('G', 'sqlglot', 1, 2.5, b'x', None)), code_version)


def resolve_base_objects(object_keys):
    """{key: live dict/set object} for the fingerprinter's object keys, or None if one does not resolve."""
    objects = {}
    missing = object()
    for key in object_keys:
        module = sys.modules.get(key[1])
        if module is None:
            return None
        if key[0] == 'G' and len(key) == 3:
            value = vars(module).get(key[2], missing)
        elif key[0] == 'A' and len(key) == 4:
            namespace = vars(module)
            for part in key[2].split('.'):
                owner = namespace.get(part)
                if not isinstance(owner, type):
                    return None
                namespace = owner.__dict__
            value = namespace.get(key[3], missing)
        else:
            return None
        if type(value) not in (dict, set, frozenset):
            return None
        objects[key] = value
    return objects


def load_base_state():
    """(state, function keys, objects) from the base_state.bin saved at build, or None."""
    if not settings.BASE_STATE_FILE or os.environ.get('PYTHONHASHSEED') != '0':
        return None
    import harness_env
    for directory in (settings.TABLES, harness_env.EXEC_CACHE, os.path.join(HERE, 'cache')):
        try:
            with open(os.path.join(directory, 'base_state.bin'), 'rb') as handle:
                saved = marshal.loads(handle.read())
        except (OSError, ValueError, EOFError, TypeError):
            continue
        try:
            if tuple(saved['header']) != base_state_header():
                log('base state map: other interpreter or hash seed')
                return None
            objects = resolve_base_objects(saved['okeys'])
            if objects is None:
                log('base state map: a table key does not resolve')
                return None
            return (saved['state'], frozenset(saved['fkeys']), objects)
        except (KeyError, TypeError, ValueError, IndexError):
            return None
    return None


def _set_base_state():
    """Load the base import state, or fingerprint the freshly imported sqlglot."""
    saved = load_base_state()
    if saved is not None:
        settings.BASE_STATE, settings.BASE_FUNCTION_KEYS, settings.BASE_OBJECTS = saved
    else:
        import state_fingerprint
        settings.BASE_STATE, base_fingerprinter = state_fingerprint.snapshot()
        settings.BASE_FUNCTION_KEYS = base_fingerprinter.function_keys
        settings.BASE_OBJECTS = base_fingerprinter.objects
        settings.BASE_CODES = base_fingerprinter.base_codes() if settings.BASE_CODES_WANTED else None


# --- the run -----------------------------------------------------------------------------

def align_tests(tests, test_ids, outputs):
    """Match tests.json to the tables' test ids (by id) when they differ.

    Records in `outputs` the input number of each of our tests ('number') and the
    input tests the tables do not know ('extra', always selected).
    """
    if [test['id'] for test in tests] == list(test_ids) and all((test.get('number', index) == index for index, test in enumerate(tests))):
        return tests
    log('tests.json differs from the maps: tests are matched by id')
    by_id = {test['id']: (test, test.get('number', index)) for index, test in enumerate(tests)}
    known_ids = set(test_ids)
    outputs['extra'] = [number for test_id, (_, number) in by_id.items() if test_id not in known_ids]
    outputs['number'] = [by_id[test_id][1] if test_id in by_id else None for test_id in test_ids]
    return [dict(by_id[test_id][0]) if test_id in by_id else {'id': test_id, 'file': test_id.split('::')[0], 'seconds': 0.0} for test_id in test_ids]


def _load_run_tables():
    """Load the profile tables and the registry / cone side tables into settings."""
    try:
        with open(os.path.join(settings.TABLES, 'registry_start.json')) as handle:
            settings.REGISTRY_START = frozenset(json.load(handle))
    except (OSError, ValueError):
        settings.REGISTRY_START = None
    tables = settings.PROFILE = ProfileTables(settings.TABLES)
    try:
        with open(os.path.join(settings.TABLES, 'cone.json')) as handle:
            cone_tables = json.load(handle)
        if not settings.NO_CONE:
            settings.CONE_MAP = cone_tables['modules']
            settings.CONE_WALK[:] = cone_tables.get('walk') or []
    except (OSError, ValueError, KeyError):
        settings.CONE_MAP = None
    return tables


def _plan_commits(commits, tables, tests):
    plans = {}
    for commit in commits:
        try:
            with open(os.path.join(settings.DATA, 'commits', commit + '.patch'), encoding='utf-8', errors='surrogateescape') as handle:
                text = handle.read()
            plans[commit] = make_plan(commit, text, tables)
            plans[commit].reductions = compute_reductions(plans[commit], tables, tests)
        except Exception as error:
            log(commit, 'plan failed', repr(error))
            plan = plans[commit] = Plan(commit)
            plan.mode = 'all'
    return plans


def _install_pandas(tests):
    """Import duckdb, and numpy/pandas (deferred until a test needs them when possible).

    Returns True if numpy/pandas are deferred (settings.PANDAS_USERS lists the tests needing them).
    """
    import duckdb  # noqa: F401
    pandas_users = None
    if settings.LAZY_PANDAS:
        try:
            with open(os.path.join(settings.TABLES, 'pandas_tests.json')) as handle:
                pandas_ids = set(json.load(handle)['tests'])
            pandas_users = frozenset((number for number, test in enumerate(tests) if test['id'] in pandas_ids))
        except (OSError, ValueError, KeyError, TypeError):
            pandas_users = None
    deferred = False
    if pandas_users is not None:
        import lazy_pandas
        deferred = lazy_pandas.install()
    if deferred:
        settings.PANDAS_USERS = pandas_users
    else:
        import pandas  # noqa: F401
        import numpy  # noqa: F401
    return deferred


def run(started_at, deadline, outputs):
    """Plan and run every commit, filling outputs['answers'] (commit -> workers)."""
    with open(os.path.join(settings.DATA, 'tests.json')) as handle:
        tests = json.load(handle)['tests']
    commits = _commit_names()
    outputs['tests'] = tests
    outputs['commits'] = commits
    tables = _load_run_tables()
    tests = align_tests(tests, tables.meta['test_ids'], outputs)
    outputs['tests'] = tests
    settings.REGISTRY_CLEAR_AT = registry_clear_at([test['id'] for test in tests])
    plans = _plan_commits(commits, tables, tests)
    log('planned', len(plans), 'in', round(time.monotonic() - started_at, 3))
    if settings.PLAN_ONLY:
        with open(settings.PLAN_ONLY, 'w') as handle:
            json.dump({commit: {'mode': plan.mode, 'cands': sorted(plan.candidates), 'note': plan.notes, 'files': sorted(plan.files)} for commit, plan in plans.items()}, handle)
        return
    import harness_env
    harness_env.install_environment(settings.REPO)
    scheduler = Scheduler(tables, tests, plans, outputs['answers'], outputs.get('number'), deadline)
    answers, seconds = (outputs['answers'], scheduler.seconds)
    hot, fresh = ([], [])
    for commit in commits:
        plan = plans[commit]
        if plan.mode == 'all':
            answers[commit] = pack(scheduler.all_tests, seconds)
        elif plan.mode == 'blind':
            answers[commit] = pack(sorted(plan.blind_tests), seconds)
            if settings.DEBUG:
                scheduler.debug_rows[commit] = {'mode': 'blind', 'cands': sorted(plan.blind_tests), 'verdicts': {}, 'note': plan.notes}
        elif plan.mode == 'noop':
            answers[commit] = []
            if settings.DEBUG:
                scheduler.debug_rows[commit] = {'mode': 'noop', 'cands': [], 'verdicts': {}, 'note': plan.notes}
        elif plan.mode == 'hot':
            hot.append(commit)
        else:
            fresh.append(commit)
    hot.sort(key=scheduler.planned_cost)
    fresh.sort(key=scheduler.planned_cost)
    needs_heavy_tests = any((tests[number]['file'] in ('tests/test_optimizer.py', 'tests/test_executor.py') for commit in hot + fresh for number in plans[commit].candidates))
    pandas_deferred = _install_pandas(tests) if needs_heavy_tests else False
    needs_pandas = scheduler.needs_pandas
    # the commit before whose run numpy/pandas are loaded in the parent (so later children share them)
    pandas_turn = None
    if pandas_deferred:
        hot = [commit for commit in hot if not needs_pandas(commit)] + [commit for commit in hot if needs_pandas(commit)]
        pandas_needers = [commit for commit in hot + fresh if needs_pandas(commit)]
        if len(pandas_needers) >= 2:
            pandas_turn = pandas_needers[0]
    import case_runner  # noqa: F401  (imported once here, shared by every child)
    want_diff = bool(fresh) and (not settings.NO_DIFF) and any((plans[commit].diffable and (not scheduler.small_plan(plans[commit])) for commit in fresh))
    if hot or want_diff:
        harness_env.import_sqlglot()
    if want_diff:
        _set_base_state()
    import reduction
    if want_diff:
        import state_fingerprint  # noqa: F401
    pandas_loaded = []

    def load_pandas_here():
        if pandas_loaded:
            return
        pandas_loaded.append(True)
        import lazy_pandas
        lazy_pandas.load()
        gc.freeze()
    preimported = []

    def preimport_once():
        if not preimported and len(fresh) >= 2 and settings.PREIMPORT:
            preimported.append(True)
            from child_runs import preimport_for_fresh
            preimport_for_fresh(fresh, plans, tests)
    cone_first = []
    if (hot or want_diff) and (not settings.NO_CONE) and fresh:
        cone_first = [commit for commit in fresh if cone_plan(plans[commit])]
    if cone_first:
        cpu_before = time.process_time()
        settings.reimport_cone.record_imports(settings.CONE_WALK)
        log('cone record', len(settings.reimport_cone.RECORDED_HEAP.records), 'tables in', round(time.process_time() - cpu_before, 3))
        if hot:
            harness_env.force_single_thread_duckdb()
        preimport_once()
        if pandas_deferred:
            cone_first = [commit for commit in cone_first if not needs_pandas(commit)] + [commit for commit in cone_first if needs_pandas(commit)]
            later = [commit for commit in fresh if commit not in set(cone_first)]
            pandas_needers = [commit for commit in cone_first + hot + later if needs_pandas(commit)]
            pandas_turn = pandas_needers[0] if len(pandas_needers) >= 2 else None
    cone_done = set()
    for commit in cone_first:
        if commit == pandas_turn:
            load_pandas_here()
        plan = plans[commit]
        plan.mode = 'fresh_cone'
        if scheduler.execute_safe(commit, plan, None) == 'REDO':
            plan.cone_modules = []
            plan.mode = 'fresh'
        else:
            cone_done.add(commit)
    if cone_done:
        fresh = [commit for commit in fresh if commit not in cone_done]
    if hot:
        test_modules = sorted({tests[number]['file'][:-3].replace('/', '.') for commit in hot for number in plans[commit].candidates})
        harness_env.import_tests(test_modules)
        reduction.install_loaders()
        wanted_classes = set()
        for commit in hot:
            if not any((rel.startswith('tests/') and rel.endswith('.py') for rel in plans[commit].files)):
                for number in plans[commit].candidates:
                    path, class_name, _ = tests[number]['id'].split('::')
                    wanted_classes.add((path[:-3].replace('/', '.'), class_name))
        shared = harness_env.build_class_fixtures(wanted_classes, exec_lazy=True, exec_prebuild=scheduler.exec_prebuild(hot))
        log('shared fixtures', shared)
        gc.unfreeze()
        scheduler.function_index = patching.FunctionIndex(settings.REPO)
        gc.freeze()
    purge = bool(hot) or want_diff
    _run_hot(scheduler, hot, fresh, pandas_turn, load_pandas_here)
    preimport_once()
    for commit in fresh:
        if commit == pandas_turn:
            load_pandas_here()
        plan = plans[commit]
        plan.mode = 'fresh_purge' if purge else 'fresh'
        scheduler.execute_safe(commit, plan, None)
    if settings.DEBUG:
        with open(settings.DEBUG, 'w') as handle:
            json.dump(scheduler.debug_rows, handle)


def _run_hot(scheduler, hot, fresh, pandas_turn, load_pandas_here):
    """Run the hot commits (fixture-only ones in batches); a hot commit needing a redo joins `fresh`."""
    plans, tables = (scheduler.plans, scheduler.tables)
    batch_groups = {}  # commit -> needs pandas (commits of one group share a batch)
    if settings.FIXTURE_BATCH and (not settings.RUN_ALL) and (not settings.NO_REDUCE) and (len(hot) >= settings.FIXTURE_BATCH_MIN):
        for commit in hot:
            try:
                if scheduler.fixture_only(commit):
                    batch_groups[commit] = scheduler.needs_pandas(commit)
            except Exception as error:
                log(commit, 'fixture batch: not planned', repr(error))
        log('fixture-only commits:', sum((1 for group in batch_groups.values() if not group)), 'and', sum((1 for group in batch_groups.values() if group)), 'with pandas')
        for group_key in (False, True):
            if sum((1 for group in batch_groups.values() if group == group_key)) < settings.FIXTURE_BATCH_MIN:
                batch_groups = {commit: group for commit, group in batch_groups.items() if group != group_key}
    answered = set()
    for commit in hot:
        if commit in answered:
            continue
        if commit == pandas_turn:
            load_pandas_here()
        if commit in batch_groups:
            members = [member for member in hot if batch_groups.get(member) == batch_groups[commit]]
            for member in members:
                del batch_groups[member]
            try:
                answered.update(scheduler.run_fixture_batch(members))
            except Exception as error:
                log('fixture batch failed', repr(error))
            if commit in answered:
                continue
        if scheduler.execute_safe(commit, plans[commit], scheduler.function_index) == 'REDO':
            fresh.append(commit)
            plan = plans[commit]
            plan.notes.append('redo')
            plan.diffable = False
            for rel in plan.files:
                record = tables.file(rel)
                if record is not None:
                    plan.candidates.update(tables.tests(record[7]))
                    plan.candidates.update(tables.tests(record[8]))
            plan.reductions = {number: spec for number, spec in plan.reductions.items() if spec[4] is None}


if __name__ == '__main__':
    main()
