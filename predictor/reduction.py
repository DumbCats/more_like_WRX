"""Test reduction: run only the parts of a test that can observe the change.

Two mechanisms share the process-wide `STATE`:
- fixture filtering: tests.helpers.load_sql_fixture_pairs / load_sql_fixtures
  get their code swapped for wrappers that yield only the fixture entries a
  test is told to keep (by index) and tag each yielded entry;
- statement reduction: a test method is recompiled with independent
  statements blanked out (see `method_shape` / `apply_reduction`).
"""
from __future__ import annotations

import ast
import sys
import types


class ReductionState:
    """What the currently running test may skip, and where it currently is."""

    def __init__(self):
        self.keep_files = None
        self.keep_tasks = None
        self.current = None
        self.task_counter = 0
        self.on_enter = None
        self.on_submit = None
        self.files_seen = {}
        self.tasks_seen = 0
        self.keep_changed = None
        self.changed_only = False
        self.in_test = False

    def reset(self, keep_files=None, keep_tasks=None):
        self.keep_files = keep_files
        self.keep_tasks = keep_tasks
        self.current = None
        self.task_counter = 0
        self.files_seen = {}
        self.tasks_seen = 0

    def enter(self, tag):
        self.current = tag
        callback = self.on_enter
        if callback is not None:
            callback(tag)


STATE = ReductionState()


# The two wrappers below are never called as themselves: their code objects are
# installed into the tests.helpers loader functions, so their globals
# (_blast_state, _blast_original_pairs, _blast_original_fixtures) resolve in the
# tests.helpers namespace. They must stay closure-free generators.

def _fixture_pairs_wrapper(filename):
    state = _blast_state
    if not state.in_test:
        yield from _blast_original_pairs(filename)
        return
    keep_files = state.keep_files
    keep = None if keep_files is None else keep_files.get(filename, ())
    keep_changed = state.keep_changed
    if keep_changed is not None and ('p', filename) in keep_changed:
        changed = keep_changed['p', filename]
        if keep is not None:
            keep = set(keep) | changed
        elif state.changed_only:
            keep = changed
    index = -1
    for entry in _blast_original_pairs(filename):
        index += 1
        if keep is None or index in keep:
            state.enter(('f', filename, index))
            yield entry
    state.files_seen[filename] = index + 1
    state.enter(None)


def _fixtures_wrapper(filename):
    state = _blast_state
    if not state.in_test:
        yield from _blast_original_fixtures(filename)
        return
    keep_files = state.keep_files
    keep = None if keep_files is None else keep_files.get(filename, ())
    keep_changed = state.keep_changed
    if keep_changed is not None and ('x', filename) in keep_changed:
        changed = keep_changed['x', filename]
        if keep is not None:
            keep = set(keep) | changed
        elif state.changed_only:
            keep = changed
    index = -1
    for entry in _blast_original_fixtures(filename):
        index += 1
        if keep is None or index in keep:
            state.enter(('f', filename, index))
            yield entry
    state.files_seen[filename] = index + 1
    state.enter(None)


def install_loaders() -> bool:
    """Swap the fixture loaders of tests.helpers for the filtering wrappers (idempotent)."""
    helpers = sys.modules.get('tests.helpers')
    if helpers is None:
        import importlib
        helpers = importlib.import_module('tests.helpers')
    if getattr(helpers, '_blast_state', None) is not None:
        return True
    pairs_loader = getattr(helpers, 'load_sql_fixture_pairs', None)
    fixtures_loader = getattr(helpers, 'load_sql_fixtures', None)
    if not isinstance(pairs_loader, types.FunctionType) or not isinstance(fixtures_loader, types.FunctionType):
        return False
    if pairs_loader.__code__.co_freevars or fixtures_loader.__code__.co_freevars:
        return False
    helpers._blast_state = STATE
    helpers._blast_original_pairs = types.FunctionType(pairs_loader.__code__, helpers.__dict__, 'load_sql_fixture_pairs')
    helpers._blast_original_fixtures = types.FunctionType(fixtures_loader.__code__, helpers.__dict__, 'load_sql_fixtures')
    pairs_loader.__code__ = _fixture_pairs_wrapper.__code__
    fixtures_loader.__code__ = _fixtures_wrapper.__code__
    return True


