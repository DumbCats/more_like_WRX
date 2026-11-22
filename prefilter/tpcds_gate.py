"""Command-line stage gate for the TPC-DS executor test, used by the predictor on its final answers.

Usage: tpcds_gate.py <seconds>   with stdin {"commit": saving in seconds, ...}

For each commit (largest saving first) whose patch the gate can check and whose saving pays for
the check (see refinement.py), runs tpcds_stages.still_passes and prints one JSON line
{"commit": ..., "verdict": "pass" | "fail" | "duckdb", "executed": n}. Commits it does not check
print nothing. Reads the patches from BLAST_DATA/commits and the pinned tree at BLAST_REPO.
"""
import json
import sys
import time

import tpcds_stages
from diff_reader import file_edits
from refinement import FULL_STAGE_OVERHEAD, SAFE_STAGE_SECONDS, STAGE_GAIN_RATIO
from settings import GENERATED_DIR, REPO, TASK_DATA


def main(seconds, savings):
    deadline = time.monotonic() + seconds
    stages = tpcds_stages.load(GENERATED_DIR / 'tpcds-stages.json')
    if not stages:
        return
    for commit, saving in sorted(savings.items(), key=lambda item: -item[1]):
        left = deadline - time.monotonic()
        try:
            skipped = set()
            files = list(file_edits((TASK_DATA / 'commits' / f'{commit}.patch').read_text(encoding='utf8'), skipped))
        except Exception:
            continue
        if skipped or not files or not all(path.startswith('sqlglot/') and path.endswith('.py') for path, *_rest in files):
            continue
        safe = tpcds_stages.downstream_safe(files, stages)
        estimate = SAFE_STAGE_SECONDS if safe else FULL_STAGE_OVERHEAD + sum(stages['cpu'])
        if estimate > STAGE_GAIN_RATIO * saving or estimate > left:
            continue
        verdict, executed = tpcds_stages.still_passes(REPO, files, stages, safe, stages['cpu'], min(left, 2 * estimate + 3), stages.get('tables'))
        print(f'tpcds gate {commit} ({"safe" if safe else "full"}): {verdict}, {executed} executed', file=sys.stderr)
        print(json.dumps({'commit': commit, 'verdict': verdict, 'executed': executed}), flush=True)


if __name__ == '__main__':
    main(float(sys.argv[1]), json.load(sys.stdin))
