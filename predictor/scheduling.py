"""Per-commit execution: which candidates to run or hold, running them, and the final answer.

Candidates are split into tests run in the commit's child ("ran") and tests
"held": selected without running when a related test fails (or when they are
cheap enough that selecting them costs little). The answer of a commit is the
failed tests plus the held tests the results cannot clear, packed into workers.
"""
import collections
import json
import os
import sys
import time

import patch_heuristics
import settings
from child_runs import run_child
from fixture_batches import run_fixture_batch_child
from packing import pack
from refinement import ONLY_SCALED, estimate_cost, refine_with_diff, watch_readers
from state_checks import resolve_watches

_FIXTURES_DIR = 'tests/fixtures/'


class Scheduler:
    """Runs commits' plans and records their answers in `answers`."""

    def __init__(self, tables, tests, plans, answers, numbering, deadline):
        self.tables = tables
        self.tests = tests
        self.seconds = [test['seconds'] for test in tests]
        self.all_tests = list(range(len(tests)))
        self.plans = plans
        self.answers = answers
        self.numbering = numbering      # our test number -> the input's number (None: the same)
        self.deadline = deadline
        self.debug_rows = {}
        self.function_index = None
        self.tests_of_file = {}
        for number, test in enumerate(tests):
            self.tests_of_file.setdefault(test['file'], []).append(number)
        self._fixture_readers = {}
        self.shared_info = []           # harness_env.shared_class_info(), loaded on first use
        self.attribute_reads = {}       # test id -> class attributes it reads
        self.prepared = {}              # commit -> prepare_plan() result (fixture batches)

    # --- helpers -----------------------------------------------------------------------

    def class_of(self, number):
        return self.tests[number]['id'].rsplit('::', 1)[0]

    def fixtures_of(self, number):
        """The SQL fixture files test `number` reads."""
        if not self._fixture_readers:
            for rel, set_id in self.tables.meta['opened'].items():
                if rel.startswith(_FIXTURES_DIR) and rel.endswith('.sql'):
                    for reader in self.tables.tests(set_id):
                        self._fixture_readers.setdefault(reader, set()).add(rel)
            self._fixture_readers.setdefault(-1, set())
        return self._fixture_readers.get(number) or frozenset()

    def cost_of(self, number, plan):
        return estimate_cost(number, plan, self.tables, self.seconds)

    def small_plan(self, plan):
        """A fresh plan whose candidates cost too little for the import-state diff to pay off."""
        if settings.RUN_ALL or plan.mode == 'hot':
            return False
        cost = self.tables.cost
        return sum((cost[number] if cost else self.seconds[number] * 1.5 for number in plan.candidates - plan.blind_tests)) <= settings.SMALL_PLAN_SECONDS

    def planned_cost(self, commit):
        plan = self.plans[commit]
        return sum((min(self.cost_of(number, plan), settings.HOLD_SECONDS * 2) for number in plan.candidates))

    def needs_pandas(self, commit):
        plan = self.plans[commit]
        return any((number in settings.PANDAS_USERS for number in plan.candidates - plan.blind_tests))

    # --- the answer ------------------------------------------------------------------------

    def finish(self, commit, plan, verdicts, status, ran, held, blind=()):
        """Decide and record the answer of `commit` from its verdicts."""
        tests, seconds = (self.tests, self.seconds)
        failed = {number for number, test_failed in verdicts.items() if test_failed}
        selected = set(failed)
        selected.update(blind)
        selected.update(plan.blind_tests)
        unanswered = [number for number in ran if number not in verdicts and number not in set(blind)]
        if unanswered:
            selected.update(unanswered)
        if plan.all_fail:
            selected = set(self.all_tests)
        if 'import state unchanged' in plan.notes and (not failed) and (not plan.broken_modules) and (not unanswered):
            pass
        elif plan.broken_modules or not verdicts or unanswered:
            selected.update(held)
        else:
            failed_classes = {self.class_of(number) for number in failed}
            failed_fixtures = set()
            for number in failed:
                failed_fixtures.update(self.fixtures_of(number))
            for number in held:
                if seconds[number] <= settings.QUIET_HOLD_SECONDS:
                    selected.add(number)
                elif self.class_of(number) in failed_classes or failed_fixtures & self.fixtures_of(number):
                    selected.add(number)
                elif len(failed) >= settings.MANY_FAILURES:
                    selected.add(number)
        selected.update((number for number in held if number in plan.must_select))
        selected.update((number for number in held if number in plan.sure_infected and seconds[number] > settings.QUIET_HOLD_SECONDS))
        selected.update((number for number in held if tests[number]['id'] in settings.SCALED_IDS))
        if plan.watch_specs:
            selected.update(watch_readers(plan, verdicts, len(tests)))
        if plan.leak_from is not None:
            selected.update(range(plan.leak_from, len(tests)))
            plan.notes.append('shared state or interpreter settings changed in test %d' % plan.leak_from)
        for number in plan.pool_deaths:
            test_class = self.class_of(number)
            for user in self.tables.tests(self.tables.meta.get('pool_users', 0)):
                if user > number and self.class_of(user) == test_class:
                    selected.add(user)
        selected.update(patch_heuristics.importer_tests(self.tables, plan.broken_modules))
        for module_name in plan.broken_modules:
            selected.update(self.tests_of_file.get(module_name.replace('.', '/') + '.py', ()))
        self.answers[commit] = pack(sorted(selected), seconds)
        if settings.DEBUG:
            self._debug_row(commit, plan, verdicts, status, ran, held)

    def _debug_row(self, commit, plan, verdicts, status, ran, held):
        row = self.debug_rows[commit] = {'mode': plan.mode, 'status': status, 'cands': sorted(plan.candidates), 'cpu': round(sum(os.times()[:4]), 3), 'verdicts': {str(number): int(test_failed) for number, test_failed in verdicts.items()}, 'note': plan.notes, 'reduced': sorted(plan.reductions), 'held': sorted(held), 'ran': len(ran), 'diff': plan.changed_names, 'must': sorted(plan.must_select), 'sure': sorted(plan.sure_infected.intersection(held)), 'tokcheck': plan.token_checks, 'times': {str(number): round(cpu, 4) for number, cpu in plan.test_times.items()}, 'costs': {str(number): round(self.cost_of(number, plan), 4) for number in ran}, 'est': round(sum((self.cost_of(number, plan) for number in ran)), 3), 'cone': len(plan.cone_modules) if plan.mode == 'fresh_cone' else 'redo' if plan.cone_modules == [] else None, 'dkeys': settings.DEBUG_DIFF_KEYS.get(commit)}
        if plan.watch_specs:
            row['leaks'] = plan.container_leaks[:20]
            row['watched'] = len(plan.watched_keys or ())

    # --- choosing what to run ------------------------------------------------------------------

    def choose(self, plan):
        """Split the candidates into (ran, held, cost per candidate)."""
        tests, seconds = (self.tests, self.seconds)
        if plan.watch_specs and (not resolve_watches(plan.watch_specs)):
            plan.notes.append('no shared container to watch')
            plan.watch_specs = []
        runnable = sorted(plan.candidates - plan.blind_tests)
        prefilter_tests = settings.RESTRICT.get(plan.commit) if settings.RESTRICT is not None and settings.PREFILTER_TRIM and (not settings.RUN_ALL) else None
        if prefilter_tests is not None:
            numbering = self.numbering
            if settings.PREFILTER_CUT:
                kept = [number for number in runnable if (number if numbering is None else numbering[number]) in prefilter_tests]
                if len(kept) < len(runnable):
                    plan.notes.append('static cut: %d -> %d runs' % (len(runnable), len(kept)))
                    plan.candidates.difference_update(set(runnable) - set(kept))
                    runnable = kept
            last_kept = max((number for number in runnable if (number if numbering is None else numbering[number]) in prefilter_tests), default=-1)
            if runnable and runnable[-1] > last_kept:
                plan.notes.append('static cut: %d -> %d runs' % (len(runnable), sum((1 for number in runnable if number <= last_kept))))
                plan.candidates.difference_update((number for number in runnable if number > last_kept))
                runnable = [number for number in runnable if number <= last_kept]
        cost = {number: self.cost_of(number, plan) for number in runnable}
        if plan.mode != 'hot' and 'import state unchanged' not in plan.notes:
            for class_prefix, setup_seconds in settings.CLASS_SETUP_SECONDS.items():
                members = [number for number in runnable if tests[number]['id'].startswith(class_prefix + '::')]
                for number in members:
                    cost[number] += setup_seconds / len(members)
        if settings.RUN_ALL:
            return ([number for number in runnable if cost[number] <= 60 or settings.RUN_SLOW], [], cost)
        hold_limit = {number: max(settings.HOLD_SECONDS, settings.HOLD_FACTOR * (settings.WORKER_COST + seconds[number])) for number in runnable}
        ran = [number for number in runnable if cost[number] <= hold_limit[number]]
        held = [number for number in runnable if cost[number] > hold_limit[number]]
        if plan.watch_specs:
            ran = [number for number in runnable if cost[number] <= hold_limit[number] or seconds[number] <= settings.QUIET_HOLD_SECONDS]
            held = [number for number in runnable if not (cost[number] <= hold_limit[number] or seconds[number] <= settings.QUIET_HOLD_SECONDS)]
        budget = settings.COMMIT_SECONDS if plan.mode == 'hot' else settings.FRESH_SECONDS
        if 'import state unchanged' in plan.notes:
            budget = settings.INERT_SECONDS
        if plan.watch_specs:
            budget = float('inf')
        long_held = [number for number in held if seconds[number] > settings.QUIET_HOLD_SECONDS]
        long_held_classes = {self.class_of(number) for number in long_held}
        long_held_fixtures = set()
        for number in long_held:
            long_held_fixtures.update(self.fixtures_of(number))
        plan.protected = {number for number in ran if self.class_of(number) in long_held_classes or (long_held_fixtures and long_held_fixtures & self.fixtures_of(number) and (cost[number] <= 2.0))}
        if sum((cost[number] for number in ran)) > budget:
            within_budget = set()
            spent = 0.0
            for number in sorted(ran, key=lambda number: (number not in plan.protected and tests[number]['id'] not in settings.SCALED_IDS, -(seconds[number] + 0.002) / (cost[number] + 0.002), cost[number])):
                if spent + cost[number] > budget and tests[number]['id'] not in settings.SCALED_IDS:
                    continue
                spent += cost[number]
                within_budget.add(number)
            held += [number for number in ran if number not in within_budget]
            ran = [number for number in ran if number in within_budget]
        return (ran, held, cost)

    def class_fixture_rule(self, plan):
        """Fixture-file edits read by shared class fixtures: candidates that only read the
        refreshed attributes become cheap `fixture_users`; ones reading none of them are dropped."""
        import harness_env
        changed_fixtures = {rel for rel, text in plan.files.items() if rel.startswith(_FIXTURES_DIR) and text is not None}
        if not changed_fixtures or len(changed_fixtures) != sum((1 for rel in plan.files if rel.startswith(_FIXTURES_DIR))):
            return
        if not self.shared_info:
            self.shared_info.extend(harness_env.shared_class_info(settings.REPO) or [None])
            try:
                with open(os.path.join(settings.TABLES, 'class_attr_reads.json')) as handle:
                    self.attribute_reads.update(json.load(handle))
            except (OSError, ValueError):
                pass
        for info in self.shared_info:
            if info is None:
                continue
            test_prefix, _, attribute_by_fixture, other_reads = info
            refreshed = changed_fixtures & set(attribute_by_fixture)
            if not refreshed or changed_fixtures & other_reads:
                continue
            for number in sorted(plan.candidates):
                test_id = self.tests[number]['id']
                if not test_id.startswith(test_prefix):
                    continue
                reads = self.attribute_reads.get(test_id)
                if reads is None:
                    continue
                if any((attribute_by_fixture[rel] in reads for rel in refreshed)):
                    plan.fixture_users.add(number)
                elif not (self.fixtures_of(number) & changed_fixtures) - refreshed:
                    plan.candidates.discard(number)

    def prepare_plan(self, plan):
        """choose() plus the run parameters: (ran, held, allowed seconds, skip-after-fail set, cap)."""
        seconds = self.seconds
        ran, held, cost = self.choose(plan)
        plan.token_checks = []
        if settings.TOKEN_CHECK and (not settings.RUN_ALL) and settings.TOKENIZER_FILES.intersection(plan.files):
            import token_check
            checkable = token_check.checkable_tests(settings.TABLES)
            plan.token_checks = sorted((number for number in held if number in checkable and seconds[number] > settings.QUIET_HOLD_SECONDS))
            if plan.watch_specs and plan.token_checks:
                plan.must_select.update(plan.token_checks)
                plan.notes.append('watch: token stream check not run, %s selected' % plan.token_checks)
                plan.token_checks = []
        allowed = {number: max(settings.TEST_TIMEOUT, 25.0 * cost[number]) * settings.SLOWDOWN for number in ran}
        skip_after_fail = set() if settings.RUN_ALL or plan.watch_specs else {number for number in ran if cost[number] > max(0.03, settings.AFTER_FAIL_RATIO * seconds[number]) and number not in plan.protected}
        plan.enders = {}
        long_held = [number for number in held if seconds[number] > settings.QUIET_HOLD_SECONDS]
        if long_held and (not settings.RUN_ALL) and (not plan.watch_specs) and (max((seconds[number] for number in long_held)) >= sum((seconds[number] for number in ran))):
            # once failures decide every long held test, the rest of the run can stop
            plan.enders['held'] = set(long_held)
            cost_left = sum((cost[number] for number in ran))
            for number in ran:
                cost_left -= cost[number]
                number_class, number_fixtures = (self.class_of(number), self.fixtures_of(number))
                decides = {held_number for held_number in long_held if self.class_of(held_number) == number_class or number_fixtures & self.fixtures_of(held_number)}
                if decides:
                    plan.enders[number] = (decides, cost_left > settings.WORKER_COST)
        cap = None
        if ran and (not settings.RUN_ALL):
            cap = settings.SLOWDOWN * (2.0 + 3.0 * sum((cost[number] for number in ran))) + max(allowed.values())
        return (ran, held, allowed, skip_after_fail, cap)

    # --- running -------------------------------------------------------------------------------

    def execute(self, commit, plan, function_index):
        """Run one commit's child and record its answer; returns 'REDO' if the plan must run fresh."""
        tests = self.tests
        chosen = {}
        if plan.mode == 'hot' and (not plan.work) and (not settings.RUN_ALL):
            self.class_fixture_rule(plan)

        def prepare():
            ran, held, allowed, skip_after_fail, cap = self.prepare_plan(plan)
            chosen['ran'], chosen['held'] = (ran, held)
            return (ran, allowed, skip_after_fail, cap)
        time_left = self.deadline - time.monotonic()
        if time_left < 3.0:
            if plan.mode != 'hot' and any((rel.endswith('.py') for rel in plan.files)):
                plan.all_fail = True
            ran, held, _ = self.choose(plan)
            self.finish(commit, plan, {}, 'DEADLINE', ran, held)
            return
        use_diff = plan.mode != 'hot' and plan.diffable and (settings.BASE_STATE is not None) and (not settings.NO_DIFF)
        if use_diff and self.small_plan(plan):
            use_diff = False
        if plan.mode != 'hot' and (not use_diff):
            plan.reductions = {number: spec for number, spec in plan.reductions.items() if spec == ONLY_SCALED}
            plan.affected_lines = {'?': None}
        if use_diff:

            def on_diff(rows):
                refine_with_diff(plan, rows, self.tables, tests)
                order, allowed, skip_after_fail, cap = prepare()
                return {'order': order, 'reductions': plan.reductions, 'skip_after_fail': skip_after_fail, 'allowed': allowed, 'cap': cap, 'enders': plan.enders, 'token_checks': plan.token_checks}
            verdicts, status, hung = run_child(plan, None, tests, function_index, {}, time_left - 2.0, (), on_diff)
            if status == 'REDO' and 'ran' not in chosen:
                return 'REDO'
            if 'ran' not in chosen:
                ran, held, _ = self.choose(plan)
                if status in ('TIMEOUT', 'DIED'):
                    for rel in plan.files:
                        if rel.endswith('.py'):
                            plan.broken_modules.append(rel[:-3].replace('/', '.'))
                    plan.notes.append('import does not end')
                self.finish(commit, plan, {}, status, ran, held)
                return
        else:
            ran, allowed, skip_after_fail, cap = prepare()
            if not ran:
                if plan.mode == 'hot' and plan.token_checks:
                    run_child(plan, [], tests, function_index, {}, time_left - 2.0)
                elif plan.mode != 'hot' and any((rel.endswith('.py') for rel in plan.files)):
                    _, status, _ = run_child(plan, [], tests, function_index, {}, time_left - 2.0)
                    if status == 'REDO':
                        return 'REDO'
                    if status in ('TIMEOUT', 'DIED'):
                        plan.broken_modules.extend((rel[:-3].replace('/', '.') for rel in plan.files if rel.endswith('.py')))
                        plan.notes.append('import does not end')
                self.finish(commit, plan, {}, 'END', ran, chosen['held'])
                return
            verdicts, status, hung = run_child(plan, ran, tests, function_index, allowed, time_left - 2.0, skip_after_fail, None, cap)
            if status == 'REDO':
                return 'REDO'
            if status in ('TIMEOUT', 'DIED') and hung is None and (not verdicts) and (plan.mode != 'hot'):
                for rel in plan.files:
                    if rel.endswith('.py'):
                        plan.broken_modules.append(rel[:-3].replace('/', '.'))
                plan.notes.append('import does not end')
        self.conclude(commit, plan, verdicts, status, hung, chosen['ran'], chosen['held'], function_index)

    def conclude(self, commit, plan, verdicts, status, hung, ran, held, function_index):
        """Retry after a hung test (at most twice, the hung one counts as failed), then finish."""
        if status == 'OVER':
            plan.notes.append('over the time cap')
        for _ in range(2):
            if hung is None:
                break
            verdicts[hung] = True
            remaining = [number for number in ran if number not in verdicts and number not in plan.skipped]
            time_left = self.deadline - time.monotonic()
            if not remaining or time_left < 3.0:
                break
            allowed = {number: settings.TEST_TIMEOUT * settings.SLOWDOWN for number in remaining}
            more_verdicts, status, hung = run_child(plan, remaining, self.tests, function_index, allowed, time_left - 2.0, set(remaining))
            verdicts.update(more_verdicts)
        selected_unrun = [number for number in ran if number in plan.skipped and number not in verdicts]
        if plan.pool_deaths:
            first_death = min(plan.pool_deaths)
            death_class = self.class_of(first_death)
            pool_users = set(self.tables.tests(self.tables.meta.get('pool_users', 0)))
            selected_unrun += [number for number in ran if number not in verdicts and number > first_death and (number in pool_users) and (self.class_of(number) == death_class)]
        self.finish(commit, plan, verdicts, status, ran, held, selected_unrun)

    def execute_safe(self, commit, plan, function_index):
        """execute(), answering conservatively if it raises."""
        try:
            return self.execute(commit, plan, function_index)
        except Exception as error:
            settings.log(commit, 'execute failed', repr(error))
            if plan.mode == 'hot':
                selected = plan.candidates | plan.blind_tests | patch_heuristics.importer_tests(self.tables, plan.broken_modules)
            else:
                selected = self.all_tests
            self.answers[commit] = pack(sorted(selected), self.seconds)

    # --- hot-mode helpers --------------------------------------------------------------------------

    def exec_prebuild(self, hot_commits):
        """TestExecutor's lazy attributes worth building once before the hot children fork."""
        import harness_env
        executor_class = getattr(sys.modules.get(harness_env.EXEC_KEY[0]), harness_env.EXEC_KEY[1], None)
        attribute_users = harness_env.exec_attribute_users(executor_class) if executor_class is not None else {}
        commits_using = collections.Counter()
        for commit in hot_commits:
            plan = self.plans[commit]
            attributes = set()
            for number in plan.candidates:
                path, class_name, method_name = self.tests[number]['id'].split('::')
                used = attribute_users.get(method_name) if (path[:-3].replace('/', '.'), class_name) == harness_env.EXEC_KEY else None
                if used and (settings.RUN_ALL or self.cost_of(number, plan) <= max(settings.HOLD_SECONDS, settings.HOLD_FACTOR * (settings.WORKER_COST + self.seconds[number]))):
                    attributes.update(used)
            commits_using.update(attributes)
        prebuild = ['tpch_sqls', 'tpcds_sqls']
        prebuild += [name for name in harness_env.EXEC_LAZY if name not in prebuild and commits_using[name] >= 2]
        return prebuild

    def fixture_only(self, commit):
        """True (and the run prepared) if a hot commit only edits SQL fixtures and can join a fixture batch."""
        import harness_env
        plan = self.plans[commit]
        if plan.mode != 'hot' or plan.work or plan.watch_specs or plan.added_tests or plan.blind_tests or plan.must_select or plan.all_fail:
            return False
        if plan.broken_modules or plan.affected_lines or plan.def_units or plan.fpairs or plan.gone or plan.infection_kinds or plan.sure_infected:
            return False
        if not plan.files or not plan.candidates:
            return False
        for rel, text in plan.files.items():
            if text is None or not rel.startswith(_FIXTURES_DIR) or (not rel.endswith('.sql')):
                return False
            if not os.path.isfile(os.path.join(settings.REPO, rel)):
                return False
        self.class_fixture_rule(plan)
        for info in self.shared_info:
            if info is not None and (set(info[2]) | info[3]).intersection(plan.files):
                return False
        executor_prefix = harness_env.EXEC_KEY[0].replace('.', '/') + '.py::' + harness_env.EXEC_KEY[1] + '::'
        if any((self.seconds[number] > settings.QUIET_HOLD_SECONDS or self.tests[number]['id'].startswith(executor_prefix) for number in plan.candidates)):
            return False
        ran, held, allowed, skip_after_fail, cap = self.prepare_plan(plan)
        if not ran or held or plan.token_checks or plan.enders or (set(ran) != plan.candidates):
            return False
        self.prepared[commit] = (ran, held, allowed, skip_after_fail, cap)
        return True

    def run_fixture_batch(self, members):
        """Run the prepared fixture-only commits `members` in one child; returns those answered."""
        time_left = self.deadline - time.monotonic()
        if time_left < 20.0:
            return set()
        batch = []
        for commit in members:
            ran, _, allowed, skip_after_fail, cap = self.prepared[commit]
            batch.append((self.plans[commit], ran, allowed, skip_after_fail, cap))
        results = run_fixture_batch_child(batch, self.tests, time_left - 2.0)
        answered = set()
        for index, commit in enumerate(members):
            if index not in results:
                continue
            verdicts, skipped, test_times = results[index]
            plan = self.plans[commit]
            plan.skipped.update(skipped)
            plan.test_times.update(test_times)
            try:
                self.conclude(commit, plan, verdicts, 'END', None, self.prepared[commit][0], self.prepared[commit][1], self.function_index)
                answered.add(commit)
                if settings.DEBUG:
                    self.debug_rows[commit]['batch'] = len(members)
            except Exception as error:
                settings.log(commit, 'fixture batch: no answer', repr(error))
                plan.skipped.clear()
                plan.test_times.clear()
        settings.log('fixture batch:', len(members), 'commits,', len(answered), 'answered')
        return answered
