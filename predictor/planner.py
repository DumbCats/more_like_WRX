"""Plan, per commit, which tests a patch can affect and how they must be run.

A plan's `mode` says how the candidates are checked:
  'noop'   nothing can change, no test selected;
  'blind'  select `blind_tests` without running anything;
  'all'    the patch is not understood: select every test;
  'hot'    the change is confined to function bodies: a child swaps the new
           code objects into the already imported sqlglot and runs the candidates;
  'fresh'  the change acts at import time: a child imports the patched tree
           (the coordinator later turns this into 'fresh_purge' or 'fresh_cone').
"""
from __future__ import annotations

import json
import os
import sys

import patch_heuristics
import patching
import settings
from source_analysis import (CODING_RE, blank_or_comment, code_kind_changed, indent_width, line_groups,
                             python_text, registry_spans, shared_write_watch, type_checking_groups,
                             unit_line_shift, writes_shared_state)


class Plan:
    """Everything known about one commit: candidates, notes, and what its child reported."""
    __slots__ = ('commit', 'mode', 'candidates', 'work', 'files', 'notes', 'all_fail', 'blind_tests', 'affected_lines',
                 'reductions', 'broken_modules', 'test_times', 'pool_deaths', 'fpairs', 'stmt_lines', 'changed_names',
                 'def_units', 'data_candidates', 'skipped', 'diffable', 'class_filter', 'plain_lines', 'unaffected',
                 'protected', 'enders', 'gone', 'fixture_users', 'leak_from', 'must_select', 'added_tests',
                 'infected_after_import', 'infection_kinds', 'watch_specs', 'watched_keys', 'container_leaks',
                 'token_checks', 'sure_infected', 'cone_modules')

    def __init__(self, commit):
        self.commit = commit
        self.mode = 'noop'
        self.candidates = set()        # test numbers that may change verdict
        self.work = []                 # hot mode: [(full path, [(old code, new code)])] to swap in
        self.files = {}                # rel path -> patched text (None = deleted)
        self.notes = []
        self.all_fail = False          # select every test (patched import broke within the deadline)
        self.blind_tests = set()       # selected without being run
        self.affected_lines = {}       # rel path -> changed lines, or {'?': None} when unknown
        self.reductions = {}           # test number -> how to run a reduced body (see refinement)
        self.broken_modules = []       # modules that failed to import in the child
        self.test_times = {}           # test number -> child CPU seconds (debug)
        self.pool_deaths = []          # tests in which the inline process pool broke
        self.fpairs = []               # (old code, new code) of changes run at import time
        self.stmt_lines = {}           # rel path -> lines of changed import-time statements
        self.infected_after_import = None
        self.infection_kinds = []      # [(tests the infection analysis found, 'hot'|'import'|'other')]
        self.changed_names = None      # names whose import-time state changed (debug)
        self.def_units = {}            # rel path -> lines of changed function units
        self.data_candidates = set()   # candidates that read changed data files
        self.skipped = set()           # tests the child skipped after a decisive failure
        self.diffable = True           # the import-state diff can refine this plan
        self.class_filter = None
        self.plain_lines = set()
        self.unaffected = set()
        self.protected = set()         # cheap tests that decide held tests of their class/fixtures
        self.enders = {}
        self.fixture_users = set()     # tests reading only refreshed class fixtures
        self.leak_from = None          # first test after which shared state leaked
        self.gone = set()              # top-level names the patch removes
        self.must_select = set()       # held tests that must be selected anyway
        self.token_checks = []         # held tests whose token streams the child compares
        self.sure_infected = set()
        self.added_tests = []          # [(new test id, number it would run before)]
        self.watch_specs = []          # [(module, container description)] to watch while testing
        self.watched_keys = None       # the containers the child resolved and watched
        self.container_leaks = []      # [(test number, container key, changed keys)]
        self.cone_modules = None       # fresh_cone: the modules to re-import ([] after a redo)


