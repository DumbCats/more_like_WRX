"""Exact test sets for single-comparison edits, from recorded evaluation profiles.

When a patch changes exactly one comparison in sqlglot, the profile tables tell,
for every evaluation of that comparison site during the suite, what the
comparison saw; from that we can decide which tests would observe a different
outcome ("infected" tests).

- tables/infect.bin (v1): the operator changed (`a < b` -> `a <= b`, ...). Each
  site stores per-test signatures: 2 bits per operator (0 false, 1 true,
  2 unknown).
- tables/infect2.bin (v2): an and/or expression was restructured ('bool'), or
  a literal operand of a comparison changed ('const').
"""
from __future__ import annotations

import ast
import marshal
import re
import zlib
from array import array

OPS = ('Is', 'IsNot', 'Eq', 'NotEq', 'Lt', 'LtE', 'Gt', 'GtE', 'In', 'NotIn')
OP_INDEX = {name: index for index, name in enumerate(OPS)}
_CODING_RE = re.compile('coding[:=]\\s*[-\\w.]+')

# caches keyed by table path
_TABLES = {}
_SITES = {}
_TEST_SETS = {}


class InfectionResult:
    """Tests that may change (`tests`), tests that surely change (`sure`), and
    whether the site is also evaluated at import time (`global_phase`)."""
    __slots__ = ('tests', 'global_phase', 'site', 'old_op', 'new_op', 'sure')

    def __init__(self, tests, global_phase, site, old_op, new_op, sure=frozenset()):
        self.tests = tests
        self.sure = sure
        self.global_phase = global_phase
        self.site = site
        self.old_op = old_op
        self.new_op = new_op

    def __repr__(self):
        return 'InfectionResult(%d tests, global=%s, site=%s, %s->%s)' % (len(self.tests), self.global_phase, self.site, self.old_op, self.new_op)


def _load(table_path):
    table = _TABLES.get(table_path, 0)
    if table == 0:
        try:
            with open(table_path, 'rb') as handle:
                table = marshal.loads(handle.read())
        except (OSError, ValueError, EOFError):
            table = None
        _TABLES[table_path] = table
    return table


def _file_sites(table_path, table, rel):
    """(line, col, end line, end col) -> (old op index, signature row) for one file."""
    cache_key = (table_path, rel)
    sites = _SITES.get(cache_key)
    if sites is None:
        packed = table['files'].get(rel)
        if packed is None:
            return None
        sites = {}
        for row in marshal.loads(zlib.decompress(packed)):
            sites[tuple(row[:4])] = (row[4], row[5])
        _SITES[cache_key] = sites
    return sites


def _test_set(table_path, table, set_id):
    """The test numbers of stored set `set_id`."""
    sets = _TEST_SETS.get(table_path)
    if sets is None:
        sets = _TEST_SETS[table_path] = marshal.loads(zlib.decompress(table['sets']))
    numbers = array('H')
    numbers.frombytes(sets[set_id])
    return numbers


def is_infected(signature, old_op, new_op):
    """True when an evaluation with this signature may change from old_op to new_op."""
    if old_op >= 8 or new_op >= 8:
        return True
    old_outcome = signature >> 2 * old_op & 3
    new_outcome = signature >> 2 * new_op & 3
    return old_outcome == 2 or new_outcome == 2 or old_outcome != new_outcome


def _blank_or_comment(lines):
    return all((not line.strip() or line.lstrip().startswith('#') for line in lines))


