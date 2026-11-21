"""Parse and apply unified diffs, compare code objects, and overlay patched files.

- `parse_patch` / `apply_file_patch`: a strict git-diff reader; anything it does
  not fully understand marks the FilePatch as not ok (with a reason in `why`).
- code comparison helpers (`code_equal`, `changed_lines`, ...) decide which
  functions a patch really changes, and `plan_code_swaps` turns pairs of old/new
  code objects into in-place `__code__` swaps of the live functions.
- `install_import_overlay` / `install_open_overlay` make the patched tree
  visible to imports and to open() without touching the read-only checkout.
"""
from __future__ import annotations

import gc
import marshal
import os
import re
import sys
import types

CO_OPTIMIZED = 1
FUTURE_ANNOTATIONS = 16777216
HUNK_RE = re.compile('^@@ -(\\d+)(?:,(\\d+))? \\+(\\d+)(?:,(\\d+))? @@')


class FilePatch:
    """One file section of a patch.

    `hunks` are (old start, old count, lines); `groups` (filled by
    apply_file_patch) are runs of changed lines as mutable lists
    [old line, removed count, new line, added count, removed lines, added lines].
    """
    __slots__ = ('old_path', 'new_path', 'status', 'hunks', 'groups', 'new_text', 'binary', 'ok', 'why')

    def __init__(self):
        self.old_path = None
        self.new_path = None
        self.status = 'modify'
        self.hunks = []
        self.groups = []
        self.new_text = None
        self.binary = False
        self.ok = True
        self.why = None

    def fail(self, why):
        if self.ok:
            self.ok = False
            self.why = why


_FILE_MODES = ('100644', '100755')


def _strip_prefix(path):
    """'a/x/y.py' -> 'x/y.py'; None when there is no prefix directory."""
    slash = path.find('/')
    return path[slash + 1:] if slash > 0 else None


def _header_path(text, section):
    """The repository path named on a ---/+++ line ('' for /dev/null)."""
    if '\t' in text:
        text = text.split('\t', 1)[0]
    if text.endswith('\r') or text.startswith('"') or (not text):
        section.fail('path form %r' % text[:80])
        return None
    if text == '/dev/null':
        return ''
    path = _strip_prefix(text)
    if path is None:
        section.fail('path without a prefix %r' % text[:80])
    return path


def _check_mode(section, line):
    if line.split()[-1] not in _FILE_MODES:
        section.fail('file mode %r' % line)


def _read_hunk(section, lines, index, header_match):
    """Read one hunk whose header line is lines[index - 1]; returns the next index."""
    old_start = int(header_match.group(1))
    old_count = int(header_match.group(2)) if header_match.group(2) is not None else 1
    new_count = int(header_match.group(4)) if header_match.group(4) is not None else 1
    body = []
    old_seen = new_seen = 0
    total = len(lines)
    while index < total and (old_seen < old_count or new_seen < new_count):
        line = lines[index]
        marker = line[:1]
        if marker == ' ' or line == '':
            old_seen += 1
            new_seen += 1
            body.append(' ' + line[1:])
        elif marker == '-':
            old_seen += 1
            body.append(line)
        elif marker == '+':
            new_seen += 1
            body.append(line)
        elif marker == '\\':
            body.append(line)
        else:
            break
        index += 1
    if old_seen != old_count or new_seen != new_count:
        section.fail('hunk line counts')
    if index < total and lines[index].startswith('\\'):
        body.append(lines[index])
        index += 1
    section.hunks.append((old_start, old_count, body))
    return index


def _read_extended_header(section, header, line):
    """Handle one git extended header line (mode, rename, binary, ...)."""
    if line.startswith('index '):
        pass
    elif line.startswith(('old mode ', 'new mode ')):
        header['mode'] = True
        _check_mode(section, line)
    elif line.startswith('new file mode '):
        section.status = 'add'
        _check_mode(section, line)
    elif line.startswith('deleted file mode '):
        section.status = 'delete'
        _check_mode(section, line)
    elif line.startswith(('similarity index ', 'dissimilarity index ')):
        pass
    elif line.startswith(('rename from ', 'rename to ', 'copy from ', 'copy to ')):
        operation, side, path = line.split(' ', 2)
        if path.startswith('"') or path.endswith('\r') or (not path):
            section.fail('path form %r' % path[:80])
        header[side] = path
        section.status = operation
    elif line.startswith('GIT binary patch') or line.startswith('Binary files '):
        section.binary = True
        section.fail('binary')
    elif line == '':
        pass
    else:
        section.fail('header line %r' % line[:60])