# --- statement reduction -------------------------------------------------------

def _names(node, context):
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name) and isinstance(child.ctx, context)}


def _bound_names(node):
    """Names a statement binds (assignments, defs, imports, except/match captures)."""
    bound = _names(node, ast.Store)
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(child.name)
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            for alias in child.names:
                bound.add(alias.asname or alias.name.partition('.')[0])
        elif isinstance(child, ast.ExceptHandler) and child.name:
            bound.add(child.name)
        elif isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name:
            bound.add(child.name)
    return bound


def _escapes(node):
    for child in ast.walk(node):
        if isinstance(child, (ast.Return, ast.Yield, ast.YieldFrom, ast.Await, ast.Global, ast.Nonlocal)):
            return True
    return False


def _stores_state(node):
    for child in ast.walk(node):
        if isinstance(child, (ast.Attribute, ast.Subscript)) and isinstance(child.ctx, (ast.Store, ast.Del)):
            return True
    return False


_DROPPABLE_STATEMENTS = (ast.Expr, ast.With, ast.For, ast.If, ast.Try, ast.While, ast.Assign, ast.AnnAssign, ast.AugAssign)


def method_shape(func: ast.FunctionDef):
    """(first line, last line, [(stmt start, stmt end, droppable)]) of a test method.

    A top-level statement is droppable when nothing after it reads what it binds
    and it neither escapes nor mutates attributes/items.
    """
    body = func.body
    first_line = min([func.lineno] + [decorator.lineno for decorator in func.decorator_list])
    read_later = set()
    droppable = [False] * len(body)
    for position in range(len(body) - 1, -1, -1):
        statement = body[position]
        can_drop = isinstance(statement, _DROPPABLE_STATEMENTS)
        if can_drop and isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            can_drop = False
        if can_drop and (_escapes(statement) or _stores_state(statement)):
            can_drop = False
        if can_drop and _bound_names(statement) & read_later:
            can_drop = False
        if can_drop and isinstance(statement, ast.AugAssign):
            can_drop = False
        droppable[position] = can_drop
        read_later |= _names(statement, ast.Load) | _names(statement, ast.Del)
    return (first_line, func.end_lineno, [(statement.lineno, statement.end_lineno, droppable[position]) for position, statement in enumerate(body)])


def file_shapes(src: str):
    """(class name, test method name) -> method_shape for every test method of a file."""
    tree = ast.parse(src)
    shapes = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name.startswith('test'):
                    shapes[node.name, member.name] = method_shape(member)
    return shapes


def reduced_lines(file_lines, shape, keep):
    """The method's source lines with droppable statements not in `keep` blanked."""
    first_line, last_line, statements = shape
    lines = list(file_lines[first_line - 1:last_line])
    kept = 0
    for position, (start, end, droppable) in enumerate(statements):
        if droppable and position not in keep:
            for line_number in range(start, end + 1):
                lines[line_number - first_line] = ''
        else:
            kept += 1
    if not kept:
        start = statements[0][0]
        original = file_lines[start - 1]
        lines[start - first_line] = original[:len(original) - len(original.lstrip())] + 'pass'
    return lines


def find_code(module_code, name, first_line):
    """The nested code object called `name` that starts at `first_line`, or None."""
    pending = [module_code]
    while pending:
        code = pending.pop()
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                if const.co_name == name and const.co_firstlineno == first_line:
                    return const
                pending.append(const)
    return None


def _entry_key(item):
    if isinstance(item, tuple):
        return tuple((tuple(sorted(part.items())) if isinstance(part, dict) else part for part in item))
    return item