def plan_python_file(plan: Plan, file_patch, tables, old_text: str) -> bool:
    """Plan a modified Python file unit by unit; True if it needs a fresh import."""
    rel = file_patch.old_path
    record = tables.file(rel)
    if record is None:
        return True
    type_checking_only = type_checking_groups(file_patch, old_text)
    if type_checking_only:
        plan.notes.append('type checking only: %d of %d hunks' % (len(type_checking_only), len(file_patch.groups)))
    units, compile_flags, import_names = (record[0], record[1], record[2])
    unit_tests, unit_at_import, import_tests = (record[5], record[6], record[7])
    lines_of_file = plan.affected_lines.setdefault(rel, set())
    old_lines = old_text.split('\n')
    new_lines = file_patch.new_text.split('\n')
    full_path = os.path.join(settings.REPO, rel)
    groups_by_unit = {}
    for group_index, group in enumerate(file_patch.groups):
        if group_index in type_checking_only:
            continue
        old_first, removed_count, new_first, _, removed_lines, added_lines = group
        hit_units = []
        for unit_index, unit in enumerate(units):
            unit_first, unit_last = (unit[0], unit[1])
            if removed_count > 0:
                if unit_first <= old_first + removed_count - 1 and old_first <= unit_last:
                    hit_units.append(unit_index)
            elif unit_first < old_first <= unit_last:
                hit_units.append(unit_index)
        if not hit_units:
            if blank_or_comment(added_lines) and blank_or_comment(removed_lines):
                if new_first > 2 or not any((CODING_RE.search(line) for line in added_lines + removed_lines)):
                    continue
            return True
        for unit_index in hit_units:
            groups_by_unit.setdefault(unit_index, []).append(group)
    for unit_index, unit_groups in groups_by_unit.items():
        unit_first, unit_last, class_path, unit_kind = units[unit_index]
        if unit_kind == 1:
            return True
        shift_before, shift_inside = unit_line_shift(file_patch.groups, unit_first, unit_last)
        try:
            old_code = patching.compile_unit(old_lines, unit_first, unit_last, class_path, full_path, compile_flags, import_names)
            new_code = patching.compile_unit(new_lines, unit_first + shift_before, unit_last + shift_before + shift_inside, class_path, full_path, compile_flags, import_names)
        except (SyntaxError, ValueError):
            return True
        if old_code is None or new_code is None:
            return True
        code_pairs = []
        if not patching.equal_at_definition_time(old_code, new_code, code_pairs):
            code_pairs = []
            if patch_heuristics.tests_read_docstrings(tables) or not patch_heuristics.code_equal_except_docstrings(old_code, new_code, code_pairs):
                return True
            plan.notes.append('docstring only')
        if all((patching.code_equal(old, new) for old, new in code_pairs)):
            continue
        if not class_path and any((line.startswith('def __getattr__(') for line in old_lines[unit_first - 1:unit_last])):
            plan.candidates.update(tables.tests(import_tests))
            plan.gone.add('__getattr__')
            plan.notes.append('module __getattr__')
        line_for_line = True
        for _, removed_count, _, added_count, removed_lines, added_lines in unit_groups:
            if removed_count != added_count or any((indent_width(old) != indent_width(new) for old, new in zip(removed_lines, added_lines))):
                line_for_line = False
            elif any((line.lstrip().startswith(('nonlocal ', 'global ')) for line in removed_lines + added_lines)):
                line_for_line = False
        if line_for_line and any((code_kind_changed(old, new) for old, new in code_pairs)):
            line_for_line = False
            plan.notes.append('code kind changed')
        if line_for_line:
            if _plan_changed_lines(plan, tables, rel, old_lines, unit_first, unit_last, unit_groups, code_pairs, record, lines_of_file):
                return True
        else:
            if unit_at_import[unit_index]:
                return True
            plan.candidates.update(tables.tests(unit_tests[unit_index]))
            lines_of_file.update(range(unit_first, unit_last + 1))
        plan.work.append((full_path, code_pairs))
    return False


