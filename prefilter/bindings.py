"""Masks derived from the static reference index (generated/refs.json.gz).

The index records, for names bound in sqlglot, where they are loaded, which modules import
what, call sites of functions with defaults, and strings that may reach getattr-style lookups.
These helpers follow changed module/class bindings and changed defaults to the lines that read them.
"""
import ast
import builtins
import gzip
import json

from ast_tools import counterpart, future_annotations, module_name

CONTAINERS = (ast.Dict, ast.List, ast.Tuple, ast.Set)
TYPING_FACTORIES = {'TypeVar', 'NewType', 'ParamSpec', 'TypeVarTuple'}
SAFE_BINOPS = (ast.BitOr, ast.Add, ast.Sub)
# Comparison operators that may replace each other (same family) inside a pure expression.
SWAPPABLE = {ast.Eq: 'eq', ast.NotEq: 'eq', ast.Is: 'is', ast.IsNot: 'is', ast.In: 'in', ast.NotIn: 'in'}
EVALUATING = (ast.Compare, ast.BoolOp, ast.UnaryOp, ast.IfExp, ast.comprehension, ast.DictComp, ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.Dict, ast.List, ast.Tuple, ast.Set)
PLAIN_NODES = (ast.Constant, ast.Tuple, ast.List, ast.Set, ast.Compare, ast.BoolOp, ast.UnaryOp, ast.expr_context, ast.cmpop, ast.boolop, ast.unaryop)
IGNORED_FIELDS = ('ctx', 'type_comment', 'kind')
# Give up when a change spreads to more bindings than this.
LIMIT = 40
EVERYTHING_BEFORE = 10 ** 9


def load_refs(maps):
    """The reference index, loaded once into maps['refs'] (None when unavailable)."""
    if 'refs' not in maps:
        try:
            with gzip.open(maps['refs_path'], 'rt', encoding='utf8') as handle:
                maps['refs'] = json.load(handle)
        except (KeyError, OSError, ValueError):
            maps['refs'] = None
    return maps['refs']


def _typing_factory(node):
    """A call such as TypeVar('T') whose string argument only names the result."""
    function = node.func if isinstance(node, ast.Call) else None
    name = function.attr if isinstance(function, ast.Attribute) else getattr(function, 'id', None)
    return name in TYPING_FACTORIES


def _typed_dict(cls, tree, seen=()):
    """Whether cls is an undecorated TypedDict (directly or via TypedDicts of the same module)."""
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    if not cls.bases or cls.decorator_list:
        return False
    for base in cls.bases:
        base_name = base.attr if isinstance(base, ast.Attribute) else getattr(base, 'id', None)
        if base_name == 'TypedDict':
            continue
        if base_name in classes and base_name not in seen and _typed_dict(classes[base_name], tree, (*seen, cls.name)):
            continue
        return False
    return True


def _collect_differences(old, new, chain, out):
    """Append (old_node, new_node, ancestors) for each minimal differing subtree of old/new."""
    if type(old) is not type(new):
        out.append((old, new, chain))
        return
    if isinstance(old, ast.AST):
        for field in old._fields:
            if field in IGNORED_FIELDS:
                continue
            old_value, new_value = getattr(old, field, None), getattr(new, field, None)
            if isinstance(old_value, list) or isinstance(new_value, list):
                if not isinstance(old_value, list) or not isinstance(new_value, list) or len(old_value) != len(new_value):
                    out.append((old, new, chain))
                    return
                pairs = list(zip(old_value, new_value))
            elif isinstance(old_value, ast.AST) or isinstance(new_value, ast.AST):
                pairs = [(old_value, new_value)]
            else:
                if old_value != new_value or type(old_value) is not type(new_value):
                    out.append((old, new, chain))
                    return
                continue
            for old_item, new_item in pairs:
                if isinstance(old_item, ast.AST) or isinstance(new_item, ast.AST):
                    if old_item is None or new_item is None:
                        out.append((old, new, chain))
                        return
                    _collect_differences(old_item, new_item, chain + [old], out)
                elif old_item != new_item:
                    out.append((old, new, chain))
                    return
    elif old != new:
        out.append((old, new, chain))


