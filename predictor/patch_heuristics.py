"""Source-level rules that widen or narrow the tests a patch can affect.

Each helper answers one question about a patch using the pinned sqlglot
checkout (BLAST_REPO) and the profile tables (`maps`, the coordinator's table
object, offering `meta`, `tests(set_id)` and `file(rel)`): which tests list
directories, reference a name, import a module, read docstrings, which edits
are inert, which pytest configuration files matter, and so on.
"""
from __future__ import annotations

import ast
import os
import re
import sys
import types

import patching

ROOT = os.environ.get('BLAST_REPO', '/repo')


def _indent(line):
    return len(line) - len(line.lstrip())


# Memoised reads of the checkout: file text by relative path, plus the derived
# lookups below ('test_files', 'tree', 'listers', 'docs').
_SOURCES = {}
_DERIVED = {}


def test_files(maps):
    """Test file path -> test numbers defined in it (profile order)."""
    by_file = _DERIVED.get('test_files')
    if by_file is None:
        by_file = _DERIVED['test_files'] = {}
        for number, test_id in enumerate(maps.meta['test_ids']):
            by_file.setdefault(test_id.split('::')[0], []).append(number)
    return by_file


def read_source(rel):
    """Text of a checkout file ('' when unreadable), cached."""
    text = _SOURCES.get(rel)
    if text is None:
        try:
            with open(os.path.join(ROOT, rel), encoding='utf-8') as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            text = ''
        _SOURCES[rel] = text
    return text


def _tree_py_files():
    """Every .py file under sqlglot/ and tests/ (os.walk order)."""
    files = _DERIVED.get('tree')
    if files is None:
        files = _DERIVED['tree'] = []
        for top in ('sqlglot', 'tests'):
            for directory, _, file_names in os.walk(os.path.join(ROOT, top)):
                for file_name in file_names:
                    if file_name.endswith('.py'):
                        files.append(os.path.relpath(os.path.join(directory, file_name), ROOT))
    return files


# --- directory listings ----------------------------------------------------------

LISTING_WORDS = ('listdir', 'scandir', 'iterdir', 'iter_modules', 'walk_packages', 'glob', 'walk')
LISTING_RE = re.compile('\\b(?:listdir|scandir|iterdir|iter_modules|walk_packages|r?glob)\\s*\\(|\\bos\\.walk\\s*\\(')
DIRECTORY_SCAN_RE = re.compile('\\b(?:scandir|iterdir|r?glob)\\s*\\(|\\bos\\.walk\\s*\\(')


def _lists_directories(text):
    return any((word in text for word in LISTING_WORDS)) and LISTING_RE.search(text) is not None


def install_listdir_overlay(root, files):
    """Make os.listdir show added files and hide deleted ones."""
    added, deleted = ({}, {})
    for rel, text in files.items():
        directory, name = os.path.split(os.path.join(root, rel))
        (deleted if text is None else added).setdefault(directory, set()).add(name)
    real_listdir = os.listdir

    def listdir(path='.'):
        names = real_listdir(path)
        if isinstance(path, (str, os.PathLike)):
            full_path = os.path.abspath(os.fspath(path))
            if full_path in added or full_path in deleted:
                names = sorted((set(names) | added.get(full_path, set())) - deleted.get(full_path, set()))
        return names
    os.listdir = listdir


def directory_listing_tests(maps):
    """Tests whose files list directories; None when sqlglot itself lists directories."""
    if 'listers' not in _DERIVED:
        listers = set()
        for rel in _tree_py_files():
            if rel.startswith('sqlglot/') and _lists_directories(read_source(rel)):
                listers = None
                break
        if listers is not None:
            for rel, numbers in test_files(maps).items():
                if _lists_directories(read_source(rel)):
                    listers.update(numbers)
        _DERIVED['listers'] = listers
    return _DERIVED['listers']


# --- name references ---------------------------------------------------------------

def referencing_tests(maps, word):
    """Tests that may see `word`: files mentioning it, and the tests importing those.

    None when a sqlglot file mentions it but has no importer record.
    """
    word_re = re.compile('\\b%s\\b' % re.escape(word))
    found = set()
    by_file = test_files(maps)
    for rel in _tree_py_files():
        text = read_source(rel)
        if word not in text or not word_re.search(text):
            continue
        if rel in by_file:
            found.update(by_file[rel])
        importers = maps.meta['importers'].get(rel)
        if importers:
            found.update(maps.tests(importers))
        elif rel.startswith('sqlglot/'):
            return None
    return found


