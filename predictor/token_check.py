"""Re-tokenize recorded SQL inputs to see whether a tokenizer edit changes any token stream.

tables/tok_inputs.bin (optional) holds, per test, the SQL strings the test
tokenizes, the tokenizer configuration for each, and the digest of the token
stream on the pinned tree. A test whose digests all still match is unaffected.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import marshal
import os
import signal
import sys
import zlib

_TABLES = {}


def load(tables_dir):
    """The decoded tok_inputs.bin of `tables_dir` (cached), or None."""
    table = _TABLES.get(tables_dir, 0)
    if table == 0:
        try:
            with open(os.path.join(tables_dir, 'tok_inputs.bin'), 'rb') as handle:
                table = marshal.loads(zlib.decompress(handle.read()))
        except (OSError, ValueError, EOFError, zlib.error):
            table = None
        _TABLES[tables_dir] = table
    return table


def checkable_tests(tables_dir):
    """Test numbers that have recorded tokenizer inputs."""
    table = load(tables_dir)
    return frozenset(table['tests']) if table else frozenset()


def _resolve(module_name, dotted_path):
    target = importlib.import_module(module_name)
    for part in dotted_path.split('.'):
        target = getattr(target, part)
    return target


def build_tokenizer(key):
    """Instantiate the tokenizer described by a recorded configuration key."""
    kind, dialect_module, dialect_path, version, normalization, settings_json = key
    dialect_class = _resolve(dialect_module, dialect_path)
    settings = json.loads(settings_json) if settings_json else {}
    if version and tuple(version) != (sys.maxsize, 0, 0):
        settings['version'] = '.'.join((str(part) for part in version))
    if normalization is not None and str(getattr(dialect_class.NORMALIZATION_STRATEGY, 'value', dialect_class.NORMALIZATION_STRATEGY)) != normalization:
        settings['normalization_strategy'] = normalization
    dialect = dialect_class(**settings)
    if kind == 'tokenizer':
        tokenizer_class = type(dialect).tokenizer_class
    elif kind == 'jsonpath':
        tokenizer_class = type(dialect).jsonpath_tokenizer_class
    else:
        _, class_module, class_path = kind.split(':', 2)
        tokenizer_class = _resolve(class_module, class_path)
    return tokenizer_class(dialect=dialect)


def digest(tokenizer, sql):
    """SHA-1 of the token stream (or of the raised error) for `sql`."""
    tokens_module = sys.modules['sqlglot.tokens']
    try:
        tokens = tokens_module.Tokenizer.tokenize(tokenizer, sql)
        stream = [(int(token.token_type), token.text, token.line, token.col, token.start, token.end, tuple(token.comments)) for token in tokens]
    except _OutOfTime:
        raise
    except Exception as error:
        stream = ('raised', type(error).__name__, str(error))
    return hashlib.sha1(repr(stream).encode('utf-8', 'surrogatepass')).digest()


class _OutOfTime(BaseException):
    pass


def _alarm(signum, frame):
    raise _OutOfTime()


def changed_token_tests(tables_dir, wanted, budget):
    """The subset of test numbers `wanted` whose token streams may have changed.

    Tests without recorded inputs count as changed, and so does every input left
    unchecked when the CPU `budget` (seconds) runs out.
    """
    table = load(tables_dir)
    if not table:
        return set(wanted)
    inputs, digests, tokenizer_keys = (table['inputs'], table['digests'], table['keys'])
    changed_tests = set()
    inputs_of_test = {number: list(table['tests'].get(number, ())) for number in wanted}
    for number, input_ids in inputs_of_test.items():
        if not input_ids or number not in table['tests']:
            changed_tests.add(number)
    to_check = sorted({input_id for number, input_ids in inputs_of_test.items() if number not in changed_tests for input_id in input_ids})
    if not to_check:
        return changed_tests
    changed_inputs = set()
    tokenizers = {}
    position = 0
    previous_handler = signal.signal(signal.SIGVTALRM, _alarm)
    signal.setitimer(signal.ITIMER_VIRTUAL, max(0.05, budget))
    try:
        for position, input_id in enumerate(to_check):
            tokenizer_id, sql = inputs[input_id]
            try:
                tokenizer = tokenizers.get(tokenizer_id)
                if tokenizer is None:
                    tokenizer = tokenizers[tokenizer_id] = build_tokenizer(tokenizer_keys[tokenizer_id])
                if digest(tokenizer, sql) != digests[input_id]:
                    changed_inputs.add(input_id)
            except _OutOfTime:
                raise
            except Exception:
                changed_inputs.add(input_id)
    except _OutOfTime:
        changed_inputs.update(to_check[position:])
    finally:
        signal.setitimer(signal.ITIMER_VIRTUAL, 0)
        signal.signal(signal.SIGVTALRM, previous_handler)
    for number, input_ids in inputs_of_test.items():
        if number not in changed_tests and (not changed_inputs.isdisjoint(input_ids)):
            changed_tests.add(number)
    return changed_tests
