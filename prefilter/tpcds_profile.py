"""Which TPC-DS executor queries a patch can affect, according to the TPC-DS profile.

The profile (data/tpcds.gz) records, per sqlglot line, which TPC-DS queries executed it,
which lines run during the test's setup, and the CPU seconds of each query.
"""
import ast
import gzip
import json

BODY_FIELDS = ('body', 'orelse', 'finalbody', 'handlers', 'cases')


def load(path):
    """The TPC-DS profile with sets/ints decoded, or None when missing or unreadable."""
    try:
        with gzip.open(path, 'rt') as handle:
            profile = json.load(handle)
    except (OSError, ValueError):
        return None
    profile['setup'] = {file: set(lines) for file, lines in profile['setup'].items()}
    profile['lines'] = {file: {int(line): int(queries, 16) for line, queries in lines.items()} for file, lines in profile['lines'].items()}
    return profile


def outer_definitions(body):
    """Outermost functions in body, looking through classes and compound statements."""
    definitions = []
    for statement in body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.append(statement)
        elif isinstance(statement, ast.ClassDef):
            definitions.extend(outer_definitions(statement.body))
        else:
            for field in BODY_FIELDS:
                block = getattr(statement, field, None)
                if isinstance(block, list):
                    definitions.extend(outer_definitions(block))
    return definitions


def edited_bodies(base, revised):
    """The base functions whose bodies changed, when nothing outside function bodies changed.

    Returns [] for no change and None when the edit is not confined to function bodies.
    """
    try:
        before, after = ast.parse(base), ast.parse(revised)
    except SyntaxError:
        return None
    if ast.dump(before) == ast.dump(after):
        return []
    old_definitions, new_definitions = outer_definitions(before.body), outer_definitions(after.body)
    if len(old_definitions) != len(new_definitions):
        return None
    edited, swapped = [], []
    for old, new in zip(old_definitions, new_definitions):
        if type(old) is not type(new) or old.name != new.name:
            return None
        if ast.dump(old) != ast.dump(new):
            edited.append(old)
            swapped.append((new, new.body))
            new.body = old.body
    try:
        confined = ast.dump(before) == ast.dump(after)
    finally:
        for new, body in swapped:
            new.body = body
    return edited if confined else None


def statement_lines(func, number):
    """Lines of the innermost statement of func covering number (its header for compound ones)."""
    innermost = None
    for node in ast.walk(func):
        if node is not func and isinstance(node, ast.stmt) and node.lineno <= number <= node.end_lineno:
            if innermost is None or node.end_lineno - node.lineno <= innermost.end_lineno - innermost.lineno:
                innermost = node
    if innermost is None or isinstance(innermost, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return None
    body = getattr(innermost, 'body', None)
    if isinstance(body, list) and body and number < body[0].lineno:
        return range(innermost.lineno, body[0].lineno)
    return range(innermost.lineno, innermost.end_lineno + 1)


def reach(func, first, removed, inserted, granular=True):
    """Lines of func whose execution would expose the edit (the whole rest of func if unsure)."""
    rest = range(first, func.end_lineno + 1)
    if not granular or any(first <= number <= func.end_lineno + 1 for number in inserted):
        return rest
    lines = set()
    for number in removed:
        if first <= number <= func.end_lineno:
            statement = statement_lines(func, number)
            if statement is None:
                return rest
            lines.update(statement)
    return sorted(lines) if lines else rest


def affected_queries(profile, files, other_masks, bit, granular=True):
    """(bitmask of TPC-DS queries reaching the edits, files they reach), or None if unknown."""
    queries = 0
    reached = set()
    for path, base, revised, removed, inserted in files:
        if not path.endswith('.py'):
            if other_masks.get(path, bit) & bit:
                return None
            continue
        if not path.startswith('sqlglot/'):
            return None
        edited = edited_bodies(base, revised)
        if edited is None:
            return None
        setup_lines = profile['setup'].get(path, set())
        line_queries = profile['lines'].get(path, {})
        for func in edited:
            body_start = func.body[0].lineno
            if any(number in setup_lines for number in range(body_start, func.end_lineno + 1)):
                return None
            first = min([func.lineno] + [decorator.lineno for decorator in func.decorator_list])
            for number in reach(func, first, removed, inserted, granular):
                if line_queries.get(number, 0):
                    queries |= line_queries[number]
                    reached.add(path)
    return queries, reached