def tests_under(maps, directory):
    """Test numbers of every test file below `directory` ('' = all)."""
    return {number for rel, numbers in test_files(maps).items() if not directory or rel.startswith(directory + '/') for number in numbers}


# --- conftest.py ---------------------------------------------------------------------

def _trivial_body(body):
    for statement in body:
        if isinstance(statement, ast.Pass) or (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)):
            continue
        if isinstance(statement, ast.Return) and (statement.value is None or isinstance(statement.value, ast.Constant)):
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Yield) and (statement.value.value is None or isinstance(statement.value.value, ast.Constant)):
            continue
        return False
    return True


def _conftest_harmless(text):
    """True for an added conftest.py that only imports stdlib/pytest and defines inert fixtures."""
    import importlib
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and (node.level or not node.module):
                return False
            module_names = [node.module] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names]
            for module_name in module_names:
                top = module_name.partition('.')[0]
                if top not in sys.stdlib_module_names and top != 'pytest':
                    return False
                try:
                    module = importlib.import_module(module_name)
                except Exception:
                    return False
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name == '*' or not hasattr(module, alias.name):
                            return False
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                call = decorator if isinstance(decorator, ast.Call) else None
                target = call.func if call else decorator
                name = target.attr if isinstance(target, ast.Attribute) else getattr(target, 'id', None)
                if name != 'fixture':
                    return False
                if call and any((keyword.arg == 'autouse' and (not (isinstance(keyword.value, ast.Constant) and (not keyword.value.value))) for keyword in call.keywords)) and (not _trivial_body(node.body)):
                    return False
            if node.name.startswith('pytest_') and (not _trivial_body(node.body)):
                return False
            continue
        return False
    return True


# --- removed names ---------------------------------------------------------------------

TOP_NAME_RE = re.compile('^(?:async\\s+def|def|class)\\s+(\\w+)|^(\\w+)\\s*(?::[^=]*)?=(?!=)')
IMPORT_RE = re.compile('^(?:from\\s+\\S+\\s+)?import\\s+(.*)')


def _defined_names(lines):
    """Names defined (def/class/assignment/import) by the given source lines."""
    names = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        match = TOP_NAME_RE.match(line)
        if match:
            names.add(match.group(1) or match.group(2))
            continue
        match = IMPORT_RE.match(line)
        if match:
            for item in match.group(1).replace('(', ' ').replace(')', ' ').split(','):
                words = item.split()
                if words:
                    names.add(words[-1].split('.')[0])
    return names


def _name_importers(maps, module, names):
    """Tests whose file does `from <module> import` one of `names`."""
    from_import_re = re.compile('^\\s*from\\s+%s\\s+import\\s+(\\([^)]*\\)|[^\\n]*)' % re.escape(module), re.M)
    found = set()
    for rel, numbers in test_files(maps).items():
        text = read_source(rel)
        if module not in text:
            continue
        for match in from_import_re.finditer(text):
            if any((re.search('\\b%s\\b' % re.escape(name), match.group(1)) for name in names)):
                found.update(numbers)
                break
    return found


def _docstring_consts(code):
    """Indices of constants used only as `__doc__ = <const>`."""
    import dis
    doc, other = (set(), set())
    instructions = list(dis.get_instructions(code))
    for index, instruction in enumerate(instructions):
        if instruction.opcode in dis.hasconst:
            following = instructions[index + 1] if index + 1 < len(instructions) else None
            if instruction.opname == 'LOAD_CONST' and following is not None and (following.opname == 'STORE_NAME') and (following.argval == '__doc__'):
                doc.add(instruction.arg)
            else:
                other.add(instruction.arg)
    return doc - other