def _git_line_path(git_paths):
    """The path of a 'diff --git a/p b/p' line when both sides name the same path."""
    middle = (len(git_paths) - 1) // 2
    if len(git_paths) % 2 == 1 and git_paths[middle] == ' ':
        old, new = (_strip_prefix(git_paths[:middle]), _strip_prefix(git_paths[middle + 1:]))
        if old is not None and old == new:
            return old
    return None


def _resolve_paths(section, header, owners):
    """Settle status/old_path/new_path of a parsed section and validate them."""
    minus, plus = (header.get('minus'), header.get('plus'))
    if 'git' in header:
        git_paths = header['git']
        git_path = _git_line_path(git_paths)
        if git_paths.startswith('"') or git_paths.endswith('"'):
            section.fail('quoted path')
    else:
        git_path = None
        if not section.hunks:
            section.fail('plain section without hunks')
    if minus == '' and plus == '':
        section.fail('both sides /dev/null')
    if minus == '':
        if section.status not in ('modify', 'add'):
            section.fail('status')
        section.status = 'add'
    if plus == '':
        if section.status not in ('modify', 'delete'):
            section.fail('status')
        section.status = 'delete'
    if section.status in ('rename', 'copy'):
        section.old_path, section.new_path = (header.get('from'), header.get('to'))
        if not section.old_path or not section.new_path or section.old_path == section.new_path:
            section.fail('rename/copy names')
        if minus and minus != section.old_path or (plus and plus != section.new_path):
            section.fail('rename/copy names differ from ---/+++')
    else:
        path = plus or minus or git_path
        if not path or (minus and plus and (minus != plus)) or (git_path and path != git_path) or ('git' in header and git_path is None):
            section.fail('file names')
        section.old_path = section.new_path = path
    if not section.hunks and (not section.binary):
        if 'minus' in header or (section.status == 'modify' and (not header.get('mode'))):
            section.fail('section without hunks')
    for path in {section.new_path} if section.status in ('copy', 'add') else {section.old_path, section.new_path}:
        if path is None:
            continue
        if path in owners and owners[path] is not section:
            section.fail('path in two sections')
            owners[path].fail('path in two sections')
        owners[path] = section


def parse_patch(text: str) -> list[FilePatch]:
    """Split a unified/git diff into FilePatch sections (in patch order)."""
    sections: list[FilePatch] = []
    headers = []
    section = None
    header = None
    phase = None
    lines = text.split('\n')
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index][:-1] if lines[index].endswith('\r') else lines[index]
        if line.startswith('diff --git '):
            section = FilePatch()
            header = {'git': line[len('diff --git '):]}
            sections.append(section)
            headers.append(header)
            phase = 'head'
            index += 1
            continue
        if line.startswith('--- ') and index + 1 < total and lines[index + 1].startswith('+++ '):
            if section is None or phase != 'head' or 'minus' in header:
                section = FilePatch()
                header = {}
                sections.append(section)
                headers.append(header)
            header['minus'] = _header_path(line[4:], section)
            header['plus'] = _header_path(lines[index + 1][4:].removesuffix('\r'), section)
            phase = 'body'
            index += 2
            continue
        if section is None or phase == 'tail':
            index += 1
            continue
        if line.startswith('@@'):
            match = HUNK_RE.match(line)
            if not match or phase not in ('body', 'hunks'):
                section.fail('hunk header %r' % line[:60])
                index += 1
                continue
            index = _read_hunk(section, lines, index + 1, match)
            phase = 'hunks'
            continue
        if phase == 'head':
            _read_extended_header(section, header, line)
        elif line == '-- ':
            phase = 'tail'
        elif line[:1] in ('+', '-', ' ') or line.startswith('GIT binary patch') or line.startswith('Binary files '):
            section.fail('line outside a hunk %r' % line[:60])
        index += 1
    owners = {}
    for section, header in zip(sections, headers):
        _resolve_paths(section, header, owners)
    return sections


