"""Reading a commit's unified diff: the paths it touches and the edited text of each file."""
import re
from pathlib import Path

from settings import REPO

HUNK_HEADER = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@')
# Top-level directories that tests import or that build the package.
IMPORTED_ROOTS = ('sqlglot', 'tests', 'sqlglotc', 'sqlglot-integration-tests')
# File names that configure the interpreter, pytest or a package wherever they live.
CONFIG_FILES = {'setup.cfg', 'pyproject.toml', 'tox.ini', 'pytest.ini', 'setup.py', 'conftest.py', 'sitecustomize.py', 'usercustomize.py', '__init__.py'}
COPY_AND_RENAME_PREFIXES = ('rename from ', 'rename to ', 'copy from ', 'copy to ')
STRUCTURAL_HEADERS = ('rename ', 'copy ', 'new file', 'deleted file', 'Binary files', 'GIT binary')


def is_inert(path):
    """A relative path outside the imported packages that no interpreter or test runner reads."""
    parts = Path(path).parts
    return (
        bool(parts)
        and parts[0] not in IMPORTED_ROOTS
        and parts[-1] not in CONFIG_FILES
        and not parts[-1].endswith('.pth')
        and '..' not in parts
        and not Path(path).is_absolute()
    )


def header_lines(lines):
    """The lines of a file block before its first hunk."""
    for position, line in enumerate(lines):
        if line.startswith('@@'):
            return lines[:position]
    return lines


def side_paths(block):
    """Every path named by a file block's header (both sides, renames and copies)."""
    git_names = block.split('\n', 1)[0].split()
    header = header_lines(block.splitlines())
    old_names = [line[4:].strip() for line in header if line.startswith('--- ')]
    new_names = [line[4:].strip() for line in header if line.startswith('+++ ')]
    paths = set()
    for name in old_names + new_names + git_names:
        if name.startswith(('a/', 'b/')):
            paths.add(name[2:])
    for line in header:
        for prefix in COPY_AND_RENAME_PREFIXES:
            if line.startswith(prefix):
                paths.add(line[len(prefix):].strip())
    return paths


def file_edits(diff, skipped=None):
    """Yield (path, base_text, revised_text, removed_lines, inserted_lines) per edited text file.

    removed_lines are 1-based base line numbers that were deleted or replaced; inserted_lines
    are base positions where lines were added without replacing anything. Blocks that create,
    delete, rename or copy files raise ValueError, unless every path involved is inert and a
    `skipped` set is given, in which case those paths are collected there instead.
    """
    for block in re.split('(?m)^diff --git ', diff)[1:]:
        lines = block.splitlines(keepends=True)
        header = header_lines(lines)
        old_names = [line[4:].strip() for line in header if line.startswith('--- ')]
        new_names = [line[4:].strip() for line in header if line.startswith('+++ ')]
        in_place_edit = (
            len(old_names) == len(new_names) == 1
            and old_names[0].startswith('a/')
            and new_names[0] == 'b/' + old_names[0][2:]
            and not any(line.startswith(STRUCTURAL_HEADERS) for line in header)
        )
        if not in_place_edit:
            paths = side_paths(block)
            if skipped is not None and paths and all(is_inert(path) for path in paths) and '"' not in block.split('\n', 1)[0]:
                skipped.update(paths)
                continue
            raise ValueError('new, removed, renamed or binary file')
        path = old_names[0][2:]
        if Path(path).is_absolute() or '..' in Path(path).parts:
            raise ValueError('invalid source path')
        base_lines = (REPO / path).read_text(encoding='utf8').splitlines(keepends=True)
        revised_lines, removed, inserted = [], set(), set()
        cursor = 0
        in_hunk = False
        follows_removal = False
        for line in lines:
            hunk = HUNK_HEADER.match(line)
            if hunk:
                start = int(hunk[1]) - (hunk[2] != '0')
                if start < cursor or start > len(base_lines):
                    raise ValueError('hunk range')
                revised_lines.extend(base_lines[cursor:start])
                cursor = start
                in_hunk = True
                follows_removal = False
            elif in_hunk and line.startswith((' ', '-')):
                if line[1:].rstrip('\n') != base_lines[cursor].rstrip('\n'):
                    raise ValueError('unmatched context')
                if line.startswith('-'):
                    removed.add(cursor + 1)
                    follows_removal = True
                else:
                    revised_lines.append(base_lines[cursor])
                    follows_removal = False
                cursor += 1
            elif in_hunk and line.startswith('+'):
                if not follows_removal:
                    inserted.add(cursor + 1)
                revised_lines.append(line[1:])
        if not in_hunk:
            raise ValueError('no text hunks')
        revised_lines.extend(base_lines[cursor:])
        yield path, ''.join(base_lines), ''.join(revised_lines), removed, inserted