def changed_fixture_entries(name, read_old):
    """{('p'|'x', fixture name): indices of entries that are new or changed by the patch}.

    `read_old` is the context manager of the open overlay that shows the
    unpatched files.
    """
    helpers = sys.modules.get('tests.helpers')
    changed = {}
    if helpers is None or getattr(helpers, '_blast_state', None) is None:
        return changed
    for kind, loader in (('p', helpers._blast_original_pairs), ('x', helpers._blast_original_fixtures)):
        try:
            new_entries = [_entry_key(entry) for entry in loader(name)]
            with read_old():
                old_entries = {_entry_key(entry) for entry in loader(name)}
            changed[kind, name] = {index for index, entry in enumerate(new_entries) if entry not in old_entries}
        except Exception:
            continue
    return changed


# Tests too slow to run as written: (old text, new text, expected occurrences) edits.
SCALED = {'tests/test_parser.py::TestParser::test_max_nodes': [('width = 10000', 'width = 20', 1), ('            parse_one(sql, max_nodes=1_000_000)', '            parse_one(sql, max_nodes=2_000)', 1), ('parse_one(sql, max_nodes=50_000)', 'parse_one(sql, max_nodes=1_000)', 1), ('"(50000)"', '"(1000)"', 1)], 'tests/test_generator.py::TestGenerator::test_generator_memory': [('for _ in range(100):', 'for _ in range(2):', 1), ('for _ in range(1000):', 'for _ in range(60):', 1)], 'tests/test_optimizer.py::TestOptimizer::test_scope': [('["SELECT x FROM t"] * 10000))', '["SELECT x FROM t"] * 100))', 1), ('" UNION SELECT 1 INTERSECT SELECT 1" * 1000, read="hive")', '" UNION SELECT 1 INTERSECT SELECT 1" * 20, read="hive")', 1), ('traverse())), 4001)', 'traverse())), 81)', 1)]}


def scale_lines(test_id, lines, reduced=False):
    """Apply the SCALED edits of `test_id` to `lines`; None if they do not apply cleanly."""
    scaled = list(lines)
    for old_text, new_text, expected in SCALED[test_id]:
        hits = [index for index, line in enumerate(scaled) if old_text in line]
        if len(hits) > expected or (len(hits) < expected and (not reduced)) or any((scaled[index].count(old_text) != 1 for index in hits)):
            return None
        for index in hits:
            scaled[index] = scaled[index].replace(old_text, new_text)
    return scaled


# When a list, apply_reduction records (function, original code) here so the
# caller can restore the functions afterwards.
SWAPPED = None


def live_function(cls, method, old_code):
    """The function object of `cls.method` (unwrapping decorators) whose code is `old_code`."""
    candidate = cls.__dict__.get(method)
    depth = 0
    while candidate is not None and depth < 8:
        if isinstance(candidate, types.FunctionType) and candidate.__code__ == old_code:
            return candidate
        candidate = getattr(candidate, '__wrapped__', None)
        depth += 1
    return None


def apply_reduction(cls, test_id, full_path, shape, file_lines, flags, import_names, keep_stmts, scaled, pinned=None):
    """Recompile one test method reduced to `keep_stmts` (and/or scaled); True on success."""
    import patching
    _, class_name, method = test_id.split('::')
    first_line = shape[0]
    old_lines = file_lines[first_line - 1:shape[1]]
    new_lines = old_lines if keep_stmts is None else reduced_lines(file_lines, shape, keep_stmts)
    if scaled:
        new_lines = scale_lines(test_id, new_lines, keep_stmts is not None)
        if new_lines is None:
            return False
    if pinned is not None:
        old_code = pinned
    else:
        old_module_code = patching.compile_lines(old_lines, first_line, (class_name,), full_path, flags, import_names)
        if old_module_code is None:
            return False
        old_code = find_code(old_module_code, method, first_line)
    new_module_code = patching.compile_lines(new_lines, first_line, (class_name,), full_path, flags, import_names)
    if new_module_code is None:
        return False
    new_code = find_code(new_module_code, method, first_line)
    if old_code is None or new_code is None:
        return False
    func = live_function(cls, method, old_code)
    if func is None or len(old_code.co_freevars) != len(new_code.co_freevars):
        return False
    if SWAPPED is not None:
        SWAPPED.append((func, func.__code__))
    func.__code__ = new_code
    return True
