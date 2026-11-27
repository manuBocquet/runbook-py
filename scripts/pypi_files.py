"""Downloads the files of a version exactly as published on PyPI (sha256 checked).

    python scripts/pypi_files.py 1.1.1 dist/

Retries for a few minutes while a just-published version is not visible yet.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

PROJECT = "runbook-py"
ATTEMPTS, DELAY = 20, 15


def release_files(version):
    url = "https://pypi.org/pypi/%s/%s/json" % (PROJECT, version)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                files = json.load(resp)["urls"]
            if files:
                return files
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        print("%s %s not on PyPI yet (%d/%d)" % (PROJECT, version, attempt, ATTEMPTS), file=sys.stderr)
        time.sleep(DELAY)
    sys.exit("%s %s: not found on PyPI" % (PROJECT, version))


def main(argv):
    if len(argv) != 3:
        sys.exit("usage: pypi_files.py X.Y.Z DEST_DIR")
    version, dest = argv[1].lstrip("v"), argv[2]
    os.makedirs(dest, exist_ok=True)
    for item in release_files(version):
        with urllib.request.urlopen(item["url"], timeout=60) as resp:
            data = resp.read()
        if hashlib.sha256(data).hexdigest() != item["digests"]["sha256"]:
            sys.exit("%s: sha256 mismatch" % item["filename"])
        with open(os.path.join(dest, item["filename"]), "wb") as handle:
            handle.write(data)
        print(item["filename"])


if __name__ == "__main__":
    main(sys.argv)
