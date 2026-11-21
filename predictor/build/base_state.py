"""Save the base state fingerprint at image build: base_state.py <repo> <out file>.

Imports all of sqlglot from the pinned checkout, takes the state_fingerprint
snapshot and writes it (with the coordinator's header and the keys of the
dict/set objects and functions) as marshal data. At run time the coordinator
loads it instead of computing the snapshot itself.
"""
import marshal
import os
import sys

PREDICTOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PREDICTOR_DIR)


def main(repo, out_path):
    import harness_env
    harness_env.install_environment(repo)
    harness_env.import_sqlglot()
    import state_fingerprint
    state, fingerprinter = state_fingerprint.snapshot()
    import coordinator
    data = {'header': coordinator.base_state_header(), 'state': state, 'fkeys': sorted(fingerprinter.function_keys), 'okeys': sorted(fingerprinter.objects)}
    objects = coordinator.resolve_base_objects(data['okeys'])
    if objects is None or any((objects[key] is not fingerprinter.objects[key] for key in fingerprinter.objects)):
        raise SystemExit("base state: the table keys do not resolve to the snapshot's objects")
    with open(out_path, 'wb') as handle:
        marshal.dump(data, handle)
    print('base state:', len(state), 'keys,', len(fingerprinter.objects), 'tables,', len(fingerprinter.function_keys), 'functions', file=sys.stderr)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
