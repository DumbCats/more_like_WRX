"""The gzip JSON maps of the static selector, also stored as marshal files (pure speed).

Reading a map costs a gzip decompression and a JSON parse (about 0.12 s of CPU for all of
them). At image build time, `python mapcache.py <dir>` (after every map in <dir> is written)
stores next to each map NAME.gz the file NAME.gz.marshal: marshal.dumps of exactly the object
that json.load gives for it, read back and compared before it is kept. A map is converted only
when its text is ASCII, so the result does not depend on the locale's encoding. load() returns
the marshal copy when there is one and the gzip JSON otherwise (same objects either way:
marshal stores dicts in their order, and str, int, float, bool and None exactly).
"""
import gzip
import json
import marshal
import os
import sys

SUFFIX = ".marshal"


def load(path, encoding=None):
    """json.load of the gzip text file at path (as gzip.open(path, "rt", encoding=encoding))."""
    try:
        with open(f"{path}{SUFFIX}", "rb") as f:
            return marshal.loads(f.read())
    except (OSError, ValueError, EOFError, TypeError):
        pass
    with gzip.open(path, "rt", encoding=encoding) as f:
        return json.load(f)


def convert(directory):
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".gz"):
            continue
        path = os.path.join(directory, name)
        target = path + SUFFIX
        try:
            os.remove(target)
        except FileNotFoundError:
            pass
        try:
            with gzip.open(path, "rb") as f:
                raw = f.read()
            if not raw.isascii():
                print("mapcache: not ASCII, stays gzip JSON:", name, file=sys.stderr)
                continue
            value = json.loads(raw.decode("ascii"))
            blob = marshal.dumps(value)
            if marshal.loads(blob) != value:
                print("mapcache: marshal copy differs, stays gzip JSON:", name, file=sys.stderr)
                continue
        except (OSError, ValueError, EOFError, TypeError) as e:
            print("mapcache: not converted:", name, repr(e), file=sys.stderr)
            continue
        with open(target + ".tmp", "wb") as f:
            f.write(blob)
        os.replace(target + ".tmp", target)


if __name__ == "__main__":
    convert(sys.argv[1])
