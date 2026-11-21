"""Environment knobs, tuning constants and the run-wide shared state.

Everything here is read as `settings.NAME`. The names in the last section are
assigned while the predictor runs (by start.py and the coordinator) and read by
the planner and by forked children, so they must have this single home.
"""
import os
import sys

PREDICTOR_DIR = os.path.dirname(os.path.abspath(__file__))

# Optional helpers: without them the predictor still works, only less precisely.
try:
    import registry_keys
except ImportError:
    registry_keys = None
try:
    import reimport_cone
except ImportError:
    reimport_cone = None

# --- locations -------------------------------------------------------------
REPO = os.environ.get('BLAST_REPO', '/repo')
DATA = os.environ.get('BLAST_DATA', '/data')
TABLES = os.environ.get('BLAST_TABLES', os.path.join(PREDICTOR_DIR, 'tables'))

# --- switches ----------------------------------------------------------------
DEBUG = os.environ.get('BLAST_DEBUG')                      # path of a JSON debug dump
PLAN_ONLY = os.environ.get('BLAST_PLAN_ONLY')              # path: write the plans and stop
RUN_ALL = os.environ.get('BLAST_RUN_ALL')                  # run every candidate, no holding back
RUN_SLOW = os.environ.get('BLAST_RUN_SLOW')                # with RUN_ALL: also tests over 60 s
NO_REDUCE = os.environ.get('BLAST_NO_REDUCE')              # never run reduced test bodies
NO_DIFF = os.environ.get('BLAST_NO_DIFF')                  # no import-state diff for fresh runs
FAULT_SECONDS = os.environ.get('BLAST_FAULT')              # children dump tracebacks after this
PREFILTER_TRIM = os.environ.get('BLAST_PREFILTER_TRIM', '1') == '1'
PREFILTER_CUT = os.environ.get('BLAST_PREFILTER_CUT', '1') == '1'
CLASS_FILTER = os.environ.get('BLAST_CLASS_FILTER') == '1'
FRESH_REDUCE = os.environ.get('BLAST_FRESH_REDUCE', '1') == '1'
NO_CLASSMAP = os.environ.get('BLAST_NO_CLASSMAP') == '1'
NO_KEYMAP = os.environ.get('BLAST_NO_KEYMAP') == '1' or registry_keys is None
NO_CONE = os.environ.get('BLAST_NO_CONE') == '1' or reimport_cone is None
TIGHT_LINES = os.environ.get('BLAST_NO_TIGHT') != '1'
TOKEN_CHECK = os.environ.get('BLAST_NO_TOKCHECK') != '1'
INFECT = os.environ.get('BLAST_NO_INFECT') != '1'
INFECT_BY_OPERAND = os.environ.get('BLAST_NO_INFECT2') != '1'
INFECT_AFTER_IMPORT = os.environ.get('BLAST_NO_INFECT_FRESH') != '1'
FIXTURE_BATCH = os.environ.get('BLAST_NO_FIXTURE_BATCH') != '1'
LAZY_PANDAS = not os.environ.get('BLAST_NO_LAZY_PANDAS')
PREIMPORT = os.environ.get('BLAST_NO_PREIMPORT') != '1'
BASE_STATE_FILE = not os.environ.get('BLAST_NO_BASE_MAP')
BASE_CODES_WANTED = os.environ.get('BLAST_NO_BASE_CODES') != '1'

# --- time budgets (seconds) --------------------------------------------------
WALL_LIMIT = float(os.environ.get('BLAST_WALL', '150'))    # reduced by start.py
SMALL_PLAN_SECONDS = float(os.environ.get('BLAST_SMALL_PLAN', '0.2'))
HOLD_SECONDS = float(os.environ.get('BLAST_HOLD', '1.5'))
HOLD_FACTOR = float(os.environ.get('BLAST_HOLD_FACTOR', '0.7'))
COMMIT_SECONDS = float(os.environ.get('BLAST_COMMIT', '8.0'))
FRESH_SECONDS = float(os.environ.get('BLAST_FRESH', '3.0'))
QUIET_HOLD_SECONDS = float(os.environ.get('BLAST_QUIET_HOLD', '5.0'))
AFTER_FAIL_RATIO = float(os.environ.get('BLAST_AFTER_FAIL', '0.5'))
SLOWDOWN = float(os.environ.get('BLAST_SLOWDOWN', '1.0'))
CLASS_SETUP_SECONDS = {'tests/test_executor.py::TestExecutor': 1.0} if os.environ.get('BLAST_CLASS_SETUP', '1') == '1' else {}
INERT_SECONDS = 1.0
WORKER_COST = 2.0
TEST_TIMEOUT = 4.0
SETUP_TIMEOUT = 6.0
TOKEN_CHECK_SECONDS = 2.0
MANY_FAILURES = 8
FIXTURE_BATCH_MIN = 3

TOKENIZER_FILES = frozenset(('sqlglot/tokens.py', 'sqlglot/tokenizer_core.py', 'sqlglot/trie.py', 'sqlglot/jsonpath.py'))
# tests whose bodies are scaled down (smaller loops) when they are run
SCALED_IDS = frozenset(('tests/test_parser.py::TestParser::test_max_nodes', 'tests/test_generator.py::TestGenerator::test_generator_memory', 'tests/test_optimizer.py::TestOptimizer::test_scope'))

# --- run-wide state (assigned while running) -------------------------------
RESTRICT = None            # prefilter answers: commit -> frozenset of test numbers, or None
ANSWER_FD = None           # duplicate of the original stdout; children close it
PROFILE = None             # the loaded profile_tables.ProfileTables
BASE_STATE = None          # import-state fingerprint of unpatched sqlglot
BASE_FUNCTION_KEYS = frozenset()
BASE_OBJECTS = {}
BASE_CODES = None
PATH_BEFORE_PREIMPORT = None   # sys.path before test libraries were pre-imported (if they changed it)
REGISTRY_CLEAR_AT = None   # first test number after the dialect registry is cleared
REGISTRY_START = None      # dialect registry keys present when the suite starts
PANDAS_USERS = frozenset()  # tests that need the real numpy/pandas (when they are deferred)
CONE_MAP = None            # module -> modules re-imported with it
CONE_WALK = []             # module walk order for reimport_cone.record_imports
DEBUG_DIFF_KEYS = {}       # commit -> import-state diff rows (debug dump only)


def log(*parts):
    """Write one line to stderr."""
    print(*parts, file=sys.stderr, flush=True)
