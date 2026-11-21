"""Build generated/instructions.gz: single-line operations that compile to their own instruction.

Usage: instruction_map.py <repo> <output.gz>

For every sqlglot module, lists 'Kind:line:col:end_col' for Compare/BinOp/UnaryOp/Call nodes
whose position is carried by a matching bytecode instruction, so a changed operator can be
attributed to that exact line's coverage.
"""
import ast
import dis
import gzip
import json
import sys
from pathlib import Path
from types import CodeType

OPCODE_KINDS = {
    'COMPARE_OP': 'Compare',
    'IS_OP': 'Compare',
    'CONTAINS_OP': 'Compare',
    'BINARY_OP': 'BinOp',
    'UNARY_NEGATIVE': 'UnaryOp',
    'UNARY_INVERT': 'UnaryOp',
    'UNARY_NOT': 'UnaryOp',
    'CALL': 'Call',
    'CALL_KW': 'Call',
}
OPERATION_NODES = (ast.Compare, ast.BinOp, ast.UnaryOp, ast.Call)


def instruction_sites(code):
    """(kind, line, col, end_col) of single-line operation instructions in code and nested code."""
    for instruction in dis.get_instructions(code):
        kind = OPCODE_KINDS.get(instruction.opname)
        position = instruction.positions
        if kind and position.lineno and position.lineno == position.end_lineno:
            yield kind, position.lineno, position.col_offset, position.end_col_offset
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            yield from instruction_sites(constant)


def build(root, output):
    sites_by_file = {}
    for path in sorted((root / 'sqlglot').rglob('*.py')):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_bytes(), relative)
        compiled = set(instruction_sites(compile(tree, relative, 'exec', dont_inherit=True)))
        sites = sorted(
            {
                f'{type(node).__name__}:{node.lineno}:{node.col_offset}:{node.end_col_offset}'
                for node in ast.walk(tree)
                if isinstance(node, OPERATION_NODES)
                and node.lineno == node.end_lineno
                and (type(node).__name__, node.lineno, node.col_offset, node.end_col_offset) in compiled
            }
        )
        if sites:
            sites_by_file[relative] = sites
    with gzip.open(output, 'wt') as handle:
        json.dump(sites_by_file, handle, separators=(',', ':'))


if __name__ == '__main__':
    build(Path(sys.argv[1]), Path(sys.argv[2]))
