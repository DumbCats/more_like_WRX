"""Build generated/fixture-fields.json: which fields of fixture pairs each test reads.

Usage: fixture_fields.py <repo> <coverage.json.gz>  (writes JSON to stdout)

For fixtures bound with `cls.attr = list(load_sql_fixture_pairs(...))`, maps the fixture path
to three masks, one per (meta, sql, expected) field: the tests that may read that field.
"""
import ast
import gzip
import json
import sys
from pathlib import Path

from fixture_map import class_bindings

ALL_FIELDS = {0, 1, 2}
DYNAMIC_SCOPE_NAMES = {'locals', 'eval', 'vars', 'exec'}


def fields(node, method, methods, parents, active):
    """Indices of the (meta, sql, expected) fields that may be read from node's value."""
    parent = parents.get(node)
    if isinstance(parent, ast.IfExp) and node in (parent.body, parent.orelse):
        return fields(parent, method, methods, parents, active)
    if isinstance(parent, ast.Subscript) and parent.value is node:
        holder = parents.get(parent)
        while isinstance(holder, ast.IfExp) and parent in (holder.body, holder.orelse):
            parent, holder = holder, parents.get(holder)
        if isinstance(holder, ast.Assign) and len(holder.targets) == 1:
            return unpacked_fields(holder.targets[0], method)
    if isinstance(parent, (ast.comprehension, ast.For)) and parent.iter is node:
        return unpacked_fields(parent.target, method)
    if (
        isinstance(parent, ast.Call)
        and isinstance(parent.func, ast.Attribute)
        and isinstance(parent.func.value, ast.Name)
        and parent.func.value.id == 'self'
        and node in parent.args
    ):
        callee = methods.get(parent.func.attr)
        if callee is None or callee in active or callee.decorator_list:
            return ALL_FIELDS
        position = 1 + parent.args.index(node)
        if position >= len(callee.args.args):
            return ALL_FIELDS
        parameter = callee.args.args[position].arg
        callee_nodes = list(ast.walk(callee))
        if any(isinstance(item, ast.Name) and item.id in DYNAMIC_SCOPE_NAMES for item in callee_nodes):
            return ALL_FIELDS
        uses = [item for item in callee_nodes if isinstance(item, ast.Name) and item.id == parameter]
        if any(not isinstance(use.ctx, ast.Load) for use in uses):
            return ALL_FIELDS
        return set().union(*(fields(use, callee, methods, parents, active | {callee}) for use in uses))
    return ALL_FIELDS


def unpacked_fields(target, method):
    """Fields used after `meta, sql, expected = ...` unpacking inside method."""
    if not isinstance(target, ast.Tuple) or len(target.elts) != 3:
        return ALL_FIELDS
    if any(not isinstance(element, ast.Name) for element in target.elts):
        return ALL_FIELDS
    loaded = {item.id for item in ast.walk(method) if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)}
    if loaded & {'locals', 'exec', 'vars', 'eval'}:
        return ALL_FIELDS
    return {position for position, element in enumerate(target.elts) if element.id in loaded}


def build(root, coverage_path):
    with gzip.open(coverage_path, 'rt') as handle:
        coverage = json.load(handle)
    result = {}
    for test_path in (root / 'tests').rglob('test_*.py'):
        tree = ast.parse(test_path.read_bytes())
        relative = test_path.relative_to(root).as_posix()
        line_masks = coverage['lines'].get(relative, {})
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            methods = {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}
            for attribute, fixture in class_bindings(cls):
                class_tests = sum(1 << position for position, name in enumerate(coverage['names']) if name.startswith(f'{relative}::{cls.name}::'))
                fixture_path = 'tests/fixtures/' + fixture
                if fixture_path in result:
                    raise ValueError('multiple fixture bindings')
                field_masks = [int(coverage['files'].get(fixture_path, '0'), 16) & ~class_tests] * 3
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and node.value == attribute:
                        break
                    if not (isinstance(node, ast.Attribute) and node.attr == attribute and isinstance(node.ctx, ast.Load)):
                        continue
                    method = parents.get(node)
                    while method is not None and not isinstance(method, ast.FunctionDef):
                        method = parents.get(method)
                    if method not in methods.values():
                        break
                    for field in fields(node, method, methods, parents, {method}):
                        field_masks[field] |= int(line_masks.get(str(node.lineno), '0'), 16)
                else:
                    result[fixture_path] = [hex(mask) for mask in field_masks]
    return result


if __name__ == '__main__':
    print(json.dumps(build(Path(sys.argv[1]), Path(sys.argv[2]))))