def _parse_unit(lines, start, end, class_path):
    """Parse lines start..end at their real line numbers, nested in their classes."""
    depth = len(class_path)
    padding = start - 1 - depth
    if padding < 0:
        return None
    class_headers = ''.join((' ' * level + 'class ' + name + ':\n' for level, name in enumerate(class_path)))
    source = '\n' * padding + class_headers + '\n'.join(lines[start - 1:end]) + '\n'
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def _unit_differences(old_lines, new_lines, groups, units, same):
    """Compare every unit the change groups touch with `same`; the collected differences.

    `units` are (start, end, class path, ...) spans of the old file. None when
    an edit falls outside any unit (other than comments/blank lines), a unit
    does not parse, or `same` rejects a unit.
    """
    groups_by_unit = {}
    for group in groups:
        old_start, old_count, new_start, new_count, removed, added = group
        touched = []
        for unit_index, unit in enumerate(units):
            unit_start, unit_end = (unit[0], unit[1])
            if old_count > 0:
                if unit_start <= old_start + old_count - 1 and old_start <= unit_end:
                    touched.append(unit_index)
            elif unit_start < old_start <= unit_end:
                touched.append(unit_index)
        if not touched:
            if not (_blank_or_comment(added) and _blank_or_comment(removed)):
                return None
            if old_start <= 2 or new_start <= 2:
                if any((_CODING_RE.search(line) for line in added + removed)):
                    return None
            continue
        for unit_index in touched:
            groups_by_unit.setdefault(unit_index, []).append(group)
    differences = []
    for unit_index in sorted(groups_by_unit):
        unit_start, unit_end, class_path = (units[unit_index][0], units[unit_index][1], units[unit_index][2])
        shift_before = shift_inside = 0
        for group in groups:
            if group[0] + group[1] - 1 < unit_start and group[0] < unit_start:
                shift_before += group[3] - group[1]
            elif group[0] <= unit_end:
                shift_inside += group[3] - group[1]
        old_tree = _parse_unit(old_lines, unit_start, unit_end, class_path)
        new_tree = _parse_unit(new_lines, unit_start + shift_before, unit_end + shift_before + shift_inside, class_path)
        if old_tree is None or new_tree is None:
            return None
        if not same(old_tree, new_tree, differences):
            return None
    return differences


# --- v1: a changed comparison operator ---------------------------------------------

