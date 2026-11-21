"""Replaying selected unit tests against a patched copy of the repository.

The patched tree is a symlink overlay of the repository with the edited files written out.
Tests run in a forked child; when possible the child is forked from a warm server process
that has already imported sqlglot, and the edited modules are loaded from the overlay.
"""
import atexit
import importlib
import json
import multiprocessing
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

RESULT_FILE = 'replay-result.json'
WARM_START_SECONDS = 2
WARM_GRACE_SECONDS = 1.5

# (pipe to the warm server, sqlglot modules it imported, files it opened); False once unusable.
_server = None


def overlay(source, dest, changes):
    """Mirror source into dest with symlinks, writing changes ({relative path: text}) as files.

    __pycache__ entries of changed modules are left out so the edited sources get compiled.
    """
    by_entry = {}
    for relative, content in changes.items():
        head, _separator, rest = relative.partition('/')
        by_entry.setdefault(head, {})[rest] = content
    for entry in source.iterdir():
        if entry.name == '__pycache__':
            changed_stems = {Path(name).stem for name in by_entry if name.endswith('.py')}
            cache_dir = dest / entry.name
            if changed_stems:
                cache_dir.mkdir()
                for cached in entry.iterdir():
                    if cached.name.split('.', 1)[0] not in changed_stems:
                        (cache_dir / cached.name).symlink_to(cached)
            else:
                cache_dir.symlink_to(entry, target_is_directory=True)
            continue
        target = dest / entry.name
        if entry.name not in by_entry:
            target.symlink_to(entry, target_is_directory=entry.is_dir())
        elif entry.is_dir():
            target.mkdir()
            overlay(entry, target, by_entry[entry.name])
        else:
            target.write_text(by_entry[entry.name][''], encoding='utf-8')


def run_in_child(root, paths, ids, seconds, source=None):
    """Run run_tests in a forked child with a time limit; True when it completed cleanly."""
    child = multiprocessing.get_context('fork').Process(target=run_tests, args=(root, paths, ids, source))
    child.start()
    child.join(seconds)
    if child.is_alive():
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            child.kill()
        child.join()
    succeeded = child.exitcode == 0
    child.close()
    return succeeded


def _silence_output():
    with open(os.devnull, 'wb') as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)


def _warm_server_loop(source, connection):
    """Server process: import sqlglot once, then fork a test child per request."""
    _silence_output()
    accessed = set()
    collecting = True

    def record_opened_files(event, args):
        if collecting and event == 'open' and isinstance(args[0], (str, bytes)):
            accessed.add(os.path.abspath(os.fsdecode(args[0])))

    sys.addaudithook(record_opened_files)
    if any(name == 'sqlglot' or name.startswith(('sqlglot.', 'tests.')) for name in sys.modules):
        connection.send(None)
        return
    sys.path.insert(0, str(source))
    os.chdir(source)
    os.environ['SKIP_INTEGRATION'] = '0'
    importlib.import_module('sqlglot')
    collecting = False
    loaded = {name for name in sys.modules if name == 'sqlglot' or name.startswith('sqlglot.')}
    connection.send((loaded, accessed))
    while True:
        request = connection.recv()
        if request is None:
            return
        root, paths, ids, seconds = request
        connection.send(run_in_child(root, paths, ids, seconds, source))


def _warm_server(source):
    """Start the warm server on first use; returns its state tuple or False."""
    global _server
    if _server is None:
        context = multiprocessing.get_context('fork')
        client, worker = context.Pipe()
        process = context.Process(target=_warm_server_loop, args=(source, worker))
        process.start()
        worker.close()

        def stop():
            try:
                client.send(None)
            except (BrokenPipeError, OSError):
                pass
            process.join(0.3)
            if process.is_alive():
                process.kill()
                process.join()
            client.close()

        atexit.register(stop)
        state = client.recv() if client.poll(WARM_START_SECONDS) else None
        _server = (client, *state) if state else False
    return _server