def code_equal_except_docstrings(a, b, pairs):
    """patching.equal_at_definition_time, but class/module docstrings may differ."""
    if a.co_code != b.co_code or a.co_names != b.co_names or a.co_exceptiontable != b.co_exceptiontable or (len(a.co_consts) != len(b.co_consts)) or (a.co_flags != b.co_flags):
        return False
    docstring_indices = None
    for index, (const_a, const_b) in enumerate(zip(a.co_consts, b.co_consts)):
        is_code = isinstance(const_a, types.CodeType)
        if is_code != isinstance(const_b, types.CodeType):
            return False
        if is_code:
            optimized = const_a.co_flags & patching.CO_OPTIMIZED
            if optimized != const_b.co_flags & patching.CO_OPTIMIZED:
                return False
            if optimized:
                if const_a.co_name != const_b.co_name or const_a.co_qualname != const_b.co_qualname or len(const_a.co_freevars) != len(const_b.co_freevars):
                    return False
                pairs.append((const_a, const_b))
            elif not code_equal_except_docstrings(const_a, const_b, pairs):
                return False
        elif not patching.const_equal(const_a, const_b):
            if docstring_indices is None:
                docstring_indices = _docstring_consts(a) & _docstring_consts(b)
            if index not in docstring_indices or not (isinstance(const_a, str) and isinstance(const_b, str)):
                return False
    return True


def _name_reader_tests(maps, names, only=None, prefix='\\b'):
    """Tests covering the lines that mention any of `names` (after `prefix`)."""
    name_re = re.compile('%s(?:%s)\\b' % (prefix, '|'.join(map(re.escape, sorted(names)))))
    found = set()
    by_file = test_files(maps)
    for rel in only or _tree_py_files():
        text = read_source(rel)
        if not any((name in text for name in names)):
            continue
        line_numbers = [index + 1 for index, line in enumerate(text.split('\n')) if any((name in line for name in names)) and name_re.search(line)]
        if not line_numbers:
            continue
        record = maps.file(rel)
        if record is None:
            found.update(by_file.get(rel, ()))
            continue
        line_sets = record[3]
        for line_number in line_numbers:
            if line_number < len(line_sets) and line_sets[line_number]:
                found.update(maps.tests(line_sets[line_number]))
    return found


PYTEST_CONFIGS = frozenset(('tox.ini', 'setup.cfg', 'pyproject.toml'))
STD_BASES = frozenset(('Enum', 'IntEnum', 'StrEnum', 'Flag', 'IntFlag', 'object', 'str', 'int', 'float', 'bytes', 'dict', 'list', 'tuple', 'set', 'frozenset', 'Exception', 'BaseException', 'ValueError', 'TypeError', 'KeyError', 'RuntimeError', 'NamedTuple', 'TypedDict', 'Protocol', 'Generic', 'ABC'))
CLASS_RE = re.compile('\\s*class\\s+\\w+\\s*(?:\\((.*)\\))?\\s*:')


def _enclosing_bases(lines, index):
    """Base class names of the class enclosing lines[index]; None if not in a class."""
    indent = _indent(lines[index])
    for previous in range(index - 1, -1, -1):
        line = lines[previous]
        if not line.strip() or _indent(line) >= indent or line.lstrip().startswith(('@', '#', ')')):
            continue
        if line.lstrip().startswith('class '):
            match = CLASS_RE.match(line)
            bases = (match.group(1) or '') if match else ''
            return [base.strip().split('.')[-1].split('[')[0] for base in bases.split(',') if base.strip() and '=' not in base]
        return None
    return None


def _defined_elsewhere(name, rel, line):
    """True when an indented definition of `name` exists in sqlglot other than at rel:line."""
    definition_re = re.compile('^\\s+(?:async\\s+def\\s+|def\\s+|class\\s+)?%s\\b\\s*[(:=]' % re.escape(name), re.M)
    for other in _tree_py_files():
        if not other.startswith('sqlglot/'):
            continue
        text = read_source(other)
        if name not in text:
            continue
        for match in definition_re.finditer(text):
            if other != rel or text.count('\n', 0, match.start()) + 1 != line:
                return True
    return False