def apply_file_patch(fp: FilePatch, old_text: str | None) -> None:
    """Apply `fp` to `old_text`, setting fp.new_text and fp.groups (fp.ok=False on mismatch)."""
    if fp.binary:
        fp.ok = False
        return
    if fp.status == 'delete':
        fp.new_text = None
        return
    old_lines = old_text.split('\n') if old_text is not None else []
    old_ends_with_newline = old_text is not None and old_text.endswith('\n')
    if old_ends_with_newline:
        old_lines.pop()
    new_lines: list[str] = []
    position = 0
    ends_with_newline = old_ends_with_newline if old_text else True
    for hunk_start, hunk_old_count, hunk_lines in fp.hunks:
        start = hunk_start - 1 if hunk_old_count > 0 else hunk_start
        if old_text is None:
            start = 0
        if start < position or start > len(old_lines):
            fp.ok = False
            return
        new_lines.extend(old_lines[position:start])
        position = start
        group = None
        previous_marker = None
        for hunk_line in hunk_lines:
            marker = hunk_line[:1]
            content = hunk_line[1:]
            if marker == ' ':
                if position >= len(old_lines) or old_lines[position] != content:
                    fp.ok = False
                    return
                new_lines.append(content)
                position += 1
                group = None
            elif marker == '-':
                if position >= len(old_lines) or old_lines[position] != content:
                    fp.ok = False
                    return
                if group is None:
                    group = [position + 1, 0, len(new_lines) + 1, 0, [], []]
                    fp.groups.append(group)
                group[1] += 1
                group[4].append(content)
                position += 1
            elif marker == '+':
                if group is None:
                    group = [position + 1, 0, len(new_lines) + 1, 0, [], []]
                    fp.groups.append(group)
                group[3] += 1
                group[5].append(content)
                new_lines.append(content)
            elif marker == '\\':
                if previous_marker == '+':
                    ends_with_newline = False
                elif previous_marker == '-':
                    ends_with_newline = True
                elif previous_marker == ' ':
                    ends_with_newline = False
            previous_marker = marker if marker != '\\' else previous_marker
    new_lines.extend(old_lines[position:])
    fp.new_text = '\n'.join(new_lines) + ('\n' if ends_with_newline and new_lines else '')


# --- code object comparison ----------------------------------------------------

def const_equal(x, y) -> bool:
    """Constants are equal when type and repr agree (so 0 != 0.0 != False, -0.0 != 0.0)."""
    return type(x) is type(y) and repr(x) == repr(y)


def code_equal(x: types.CodeType, y: types.CodeType) -> bool:
    """Deep equality of two code objects, ignoring line numbers and file names."""
    if x.co_code != y.co_code or x.co_names != y.co_names or x.co_varnames != y.co_varnames or (x.co_freevars != y.co_freevars) or (x.co_cellvars != y.co_cellvars) or (x.co_argcount != y.co_argcount) or (x.co_posonlyargcount != y.co_posonlyargcount) or (x.co_kwonlyargcount != y.co_kwonlyargcount) or (x.co_flags != y.co_flags) or (x.co_exceptiontable != y.co_exceptiontable) or (x.co_name != y.co_name) or (x.co_qualname != y.co_qualname) or (len(x.co_consts) != len(y.co_consts)):
        return False
    for const_x, const_y in zip(x.co_consts, y.co_consts):
        is_code = isinstance(const_x, types.CodeType)
        if is_code != isinstance(const_y, types.CodeType):
            return False
        if is_code:
            if not code_equal(const_x, const_y):
                return False
        elif not const_equal(const_x, const_y):
            return False
    return True


