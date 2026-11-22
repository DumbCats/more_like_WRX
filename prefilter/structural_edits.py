"""Recognizers for edits that leave a module's behaviour (nearly) intact.

These cover docstrings, annotations, inert statements, new methods, lambdas, removed parser
members and identity-equivalent comparisons. Recognizers return a mask or None.
"""
import ast
from copy import copy
from functools import cache

from ast_tools import baseline_dump, child_mask, counterpart, dotted_path, future_annotations, spanning_nodes

FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
# Comparison operators grouped by polarity: is/== and is not/!= agree for identity-like operands.
COMPARISON_POLARITY = {ast.Is: 1, ast.Eq: 1, ast.IsNot: 2, ast.NotEq: 2}
# Names through which code can observe its own frame or locals.
FRAME_ACCESS = {'locals', 'vars', 'dir', 'eval', 'exec', 'inspect', 'sys', 'super', 'f_locals', 'gi_frame', 'cr_frame', 'tb_frame', 'currentframe', '_getframe'}
INERT_CONSTANT_TYPES = (type(None), bool, int, str)


def identity_equivalent(left, right):
    """Whether `is` and `==` agree for operands of these observed kinds."""
    if left == right == 'identity':
        return True
    if left == right and left.startswith('enum:'):
        return True
    if 'none' in (left, right):
        return all(kind in {'none', 'identity', 'builtin'} or kind.startswith('enum:') for kind in (left, right))
    return False


def normalize_comparisons(before, after, path, operands):
    """Undo is<->== swaps in after where the profiled operand kinds make them equivalent."""
    for old, new in zip(ast.walk(before), ast.walk(after)):
        if not isinstance(old, ast.Compare) or not isinstance(new, ast.Compare):
            continue
        if len(old.ops) != 1 or len(new.ops) != 1:
            continue
        old_op, new_op = type(old.ops[0]), type(new.ops[0])
        if old_op is new_op or old_op not in COMPARISON_POLARITY or COMPARISON_POLARITY[old_op] != COMPARISON_POLARITY.get(new_op):
            continue
        site = f'{path}:{old.lineno}:{old.col_offset}:'
        left_kinds, right_kinds = operands.get(site + '0'), operands.get(site + '1')
        if left_kinds and right_kinds and all(identity_equivalent(left, right) for left in left_kinds for right in right_kinds):
            new.ops[0] = old.ops[0]


def _plain_new_method(member, existing_names):
    """A new, undecorated, unannotated instance method without defaults."""
    return (
        isinstance(member, ast.FunctionDef)
        and member.name not in existing_names
        and not member.name.startswith('__')
        and not member.decorator_list
        and not member.type_params
        and not member.returns
        and not member.args.defaults
        and not any(member.args.kw_defaults)
        and member.args.args
        and member.args.args[0].arg == 'self'
        and all(not argument.annotation for argument in ast.walk(member.args) if isinstance(argument, ast.arg))
    )


def added_method_receivers(path, before, after, maps):
    """Only plain methods were added to existing profiled classes: tests using those types."""
    old_classes = {node.name: node for node in before.body if isinstance(node, ast.ClassDef)}
    receivers = 0
    stripped = copy(after)
    stripped.body = list(after.body)
    for position, cls in enumerate(after.body):
        if not isinstance(cls, ast.ClassDef) or cls.name not in old_classes:
            continue
        type_tests = maps['types'].get(dotted_path(path) + '.' + cls.name)
        if type_tests is None:
            continue
        existing_names = {getattr(member, 'name', None) for member in old_classes[cls.name].body}
        kept = []
        for member in cls.body:
            if _plain_new_method(member, existing_names):
                receivers |= int(type_tests, 16)
            else:
                kept.append(member)
        stripped.body[position] = copy(cls)
        stripped.body[position].body = kept
    if receivers and baseline_dump(before) == ast.dump(stripped):
        return receivers
    return None


