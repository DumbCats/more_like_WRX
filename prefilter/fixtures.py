"""Masks for patches that only touch SQL fixture files under tests/fixtures/."""
import re
import runpy
from functools import cache
from io import StringIO
from pathlib import Path
from types import FunctionType

from diff_reader import side_paths
from settings import REPO

FILE_LEVEL_HEADERS = ('new file mode 100644', 'deleted file mode 100644', 'rename from ')
BENCHMARK_FIXTURES = ('tests/fixtures/optimizer/tpc-ds/', 'tests/fixtures/optimizer/tpc-h/')
PARSED_MODES = {'load_sql_fixtures', 'load_sql_fixture_pairs'}


def fixture_structure_mask(diff, maps, all_mask):
    """Mask for a single-block patch that adds, deletes or renames one .sql fixture, else None.

    Such a patch changes which fixture files exist, so it selects every test that read,
    listed or globbed one of the affected paths or their directories.
    """
    if diff.count('diff --git ') != 1:
        return None
    block = diff.partition('diff --git ')[2]
    git_line = block.splitlines()[0] if block else ''
    if not re.fullmatch(r'a/[\w./-]+ b/[\w./-]+', git_line):
        return None
    if not any(line.startswith(FILE_LEVEL_HEADERS) for line in block.splitlines()):
        return None
    paths = side_paths(block)
    if not paths or any(
        not path.startswith('tests/fixtures/')
        or not path.endswith('.sql')
        or '..' in Path(path).parts
        or not (REPO / path).parent.is_dir()
        for path in paths
    ):
        return None
    if any(path.startswith(BENCHMARK_FIXTURES) for path in paths):
        return None
    fixture_paths = maps['fixture_paths']
    mask = int(fixture_paths['external'], 16)
    for path in paths:
        folder = str(Path(path).parent)
        for event in ('read:' + path, 'file:' + path, 'dir:' + folder, 'file:' + folder):
            mask |= int(fixture_paths['events'].get(event, '0'), 16)
        mask |= int(maps['files'].get(path, '0'), 16)
    return mask & all_mask


@cache
def fixture_helpers():
    """The namespace of the repository's tests/helpers.py."""
    return runpy.run_path(str(REPO / 'tests/helpers.py'))


def fixture_values(mode, content):
    """What the named tests/helpers.py loader yields for a fixture file with this content."""
    loader = fixture_helpers()[mode]
    namespace = dict(loader.__globals__, open=lambda *args, **kwargs: StringIO(content))
    patched_loader = FunctionType(loader.__code__, namespace)
    if mode == 'load_sql_fixture_pairs':
        return [(tuple(meta.items()), sql, expected) for meta, sql, expected in patched_loader('fixture.sql')]
    return list(patched_loader('fixture.sql'))


def fixture_selection(path, base, revised, readers, maps):
    """Narrow the readers of an edited fixture by what each loading mode actually sees change."""
    modes = maps['fixture_modes'].get(path)
    if not modes:
        return readers
    profiled = 0
    selected = 0
    for mode, mode_hex in modes.items():
        mode_tests = int(mode_hex, 16)
        profiled |= mode_tests
        if mode in PARSED_MODES:
            try:
                old_values = fixture_values(mode, base)
                new_values = fixture_values(mode, revised)
                if old_values == new_values:
                    continue
            except (IndexError, ValueError):
                selected |= mode_tests
                continue
            if mode == 'load_sql_fixture_pairs' and path in maps['fixture_attributes']:
                mode_tests &= int(maps['fixture_attributes'][path], 16)
                field_masks = maps['fixture_fields'].get(path)
                if field_masks and len(old_values) == len(new_values):
                    changed_fields = 0
                    for field, field_hex in enumerate(field_masks):
                        if any(old[field] != new[field] for old, new in zip(old_values, new_values)):
                            changed_fields |= int(field_hex, 16)
                    mode_tests &= changed_fields
        selected |= mode_tests
    return selected | (readers & ~profiled)