def _plan_changed_lines(plan, tables, rel, old_lines, unit_first, unit_last, unit_groups, code_pairs, record, lines_of_file):
    """Add the tests of the changed lines of a unit edited line for line.

    Returns True (after recording the import-time statements) if a changed
    line runs at import time, so the file needs a fresh import.
    """
    line_tests, line_at_import = (record[3], record[4])
    statement_first, statement_last = (record[9], record[10])
    changed = [line for group in unit_groups for line in range(group[0], group[0] + group[1])]
    for group in unit_groups:
        for offset in range(group[1]):
            span = patch_heuristics.statement_around_lambda(old_lines, unit_first, unit_last, group[0] + offset, group[4][offset], group[5][offset])
            if span:
                changed.extend((line for line in range(span[0], span[1] + 1) if line not in changed))
    tight = tables.tight_lines()
    # code lines changed by the patch: False = not computed yet, None = unknown
    changed_code_lines = False
    for line in changed:
        if line_at_import[line]:
            plan.fpairs.extend(code_pairs)
            statement_lines = plan.stmt_lines.setdefault(rel, set())
            for changed_line in changed:
                statement_lines.update(range(statement_first[changed_line], statement_last[changed_line] + 1) if statement_first[changed_line] else (changed_line,))
            return True
        tests = tables.tests(line_tests[line])
        if tight and changed_code_lines is not None and (not tight.keys().isdisjoint(tests)):
            if changed_code_lines is False:
                changed_code_lines = set()
                for old_code, new_code in code_pairs:
                    lines_of_pair = patching.changed_lines(old_code, new_code)
                    if lines_of_pair is None:
                        changed_code_lines = None
                        break
                    changed_code_lines |= lines_of_pair
            if changed_code_lines:
                running = [number for number in tests if number not in tight or not tight[number].get(rel, frozenset()).isdisjoint(changed_code_lines)]
                if len(running) != len(tests):
                    plan.notes.append('exact lines: %s not run' % sorted(set(tests) - set(running)))
                    tests = running
        plan.candidates.update(tests)
        lines_of_file.update(range(statement_first[line], statement_last[line] + 1) if statement_first[line] else (line,))
    return False


_TEST_READERS = None


def test_data_candidates(plan: Plan, file_patch, tables) -> bool:
    """Plan an edit of test data inside a test file (e.g. a list of SQL cases).

    test_readers.json lists, per test file, its data statements and the tests
    reading each name they bind. Returns True if every change is inside such a
    statement and the readers became the candidates.
    """
    global _TEST_READERS
    if _TEST_READERS is None:
        try:
            with open(os.path.join(tables.path, 'test_readers.json')) as handle:
                _TEST_READERS = json.load(handle)
        except (OSError, ValueError):
            _TEST_READERS = {}
    info = _TEST_READERS.get(file_patch.old_path)
    if not info or file_patch.status != 'modify':
        return False
    names = set()
    for old_first, removed_count, _, added_count, _, _ in file_patch.groups:
        if removed_count != added_count:
            return False
        for line in range(old_first, old_first + removed_count):
            statements = [statement for statement in info['stmts'] if statement[0] < line < statement[1] and statement[3]]
            if not statements:
                return False
            names.update(statements[0][2])
    readers = set()
    for name in names:
        readers.update(info['readers'].get(name, ()))
    if not readers:
        return False
    plan.candidates.update(readers)
    for name in names:
        plan.must_select.update(info['direct'].get(name, ()))
    plan.affected_lines['?'] = None
    plan.notes.append('test data readers of %s' % sorted(names))
    return True


def add_import_time_candidates(plan: Plan, file_patch, record, tables) -> None:
    """Add the tests that can see a change made at import time of a module."""
    if test_data_candidates(plan, file_patch, tables):
        return
    line_tests, import_line_tests, import_line_flags = (record[3], record[11], record[12])
    import_line_readers = record[13] if len(record) > 13 else {}
    paths = tables.paths()
    last_line = len(import_line_tests) - 2
    only_reshaped = True
    # rel path -> lines of test code reading the changed names (None = unknown)
    reader_lines = {}
    units = record[0]
    for old_first, _, _, _, _, _ in file_patch.groups:
        for unit_first, unit_last, _, unit_kind in units:
            if unit_kind == 0 and unit_first <= old_first <= unit_last and (unit_last > unit_first):
                plan.def_units.setdefault(file_patch.old_path, set()).update(range(unit_first, unit_last + 1))
    for old_first, removed_count, _, added_count, removed_lines, added_lines in file_patch.groups:
        lines = range(old_first, old_first + removed_count) if removed_count else (old_first - 1, old_first)
        line_for_line = removed_count == added_count and all((indent_width(old) == indent_width(new) for old, new in zip(removed_lines, added_lines)))
        if not line_for_line and (not (blank_or_comment(added_lines) and blank_or_comment(removed_lines))):
            only_reshaped = False
        for line in lines:
            if not 0 < line <= last_line:
                continue
            if import_line_flags[line]:
                plan.candidates.update(tables.tests(import_line_tests[line]))
                readers = import_line_readers.get(line) if line_for_line else None
                if readers is None:
                    reader_lines = None
                elif reader_lines is not None:
                    for file_id, reader_line in readers:
                        reader_lines.setdefault(paths[file_id], set()).add(reader_line)
            elif line_tests[line]:
                reader_lines = None
                plan.candidates.update(tables.tests(line_tests[line]))
            elif removed_count:
                only_reshaped = False
    if not only_reshaped:
        plan.candidates.update(tables.tests(record[7]))
        plan.candidates.update(tables.tests(record[8]))
        plan.notes.append('import-time fallback')
        reader_lines = None
    if reader_lines:
        for rel, lines in reader_lines.items():
            plan.affected_lines.setdefault(rel, set()).update(lines)
        plan.notes.append('reader lines')
    else:
        plan.affected_lines['?'] = None


