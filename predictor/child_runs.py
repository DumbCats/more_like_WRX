"""Forked children that run a commit's tests against the patched code, and their parent side.

The child reports on a pipe, one line per event:
  'S n'            test n starts
  'n v us'         test n ended: v = 0 passed, 1 failed, 2 skipped; us = CPU microseconds
  'B module'       a module failed to import
  'G n'            interpreter settings / shared state changed in test n (or an added test)
  'W n json'       a watched container changed in test n
  'H json'         the keys of the watched containers
  'P n'            the inline process pool broke in test n
  'M n'            token streams of held test n changed (n = -1: the check starts)
  'A n' / 'A ok'   an added test (that would run before test n) starts / ended
  'D json|many'    fresh runs with a diff: the import-state diff; the parent then
                   sends the run order back on a second pipe
  'END' | 'REDO' | 'ERR ...'
"""
import gc
import json
import marshal
import os
import select
import signal
import sys
import time

import code_cache
import patch_heuristics
import patching
import settings
from state_checks import changed_watches, interpreter_settings, resolve_watches, shared_marks

_FIXTURES_DIR = 'tests/fixtures/'


class ChildReporter:
    """Writes a child's per-test events to the parent's pipe (case_runner callbacks)."""

    def __init__(self, pipe_fd):
        self.pipe_fd = pipe_fd
        self.settings_before = None   # interpreter_settings() when the tests start
        self.settings_changed = False
        self.pool_broke = False
        self.watches = []
        self.last_cpu = time.process_time()
        import inline_pool
        self._pool_class = inline_pool.InlinePool
        self._registry_cleared = False
        self._registry_trimmed = False

    def reset(self):
        """Start a new commit's run (fixture batches run several in one child)."""
        self.settings_before, self.settings_changed, self.pool_broke = (None, False, False)
        self._registry_cleared = False
        self._registry_trimmed = False
        self.last_cpu = time.process_time()

    def emit(self, number, failed):
        now = time.process_time()
        verdict = 2 if failed is None else 1 if failed else 0
        if self.settings_before is not None and (not self.settings_changed) and (interpreter_settings() != self.settings_before):
            self.settings_changed = True
            os.write(self.pipe_fd, b'G %d\n' % number)
        if self.watches:
            for key, changed_keys in changed_watches(self.watches):
                os.write(self.pipe_fd, b'W %d %s\n' % (number, json.dumps([key, changed_keys]).encode()))
        os.write(self.pipe_fd, b'%d %d %d\n' % (number, verdict, int((now - self.last_cpu) * 1000000.0)))
        self.last_cpu = now
        if self._pool_class.broke_any:
            self._pool_class.broke_any = False
            self.pool_broke = True
            os.write(self.pipe_fd, b'P %d\n' % number)

    def started(self, number):
        """Before test `number`: mimic the dialect registry as the full suite would have it."""
        if settings.REGISTRY_START is not None and (not self._registry_trimmed):
            self._registry_trimmed = True
            try:
                dialect_module = sys.modules['sqlglot.dialects.dialect']
                registry = dialect_module.Dialect._classes
                for name in [name for name in registry if name in dialect_module.DIALECT_MODULE_NAMES and name not in settings.REGISTRY_START]:
                    del registry[name]
            except Exception:
                pass
        if not self._registry_cleared and settings.REGISTRY_CLEAR_AT is not None and (number >= settings.REGISTRY_CLEAR_AT):
            self._registry_cleared = True
            try:
                sys.modules['sqlglot.dialects.dialect'].Dialect._classes.clear()
            except Exception:
                pass
        os.write(self.pipe_fd, b'S %d\n' % number)


def silence_child_output():
    """In a fresh child: drop the answer fd and send stdout (and stderr unless debugging) to /dev/null."""
    if settings.ANSWER_FD is not None:
        os.close(settings.ANSWER_FD)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    if not settings.DEBUG:
        os.dup2(devnull, 2)
    if settings.DEBUG:
        import faulthandler
        faulthandler.enable(file=sys.stderr)


