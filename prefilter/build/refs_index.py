"""Build generated/refs.json.gz: a static reference index of sqlglot and its tests.

Usage: refs_index.py <repo> <output.gz>

Keys of the index (consumed by prefilter/bindings.py and the recognizers):
  loads         name -> [[file, line, 'n'|'a', assigned]] loads of names bound in sqlglot;
                assigned is 'M:x'/'C:x' when evaluated into module/class binding x at import,
                '' at import time otherwise, and None inside functions/lambdas
  imports       file -> [[module, imported name ('' for plain import), alias]]
  cached        file -> lines of functions that fill module-level caches (and their callees)
  strings / dynamic_strings / doc_strings / test_strings
                tracked names that appear in strings used for dynamic lookups or doctests
  calls         function name -> call sites of functions with defaults
  escapes       functions with defaults referenced other than by a direct call
  patterns      (prefix, suffix) of f-strings used for dynamic attribute lookups
  special       enum/tuple/dataclass/TypedDict/Protocol classes defined in sqlglot
"""
import ast
import dataclasses
import enum
import gzip
import importlib
import json
import re
import sys
from pathlib import Path

DYNAMIC_CALLS = {'getattr', 'hasattr', 'setattr', 'delattr', 'attrgetter', 'import_module', '__import__', 'eval', 'exec', 'patch', 'object', 'dict'}
# Dynamic calls whose second argument (rather than the first) is the looked-up name.
NAME_IN_SECOND_ARGUMENT = {'getattr', 'hasattr', 'setattr', 'delattr', 'object'}
NAMESPACE_FUNCTIONS = {'globals', 'vars', 'locals'}
MAPPING_LOOKUPS = {'get', 'pop', 'setdefault', '__getitem__'}
WORD = re.compile('[A-Za-z_][A-Za-z0-9_]*')
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def module_bindings(tree):
    """Names bound by assignments and classes at module/class level (through if/try)."""
    names = set()

    def visit_body(statements):
        for statement in statements:
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                names.add(statement.target.id)
            elif isinstance(statement, ast.ClassDef):
                names.add(statement.name)
                visit_body(statement.body)
            elif isinstance(statement, (ast.If, ast.Try)):
                visit_body(statement.body)
                visit_body(getattr(statement, 'orelse', []))

    visit_body(tree.body)
    return names