def plan_with_infection(plan: Plan, file_patch, tables, old_text: str, infected_files: list) -> bool:
    """plan_python_file, narrowed by the comparison-infection analysis when it applies.

    The analysis (compare_infection) finds the tests whose evaluation a changed
    comparison/operand can reach. Files it handled are appended to `infected_files`.
    """
    record = tables.file(file_patch.old_path)
    result = None
    if record is not None:
        import compare_infection
        old_lines, new_lines = (old_text.split('\n'), file_patch.new_text.split('\n'))
        result = compare_infection.infected_tests_by_operator(file_patch.old_path, old_lines, new_lines, file_patch.groups, record[0], os.path.join(tables.path, 'infect.bin'))
        if result is None and settings.INFECT_BY_OPERAND:
            result = compare_infection.infected_tests_by_operand(file_patch.old_path, old_lines, new_lines, file_patch.groups, record[0], os.path.join(tables.path, 'infect2.bin'))
    if result is None or result.global_phase:
        if result is not None and result.sure:
            plan.sure_infected.update(result.sure)
        return plan_python_file(plan, file_patch, tables, old_text)
    infected_files.append(file_patch.old_path)
    if not result.tests:
        plan.notes.append('infect: no evaluation changes')
        plan.infection_kinds.append((result.tests, 'hot'))
        return False
    candidates_before = set(plan.candidates)
    pairs_before = len(plan.fpairs)
    needs_fresh = plan_python_file(plan, file_patch, tables, old_text)
    if not needs_fresh:
        added = plan.candidates - candidates_before
        plan.candidates = candidates_before | added & result.tests
        plan.notes.append('infect: %d of %d candidates' % (len(added & result.tests), len(added)))
        plan.infection_kinds.append((result.tests, 'hot'))
    elif plan.candidates == candidates_before and len(plan.fpairs) > pairs_before:
        plan.infection_kinds.append((result.tests, 'import'))
    else:
        plan.infection_kinds.append((result.tests, 'other'))
    return needs_fresh


# (test file, class) -> {method name without parameters: first test number}
_KNOWN_TESTS = None


def record_added_tests(plan: Plan, tables, rel, old_text, new_text) -> None:
    """Note the test methods a patch adds to test file `rel` (and where they would run)."""
    import ast
    global _KNOWN_TESTS
    test_ids = tables.meta['test_ids']
    if _KNOWN_TESTS is None:
        _KNOWN_TESTS = {}
        for number, test_id in enumerate(test_ids):
            parts = test_id.split('::')
            if len(parts) >= 3:
                _KNOWN_TESTS.setdefault((parts[0], parts[1]), {}).setdefault(parts[2].split('[')[0], number)

    def test_methods(text):
        try:
            tree = ast.parse(text or '')
        except (SyntaxError, ValueError):
            return None
        methods = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith('test'):
                        methods[node.name, item.name] = item
        return methods
    new_methods = test_methods(new_text)
    old_methods = test_methods(old_text) if old_text is not None else {}
    if new_methods is None or old_methods is None:
        return
    for (class_name, method_name), function in new_methods.items():
        if (class_name, method_name) in old_methods:
            continue
        known = _KNOWN_TESTS.get((rel, class_name))
        if known:
            later = [number for name, number in known.items() if name > method_name]
            runs_before = min(later) if later else max(known.values()) + 1
        else:
            runs_before = 0
        plan.added_tests.append(('%s::%s::%s' % (rel, class_name, method_name), runs_before))
        if writes_shared_state(function):
            plan.blind_tests.update(range(runs_before, len(test_ids)))
            plan.notes.append('added test %s writes shared state' % method_name)


