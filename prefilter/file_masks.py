"""The mask of tests affected by an edit to one file.

mask_for_file tries progressively more general explanations of the edit: inert or unknown
files, fixture files, no-op or documentation-only changes, the specific edit recognizers, and
finally per-line coverage of the changed lines.
"""
import ast
import re
from functools import cache

from ast_tools import baseline_dump, child_mask, counterpart, dotted_path, module_name
from bindings import binding_mask, default_callers, importer_mask, load_refs, signature_safe
from comparison_edits import boolop_edit, changed_predicate, conditional_edit, membership_edit, operation_edit
from diff_reader import is_inert
from fixtures import fixture_selection
from setting_edits import callback_edit, config_edit, copy_edit, default_edit, flag_edit, keyword_edit, lookup_edit, metadata_callback_edit, module_edit
from structural_edits import (
    added_method_receivers,
    annotation_on_line,
    documentation_edit,
    edited_function,
    equal_without_annotations,
    inert_statement,
    lambda_edit,
    normalize_comparisons,
    removed_parser_members,
    scalar_setting,
)

FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
COMPARISON_TOKENS = re.compile(r'\bis\b|!=|==')
# Packages whose class bodies are settings tables profiled per class.
CLASS_TABLE_PACKAGES = ('sqlglot/dialects/', 'sqlglot/parsers/', 'sqlglot/generators/', 'sqlglot/expressions/')
BRANCH_CLAUSES = ('else:', 'finally:', 'except ', 'except:', 'case ')
COMPOUND_STATEMENTS = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
# Recognizers tried, in order, on the (function-scoped) trees of an edit without insertions.
SCOPED_RECOGNIZERS = (membership_edit, copy_edit, lookup_edit, config_edit, module_edit, changed_predicate, callback_edit, conditional_edit, operation_edit)


@cache
def parse_base(source):
    """Parsed base source, shared by every patch that edits the same file."""
    return ast.parse(source)


def mask_for_file(path, base, revised, removed, inserted, maps, all_mask, neighbors, isolated=False):
    """Tests affected by changing path from base to revised.

    neighbors collects masks of code next to the edit (used to widen the schedule).
    isolated is true when this is the only file the patch touches.
    """
    file_readers = int(maps['files'].get(path, '0'), 16)
    if is_inert(path):
        return file_readers | int(maps['source_readers'].get(path, '0'), 16)
    module = module_name(path) if path.endswith('.py') else None
    if module and path.startswith('sqlglot/') and maps.get('modules') and module not in maps['modules'] and path not in maps.get('child_paths', {}):
        return file_readers | int(maps['source_readers'].get(path, '0'), 16) | maps.get('unknown_children', 0)
    if not path.endswith('.py'):
        neighbors.append(file_readers)
        return fixture_selection(path, base, revised, file_readers, maps) if file_readers else all_mask
    try:
        before, after = parse_base(base), ast.parse(revised)
        compile(after, path, 'exec', dont_inherit=True)
    except SyntaxError:
        importers = importer_mask(path, maps) if path.startswith('tests/') else None
        if importers is None:
            raise
        return file_readers | importers
    scoped_before, scoped_after = edited_function(before, after, base, revised, removed) if not inserted else (before, after)
    source_readers = int(maps['source_readers'].get(path, '0'), 16)
    if removed:
        base_lines = base.splitlines()
        if any(COMPARISON_TOKENS.search(base_lines[number - 1]) for number in removed):
            normalize_comparisons(scoped_before, scoped_after, path, maps.get('operands', {}))
    if baseline_dump(scoped_before) == ast.dump(scoped_after):
        return source_readers
    documentation = documentation_edit(path, before, after, removed, inserted, maps)
    if documentation is not None:
        return source_readers | documentation
    changed_lines = removed | inserted
    if any(
        isinstance(node, ast.FunctionDef) and node.name == '__getattr__' and any(node.lineno <= number <= node.end_lineno for number in changed_lines)
        for node in before.body
    ):
        return all_mask
    if inserted and not removed:
        inert = inert_statement(path, base, before, after, inserted, maps)
        if inert is not None:
            return source_readers | (inert & maps['cold_imports'].get(path, all_mask))
    annotation_edit = True
    if len(removed) == 1 and not inserted and base.count('\n') == revised.count('\n'):
        line = next(iter(removed))
        annotation_edit = annotation_on_line(scoped_before, line) or annotation_on_line(scoped_after, line)
    if annotation_edit and equal_without_annotations(before, after):
        return int(maps['source_readers'].get(path, '0'), 16)
    neighbors.append(0)
    if not inserted:
        recognized = _recognize_edit(path, before, after, scoped_before, scoped_after, removed, maps)
        if recognized is not None:
            return source_readers | recognized
    if inserted and not removed:
        receivers = added_method_receivers(path, before, after, maps)
        if receivers is not None:
            return source_readers | receivers
    selected = coverage_mask(path, base, before, after, removed, inserted, maps, all_mask, neighbors, source_readers, scoped_before)
    if isolated and selected == all_mask and removed and not inserted and scoped_before is before:
        parser_members = removed_parser_members(path, before, after, removed, maps)
        if parser_members is not None:
            return source_readers | parser_members
    if not inserted and before is scoped_before:
        bound = binding_mask(path, before, after, maps)
        if bound is not None:
            selected &= bound | source_readers
    if not inserted:
        boolop = boolop_edit(path, scoped_before, scoped_after, removed, inserted, maps)
        if boolop is not None:
            selected &= boolop | source_readers
    return selected