class LoadCollector(ast.NodeVisitor):
    """Records loads of wanted names into out[name] (see 'loads' in the module docstring)."""

    def __init__(self, file, wanted, out, future=False):
        self.file = file
        self.future = future  # postponed annotations are never evaluated
        self.wanted = wanted
        self.out = out
        self.deferred = 0  # depth of function/lambda bodies
        self.classes = 0  # depth of class bodies
        self.target = []  # binding currently being evaluated at import time ('' for none)
        self.statement = []  # first lines of the enclosing statements inside deferred code

    def record(self, name, node, kind):
        if name not in self.wanted:
            return
        sites = self.out.setdefault(name, [])
        if self.deferred:
            sites.append([self.file, node.lineno, kind, None])
            if self.statement and self.statement[-1] != node.lineno:
                sites.append([self.file, self.statement[-1], kind, None])
        else:
            assigned = self.target[-1] if self.target else ''
            if assigned:
                assigned = ('C:' if self.classes else 'M:') + assigned
            sites.append([self.file, node.lineno, kind, assigned])

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.record(node.id, node, 'n')

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load):
            self.record(node.attr, node, 'a')
        self.generic_visit(node)

    def generic_visit(self, node):
        if isinstance(node, ast.stmt) and self.deferred:
            self.statement.append(node.lineno)
            super().generic_visit(node)
            self.statement.pop()
        else:
            super().generic_visit(node)

    def visit_function(self, node):
        for decorator in getattr(node, 'decorator_list', []):
            self.visit(decorator)
        self.visit(node.args)
        if getattr(node, 'returns', None) is not None and not self.future:
            self.visit(node.returns)
        self.deferred += 1
        self.statement.append(node.lineno)
        body = node.body if isinstance(node.body, list) else [node.body]
        for statement in body:
            self.visit(statement)
        self.statement.pop()
        self.deferred -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = visit_function

    def visit_arguments(self, node):
        for default in [*node.defaults, *[default for default in node.kw_defaults if default is not None]]:
            self.visit(default)
        if not self.deferred and not self.future:
            for argument in [*node.posonlyargs, *node.args, *node.kwonlyargs, node.vararg, node.kwarg]:
                if argument is not None and argument.annotation is not None:
                    self.visit(argument.annotation)

    def visit_assignment(self, node, targets, value):
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        single_name = not self.deferred and len(names) == len(targets) == 1
        for target in targets:
            self.visit(target)
        if getattr(node, 'annotation', None) is not None and not self.deferred and not self.future:
            self.target.append('')
            self.visit(node.annotation)
            self.target.pop()
        if value is not None:
            self.target.append(names[0] if single_name else '')
            self.visit(value)
            self.target.pop()

    def visit_Assign(self, node):
        if self.deferred:
            return self.generic_visit(node)
        self.visit_assignment(node, node.targets, node.value)

    def visit_AnnAssign(self, node):
        if self.deferred:
            self.statement.append(node.lineno)
            self.visit(node.target)
            if node.value is not None:
                self.visit(node.value)
            self.statement.pop()
            return
        self.visit_assignment(node, [node.target], node.value)

    def visit_If(self, node):
        test = node.test
        if isinstance(test, ast.Name) and test.id == 'TYPE_CHECKING' or (isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING'):
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        if self.deferred:
            return self.generic_visit(node)
        self.target.append('')
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.target.pop()
        self.target.append(node.name)
        for base in [*node.bases, *node.keywords]:
            self.visit(base)
        self.target.pop()
        self.classes += 1
        for statement in node.body:
            self.visit(statement)
        self.classes -= 1


def cached_lines(tree):
    """Lines of functions that write into module-level containers, plus the functions they call."""
    module_names = set()
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            for target in statement.targets if isinstance(statement, ast.Assign) else [statement.target]:
                if isinstance(target, ast.Name):
                    module_names.add(target.id)
    functions_by_name = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions_by_name.setdefault(node.name, []).append(node)
    pending = []
    for definitions in functions_by_name.values():
        for function in definitions:
            local_names = {argument.arg for argument in ast.walk(function.args) if isinstance(argument, ast.arg)}
            for node in ast.walk(function):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    local_names.add(node.id)
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.ctx, ast.Store)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in module_names
                    and node.value.id not in local_names
                ):
                    pending.append(function)
                    break
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {'setdefault', 'update'}
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in module_names
                    and node.func.value.id not in local_names
                ):
                    pending.append(function)
                    break
    lines = set()
    visited = set()
    while pending:
        function = pending.pop()
        if id(function) in visited:
            continue
        visited.add(id(function))
        lines.update(range(function.lineno, function.end_lineno + 1))
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                pending.extend(functions_by_name.get(node.func.id, []))
    return sorted(lines)


def defaulted_functions(trees):
    """Names of sqlglot functions that have default values -> files defining them."""
    found = {}
    for file, tree in trees.items():
        if not file.startswith('sqlglot/'):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (node.args.defaults or any(default is not None for default in node.args.kw_defaults)):
                found.setdefault(node.name, []).append(file)
    return found


def receiver_root(node):
    """Root name of an attribute chain (None for a plain name, '' for a non-name root)."""
    if not isinstance(node, ast.Attribute):
        return None
    root = node.value
    while isinstance(root, ast.Attribute):
        root = root.value
    return root.id if isinstance(root, ast.Name) else ''