def run_token_check(numbers, pipe_fd):
    """Compare token streams of held tests `numbers` and report the changed ones ('M' lines)."""
    if not numbers or not settings.TOKEN_CHECK:
        return
    os.write(pipe_fd, b'M -1\n')
    try:
        import token_check
        changed = token_check.changed_token_tests(settings.TABLES, numbers, settings.TOKEN_CHECK_SECONDS * settings.SLOWDOWN)
    except Exception:
        changed = set(numbers)
    for number in sorted(changed):
        os.write(pipe_fd, b'M %d\n' % number)


def run_added_tests(added_tests, pipe_fd):
    """Run each test the patch adds and report ('G') those that change shared state."""
    import unittest
    import case_runner
    before = (shared_marks(), interpreter_settings())
    for test_id, runs_before in added_tests:
        os.write(pipe_fd, b'A %d\n' % runs_before)
        try:
            case = case_runner.load_case(test_id, -1)
            unittest.TestSuite([case]).run(unittest.TestResult())
        except Exception:
            pass
        after = (shared_marks(), interpreter_settings())
        if after != before:
            os.write(pipe_fd, b'G %d\n' % runs_before)
        before = after
        os.write(pipe_fd, b'A ok\n')


# --- reduced test bodies ------------------------------------------------------------

_PINNED_INDEX = {}


def pinned_code(path, name, first_line):
    """The original code object of function `name` defined at `first_line` of `path` (from the code cache)."""
    by_name_and_line = _PINNED_INDEX.get(path)
    if by_name_and_line is None:
        by_name_and_line = _PINNED_INDEX[path] = {}
        module_code = code_cache.CODE_CACHE.get(path)
        pending = [module_code] if module_code is not None else []
        while pending:
            code = pending.pop()
            for const in code.co_consts:
                if isinstance(const, type(code)):
                    by_name_and_line.setdefault((const.co_name, const.co_firstlineno), const)
                    pending.append(const)
    return by_name_and_line.get((name, first_line))


def _moved_shape(full_path, lines, shape):
    """`shape` shifted to where its method's lines now are in `lines`, or None if not found once."""
    try:
        fd = os.open(full_path, os.O_RDONLY)
        try:
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
        original_lines = b''.join(chunks).decode('utf-8').split('\n')
    except (OSError, UnicodeDecodeError):
        return None
    method_lines = original_lines[shape[0] - 1:shape[1]]
    if not method_lines:
        return None
    starts = [index for index, line in enumerate(lines) if line == method_lines[0] and lines[index:index + len(method_lines)] == method_lines]
    if len(starts) != 1:
        return None
    offset = starts[0] - (shape[0] - 1)
    return (shape[0] + offset, shape[1] + offset) + tuple(shape[2:])


def apply_reductions(plan, tables, tests):
    """Install the reduced bodies of `plan.reductions`; {test number: (keep_files, keep_tasks)} of those applied."""
    import importlib
    import reduction
    applied = {}
    lines_cache = {}
    faithful = tables.faithful()
    for number, (keep_stmts, keep_files, keep_tasks, scaled, _) in plan.reductions.items():
        test_id = tests[number]['id']
        rel, class_name, method_name = test_id.split('::')
        profile = tables.test_units(number)
        shape = None
        if profile is not None:
            shape = (profile[0], profile[1], profile[2])
            if keep_stmts is not None and {index for index, statement in enumerate(profile[2]) if statement[2]} <= keep_stmts:
                keep_stmts = None
        if keep_stmts is not None or scaled:
            record = tables.file(rel)
            if record is None:
                continue
            if shape is None:
                shape = tables.meta['shapes'].get(number)
                if shape is None:
                    continue
            lines = lines_cache.get(rel)
            if lines is None:
                with open(os.path.join(settings.REPO, rel), encoding='utf-8') as handle:
                    lines = lines_cache[rel] = handle.read().split('\n')
            try:
                cls = getattr(importlib.import_module(rel[:-3].replace('/', '.')), class_name)
            except Exception:
                continue
            full_path = os.path.join(settings.REPO, rel)
            if scaled and keep_stmts is None and (plan.files.get(rel) is not None):
                shape = _moved_shape(full_path, lines, shape)
                if shape is None:
                    continue
            pinned = pinned_code(full_path, method_name, shape[0]) if number in faithful and rel not in plan.files else None
            if not reduction.apply_reduction(cls, test_id, full_path, shape, lines, record[1], record[2], keep_stmts, scaled, pinned):
                if settings.DEBUG:
                    sys.stderr.write('reduction not applied: %s\n' % test_id)
                continue
        applied[number] = (keep_files, keep_tasks)
    return applied


