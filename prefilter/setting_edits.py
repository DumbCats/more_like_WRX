"""Recognizers for edits to settings: constants, configuration tables, defaults and callbacks.

Each recognizer returns the mask of tests that can observe the edit, or None when the edit
is not of its kind (the caller then tries the next one).
"""
import ast

from ast_tools import baseline_dump, child_mask, spanning_nodes, unchanged_with_original

SCALAR_TYPES = (int, bool, str, float)
CALLBACK_PACKAGES = ('sqlglot/parsers/', 'sqlglot/generators/')
SETTING_PACKAGES = ('sqlglot/parsers/', 'sqlglot/generators/', 'sqlglot/typing/', 'sqlglot/dialects/')
COPYING_FACTORIES = {'alias_', 'to_identifier', 'maybe_parse'}


def module_edit(path, before, after, removed, maps):
    """A scalar module-level constant of a dialect/parser/generator/typing module changed."""
    if not removed or not path.startswith(SETTING_PACKAGES):
        return None
    for old, new in zip(before.body, after.body):
        if type(old) is not type(new) or not isinstance(old, (ast.Assign, ast.AnnAssign)):
            continue
        if not old.lineno <= min(removed) <= max(removed) <= old.end_lineno:
            continue
        targets = old.targets if isinstance(old, ast.Assign) else [old.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        readers = maps['globals'].get(f'{path}:{old.lineno}:{targets[0].id}')
        if readers is None or not isinstance(old.value, ast.Constant) or not isinstance(new.value, ast.Constant):
            continue
        if type(old.value.value) not in (str, int, bool, float) or type(old.value.value) is not type(new.value.value):
            continue
        if unchanged_with_original(after, baseline_dump(before), new, {'value': old.value}):
            return int(readers, 16) | child_mask(path, maps)
    return None


def _is_escape_table(value):
    return all(isinstance(entry, tuple) and len(entry) == 4 and all(type(item) is int for item in entry) for entry in value)


def config_edit(path, before, after, removed, maps):
    """A profiled class setting (scalar, arg_types flags or NUMERIC_ESCAPES entries) changed."""
    if not removed:
        return None
    first, last = min(removed), max(removed)
    old_assignments = {
        node.lineno: node
        for node in spanning_nodes(before, first, last)
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.lineno <= first <= last <= node.end_lineno
    }
    for new in spanning_nodes(after, first, last):
        if not isinstance(new, ast.Assign) or new.lineno not in old_assignments:
            continue
        old = old_assignments[new.lineno]
        name = old.targets[0].id
        config = maps['config'].get(f'{path}:{old.lineno}:{name}')
        arguments = maps['arguments'].get(f'{path}:{old.lineno}') if name == 'arg_types' else None
        if config is None and arguments is None or not unchanged_with_original(after, baseline_dump(before), new, {'value': old.value}):
            continue
        try:
            old_value, new_value = ast.literal_eval(old.value), ast.literal_eval(new.value)
        except (ValueError, TypeError):
            continue
        if (
            arguments is not None
            and isinstance(old_value, dict)
            and isinstance(new_value, dict)
            and list(old_value) == list(new_value)
            and all(type(flag) is bool for flag in (*old_value.values(), *new_value.values()))
        ):
            flipped = [argument for argument in old_value if old_value[argument] != new_value[argument]]
            masks = [arguments.get(argument, '0') for argument in ['!errors', *flipped]]
            if 'unknown' in masks:
                return None
            mask = child_mask(path, maps)
            for argument_hex in masks:
                mask |= int(argument_hex, 16)
            return mask
        if config is None:
            continue
        if type(old_value) in SCALAR_TYPES and type(old_value) is type(new_value) and 'scalar' in config:
            return int(config['scalar'], 16) | child_mask(path, maps)
        if (
            isinstance(old_value, dict)
            and isinstance(new_value, dict)
            and list(old_value) == list(new_value)
            and name == 'NUMERIC_ESCAPES'
            and _is_escape_table([*old_value.values(), *new_value.values()])
        ):
            mask = child_mask(path, maps)
            for key in old_value:
                if old_value[key] != new_value[key]:
                    mask |= int(config.get(key, '0'), 16)
            return mask
    return None


def same_factory(old, new):
    """Same callee and argument shape; differing arguments are constants of the same type."""
    if not isinstance(old, ast.Call) or not isinstance(new, ast.Call) or ast.dump(old.func) != ast.dump(new.func):
        return False
    if len(old.args) != len(new.args) or [keyword.arg for keyword in old.keywords] != [keyword.arg for keyword in new.keywords]:
        return False
    for old_argument, new_argument in zip(old.args + [keyword.value for keyword in old.keywords], new.args + [keyword.value for keyword in new.keywords]):
        if ast.dump(old_argument) != ast.dump(new_argument) and not (
            isinstance(old_argument, ast.Constant) and isinstance(new_argument, ast.Constant) and type(old_argument.value) is type(new_argument.value)
        ):
            return False
    return True


def callback_edit(path, before, after, removed, maps):
    """A lambda or factory call stored in a parser/generator dispatch table changed."""
    if not removed or not (path.startswith(CALLBACK_PACKAGES) or path in {'sqlglot/parser.py', 'sqlglot/generator.py'}):
        return None
    first, last = min(removed), max(removed)
    old_callbacks = {
        (node.lineno, node.col_offset): node
        for node in spanning_nodes(before, first, last)
        if isinstance(node, (ast.Lambda, ast.Call)) and node.lineno <= first <= last <= node.end_lineno
    }
    for table in spanning_nodes(after, first, last):
        if not isinstance(table, ast.Dict):
            continue
        for position, new in enumerate(table.values):
            old = old_callbacks.get((new.lineno, new.col_offset))
            if old is None:
                continue
            profile = maps['config'].get(f'{path}:{old.lineno}:{old.col_offset}:callback')
            if profile is None:
                continue
            same_lambda = isinstance(old, ast.Lambda) and isinstance(new, ast.Lambda) and ast.dump(old.args) == ast.dump(new.args)
            if same_lambda or same_factory(old, new):
                restored = table.values.copy()
                restored[position] = old
                if unchanged_with_original(after, baseline_dump(before), table, {'values': restored}):
                    return int(profile.get('call', '0'), 16) | child_mask(path, maps)
    return None


def _string_get_calls(tree, first, last):
    """((line, column), call) for `x.get('key'[, default])` calls spanning first..last."""
    for node in spanning_nodes(tree, first, last):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'get'
            and 1 <= len(node.args) <= 2
            and not node.keywords
            and isinstance(node.args[0], ast.Constant)
            and type(node.args[0].value) is str
            and node.lineno <= first <= last <= node.end_lineno
        ):
            yield (node.lineno, node.col_offset), node