def local_names(scope):
    """Names local to a function/lambda scope (parameters, stores, imports, nested defs)."""
    names = {argument.arg for argument in ast.walk(scope.args) if isinstance(argument, ast.arg)}
    pending = list(scope.body) if isinstance(scope.body, list) else [scope.body]
    while pending:
        node = pending.pop()
        if isinstance(node, SCOPES) or isinstance(node, ast.ClassDef):
            if not isinstance(node, ast.Lambda):
                names.add(node.name)
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split('.')[0] for alias in node.names)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.difference_update(node.names)
        pending.extend(ast.iter_child_nodes(node))
    return names


def enclosing_scope(node, parents):
    scope = parents.get(node)
    while scope is not None and not isinstance(scope, SCOPES):
        scope = parents.get(scope)
    return scope


def call_sites(trees, names):
    """(name -> call site records, names that escape) for the given function names."""
    calls = {}
    escapes = set()
    for file, tree in trees.items():
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        locals_by_scope = {}

        def bound_locally(node, name):
            scope = enclosing_scope(node, parents)
            if scope is None:
                return False
            if id(scope) not in locals_by_scope:
                locals_by_scope[id(scope)] = local_names(scope)
            return name in locals_by_scope[id(scope)]

        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                name = node.id
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                name = node.attr
            else:
                continue
            if name not in names:
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Call) and parent.func is node:
                scope = enclosing_scope(parent, parents)
                # a call inside a function body runs when that function runs (not at import)
                deferred = scope is not None and (
                    isinstance(scope, ast.Lambda)
                    or not any(
                        parent is evaluated_early or any(parent is inner for inner in ast.walk(evaluated_early))
                        for evaluated_early in [*scope.decorator_list, *scope.args.defaults, *[default for default in scope.args.kw_defaults if default is not None]]
                    )
                )
                calls.setdefault(name, []).append(
                    [
                        file,
                        parent.lineno,
                        sum(1 for argument in parent.args if not isinstance(argument, ast.Starred)),
                        any(isinstance(argument, ast.Starred) for argument in parent.args),
                        [keyword.arg for keyword in parent.keywords if keyword.arg is not None],
                        any(keyword.arg is None for keyword in parent.keywords),
                        deferred,
                        receiver_root(node),
                    ]
                )
            elif not (isinstance(node, ast.Name) and bound_locally(node, name)):
                escapes.add(name)
    return calls, escapes


def special_classes(root):
    """Qualified names of enum/tuple/dataclass/TypedDict/Protocol classes defined in sqlglot."""
    sys.path.insert(0, str(root))
    found = []
    for path in sorted((root / 'sqlglot').rglob('*.py')):
        module_name = '.'.join(path.relative_to(root).with_suffix('').parts).removesuffix('.__init__')
        if module_name.endswith('__main__'):
            continue
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        for value in list(vars(module).values()):
            if not isinstance(value, type) or value.__module__ != module_name:
                continue
            stack = [value]
            while stack:
                cls = stack.pop()
                metaclass = type(cls).__name__
                if issubclass(cls, (enum.Enum, tuple)) or dataclasses.is_dataclass(cls) or metaclass in {'_TypedDictMeta', '_ProtocolMeta'}:
                    found.append(f'{cls.__module__}.{cls.__qualname__}')
                stack.extend(member for member in vars(cls).values() if isinstance(member, type) and member.__module__ == module_name)
    return sorted(set(found))


def _namespace_access(node):
    """`x.__dict__` or globals()/vars()/locals()."""
    return isinstance(node, ast.Attribute) and node.attr == '__dict__' or (isinstance(node, ast.Call) and getattr(node.func, 'id', '') in NAMESPACE_FUNCTIONS)


