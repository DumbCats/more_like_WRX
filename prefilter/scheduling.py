"""Packing selected tests into parallel workers.

Tests are (milliseconds, test number) pairs; a plan is a list of workers, each a list of
test numbers. Every worker costs a fixed start-up overhead on top of its running time.
"""
import bisect

WORKER_OVERHEAD_MS = 2000
MAX_WORKERS = 8


def rebalance(workers, loads, floor, rounds=100):
    """Move or swap single tests off the busiest worker while that lowers the peak load."""
    for _round in range(rounds):
        busiest = loads.index(max(loads))
        if loads[busiest] <= floor:
            return
        best = None
        for other in range(len(loads)):
            gap = loads[busiest] - loads[other]
            if other == busiest or gap <= 1:
                continue
            # (ms, index in other) of every test that could be swapped back; (0, None) = plain move
            swaps = sorted((ms, index) for index, (ms, _number) in enumerate(workers[other]))
            swaps.insert(0, (0, None))
            swap_ms = [ms for ms, _index in swaps]
            for moved_index, (moved_ms, _number) in enumerate(workers[busiest]):
                nearest = bisect.bisect_left(swap_ms, moved_ms - gap / 2)
                for choice in (nearest - 1, nearest):
                    if 0 <= choice < len(swaps):
                        shift = moved_ms - swap_ms[choice]
                        if 0 < shift < gap:
                            peak = max(loads[busiest] - shift, loads[other] + shift)
                            if best is None or peak < best[0]:
                                best = (peak, other, moved_index, swaps[choice][1])
        if best is None:
            return
        _peak, other, moved_index, swapped_index = best
        moved = workers[busiest].pop(moved_index)
        loads[busiest] -= moved[0]
        if swapped_index is not None:
            swapped = workers[other].pop(swapped_index)
            workers[busiest].append(swapped)
            loads[busiest] += swapped[0]
            loads[other] -= swapped[0]
        workers[other].append(moved)
        loads[other] += moved[0]


def schedule(tests):
    """The cheapest plan (peak load plus per-worker overhead) over 1..8 workers."""
    if not tests:
        return []
    best_cost = float('inf')
    best_plan = []
    longest_first = sorted(tests, reverse=True)
    total_ms = sum(ms for ms, _number in longest_first)
    longest_ms = longest_first[0][0]
    for count in range(1, min(MAX_WORKERS, len(tests)) + 1):
        floor = max(longest_ms, (total_ms + count - 1) // count)
        if floor + WORKER_OVERHEAD_MS * count >= best_cost:
            continue
        workers = [[] for _worker in range(count)]
        loads = [0] * count
        for test in longest_first:
            lightest = loads.index(min(loads))
            workers[lightest].append(test)
            loads[lightest] += test[0]
        rebalance(workers, loads, floor)
        cost = max(loads) + WORKER_OVERHEAD_MS * sum(bool(worker) for worker in workers)
        if cost < best_cost:
            best_cost, best_plan = cost, workers
    return [[number for _ms, number in worker] for worker in best_plan if worker]


def selected_tests(mask, numbered):
    """(ms, number) of the tests in mask; tests unknown to the profiles are always included."""
    return [test for bit, test in numbered if bit is None or bit & mask]


def schedule_with_margin(mask, nearby, numbered, duration, widen):
    """Schedule the tests in mask; when widen, fill idle worker time with unselected tests.

    Tests near the edit (in nearby) are preferred, then shorter ones.
    """
    plan = schedule(selected_tests(mask, numbered))
    if not widen:
        return plan
    if not plan:
        return []
    loads = [sum(duration[number] for number in worker) for worker in plan]
    peak = max(loads)
    spares = sorted((not bool(bit & nearby), ms, number) for bit, (ms, number) in numbered if bit is not None and not bit & mask)
    for _far, ms, number in spares:
        lightest = loads.index(min(loads))
        if loads[lightest] + ms <= peak:
            plan[lightest].append(number)
            loads[lightest] += ms
    return plan


def plan_milliseconds(plan, duration):
    """Estimated wall time of a plan: overhead per worker plus the busiest worker."""
    return WORKER_OVERHEAD_MS * len(plan) + max((sum(duration[number] for number in worker) for worker in plan), default=0)


def mask_cost(mask, numbered, duration):
    """Estimated wall time of the plan for the tests in mask."""
    return plan_milliseconds(schedule(selected_tests(mask, numbered)), duration)