def lookup_edit(path, before, after, removed, maps):
    """The key or default of a profiled `mapping.get('key', default)` call changed."""
    if not removed:
        return None
    first, last = min(removed), max(removed)
    originals = dict(_string_get_calls(before, first, last))
    if not originals:
        return None
    for location, new in _string_get_calls(after, first, last):
        old = originals.get(location)
        profile = maps['lookups'].get(f'{path}:{location[0]}:{location[1]}')
        if old is None or profile is None or ast.dump(old) == ast.dump(new):
            continue
        if not unchanged_with_original(after, baseline_dump(before), new, {'args': old.args}):
            continue
        keys = [old.args[0].value, new.args[0].value]
        defaults = [call.args[1] if len(call.args) == 2 else ast.Constant(None) for call in (old, new)]
        if ast.dump(defaults[0]) != ast.dump(defaults[1]) and not all(isinstance(default, ast.Constant) for default in defaults):
            continue
        mask = child_mask(path, maps) | int(profile.get('unknown', '0'), 16)
        if keys[0] != keys[1]:
            mask |= int(profile.get('key:' + keys[0], '0'), 16) | int(profile.get('key:' + keys[1], '0'), 16)
        if ast.dump(defaults[0]) != ast.dump(defaults[1]):
            mask |= int(profile.get('missing', '0'), 16)
        for number in range(old.lineno, old.end_lineno + 1):
            mask |= int(maps['imports'].get(path, {}).get(str(number), '0'), 16)
        return mask
    return None