def changed_lines(x: types.CodeType, y: types.CodeType):
    """Source lines whose instructions differ between two same-shaped code objects.

    None when the code objects differ in shape (so a line set cannot be given).
    """
    import dis
    if x.co_varnames != y.co_varnames or x.co_freevars != y.co_freevars or x.co_cellvars != y.co_cellvars or (x.co_argcount != y.co_argcount) or (x.co_posonlyargcount != y.co_posonlyargcount) or (x.co_kwonlyargcount != y.co_kwonlyargcount) or (x.co_flags != y.co_flags) or (x.co_exceptiontable != y.co_exceptiontable) or (x.co_name != y.co_name) or (x.co_qualname != y.co_qualname) or (len(x.co_consts) != len(y.co_consts)) or (len(x.co_code) != len(y.co_code)):
        return None
    instructions_x = list(dis.get_instructions(x))
    instructions_y = list(dis.get_instructions(y))
    if len(instructions_x) != len(instructions_y):
        return None
    lines = set()
    consts_used = set()
    for a, b in zip(instructions_x, instructions_y):
        if a.offset != b.offset:
            return None
        if a.opcode in dis.hasconst:
            consts_used.add(a.arg)
        same = a.opname == b.opname
        if same:
            if isinstance(a.argval, types.CodeType) or isinstance(b.argval, types.CodeType):
                if not (isinstance(a.argval, types.CodeType) and isinstance(b.argval, types.CodeType)):
                    same = False
                else:
                    inner = changed_lines(a.argval, b.argval)
                    if inner is None:
                        return None
                    lines |= inner
            elif a.opcode in dis.hasconst:
                same = const_equal(a.argval, b.argval)
            elif a.opcode in dis.hasname:
                same = const_equal(a.argval, b.argval) and a.arg & 3 == b.arg & 3
            elif a.opcode in dis.haslocal or a.opcode in dis.hasfree:
                same = const_equal(a.argval, b.argval)
            elif a.opcode in dis.hasjump:
                same = a.argval == b.argval
            else:
                same = a.arg == b.arg
        if not same:
            line_a, line_b = (a.positions.lineno, b.positions.lineno)
            if line_a is None or line_b is None:
                return None
            lines.add(line_a)
            lines.add(line_b)
    for index, (const_a, const_b) in enumerate(zip(x.co_consts, y.co_consts)):
        if index not in consts_used and (not isinstance(const_a, types.CodeType)) and (not const_equal(const_a, const_b)):
            return None
    return lines


def equal_at_definition_time(a: types.CodeType, b: types.CodeType, pairs: list) -> bool:
    """True when two module/class bodies do the same thing when executed.

    Nested function bodies are not compared; their (old, new) code pairs are
    appended to `pairs` instead, for the caller to swap or compare.
    """
    if a.co_code != b.co_code or a.co_names != b.co_names or a.co_exceptiontable != b.co_exceptiontable or (len(a.co_consts) != len(b.co_consts)) or (a.co_flags != b.co_flags):
        return False
    for const_a, const_b in zip(a.co_consts, b.co_consts):
        is_code = isinstance(const_a, types.CodeType)
        if is_code != isinstance(const_b, types.CodeType):
            return False
        if is_code:
            optimized = const_a.co_flags & CO_OPTIMIZED
            if optimized != const_b.co_flags & CO_OPTIMIZED:
                return False
            if optimized:
                if const_a.co_name != const_b.co_name or const_a.co_qualname != const_b.co_qualname or len(const_a.co_freevars) != len(const_b.co_freevars):
                    return False
                pairs.append((const_a, const_b))
            elif not equal_at_definition_time(const_a, const_b, pairs):
                return False
        elif not const_equal(const_a, const_b):
            return False
    return True


def compile_unit(lines: list[str], start: int, end: int, class_path, filename: str, flags: int, import_names=()):
    """Compile lines start..end (1-based) of a file as in compile_lines."""
    return compile_lines(lines[start - 1:end], start, class_path, filename, flags, import_names)


