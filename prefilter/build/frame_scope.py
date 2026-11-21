"""Build generated/frame-scope.json: source files whose code test_typing.py can introspect.

Usage: frame_scope.py <repo>  (writes JSON to stdout)

tests/test_typing.py inspects the annotator callables in EXPRESSION_METADATA; their code may
live outside sqlglot/typing, so every file defining one of them is listed.
"""
import importlib
import json
import pkgutil
import sys
from pathlib import Path


def callables(value, seen):
    """Functions reachable from value through containers and closures."""
    if id(value) in seen:
        return
    seen.add(id(value))
    if isinstance(value, dict):
        for item in value.values():
            yield from callables(item, seen)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from callables(item, seen)
    elif hasattr(value, '__code__'):
        yield value
        for cell in getattr(value, '__closure__', None) or ():
            try:
                yield from callables(cell.cell_contents, seen)
            except ValueError:
                pass


def main():
    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root))
    import sqlglot.typing

    package = Path(sqlglot.typing.__file__).parent
    files = {path.relative_to(root).as_posix() for path in package.glob('*.py')}
    seen = set()
    for info in pkgutil.iter_modules([str(package)]):
        module = importlib.import_module(f'sqlglot.typing.{info.name}')
        for function in callables(getattr(module, 'EXPRESSION_METADATA', {}), seen):
            source = Path(function.__code__.co_filename).resolve()
            try:
                files.add(source.relative_to(root).as_posix())
            except ValueError:
                pass
    json.dump({'tests/test_typing.py': sorted(files)}, sys.stdout)


if __name__ == '__main__':
    main()
