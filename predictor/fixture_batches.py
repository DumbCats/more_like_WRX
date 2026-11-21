"""Fixture batches: several hot commits that only edit SQL fixture files, run in one child.

Between commits the child restores the test classes, the dialect registry and
the I/O functions, and checks that nothing else leaked (interpreter settings,
threads, warning filters, numpy/pandas). On any surprise it stops; the commits
it did not finish cleanly are run one by one as usual.

Extra child -> parent lines (besides those of child_runs):
  'C i'  commit i of the batch starts;  'E i' / 'N i'  it ended (tests ran / not run);
  'X i reasons'  it leaked state, the batch stops;  'Z ok' | 'Z bad'  final shared-state check.
"""
import os
import select
import sys
import time

import patch_heuristics
import patching
import settings
from child_runs import ChildReporter, apply_reductions, kill_and_reap, silence_child_output
from state_checks import interpreter_settings, picture_of_tests, restore_tests, shared_marks

_FIXTURES_DIR = 'tests/fixtures/'
_DEFERRED_MODULES = ('numpy', 'pandas', 'pandas.testing')


def fixture_batch_child(batch, tests, pipe_fd):
    """Child body: run each (plan, order, skip_after_fail) of `batch` in turn."""
    import builtins
    import io
    import threading
    import warnings
    import case_runner
    reporter = ChildReporter(pipe_fd)
    try:
        silence_child_output()
        import harness_env
        import reduction
        cpu_start = time.process_time()
        picture = picture_of_tests({tests[number]['id'] for _, order, _ in batch for number in order})
        dialect_module = sys.modules.get('sqlglot.dialects.dialect')
        if picture is None or dialect_module is None or (not reduction.install_loaders()):
            raise RuntimeError('fixture batch: tests or loaders are not as expected')
        registry = dialect_module.Dialect._classes
        registry_at_start = dict(registry)
        marks = shared_marks()
        io_functions = (builtins.open, io.open, io.open_code, os.listdir)
        warning_filters = list(warnings.filters)
        thread_count = threading.active_count()
        pinned_settings = None
        graveyard = []
        for index, (plan, order, skip_after_fail) in enumerate(batch):
            os.write(pipe_fd, b'C %d\n' % index)
            problems = []
            ran = False
            reduction.SWAPPED = swapped = []
            module_types = None
            try:
                read_old = patching.install_open_overlay(settings.REPO, plan.files)
                patch_heuristics.install_listdir_overlay(settings.REPO, plan.files)
                comparable = not any((failed for _, failed in harness_env.refresh_class_fixtures(settings.REPO, plan.files)))
                changed = {}
                for rel in plan.files:
                    entries = reduction.changed_fixture_entries(rel[len(_FIXTURES_DIR):], read_old)
                    if not entries:
                        comparable = False
                    changed.update(entries)
                if comparable:
                    reduction.STATE.keep_changed = changed
                    reduction.STATE.changed_only = True
                    harness_env.reduce_class_fixtures(changed)
                    runnable_reductions = None
                    if plan.reductions and (not settings.NO_REDUCE):
                        runnable_reductions = apply_reductions(plan, settings.PROFILE, tests)
                    if settings.PANDAS_USERS and any((number in settings.PANDAS_USERS for number in order)):
                        import lazy_pandas
                        lazy_pandas.load()
                    module_types = [type(sys.modules.get(name)) for name in _DEFERRED_MODULES]
                    reporter.reset()
                    now = interpreter_settings()
                    if pinned_settings is None:
                        pinned_settings = now
                    if now != pinned_settings:
                        problems.append('settings before the tests')
                    else:
                        reporter.settings_before = now
                        result = case_runner.run_tests([(number, tests[number]['id']) for number in order], reporter.emit, False, runnable_reductions, reporter.started, skip_after_fail, plan.enders)
                        ran = True
                        if reporter.settings_changed:
                            problems.append('settings changed in a test')
                        if reporter.pool_broke:
                            problems.append('pool died')
                        if result.class_errors:
                            problems.append('class setup failed')
                        if case_runner.FAILED_IMPORTS:
                            problems.append('test module not imported')
            finally:
                reduction.STATE.keep_changed = None
                reduction.STATE.changed_only = False
                reduction.STATE.in_test = False
                reduction.STATE.reset()
                for function, code in reversed(swapped):
                    function.__code__ = code
                reduction.SWAPPED = None
                builtins.open, io.open, io.open_code, os.listdir = io_functions
            if dialect_module.Dialect._classes is not registry:
                problems.append('dialect registry replaced')
            registry.clear()
            registry.update(registry_at_start)
            problems.extend(restore_tests(picture, graveyard))
            if pinned_settings is not None and interpreter_settings() != pinned_settings:
                problems.append('settings after the tests')
            if module_types is not None and module_types != [type(sys.modules.get(name)) for name in _DEFERRED_MODULES]:
                problems.append('numpy or pandas loaded in a test')
            if threading.active_count() != thread_count:
                problems.append('threads')
            if len(warnings.filters) != len(warning_filters) or any((now_filter is not saved_filter for now_filter, saved_filter in zip(warnings.filters, warning_filters))):
                problems.append('warning filters')
            if problems:
                os.write(pipe_fd, ('X %d %s\n' % (index, '; '.join(problems)[:300])).encode())
                break
            os.write(pipe_fd, b'%s %d\n' % (b'E' if ran else b'N', index))
        os.write(pipe_fd, b'Z ok\n' if shared_marks() == marks else b'Z bad\n')
        if settings.DEBUG:
            sys.stderr.write('fixture batch child: %d commits, cpu %.3f\n' % (len(batch), time.process_time() - cpu_start))
        os.write(pipe_fd, b'END\n')
    except BaseException as error:
        try:
            os.write(pipe_fd, ('ERR %r\n' % (error,)).encode()[:400])
        except OSError:
            pass
    finally:
        os._exit(0)


