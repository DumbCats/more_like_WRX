"""The gzip JSON maps of the static selector, also stored as marshal files (pure speed).

Reading a map costs a gzip decompression and a JSON parse (about 0.12 s of CPU for all of
them). At image build time, `python mapcache.py <dir>` (after every map in <dir> is written)
stores next to each map NAME.gz the file NAME.gz.marshal: marshal.dumps of exactly the object
that json.load gives for it, read back and compared before it is kept. A map is converted only
when its text is ASCII, so the result does not depend on the locale's encoding. load() returns
the marshal copy when there is one and the gzip JSON otherwise (same objects either way:
marshal stores dicts in their order, and str, int, float, bool and None exactly).

Equal strings of a map are stored once (shared_strings): marshal writes a repeated object once
and refers back to it, so the copy is smaller and loads faster (the coverage map 3.7 MB instead
of 18.6 MB, about 9 ms less CPU per run). Loaded, the equal strings are one str object instead of
several: str is immutable and the selector never compares map strings by identity, so the
values are the same (the converted copy is still compared with json.load's object).
"""
import gzip
import json
import marshal
import mmap
import os
import sys

SUFFIX = ".marshal"


def load(path, encoding=None):
    """json.load of the gzip text file at path (as gzip.open(path, "rt", encoding=encoding))."""
    try:
        with open(f"{path}{SUFFIX}", "rb") as f:
            # marshal reads the file's pages in place (the same bytes as f.read(), without copying
            # them into a new buffer first: about 10 ms less CPU for all the maps). An empty file
            # cannot be mapped (ValueError, as marshal.loads(b"") raises EOFError): gzip JSON then.
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                return marshal.loads(data)
    except (OSError, ValueError, EOFError, TypeError):
        pass
    with gzip.open(path, "rt", encoding=encoding) as f:
        return json.load(f)


def shared_strings(value):
    """value (a json.load result) with every str replaced by the first equal str met (new lists and dicts)."""
    memo = {}

    def share(v):
        kind = type(v)
        if kind is str:
            return memo.setdefault(v, v)
        if kind is dict:
            return {share(k): share(x) for k, x in v.items()}
        if kind is list:
            return [share(x) for x in v]
        return v

    try:
        return share(value)
    except RecursionError:
        return value


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
            blob = marshal.dumps(shared_strings(value))
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
