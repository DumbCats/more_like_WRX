"""Download sqlglot at the pinned commit and unpack it into the image.

usage: python fetch_sqlglot.py <destination> [expected_tree_sha256]

The archive comes from GitHub's tarball endpoint (no git or apt needed in the image). After
extraction the unpacked tree is fingerprinted (path, mode and content of every file) and
compared with the expected digest, so a changed upstream archive fails the build loudly.
"""

import hashlib
import io
import os
import sys
import tarfile
import time
import urllib.request

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
ARCHIVE_URL = f"https://github.com/tobymao/sqlglot/archive/{PINNED_COMMIT}.tar.gz"
REQUIRED_DIRECTORIES = ("sqlglot", "tests", "sqlglotc", "sqlglot-integration-tests")
DOWNLOAD_ATTEMPTS = 5


def fingerprint_tree(root):
    """Return (sha256 over every file's path, mode and content hash, number of files)."""
    records = []
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories.sort()
        for filename in sorted(filenames):
            full_path = os.path.join(directory, filename)
            relative_path = os.path.relpath(full_path, root)
            mode = "755" if os.stat(full_path).st_mode & 0o100 else "644"
            with open(full_path, "rb") as handle:
                content_hash = hashlib.sha256(handle.read()).hexdigest()
            records.append(f"{relative_path}\x00{mode}\x00{content_hash}\n")
    records.sort()
    return hashlib.sha256("".join(records).encode()).hexdigest(), len(records)


def download_archive():
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(ARCHIVE_URL, timeout=120) as response:
                return response.read()
        except Exception as error:
            print(f"download attempt {attempt} failed: {error!r}", file=sys.stderr)
            time.sleep(3 * attempt)
    sys.exit("could not download sqlglot")


def extract_archive(archive_bytes, destination):
    """Unpack the archive's single top-level folder directly into destination."""
    top_folder = f"sqlglot-{PINNED_COMMIT}/"
    os.makedirs(destination, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        members = []
        for member in archive.getmembers():
            if member.name.rstrip("/") + "/" == top_folder:
                continue
            if not member.name.startswith(top_folder):
                sys.exit(f"unexpected member {member.name}")
            member.name = member.name[len(top_folder):]
            members.append(member)
        archive.extractall(destination, members=members, filter="data")


def main():
    destination = sys.argv[1] if len(sys.argv) > 1 else "/repo"
    expected_digest = sys.argv[2] if len(sys.argv) > 2 else None

    extract_archive(download_archive(), destination)

    digest, file_count = fingerprint_tree(destination)
    print(f"sqlglot {PINNED_COMMIT}: {file_count} files, tree sha256 {digest}", file=sys.stderr)
    if expected_digest and digest != expected_digest:
        sys.exit(f"tree sha256 mismatch: got {digest}, want {expected_digest}")
    for required in REQUIRED_DIRECTORIES:
        if not os.path.isdir(os.path.join(destination, required)):
            sys.exit(f"missing {required}/")


if __name__ == "__main__":
    main()
