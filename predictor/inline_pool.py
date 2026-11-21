"""An in-process stand-in for concurrent.futures.ProcessPoolExecutor.

Tests that use a process pool run their tasks lazily in the calling process
(each task's arguments and result still go through pickle, as with a real
pool). Tasks are tagged through reduction.STATE so a reduced test can skip
tasks it does not need. A task that cannot be (un)pickled, or calls
sys.exit, marks the pool broken just as a dead worker process would.
"""
from __future__ import annotations

import pickle
from concurrent.futures import Future

from reduction import STATE


def BrokenProcessPool(*args):
    """Build concurrent.futures.process.BrokenProcessPool (imported only when needed)."""
    from concurrent.futures.process import BrokenProcessPool as error_class
    return error_class(*args)


_FINISHED = 'FINISHED'
_WORKER_DIED = 'A process in the process pool was terminated abruptly'


class LazyFuture(Future):
    """A future that runs its task on the first result()/exception() call."""

    def __init__(self, pool, fn, args, kwargs):
        super().__init__()
        self._pool = pool
        self._task = (fn, args, kwargs)
        self._state = _FINISHED
        self._computed = False
        self._tag = STATE.current
        self._index = STATE.task_counter
        STATE.task_counter += 1
        STATE.tasks_seen = STATE.task_counter
        if STATE.on_submit is not None and self._tag is None:
            STATE.on_submit(self._index)

    def _compute(self):
        if self._computed:
            return
        self._computed = True
        pool = self._pool
        fn, args, kwargs = self._task
        self._task = None
        if self._tag is not None:
            STATE.enter(self._tag)
        else:
            keep_tasks = STATE.keep_tasks
            if keep_tasks is not None and self._index not in keep_tasks:
                self._result = None
                return
            STATE.enter(('t', self._index))
        if pool._broken or not pool._start_worker():
            self._exception = BrokenProcessPool(_WORKER_DIED)
            return
        try:
            payload = pickle.dumps((fn, args, kwargs))
        except BaseException as error:
            self._exception = error
            return
        try:
            fn, args, kwargs = pickle.loads(payload)
        except BaseException:
            pool._mark_broken()
            self._exception = BrokenProcessPool(_WORKER_DIED)
            return
        try:
            value = fn(*args, **kwargs)
        except SystemExit:
            pool._mark_broken()
            self._exception = BrokenProcessPool(_WORKER_DIED)
            return
        except BaseException as error:
            if isinstance(error, KeyboardInterrupt):
                raise
            self._exception = error
            return
        try:
            payload = pickle.dumps(value)
        except BaseException as error:
            self._exception = error
            return
        try:
            self._result = pickle.loads(payload)
        except BaseException:
            pool._mark_broken()
            self._exception = BrokenProcessPool(_WORKER_DIED)

    def result(self, timeout=None):
        self._compute()
        if self._exception is not None:
            raise self._exception
        return self._result

    def exception(self, timeout=None):
        self._compute()
        return self._exception

    def cancel(self):
        return False


class InlinePool:
    """ProcessPoolExecutor replacement that runs tasks inline.

    `broke_any` is set when a pool created outside a test (class-level, e.g. in
    setUpClass) breaks; the coordinator reads and clears it after each test.
    The instance attribute names `_started` and `_shutdown` are referenced by
    the coordinator's class-state comparison.
    """
    broke_any = False

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        self._class_level = not STATE.in_test
        self._initializer = initializer
        self._initargs = initargs
        self._started = False
        self._broken = False
        self._shutdown = False

    def _mark_broken(self):
        self._broken = True
        if self._class_level:
            InlinePool.broke_any = True

    def _start_worker(self) -> bool:
        """Run the initializer once; False when the pool is (or becomes) broken."""
        if self._started:
            return not self._broken
        self._started = True
        if self._initializer is not None:
            try:
                self._initializer(*self._initargs)
            except BaseException as error:
                if isinstance(error, KeyboardInterrupt):
                    raise
                self._mark_broken()
                return False
        return True

    def submit(self, fn, /, *args, **kwargs):
        if self._broken:
            raise BrokenProcessPool('A child process terminated abruptly, the process pool is not usable anymore')
        if self._shutdown:
            raise RuntimeError('cannot schedule new futures after shutdown')
        return LazyFuture(self, fn, args, kwargs)

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        futures = [self.submit(fn, *args) for args in zip(*iterables)]

        def results():
            for future in futures:
                yield future.result()
        return results()

    def shutdown(self, wait=True, *, cancel_futures=False):
        self._shutdown = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False


def install():
    """Make concurrent.futures.ProcessPoolExecutor the inline pool."""
    import concurrent.futures
    concurrent.futures.ProcessPoolExecutor = InlinePool