def scalar_setting(path, before, after, removed, maps):
    """A one-line scalar class attribute of a dialect changed type-preservingly (profiled field)."""
    if not path.startswith('sqlglot/dialects/') or len(removed) != 1:
        return None
    line = next(iter(removed))
    for cls in before.body:
        if not isinstance(cls, ast.ClassDef) or not cls.lineno < line <= cls.end_lineno:
            continue
        for old in cls.body:
            if (
                not isinstance(old, ast.Assign)
                or old.lineno != line
                or old.end_lineno != line
                or len(old.targets) != 1
                or not isinstance(old.targets[0], ast.Name)
            ):
                continue
            field_tests = maps['fields'].get('.'.join((dotted_path(path), cls.name, old.targets[0].id)))
            if field_tests is None:
                continue
            for new_cls in after.body:
                if not isinstance(new_cls, ast.ClassDef) or new_cls.name != cls.name:
                    continue
                for new in new_cls.body:
                    if not isinstance(new, ast.Assign) or new.lineno != line:
                        continue
                    try:
                        old_value, new_value = ast.literal_eval(old.value), ast.literal_eval(new.value)
                    except (ValueError, TypeError):
                        continue
                    if type(old_value) is not type(new_value) or type(old_value) not in (int, bool, str, float):
                        continue
                    new_expression = new.value
                    new.value = old.value
                    try:
                        if baseline_dump(before) == ast.dump(after):
                            return int(field_tests, 16) | child_mask(path, maps)
                    finally:
                        new.value = new_expression
    return None


@cache
def single_definition(node):
    """A module holding just node (memoized so its baseline dump is cached too)."""
    return ast.Module(body=[node], type_ignores=[])


def _top_level_functions(tree):
    """Module-level functions and the methods of module-level classes."""
    for statement in tree.body:
        if isinstance(statement, FUNCTION_TYPES):
            yield statement
        elif isinstance(statement, ast.ClassDef):
            for member in statement.body:
                if isinstance(member, FUNCTION_TYPES):
                    yield member


def edited_function(before, after, base, revised, removed):
    """Narrow (before, after) to the one function containing every removed line, if possible.

    Returns the original trees when the edit is not confined to a single function whose
    surrounding text is unchanged.
    """
    if not removed:
        return before, after
    first, last = min(removed), max(removed)
    old = next((func for func in _top_level_functions(before) if func.lineno < first <= last <= func.end_lineno), None)
    if old is None:
        return before, after
    old_lines, new_lines = base.splitlines(True), revised.splitlines(True)
    shift = len(new_lines) - len(old_lines)
    new = next(
        (
            func
            for func in _top_level_functions(after)
            if type(func) is type(old) and func.name == old.name and func.lineno == old.lineno and func.end_lineno == old.end_lineno + shift
        ),
        None,
    )
    if new is None or old_lines[:old.lineno - 1] != new_lines[:new.lineno - 1] or old_lines[old.end_lineno:] != new_lines[new.end_lineno:]:
        return before, after
    return single_definition(old), ast.Module(body=[new], type_ignores=[])


def lambda_edit(path, before, after, maps):
    """Only module/class-level lambdas changed: tests reaching their lines."""
    old_lambdas = {(node.lineno, node.col_offset): node for node in ast.walk(before) if isinstance(node, ast.Lambda)}
    changed = []
    for new in ast.walk(after):
        if isinstance(new, ast.Lambda):
            old = old_lambdas.get((new.lineno, new.col_offset))
            if old is not None and ast.dump(old) != ast.dump(new):
                changed.append((old, new))
    if not changed:
        return None
    saved = [(new, new.args, new.body) for _old, new in changed]
    try:
        for old, new in changed:
            new.args, new.body = old.args, old.body
        if ast.dump(before) != ast.dump(after):
            return None
    finally:
        for new, arguments, body in saved:
            new.args, new.body = arguments, body
    functions = [node for node in ast.walk(before) if isinstance(node, FUNCTION_TYPES)]
    line_masks = maps['lines'].get(path, {})
    import_masks = maps['imports'].get(path, {})
    mask = child_mask(path, maps)
    for old, _new in changed:
        if any(func.lineno <= old.lineno <= func.end_lineno for func in functions):
            return None
        reached = 0
        for number in range(old.lineno, old.end_lineno + 1):
            reached |= int(line_masks.get(str(number), '0'), 16)
        if not reached:
            # The profiles miss many lambda bodies (e.g. the executor's ENV table) even though
            # tests call them, so an unreached lambda falls back to every test that runs any
            # code of this file.
            for line_tests in line_masks.values():
                reached |= int(line_tests, 16)
        mask |= reached
        for number in range(old.lineno, old.end_lineno + 1):
            mask |= int(import_masks.get(str(number), '0'), 16)
    return mask


