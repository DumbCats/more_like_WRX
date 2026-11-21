"""Predictor entry point: run the prefilter, then the coordinator with what is left of the wall budget.

The prefilter (BLAST_PREFILTER, default /prefilter/main.py) is a static
selector run as a subprocess; its answers (commit -> tests it may select)
restrict what the coordinator runs and selects. BLAST_NO_PREFILTER=1 skips it.
"""
import json
import os
import subprocess
import sys
import time

import coordinator
import settings

PREFILTER = os.environ.get('BLAST_PREFILTER', '/prefilter/main.py')
PREFILTER_SECONDS = float(os.environ.get('BLAST_PREFILTER_SECONDS', '60'))


def prefilter_environment():
    """The prefilter's environment: ours plus defaults for the knobs it reads.

    Its replay and TPC-DS budgets default to 0 (off).
    """
    environment = dict(os.environ)
    environment.setdefault('BLAST_REPO', '/repo')
    environment.setdefault('BLAST_DATA', '/data')
    environment.setdefault('BLAST_PREFILTER_REPLAY_BUDGET', '0')
    environment.setdefault('BLAST_PREFILTER_TPCDS_BUDGET', '0')
    return environment


def prefilter_answers():
    """{commit: frozenset of test numbers} from the prefilter, or None when it is off or failed."""
    if os.environ.get('BLAST_NO_PREFILTER') == '1' or not os.path.exists(PREFILTER):
        return None
    try:
        completed = subprocess.run([sys.executable, PREFILTER], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, env=prefilter_environment(), timeout=PREFILTER_SECONDS)
        output = completed.stdout
    except subprocess.TimeoutExpired as timeout:
        output = timeout.stdout or b''
        print('prefilter timed out', file=sys.stderr)
    except Exception as error:
        print('prefilter failed:', repr(error), file=sys.stderr)
        return None
    answers = {}
    for line in output.decode(errors='replace').splitlines():
        try:
            answer = json.loads(line)
            answers[answer['commit']] = frozenset((int(number) for worker in answer['workers'] for number in worker))
        except (ValueError, KeyError, TypeError):
            continue
    return answers


if __name__ == '__main__':
    started_at = time.monotonic()
    settings.RESTRICT = prefilter_answers()
    print('prefilter answers:', None if settings.RESTRICT is None else len(settings.RESTRICT), 'in', round(time.monotonic() - started_at, 2), 's', file=sys.stderr)
    settings.WALL_LIMIT = max(30.0, settings.WALL_LIMIT - (time.monotonic() - started_at))
    coordinator.main()
