"""Narrowing a plan: reduced test bodies, the import-state diff, costs and watched containers.

A reduction of test `n` is (keep_stmts, keep_files, keep_tasks, scaled, needed_tasks):
the statements of the test body to keep, the fixture entries to keep per fixture
file, the task indices to keep, whether the body is scaled down (SCALED_IDS),
and the profiled tasks the change can reach (None when unknown).
"""
import os
from array import array

import settings
from profile_tables import BASE_PATTERN

# the reduction of a test that is only scaled down
ONLY_SCALED = (None, None, None, True, None)
_FIXTURES_DIR = 'tests/fixtures/'


def compute_reductions(plan, tables, tests):
    """{test number: reduction} for the candidates whose body can run reduced."""
    reductions = {}
    if plan.mode not in ('hot', 'fresh', 'fresh_purge', 'fresh_cone'):
        return reductions
    for number in plan.candidates:
        if tests[number]['id'] in settings.SCALED_IDS:
            reductions[number] = ONLY_SCALED
    if any((rel.startswith('tests/') and rel.endswith('.py') for rel in plan.files)):
        return reductions
    if plan.mode != 'hot' and (None in plan.affected_lines.values() or not plan.affected_lines):
        return reductions
    if plan.watch_specs:
        return reductions
    changed_lines = [(rel, line) for rel, lines in plan.affected_lines.items() for line in lines]
    fixture_readers = {}
    for rel in plan.files:
        if rel.startswith(_FIXTURES_DIR):
            readers_set = tables.meta['opened'].get(rel)
            fixture_readers[rel[len(_FIXTURES_DIR):]] = set(tables.tests(readers_set)) if readers_set else set()
    for number in plan.candidates:
        profile = tables.test_units(number)
        if profile is None:
            continue
        tasks, patterns = (profile[3], profile[4])
        task_classes = profile[8] if len(profile) > 8 else None
        base_classes = profile[9] if len(profile) > 9 else None
        class_filter = plan.class_filter if task_classes is not None else None
        needed_tasks = set()
        if changed_lines:
            reached = False
            needs_whole_body = False
            for rel, line in changed_lines:
                pattern = tables.unit_entries(rel, line).get(number)
                if pattern is None:
                    continue
                reached = True
                unfiltered = class_filter is None or (rel, line) in plan.plain_lines
                if pattern == BASE_PATTERN:
                    if unfiltered or base_classes is None or (not class_filter.isdisjoint(base_classes)):
                        needs_whole_body = True
                        break
                    continue
                if unfiltered:
                    needed_tasks.update(patterns[pattern])
                else:
                    for task in patterns[pattern]:
                        classes = task_classes[task]
                        if classes is None or not class_filter.isdisjoint(classes):
                            needed_tasks.add(task)
            if needs_whole_body:
                continue
            if not reached and (plan.work or plan.mode != 'hot'):
                continue
        keep_stmts = set()
        keep_files = {}
        keep_tasks = set() if any((task[0] == 't' for task in tasks)) else None
        for task in needed_tasks:
            kind, fixture, index, statement = tasks[task]
            if kind == 's':
                keep_stmts.add(index)
            elif kind == 'f':
                keep_files.setdefault(fixture, set()).add(index)
            else:
                keep_tasks.add(index)
            if statement >= 0:
                keep_stmts.add(statement)
        fixtures_covered = True
        for fixture, readers in fixture_readers.items():
            if number in readers:
                fixture_statements = {task[3] for task in tasks if task[0] == 'f' and task[1] == fixture}
                if not fixture_statements:
                    fixtures_covered = False
                    break
                keep_stmts.update((statement for statement in fixture_statements if statement >= 0))
        if not fixtures_covered:
            continue
        if changed_lines and (not needed_tasks) and (not keep_stmts) and (not fixture_readers) and (plan.class_filter is not None):
            plan.unaffected.add(number)
            continue
        reductions[number] = (keep_stmts, keep_files, keep_tasks, number in reductions, needed_tasks)
    return reductions