def make_plan(commit, text, tables, use_infect=settings.INFECT) -> Plan:
    """Build the Plan of one commit from its patch text."""
    plan = Plan(commit)
    infected_files = []
    # per file: counts of what the plan held before it (to spot files not narrowed by infection)
    checkpoints = []
    file_patches = patching.parse_patch(text)
    if not file_patches:
        if text.strip():
            plan.notes.append('no file section understood')
            plan.mode = 'all'
        else:
            plan.notes.append('no files in patch')
        return plan
    for file_patch in file_patches:
        if not file_patch.ok:
            plan.notes.append('patch not understood: %s (%s)' % (file_patch.new_path or file_patch.old_path, file_patch.why))
            plan.mode = 'all'
            return plan
    test_ids = tables.meta['test_ids']
    file_needs_fresh = False
    fresh_files = 0
    for file_patch in file_patches:
        fresh_files += file_needs_fresh
        file_needs_fresh = False
        checkpoints.append(_plan_counts(plan, fresh_files, infected_files))
        path = file_patch.new_path if file_patch.status != 'delete' else file_patch.old_path
        old_text = None
        if file_patch.status in ('modify', 'delete', 'rename', 'copy'):
            try:
                with open(os.path.join(settings.REPO, file_patch.old_path), encoding='utf-8', newline='') as handle:
                    old_text = handle.read()
            except (OSError, UnicodeDecodeError):
                file_patch.ok = False
        if file_patch.status in ('add', 'rename', 'copy') and os.path.lexists(os.path.join(settings.REPO, path)):
            file_patch.ok = False
        if file_patch.ok:
            patching.apply_file_patch(file_patch, old_text)
        if not file_patch.ok:
            plan.notes.append(f'cannot apply {path}')
            plan.mode = 'all'
            return plan
        if file_patch.status == 'copy':
            file_patch.status = 'add'
            file_patch.old_path = path
            old_text = None
        if file_patch.status == 'rename':
            plan.files[file_patch.old_path] = None
        plan.files[path] = file_patch.new_text
        is_python = path.endswith('.py')
        in_packages = path.startswith(('sqlglot/', 'tests/'))
        encoding_changes_text = False
        decoded = python_text(file_patch.new_text) if is_python and in_packages and (file_patch.status != 'delete') else file_patch.new_text
        if decoded is not None and decoded != file_patch.new_text and (old_text is None or python_text(old_text) == old_text):
            file_patch.groups = line_groups(old_text or '', decoded)
            file_patch.new_text = decoded
            plan.notes.append('source encoding of %s: planned on the decoded text' % path)
        elif decoded != file_patch.new_text or (is_python and in_packages and (old_text is not None) and (python_text(old_text) != old_text)):
            encoding_changes_text = True
            file_needs_fresh = True
            plan.affected_lines['?'] = None
            record = tables.file(file_patch.old_path) if file_patch.old_path else None
            if record is not None:
                plan.candidates.update(tables.tests(record[7]))
                plan.candidates.update(tables.tests(record[8]))
            if path.startswith('tests/'):
                plan.candidates.update((number for number, test_id in enumerate(test_ids) if test_id.split('::')[0] in (path, file_patch.old_path)))
            plan.notes.append('source encoding changes the text of %s' % path)
        if len(file_patches) == 1 and file_patch.status == 'modify' and (not encoding_changes_text) and patch_heuristics.is_inert_function_edit(path, old_text, file_patch.new_text):
            plan.blind_tests.update(tables.tests(tables.meta['opened'].get(path, 0)))
            record = tables.file(path)
            if record is not None:
                plan.blind_tests.update((number for number in tables.tests(record[8]) if '::test_generator_memory' in test_ids[number]))
            plan.notes.append('inert optimizer body edit')
            plan.mode = 'blind' if plan.blind_tests else 'noop'
            return plan
        if is_python and path.startswith('tests/') and (file_patch.status != 'delete'):
            record_added_tests(plan, tables, path, old_text if file_patch.status != 'add' else None, file_patch.new_text)
        pytest_tests = patch_heuristics.pytest_config_scope(tables, file_patch, path, old_text)
        if pytest_tests is not None:
            plan.blind_tests.update(pytest_tests)
            plan.notes.append('pytest file %s: %d tests' % (path, len(pytest_tests)))
            continue
        if file_patch.status in ('add', 'rename') and patch_heuristics.shadows_module(path):
            plan.blind_tests.update(range(len(test_ids)))
            plan.notes.append('shadows a module')
        if is_python and in_packages:
            referencing = None
            if file_patch.status == 'add':
                stem = os.path.basename(path)[:-3]
                module_word = os.path.basename(os.path.dirname(path)) if stem == '__init__' else stem
                referencing = patch_heuristics.referencing_tests(tables, module_word)
                if referencing is not None:
                    plan.candidates.update(referencing)
                    plan.notes.append('new module, %d referencing tests' % len(referencing))
                    if referencing:
                        file_needs_fresh = True
                        plan.affected_lines['?'] = None
            if encoding_changes_text:
                pass
            elif file_patch.status != 'modify' and referencing is None:
                file_needs_fresh = True
                plan.affected_lines['?'] = None
                record = tables.file(file_patch.old_path) if file_patch.old_path else None
                if record is not None:
                    plan.candidates.update(tables.tests(record[7]))
                    plan.candidates.update(tables.tests(record[8]))
            elif file_patch.status == 'modify' and (plan_with_infection(plan, file_patch, tables, old_text, infected_files) if use_infect else plan_python_file(plan, file_patch, tables, old_text)):
                file_needs_fresh = True
                plan.affected_lines.pop(file_patch.old_path, None)
                record = tables.file(file_patch.old_path)
                if record is not None:
                    add_import_time_candidates(plan, file_patch, record, tables)
                gone_names, gone_tests = patch_heuristics.removed_name_tests(tables, file_patch, old_text)
                if gone_names:
                    plan.gone.update(gone_names)
                    plan.candidates.update(gone_tests)
                    plan.affected_lines['?'] = None
                    plan.notes.append('names gone %s: %d tests' % (sorted(gone_names)[:3], len(gone_tests)))
            if os.path.basename(path) == '__init__.py' and path.startswith('tests/'):
                plan.candidates.update(patch_heuristics.tests_under(tables, os.path.dirname(path)))
                plan.affected_lines['?'] = None
        if is_python and path.startswith('sqlglot/') and (file_patch.status == 'modify') and (not encoding_changes_text):
            record = tables.file(file_patch.old_path)
            containers = shared_write_watch(record[0], old_text, file_patch.new_text, file_patch.groups) if record is not None else []
            if containers:
                module_name = path[:-3].replace('/', '.').removesuffix('.__init__')
                plan.watch_specs.extend(((module_name, container) for container in containers))
                plan.notes.append('watch shared containers %s' % ['.'.join(container[1:]) for container in containers][:4])
        for touched in {file_patch.old_path, path}:
            if touched:
                readers_set = tables.meta['opened'].get(touched)
                if readers_set:
                    plan.candidates.update(tables.tests(readers_set))
                    plan.data_candidates.update(tables.tests(readers_set))
        if file_patch.status != 'modify':
            listers_set = tables.meta['listed'].get(os.path.dirname(path))
            if listers_set:
                listing_tests = patch_heuristics.directory_listing_tests(tables)
                for number in tables.tests(listers_set):
                    if listing_tests is None or number in listing_tests:
                        test_file = test_ids[number].split('::')[0]
                        scans_directory = listing_tests is not None and patch_heuristics.DIRECTORY_SCAN_RE.search(patch_heuristics.read_source(test_file))
                        (plan.blind_tests if scans_directory else plan.candidates).add(number)
                        plan.data_candidates.add(number)
        if is_python and (not (path.startswith('sqlglot/') and file_patch.status == 'modify')):
            plan.diffable = False
    fresh_files += file_needs_fresh
    needs_fresh = fresh_files > 0
    if infected_files:
        checkpoints.append(_plan_counts(plan, fresh_files, infected_files))
        for before, after in zip(checkpoints, checkpoints[1:]):
            if before[:6] != after[:6] and before[6] == after[6]:
                # a file the infection analysis did not narrow changed the plan: plan without it
                return make_plan(commit, text, tables, use_infect=False)
        kinds = {kind for _, kind in plan.infection_kinds}
        if 'import' in kinds and 'other' not in kinds:
            plan.infected_after_import = set().union(*(tests for tests, _ in plan.infection_kinds))
    add_registry_candidates(plan, file_patches, tables)
    if plan.added_tests:
        needs_fresh = True
    if needs_fresh:
        plan.mode = 'fresh'
    elif plan.work or plan.candidates - plan.blind_tests:
        plan.mode = 'hot'
    elif plan.blind_tests:
        plan.mode = 'blind'
    return plan