def removed_name_tests(maps, fp, old_text):
    """(names the patch removes, tests that may use them).

    Top-level names count when no added line redefines them; class members
    count when the class has only standard bases or the member is not defined
    elsewhere in sqlglot.
    """
    old_lines = old_text.split('\n')
    redefined = set()
    for group in fp.groups:
        redefined |= _defined_names(group[5])
    top_level, members = (set(), set())
    for group in fp.groups:
        for offset, removed_line in enumerate(group[4]):
            names = _defined_names([removed_line]) - redefined
            if not names:
                continue
            if not removed_line[:1].isspace():
                top_level |= names
                continue
            index = group[0] - 1 + offset
            bases = _enclosing_bases(old_lines, index) if index < len(old_lines) else None
            if bases is None:
                continue
            for name in names:
                if all((base in STD_BASES for base in bases)) or not _defined_elsewhere(name, fp.old_path, index + 1):
                    members.add(name)
    found = set()
    if top_level:
        module = fp.old_path[:-3].replace('/', '.')
        if module.endswith('.__init__'):
            module = module[:-len('.__init__')]
        found |= _name_importers(maps, module, top_level)
        found |= _name_reader_tests(maps, top_level, only=[fp.old_path])
        found |= _name_reader_tests(maps, top_level, prefix='\\b%s\\s*\\.\\s*' % re.escape(module.rpartition('.')[2]))
    if members:
        found |= _name_reader_tests(maps, members)
    return (top_level | members, found)


# --- docstrings ---------------------------------------------------------------------------

DOC_READ_RE = re.compile('__doc__|getdoc|doctest')


def tests_read_docstrings(maps):
    """True when any test file reads docstrings."""
    if 'docs' not in _DERIVED:
        _DERIVED['docs'] = any((DOC_READ_RE.search(read_source(rel)) for rel in test_files(maps)))
    return _DERIVED['docs']


def _same_doc_examples(before, after):
    if '>>>' not in before and '>>>' not in after:
        return True
    from doctest import DocTestParser
    from inspect import cleandoc
    parser = DocTestParser()
    try:
        examples = [[(example.source, example.want, example.exc_msg, example.options) for example in parser.get_examples(cleandoc(doc))] for doc in (before, after)]
    except ValueError:
        return False
    return examples[0] == examples[1]


def _is_dead_return(node):
    """`if False: return <const>` - a statement that never runs."""
    return isinstance(node, ast.If) and isinstance(node.test, ast.Constant) and (node.test.value is False) and (not node.orelse) and (len(node.body) == 1) and isinstance(node.body[0], ast.Return) and (node.body[0].value is None or isinstance(node.body[0].value, ast.Constant))


def is_inert_function_edit(path, original, patched):
    """True for an optimizer edit that only adds dead returns or re-wraps docstrings."""
    if not path.startswith('sqlglot/optimizer/') or not path.endswith('.py'):
        return False
    if '__doc__' in original or 'getdoc' in original or 'inspect' in original:
        return False
    try:
        baseline = ast.parse(original, filename=path)
        candidate = ast.parse(patched, filename=path)
        compile(candidate, path, 'exec', dont_inherit=True)
    except (SyntaxError, ValueError):
        return False
    if len(baseline.body) != len(candidate.body):
        return False
    changed = False
    for before, after in zip(baseline.body, candidate.body):
        original_shape = ast.dump(before)
        if original_shape == ast.dump(after):
            continue
        if not (type(before) is type(after) is ast.FunctionDef and (not before.decorator_list) and (not after.decorator_list)):
            return False
        old_body = before.body
        new_body = after.body
        if len(new_body) > len(old_body):
            new_body = [statement for statement in new_body if not _is_dead_return(statement)]
            if ast.get_docstring(before) != ast.get_docstring(after):
                return False
        elif len(new_body) == len(old_body):
            old_doc = ast.get_docstring(before, clean=False)
            new_doc = ast.get_docstring(after, clean=False)
            if old_doc is None or new_doc is None or old_doc == new_doc or (old_doc.split() != new_doc.split()) or (not _same_doc_examples(old_doc, new_doc)):
                return False
            new_body = [old_body[0], *new_body[1:]]
        after.body = new_body
        if original_shape != ast.dump(after):
            return False
        changed = True
    return changed


# --- importers, pytest configuration, shadowing ------------------------------------------

