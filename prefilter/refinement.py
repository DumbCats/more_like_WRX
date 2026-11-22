"""Optional, time-budgeted refinements of a static selection by actually running code.

drop_tpcds replays the TPC-DS queries an edit can reach and drops the (expensive) executor
test when they all still pass; narrow_with_replay runs cheap unit tests on the patched
sources and keeps only the failing ones. The replay modules (multiprocessing, unittest, ...)
are imported only when a budget is actually spent, before its clock starts.
"""
import os
import sys
import time

import tpcds_profile
import tpcds_stages
from scheduling import mask_cost, plan_milliseconds, schedule, schedule_with_margin
from settings import REPO

TPCDS_OVERHEAD = 3.0
TPCDS_SLOWDOWN = 1.3
# Stage gate (tpcds_stages), measured CPU seconds: a safe check (re-import, parse/optimize/
# transpile, a few executions), the fixed part of a full check (re-import, parse/transpile,
# pickled tables) on top of the profiled executions; and the share of the selection saving the
# gate may cost (most selections that keep the test are right, and failing checks are cheap).
SAFE_STAGE_SECONDS = 4.0
FULL_STAGE_OVERHEAD = 1.0
STAGE_GAIN_RATIO = 0.6
# Test id prefixes whose tests can run under plain unittest in the replay child.
REPLAYABLE = (
    'tests/dialects/',
    'tests/test_parser.py::',
    'tests/test_transpile.py::',
    'tests/test_expressions.py::',
    'tests/test_generator.py::',
    'tests/test_typing.py::',
    'tests/test_serde.py::',
    'tests/test_tokens.py::',
    'tests/test_build.py::',
)
MAX_REPLAYED = 120
MAX_REPLAYED_MS = 2200
MIN_SAVING_MS = 650
EXTRA_DIALECT_MS = 250
MAX_WITH_EXTRAS = 48
REPLAY_SECONDS = 2.4


def drop_tpcds(mask, files, skipped, file_masks, maps, numbered, duration, bit, left):
    """Clear the TPC-DS test's bit when replaying its affected queries passes.

    Returns (mask, seconds of TPC-DS budget left).
    """
    profile = maps['tpcds']
    if any(file_masks.get(path, bit) & bit for path in skipped):
        return mask, left
    gain = (mask_cost(mask, numbered, duration) - mask_cost(mask & ~bit, numbered, duration)) / 1000
    stages = maps.get('tpcds_stages')
    if stages and not skipped:
        safe = tpcds_stages.downstream_safe(files, stages)
        estimate = SAFE_STAGE_SECONDS if safe else FULL_STAGE_OVERHEAD + sum(stages['cpu'])
        if estimate <= STAGE_GAIN_RATIO * gain and estimate <= left and all(path.startswith('sqlglot/') and path.endswith('.py') for path, *_rest in files):
            started = time.monotonic()
            verdict, executed = tpcds_stages.still_passes(REPO, files, stages, safe, stages['cpu'], min(left, 2 * estimate + 3), stages.get('tables'))
            left -= time.monotonic() - started
            print(f"tpcds stages ({'safe' if safe else 'full'}): {verdict}, {executed} executed", file=sys.stderr)
            if verdict == 'pass':
                return mask & ~bit, left
            if verdict == 'fail':
                return mask, left
    if os.environ.get('BLAST_PREFILTER_FULL_REPLAY', '1') != '1':
        return mask, left
    found = tpcds_profile.affected_queries(profile, files, file_masks, bit)
    if found is not None and not found[0]:
        found = tpcds_profile.affected_queries(profile, files, file_masks, bit, granular=False)
    if found is None or not found[0]:
        return mask, left
    queries, reached = found
    order = [query for query in range(profile['count']) if queries >> query & 1]
    estimate = TPCDS_OVERHEAD + TPCDS_SLOWDOWN * sum(profile['cpu'][query] for query in order)
    if estimate > gain / 2 or estimate > left:
        return mask, left
    import tpcds_replay

    started = time.monotonic()
    passed = tpcds_replay.passes(REPO, files, order, reached, profile, min(left, 2 * estimate + 5))
    left -= time.monotonic() - started
    print(f"tpcds replay: {len(order)} queries, {('passed' if passed else 'kept')}", file=sys.stderr)
    return (mask & ~bit if passed else mask), left


def narrow_with_replay(workers, mask, nearby, files, numbered, duration, test_ids, remaining):
    """Replace workers by a plan without the replayable tests that pass on the patched sources.

    Only used for small selections of cheap unit tests. Returns (workers, replay seconds left).
    """
    selected = [number for bit, (_ms, number) in numbered if bit is None or bit & mask]
    replayable = [number for number in selected if test_ids[number].startswith(REPLAYABLE)]
    retained = [number for number in selected if not test_ids[number].startswith(REPLAYABLE)]
    if replayable and len(replayable) <= MAX_REPLAYED and sum(duration[number] for number in replayable) <= MAX_REPLAYED_MS:
        if retained:
            retained_plan = schedule([(duration[number], number) for number in retained])
            if plan_milliseconds(workers, duration) - plan_milliseconds(retained_plan, duration) < MIN_SAVING_MS:
                replayable = []
    else:
        replayable = []
    if not replayable:
        return workers, remaining
    candidates = replayable
    extras = sorted(
        (ms, number)
        for bit, (ms, number) in numbered
        if bit is not None and bit & nearby and not bit & mask and test_ids[number].startswith('tests/dialects/')
    )
    spare_ms = EXTRA_DIALECT_MS
    for ms, number in extras:
        if ms > spare_ms or len(candidates) >= MAX_WITH_EXTRAS:
            break
        candidates.append(number)
        spare_ms -= ms
    from replay import failing_tests

    started = time.monotonic()
    failed = failing_tests(REPO, files, [test_ids[number] for number in candidates], min(REPLAY_SECONDS, remaining))
    if failed is not None:
        required = set(retained) | {number for number in candidates if test_ids[number] in failed}
        if retained:
            reduced = sum(bit for bit, (_ms, number) in numbered if bit is not None and number in required)
            workers = schedule_with_margin(reduced, nearby, numbered, duration, True)
        else:
            workers = schedule([(duration[number], number) for number in required])
    remaining -= time.monotonic() - started
    return workers, remaining
