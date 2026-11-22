"""The examples of a docstring as doctest finds them, without importing doctest (pure speed).

examples(doc) == [(e.source, e.want, e.exc_msg, e.options) for e in doctest.DocTestParser().get_examples(doc)],
and it raises ValueError exactly when get_examples does. The code is that of DocTestParser
(parse, _parse_example, _find_options, _min_indent, _check_prompt_blank, _check_prefix), of
Example.__init__ and of the option flags of Python 3.13's doctest module, unchanged but for the
names. Importing doctest costs about 0.01 s of CPU (it imports pdb, unittest, inspect, ...).
"""
import re

OPTIONFLAGS_BY_NAME = {}


def register_optionflag(name):
    # Create a new flag unless `name` is already known.
    return OPTIONFLAGS_BY_NAME.setdefault(name, 1 << len(OPTIONFLAGS_BY_NAME))


for _name in ('DONT_ACCEPT_TRUE_FOR_1', 'DONT_ACCEPT_BLANKLINE', 'NORMALIZE_WHITESPACE', 'ELLIPSIS', 'SKIP',
              'IGNORE_EXCEPTION_DETAIL', 'REPORT_UDIFF', 'REPORT_CDIFF', 'REPORT_NDIFF',
              'REPORT_ONLY_FIRST_FAILURE', 'FAIL_FAST'):
    register_optionflag(_name)

_EXAMPLE_RE = re.compile(r'''
    # Source consists of a PS1 line followed by zero or more PS2 lines.
    (?P<source>
        (?:^(?P<indent> [ ]*) >>>    .*)    # PS1 line
        (?:\n           [ ]*  \.\.\. .*)*)  # PS2 lines
    \n?
    # Want consists of any non-blank lines that do not start with PS1.
    (?P<want> (?:(?![ ]*$)    # Not a blank line
                 (?![ ]*>>>)  # Not a line starting with PS1
                 .+$\n?       # But any other line
              )*)
    ''', re.MULTILINE | re.VERBOSE)

_EXCEPTION_RE = re.compile(r"""
    # Grab the traceback header.  Different versions of Python have
    # said different things on the first traceback line.
    ^(?P<hdr> Traceback\ \(
        (?: most\ recent\ call\ last
        |   innermost\ last
        ) \) :
    )
    \s* $                # toss trailing whitespace on the header.
    (?P<stack> .*?)      # don't blink: absorb stuff until...
    ^ (?P<msg> \w+ .*)   #     a line *starts* with alphanum.
    """, re.VERBOSE | re.MULTILINE | re.DOTALL)

_IS_BLANK_OR_COMMENT = re.compile(r'^[ ]*(#.*)?$').match

_OPTION_DIRECTIVE_RE = re.compile(r'#\s*doctest:\s*([^\n\'"]*)$', re.MULTILINE)

_INDENT_RE = re.compile(r'^([ ]*)(?=\S)', re.MULTILINE)


def examples(string, name='<string>'):
    """[(source, want, exc_msg, options)] of the doctest examples of string (DocTestParser.get_examples)."""
    string = string.expandtabs()
    # If all lines begin with the same indentation, then strip it.
    min_indent = _min_indent(string)
    if min_indent > 0:
        string = '\n'.join([l[min_indent:] for l in string.split('\n')])

    output = []
    charno, lineno = 0, 0
    # Find all doctest examples in the string:
    for m in _EXAMPLE_RE.finditer(string):
        # Update lineno (lines before this example)
        lineno += string.count('\n', charno, m.start())
        # Extract info from the regexp match.
        (source, options, want, exc_msg) = _parse_example(m, name, lineno)
        # Create an Example, and add it to the list.
        if not _IS_BLANK_OR_COMMENT(source):
            # Example.__init__
            if not source.endswith('\n'):
                source += '\n'
            if want and not want.endswith('\n'):
                want += '\n'
            if exc_msg is not None and not exc_msg.endswith('\n'):
                exc_msg += '\n'
            output.append((source, want, exc_msg, options))
        # Update lineno (lines inside this example)
        lineno += string.count('\n', m.start(), m.end())
        # Update charno.
        charno = m.end()
    return output


def _parse_example(m, name, lineno):
    # Get the example's indentation level.
    indent = len(m.group('indent'))

    # Divide source into lines; check that they're properly
    # indented; and then strip their indentation & prompts.
    source_lines = m.group('source').split('\n')
    _check_prompt_blank(source_lines, indent, name, lineno)
    _check_prefix(source_lines[1:], ' '*indent + '.', name, lineno)
    source = '\n'.join([sl[indent+4:] for sl in source_lines])

    # Divide want into lines; check that it's properly indented; and
    # then strip the indentation.  Spaces before the last newline should
    # be preserved, so plain rstrip() isn't good enough.
    want = m.group('want')
    want_lines = want.split('\n')
    if len(want_lines) > 1 and re.match(r' *$', want_lines[-1]):
        del want_lines[-1]  # forget final newline & spaces after it
    _check_prefix(want_lines, ' '*indent, name,
                  lineno + len(source_lines))
    want = '\n'.join([wl[indent:] for wl in want_lines])

    # If `want` contains a traceback message, then extract it.
    m = _EXCEPTION_RE.match(want)
    if m:
        exc_msg = m.group('msg')
    else:
        exc_msg = None

    # Extract options from the source.
    options = _find_options(source, name, lineno)

    return source, options, want, exc_msg


def _find_options(source, name, lineno):
    options = {}
    # (note: with the current regexp, this will match at most once:)
    for m in _OPTION_DIRECTIVE_RE.finditer(source):
        option_strings = m.group(1).replace(',', ' ').split()
        for option in option_strings:
            if (option[0] not in '+-' or
                    option[1:] not in OPTIONFLAGS_BY_NAME):
                raise ValueError('line %r of the doctest for %s '
                                 'has an invalid option: %r' %
                                 (lineno+1, name, option))
            flag = OPTIONFLAGS_BY_NAME[option[1:]]
            options[flag] = (option[0] == '+')
    if options and _IS_BLANK_OR_COMMENT(source):
        raise ValueError('line %r of the doctest for %s has an option '
                         'directive on a line with no example: %r' %
                         (lineno, name, source))
    return options


def _min_indent(s):
    "Return the minimum indentation of any non-blank line in `s`"
    indents = [len(indent) for indent in _INDENT_RE.findall(s)]
    if len(indents) > 0:
        return min(indents)
    else:
        return 0


def _check_prompt_blank(lines, indent, name, lineno):
    for i, line in enumerate(lines):
        if len(line) >= indent+4 and line[indent+3] != ' ':
            raise ValueError('line %r of the docstring for %s '
                             'lacks blank after %s: %r' %
                             (lineno+i+1, name,
                              line[indent:indent+3], line))


def _check_prefix(lines, prefix, name, lineno):
    for i, line in enumerate(lines):
        if line and not line.startswith(prefix):
            raise ValueError('line %r of the docstring for %s has '
                             'inconsistent leading whitespace: %r' %
                             (lineno+i+1, name, line))