def _plan_counts(plan, fresh_files, infected_files):
    return (len(plan.candidates), len(plan.work), fresh_files, len(plan.blind_tests), len(plan.data_candidates), len(plan.gone), len(infected_files))


# --- the dialect registry --------------------------------------------------------

_REGISTRY_CLEAR_CACHE = {}


def registry_clear_at(test_ids):
    """The number of the first test after the last dialect entry-point test (which clears
    the dialect registry), or None."""
    cache_key = (id(test_ids), len(test_ids))
    cached = _REGISTRY_CLEAR_CACHE.get(cache_key)
    if cached is not None and cached[0] is test_ids:
        return cached[1]
    last = None
    for number, test_id in enumerate(test_ids):
        if test_id.startswith('tests/test_dialect_entry_points.py::'):
            last = number
    _REGISTRY_CLEAR_CACHE[cache_key] = (test_ids, None if last is None else last + 1)
    return _REGISTRY_CLEAR_CACHE[cache_key][1]


def add_registry_candidates(plan: Plan, file_patches, tables) -> None:
    """A change to Dialect's registry methods also affects tests run after the registry is cleared."""
    clear_at = registry_clear_at(tables.meta['test_ids'])
    if clear_at is None:
        return
    for file_patch in file_patches:
        if file_patch.old_path != 'sqlglot/dialects/dialect.py' or file_patch.status != 'modify':
            continue
        try:
            with open(os.path.join(settings.REPO, file_patch.old_path), encoding='utf-8') as handle:
                spans = registry_spans(handle.read())
        except (OSError, SyntaxError, ValueError):
            return
        touches_registry = False
        for old_first, removed_count, _, _, _, _ in file_patch.groups:
            first, last = (old_first, old_first + max(removed_count, 1) - 1)
            if any((start <= last and first <= end for start, end in spans)):
                touches_registry = True
        if not touches_registry:
            continue
        record = tables.file(file_patch.old_path)
        if record is None:
            return
        later_tests = set()
        for start, end in spans:
            for line in range(start, end + 1):
                if line < len(record[3]):
                    later_tests.update((number for number in tables.tests(record[3][line]) if number >= clear_at))
        plan.candidates.update(later_tests)
        plan.notes.append('registry: %d tests after the clear' % len(later_tests))


