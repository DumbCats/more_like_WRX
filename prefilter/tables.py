"""Loading the shipped profiles and build-time indexes into one `maps` dictionary.

Every profile names the test inventory it was measured with; they must all agree with the
coverage map's inventory (bit i of a mask is test maps['names'][i]).
"""
import gzip
import json

from ast_tools import child_mask
from settings import GENERATED_DIR, SHIPPED_DIR

# (file in data/, key holding the table, key in maps) for profiles that are copied as they are.
SITE_PROFILES = (
    ('arguments.gz', 'sites', 'arguments'),
    ('config.gz', 'sites', 'config'),
    ('predicates.gz', 'values', 'predicates'),
    ('cold-orders.gz', 'orders', 'cold_orders'),
    ('globals.gz', 'sites', 'globals'),
    ('fixture-attrs.gz', 'sites', 'fixture_attributes'),
    ('lookups.gz', 'sites', 'lookups'),
    ('fixture-modes.gz', 'sites', 'fixture_modes'),
    ('copies.gz', 'sites', 'copies'),
    ('order.gz', 'sites', 'orders'),
)
OPTIONAL_PROFILE_ERRORS = (OSError, ValueError, KeyError)


def read_shipped(name):
    with gzip.open(SHIPPED_DIR / name, 'rt') as handle:
        return json.load(handle)


def read_generated_json(name):
    return json.loads((GENERATED_DIR / name).read_text())


def _require_inventory(profile_names, maps, message='inconsistent build-time test inventory'):
    if profile_names != maps['names']:
        raise ValueError(message)


def load_tables():
    """Return (maps, index) where index maps each test id of the inventory to its bit."""
    maps = read_shipped('coverage.json.gz')
    maps['operands'] = read_shipped('operands.gz')
    fields = read_shipped('fields.gz')
    maps['fields'] = fields['fields'] if fields['names'] == maps['names'] else {}
    imports = read_shipped('imports.gz')
    _require_inventory(imports['names'], maps)
    maps['imports'] = imports['lines']
    for file, table_key, target in SITE_PROFILES:
        profile = read_shipped(file)
        _require_inventory(profile['names'], maps)
        maps[target] = profile[table_key]
        if target == 'orders':
            maps['unmeasured_orders'] = int(profile['unmeasured'], 16)
        if 'external' in profile:
            maps['unknown_children'] = int(profile.get('unknown', profile['external']), 16)
            maps['child_paths'] = profile.get('child_paths', {})
    branches = read_shipped('branches.gz')
    _require_inventory(branches.pop('names'), maps, 'branch observations use a different test inventory')
    maps['unmeasured_branch'] = int(branches['unmeasured'], 16)
    maps['branch_orders'] = branches['orders']
    maps['choices'] = branches['choices']
    with gzip.open(GENERATED_DIR / 'instructions.gz', 'rt') as handle:
        maps['instructions'] = {path: set(sites) for path, sites in json.load(handle).items()}

    index = {name: position for position, name in enumerate(maps['names'])}
    maps['test_files'] = {}
    for name, position in index.items():
        file = name.split('::')[0]
        maps['test_files'][file] = maps['test_files'].get(file, 0) | 1 << position

    typing_calls = read_shipped('typing-calls.gz')
    _require_inventory(typing_calls['names'], maps, 'typing callback inventory mismatch')
    maps['typing_calls'] = typing_calls['sites']
    maps['metadata_readers'] = 0
    for file in read_generated_json('metadata-readers.json'):
        maps['metadata_readers'] |= maps['test_files'].get(file, 0)
    word_profile = read_shipped('words.gz')
    _require_inventory(word_profile.pop('names'), maps, 'tokenizer lookup inventory mismatch')
    maps['word_profile'] = word_profile
    maps['fixture_fields'] = read_generated_json('fixture-fields.json')
    _load_introspecting_tests(maps)
    _load_default_flow(maps)
    maps['refs_path'] = GENERATED_DIR / 'refs.json.gz'
    _load_optional_profiles(maps)
    try:
        maps['modules'] = set(json.loads((SHIPPED_DIR / 'modules.json').read_text()))
    except (OSError, ValueError):
        maps['modules'] = None
    _load_cold_imports(maps)
    maps['fixture_paths'] = read_shipped('fixture-paths.gz')
    _require_inventory(maps['fixture_paths']['names'], maps, 'fixture path inventory mismatch')
    return maps, index


def _load_introspecting_tests(maps):
    """Masks of test files that read docstrings, tracemalloc or frames/code objects."""
    maps['frame_readers'] = 0
    maps['memory_readers'] = 0
    maps['documentation_readers'] = 0
    maps['frame_reader_files'] = {}
    for file, kinds in read_generated_json('frame-readers.json').items():
        for kind in kinds:
            maps[kind] |= maps['test_files'].get(file, 0)
            if kind == 'frame_readers':
                maps['frame_reader_files'][file] = maps['test_files'].get(file, 0)
    try:
        maps['frame_scopes'] = read_generated_json('frame-scope.json')
    except (OSError, ValueError):
        maps['frame_scopes'] = {}


def _load_default_flow(maps):
    """Default parameters proven to flow only into safe uses, and the tests observing defaults."""
    defaults = read_generated_json('default-flow.json')
    maps['default_sites'] = defaults['sites']
    maps['default_observers'] = maps['frame_readers']
    for file, lines in defaults['observers'].items():
        for line in lines:
            for table in (maps['lines'], maps['imports']):
                maps['default_observers'] |= int(table.get(file, {}).get(str(line), '0'), 16)


def _load_optional_profiles(maps):
    """Profiles whose absence only disables a recognizer."""
    try:
        profile = read_shipped('boolops.gz')
        maps['boolops'] = profile if profile.pop('names') == maps['names'] else None
    except OPTIONAL_PROFILE_ERRORS:
        maps['boolops'] = None
    try:
        profile = read_shipped('members.gz')
        maps['members'] = profile if profile.pop('names') == maps['names'] else None
    except OPTIONAL_PROFILE_ERRORS:
        maps['members'] = None
    try:
        profile = read_shipped('flips.gz')
        maps['flips'] = profile['sites'] if profile['names'] == maps['names'] else None
    except OPTIONAL_PROFILE_ERRORS:
        maps['flips'] = None
    if maps['flips'] is not None:
        forwarded = read_shipped('forward.gz')
        _require_inventory(forwarded['names'], maps, 'forwarded settings inventory mismatch')
        for site, site_tests in forwarded['sites'].items():
            if site in maps['flips']:
                maps['flips'][site] = hex(int(site_tests, 16) & int(maps['flips'][site], 16))
    try:
        profile = read_shipped('import_types.gz')
        maps['import_types'] = profile['types'] if profile['names'] == maps['names'] else None
    except OPTIONAL_PROFILE_ERRORS:
        maps['import_types'] = None


def _load_cold_imports(maps):
    """Per lazily imported module: every test that can import or reach it."""
    cold = read_shipped('cold-imports.gz')
    _require_inventory(cold['names'], maps, 'cold import inventory mismatch')
    maps['cold_imports'] = {}
    for path, site_tests in cold['sites'].items():
        mask = int(site_tests, 16) | child_mask(path, maps)
        mask |= int(maps['source_readers'].get(path, '0'), 16)
        for line_tests in maps['lines'].get(path, {}).values():
            mask |= int(line_tests, 16)
        maps['cold_imports'][path] = mask
