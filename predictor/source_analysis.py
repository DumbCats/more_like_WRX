"""Text, AST and code-object checks the planner applies to changed source files."""
import json
import marshal
import os
import re
import zlib

import settings

CODING_RE = re.compile('coding[:=]\\s*[-\\w.]+')

# CO_GENERATOR | CO_COROUTINE | CO_ITERABLE_COROUTINE | CO_ASYNC_GENERATOR
_KIND_FLAGS = 32 | 128 | 256 | 512


def python_text(text):
    """The text Python would decode from `text`'s bytes (its coding cookie applied).

    Returns `text` itself for plain ASCII without a cookie, None if it cannot be decoded.
    """
    first_newline = text.find('\n')
    second_newline = text.find('\n', first_newline + 1) if first_newline >= 0 else -1
    if text.isascii() and 'coding' not in (text if second_newline < 0 else text[:second_newline]):
        return text
    import io
    import tokenize
    data = text.encode('utf-8', 'surrogateescape')
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return data.decode(encoding)
    except (SyntaxError, LookupError, UnicodeDecodeError, ValueError):
        return None


def line_groups(old_text, new_text):
    """Diff two texts line by line into FilePatch-style groups."""
    import difflib
    old_lines, new_lines = (old_text.split('\n'), new_text.split('\n'))
    groups = []
    for tag, old_start, old_end, new_start, new_end in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag != 'equal':
            groups.append([old_start + 1, old_end - old_start, new_start + 1, new_end - new_start, old_lines[old_start:old_end], new_lines[new_start:new_end]])
    return groups


def blank_or_comment(lines):
    """True if every line is empty or a comment."""
    return all((not line.strip() or line.lstrip().startswith('#') for line in lines))


def indent_width(line):
    return len(line) - len(line.lstrip())


def code_kind_changed(old_code, new_code):
    """True if a function became (or stopped being) a generator/coroutine, here or in nested code."""
    if (old_code.co_flags ^ new_code.co_flags) & _KIND_FLAGS:
        return True
    old_nested = [const for const in old_code.co_consts if hasattr(const, 'co_code')]
    new_nested = [const for const in new_code.co_consts if hasattr(const, 'co_code')]
    if len(old_nested) != len(new_nested):
        return True
    return any((code_kind_changed(old, new) for old, new in zip(old_nested, new_nested)))


def unit_line_shift(groups, first_line, last_line):
    """(lines added before the unit, lines added inside it) for a unit at first_line..last_line."""
    shift_before = 0
    shift_inside = 0
    for group in groups:
        if group[0] + group[1] - 1 < first_line and group[0] < first_line:
            shift_before += group[3] - group[1]
        elif group[0] <= last_line:
            shift_inside += group[3] - group[1]
    return (shift_before, shift_inside)


# --- `if TYPE_CHECKING:` blocks -------------------------------------------------

def type_checking_spans(text):
    """Line spans of the bodies of `if TYPE_CHECKING:` blocks at module/class level.

    Returns None if the text does not compile and [] if TYPE_CHECKING could be
    rebound (so the blocks cannot be trusted to be dead at run time).
    """
    import ast
    try:
        tree = ast.parse(text)
        compile(tree, '<check>', 'exec', dont_inherit=True)
    except (SyntaxError, ValueError):
        return None
    typing_names, flag_names = (set(), set())
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            typing_names.update((alias.asname or alias.name for alias in statement.names if alias.name == 'typing'))
        elif isinstance(statement, ast.ImportFrom) and statement.module == 'typing' and (not statement.level):
            flag_names.update((alias.asname or alias.name for alias in statement.names if alias.name == 'TYPE_CHECKING'))
    if not typing_names and (not flag_names):
        return []
    names = typing_names | flag_names
    for node in ast.walk(tree):
        if isinstance(node, ast.Global) and names & set(node.names):
            return []
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)) and (node.attr == 'TYPE_CHECKING'):
            return []
    pending = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and (node.id in names):
            return []
        if isinstance(node, ast.Import) and any((alias.name != 'typing' and (alias.asname or alias.name.split('.')[0]) in names for alias in node.names)):
            return []
        if isinstance(node, ast.ImportFrom) and any(((alias.asname or alias.name) in names and (not (node.module == 'typing' and alias.name == 'TYPE_CHECKING')) for alias in node.names)):
            return []
        pending.extend(ast.iter_child_nodes(node))

    def is_flag(expression):
        if isinstance(expression, ast.Name):
            return expression.id in flag_names
        return isinstance(expression, ast.Attribute) and expression.attr == 'TYPE_CHECKING' and isinstance(expression.value, ast.Name) and (expression.value.id in typing_names)
    spans = []

    def collect(body):
        for statement in body:
            if isinstance(statement, ast.If) and is_flag(statement.test):
                spans.append((statement.body[0].lineno, statement.body[-1].end_lineno))
                collect(statement.orelse)
            elif isinstance(statement, ast.ClassDef):
                collect(statement.body)
    collect(tree.body)
    return spans


