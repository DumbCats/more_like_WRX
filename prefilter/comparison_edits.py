"""Recognizers for edits to comparisons, boolean operators and single operations.

Each recognizer returns the mask of tests that can observe the edit, or None when the edit
is not of its kind (the caller then tries the next one).
"""
import ast
import json
import math
import operator

from ast_tools import baseline_dump, child_mask, spanning_nodes, unchanged_with_original
from bindings import load_refs

ORDERING_OPS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}
# Observed relation of the operands ('<', '=', '>') that makes each ordering operator true.
TRUE_RELATIONS = {ast.Lt: {'<'}, ast.LtE: {'<', '='}, ast.Gt: {'>'}, ast.GtE: {'>', '='}}
OPERATIONS = (ast.Compare, ast.BinOp, ast.UnaryOp, ast.Call)
# Pairs of operators that only negate each other.
TRUTH_SWAPS = ({ast.Eq, ast.NotEq}, {ast.Is, ast.IsNot}, {ast.In, ast.NotIn})


def scalar_key(value):
    """The profile key of an observed operand value ('?' when it was not recorded)."""
    if type(value) is int and value.bit_length() <= 64:
        return 'i' + str(value)
    if type(value) is str and len(value) <= 48:
        return 's' + value
    if type(value) is float and math.isfinite(value):
        return 'f' + value.hex()
    if type(value) is tuple and len(value) <= 6 and all(type(item) is int and item.bit_length() <= 64 for item in value):
        return 't' + json.dumps(value, separators=(',', ':'))
    return '?'


def literal_operand(node):
    """The literal value of a comparison operand, when it is one the profiles can key on."""
    if not isinstance(node, (ast.Constant, ast.UnaryOp, ast.Tuple)):
        return None
    try:
        value = ast.literal_eval(node)
    except (TypeError, ValueError):
        return None
    return value if scalar_key(value) != '?' else None


def operand_value(key):
    """Inverse of scalar_key."""
    if key.startswith('i'):
        return int(key[1:])
    if key.startswith('f'):
        return float.fromhex(key[1:])
    if key.startswith('t'):
        return tuple(json.loads(key[1:]))
    return key[1:]


def _ordering_outcome(compare, literal, observed, literal_side):
    """Result of compare with the literal on literal_side and the observed value on the other."""
    apply = ORDERING_OPS[type(compare.ops[0])]
    return apply(observed, literal) if literal_side else apply(literal, observed)


def changed_predicate(path, before, after, removed, maps):
    """A literal operand or the operator of a single comparison changed on one line."""
    if len(removed) != 1:
        return None
    line = next(iter(removed))
    old_compares = {
        (node.lineno, node.col_offset): node
        for node in spanning_nodes(before, line, line)
        if isinstance(node, ast.Compare) and node.lineno <= line <= node.end_lineno and len(node.ops) == 1 and type(node.ops[0]) in ORDERING_OPS
    }
    for new in spanning_nodes(after, line, line):
        if not isinstance(new, ast.Compare) or len(new.ops) != 1 or type(new.ops[0]) not in ORDERING_OPS:
            continue
        old = old_compares.get((new.lineno, new.col_offset))
        if old is None or ast.dump(old) == ast.dump(new):
            continue
        old_operands = (old.left, old.comparators[0])
        new_operands = (new.left, new.comparators[0])
        for side in (0, 1):
            other_old, other_new = old_operands[1 - side], new_operands[1 - side]
            old_literal, new_literal = literal_operand(old_operands[side]), literal_operand(new_operands[side])
            if old_literal is None or type(old_literal) is not type(new_literal) or ast.dump(other_old) != ast.dump(other_new):
                continue
            observations = maps['predicates'].get(f'{path}:{old.lineno}:{old.col_offset}:{side}')
            if not observations or not unchanged_with_original(after, baseline_dump(before), new, dict(left=old.left, ops=old.ops, comparators=old.comparators)):
                continue
            mask = child_mask(path, maps)
            import_time = 0
            for number in range(old.lineno, old.end_lineno + 1):
                import_time |= int(maps['imports'].get(path, {}).get(str(number), '0'), 16)
            if old_literal == new_literal:
                import_time = cold_order_mask(path, old, new, import_time, maps)
            mask |= import_time
            for key, key_tests in observations.items():
                keep = True
                if key != '?':
                    observed = operand_value(key)
                    try:
                        keep = _ordering_outcome(old, old_literal, observed, side) != _ordering_outcome(new, new_literal, observed, side)
                    except TypeError:
                        pass
                if keep:
                    mask |= int(key_tests, 16)
            return mask
    return None


