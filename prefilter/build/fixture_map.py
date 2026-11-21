"""Offline generator of data/fixture-attrs.gz (not run at image build; its output is shipped).

Run from the repository root. For each test class that binds a fixture file in setUpClass
via `cls.attr = list(load_sql_fixture_pairs('file'))`, records the tests outside that class
that read the fixture plus the tests executing a line that loads `attr`.
Also provides class_bindings(), used by fixture_fields.py.
"""
import ast
import gzip
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'


def class_bindings(cls):
    """(attribute, fixture name) for each `cls.attribute = list(load_sql_fixture_pairs('name'))`."""
    setup = next((node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'setUpClass'), None)
    if setup is None:
        return
    for statement in setup.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target, value = statement.targets[0], statement.value
        if not (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == 'cls'
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == 'list'
            and len(value.args) == 1
            and isinstance(value.args[0], ast.Call)
        ):
            continue
        loader = value.args[0]
        if (
            isinstance(loader.func, ast.Name)
            and loader.func.id == 'load_sql_fixture_pairs'
            and len(loader.args) == 1
            and isinstance(loader.args[0], ast.Constant)
            and isinstance(loader.args[0].value, str)
        ):
            yield target.attr, loader.args[0].value


def build():
    with gzip.open(DATA_DIR / 'coverage.json.gz', 'rt') as handle:
        coverage = json.load(handle)
    sites = {}
    for test_path in Path('tests').rglob('test_*.py'):
        relative = test_path.as_posix()
        tree = ast.parse(test_path.read_bytes())
        for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            for attribute, fixture in class_bindings(cls):
                if test_path.read_text().count('"' + fixture + '"') != 1:
                    continue
                if any(isinstance(node, ast.Constant) and node.value == attribute for node in ast.walk(tree)):
                    continue
                class_tests = sum(1 << position for position, name in enumerate(coverage['names']) if name.startswith(f'{relative}::{cls.name}::'))
                fixture_path = 'tests/fixtures/' + fixture
                readers = int(coverage['files'].get(fixture_path, '0'), 16)
                if not class_tests or readers & class_tests != class_tests or fixture_path in sites:
                    continue
                mask = readers & ~class_tests
                for node in ast.walk(tree):
                    if isinstance(node, ast.Attribute) and node.attr == attribute and isinstance(node.ctx, ast.Load):
                        mask |= int(coverage['lines'].get(relative, {}).get(str(node.lineno), '0'), 16)
                sites[fixture_path] = hex(mask)
    with gzip.open(DATA_DIR / 'fixture-attrs.gz', 'wt') as handle:
        json.dump({'names': coverage['names'], 'sites': sites}, handle, separators=(',', ':'))


if __name__ == '__main__':
    build()