_SAVED_TYPE_CHECKING_SPANS = None


def _old_type_checking_spans(rel, old_text):
    """type_checking_spans of an unpatched file, from tc_spans.bin when it matches."""
    global _SAVED_TYPE_CHECKING_SPANS
    if _SAVED_TYPE_CHECKING_SPANS is None:
        _SAVED_TYPE_CHECKING_SPANS = {}
        for directory in (settings.TABLES, settings.PREDICTOR_DIR):
            try:
                with open(os.path.join(directory, 'tc_spans.bin'), 'rb') as handle:
                    _SAVED_TYPE_CHECKING_SPANS = marshal.load(handle)
                break
            except (OSError, ValueError, EOFError, TypeError):
                pass
    saved = _SAVED_TYPE_CHECKING_SPANS.get(rel)
    if saved is not None and saved[0] == len(old_text) and (saved[1] == zlib.crc32(old_text.encode('utf-8', 'surrogateescape'))):
        return saved[2]
    return type_checking_spans(old_text)


def type_checking_groups(file_patch, old_text):
    """Indices of the change groups that lie entirely inside TYPE_CHECKING blocks, old and new."""
    if 'TYPE_CHECKING' not in old_text or 'TYPE_CHECKING' not in file_patch.new_text:
        return set()
    old_spans = _old_type_checking_spans(file_patch.old_path, old_text)
    if not old_spans:
        return set()

    def inside(first, count, spans):
        return all((any((start <= line <= end for start, end in spans)) for line in range(first, first + count)))
    maybe = [indexed for indexed in enumerate(file_patch.groups) if indexed[1][1] + indexed[1][3] and inside(indexed[1][0], indexed[1][1], old_spans) and (not (blank_or_comment(indexed[1][4]) and blank_or_comment(indexed[1][5])))]
    if not maybe:
        return set()
    new_spans = type_checking_spans(file_patch.new_text)
    if not new_spans:
        return set()
    return {index for index, (_, _, new_line, added_count, _, _) in maybe if inside(new_line, added_count, new_spans)}


# --- writes to shared containers ------------------------------------------------

_MUTATORS = frozenset(('pop', 'popitem', 'clear', 'update', 'setdefault', 'append', 'extend', 'insert', 'remove', 'add', 'discard', '__setitem__', '__delitem__'))
_ADD_METHODS = frozenset(('append', 'extend', 'insert', 'add', 'update', 'setdefault', '__setitem__', '__ior__'))
_REMOVE_METHODS = frozenset(('pop', 'popitem', 'clear', 'remove', 'discard', '__delitem__'))


def writes_shared_state(function):
    """True if the function AST writes to anything that is not its own local."""
    import ast
    local_names = set()
    for argument in function.args.posonlyargs + function.args.args + function.args.kwonlyargs:
        local_names.add(argument.arg)
    for node in ast.walk(function):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.NamedExpr)):
            targets = [node.target]
        elif isinstance(node, ast.With):
            targets = [item.optional_vars for item in node.items]
        for target in targets:
            if isinstance(target, ast.Name):
                local_names.add(target.id)

    def root_name(expression):
        depth = 0
        while isinstance(expression, (ast.Attribute, ast.Subscript)):
            expression = expression.value
            depth += 1
        return (expression.id if isinstance(expression, ast.Name) else None, depth)
    for node in ast.walk(function):
        targets = []
        if isinstance(node, (ast.Assign, ast.Delete)):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, (ast.Attribute, ast.Subscript)):
                name, _ = root_name(target)
                if name is not None and name not in local_names:
                    return True
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Name) and callee.id in ('setattr', 'delattr'):
                return True
            if isinstance(callee, ast.Attribute) and callee.attr in _MUTATORS:
                name, depth = root_name(callee.value)
                if name is not None and name not in local_names and (depth >= 1):
                    return True
    return False