def _copying_calls(tree, first, last):
    """((line, column), call) for alias_/to_identifier/maybe_parse calls spanning first..last."""
    for node in spanning_nodes(tree, first, last):
        if not isinstance(node, ast.Call) or not node.lineno <= first <= last <= node.end_lineno:
            continue
        callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', None)
        if (
            callee in COPYING_FACTORIES
            and 1 <= len(node.args) <= 2
            and not any(isinstance(argument, ast.Starred) for argument in node.args)
            and all(keyword.arg is not None for keyword in node.keywords)
        ):
            yield (node.lineno, node.col_offset), node


def copy_edit(path, before, after, removed, maps):
    """Only the `copy=` flag of a profiled alias_/to_identifier/maybe_parse call changed."""
    if not removed:
        return None
    first, last = min(removed), max(removed)
    originals = dict(_copying_calls(before, first, last))
    if not originals:
        return None
    for location, new in _copying_calls(after, first, last):
        old = originals.get(location)
        profile = maps['copies'].get(f'{path}:{location[0]}:{location[1]}')
        if old is None or profile is None:
            continue
        copy_flags = [[keyword.value for keyword in call.keywords if keyword.arg == 'copy'] for call in (old, new)]
        if (
            not any(copy_flags)
            or any(len(flags) > 1 for flags in copy_flags)
            or not all(isinstance(flag, ast.Constant) and type(flag.value) is bool for flags in copy_flags for flag in flags)
        ):
            continue
        if [flag.value for flag in copy_flags[0]] == [flag.value for flag in copy_flags[1]]:
            continue
        other_keywords = [[ast.dump(keyword) for keyword in call.keywords if keyword.arg != 'copy'] for call in (old, new)]
        if other_keywords[0] != other_keywords[1] or not unchanged_with_original(after, baseline_dump(before), new, {'keywords': old.keywords}):
            continue
        mask = child_mask(path, maps) | int(profile.get('other', '0'), 16) | int(profile.get('unverified_factory', '0'), 16)
        for number in range(old.lineno, old.end_lineno + 1):
            mask |= int(maps['imports'].get(path, {}).get(str(number), '0'), 16)
        return mask
    return None


def _falsy_defaults(tree, line):
    """((def line, parameter), default) for falsy constant defaults written on line."""
    for func in spanning_nodes(tree, line, line):
        if not isinstance(func, ast.FunctionDef) or line >= func.body[0].lineno:
            continue
        positional = func.args.posonlyargs + func.args.args
        defaulted = [
            *zip(positional[len(positional) - len(func.args.defaults):], func.args.defaults),
            *zip(func.args.kwonlyargs, func.args.kw_defaults),
        ]
        for parameter, default in defaulted:
            if isinstance(default, ast.Constant) and default.lineno == line and type(default.value) in (type(None), int, bool, str, float) and not default.value:
                yield (func.lineno, parameter.arg), default


def default_edit(path, before, after, removed, maps):
    """A falsy default that the build-time flow analysis showed only reaches safe uses changed."""
    if len(removed) != 1:
        return None
    line = next(iter(removed))
    old_defaults = dict(_falsy_defaults(before, line))
    for key, default in _falsy_defaults(after, line):
        previous = old_defaults.get(key)
        if (
            previous is not None
            and f'{path}:{key[0]}:{key[1]}' in maps['default_sites']
            and (type(default.value) is not type(previous.value) or default.value != previous.value)
        ):
            if unchanged_with_original(after, baseline_dump(before), default, {'value': previous.value}):
                return maps['default_observers'] | child_mask(path, maps)
    return None


def _flag_name(node):
    """Name of a boolean class flag assignment (`X = True` or `X: ClassVar[bool] = True`)."""
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, bool)
    ):
        return node.targets[0].id
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.simple
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, bool)
        and 'ClassVar' in ast.dump(node.annotation)
    ):
        return node.target.id
    return None


def flag_edit(path, before, after, removed, maps, dynamic_names):
    """A profiled boolean class flag was flipped."""
    flips = maps.get('flips')
    if not flips or not removed:
        return None
    for cls in ast.walk(before):
        if not isinstance(cls, ast.ClassDef):
            continue
        for statement in cls.body:
            name = _flag_name(statement)
            if name is None or not statement.lineno <= min(removed) <= max(removed) <= statement.end_lineno:
                continue
            site = f'{path}:{statement.lineno}:{name}'
            if site not in flips or name in dynamic_names:
                return None
            for node in ast.walk(after):
                if (
                    type(node) is type(statement)
                    and node.lineno == statement.lineno
                    and _flag_name(node) == name
                    and node.value.value != statement.value.value
                    and unchanged_with_original(after, ast.dump(before), node, {'value': statement.value})
                ):
                    return int(flips[site], 16) | child_mask(path, maps)
            return None
    return None


