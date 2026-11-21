"""Split the selected tests of one commit into worker groups.

Each extra worker costs a fixed start-up time, so the packer tries 1..8 workers,
fills them greedily (longest test first, into the least loaded worker) and keeps
the layout with the smallest estimated wall time.
"""
from functools import lru_cache

WORKER_START_MS = 2000
MAX_WORKERS = 8


@lru_cache(maxsize=512)
def _best_groups(test_numbers, durations_ms):
    """Return the cheapest grouping of `test_numbers` as a tuple of tuples."""
    longest_first = sorted(zip(test_numbers, durations_ms), key=lambda pair: -pair[1])
    total_ms = sum(durations_ms)
    longest_ms = longest_first[0][1]
    best_cost = total_ms + WORKER_START_MS
    best_groups = [tuple(number for number, _ in longest_first)]
    for workers in range(2, min(MAX_WORKERS, len(longest_first)) + 1):
        startup_ms = WORKER_START_MS * workers
        lower_bound = max(longest_ms, (total_ms + workers - 1) // workers)
        if startup_ms + lower_bound >= best_cost:
            continue
        loads = [0] * workers
        groups = [[] for _ in range(workers)]
        for number, duration in longest_first:
            target = loads.index(min(loads))
            loads[target] += duration
            groups[target].append(number)
        cost = max(loads) + startup_ms
        if cost < best_cost:
            best_cost = cost
            best_groups = [tuple(group) for group in groups if group]
    return tuple(best_groups)


def pack(test_numbers, seconds):
    """Group `test_numbers` (with per-test `seconds`) into lists, one per worker."""
    numbers = tuple(test_numbers)
    if not numbers:
        return []
    durations_ms = tuple(round(seconds[number] * 1000) for number in numbers)
    return [list(group) for group in _best_groups(numbers, durations_ms)]
