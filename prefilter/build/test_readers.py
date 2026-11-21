"""Build generated/metadata-readers.json and generated/frame-readers.json from the test sources.

Usage: test_readers.py <repo> <metadata-readers.json> <frame-readers.json>

metadata-readers.json lists the test files mentioning expression_metadata; frame-readers.json
maps every test file to the introspection kinds it uses (docstrings, tracemalloc, frames).
"""
import json
import re
import sys
from pathlib import Path

INTROSPECTION_PATTERNS = {
    'documentation_readers': r'__doc__|getdoc|doctest|pydoc|help[(]',
    'memory_readers': r'tracemalloc',
    'frame_readers': r'__code__|co_varnames|f_locals|currentframe|_getframe|settrace|setprofile|inspect[.]|dis[.]',
}


def metadata_readers(root):
    return [path.relative_to(root).as_posix() for path in (root / 'tests').rglob('*.py') if 'expression_metadata' in path.read_text().lower()]


def introspection_kinds(root):
    return {
        path.relative_to(root).as_posix(): [kind for kind, pattern in INTROSPECTION_PATTERNS.items() if re.search(pattern, path.read_text())]
        for path in (root / 'tests').rglob('*.py')
    }


def main():
    root = Path(sys.argv[1])
    Path(sys.argv[2]).write_text(json.dumps(metadata_readers(root)) + '\n')
    Path(sys.argv[3]).write_text(json.dumps(introspection_kinds(root)) + '\n')


if __name__ == '__main__':
    main()