def importer_tests(maps, names):
    """Tests that import any of the dotted module `names` (or live under such a tests/ dir)."""
    found = set()
    for name in names:
        rel = name.replace('.', '/')
        for candidate in (rel + '.py', rel + '/__init__.py'):
            importers = maps.meta['importers'].get(candidate)
            if importers:
                found.update(maps.tests(importers))
        if rel.partition('/')[0] == 'tests' and os.path.isdir(os.path.join(ROOT, rel)):
            found.update(tests_under(maps, rel))
    return found


PYTEST_OWN_CONFIGS = frozenset(('pytest.ini', '.pytest.ini', 'pytest.toml', '.pytest.toml'))
PYTEST_SECTION_RE = re.compile('^\\s*\\[(?:pytest|tool:pytest|tool\\.pytest[^\\]]*)\\]', re.M)


def pytest_config_scope(maps, fp, path, old_text):
    """Tests affected by a conftest/pytest configuration change; None if `path` is neither."""
    base_name = os.path.basename(path)
    if 'conftest.py' in (base_name, os.path.basename(fp.old_path or '')):
        if fp.status == 'add' and _conftest_harmless(fp.new_text):
            return set()
        return tests_under(maps, os.path.dirname(path))
    if base_name in PYTEST_OWN_CONFIGS:
        return tests_under(maps, '')
    if base_name in PYTEST_CONFIGS:
        if any((PYTEST_SECTION_RE.search(text) for text in (old_text, fp.new_text) if text)):
            return tests_under(maps, '')
    return None


def shadows_module(path):
    """True when an added top-level file would shadow an importable module."""
    import importlib.util
    if '/' in path or not path.endswith('.py'):
        return False
    name = path[:-3]
    try:
        return name in sys.stdlib_module_names or importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return True


# --- lambdas --------------------------------------------------------------------------------

def statement_around_lambda(old_lines, start, end, line, old_line, new_line):
    """Narrow a changed line inside a unit to its innermost enclosing statement.

    Only for units containing a lambda: returns None when the edit is inside
    the lambda body (or there is no lambda on the line), else the (first, last)
    line span of the smallest statement around `line` (the whole unit when the
    unit cannot be parsed).
    """
    text = old_lines[start - 1:end]
    if not any(('lambda' in unit_line for unit_line in text)):
        return None
    shift = start - 1
    source = '\n'.join(text)
    if text and text[0][:1] in (' ', '\t'):
        source = 'if 1:\n' + source
        shift = start - 2
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return (start, end)
    at = line - shift
    lambdas = [node for node in ast.walk(tree) if isinstance(node, ast.Lambda) and node.lineno <= at <= node.end_lineno]
    if not lambdas:
        return None
    old_bytes = old_line.encode('utf-8', 'surrogateescape')
    new_bytes = new_line.encode('utf-8', 'surrogateescape')
    prefix = 0
    while prefix < len(old_bytes) and prefix < len(new_bytes) and (old_bytes[prefix] == new_bytes[prefix]):
        prefix += 1
    suffix = 0
    while suffix < len(old_bytes) - prefix and suffix < len(new_bytes) - prefix and (old_bytes[len(old_bytes) - 1 - suffix] == new_bytes[len(new_bytes) - 1 - suffix]):
        suffix += 1
    edit_start, edit_end = (prefix, len(old_bytes) - suffix)
    inner = min(lambdas, key=lambda node: (node.end_lineno - node.lineno, -node.col_offset))
    inside = (at > inner.lineno or edit_start > inner.col_offset) and (at < inner.end_lineno or edit_end < inner.end_col_offset)
    if inside:
        return None
    best = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt) or (node is tree.body[0] and shift == start - 2):
            continue
        first = node.lineno
        decorators = getattr(node, 'decorator_list', None)
        if decorators:
            first = min(first, min((decorator.lineno for decorator in decorators)))
        last = node.end_lineno
        body = getattr(node, 'body', None)
        if isinstance(body, list) and body and isinstance(body[0], ast.AST):
            body_start = body[0].lineno
            body_decorators = getattr(body[0], 'decorator_list', None)
            if body_decorators:
                body_start = min(body_start, min((decorator.lineno for decorator in body_decorators)))
            if first <= at <= body_start - 1:
                last = body_start - 1
        if first <= at <= last and (best is None or last - first < best[1] - best[0]):
            best = (first, last)
    if best is None:
        return (start, end)
    return (best[0] + shift, best[1] + shift)
