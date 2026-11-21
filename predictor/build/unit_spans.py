"""Offline helper: split a Python source file into top-level "units".

Not used at run time. A unit is a top-level statement, or for a class the
header part (up to its first member) plus, recursively, each member. Each unit
is [first line incl. decorators, last line, class path, is_class_header].
"""
from __future__ import annotations

import ast

FUTURE_ANNOTATIONS_FLAG = 16777216


def _first_line(node):
    decorators = getattr(node, 'decorator_list', None)
    if decorators:
        return min(node.lineno, min((decorator.lineno for decorator in decorators)))
    return node.lineno


def build_units(src: str):
    """(units, compile flags, names imported at module level) of a source text."""
    tree = ast.parse(src)
    units = []
    flags = 0
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == '__future__':
            if any((alias.name == 'annotations' for alias in node.names)):
                flags = FUTURE_ANNOTATIONS_FLAG

    def walk(body, class_path):
        for node in body:
            first = _first_line(node)
            if isinstance(node, ast.ClassDef):
                first_member = _first_line(node.body[0])
                if first_member == node.lineno:
                    units.append([first, node.end_lineno, class_path, 0])
                    continue
                units.append([first, first_member - 1, class_path, 1])
                walk(node.body, class_path + (node.name,))
            else:
                units.append([first, node.end_lineno, class_path, 0])
    walk(tree.body, ())
    names = set()

    def scan(nodes):
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name != '*':
                        names.add(alias.asname or alias.name.partition('.')[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            else:
                for field in ('body', 'orelse', 'finalbody', 'handlers', 'cases'):
                    children = getattr(node, field, None)
                    if isinstance(children, list):
                        scan(children)
    scan(tree.body)
    return (units, flags, sorted(names))
