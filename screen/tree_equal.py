"""Exact comparison of two syntax trees, equal to ast.dump(a) == ast.dump(b) without the text.

ast.dump writes the class name and, for every field, name=value; it leaves out a field that
is missing, None or an empty list (on Constant and MatchSingleton only a field whose class
default is None, such as kind). Positions are not written. The comparison walks both trees
by the same rules and stops at the first difference."""
import ast
import os

_AST = ast.AST
_SHOW = (ast.Constant, ast.MatchSingleton)
_PLAIN = (str, int, bytes, bool, type(None))
_CHECK = os.environ.get("SCREEN_CHECK_EQUAL") == "1"


def _same(a, b):
    stack = [(a, b)]
    while stack:
        a, b = stack.pop()
        if isinstance(a, _AST):
            cls = type(a)
            if cls is not type(b):
                if not isinstance(b, _AST) or cls.__name__ != type(b).__name__ or a._fields != b._fields:
                    return False
            shown = isinstance(a, _SHOW)
            for name in a._fields:
                x = getattr(a, name, None)
                y = getattr(b, name, None)
                if x is y:
                    if x is None and shown and getattr(cls, name, ...) is not None and (
                            hasattr(a, name) != hasattr(b, name)):
                        return False
                    continue
                if shown:
                    # None is written unless the class default of the field is None
                    if x is None or y is None:
                        if getattr(cls, name, ...) is None or hasattr(a, name) != hasattr(b, name):
                            return False
                else:
                    if x is None or (type(x) is list and not x):
                        if y is None or (type(y) is list and not y):
                            continue
                        return False
                    if y is None or (type(y) is list and not y):
                        return False
                stack.append((x, y))
        elif isinstance(a, list):
            if not isinstance(b, list) or len(a) != len(b):
                return False
            stack.extend(zip(a, b))
        elif isinstance(b, (_AST, list)):
            return False
        elif type(a) is type(b) and type(a) in _PLAIN:
            if a != b:
                return False
        elif repr(a) != repr(b):
            return False
    return True


# Walks of the trees of one file analysis (pure speed). ast.walk(tree) yields the nodes breadth
# first, each node's children in the order of iter_child_nodes; nodes(tree) is that sequence as a
# list, computed once per tree. Statements (and except handlers and match cases) only occur in
# list fields of statements, except handlers, match cases and modules, never inside expressions,
# so the breadth first walk that only follows those list items yields exactly the subsequence of
# ast.walk(tree) of the statement-like nodes, in the same order: select() uses it when every
# requested type is a statement type. The lists are valid only while the trees keep the content
# they had when the lists were made: mask_for_file resets them at its start and end, and they are
# only read when no tree is temporarily altered (the recognizers restore every change before
# they walk again; the one lasting change, normalize_comparisons, happens before any cached walk).
_STATEMENT_LIKE = (ast.stmt, ast.excepthandler, ast.match_case)
_nodes = {}
_statements = {}
_selected = {}


def reset_walks():
    _nodes.clear()
    _statements.clear()
    _selected.clear()


def nodes(tree):
    """list(ast.walk(tree))"""
    found = _nodes.get(tree)
    if found is None:
        found = [tree]
        append = found.append
        i = 0
        while i < len(found):
            node = found[i]
            i += 1
            for name in node._fields:
                value = getattr(node, name, None)
                if isinstance(value, _AST):
                    append(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, _AST):
                            append(item)
        _nodes[tree] = found
    return found


def _statement_nodes(tree):
    found = _statements.get(tree)
    if found is None:
        found = [tree]
        append = found.append
        i = 0
        while i < len(found):
            node = found[i]
            i += 1
            for name in node._fields:
                value = getattr(node, name, None)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, _STATEMENT_LIKE):
                            append(item)
        _statements[tree] = found
    return found


def statement_children(node):
    """The statement-like nodes among list(ast.iter_child_nodes(node)), in that order."""
    found = []
    for name in node._fields:
        value = getattr(node, name, None)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, _STATEMENT_LIKE):
                    found.append(item)
    return found


def select(tree, types):
    """[n for n in ast.walk(tree) if isinstance(n, types)] for a tuple of node types (do not modify it)"""
    key = (tree, types)
    found = _selected.get(key)
    if found is None:
        source = _statement_nodes(tree) if all(issubclass(t, ast.stmt) for t in types) else nodes(tree)
        found = _selected[key] = [n for n in source if isinstance(n, types)]
    return found


def same_tree(a, b):
    """ast.dump(a) == ast.dump(b)"""
    if not isinstance(a, _AST) or not isinstance(b, _AST):
        return ast.dump(a) == ast.dump(b)  # as before (ast.dump raises for anything else)
    result = _same(a, b)
    if _CHECK and result != (ast.dump(a) == ast.dump(b)):
        raise AssertionError("same_tree differs from ast.dump")
    return result