def comparison_outcome(op, identity, relation):
    """Result of op given whether the operands were identical and their observed relation.

    None when the observation cannot decide it.
    """
    if op is ast.Is:
        return identity
    if op is ast.IsNot:
        return not identity
    if relation == '?':
        return None
    if op is ast.Eq:
        return relation in {'=', 'equal_only'}
    if op is ast.NotEq:
        return relation not in {'=', 'equal_only'}
    if relation in {'equal_only', 'unequal_only'}:
        return None
    return relation in TRUE_RELATIONS[op] if op in TRUE_RELATIONS else None


def changed_outcomes(profile, old_op, new_op):
    """Tests whose observed operands give a different (or undecidable) result under new_op."""
    flipped = 0
    for observation, observation_tests in profile.items():
        identity, relation = observation.split('/')
        old_outcome = comparison_outcome(old_op, identity == 'same', relation)
        new_outcome = comparison_outcome(new_op, identity == 'same', relation)
        if old_outcome is None or new_outcome is None or old_outcome != new_outcome:
            flipped |= int(observation_tests, 16)
    return flipped


def cold_order_mask(path, old, new, import_time, maps):
    """Drop import-time tests from import_time whose recorded comparison outcome is unchanged."""
    orders = maps['cold_orders'].get(f'{path}:{old.lineno}:{old.col_offset}')
    if not import_time or not orders:
        return import_time
    profiled = 0
    for observation_tests in orders.values():
        profiled |= int(observation_tests, 16)
    flipped = changed_outcomes(orders, type(old.ops[0]), type(new.ops[0]))
    return import_time & (~profiled | flipped)


def deferred_nodes(tree):
    """ids of the nodes that only run when a function or lambda is called."""
    found = set()

    def visit(node, inside):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for part in [*node.decorator_list, node.args, *([node.returns] if node.returns else [])]:
                visit(part, inside)
            for statement in node.body:
                visit(statement, True)
            return
        if isinstance(node, ast.Lambda):
            visit(node.args, inside)
            visit(node.body, True)
            return
        if inside:
            found.add(id(node))
        for child in ast.iter_child_nodes(node):
            visit(child, inside)

    visit(tree, False)
    return found