def main():
    root, output = Path(sys.argv[1]), Path(sys.argv[2])
    sources = sorted([*root.joinpath('sqlglot').rglob('*.py'), *root.joinpath('tests').rglob('*.py')])
    trees = {path.relative_to(root).as_posix(): ast.parse(path.read_bytes()) for path in sources}
    bound_names = set()
    for file, tree in trees.items():
        if file.startswith('sqlglot/'):
            bound_names |= module_bindings(tree)
    defaulted = defaulted_functions(trees)
    calls, escapes = call_sites(trees, set(defaulted))
    tracked = bound_names | set(defaulted)
    library_words = set()  # words in lookup strings, __all__ and doctests of sqlglot
    dynamic_words = set()  # words in dynamic lookup strings of sqlglot
    doctest_words = set()  # words in doctest lines of sqlglot
    patterns = set()
    test_strings = {}
    for file, tree in trees.items():
        words = set()
        is_test = file.startswith('tests/')
        for node in ast.walk(tree):
            lookups = []
            if isinstance(node, ast.Call):
                function = node.func
                function_name = function.attr if isinstance(function, ast.Attribute) else getattr(function, 'id', '')
                if function_name in DYNAMIC_CALLS:
                    lookups = node.args[1:2] if function_name in NAME_IN_SECOND_ARGUMENT else node.args[:1]
                    for argument in lookups:
                        if isinstance(argument, ast.JoinedStr):
                            parts = argument.values
                            prefix = parts[0].value if parts and isinstance(parts[0], ast.Constant) else ''
                            suffix = parts[-1].value if parts and isinstance(parts[-1], ast.Constant) else ''
                            if prefix or suffix:
                                patterns.add((prefix, suffix))
                if isinstance(function, ast.Attribute) and function.attr in MAPPING_LOOKUPS and _namespace_access(function.value):
                    lookups = node.args[:1]
            elif isinstance(node, ast.Subscript):
                if _namespace_access(node.value):
                    lookups = [node.slice]
            elif isinstance(node, ast.Compare) and any(_namespace_access(comparator) for comparator in node.comparators):
                lookups = [node.left]
            elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == '__all__' for target in node.targets):
                for item in ast.walk(node.value):
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        words.update(WORD.findall(item.value))
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for line in node.value.value.splitlines():
                    if line.lstrip().startswith(('>>>', '...')):
                        words.update(WORD.findall(line))
                        if not is_test:
                            doctest_words.update(WORD.findall(line))
            for expression in lookups:
                for item in ast.walk(expression):
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        words.update(WORD.findall(item.value))
                        if not is_test:
                            dynamic_words.update(WORD.findall(item.value))
        if is_test:
            for name in words & tracked:
                test_strings.setdefault(name, []).append(file)
        else:
            library_words |= words
    loads = {}
    imports = {}
    cached = {}
    for file, tree in trees.items():
        postponed = any(
            isinstance(statement, ast.ImportFrom) and statement.module == '__future__' and any(alias.name == 'annotations' for alias in statement.names)
            for statement in tree.body
        )
        LoadCollector(file, bound_names, loads, postponed).visit(tree)
        lines = cached_lines(tree)
        if lines:
            cached[file] = lines
        package = file[:-3].replace('/', '.').removesuffix('.__init__').split('.')
        if not file.endswith('__init__.py'):
            package = package[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.setdefault(file, []).append([alias.name, '', alias.asname or alias.name])
            if isinstance(node, ast.ImportFrom):
                base = package[:len(package) - node.level + 1] if node.level else []
                source = '.'.join([*base, *([node.module] if node.module else [])])
                for alias in node.names:
                    imports.setdefault(file, []).append([source, alias.name, alias.asname or alias.name])
    index = {
        'loads': loads,
        'imports': imports,
        'cached': cached,
        'strings': sorted(tracked & library_words),
        'test_strings': test_strings,
        'dynamic_strings': sorted(tracked & dynamic_words),
        'doc_strings': sorted(tracked & doctest_words),
        'calls': calls,
        'escapes': sorted(escapes),
        'patterns': sorted(patterns),
        'special': special_classes(root),
    }
    with gzip.open(output, 'wt', encoding='utf8') as handle:
        json.dump(index, handle, separators=(',', ':'))


if __name__ == '__main__':
    main()