def equal_without_annotations(before, after):
    """With postponed annotations, whether the trees match once function annotations are blanked."""
    if not future_annotations(before) or not future_annotations(after):
        return False
    saved = []
    for tree in (before, after):
        for func in ast.walk(tree):
            if not isinstance(func, FUNCTION_TYPES):
                continue
            saved.append((func, 'returns', func.returns))
            func.returns = None
            for argument in ast.walk(func.args):
                if isinstance(argument, ast.arg):
                    saved.append((argument, 'annotation', argument.annotation))
                    argument.annotation = None
            pending = list(func.body)
            while pending:
                node = pending.pop()
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if isinstance(node, ast.AnnAssign):
                    saved.append((node, 'annotation', node.annotation))
                    node.annotation = ast.Constant(None)
                pending.extend(ast.iter_child_nodes(node))
    try:
        return ast.dump(before) == ast.dump(after)
    finally:
        for node, field, value in reversed(saved):
            setattr(node, field, value)


def annotation_on_line(tree, line):
    """Whether some annotation in tree covers line."""
    for node in spanning_nodes(tree, line, line):
        if isinstance(node, (ast.arg, ast.AnnAssign)):
            annotation = node.annotation
        elif isinstance(node, FUNCTION_TYPES):
            annotation = node.returns
        else:
            continue
        if annotation is not None and annotation.lineno <= line <= annotation.end_lineno:
            return True
    return False


def frame_readers_for(path, maps):
    """Tests in files that introspect frames/code objects and may reach path."""
    readers = 0
    for file, file_tests in maps.get('frame_reader_files', {}).items():
        scope = maps.get('frame_scopes', {}).get(file)
        if scope is None or path in scope:
            readers |= file_tests
    return readers if maps.get('frame_reader_files') else maps['frame_readers']


def _inert_candidate(statement, base):
    """Whether statement is `name = constant` (fresh name) or `if False: pass/return`.

    Returns None when the edit must not be treated as inert at all.
    """
    if isinstance(statement, ast.Assign):
        if not (
            len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Constant)
            and type(statement.value.value) in INERT_CONSTANT_TYPES
        ):
            return False
        name = statement.targets[0].id
        if name in base or name.startswith('__'):
            return None
        return True
    if isinstance(statement, ast.If):
        return (
            isinstance(statement.test, ast.Constant)
            and statement.test.value is False
            and not statement.orelse
            and all(
                isinstance(inner, ast.Pass) or (isinstance(inner, ast.Return) and (inner.value is None or isinstance(inner.value, ast.Constant)))
                for inner in statement.body
            )
        )
    return False


def inert_statement(path, base, before, after, inserted, maps):
    """A single inserted statement in a sqlglot function that cannot change its results."""
    if len(inserted) != 1 or not path.startswith('sqlglot/'):
        return None
    line = next(iter(inserted))
    owners = [node for node in spanning_nodes(after, line, line) if isinstance(node, ast.FunctionDef) and node.lineno < line]
    if len(owners) != 1:
        return None
    func = owners[0]
    for position, statement in enumerate(func.body):
        if statement.lineno != line or func.decorator_list:
            continue
        candidate = _inert_candidate(statement, base)
        if candidate is None:
            return None
        if not candidate:
            continue
        if (
            position == 0
            and len(func.body) > 1
            and isinstance(func.body[1], ast.Expr)
            and isinstance(func.body[1].value, ast.Constant)
            and isinstance(func.body[1].value.value, str)
        ):
            return None
        if any(
            isinstance(node, ast.Name) and node.id in FRAME_ACCESS or (isinstance(node, ast.Attribute) and node.attr in FRAME_ACCESS)
            for node in ast.walk(func)
        ):
            return None
        func.body.pop(position)
        try:
            if baseline_dump(before) == ast.dump(after):
                reached = 0
                for table in (maps['lines'], maps['imports']):
                    file_masks = table.get(path, {})
                    for number in range(func.lineno, func.end_lineno + 1):
                        reached |= int(file_masks.get(str(number), '0'), 16)
                return frame_readers_for(path, maps) | (maps['memory_readers'] & reached) | child_mask(path, maps)
        finally:
            func.body.insert(position, statement)
    return None