def container_writes(function):
    """{container: {'add' | 'remove' | 'rebind'}} for the shared containers a function writes.

    A container is ('A', attribute) for an attribute of the first parameter
    (self/cls) or ('G', global, attr, ...) for a global or a dotted path from one.
    """
    import ast
    positional = function.args.posonlyargs + function.args.args
    first_parameter = positional[0].arg if positional else None
    declared = set()
    local_names = {argument.arg for argument in function.args.posonlyargs + function.args.args + function.args.kwonlyargs}
    for argument in (function.args.vararg, function.args.kwarg):
        if argument is not None:
            local_names.add(argument.arg)
    for node in ast.walk(function):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            local_names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not function:
            local_names.add(node.name)
        elif isinstance(node, ast.arg):
            local_names.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            local_names.update(((alias.asname or alias.name).split('.')[0] for alias in node.names))
    local_names -= declared

    def describe(expression):
        attributes = []
        while isinstance(expression, ast.Attribute):
            attributes.append(expression.attr)
            expression = expression.value
        if not isinstance(expression, ast.Name):
            return None
        attributes.reverse()
        if expression.id == first_parameter and first_parameter is not None and (first_parameter not in declared):
            return ('A', attributes[0]) if len(attributes) == 1 else None
        if expression.id in local_names:
            return None
        return ('G', expression.id) + tuple(attributes)
    writes = {}

    def note(container, kind):
        if container is not None:
            writes.setdefault(container, set()).add(kind)
    for node in ast.walk(function):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for target in targets:
                if isinstance(target, (ast.Tuple, ast.List)):
                    continue
                if isinstance(target, ast.Subscript):
                    note(describe(target.value), 'remove' if isinstance(node, ast.Delete) else 'add')
                elif isinstance(target, ast.Attribute):
                    container = describe(target)
                    if container is not None and container[0] == 'A' and isinstance(node, (ast.Assign, ast.AnnAssign)) and (first_parameter != 'cls'):
                        continue
                    note(container, 'add' if isinstance(node, ast.AugAssign) else 'rebind')
                elif isinstance(target, ast.Name) and target.id in declared:
                    note(('G', target.id), 'rebind')
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _ADD_METHODS:
                note(describe(node.func.value), 'add')
            elif node.func.attr in _REMOVE_METHODS:
                note(describe(node.func.value), 'remove')
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and (node.func.id in ('setattr', 'delattr')) and node.args:
            note(describe(node.args[0]), 'rebind')
    return writes


def _unit_function(unit_lines, class_path):
    """Parse a code unit (nested in its classes) and return its function node, or None."""
    import ast
    class_header = ''.join((' ' * depth + 'class ' + name + ':\n' for depth, name in enumerate(class_path)))
    try:
        body = ast.parse(class_header + '\n'.join(unit_lines) + '\n').body
    except (SyntaxError, ValueError):
        return None
    for _ in class_path:
        body = body[0].body if body and isinstance(body[0], ast.ClassDef) else []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def shared_write_watch(units, old_text, new_text, groups):
    """Shared containers that a changed function now fills or empties differently.

    For each changed function unit, compares the container writes of the old and
    new versions; a container is watched when the new code writes it in a way
    the old code did not (or the old one only filled and emptied it).
    """
    old_lines = old_text.split('\n')
    new_lines = new_text.split('\n')
    changed_units = set()
    for first_changed, removed_count, _, _, _, _ in groups:
        for unit_index, unit in enumerate(units):
            unit_first, unit_last, unit_kind = (unit[0], unit[1], unit[3])
            if unit_kind == 0 and (unit_first <= first_changed + removed_count - 1 and first_changed <= unit_last if removed_count > 0 else unit_first < first_changed <= unit_last):
                changed_units.add(unit_index)
    watched = set()
    for unit_index in sorted(changed_units):
        unit_first, unit_last, class_path, _ = units[unit_index]
        shift_before, shift_inside = unit_line_shift(groups, unit_first, unit_last)
        old_function = _unit_function(old_lines[unit_first - 1:unit_last], class_path)
        new_function = _unit_function(new_lines[unit_first - 1 + shift_before:unit_last + shift_before + shift_inside], class_path)
        old_writes = container_writes(old_function) if old_function is not None else {}
        for writes in (old_writes, container_writes(new_function) if new_function is not None else {}):
            for container in writes:
                old_kinds = old_writes.get(container, set())
                if not old_kinds or ({'add', 'remove'} <= old_kinds and 'rebind' not in old_kinds):
                    watched.add(container)
    return sorted(watched)


# --- the dialect registry ------------------------------------------------------------

REGISTRY_METHODS = ('classes', '_try_load', '__getitem__', 'get', 'get_or_raise')
_REGISTRY_SPANS = {}


def registry_spans(text):
    """Line spans (decorators included) of the registry methods of the classes in `text`."""
    import ast
    spans = _REGISTRY_SPANS.get(text)
    if spans is None:
        try:
            with open(os.path.join(settings.TABLES, 'registry_spans.json')) as handle:
                saved = json.load(handle)
            if saved['len'] == len(text) and saved['crc'] == zlib.crc32(text.encode('utf-8', 'surrogateescape')):
                spans = _REGISTRY_SPANS[text] = [tuple(span) for span in saved['spans']]
                return spans
        except (OSError, ValueError, KeyError, TypeError):
            pass
        tree = ast.parse(text)
        spans = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name in REGISTRY_METHODS:
                        start = min([item.lineno] + [decorator.lineno for decorator in item.decorator_list])
                        spans.append((start, item.end_lineno))
        _REGISTRY_SPANS[text] = spans
    return spans
