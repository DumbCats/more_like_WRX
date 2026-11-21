"""Static prefilter: for each commit patch, print the tests it may affect, packed into workers.

Reads BLAST_DATA/tests.json and BLAST_DATA/commits/*.patch, analyses each diff against the
pinned sources in BLAST_REPO using build-time coverage profiles, and prints one JSON line
{"commit": ..., "workers": [[test numbers], ...]} per patch. Whatever cannot be analysed
(or is left over after a crash) selects every test.
"""
import json
import os

import tpcds_profile
from diff_reader import file_edits
from file_masks import mask_for_file
from fixtures import fixture_structure_mask
from refinement import drop_tpcds, narrow_with_replay
from scheduling import schedule, schedule_with_margin
from settings import REPLAY_BUDGET_DEFAULT, REPLAY_BUDGET_VARIABLE, SHIPPED_DIR, TASK_DATA, TPCDS_BUDGET_DEFAULT, TPCDS_BUDGET_VARIABLE
from tables import load_tables


def emit(printed, commit, workers):
    """Print the answer for commit once."""
    if commit not in printed:
        printed.add(commit)
        print(json.dumps(dict(commit=commit, workers=workers), separators=(',', ':')), flush=True)


def fallback(printed):
    """Answer every commit not answered yet with the full test suite."""
    tests = json.loads((TASK_DATA / 'tests.json').read_text())['tests']
    everything = schedule([(round(1000 * test['seconds']), test['number']) for test in tests])
    for patch in sorted((TASK_DATA / 'commits').glob('*.patch')):
        emit(printed, patch.stem, everything)


def analyse_patch(diff, maps, all_mask, files, skipped, file_masks, neighbors):
    """The mask of tests affected by diff (fills files, skipped, file_masks and neighbors)."""
    mask = 0
    files.extend(list(file_edits(diff, skipped)))  # all or nothing when the diff is unreadable
    if not files and not skipped:
        raise ValueError('missing diff')
    for path in skipped:
        file_masks[path] = int(maps['files'].get(path, '0'), 16) | int(maps['source_readers'].get(path, '0'), 16)
        mask |= file_masks[path]
    for edit in files:
        path = edit[0]
        try:
            file_masks[path] = mask_for_file(*edit, maps, all_mask, neighbors, isolated=len(files) == 1 and not skipped)
        except SyntaxError:
            if len(files) != 1 or skipped or path not in maps['cold_imports']:
                raise
            file_masks[path] = maps['cold_imports'][path]
        mask |= file_masks[path]
    if neighbors:
        mask |= int(maps.get('opaque', '0'), 16)
    return mask


def main(printed):
    maps, index = load_tables()
    tests = json.loads((TASK_DATA / 'tests.json').read_text())['tests']
    all_mask = (1 << len(index)) - 1
    # (bit of the test in the profiles or None when unknown, (milliseconds, test number))
    numbered = [(1 << index[test['id']] if test['id'] in index else None, (round(1000 * test['seconds']), test['number'])) for test in tests]
    duration = {number: ms for _bit, (ms, number) in numbered}
    test_ids = {test['number']: test['id'] for test in tests}
    maps['tpcds'] = tpcds_profile.load(SHIPPED_DIR / 'tpcds.gz')
    tpcds_id = maps['tpcds']['test'] if maps['tpcds'] else None
    tpcds_bit = 1 << index[tpcds_id] if tpcds_id in index and any(test['id'] == tpcds_id for test in tests) else 0
    if maps['tpcds'] and maps['tpcds']['count'] != len(maps['tpcds']['cpu']):
        tpcds_bit = 0
    tpcds_left = float(os.environ.get(TPCDS_BUDGET_VARIABLE, TPCDS_BUDGET_DEFAULT))
    plans = {}
    replay_left = float(os.environ.get(REPLAY_BUDGET_VARIABLE, REPLAY_BUDGET_DEFAULT))
    for patch in sorted((TASK_DATA / 'commits').glob('*.patch')):
        mask = 0
        neighbors = []
        files = []
        skipped = set()
        file_masks = {}
        analysed = False
        try:
            diff = patch.read_text(encoding='utf8')
            structural = fixture_structure_mask(diff, maps, all_mask)
            if structural is not None:
                emit(printed, patch.stem, schedule([test for bit, test in numbered if bit is None or bit & structural]))
                continue
            mask = analyse_patch(diff, maps, all_mask, files, skipped, file_masks, neighbors)
            analysed = True
        except Exception:
            mask = all_mask
        if analysed and files and mask != all_mask and tpcds_bit and mask & tpcds_bit:
            try:
                mask, tpcds_left = drop_tpcds(mask, files, skipped, file_masks, maps, numbered, duration, tpcds_bit, tpcds_left)
            except Exception:
                import sys
                import traceback

                traceback.print_exc(file=sys.stderr)
        nearby = 0
        for neighbor_mask in neighbors:
            nearby |= neighbor_mask
        key = (mask, nearby, bool(neighbors))
        if key not in plans:
            plans[key] = schedule_with_margin(mask, nearby, numbered, duration, bool(neighbors))
        workers = plans[key]
        if replay_left > 1 and workers and files and all(path.startswith('sqlglot/') and path.endswith('.py') for path, *_rest in files):
            workers, replay_left = narrow_with_replay(workers, mask, nearby, files, numbered, duration, test_ids, replay_left)
        emit(printed, patch.stem, workers)


if __name__ == '__main__':
    answered = set()
    try:
        main(answered)
    except BaseException:
        import sys
        import traceback

        traceback.print_exc(file=sys.stderr)
    finally:
        fallback(answered)