def _annotator_entries(tree, first, last):
    """`{'annotator': lambda ...}` entries of EXPRESSION_METADATA spanning first..last."""
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if not any(isinstance(target, ast.Name) and target.id == 'EXPRESSION_METADATA' for target in targets):
            continue
        if not isinstance(statement.value, ast.Dict):
            continue
        for entry in statement.value.values:
            if (
                isinstance(entry, ast.Dict)
                and len(entry.keys) == 1
                and isinstance(entry.keys[0], ast.Constant)
                and type(entry.keys[0].value) is str
                and isinstance(entry.values[0], ast.Lambda)
                and entry.lineno <= first <= last <= entry.end_lineno
            ):
                yield entry


def metadata_callback_edit(path, before, after, removed, maps):
    """The body of a typing annotator lambda changed: tests that called it, plus metadata readers."""
    if not path.startswith('sqlglot/typing/') or not removed:
        return None
    first, last = min(removed), max(removed)
    for old, new in zip(_annotator_entries(before, first, last), _annotator_entries(after, first, last)):
        if old.keys[0].value != 'annotator':
            continue
        old_lambda, new_lambda = old.values[0], new.values[0]
        if ast.dump(old_lambda.args) != ast.dump(new_lambda.args):
            continue
        if new.keys[0].value != 'annotator' and ast.dump(old_lambda) != ast.dump(new_lambda):
            continue
        if not unchanged_with_original(after, baseline_dump(before), new, {'keys': old.keys, 'values': old.values}):
            continue
        callers = int(maps['typing_calls'].get(f'{path}:{old_lambda.lineno}', '0'), 16)
        if callers < 0:
            return None
        return callers | maps['metadata_readers'] | child_mask(path, maps) | child_mask('sqlglot/optimizer/annotate_types.py', maps)
    return None


def _keyword_entries(tree, line):
    """(key, value, table) for identifier-like keys of a KEYWORDS dict written on line."""
    for node in spanning_nodes(tree, line, line):
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        else:
            continue
        if not (isinstance(target, ast.Name) and target.id == 'KEYWORDS' and isinstance(node.value, ast.Dict)):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Constant) and key.lineno == line and isinstance(key.value, str) and key.value.isascii() and key.value.isidentifier():
                yield key, value, node.value


def keyword_edit(path, before, after, removed, maps):
    """A tokenizer KEYWORDS entry was renamed or remapped to another existing TokenType."""
    if len(removed) != 1 or not (path == 'sqlglot/tokens.py' or path.startswith('sqlglot/dialects/')):
        return None
    line = next(iter(removed))
    profile = maps['word_profile']
    for (old, old_value, table), (new, new_value, _new_table) in zip(_keyword_entries(before, line), _keyword_entries(after, line)):
        words = {old.value, new.value}
        if words.intersection(profile['reserved']):
            continue
        if any(symbol in word for symbol in profile['single'] for word in words):
            continue
        if len(words) == 2:
            unchanged = unchanged_with_original(after, baseline_dump(before), new, {'value': old.value})
        else:
            if not (
                isinstance(old_value, ast.Attribute)
                and isinstance(new_value, ast.Attribute)
                and isinstance(new_value.value, ast.Name)
                and new_value.value.id == 'TokenType'
                and ast.dump(old_value.value) == ast.dump(new_value.value)
                and any(isinstance(value, ast.Attribute) and ast.dump(value) == ast.dump(new_value) for value in table.values)
            ):
                continue
            unchanged = unchanged_with_original(after, baseline_dump(before), new_value, {'attr': old_value.attr})
        if not unchanged:
            continue
        hits = child_mask(path, maps) | child_mask('sqlglot/tokenizer_core.py', maps)
        for word in words:
            observed = int(profile['words'].get(word, '0'), 16)
            if observed < 0:
                return None
            hits |= observed
        return hits
    return None