def compile_lines(unit_lines, start: int, class_path, filename: str, flags: int, import_names=()):
    """Compile a top-level unit at its real line numbers, nested in its classes.

    Blank lines pad the source so line numbers match; `class X:` headers
    recreate the qualified names. `import_names` are imported at the end so the
    names count as globals. None when there is no room for the class headers.
    """
    depth = len(class_path)
    padding = start - 1 - depth
    if padding < 0:
        return None
    class_headers = ''.join((' ' * level + 'class ' + name + ':\n' for level, name in enumerate(class_path)))
    source = '\n' * padding + class_headers + '\n'.join(unit_lines) + '\n'
    if import_names:
        source += 'import ' + ', '.join(import_names) + '\n'
    return compile(source, filename, 'exec', flags=flags, dont_inherit=True)


class FunctionIndex:
    """All live functions defined in files under `root`, by (file, first line, name)."""

    def __init__(self, root):
        index: dict = {}
        function_type = types.FunctionType
        for obj in gc.get_objects():
            if type(obj) is function_type:
                code = obj.__code__
                if code.co_filename.startswith(root):
                    index.setdefault((code.co_filename, code.co_firstlineno, code.co_name), []).append(obj)
        self.idx = index

    def live(self, code):
        """Functions currently running exactly `code`."""
        return [func for func in self.idx.get((code.co_filename, code.co_firstlineno, code.co_name), ()) if func.__code__ == code]

    def any_at(self, code):
        """Functions defined at the same place as `code`, whatever their current code."""
        return self.idx.get((code.co_filename, code.co_firstlineno, code.co_name), ())


def _nested_codes(code):
    return [const for const in code.co_consts if isinstance(const, types.CodeType)]


def plan_code_swaps(pairs, index: FunctionIndex):
    """[(function, new code)] that turn the old code of `pairs` into the new code.

    None when a swap is impossible (a function was already changed, closure
    sizes differ, or a nested function that exists live disappears).
    """
    swaps = []
    pending = list(pairs)
    while pending:
        old_code, new_code = pending.pop()
        if code_equal(old_code, new_code):
            continue
        defined_here = index.any_at(old_code)
        running_old = [func for func in defined_here if func.__code__ == old_code]
        if len(running_old) != len(defined_here):
            return None
        if running_old and len(old_code.co_freevars) != len(new_code.co_freevars):
            return None
        for func in running_old:
            swaps.append((func, new_code))
        old_children = _nested_codes(old_code)
        new_children = _nested_codes(new_code)
        if len(old_children) != len(new_children) or any((old_child.co_name != new_child.co_name for old_child, new_child in zip(old_children, new_children))):
            orphans = list(old_children)
            while orphans:
                orphan = orphans.pop()
                if index.any_at(orphan):
                    return None
                orphans.extend(_nested_codes(orphan))
            continue
        pending.extend(zip(old_children, new_children))
    return swaps


# --- overlays ------------------------------------------------------------------

class OverlayFinder:
    """meta_path finder serving patched sqlglot/tests modules from memory."""

    def __init__(self, root: str, files: dict):
        self.root = root
        self.files = files

    def find_spec(self, name, path=None, target=None):
        import importlib.util
        top = name.partition('.')[0]
        if top not in ('sqlglot', 'tests'):
            return None
        rel = name.replace('.', '/')
        for candidate, is_package in ((rel + '/__init__.py', True), (rel + '.py', False)):
            if candidate in self.files:
                text = self.files[candidate]
                if text is None:
                    continue
                full_path = os.path.join(self.root, candidate)
                loader = _MemoryLoader(full_path, text)
                return importlib.util.spec_from_file_location(name, full_path, loader=loader, submodule_search_locations=[os.path.dirname(full_path)] if is_package else None)
            if os.path.exists(os.path.join(self.root, candidate)):
                return None
        return None

    def hidden(self, name):
        rel = name.replace('.', '/')
        return self.files.get(rel + '.py', 1) is None or self.files.get(rel + '/__init__.py', 1) is None


# Set by the coordinator: keep each overlay module's code object on its loader.
KEEP_CODE = False