def _same_but_operator(a, b, out):
    """AST equality allowing one comparison whose operator changed (recorded in `out`)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        if isinstance(a, ast.Compare) and len(a.ops) == 1 and (len(b.ops) == 1) and (type(a.ops[0]) is not type(b.ops[0])):
            nested = []
            if not (_same_but_operator(a.left, b.left, nested) and _same_but_operator(a.comparators[0], b.comparators[0], nested)) or nested:
                return False
            out.append((a, type(a.ops[0]).__name__, type(b.ops[0]).__name__))
            return len(out) <= 1
        for field in a._fields:
            if not _same_but_operator(getattr(a, field, None), getattr(b, field, None), out):
                return False
        return True
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        for item_a, item_b in zip(a, b):
            if not _same_but_operator(item_a, item_b, out):
                return False
        return True
    if isinstance(a, (float, complex)):
        return repr(a) == repr(b)
    return a == b


def single_compare_edit(old_lines, new_lines, groups, units):
    """(site, old op name, new op name) of the one changed operator; () if none; None otherwise."""
    differences = _unit_differences(old_lines, new_lines, groups, units, _same_but_operator)
    if differences is None:
        return None
    if not differences:
        return ()
    if len(differences) != 1:
        return None
    node, old_op, new_op = differences[0]
    return ((node.lineno, node.col_offset, node.end_lineno, node.end_col_offset), old_op, new_op)


def infected_tests_by_operator(path, old_lines, new_lines, groups, units, table_path):
    """InfectionResult for a single operator change in `path`, or None when not applicable."""
    table = _load(table_path)
    if table is None or path not in table['files']:
        return None
    edit = single_compare_edit(old_lines, new_lines, groups, units)
    if not edit:
        return None
    site, old_name, new_name = edit
    sites = _file_sites(table_path, table, path)
    recorded = sites.get(site) if sites else None
    if recorded is None:
        return None
    recorded_op, signatures = recorded
    old_op, new_op = (OP_INDEX[old_name], OP_INDEX[new_name])
    if recorded_op != old_op:
        return None
    tests = set()
    sure = set()
    global_phase = False
    for offset in range(0, len(signatures), 3):
        signature, set_id, at_import = (signatures[offset], signatures[offset + 1], signatures[offset + 2])
        if is_infected(signature, old_op, new_op):
            if set_id:
                numbers = _test_set(table_path, table, set_id)
                tests.update(numbers)
                if old_op < 8 and new_op < 8:
                    old_outcome, new_outcome = (signature >> 2 * old_op & 3, signature >> 2 * new_op & 3)
                    if old_outcome != 2 and new_outcome != 2 and (old_outcome != new_outcome):
                        sure.update(numbers)
            if at_import:
                global_phase = True
    return InfectionResult(frozenset(tests), global_phase, (path,) + site, old_name, new_name, frozenset(sure))


# --- v2: and/or restructuring and changed literal operands --------------------------

BOOL_RAISED = (1 << 63) - 1
_TABLES_V2 = {}
_ROWS_V2 = {}


def _load_v2(table_path):
    table = _TABLES_V2.get(table_path, 0)
    if table == 0:
        try:
            with open(table_path, 'rb') as handle:
                table = marshal.loads(handle.read())
            if table.get('version') != 2:
                table = None
        except (OSError, ValueError, EOFError):
            table = None
        _TABLES_V2[table_path] = table
    return table


def _rows_v2(table_path, table, kind, rel):
    """Site -> recorded row for one file of the 'bfiles' or 'kfiles' section."""
    cache_key = (table_path, kind, rel)
    rows = _ROWS_V2.get(cache_key)
    if rows is None:
        packed = table[kind].get(rel)
        if packed is None:
            return None
        rows = {}
        for row in marshal.loads(zlib.decompress(packed)):
            rows[tuple(row[:4])] = row[4:]
        _ROWS_V2[cache_key] = rows
    return rows


def _flatten(node, leaves):
    """Shape of a nested and/or: ('O'|'A', leaf index or sub-shape, ...); collects leaves."""
    shape = ['O' if isinstance(node.op, ast.Or) else 'A']
    for value in node.values:
        if isinstance(value, ast.BoolOp):
            shape.append(_flatten(value, leaves))
        else:
            shape.append(len(leaves))
            leaves.append(value)
    return tuple(shape)


def _const_value(node):
    """(True, value) for a literal (incl. signed numbers), else (False, None)."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        operand = node.operand
        if isinstance(operand, ast.Constant) and type(operand.value) in (int, float):
            return (True, -operand.value if isinstance(node.op, ast.USub) else operand.value)
        return (False, None)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float, str, bool, type(None)):
        return (True, node.value)
    return (False, None)