def run_fixture_batch_child(batch, tests, time_left):
    """Fork a fixture batch child for `batch` = [(plan, order, allowed, skip_after_fail, cap)].

    Returns {batch index: (verdicts, skipped, times)} for the commits that ended
    cleanly, or {} if the child did not end cleanly.
    """
    result_read, result_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(result_read)
        try:
            os.setpgid(0, 0)
        except OSError:
            pass
        fixture_batch_child([(plan, order, skip_after_fail) for plan, order, _, skip_after_fail, _ in batch], tests, result_write)
        os._exit(0)
    os.close(result_write)
    results = {}
    current = None
    status = 'DIED'
    marks_unchanged = False
    pending = b''
    deadline = time.monotonic() + time_left
    step_deadline = time.monotonic() + settings.SETUP_TIMEOUT * settings.SLOWDOWN
    allowed, cap, cap_deadline = ({}, None, None)
    try:
        while True:
            now = time.monotonic()
            if now >= deadline or now >= step_deadline or (cap_deadline is not None and now >= cap_deadline):
                status = 'TIMEOUT'
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
                if line == b'END':
                    status = 'END'
                elif line.startswith(b'ERR'):
                    status = 'ERR'
                    settings.log('fixture batch', line.decode(errors='replace'))
                elif line.startswith(b'C '):
                    index = int(line[2:])
                    current = ({}, set(), {})
                    allowed, cap, cap_deadline = (batch[index][2], batch[index][4], None)
                    step_deadline = time.monotonic() + settings.SETUP_TIMEOUT * settings.SLOWDOWN
                elif line.startswith(b'E '):
                    if current is not None:
                        results[int(line[2:])] = current
                    current, cap_deadline = (None, None)
                    step_deadline = time.monotonic() + settings.SETUP_TIMEOUT * settings.SLOWDOWN
                elif line.startswith(b'N '):
                    current, cap_deadline = (None, None)
                    step_deadline = time.monotonic() + settings.SETUP_TIMEOUT * settings.SLOWDOWN
                elif line.startswith(b'X '):
                    current, cap_deadline = (None, None)
                    settings.log('fixture batch ends:', batch[int(line.split()[1])][0].commit, line.decode(errors='replace'))
                    step_deadline = time.monotonic() + settings.SETUP_TIMEOUT * settings.SLOWDOWN
                elif line.startswith(b'Z '):
                    marks_unchanged = line == b'Z ok'
                elif line.startswith(b'S '):
                    if cap_deadline is None and cap is not None:
                        cap_deadline = time.monotonic() + cap
                    step_deadline = time.monotonic() + allowed.get(int(line[2:]), 30.0)
                elif line[:1].isalpha():
                    # any other event (leak, pool, ...) makes this commit's result unusable
                    current = None
                else:
                    fields = line.split()
                    if current is not None:
                        if fields[1] == b'2':
                            current[1].add(int(fields[0]))
                        else:
                            current[0][int(fields[0])] = fields[1] == b'1'
                        if len(fields) > 2:
                            current[2][int(fields[0])] = int(fields[2]) / 1000000.0
                    step_deadline = time.monotonic() + 30.0
    finally:
        os.close(result_read)
        kill_and_reap(pid)
    if status != 'END' or not marks_unchanged:
        settings.log('fixture batch: no result (status %s, last check %s), %d commits run one by one' % (status, marks_unchanged, len(batch)))
        return {}
    return results