def _doctest_examples(docstring):
    import doctest

    return [(example.source, example.want, example.exc_msg, example.options) for example in doctest.DocTestParser().get_examples(docstring)]


def documentation_edit(path, before, after, removed, inserted, maps):
    """Only a docstring's prose changed (its doctest examples are identical)."""
    if not path.startswith('sqlglot/') or not (removed or inserted):
        return None
    first = min(removed | inserted)
    owners = [before] + [
        node
        for node in ast.walk(before)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.decorator_list and node.lineno <= first <= node.end_lineno
    ]
    for owner in owners:
        if not owner.body or not isinstance(owner.body[0], ast.Expr):
            continue
        old = owner.body[0].value
        if not (isinstance(old, ast.Constant) and type(old.value) is str and old.lineno <= first <= old.end_lineno):
            continue
        twin = after if owner is before else counterpart(after, owner)
        if twin is None or not twin.body or not isinstance(twin.body[0], ast.Expr):
            continue
        new = twin.body[0].value
        if not isinstance(new, ast.Constant) or type(new.value) is not str or new.value == old.value:
            continue
        try:
            if _doctest_examples(new.value) != _doctest_examples(old.value):
                continue
        except ValueError:
            continue
        twin.body[0].value = old
        try:
            if baseline_dump(before) == ast.dump(after):
                mask = maps['documentation_readers'] | child_mask(path, maps)
                if owner is not before:
                    mask |= frame_readers_for(path, maps)
                return mask
        finally:
            twin.body[0].value = new
    return None


def _removable_method(member, removed):
    return (
        isinstance(member, ast.FunctionDef)
        and not member.name.startswith('__')
        and not member.decorator_list
        and not member.args.defaults
        and not any(member.args.kw_defaults)
        and set(range(member.lineno, member.end_lineno + 1)) <= removed
    )


def _removable_entry(key, value, removed):
    return (
        isinstance(key, ast.Constant)
        and type(key.value) is str
        and isinstance(value, ast.Lambda)
        and not value.args.defaults
        and not any(value.args.kw_defaults)
        and set(range(key.lineno, value.end_lineno + 1)) <= removed
    )


def removed_parser_members(path, before, after, removed, maps):
    """Whole methods / lambda table entries were deleted from a lazily imported dialect parser."""
    if not path.startswith('sqlglot/parsers/') or path not in maps['cold_imports'] or not future_annotations(before):
        return None
    pruned = copy(before)
    pruned.body = list(before.body)
    mask = maps['cold_imports'][path]
    dropped_any = False
    for position, cls in enumerate(before.body):
        if not (
            isinstance(cls, ast.ClassDef)
            and not cls.decorator_list
            and not cls.keywords
            and len(cls.bases) == 1
            and ast.unparse(cls.bases[0]) == 'parser.Parser'
            and all(cls.lineno < number <= cls.end_lineno for number in removed)
        ):
            continue
        qualified = dotted_path(path) + '.' + cls.name
        if qualified not in maps['types'] or maps.get('import_types') is None:
            return None
        mask |= int(maps['types'][qualified], 16) | int(maps['import_types'].get(qualified, '0'), 16)
        kept_members = []
        for member in cls.body:
            if _removable_method(member, removed):
                dropped_any = True
                continue
            if isinstance(member, ast.Assign) and isinstance(member.value, ast.Dict) and len(member.targets) == 1 and isinstance(member.targets[0], ast.Name):
                table = member.value
                kept = [index for index, (key, value) in enumerate(zip(table.keys, table.values)) if not _removable_entry(key, value, removed)]
                if len(kept) != len(table.keys):
                    dropped_any = True
                    member = copy(member)
                    member.value = copy(table)
                    member.value.keys = [table.keys[index] for index in kept]
                    member.value.values = [table.values[index] for index in kept]
            kept_members.append(member)
        pruned_class = copy(cls)
        pruned_class.body = kept_members
        pruned.body[position] = pruned_class
    if dropped_any and ast.dump(pruned) == ast.dump(after):
        return mask
    return None