# --- re-importing only a cone of modules ------------------------------------------

def cone_plan(plan: Plan) -> bool:
    """Make a fresh plan re-import only the module cone its files belong to, if that is safe.

    Requires every changed file to be an existing sqlglot module listed in the
    cone map, decodable as-is and with unchanged import statements, and every
    cone module to be imported already. Sets `plan.cone_modules`.
    """
    cone_map = settings.CONE_MAP
    if cone_map is None or plan.mode != 'fresh' or plan.added_tests or (not plan.files):
        return False
    cone = set()
    for rel, text in plan.files.items():
        if text is None or not rel.endswith('.py') or (not rel.startswith('sqlglot/')):
            return False
        module_name = rel[:-3].replace('/', '.').removesuffix('.__init__')
        if module_name not in cone_map:
            return False
        if python_text(text) != text:
            return False
        try:
            with open(os.path.join(settings.REPO, rel), encoding='utf-8') as handle:
                old_text = handle.read()
            if settings.reimport_cone.import_statements(old_text) != settings.reimport_cone.import_statements(text):
                return False
        except (OSError, SyntaxError, ValueError):
            return False
        cone.update(cone_map[module_name])
    loaded = [name for name in sys.modules if name in cone]
    if len(loaded) != len(cone):
        return False
    plan.cone_modules = loaded
    return True
