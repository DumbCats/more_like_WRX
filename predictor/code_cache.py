"""Keep every code object compiled from the sqlglot checkout in memory.

Importing this module wraps `SourceFileLoader.get_code` (unless
BLAST_NO_CODE_CACHE=1). It must be imported before anything else of ours, so
the wrapper is in place before sqlglot or its tests are imported. Forked
children that re-import modules then reuse the cached code objects instead of
reading .pyc files again, and reductions can find a test method's original
code object (see `child_runs.pinned_code`).
"""
import os
from importlib.machinery import SourceFileLoader

# source path -> code object, for files under the checkout
CODE_CACHE = {}

_CHECKOUT_PREFIX = os.path.join(os.environ.get('BLAST_REPO', '/repo'), '')
_uncached_get_code = SourceFileLoader.get_code


def _get_code_cached(loader, fullname):
    """SourceFileLoader.get_code, remembering the result for checkout files."""
    path = loader.path
    code = CODE_CACHE.get(path)
    if code is None:
        code = _uncached_get_code(loader, fullname)
        if path.startswith(_CHECKOUT_PREFIX):
            CODE_CACHE[path] = code
    return code


INSTALLED = os.environ.get('BLAST_NO_CODE_CACHE') != '1'
if INSTALLED:
    SourceFileLoader.get_code = _get_code_cached
