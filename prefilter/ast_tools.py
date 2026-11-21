"""Small AST helpers shared by the edit recognizers.

Masks are Python ints with one bit per test of the build-time inventory; the profile
tables store them as hex strings.
"""
import ast
from functools import cache


@cache
def baseline_dump(tree):
    """ast.dump of an unmodified tree, memoized per tree object (base trees are shared)."""
    return ast.dump(tree)


def spanning_nodes(root, first, last):
    """Yield the nodes under root whose line span covers first..last (unpositioned nodes always)."""
    stack = [root]
    while stack:
        node = stack.pop()
        if hasattr(node, 'lineno') and (node.lineno > first or node.end_lineno < last):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def child_mask(path, maps):
    """Tests that may run the file in a child process, where coverage was not measured."""
    return maps['unknown_children'] | int(maps['child_paths'].get(path, '0'), 16)


def unchanged_with_original(root, expected, node, replacements):
    """Whether root dumps to expected once node's fields are temporarily set to replacements."""
    saved = {field: getattr(node, field) for field in replacements}
    try:
        for field, value in replacements.items():
            setattr(node, field, value)
        return ast.dump(root) == expected
    finally:
        for field, value in saved.items():
            setattr(node, field, value)


def future_annotations(tree):
    """Whether the module has `from __future__ import annotations`."""
    return any(
        isinstance(statement, ast.ImportFrom)
        and statement.module == '__future__'
        and any(alias.name == 'annotations' for alias in statement.names)
        for statement in tree.body
    )


def counterpart(tree, definition):
    """The definition in tree with the same type, name, line and column as definition, if any."""
    return next(
        (
            candidate
            for candidate in ast.walk(tree)
            if type(candidate) is type(definition)
            and candidate.name == definition.name
            and candidate.lineno == definition.lineno
            and candidate.col_offset == definition.col_offset
        ),
        None,
    )


def dotted_path(path):
    """'a/b/c.py' -> 'a.b.c' (keeps a trailing '__init__')."""
    return path[:-3].replace('/', '.')


def module_name(path):
    """Importable module name of a repository-relative .py path."""
    return path[:-3].replace('/', '.').removesuffix('.__init__')
