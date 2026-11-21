"""Build generated/default-flow.json: falsy defaults whose value only reaches safe uses.

Usage: default_flow.py <repo>  (writes JSON to stdout)

A default parameter site 'path:def_line:name' is safe when, inside its function, the parameter
is only tested for truth or passed on (by position or keyword) to another known function's
parameter that is itself safe (up to a small depth). 'observers' lists lines in tests/ and
sqlglot/ that introspect signatures or defaults.
"""
import ast
import importlib
import inspect
import json
import sys
from collections import Counter
from functools import cache
from pathlib import Path

INTROSPECTION_ATTRIBUTES = {'signature', 'getfullargspec', '__defaults__', '__kwdefaults__', '__func__'}
DYNAMIC_NAMES = {'locals', 'exec', 'eval', 'vars', 'dir', 'super', 'inspect'}
MAX_DEPTH = 3


def _harmless_introspection(node, parents):
    """getfullargspec(...).args or enumerate(signature(...).parameters) only read parameter names."""
    parent = parents.get(node)
    access = parents.get(parent)
    return (
        isinstance(parent, ast.Call)
        and isinstance(access, ast.Attribute)
        and (
            node.attr == 'getfullargspec'
            and access.attr == 'args'
            or (
                node.attr == 'signature'
                and access.attr == 'parameters'
                and isinstance(parents.get(access), ast.Call)
                and getattr(parents[access].func, 'id', None) == 'enumerate'
            )
        )
    )


