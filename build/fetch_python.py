"""Build step: adds a second build of the same CPython version (3.13.18) to the image.

The pinned image's interpreter is built with gcc as a small program plus a shared
libpython. The python-build-standalone project publishes the same CPython source built
with clang, profile-guided and link-time optimization and a static libpython; it runs the
same Python code with fewer CPU cycles. This script downloads one release file (GitHub
release at a fixed tag, fixed file name), checks its sha256 and unpacks it to DEST/python.
Usage: python fetch_python.py DEST"""
import hashlib
import io
import os
import sys
import tarfile
import time
import urllib.request

TAG = "20261110"
NAME = "cpython-3.13.18+20261110-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
URL = "https://github.com/astral-sh/python-build-standalone/releases/download/%s/%s" % (TAG, NAME.replace("+", "%2B"))
SHA256 = "f2d2199eb25880e07863b391cf7124e7ffa1338b0d65600dfcec117c4223a7fb"
DEST = sys.argv[1] if len(sys.argv) > 1 else "/opt"


ATTEMPTS = 6
TOTAL_SECONDS = 150.0  # the build goes on with the image's interpreter after this


def download():
    """The release file with the right sha256, or None. A failed, cut-off or corrupted download is
    retried with growing pauses: without this file the scored run uses the slower interpreter."""
    deadline = time.monotonic() + TOTAL_SECONDS
    pause = 2.0
    for attempt in range(1, ATTEMPTS + 1):
        left = deadline - time.monotonic()
        if left <= 5:
            break
        try:
            with urllib.request.urlopen(URL, timeout=min(60.0, left)) as r:
                data = r.read()
            digest = hashlib.sha256(data).hexdigest()
            if digest == SHA256:
                return data
            print(f"download attempt {attempt}: sha256 {digest} ({len(data)} bytes), want {SHA256}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"download attempt {attempt} failed: {e!r}", file=sys.stderr)
        time.sleep(min(pause, max(0.0, deadline - time.monotonic())))
        pause = min(pause * 2, 20.0)
    return None


def main():
    data = download()
    if data is None:
        sys.exit("could not download the interpreter build")
    os.makedirs(DEST, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not (m.name == "python" or m.name.startswith("python/")):
                sys.exit(f"unexpected member {m.name}")
        tf.extractall(DEST, filter="data")
    exe = os.path.join(DEST, "python", "bin", "python3.13")
    if not os.path.isfile(exe):
        sys.exit("no bin/python3.13 in the archive")
    print(f"interpreter build {NAME}: {len(data)} bytes, sha256 ok", file=sys.stderr)


if __name__ == "__main__":
    main()