def _recognize_edit(path, before, after, scoped_before, scoped_after, removed, maps):
    """The mask from the first specific recognizer that explains an edit without insertions."""
    for recognizer, old_tree, new_tree in (
        (default_edit, scoped_before, scoped_after),
        (keyword_edit, before, after),
        (metadata_callback_edit, before, after),
    ):
        mask = recognizer(path, old_tree, new_tree, removed, maps)
        if mask is not None:
            return mask
    class_scope = before is scoped_before or any(isinstance(node, ast.ClassDef) for node in ast.walk(scoped_before))
    refs = load_refs(maps) if class_scope and maps.get('flips') else None
    mask = flag_edit(path, before, after, removed, maps, set(refs['strings'])) if refs else None
    for recognizer in SCOPED_RECOGNIZERS:
        if mask is not None:
            break
        mask = recognizer(path, scoped_before, scoped_after, removed, maps)
    if mask is None and before is scoped_before:
        mask = lambda_edit(path, before, after, maps)
    if mask is not None:
        return mask
    return scalar_setting(path, before, after, removed, maps)


def confined(func, before, after):
    """Whether before and after differ only inside the body of func."""
    new_func = counterpart(after, func)
    if new_func is None:
        return False
    new_body = new_func.body
    new_func.body = func.body
    try:
        return ast.dump(before) == ast.dump(after)
    finally:
        new_func.body = new_body


def insertion_reach(func, number, before, after):
    """Lines whose coverage shows that execution reached an insertion at number inside func.

    None when the insertion cannot be attributed to the surrounding statements.
    """
    if number <= func.body[0].lineno or not confined(func, before, after):
        return None
    statements = [node for node in ast.walk(func) if isinstance(node, ast.stmt) and node is not func]
    earlier_ends = [statement.end_lineno for statement in statements if statement.end_lineno < number]
    if not earlier_ends:
        return None
    previous_end = max(earlier_ends)
    lines = {statement.lineno for statement in statements if statement.end_lineno == previous_end}
    for statement in statements:
        if statement.lineno < number <= statement.end_lineno:
            lines.add(statement.lineno)
            if not isinstance(statement, COMPOUND_STATEMENTS):
                lines.update(range(statement.lineno, statement.end_lineno + 1))
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return None
    return lines


def _referenced_names(tree):
    return {(type(node), node.id if isinstance(node, ast.Name) else node.attr) for node in ast.walk(tree) if isinstance(node, (ast.Name, ast.Attribute))}


def _class_attribute_mask(path, before, after, number, removed, maps):
    """Tests using the class whose attribute assignment covers a removed line, else None."""
    classes = sorted((node for node in ast.walk(before) if isinstance(node, ast.ClassDef) and node.lineno < number <= node.end_lineno), key=lambda node: node.lineno)
    if not (classes and number in removed and path.startswith(CLASS_TABLE_PACKAGES)):
        return None
    assignments = (node for node in classes[-1].body if isinstance(node, (ast.Assign, ast.AnnAssign)))
    if not any(node.lineno <= number <= node.end_lineno for node in assignments):
        return None
    if _referenced_names(after) - _referenced_names(before):
        return None
    owner = '.'.join([dotted_path(path)] + [cls.name for cls in classes])
    type_tests = maps.get('types', {}).get(owner)
    if type_tests and maps.get('import_types') is not None:
        return int(type_tests, 16) | int(maps['import_types'].get(owner, '0'), 16)
    return None


def coverage_mask(path, base, before, after, removed, inserted, maps, all_mask, neighbors, readers, scoped_before):
    """Tests whose recorded coverage reaches the changed lines (all_mask when undecidable)."""
    line_masks = maps['lines'].get(path, {})
    import_masks = maps['imports'].get(path, {})
    selected = readers
    functions = [node for node in ast.walk(scoped_before) if isinstance(node, FUNCTION_TYPES)]
    base_lines = base.splitlines()
    for number in removed | inserted:
        enclosing = [func for func in functions if func.lineno < number <= func.end_lineno]
        if not enclosing and path.startswith('tests/'):
            importers = importer_mask(path, maps)
            if importers is None:
                return all_mask
            selected |= importers
            continue
        if not enclosing:
            class_tests = _class_attribute_mask(path, before, after, number, removed, maps)
            if class_tests is None:
                return all_mask
            selected |= class_tests
            continue
        func = min(enclosing, key=lambda candidate: candidate.end_lineno - candidate.lineno)
        nearby = 0
        for line in range(func.lineno, func.end_lineno + 1):
            nearby |= int(line_masks.get(str(line), '0'), 16)
        neighbors.append(nearby)
        reach = insertion_reach(func, number, before, after) if number in inserted else None
        if reach is not None:
            for line in reach:
                key = str(line)
                selected |= int(line_masks.get(key, '0'), 16) | int(import_masks.get(key, '0'), 16)
        elif number in inserted or number < func.body[0].lineno:
            if number not in inserted and not signature_safe(func, before, after):
                return all_mask
            callers = default_callers(func, before, after, path, maps) if number not in inserted else None
            if callers is not None:
                selected |= callers | child_mask(path, maps)
                continue
            selected |= nearby
            for line in range(func.lineno, func.end_lineno + 1):
                selected |= int(import_masks.get(str(line), '0'), 16)
        else:
            hits = {number}
            hits.update(node.lineno for node in ast.walk(func) if isinstance(node, ast.expr) and node.lineno <= number <= node.end_lineno)
            if base_lines[number - 1].lstrip().startswith(BRANCH_CLAUSES):
                selected |= nearby
            for line in hits:
                key = str(line)
                selected |= int(line_masks.get(key, '0'), 16) | int(import_masks.get(key, '0'), 16)
    return selected