def _same_but_operand(a, b, out):
    """AST equality allowing one restructured and/or, or one changed literal operand."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        if isinstance(a, ast.BoolOp):
            leaves_a, leaves_b = ([], [])
            shape_a, shape_b = (_flatten(a, leaves_a), _flatten(b, leaves_b))
            if len(leaves_a) != len(leaves_b):
                return False
            if shape_a == shape_b:
                for leaf_a, leaf_b in zip(leaves_a, leaves_b):
                    if not _same_but_operand(leaf_a, leaf_b, out):
                        return False
                return True
            for leaf_a, leaf_b in zip(leaves_a, leaves_b):
                nested = []
                if not _same_but_operand(leaf_a, leaf_b, nested) or nested:
                    return False
            out.append(('bool', a, shape_a, shape_b))
            return len(out) <= 1
        if isinstance(a, ast.Compare) and len(a.ops) == 1 and (len(b.ops) == 1) and (type(a.ops[0]) is type(b.ops[0])):
            operands = ((a.left, a.comparators[0]), (b.left, b.comparators[0]))
            old_consts = [_const_value(operand) for operand in operands[0]]
            new_consts = [_const_value(operand) for operand in operands[1]]
            for side, other in ((1, 0), (0, 1)):
                if old_consts[side][0] and new_consts[side][0] and (not old_consts[other][0]):
                    old_value, new_value = (old_consts[side][1], new_consts[side][1])
                    if type(old_value) is type(new_value) and (repr(old_value) == repr(new_value) if type(old_value) is float else old_value == new_value):
                        break
                    nested = []
                    if not _same_but_operand(operands[0][other], operands[1][other], nested) or nested:
                        return False
                    out.append(('const', a, side, old_value, new_value))
                    return len(out) <= 1
        for field in a._fields:
            if not _same_but_operand(getattr(a, field, None), getattr(b, field, None), out):
                return False
        return True
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        for item_a, item_b in zip(a, b):
            if not _same_but_operand(item_a, item_b, out):
                return False
        return True
    if isinstance(a, (float, complex)):
        return repr(a) == repr(b)
    return type(a) is type(b) and a == b


def single_operand_edit(old_lines, new_lines, groups, units):
    """('bool', site, old shape, new shape) or ('const', site, op, side, old, new); else None."""
    differences = _unit_differences(old_lines, new_lines, groups, units, _same_but_operand)
    if differences is None or len(differences) != 1:
        return None
    difference = differences[0]
    node = difference[1]
    site = (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
    if difference[0] == 'bool':
        return ('bool', site, difference[2], difference[3])
    return ('const', site, type(node.ops[0]).__name__, difference[2], difference[3], difference[4])


class _Unknown(Exception):
    pass


def _bool_changed(signature, leaf_count, old_shape, new_shape, value_context):
    """Whether one recorded evaluation of an and/or can differ under the new shape.

    The signature packs 4 bits per leaf (bits 0-1 truth: 0 false, 1 true,
    2 unknown; bit 2 evaluated before; bit 3 safe to evaluate) and, from bit 32,
    3 bits per leaf with a value class (compared when the value is used).
    """
    if signature == BOOL_RAISED:
        return True
    leaf_state = [signature >> 4 * index & 15 for index in range(leaf_count)]
    value_class = [signature >> 32 + 3 * index & 7 for index in range(leaf_count)]
    evaluated_before = {index for index in range(leaf_count) if leaf_state[index] & 4}

    def evaluate(node, evaluated, allowed):
        operator = node[0]
        children = node[1:]
        last = len(children) - 1
        for position, child in enumerate(children):
            if isinstance(child, tuple):
                result = evaluate(child, evaluated, allowed)
            else:
                if not allowed(child):
                    raise _Unknown
                evaluated.append(child)
                result = child
            if position == last:
                return result
            truth = leaf_state[result] & 3
            if truth == 2:
                raise _Unknown
            if (operator == 'O') == (truth == 1):
                return result
        raise _Unknown
    try:
        evaluated_old = []
        result_old = evaluate(old_shape, evaluated_old, lambda index: leaf_state[index] & 4)
        if set(evaluated_old) != evaluated_before or (evaluated_before and result_old != max(evaluated_before)):
            return True
        evaluated_new = []
        result_new = evaluate(new_shape, evaluated_new, lambda index: leaf_state[index] & 12)
    except _Unknown:
        return True
    evaluated_now = set(evaluated_new)
    for index in evaluated_before ^ evaluated_now:
        if not leaf_state[index] & 8:
            return True
    tested = evaluated_before | evaluated_now if not value_context else evaluated_before - {result_old} | evaluated_now - {result_new}
    if not value_context:
        tested |= {result_old, result_new}
    for index in tested:
        if leaf_state[index] & 3 == 2:
            return True
    if value_context:
        return not (result_old == result_new or value_class[result_old] == value_class[result_new])
    return leaf_state[result_old] & 3 != leaf_state[result_new] & 3


def _decode(code):
    """A recorded runtime operand value: b0/b1, n (None), i<int>, s<hex utf-8>, f<float hex>."""
    tag = code[0]
    if tag == 'b':
        return code == 'b1'
    if tag == 'n':
        return None
    if tag == 'i':
        return int(code[1:])
    if tag == 's':
        return bytes.fromhex(code[1:]).decode('utf-8', 'surrogatepass')
    if tag == 'f':
        return float.fromhex(code[1:])
    raise _Unknown


_COMPARE = {'Eq': lambda a, b: a == b, 'NotEq': lambda a, b: a != b, 'Lt': lambda a, b: a < b, 'LtE': lambda a, b: a <= b, 'Gt': lambda a, b: a > b, 'GtE': lambda a, b: a >= b}


def _compare_value(op, value, literal, side):
    """Outcome of `value <op> literal` (literal on `side` 1 = right, 0 = left)."""
    left, right = (value, literal) if side == 1 else (literal, value)
    if op in ('Is', 'IsNot'):
        if type(left) is not type(right):
            outcome = False
        elif type(left) in (bool, type(None)):
            outcome = left == right
        else:
            raise _Unknown
        return outcome if op == 'Is' else not outcome
    if type(left) is str and type(right) is not str or (type(right) is str and type(left) is not str):
        if op in ('Eq', 'NotEq'):
            return op == 'NotEq'
        raise _Unknown
    try:
        return bool(_COMPARE[op](left, right))
    except Exception:
        raise _Unknown


def _const_changed(code, op, side, old, new):
    """Whether a recorded operand (`code`) compares differently against old vs new literal.

    'U' unknown, 'O' an operand of another type, 'l'/'h' an int below/above
    the old literal (exact value not recorded), anything else an encoded value.
    """
    try:
        if code == 'U':
            return True
        if code == 'O':
            if op in ('Is', 'Eq'):
                return False
            if op in ('IsNot', 'NotEq'):
                return False
            return True
        if code in ('l', 'h'):
            if type(old) is not int or type(new) is not int or op in ('Is', 'IsNot'):
                return True
            if code == 'l':
                if new < old - 4:
                    return True
                probe = old - 5
            else:
                if new > old + 4:
                    return True
                probe = old + 5
            return _compare_value(op, probe, old, side) != _compare_value(op, probe, new, side)
        value = _decode(code)
        return _compare_value(op, value, old, side) != _compare_value(op, value, new, side)
    except _Unknown:
        return True


def _leaf_count(shape):
    return sum((_leaf_count(child) if isinstance(child, tuple) else 1 for child in shape[1:]))


def infected_tests_by_operand(path, old_lines, new_lines, groups, units, table_path):
    """InfectionResult for an and/or or literal-operand change in `path`, or None."""
    table = _load_v2(table_path)
    if table is None:
        return None
    edit = single_operand_edit(old_lines, new_lines, groups, units)
    if edit is None:
        return None
    kind, site = (edit[0], edit[1])
    tests = set()
    global_phase = False
    if kind == 'bool':
        rows = _rows_v2(table_path, table, 'bfiles', path)
        row = rows.get(site) if rows else None
        if row is None:
            return None
        old_shape, context, signatures = row
        if old_shape != edit[2]:
            return None
        leaf_count = _leaf_count(old_shape)
        new_shape = edit[3]
        value_context = context == 'V'
        for offset in range(0, len(signatures), 3):
            signature, set_id, at_import = (signatures[offset], signatures[offset + 1], signatures[offset + 2])
            if _bool_changed(signature, leaf_count, old_shape, new_shape, value_context):
                if set_id:
                    tests.update(_test_set(table_path, table, set_id))
                if at_import:
                    global_phase = True
        return InfectionResult(frozenset(tests), global_phase, (path,) + site, 'and/or', 'and/or')
    op, side, old_value, new_value = (edit[2], edit[3], edit[4], edit[5])
    if old_value is None or new_value is None or type(old_value) not in (int, float, str, bool):
        return None
    rows = _rows_v2(table_path, table, 'kfiles', path)
    row = rows.get(site) if rows else None
    if row is None or OPS[row[0]] != op:
        return None
    signatures = row[1]
    for offset in range(0, len(signatures), 3):
        code, set_id, at_import = (signatures[offset], signatures[offset + 1], signatures[offset + 2])
        if _const_changed(code, op, side, old_value, new_value):
            if set_id:
                tests.update(_test_set(table_path, table, set_id))
            if at_import:
                global_phase = True
    return InfectionResult(frozenset(tests), global_phase, (path,) + site, repr(old_value), repr(new_value))