def classmap_readers(rows, tables):
    """Tests that read the changed class attributes in `rows`, or None if the class map cannot tell."""
    class_map = tables.classmap()
    if class_map is None or not rows:
        return None
    classes = class_map['classes']
    readers = set(class_map['unknown'])
    receivers = None
    for key, is_function in (row[:2] for row in rows):
        if is_function:
            return None
        if key[0] == 'A' and len(key) == 4:
            owner, attribute = ('%s:%s' % (key[1], key[2]), key[3])
        elif key[0] == 'O' and len(key) == 5:
            owner, attribute = ('%s:%s' % (key[1], key[2]), key[3])
        else:
            return None
        if not isinstance(attribute, str) or attribute[:2] == '__' or attribute in class_map['fnames']:
            return None
        if owner not in class_map['hooked']:
            return None
        if receivers is None:
            receivers = []
            for packed in class_map['receivers']:
                class_indices = array('I')
                class_indices.frombytes(packed)
                receivers.append({classes[class_index] for class_index in class_indices})
        packed = class_map['names'].get(attribute)
        if not packed:
            continue
        pairs = array('I')
        pairs.frombytes(packed)
        for position in range(0, len(pairs), 2):
            if owner in receivers[pairs[position]]:
                tests = array('H')
                tests.frombytes(class_map['sets'][pairs[position + 1]])
                readers.update(tests)
    return readers


def _changes_def_header(plan):
    """True if a changed function unit starts with a def/class/decorator line (or cannot be read)."""
    for rel, lines in plan.def_units.items():
        try:
            with open(os.path.join(settings.REPO, rel), encoding='utf-8') as handle:
                text_lines = handle.read().split('\n')
        except OSError:
            return True
        for line in lines:
            if 0 < line <= len(text_lines) and text_lines[line - 1].lstrip().startswith(('def ', 'async def ', '@', 'class ')):
                return True
    return False


def classmap_cut(plan, rows, tables):
    if not rows or settings.NO_CLASSMAP or plan.fpairs or plan.stmt_lines or plan.gone or plan.work or _changes_def_header(plan):
        return None
    return classmap_readers(rows, tables)


def refine_with_diff(plan, rows, tables, tests) -> None:
    """Narrow a fresh plan with the import-state diff its child reported.

    `rows` are [key, is_function, changed entries] per changed state key, or
    None when there were too many changes.
    """
    if rows is None:
        plan.notes.append('import state: many changes')
        plan.affected_lines = {'?': None}
        return
    changed_names = set()
    unrefinable = bool(plan.gone)
    owners = set()
    for row in rows:
        key, is_function = (row[0], row[1])
        owners.add('%s.%s' % (key[1], key[2]) if key[0] == 'A' else None)
        name = key[-1]
        if name == '__bases__' or not isinstance(name, str):
            unrefinable = True
        elif is_function:
            if not plan.def_units:
                unrefinable = True
        else:
            changed_names.add(name)
    plan.changed_names = sorted(changed_names)
    if not unrefinable:
        readers = classmap_cut(plan, rows, tables)
        if readers is not None:
            count_before = len(plan.candidates)
            plan.candidates &= readers | plan.data_candidates
            plan.notes.append('class map: %d -> %d' % (count_before, len(plan.candidates)))
    if not unrefinable and (not settings.NO_KEYMAP):
        readers = settings.registry_keys.tests_reading_changed_entries(plan, rows, settings.TABLES, settings.REPO)
        if readers is not None:
            count_before = len(plan.candidates)
            plan.candidates &= readers | plan.data_candidates
            plan.notes.append('key map: %d -> %d' % (count_before, len(plan.candidates)))
    if not rows and (not plan.fpairs):
        plan.notes.append('import state unchanged')
        plan.affected_lines = {'?': None}
        plan.reductions = compute_reductions(plan, tables, tests)
        return
    reader_lines = {}
    for name in changed_names:
        readers = tables.readers(name)
        if not readers or len(readers) > 600:
            unrefinable = True
            break
        for rel, line in readers:
            reader_lines.setdefault(rel, set()).add(line)
    if unrefinable or not settings.FRESH_REDUCE:
        plan.notes.append('import state: no reduced runs')
        plan.affected_lines = {'?': None}
        plan.reductions = compute_reductions(plan, tables, tests)
        return
    affected = {}
    for rel, lines in reader_lines.items():
        record = tables.file(rel)
        statement_lines = affected.setdefault(rel, set())
        for line in lines:
            if record is not None and line < len(record[9]) and record[9][line]:
                statement_lines.update(range(record[9][line], record[10][line] + 1))
            else:
                statement_lines.add(line)
    for lines_by_file in (plan.stmt_lines, plan.def_units):
        for rel, lines in lines_by_file.items():
            affected.setdefault(rel, set()).update(lines)
            plan.plain_lines.update(((rel, line) for line in lines))
    if owners and None not in owners and settings.CLASS_FILTER:
        class_users = set()
        for owner in owners:
            users = tables.class_users(owner)
            if users is None:
                class_users = None
                break
            class_users.update(users)
        plan.class_filter = class_users
    plan.affected_lines = affected
    plan.notes.append('import state: %d names' % len(changed_names))
    if plan.infected_after_import is not None and (not rows) and (not plan.gone) and (not plan.added_tests) and settings.INFECT_AFTER_IMPORT:
        count_before = len(plan.candidates)
        plan.candidates &= plan.infected_after_import | plan.data_candidates
        plan.notes.append('infect after import: %d -> %d' % (count_before, len(plan.candidates)))
    plan.reductions = compute_reductions(plan, tables, tests)
    if plan.unaffected:
        plan.candidates -= plan.unaffected
        plan.notes.append('unaffected %d' % len(plan.unaffected))