def build(root):
    sys.path.insert(0, str(root))
    functions = {}  # (module, class name or None, function name) -> (FunctionDef, parent map)
    module_files = {}
    overwritten = set()  # attribute names that are assigned or named by a string somewhere
    definition_counts = Counter()
    observers = {}
    for path in sorted(root.rglob('*.py')):
        relative = path.relative_to(root).as_posix()
        if not relative.startswith(('tests/', 'sqlglot/')):
            continue
        tree = ast.parse(path.read_bytes())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        module = relative[:-3].replace('/', '.').removesuffix('.__init__')
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                definition_counts[node.name] += 1
            elif isinstance(node, ast.Attribute):
                if not isinstance(node.ctx, ast.Load):
                    overwritten.add(node.attr)
                if node.attr in INTROSPECTION_ATTRIBUTES:
                    if _harmless_introspection(node, parents):
                        continue
                    observers.setdefault(relative, []).append(node.lineno)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                tail = node.value.rsplit('.', 1)[-1]
                if tail.isidentifier():
                    overwritten.add(tail)
        if not relative.startswith('sqlglot/'):
            continue
        module_files[module] = relative
        for top in tree.body:
            if isinstance(top, ast.ClassDef) and (top.decorator_list or top.keywords):
                continue
            members = [(top.name, member) for member in top.body] if isinstance(top, ast.ClassDef) else [(None, top)]
            for owner, function in members:
                if isinstance(function, ast.FunctionDef):
                    functions[module, owner, function.name] = (function, parents)

    @cache
    def fixed_classmethod(module, owner, name):
        """Whether owner.name is a classmethod that no subclass overrides."""
        try:
            cls = getattr(importlib.import_module(module), owner)
            if type(cls) is not type or not isinstance(inspect.getattr_static(cls, name), classmethod):
                return False
            method = inspect.getattr_static(cls, name)
            pending = list(cls.__subclasses__())
            seen = set()
            while pending:
                subclass = pending.pop()
                if subclass in seen:
                    continue
                if type(subclass) is not type or inspect.getattr_static(subclass, name) is not method:
                    return False
                seen.add(subclass)
                pending.extend(subclass.__subclasses__())
            return True
        except (AttributeError, ImportError, TypeError):
            return False

    @cache
    def fixed_function(module, name):
        """Whether module.name at run time is the function defined in module's source."""
        try:
            value = getattr(importlib.import_module(module), name)
            node, _parents = functions[module, None, name]
            return inspect.isfunction(value) and value.__module__ == module and value.__code__.co_firstlineno == node.lineno
        except (AttributeError, ImportError, KeyError):
            return False

    @cache
    def safe(key, parameter, depth):
        """Whether the parameter of function key is only truth-tested or forwarded safely."""
        if depth > MAX_DEPTH:
            return False
        module, owner, _name = key
        function, parents = functions[key]
        if function.decorator_list and not (
            len(function.decorator_list) == 1 and getattr(function.decorator_list[0], 'id', None) in {'classmethod', 'staticmethod'}
        ):
            return False
        parameters = [argument.arg for argument in function.args.posonlyargs + function.args.args + function.args.kwonlyargs]
        imported = {}
        body_nodes = [node for statement in function.body for node in ast.walk(statement)]
        for node in body_nodes:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Lambda)) or (isinstance(node, ast.Name) and node.id in DYNAMIC_NAMES):
                return False
            if isinstance(node, ast.ImportFrom) and not node.level and node.module:
                for alias in node.names:
                    imported[alias.asname or alias.name] = (node.module, None, alias.name)
        for node in body_nodes:
            if not isinstance(node, ast.Name) or node.id != parameter or not isinstance(node.ctx, ast.Load):
                continue
            use = parents[node]
            if (
                isinstance(use, (ast.If, ast.While, ast.IfExp))
                and use.test is node
                or (isinstance(use, ast.UnaryOp) and isinstance(use.op, ast.Not))
                or (isinstance(use, ast.BoolOp) and isinstance(use.op, ast.Or) and use.values[-1] is not node)
            ):
                continue
            call = parents[use] if isinstance(use, ast.keyword) else use
            if not isinstance(call, ast.Call):
                return False
            callee = call.func
            receiver_shift = 0
            if (
                isinstance(callee, ast.Name)
                and callee.id in imported
                and callee.id not in parameters
                and not any(isinstance(other, ast.Name) and other.id == callee.id and not isinstance(other.ctx, ast.Load) for other in body_nodes)
            ):
                target = imported[callee.id]
                if not fixed_function(target[0], target[2]):
                    return False
            elif (
                isinstance(callee, ast.Attribute)
                and owner
                and isinstance(callee.value, ast.Name)
                and parameters
                and callee.value.id == parameters[0]
                and function.decorator_list
                and getattr(function.decorator_list[0], 'id', None) == 'classmethod'
                and definition_counts[callee.attr] == 1
                and fixed_classmethod(module, owner, callee.attr)
            ):
                target = (module, owner, callee.attr)
                receiver_shift = 1
            else:
                return False
            if target not in functions or target[-1] in overwritten:
                return False
            target_function = functions[target][0]
            target_positional = target_function.args.posonlyargs + target_function.args.args
            if isinstance(use, ast.keyword):
                target_parameter = use.arg
            elif (
                node in call.args
                and call.args.index(node) + receiver_shift < len(target_positional)
                and all(not isinstance(argument, ast.Starred) for argument in call.args)
            ):
                target_parameter = target_positional[call.args.index(node) + receiver_shift].arg
            else:
                return False
            if (
                not target_parameter
                or target_parameter not in [argument.arg for argument in target_positional + target_function.args.kwonlyargs]
                or not safe(target, target_parameter, depth + 1)
            ):
                return False
        return True

    sites = {}
    for key, (function, _parents) in functions.items():
        positional = function.args.posonlyargs + function.args.args
        defaulted = [
            *zip(positional[len(positional) - len(function.args.defaults):], function.args.defaults),
            *zip(function.args.kwonlyargs, function.args.kw_defaults),
        ]
        for argument, default in defaulted:
            if isinstance(default, ast.Constant) and not default.value and safe(key, argument.arg, 0):
                sites[f'{module_files[key[0]]}:{function.lineno}:{argument.arg}'] = True
    return dict(sites=sites, observers=observers)


if __name__ == '__main__':
    print(json.dumps(build(Path(sys.argv[1]))))
