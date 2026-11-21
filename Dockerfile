# Test-selection image for the Blast Radius track.
# Pinned base image (the one the organizers specify).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONNOUSERSITE=1 \
    SKIP_INTEGRATION=0 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    BLAST_REPO=/repo \
    BLAST_DATA=/data \
    BLAST_TABLES=/predictor/tables \
    BLAST_EXEC_CACHE=/predictor/cache \
    BLAST_PREFILTER=/prefilter/main.py

# --- Python packages: exact PyPI pins, installed without dependency resolution ----------------
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps --no-compile --disable-pip-version-check \
        --root-user-action=ignore -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# --- sqlglot source at the pinned commit (GitHub tarball, verified by tree digest) -------------
COPY setup/fetch_sqlglot.py /tmp/fetch_sqlglot.py
RUN python /tmp/fetch_sqlglot.py /repo b5e23dd1faff7c2a14ef374d02be8bab60c9343d6d3140690cbad12dbcc7764e \
    && rm /tmp/fetch_sqlglot.py

# --- Precompiled bytecode -----------------------------------------------------------------------
# The run filesystem is read-only, so every importable file (stdlib, site-packages, sqlglot and
# its tests) gets an unchecked-hash .pyc now. The slim image ships the stdlib without .pyc files,
# and pip's own .pyc files are timestamp-based, hence -f.
RUN python -m compileall -f -q -j 0 --invalidation-mode unchecked-hash /usr/local/lib/python3.13 /repo \
    && chmod -R a+rX /repo /usr/local/lib/python3.13

# --- Interpreter settings for the scored run ----------------------------------------------------
# A fixed hash seed (the saved base state depends on it) and mimalloc with large pages
# (cheaper fork, exit and page faults; harmless where the host has transparent huge pages off).
ENV PYTHONHASHSEED=0 \
    PYTHONMALLOC=mimalloc \
    MIMALLOC_LARGE_OS_PAGES=1 \
    MIMALLOC_RESERVE_OS_MEMORY=256MiB \
    MIMALLOC_ARENA_EAGER_COMMIT=1 \
    MIMALLOC_PURGE_DELAY=-1

# --- Static prefilter ---------------------------------------------------------------------------
# Runs first (predictor/start.py launches it); the predictor keeps only tests both sides select.
# Its indexes over the pinned tree are generated here into /prefilter/generated.
COPY prefilter /prefilter
RUN mkdir -p /prefilter/generated \
    && python /prefilter/build/instruction_map.py /repo /prefilter/generated/instructions.gz \
    && python /prefilter/build/refs_index.py /repo /prefilter/generated/refs.json.gz \
    && python /prefilter/build/test_readers.py /repo /prefilter/generated/metadata-readers.json /prefilter/generated/frame-readers.json \
    && python /prefilter/build/fixture_fields.py /repo /prefilter/data/coverage.json.gz > /prefilter/generated/fixture-fields.json \
    && python /prefilter/build/frame_scope.py /repo > /prefilter/generated/frame-scope.json \
    && python /prefilter/build/default_flow.py /repo > /prefilter/generated/default-flow.json \
    && python -m compileall -f -q --invalidation-mode unchecked-hash /prefilter \
    && chmod -R a+rX /prefilter

# --- Dynamic predictor (last, so code changes rebuild only this layer) --------------------------
# The TestExecutor row cache is built from the pinned csv files and verified against the real
# setUpClass; if verification fails the cache is dropped and setUpClass runs at test time.
# The base-state snapshot is optional too: without it the predictor computes it on the fly.
COPY predictor /predictor
RUN (python /predictor/build/executor_rows.py /repo /predictor/cache --check \
     || (echo "exec cache check failed; running without it" >&2; rm -rf /predictor/cache)) \
    && mkdir -p /predictor/cache \
    && (python /predictor/build/base_state.py /repo /predictor/cache/base_state.bin \
     || (echo "no saved base state; the predictor computes it" >&2; rm -f /predictor/cache/base_state.bin)) \
    && python -m compileall -f -q --invalidation-mode unchecked-hash /predictor \
    && chmod -R a+rX /predictor

WORKDIR /repo
CMD ["python", "/predictor/start.py"]
