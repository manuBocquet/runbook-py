"""Prints the CHANGELOG.md section of a version (used as GitHub Release notes).

    python scripts/release_notes.py 1.1.1 > notes.md

Exits 1 if the version has no "## X.Y.Z" heading or its section is empty.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def section(text, version):
    heading = re.compile(r"^## %s(\s|$)" % re.escape(version))
    lines, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = bool(heading.match(line))
            continue
        if inside:
            lines.append(line)
    return "\n".join(lines).strip()


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: release_notes.py X.Y.Z")
    version = argv[1].lstrip("v")
    with open(os.path.join(ROOT, "CHANGELOG.md")) as handle:
        notes = section(handle.read(), version)
    if not notes:
        sys.exit("CHANGELOG.md: no notes for %s" % version)
    print(notes)


if __name__ == "__main__":
    main(sys.argv)
