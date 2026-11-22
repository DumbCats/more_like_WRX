"""Launch the selection: the static selector first, then the run-based selector (selector.py).

The static selector (/screen/selector.py) prints, per commit, a set of tests that
holds every test the commit can break. The pipeline then runs candidates on the
patched code and keeps the ones that fail. Each answer is a superset of the
failing tests, so their intersection is one too: the pipeline keeps, for every
commit, only the tests that both select. When the static selector fails, takes
too long or leaves a commit out, the pipeline's own answer stands for it.
"""
import json
import os
import subprocess
import sys
import time

import selector as impact_pipeline

SCREEN = os.environ.get("BR_SCREEN", "/screen/selector.py")
SCREEN_SECONDS = float(os.environ.get("BR_SCREEN_SECONDS", "60"))


def screen_answers():
    if os.environ.get("BR_NO_SCREEN") == "1" or not os.path.exists(SCREEN):
        return None
    env = dict(os.environ)
    env.setdefault("SOURCE", os.environ.get("BR_ROOT", "/repo"))
    env.setdefault("DATA", os.environ.get("BR_DATA", "/data"))
    # The pipeline runs tests on the patched code itself, so the static selector's own
    # replays (unittest and TPC-DS) would only repeat that work: its static answer is enough.
    env.setdefault("SCREEN_REPLAY_BUDGET", "0")
    env.setdefault("SCREEN_TPCDS_BUDGET", "0")
    try:
        done = subprocess.run([sys.executable, SCREEN], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                              env=env, timeout=SCREEN_SECONDS)
        out = done.stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout or b""
        print("static selector timed out", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print("static selector failed:", repr(e), file=sys.stderr)
        return None
    answers = {}
    for line in out.decode(errors="replace").splitlines():
        try:
            row = json.loads(line)
            answers[row["commit"]] = frozenset(int(n) for w in row["workers"] for n in w)
        except (ValueError, KeyError, TypeError):
            continue  # a partial line (a timeout): that commit keeps the pipeline's answer
    return answers


if __name__ == "__main__":
    start = time.monotonic()
    impact_pipeline.RESTRICT = screen_answers()
    print("static answers:", None if impact_pipeline.RESTRICT is None else len(impact_pipeline.RESTRICT),
          "in", round(time.monotonic() - start, 2), "s", file=sys.stderr)
    impact_pipeline.WALL_LIMIT = max(30.0, impact_pipeline.WALL_LIMIT - (time.monotonic() - start))
    impact_pipeline.main()