def conditional_edit(path, before, after, removed, maps):
    """`a if x is None else b` became `a if not (x is None) else b` (or back) on one line."""
    if len(removed) != 1:
        return None
    line = next(iter(removed))

    def choices(tree):
        return {
            node.col_offset: node
            for node in spanning_nodes(tree, line, line)
            if isinstance(node, ast.IfExp) and node.lineno == node.end_lineno == line and isinstance(node.body, ast.Name) and isinstance(node.orelse, ast.Name)
        }

    for column, old in choices(before).items():
        key = f'{path}:{line}:{column}'
        if key not in maps['choices']:
            continue
        new = choices(after).get(column)
        if new is None or new.body.id != old.body.id or new.orelse.id != old.orelse.id:
            continue
        enclosing = [node for node in spanning_nodes(before, line, line) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if not enclosing:
            continue
        func = min(enclosing, key=lambda node: node.end_lineno - node.lineno)
        parameters = {argument.arg for argument in [*func.args.args, *func.args.posonlyargs, *func.args.kwonlyargs]}
        if old.body.id not in parameters or old.orelse.id not in parameters:
            continue
        old_test, new_test = old.test, new.test
        if not any(
            isinstance(negated, ast.UnaryOp) and isinstance(negated.op, ast.Not) and ast.dump(negated.operand) == ast.dump(plain)
            for negated, plain in ((old_test, new_test), (new_test, old_test))
        ):
            continue
        condition = old_test.operand if isinstance(old_test, ast.UnaryOp) and isinstance(old_test.op, ast.Not) else old_test
        if not (isinstance(condition, ast.Compare) and len(condition.ops) == 1 and isinstance(condition.ops[0], (ast.Is, ast.IsNot))):
            continue
        if unchanged_with_original(after, baseline_dump(before), new, {'test': old.test}):
            risk = int(maps['choices'][key], 16) | maps['unmeasured_branch'] | child_mask(path, maps)
            reached = int(maps['lines'].get(path, {}).get(str(line), '0'), 16)
            return (reached & risk) | int(maps['imports'].get(path, {}).get(str(line), '0'), 16)
    return None


def _same_call_shape(old, new):
    """Same arity and keyword names; differing arguments must both be constants."""
    return (
        len(old.args) == len(new.args)
        and [keyword.arg for keyword in old.keywords] == [keyword.arg for keyword in new.keywords]
        and not any(
            ast.dump(old_argument) != ast.dump(new_argument) and not (isinstance(old_argument, ast.Constant) and isinstance(new_argument, ast.Constant))
            for old_argument, new_argument in zip(old.args + [keyword.value for keyword in old.keywords], new.args + [keyword.value for keyword in new.keywords])
        )
    )


def operation_edit(path, before, after, removed, maps):
    """One deferred operator, comparison or call argument changed on a single line."""
    if len(removed) != 1 or path not in maps['instructions']:
        return None
    deferred = deferred_nodes(before)
    line = next(iter(removed))
    old_operations = {
        (type(node), node.col_offset): node for node in spanning_nodes(before, line, line) if isinstance(node, OPERATIONS) and node.lineno == node.end_lineno == line
    }
    for new in spanning_nodes(after, line, line):
        if not isinstance(new, OPERATIONS) or new.lineno != new.end_lineno:
            continue
        old = old_operations.get((type(new), new.col_offset))
        if old is None or ast.dump(new) == ast.dump(old):
            continue
        if id(old) not in deferred:
            return None
        if f'{type(old).__name__}:{line}:{old.col_offset}:{old.end_col_offset}' not in maps['instructions'][path]:
            continue
        if isinstance(old, ast.Compare):
            if len(old.ops) != 1 or len(new.ops) != 1:
                continue
            restore = {'ops': old.ops}
        elif isinstance(old, ast.Call):
            if not _same_call_shape(old, new):
                continue
            restore = {'args': old.args, 'keywords': old.keywords}
        else:
            restore = {'op': old.op}
        if unchanged_with_original(after, baseline_dump(before), new, restore):
            reached = int(maps['lines'].get(path, {}).get(str(line), '0'), 16)
            import_time = int(maps['imports'].get(path, {}).get(str(line), '0'), 16)
            if isinstance(old, ast.Compare):
                import_time = cold_order_mask(path, old, new, import_time, maps)
                site = f'{path}:{line}:{old.col_offset}'
                for table, unmeasured in (('orders', 'unmeasured_orders'), ('branch_orders', 'unmeasured_branch')):
                    profile = maps[table].get(site)
                    if profile is not None:
                        flipped = changed_outcomes(profile, type(old.ops[0]), type(new.ops[0]))
                        reached &= flipped | child_mask(path, maps) | maps[unmeasured]
            return reached | import_time
    return None


def _member_keys(compare):
    return {('s' if isinstance(element.value, str) else 'i') + str(element.value) for element in compare.comparators[0].elts}


def _cached_lines(refs, path):
    return set(refs.get('cached', {}).get(path, ()))


def membership_edit(path, before, after, removed, maps):
    """The literal collection of an `x in (...)` test changed on one line."""
    members = maps.get('members')
    if not members or len(removed) != 1:
        return None
    line = next(iter(removed))

    def sites(tree):
        return {
            node.col_offset: node
            for node in spanning_nodes(tree, line, line)
            if isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and node.lineno == line
            and isinstance(node.comparators[0], (ast.Tuple, ast.List, ast.Set))
            and node.comparators[0].elts
            and all(isinstance(element, ast.Constant) and type(element.value) in (str, int) for element in node.comparators[0].elts)
        }

    old_sites, new_sites = sites(before), sites(after)
    for column, old in old_sites.items():
        new = new_sites.get(column)
        if new is None or ast.dump(new) == ast.dump(old) or type(new.ops[0]) is not type(old.ops[0]):
            continue
        if type(new.comparators[0]) is not type(old.comparators[0]) or ast.dump(new.left) != ast.dump(old.left):
            continue
        if not unchanged_with_original(after, baseline_dump(before), new, {'comparators': old.comparators}):
            continue
        site = f'{path}:{old.lineno}:{old.col_offset}'
        observed = members['sites'].get(site)
        if site in members['overflow']:
            return None
        refs = load_refs(maps)
        if not refs or old.lineno in _cached_lines(refs, path):
            return None
        flipped = _member_keys(old) ^ _member_keys(new)
        if flipped & set(members['imported'].get(site, ())) or '?' in members['imported'].get(site, ()):
            return None
        mask = child_mask(path, maps)
        for key, key_tests in (observed or {}).items():
            if key == '?' or key in flipped:
                mask |= int(key_tests, 16)
        return mask
    return None


def truth_only(old, new):
    """Whether new is just the negation of old (so every evaluation flips)."""
    for negated, plain in ((old, new), (new, old)):
        if isinstance(negated, ast.UnaryOp) and isinstance(negated.op, ast.Not) and ast.dump(negated.operand) == ast.dump(plain):
            return True
    if isinstance(old, ast.Constant) and isinstance(new, ast.Constant) and isinstance(old.value, bool) and isinstance(new.value, bool):
        return True
    if (
        isinstance(old, ast.Compare)
        and isinstance(new, ast.Compare)
        and len(old.ops) == len(new.ops) == 1
        and ast.dump(old.left) == ast.dump(new.left)
        and ast.dump(old.comparators[0]) == ast.dump(new.comparators[0])
    ):
        return any({type(old.ops[0]), type(new.ops[0])} <= pair for pair in TRUTH_SWAPS)
    return False


def _touches_cached_lines(refs, path, node):
    cached = _cached_lines(refs, path)
    return any(number in cached for number in range(node.lineno, node.end_lineno + 1))


def boolop_edit(path, before, after, removed, inserted, maps):
    """One operand of an and/or chain changed: tests that evaluated that operand."""
    profile = maps.get('boolops')
    if not profile or inserted or not removed:
        return None
    first, last = min(removed), max(removed)
    old_chains = {(node.lineno, node.col_offset): node for node in ast.walk(before) if isinstance(node, ast.BoolOp) and node.lineno <= first and last <= node.end_lineno}
    best = None
    for new in ast.walk(after):
        if not isinstance(new, ast.BoolOp):
            continue
        old = old_chains.get((new.lineno, new.col_offset))
        if old is None or type(old.op) is not type(new.op) or len(old.values) != len(new.values) or ast.dump(old) == ast.dump(new):
            continue
        changed = [position for position, (old_value, new_value) in enumerate(zip(old.values, new.values)) if ast.dump(old_value) != ast.dump(new_value)]
        if len(changed) != 1:
            continue
        position = changed[0]
        if not (old.values[position].lineno <= first and last <= old.values[position].end_lineno):
            continue
        restored = list(new.values)
        restored[position] = old.values[position]
        if not unchanged_with_original(after, baseline_dump(before), new, {'values': restored}):
            continue
        if best is None or old.end_lineno - old.lineno < best[0].end_lineno - best[0].lineno:
            best = (old, new, position)
    if best is None:
        return swapped_boolop(path, before, after, removed, maps, old_chains)
    old, new, position = best
    site = f'{path}:{old.lineno}:{old.col_offset}'
    if position in set(profile['imported'].get(site, ())):
        return None
    refs = load_refs(maps)
    if not refs or _touches_cached_lines(refs, path, old):
        return None
    table = profile['unsafe'] if truth_only(old.values[position], new.values[position]) else profile['ev']
    return int(table.get(site, {}).get(str(position), '0'), 16) | int(profile['unprofiled'], 16) | child_mask(path, maps)


def swapped_boolop(path, before, after, removed, maps, old_chains):
    """An `and` chain became `or` (or the reverse) with the same operands."""
    profile = maps['boolops']
    for new in ast.walk(after):
        if not isinstance(new, ast.BoolOp):
            continue
        old = old_chains.get((new.lineno, new.col_offset))
        if (
            old is None
            or type(old.op) is type(new.op)
            or len(old.values) != len(new.values)
            or any(ast.dump(old_value) != ast.dump(new_value) for old_value, new_value in zip(old.values, new.values))
        ):
            continue
        if not unchanged_with_original(after, baseline_dump(before), new, {'op': old.op}):
            continue
        site = f'{path}:{old.lineno}:{old.col_offset}'
        if profile['imported'].get(site) or 'swap' not in profile:
            return None
        refs = load_refs(maps)
        if not refs or _touches_cached_lines(refs, path, old):
            return None
        return int(profile['swap'].get(site, '0'), 16) | int(profile['unprofiled'], 16) | child_mask(path, maps)
    return None