def estimate_cost(number, plan, tables, seconds):
    """Expected CPU seconds of running test `number` in this plan's child (reductions applied)."""
    full_cost = tables.cost[number] if tables.cost else seconds[number] * 1.5
    if number in plan.fixture_users:
        return min(full_cost, 0.05 + 0.05 * full_cost)
    reduction = plan.reductions.get(number)
    if reduction is None:
        return full_cost
    scaled, needed_tasks = (reduction[3], reduction[4])
    if needed_tasks is None:
        return min(full_cost, 0.15) if scaled else full_cost
    profile = tables.test_units(number)
    profiled_total = profile[5] or 0.0
    if profiled_total <= 0:
        return full_cost
    base_cost = profile[6]
    reduced_total = base_cost + sum((max(0.0, profile[7][task] - base_cost) for task in needed_tasks))
    estimate = full_cost * min(1.0, reduced_total / profiled_total)
    return min(estimate, 0.15) if scaled else estimate


def watch_readers(plan, verdicts, test_count):
    """Tests after a watched container changed that read it (by the key map), to be selected."""
    registry_keys = settings.registry_keys
    key_map = registry_keys.load_key_map(settings.TABLES) if registry_keys is not None else None
    # (test number after which, container key, changed entries, observed while testing)
    changes = [(number, key, entries, True) for number, key, entries in plan.container_leaks]
    not_run = sorted((number for number in plan.candidates if number not in verdicts))
    if not_run:
        if plan.watched_keys is None:
            changes.append((not_run[0], None, None, False))
        for key in plan.watched_keys or ():
            changes.append((not_run[0], key, None, False))
    attribute_holders = {}
    if key_map is not None:
        for holder in key_map.holders:
            parts = holder.split('|')
            if parts[0] == 'A':
                attribute_holders.setdefault(parts[-1], []).append(holder)
    readers = set()
    for after, key, entries, observed in changes:
        own_readers = other_readers = None
        if key_map is not None and key is not None:
            key = tuple(key)
            own_holder = registry_keys.holder_name(key)
            own_readers, other_readers = (set(key_map.unknown), set())
            for holder in [own_holder] + (sorted(set(attribute_holders.get(key[-1], ())) - {own_holder}) if key[0] == 'A' else []):
                table_index = key_map.holders.get(holder)
                if table_index is None:
                    if holder == own_holder:
                        own_readers = None
                        break
                    continue
                table = key_map.tables[table_index]
                target = own_readers if holder == own_holder else other_readers
                for entry in [registry_keys.WHOLE_CONTAINER] + list(entries) if entries is not None else list(table):
                    set_id = table.get(entry)
                    if set_id is not None:
                        target.update(key_map.tests(set_id))
        if own_readers is None:
            readers.update(range(after + 1, test_count))
            continue
        readers.update((number for number in own_readers if number > after and (not (observed and number in verdicts))))
        readers.update((number for number in other_readers if number > after))
    if readers:
        plan.notes.append('watched container changed: %d later readers' % len(readers))
    return readers