def _run_warm(source, dest, paths, ids, seconds):
    """Run the tests via the warm server when none of the edited modules was preloaded there."""
    global _server
    server = _warm_server(source)
    if server:
        client, loaded, accessed = server
        eligible = all(
            path[:-3].replace('/', '.').removesuffix('.__init__') not in loaded and str(source / path) not in accessed
            for path in paths
        )
        if eligible:
            client.send((dest, paths, ids, seconds))
            if client.poll(seconds + WARM_GRACE_SECONDS):
                return client.recv()
            _server = False
            return False
    return run_in_child(dest, paths, ids, seconds)


def failing_tests(source, files, ids, seconds):
    """The subset of test ids that fail with the edited files, or None when undecided."""
    try:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            overlay(source, root, {path: revised for path, _base, revised, *_lines in files})
            if not _run_warm(source, root, [path for path, *_rest in files], ids, seconds):
                return None
            failed = set(json.loads((root / RESULT_FILE).read_text()))
            return failed if failed <= set(ids) else None
    except (EOFError, OSError, ValueError, KeyError, TypeError, RuntimeError):
        return None


def _redirect_preloaded_modules(root, source):
    """Point already-imported sqlglot modules (from the warm server) at the overlay root."""
    for name, module in list(sys.modules.items()):
        if name == 'sqlglot' or name.startswith('sqlglot.'):
            directory = root.joinpath(*name.split('.'))
            if hasattr(module, '__path__'):
                module.__path__ = [str(directory)]
                module.__spec__.submodule_search_locations = module.__path__
            filename = getattr(module, '__file__', None)
            if filename and Path(filename).is_relative_to(source):
                module.__file__ = str(root / Path(filename).relative_to(source))
                module.__spec__.origin = module.__file__


def run_tests(root, paths, ids, source=None):
    """Child process: run the unittest ids under root and write the failing ids to RESULT_FILE.

    Exits non-zero whenever the run cannot be trusted (unsupported test, skips, edited
    module not loaded from the overlay, ...).
    """
    os.setsid()
    _silence_output()
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.dont_write_bytecode = True
    os.environ.update(PYTHONPATH=str(root), SKIP_INTEGRATION='0', PYTHONDONTWRITEBYTECODE='1')
    if source is not None:
        _redirect_preloaded_modules(root, source)
        changed = {str(source / path) for path in paths}
        basenames = {Path(path).name for path in paths}
        bypassed = []

        def detect_original_reads(event, args):
            if event == 'open' and isinstance(args[0], str) and os.path.basename(args[0]) in basenames:
                if os.path.realpath(args[0]) in changed:
                    bypassed.append(args[0])

        sys.addaudithook(detect_original_reads)
    suite = unittest.TestSuite()
    labels = {}
    for label in sorted(ids):
        filename, class_name, method = label.split('::')
        module = importlib.import_module(filename[:-3].replace('/', '.'))
        cls = getattr(module, class_name)
        if (
            not issubclass(cls, unittest.TestCase)
            or not method.startswith('test')
            or hasattr(module, 'load_tests')
            or hasattr(cls, 'pytestmark')
            or hasattr(getattr(cls, method), 'pytestmark')
        ):
            sys.exit(1)
        test = cls(method)
        suite.addTest(test)
        labels[test] = label
    failed = set()
    completed = set()

    class Recorder(unittest.TestResult):
        def stopTest(self, test):
            completed.add(labels[test])
            super().stopTest(test)

        def addSubTest(self, test, subtest, error):
            if error is not None:
                failed.add(labels[test])

        def addError(self, test, error):
            failed.add(labels[test])

        def addFailure(self, test, error):
            failed.add(labels[test])

    recorder = Recorder()
    suite.run(recorder)
    verified = source is None or not bypassed
    for path in paths:
        loaded_from = getattr(sys.modules.get(path[:-3].replace('/', '.').removesuffix('.__init__')), '__file__', None)
        if not loaded_from or Path(loaded_from).resolve() != root / path:
            verified = False
            break
    if not verified or recorder.skipped or recorder.expectedFailures or recorder.unexpectedSuccesses or set(ids) != completed:
        sys.exit(1)
    (root / RESULT_FILE).write_text(json.dumps(sorted(failed)))