class _MemoryLoader:

    def __init__(self, path, text):
        self.path = path
        self.text = text
        self.code = None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        code = compile(self.text.encode('utf-8', 'surrogateescape'), self.path, 'exec', dont_inherit=True)
        code = marshal.loads(marshal.dumps(code))
        if KEEP_CODE:
            self.code = code
        exec(code, module.__dict__)

    def get_source(self, name):
        return self.text

    def get_filename(self, name):
        return self.path


class _HideFinder:
    """meta_path finder making deleted modules unimportable."""

    def __init__(self, names):
        self.names = names

    def find_spec(self, name, path=None, target=None):
        if name in self.names:
            raise ModuleNotFoundError(f'No module named {name!r}', name=name)
        return None


def install_import_overlay(root: str, files: dict):
    """Make imports see `files` (rel path -> new text, None = deleted)."""
    deleted = set()
    for rel, text in files.items():
        if text is None and rel.endswith('.py'):
            module_name = rel[:-3].replace('/', '.')
            if module_name.endswith('.__init__'):
                module_name = module_name[:-len('.__init__')]
            deleted.add(module_name)
    sys.meta_path.insert(0, OverlayFinder(root, files))
    if deleted:
        sys.meta_path.insert(0, _HideFinder(deleted))
    import importlib
    importlib.invalidate_caches()


def install_open_overlay(root: str, files: dict):
    """Make open()/io.open_code() read `files` instead of the checkout.

    Returns `read_old`, a context manager under which the real files are seen.
    """
    import builtins
    import contextlib
    import io
    table = {os.path.join(root, rel): text for rel, text in files.items()}
    real_open = builtins.open
    active = [True]

    def patched_open(file, mode='r', *args, **kwargs):
        try:
            full_path = os.path.abspath(os.fspath(file)) if isinstance(file, (str, os.PathLike)) else None
        except TypeError:
            full_path = None
        if active[0] and full_path is not None and (full_path in table):
            text = table[full_path]
            if text is None:
                raise FileNotFoundError(2, 'No such file or directory', str(file))
            if 'w' in mode or 'a' in mode or '+' in mode or ('x' in mode):
                return real_open(file, mode, *args, **kwargs)
            if 'b' in mode:
                return io.BytesIO(text.encode('utf-8', 'surrogateescape'))
            newline = kwargs.get('newline')
            encoding = kwargs.get('encoding', args[1] if len(args) > 1 else None)
            if (encoding or 'utf-8').lower().replace('_', '-') in ('utf-8', 'utf8') and text.isascii():
                return io.StringIO(text, newline=newline)
            errors = kwargs.get('errors', args[2] if len(args) > 2 else None)
            return io.TextIOWrapper(io.BytesIO(text.encode('utf-8', 'surrogateescape')), encoding=encoding or 'utf-8', errors=errors, newline=newline)
        return real_open(file, mode, *args, **kwargs)
    builtins.open = patched_open
    io.open = patched_open
    real_open_code = io.open_code

    def patched_open_code(path):
        try:
            full_path = os.path.abspath(os.fspath(path))
        except TypeError:
            full_path = None
        if active[0] and full_path is not None and (full_path in table):
            text = table[full_path]
            if text is None:
                raise FileNotFoundError(2, 'No such file or directory', str(path))
            return io.BytesIO(text.encode('utf-8', 'surrogateescape'))
        return real_open_code(path)
    io.open_code = patched_open_code

    @contextlib.contextmanager
    def read_old():
        active[0] = False
        try:
            yield
        finally:
            active[0] = True
    return read_old


def run_single_test(test_id: str, failfast: bool=True) -> bool:
    """Run one test id in this process; True when it fails (or cannot be loaded)."""
    import importlib
    import unittest
    path, class_name, method_name = test_id.split('::')
    module_name = path[:-3].replace('/', '.')
    try:
        module = importlib.import_module(module_name)
        case = getattr(module, class_name)(method_name)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        return True
    result = unittest.TestResult()
    result.failfast = failfast
    suite = unittest.TestSuite([case])
    try:
        suite.run(result)
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt):
            raise
        return True
    return bool(result.failures or result.errors or result.unexpectedSuccesses)
