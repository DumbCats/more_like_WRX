"""Locations and environment knobs shared by the prefilter modules."""
import os
from pathlib import Path

PREFILTER_DIR = Path(__file__).resolve().parent
# Profiles shipped with the entry (measured offline against the pinned tree).
SHIPPED_DIR = PREFILTER_DIR / 'data'
# Indexes derived from the pinned tree while the image is built (see build/).
GENERATED_DIR = PREFILTER_DIR / 'generated'

# The pinned sqlglot checkout and the task directory (tests.json + commits/*.patch).
REPO = Path(os.environ.get('BLAST_REPO', '/src'))
TASK_DATA = Path(os.environ.get('BLAST_DATA', '/data'))

# Seconds the prefilter may spend replaying tests / TPC-DS queries on patched sources.
REPLAY_BUDGET_VARIABLE = 'BLAST_PREFILTER_REPLAY_BUDGET'
REPLAY_BUDGET_DEFAULT = '22.0'
TPCDS_BUDGET_VARIABLE = 'BLAST_PREFILTER_TPCDS_BUDGET'
TPCDS_BUDGET_DEFAULT = 45.0