# --- the child --------------------------------------------------------------------------

def _import_patched_tree(plan, order, tests, reporter, skip_after_fail, plan_fd, cpu_start):
    """Fresh modes: import the patched tree (whole or cone), then (with `plan_fd`) report
    the import-state diff and read the run order back. Returns (order, skip_after_fail)."""
    import case_runner
    import harness_env
    pipe_fd = reporter.pipe_fd
    python_files = {rel: text for rel, text in plan.files.items() if rel.endswith('.py')}
    path_after_preimport = None
    if settings.PATH_BEFORE_PREIMPORT is not None:
        path_after_preimport = list(sys.path)
        sys.path[:] = settings.PATH_BEFORE_PREIMPORT
    if plan.mode == 'fresh_cone':
        gc.freeze()
        try:
            cone_touched = settings.reimport_cone.reimport(plan.cone_modules, lambda: patching.install_import_overlay(settings.REPO, python_files))
        except Exception as error:
            if settings.DEBUG:
                sys.stderr.write('child %s cone: %r\n' % (plan.commit, error))
            os.write(pipe_fd, b'REDO\n')
            os._exit(0)
        changed_test_modules = []
        broken = []
        if settings.DEBUG:
            sys.stderr.write('child %s cone %d modules: %.3f\n' % (plan.commit, len(plan.cone_modules), time.process_time() - cpu_start))
    else:
        if plan.mode == 'fresh_purge':
            for name in [name for name in sys.modules if name.partition('.')[0] in ('sqlglot', 'tests')]:
                del sys.modules[name]
        patching.install_import_overlay(settings.REPO, python_files)
        changed_test_modules = [rel[:-3].replace('/', '.').removesuffix('.__init__') for rel in python_files if rel.startswith('tests/') and os.path.basename(rel) != 'conftest.py']
        broken = harness_env.broken_modules(changed_test_modules)
    for name in broken:
        os.write(pipe_fd, b'B %s\n' % name.encode())

    def import_all_tests():
        if path_after_preimport is not None:
            sys.path[:] = path_after_preimport
        test_modules = sorted({test['id'].split('::')[0][:-3].replace('/', '.') for test in tests} - set(changed_test_modules))
        for name in harness_env.broken_test_modules(test_modules):
            os.write(pipe_fd, b'B %s\n' % name.encode())
    if plan_fd is None:
        import_all_tests()
    if harness_env.exec_fresh_ok(plan.files):
        case_runner.IMPORT_HOOKS[harness_env.EXEC_KEY[0]] = harness_env.exec_fresh_hook
    if plan_fd is not None:
        _report_import_diff(plan, pipe_fd, cone_touched if plan.mode == 'fresh_cone' else None)
        cpu_diffed = time.process_time()
        import_all_tests()
        if settings.DEBUG:
            sys.stderr.write('child %s import+diff %.3f tests %.3f\n' % (plan.commit, cpu_diffed - cpu_start, time.process_time() - cpu_diffed))
        chunks = []
        while True:
            chunk = os.read(plan_fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(plan_fd)
        run_plan = marshal.loads(b''.join(chunks)) if chunks else None
        if run_plan:
            run_token_check(run_plan.get('token_checks'), pipe_fd)
        if not run_plan or not run_plan['order']:
            os.write(pipe_fd, b'END\n')
            os._exit(0)
        order = run_plan['order']
        skip_after_fail = run_plan['skip_after_fail']
        plan.enders = run_plan['enders']
        plan.reductions = run_plan['reductions']
    if broken:
        dead = patch_heuristics.importer_tests(settings.PROFILE, broken)
        for number in order:
            if number in dead:
                reporter.emit(number, True)
        order = [number for number in order if number not in dead]
    return (order, skip_after_fail)


def _report_import_diff(plan, pipe_fd, cone_touched):
    """Fingerprint the patched import state and send its difference from the base state ('D')."""
    import state_fingerprint
    code_alias = {}
    if plan.fpairs:
        fingerprinter = state_fingerprint.Fingerprinter()
        pending = list(plan.fpairs)
        while pending:
            old_code, new_code = pending.pop()
            code_alias[fingerprinter.code_hash(new_code)] = fingerprinter.code_hash(old_code)
            old_nested = [const for const in old_code.co_consts if isinstance(const, type(old_code))]
            new_nested = [const for const in new_code.co_consts if isinstance(const, type(new_code))]
            if len(old_nested) == len(new_nested):
                pending.extend(zip(old_nested, new_nested))
    if plan.mode == 'fresh_cone':
        only = set(plan.cone_modules) | cone_touched
        new_state, fingerprinter = state_fingerprint.snapshot(code_alias, settings.BASE_CODES, only)
        changed = state_fingerprint.changed_keys_within(settings.BASE_STATE, new_state, only)
    else:
        new_state, fingerprinter = state_fingerprint.snapshot(code_alias, settings.BASE_CODES)
        changed = state_fingerprint.changed_keys(settings.BASE_STATE, new_state)
    if len(changed) > 300:
        os.write(pipe_fd, b'D many\n')
        return
    entries = {}
    if not settings.NO_KEYMAP:
        try:
            entries = settings.registry_keys.changed_entry_rows(changed, settings.BASE_OBJECTS, settings.BASE_STATE, fingerprinter.objects, fingerprinter, state_fingerprint.Fingerprinter)
        except Exception:
            entries = {}
    rows = [[list(key), key in fingerprinter.function_keys or key in settings.BASE_FUNCTION_KEYS, entries.get(key)] for key in changed]
    os.write(pipe_fd, b'D ' + json.dumps(rows).encode() + b'\n')


def child_main(plan, order, tests, pipe_fd, function_index, skip_after_fail, plan_fd=None):
    """Body of a forked child: apply the patch, run `order`, report on `pipe_fd`, exit."""
    import case_runner
    reporter = ChildReporter(pipe_fd)
    try:
        silence_child_output()
        if settings.FAULT_SECONDS:
            import faulthandler
            faulthandler.dump_traceback_later(float(settings.FAULT_SECONDS), file=sys.stderr)
        cpu_start = time.process_time()
        if any((not (rel.startswith('sqlglot/') or (rel.startswith(_FIXTURES_DIR) and rel.endswith('.sql'))) for rel in plan.files)):
            import harness_env
            harness_env.make_exec_tables_strict()
        if plan.mode in ('fresh', 'fresh_purge', 'fresh_cone'):
            order, skip_after_fail = _import_patched_tree(plan, order, tests, reporter, skip_after_fail, plan_fd, cpu_start)
        else:
            for _, code_pairs in plan.work:
                swaps = patching.plan_code_swaps(code_pairs, function_index)
                if swaps is None:
                    os.write(pipe_fd, b'REDO\n')
                    os._exit(0)
                for function, code in swaps:
                    function.__code__ = code
        read_old = patching.install_open_overlay(settings.REPO, plan.files)
        patch_heuristics.install_listdir_overlay(settings.REPO, plan.files)
        if plan.mode == 'hot':
            import harness_env
            for module_name, failed in harness_env.refresh_class_fixtures(settings.REPO, plan.files):
                if failed:
                    os.write(pipe_fd, b'B %s\n' % module_name.encode())
        import reduction
        runnable_reductions = None
        if plan.mode != 'hot' and plan.reductions and (not settings.NO_REDUCE):
            fixture_files = [rel for rel in plan.files if rel.startswith(_FIXTURES_DIR)]
            loaders = False
            if fixture_files or any((reduction_spec[0] is not None or reduction_spec[1] is not None for reduction_spec in plan.reductions.values())):
                loaders = reduction.install_loaders()
            changed = {}
            comparable = True
            for rel in fixture_files:
                entries = {}
                if loaders and plan.files[rel] is not None:
                    entries = reduction.changed_fixture_entries(rel[len(_FIXTURES_DIR):], read_old)
                if not entries:
                    comparable = False
                changed.update(entries)
            if comparable:
                if changed:
                    reduction.STATE.keep_changed = changed
                runnable_reductions = apply_reductions(plan, settings.PROFILE, tests)
        if plan.mode == 'hot' and reduction.install_loaders():
            changed = {}
            for rel, text in plan.files.items():
                if rel.startswith(_FIXTURES_DIR) and text is not None:
                    changed.update(reduction.changed_fixture_entries(rel[len(_FIXTURES_DIR):], read_old))
            if changed:
                reduction.STATE.keep_changed = changed
                reduction.STATE.changed_only = not plan.work
                if not plan.work:
                    import harness_env
                    harness_env.reduce_class_fixtures(changed)
            if plan.reductions and (not settings.NO_REDUCE):
                runnable_reductions = apply_reductions(plan, settings.PROFILE, tests)
        if settings.PANDAS_USERS and any((number in settings.PANDAS_USERS for number in order)):
            import lazy_pandas
            lazy_pandas.load()
        if plan.watch_specs:
            reporter.watches.extend(resolve_watches(plan.watch_specs))
            os.write(pipe_fd, b'H %s\n' % json.dumps([watch[0] for watch in reporter.watches]).encode())
        run_token_check(plan.token_checks, pipe_fd)
        if plan.mode != 'hot':
            gc.freeze()
        cpu_setup = time.process_time()
        reporter.settings_before = interpreter_settings()
        case_runner.run_tests([(number, tests[number]['id']) for number in order], reporter.emit, False, runnable_reductions, reporter.started, skip_after_fail, plan.enders)
        if plan.added_tests and plan.mode in ('fresh', 'fresh_purge'):
            run_added_tests(plan.added_tests, pipe_fd)
        if settings.DEBUG:
            sys.stderr.write('child %s setup %.3f run %.3f\n' % (plan.commit, cpu_setup - cpu_start, time.process_time() - cpu_setup))
        os.write(pipe_fd, b'END\n')
    except BaseException as error:
        try:
            os.write(pipe_fd, ('ERR %r\n' % (error,)).encode()[:400])
        except OSError:
            pass
    finally:
        os._exit(0)


# --- the parent side ------------------------------------------------------------------

def kill_and_reap(pid):
    """Kill a child's process group and the child, reap it (and any stray children); return its wait status."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    _, wait_status = os.waitpid(pid, 0)
    try:
        while os.waitpid(-1, os.WNOHANG)[0] > 0:
            pass
    except ChildProcessError:
        pass
    return wait_status


def run_child(plan, order, tests, function_index, allowed, time_left, skip_after_fail=(), on_diff=None, cap=None):
    """Fork a child for `plan` and collect its verdicts.

    `allowed` maps a test to its seconds once started; `cap` bounds the whole
    run once the first test starts. With `on_diff`, `order` is None: the child
    reports the import-state diff and `on_diff(rows)` returns the run plan,
    sent back to it (its 'allowed' and 'cap' are used here).
    Returns (verdicts {number: failed}, status, number of the test that hung or None).
    """
    result_read, result_write = os.pipe()
    plan_read = plan_write = None
    if on_diff is not None:
        plan_read, plan_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(result_read)
        if plan_write is not None:
            os.close(plan_write)
        try:
            os.setpgid(0, 0)
        except OSError:
            pass
        child_main(plan, order, tests, result_write, function_index, skip_after_fail, plan_read)
        os._exit(0)
    os.close(result_write)
    if plan_read is not None:
        os.close(plan_read)
    verdicts = {}
    status = 'DIED'
    hung = None
    broken = plan.broken_modules
    pending = b''
    deadline = time.monotonic() + time_left
    running = None
    step_deadline = time.monotonic() + settings.SETUP_TIMEOUT * settings.SLOWDOWN
    cap_deadline = None
    added_running = None
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                status = 'TIMEOUT'
                break
            if cap_deadline is not None and now >= cap_deadline:
                status = 'OVER'
                break
            if now >= step_deadline:
                status = 'TIMEOUT'
                hung = running
                break
            ready, _, _ = select.select([result_read], [], [], max(0.01, min(deadline, step_deadline, cap_deadline or deadline) - now))
            if not ready:
                continue
            chunk = os.read(result_read, 65536)
            if not chunk:
                break
            pending += chunk
            *lines, pending = pending.split(b'\n')
            for line in lines:
                if line in (b'END', b'REDO'):
                    status = line.decode()
                elif line.startswith(b'ERR'):
                    status = 'ERR'
                    settings.log(plan.commit, line.decode(errors='replace'))
                elif line.startswith(b'S '):
                    running = int(line[2:])
                    if cap_deadline is None and cap is not None:
                        cap_deadline = time.monotonic() + cap
                    step_deadline = time.monotonic() + allowed.get(running, 30.0)
                elif line.startswith(b'B '):
                    broken.append(line[2:].decode())
                elif line.startswith(b'P '):
                    plan.pool_deaths.append(int(line[2:]))
                elif line.startswith(b'W '):
                    number, _, payload = line[2:].partition(b' ')
                    key, changed_keys = json.loads(payload)
                    plan.container_leaks.append((int(number), key, changed_keys))
                elif line.startswith(b'H '):
                    plan.watched_keys = json.loads(line[2:])
                elif line.startswith(b'M '):
                    number = int(line[2:])
                    if number >= 0:
                        plan.must_select.add(number)
                        plan.notes.append('token streams of %d changed' % number)
                    step_deadline = time.monotonic() + settings.SETUP_TIMEOUT * settings.SLOWDOWN
                elif line.startswith(b'G '):
                    number = int(line[2:])
                    plan.leak_from = number if plan.leak_from is None else min(plan.leak_from, number)
                elif line == b'A ok':
                    added_running = None
                elif line.startswith(b'A '):
                    added_running = int(line[2:])
                    cap_deadline = None
                    cap = None
                    step_deadline = time.monotonic() + 2 * settings.TEST_TIMEOUT * settings.SLOWDOWN
                elif line.startswith(b'D '):
                    rows = None if line == b'D many' else json.loads(line[2:])
                    if settings.DEBUG:
                        settings.DEBUG_DIFF_KEYS[plan.commit] = rows
                    run_plan = on_diff(rows)
                    allowed = run_plan.pop('allowed')
                    cap = run_plan.pop('cap')
                    payload = marshal.dumps(run_plan)
                    while payload:
                        payload = payload[os.write(plan_write, payload):]
                    os.close(plan_write)
                    plan_write = None
                    step_deadline = time.monotonic() + settings.SETUP_TIMEOUT * settings.SLOWDOWN
                else:
                    fields = line.split()
                    if fields[1] == b'2':
                        plan.skipped.add(int(fields[0]))
                    else:
                        verdicts[int(fields[0])] = fields[1] == b'1'
                    if len(fields) > 2:
                        plan.test_times[int(fields[0])] = int(fields[2]) / 1000000.0
                    running = None
                    step_deadline = time.monotonic() + 30.0
    finally:
        os.close(result_read)
        if plan_write is not None:
            os.close(plan_write)
        wait_status = kill_and_reap(pid)
    if added_running is not None:
        plan.leak_from = added_running if plan.leak_from is None else min(plan.leak_from, added_running)
        plan.notes.append('added test did not end')
    if status == 'DIED':
        settings.log(plan.commit, 'child died, wait status', wait_status, 'after', len(verdicts), 'tests')
        if running is not None:
            hung = running
    return (verdicts, status, hung)


# --- before the fresh children ---------------------------------------------------------

# Imported once in the parent so fresh children (which drop sqlglot and tests) need not.
TEST_LIBRARIES = ('unittest.mock', 'sqlite3', 'tracemalloc', 'runpy', 'doctest', 'multiprocessing', 'concurrent.futures', 'setuptools', 'setuptools.command.build_ext', 'pytz', 'duckdb', 'numpy', 'pandas', 'pandas.testing', 'resource', 'argparse', 'csv', 'pickle', 'weakref', 'tempfile', 'pathlib', 'ast', 'pkgutil', 'json', 'datetime', 'decimal', 'contextlib', 'math', 'time', 'collections.abc', 'functools')


def preimport_for_fresh(fresh_commits, plans, tests):
    """Import the test libraries and compile every test file once, before the fresh children fork."""
    import importlib
    if any((rel.startswith('tests/') and rel.endswith('.py') for commit in fresh_commits for rel in plans[commit].files)):
        return
    path_before = list(sys.path)
    for name in TEST_LIBRARIES:
        try:
            importlib.import_module(name)
        except Exception:
            pass
    if sys.path != path_before:
        settings.PATH_BEFORE_PREIMPORT = path_before
    for path in sorted({os.path.join(settings.REPO, test['file']) for test in tests}):
        if path not in code_cache.CODE_CACHE and path.endswith('.py') and os.path.exists(path):
            name = os.path.relpath(path, settings.REPO)[:-3].replace('/', '.')
            try:
                code_cache.SourceFileLoader(name, path).get_code(name)
            except Exception:
                pass
    gc.freeze()