def _selected_by(node):
    """The operands whose value node may pass through unchanged."""
    if isinstance(node, ast.IfExp):
        return [node.body, node.orelse]
    if isinstance(node, ast.BoolOp):
        return list(node.values)
    if isinstance(node, ast.DictComp):
        return [node.key, node.value]
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return [node.elt]
    return []


def _plain(node, names, attributes, targets=()):
    """Whether node only combines known names/attributes, constants and comparisons."""
    for item in ast.walk(node):
        if isinstance(item, ast.Name):
            if item.id not in names and item.id not in vars(builtins) and item.id not in targets:
                return False
        elif isinstance(item, ast.Attribute):
            root = item.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if (ast.dump(item.value), item.attr) not in attributes or (isinstance(root, ast.Name) and root.id in targets):
                return False
        elif not isinstance(item, PLAIN_NODES):
            return False
    return True


def _bound_before(body, lineno):
    """Names bound by the statements of body that start before lineno."""
    names = set()
    for statement in body:
        if statement.lineno >= lineno:
            break
        if isinstance(statement, ast.Assign):
            names.update(target.id for target in statement.targets if isinstance(target, ast.Name))
        elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)) and isinstance(statement.target, ast.Name):
            names.add(statement.target.id)
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(statement.name)
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split('.')[0] for alias in statement.names)
    return names


def _import_attributes(tree):
    """(dumped receiver, attribute) pairs loaded at import time (outside functions and lambdas)."""
    attributes = set()
    stack = [(tree, False)]
    while stack:
        node, deferred = stack.pop()
        if isinstance(node, ast.Attribute) and not deferred:
            attributes.add((ast.dump(node.value), node.attr))
        inner_deferred = deferred or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        stack.extend((child, inner_deferred) for child in ast.iter_child_nodes(node))
    return attributes


