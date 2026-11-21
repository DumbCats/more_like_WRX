"""Load and run unittest cases inside a (forked) worker, reporting one verdict per test.

Verdicts are reported through an `emit(number, failed)` callback, where `failed`
is True/False, or None for a test that was skipped because an earlier failure
already decided the answer.
"""
from __future__ import annotations

import functools
import importlib
import unittest
from unittest.suite import _ErrorHolder

from reduction import STATE

# module name -> exception raised by its import (re-raised on later loads)
FAILED_IMPORTS = {}
# module name -> callback(module), called (and removed) right after the first import
IMPORT_HOOKS = {}


class VerdictCollector(unittest.TestResult):
    """A TestResult that reports each test's outcome as soon as it stops.

    `enders` maps a test number to (numbers it decides, stop_when_decided); its
    'held' entry lists the numbers that still need a decision. Once every held
    number is decided by failures, the remaining tests are skipped.
    """

    def __init__(self, emit, stop_after_fail=False, started=None, enders=()):
        super().__init__()
        self._enders = enders or {}
        self._undecided = set(enders.get('held', ())) if enders else set()
        self.skip_rest = False
        self._started = started
        self.failfast = True
        self._emit = emit
        self._bad_before_test = 0
        self._current = None
        self._stop_after_fail = stop_after_fail
        self.class_errors = []
        self.any_failed = False

    def stop(self):
        pass

    def _exc_info_to_string(self, err, test):
        return ''

    def _bad_count(self):
        return len(self.failures) + len(self.errors) + len(self.unexpectedSuccesses)

    def startTest(self, test):
        super().startTest(test)
        self._bad_before_test = self._bad_count()
        self._current = test
        if self._started is not None:
            self._started(test._blast_number)
        keep = getattr(test, '_blast_keep', None)
        if keep is None:
            STATE.reset()
        else:
            STATE.reset(keep[0], keep[1])
        STATE.in_test = True

    def stopTest(self, test):
        super().stopTest(test)
        STATE.in_test = False
        STATE.reset()
        failed = self._bad_count() > self._bad_before_test
        self._current = None
        if getattr(test, '_blast_skipped', False):
            self._emit(test._blast_number, None)
            return
        self._emit(test._blast_number, failed)
        if failed:
            self.any_failed = True
            decides = self._enders.get(test._blast_number)
            if decides:
                self._undecided -= decides[0]
                if not self._undecided and decides[1]:
                    self.skip_rest = True
            if self._stop_after_fail:
                self.shouldStop = True

    def addError(self, test, err):
        super().addError(test, err)
        if isinstance(test, _ErrorHolder):
            self.class_errors.append(test.description)


def load_case(test_id: str, number: int):
    """Import the module of `test_id` ('path.py::Class::method') and build its TestCase."""
    path, class_name, method_name = test_id.split('::')
    module_name = path[:-3].replace('/', '.')
    if module_name in FAILED_IMPORTS:
        raise FAILED_IMPORTS[module_name]
    try:
        module = importlib.import_module(module_name)
    except BaseException as error:
        if not isinstance(error, (KeyboardInterrupt, SystemExit)):
            FAILED_IMPORTS[module_name] = error
        raise
    if IMPORT_HOOKS:
        hook = IMPORT_HOOKS.pop(module_name, None)
        if hook is not None:
            hook(module)
    case = getattr(module, class_name)(method_name)
    case._blast_number = number
    return case


def _skip_when_failed(case, result_holder, after_any_failure):
    """Wrap the test method so it is skipped once the result says nothing more is needed."""
    method_name = case._testMethodName
    real_method = getattr(case, method_name)

    @functools.wraps(real_method)
    def method(*args, **kwargs):
        result = result_holder[0]
        if result is not None and (result.skip_rest or (after_any_failure and result.any_failed)):
            case._blast_skipped = True
            raise unittest.SkipTest('not needed after a failure')
        return real_method(*args, **kwargs)
    setattr(case, method_name, method)


def run_tests(items, emit, stop_after_fail=False, keeps=None, started=None, skip_after_fail=(), enders=()):
    """Run (number, test_id) `items` in one suite and return the VerdictCollector.

    `keeps` maps a test number to its (keep_files, keep_tasks) reduction;
    tests in `skip_after_fail` are skipped once any test failed.
    """
    suite = unittest.TestSuite()
    numbers_by_class = {}
    result_holder = [None]
    for number, test_id in items:
        try:
            case = load_case(test_id, number)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            emit(number, True)
            continue
        if keeps and number in keeps:
            case._blast_keep = keeps[number]
        if enders or number in skip_after_fail:
            _skip_when_failed(case, result_holder, number in skip_after_fail)
        suite.addTest(case)
        numbers_by_class.setdefault(type(case), []).append(number)
    reported = set()

    def emit_once(number, failed):
        reported.add(number)
        emit(number, failed)
    result = VerdictCollector(emit_once, stop_after_fail, started, enders)
    result_holder[0] = result
    suite.run(result)
    if result.class_errors and (not result.shouldStop):
        for numbers in numbers_by_class.values():
            for number in numbers:
                if number not in reported:
                    emit(number, True)
    return result