def _safe_value(node, names, attributes):
    """Whether evaluating node cannot fail or run code (constants, known names, displays)."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        return node.id in names or (node.id in vars(builtins) and node.id[0].isupper())
    if isinstance(node, ast.Attribute):
        return (ast.dump(node.value), node.attr) in attributes and _safe_value(node.value, names, attributes)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_safe_value(element, names, attributes) for element in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is not None and _hashable(key) and _safe_value(key, names, attributes) for key in node.keys) and all(
            _safe_value(value, names, attributes) for value in node.values
        )
    return False


def _hashable(node):
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Name, ast.Attribute)):
        return True
    if isinstance(node, ast.Tuple):
        return all(_hashable(element) for element in node.elts)
    return False


def _only_field_types_changed(old, new):
    """Whether two class bodies differ only in simple annotations of value-less fields."""
    if len(old.body) != len(new.body):
        return False
    simple_annotation = (ast.Name, ast.Load, ast.Attribute, ast.Subscript, ast.Tuple, ast.BinOp, ast.BitOr, ast.Constant)
    swapped = []
    try:
        for old_field, new_field in zip(old.body, new.body):
            if not isinstance(old_field, ast.AnnAssign) or not isinstance(new_field, ast.AnnAssign):
                continue
            if not isinstance(new_field.target, ast.Name) or new_field.value is not None:
                continue
            if not all(
                isinstance(part, simple_annotation) and (not isinstance(part, ast.Constant) or part.value is None or type(part.value) is int)
                for part in ast.walk(new_field.annotation)
            ):
                continue
            swapped.append((new_field, new_field.annotation))
            new_field.annotation = old_field.annotation
        return bool(swapped) and ast.dump(old) == ast.dump(new)
    finally:
        for field, annotation in swapped:
            field.annotation = annotation


def _class_header(cls):
    return ast.dump(ast.ClassDef(cls.name, cls.bases, cls.keywords, [], cls.decorator_list))


def _names_in(node):
    return {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}


def _attributes_in(node):
    return {(ast.dump(item.value), item.attr) for item in ast.walk(node) if isinstance(item, ast.Attribute)}


def _changed_bindings(path, before, after):
    """The module/class bindings whose value changed, as (kind, owner, name), or None.

    kind is 'global' (module attribute), 'attr' (class attribute) or 'display' (only the
    name string passed to a typing factory changed). None means some other kind of edit.
    """
    module = module_name(path)
    attributes = _import_attributes(before)
    found = []

    def compare_bodies(old_body, new_body, owner, scope_names):
        if len(old_body) != len(new_body):
            return False
        for old, new in zip(old_body, new_body):
            if ast.dump(old) == ast.dump(new):
                continue
            if isinstance(old, ast.ClassDef) and isinstance(new, ast.ClassDef):
                if _class_header(old) != _class_header(new):
                    same_shape = (
                        old.name == new.name
                        and ast.dump(ast.Tuple(old.bases)) == ast.dump(ast.Tuple(new.bases))
                        and [keyword.arg for keyword in old.keywords] == [keyword.arg for keyword in new.keywords]
                        and not old.decorator_list
                        and not new.decorator_list
                        and all(
                            isinstance(old_keyword.value, ast.Constant)
                            and isinstance(new_keyword.value, ast.Constant)
                            and type(old_keyword.value.value) is type(new_keyword.value.value)
                            for old_keyword, new_keyword in zip(old.keywords, new.keywords)
                        )
                    )
                    if not same_shape or not _typed_dict(old, before) or not _typed_dict(new, after):
                        return False
                    found.append(('attr' if owner else 'global', '.'.join([module, *owner]), old.name))
                    if ast.dump(ast.Module(old.body, [])) == ast.dump(ast.Module(new.body, [])):
                        continue
                if (
                    not owner
                    and _typed_dict(old, before)
                    and _typed_dict(new, after)
                    and future_annotations(before)
                    and _only_field_types_changed(old, new)
                ):
                    found.append(('global', module, old.name))
                    continue
                if not compare_bodies(old.body, new.body, owner + [old.name], None):
                    return False
                continue
            if type(old) is not type(new) or not isinstance(old, (ast.Assign, ast.AnnAssign)):
                return False
            old_targets = old.targets if isinstance(old, ast.Assign) else [old.target]
            new_targets = new.targets if isinstance(new, ast.Assign) else [new.target]
            if len(old_targets) != 1 or not isinstance(old_targets[0], ast.Name) or ast.dump(ast.Module(old_targets, [])) != ast.dump(ast.Module(new_targets, [])):
                return False
            if isinstance(old, ast.AnnAssign) and ast.dump(old.annotation) != ast.dump(new.annotation):
                return False
            if old.value is None or new.value is None:
                return False
            differences = []
            _collect_differences(old.value, new.value, [], differences)
            bound = _bound_before(old_body, old.lineno) | (scope_names or set())
            for old_part, new_part, chain in differences:
                if type(old_part) in SWAPPABLE and type(new_part) in SWAPPABLE and SWAPPABLE[type(old_part)] == SWAPPABLE[type(new_part)]:
                    known_attributes = attributes | _attributes_in(old.value)
                    loop_targets = {
                        name.id
                        for node in chain
                        if isinstance(node, ast.comprehension)
                        for name in ast.walk(node.target)
                        if isinstance(name, ast.Name)
                    }
                    loop_targets |= {
                        name.id
                        for node in chain
                        for generator in getattr(node, 'generators', ())
                        for name in ast.walk(generator.target)
                        if isinstance(name, ast.Name)
                    }
                    known_names = bound | _names_in(old.value)
                    if all(isinstance(node, EVALUATING) for node in chain) and all(
                        _plain(operand, known_names, known_attributes, loop_targets) for node in chain for operand in _selected_by(node)
                    ):
                        continue
                    return False
                if not isinstance(old_part, ast.expr) or not isinstance(new_part, ast.expr):
                    return False
                for ancestor in chain:
                    if isinstance(ancestor, CONTAINERS):
                        continue
                    if (
                        _typing_factory(ancestor)
                        and isinstance(old_part, ast.Constant)
                        and isinstance(new_part, ast.Constant)
                        and isinstance(old_part.value, str)
                        and isinstance(new_part.value, str)
                    ):
                        continue
                    if isinstance(ancestor, ast.BinOp) and isinstance(ancestor.op, SAFE_BINOPS):
                        continue
                    return False
                parent = chain[-1] if chain else None
                if isinstance(parent, ast.Dict) and any(key is old_part for key in parent.keys) and not _hashable(new_part):
                    return False
                if isinstance(parent, ast.Set) and not _hashable(new_part):
                    return False
                if not _safe_value(new_part, bound | _names_in(old_part), attributes | _attributes_in(old_part)):
                    return False
            if (
                _typing_factory(old.value)
                and differences
                and all(
                    chain == [old.value]
                    and isinstance(old_part, ast.Constant)
                    and isinstance(old_part.value, str)
                    and isinstance(new_part, ast.Constant)
                    and isinstance(new_part.value, str)
                    for old_part, new_part, chain in differences
                )
            ):
                found.append(('display', '.'.join([module, *owner]), old_targets[0].id))
                continue
            found.append(('attr' if owner else 'global', '.'.join([module, *owner]), old_targets[0].id))
        return True

    if not compare_bodies(before.body, after.body, [], _bound_before(before.body, EVERYTHING_BEFORE)):
        return None
    return found


def _module_file(module, files):
    """The file among files that defines module (plain module or package __init__)."""
    for candidate in (module.replace('.', '/') + '.py', module.replace('.', '/') + '/__init__.py'):
        if candidate in files:
            return candidate
    return None


def binding_mask(path, before, after, maps):
    """Tests that load a changed module/class binding (following re-exports), or None."""
    changed = _changed_bindings(path, before, after)
    if not changed:
        return None
    refs = load_refs(maps)
    if not refs:
        return None
    loads, imports, dynamic_names, special_classes = refs['loads'], refs['imports'], set(refs['strings']), refs['special']
    known_files = set(imports) | {path}
    for sites in loads.values():
        known_files.update(site[0] for site in sites)
    line_masks = maps['lines']
    import_masks = maps['imports']
    test_files = maps['test_files']
    selected = 0
    seen = set()
    cached_lines = {}
    pending = list(changed)
    while pending:
        kind, owner, name = pending.pop()
        if (kind, owner, name) in seen:
            continue
        seen.add((kind, owner, name))
        if kind == 'display':
            continue
        if len(seen) > LIMIT or name in dynamic_names or name.startswith('__') or name.endswith('_sql'):
            return None
        if any(name.startswith(prefix) and name.endswith(suffix) for prefix, suffix in refs.get('patterns', ())):
            return None
        if kind == 'attr' and any(owner == special or owner.startswith(special + '.') for special in special_classes):
            return None
        for test_file in refs.get('test_strings', {}).get(name, ()):
            selected |= test_files.get(test_file, 0)
        if name not in loads:
            continue
        if kind == 'global':
            defining_file = _module_file(owner, known_files)
            if defining_file is None:
                return None
            readers = {defining_file: name}
            for importer, entries in imports.items():
                for source_module, imported, alias in entries:
                    if imported == '*' and source_module == owner:
                        readers.setdefault(importer, name)
                    elif imported == name and source_module == owner:
                        if alias != name:
                            return None
                        readers.setdefault(importer, name)
            sites = [site for site in loads[name] if site[2] == 'a' or site[0] in readers]
        else:
            owner_file = None
            parts = owner.split('.')
            for length in range(len(parts), 0, -1):
                owner_file = _module_file('.'.join(parts[:length]), known_files)
                if owner_file:
                    break
            if owner_file is None:
                return None
            sites = [site for site in loads[name] if site[2] == 'a' or (site[0] == owner_file and (site[3] or '').startswith('C:'))]
        for file, line, _load_kind, assigned in sites:
            if assigned is None and line in cached_lines.setdefault(file, set(refs.get('cached', {}).get(file, ()))):
                return None
            if assigned is None:
                key = str(line)
                selected |= int(line_masks.get(file, {}).get(key, '0'), 16)
                selected |= int(import_masks.get(file, {}).get(key, '0'), 16)
            elif file.startswith('tests/'):
                selected |= test_files.get(file, 0)
            elif assigned == '':
                return None
            else:
                reader_module = module_name(file)
                scope, assigned_name = assigned.split(':', 1)
                if scope == 'M':
                    pending.append(('global', reader_module, assigned_name))
                else:
                    pending.append(('attr', reader_module + '.?', assigned_name))
    return selected


def importer_mask(path, maps):
    """Tests in test files that (transitively) import the test helper module at path, or None."""
    refs = load_refs(maps)
    if not refs or not path.startswith('tests/') or not path.endswith('.py'):
        return None
    importers_of = {}
    for importer, entries in refs['imports'].items():
        for source_module, imported, _alias in entries:
            for target in (source_module, f'{source_module}.{imported}' if imported and imported != '*' else source_module):
                importers_of.setdefault(target, set()).add(importer)
    visited, pending = set(), [(path, module_name(path), path.endswith('/__init__.py'))]
    selected = 0
    while pending:
        file, module, is_package = pending.pop()
        if file in visited:
            continue
        visited.add(file)
        selected |= maps['test_files'].get(file, 0)
        dependents = set(importers_of.get(module, ()))
        if is_package:
            folder = file.rsplit('/', 1)[0] + '/'
            dependents |= {test_file for test_file in maps['test_files'] if test_file.startswith(folder)}
            dependents |= {importer for importer in refs['imports'] if importer.startswith(folder)}
        for dependent in dependents:
            pending.append((dependent, module_name(dependent), dependent.endswith('/__init__.py')))
    return selected


def signature_safe(func, before, after):
    """Whether only defaults/annotations of func changed, to values that evaluate safely."""
    new_func = counterpart(after, func)
    if new_func is None or ast.dump(ast.Module(func.decorator_list, [])) != ast.dump(ast.Module(new_func.decorator_list, [])):
        return False
    attributes = _import_attributes(before)
    names = _bound_before(before.body, EVERYTHING_BEFORE) | {node.id for node in ast.walk(before) if isinstance(node, ast.Name)}
    postponed = future_annotations(before)
    old_defaults = [*func.args.defaults, *func.args.kw_defaults]
    new_defaults = [*new_func.args.defaults, *new_func.args.kw_defaults]
    if len(old_defaults) != len(new_defaults):
        return False
    pairs = list(zip(old_defaults, new_defaults))
    if not postponed:
        old_arguments = [node for node in ast.walk(func.args) if isinstance(node, ast.arg)]
        new_arguments = [node for node in ast.walk(new_func.args) if isinstance(node, ast.arg)]
        if len(old_arguments) != len(new_arguments):
            return False
        pairs += [(old_argument.annotation, new_argument.annotation) for old_argument, new_argument in zip(old_arguments, new_arguments)]
        pairs.append((func.returns, new_func.returns))
    for old_expression, new_expression in pairs:
        if old_expression is None or new_expression is None:
            if old_expression is not new_expression:
                return False
            continue
        differences = []
        _collect_differences(old_expression, new_expression, [], differences)
        for old_part, new_part, chain in differences:
            if not isinstance(new_part, ast.expr) or not all(isinstance(ancestor, CONTAINERS) for ancestor in chain):
                return False
            if not _safe_value(new_part, names | _names_in(old_part), attributes | _attributes_in(old_part)):
                return False
    return True


def _signature_shape(arguments):
    """Dump of a signature with every default blanked out."""
    return ast.dump(
        ast.arguments(arguments.posonlyargs, arguments.args, arguments.vararg, arguments.kwonlyargs, [None] * len(arguments.kw_defaults), arguments.kwarg, []),
        include_attributes=False,
    )


def default_callers(func, before, after, path, maps):
    """Tests reaching a call of func that relies on a changed constant default, or None."""
    if func.name.startswith('__') or any(
        not (isinstance(decorator, ast.Name) and decorator.id in {'staticmethod', 'classmethod'}) for decorator in func.decorator_list
    ):
        return None
    new_func = counterpart(after, func)
    if new_func is None:
        return None
    old_arguments, new_arguments = func.args, new_func.args
    if _signature_shape(old_arguments) != _signature_shape(new_arguments) or len(old_arguments.defaults) != len(new_arguments.defaults):
        return None
    positional_names = [argument.arg for argument in [*old_arguments.posonlyargs, *old_arguments.args]]
    # (name, positional index or None, may be passed by keyword) of each changed default
    changed = []
    for offset, (old_default, new_default) in enumerate(zip(old_arguments.defaults, new_arguments.defaults)):
        if ast.dump(old_default) != ast.dump(new_default):
            if not (isinstance(old_default, ast.Constant) and isinstance(new_default, ast.Constant)):
                return None
            position = len(positional_names) - len(old_arguments.defaults) + offset
            changed.append((positional_names[position], position, position >= len(old_arguments.posonlyargs)))
    for argument, old_default, new_default in zip(old_arguments.kwonlyargs, old_arguments.kw_defaults, new_arguments.kw_defaults):
        if (old_default is None) != (new_default is None):
            return None
        if old_default is not None and ast.dump(old_default) != ast.dump(new_default):
            if not (isinstance(old_default, ast.Constant) and isinstance(new_default, ast.Constant)):
                return None
            changed.append((argument.arg, None, True))
    if not changed or ast.dump(ast.Module(func.body, [])) != ast.dump(ast.Module(new_func.body, [])):
        return None
    refs = load_refs(maps)
    if not refs or 'calls' not in refs:
        return None
    name = func.name
    if any(imported == name and alias != name for entries in refs['imports'].values() for _module, imported, alias in entries):
        return None
    dynamic_names = refs.get('dynamic_strings', refs['strings'])
    if name in refs['escapes'] or name in set(dynamic_names) or any(name.startswith(prefix) and name.endswith(suffix) for prefix, suffix in refs.get('patterns', ())):
        return None
    selected = maps.get('documentation_readers', 0) if name in set(refs.get('doc_strings', ())) else 0
    for test_file in refs.get('test_strings', {}).get(name, ()):
        selected |= maps['test_files'].get(test_file, 0)
    line_masks, import_masks = maps['lines'], maps['imports']
    module_level = any(statement is func for statement in before.body)
    imported_roots = {}
    for file, line, positional_count, star_args, keywords, star_kwargs, deferred, receiver in refs['calls'].get(name, ()):
        if module_level and receiver is not None:
            if file not in imported_roots:
                imported_roots[file] = {alias.split('.')[0] for _module, _imported, alias in refs['imports'].get(file, ())}
            if receiver not in imported_roots[file]:
                continue
        if not star_args and not star_kwargs and all(
            position is not None and position < positional_count or (by_keyword and argument_name in keywords)
            for argument_name, position, by_keyword in changed
        ):
            continue
        if not deferred and not file.startswith('tests/'):
            return None
        if not deferred:
            selected |= maps['test_files'].get(file, 0)
            continue
        key = str(line)
        selected |= int(line_masks.get(file, {}).get(key, '0'), 16) | int(import_masks.get(file, {}).get(key, '0'), 16)
    return selected
