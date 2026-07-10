#!/usr/bin/env python3
"""Guard: banto.py VERSION == pyproject version (and == the release tag, if given).

Run bare in CI:            python .github/scripts/check_version.py
Run in release with a tag: python .github/scripts/check_version.py 0.5.0
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]


def find(path: str, pattern: str) -> str:
    m = re.search(pattern, (ROOT / path).read_text())
    if not m:
        sys.exit(f"could not find version in {path}")
    return m.group(1)


banto_v = find("banto.py", r'VERSION\s*=\s*"([^"]+)"')
pyproj_v = find("pyproject.toml", r'version\s*=\s*"([^"]+)"')
if banto_v != pyproj_v:
    sys.exit(f"version mismatch: banto.py={banto_v} pyproject={pyproj_v}")

tag = sys.argv[1] if len(sys.argv) > 1 else ""
if tag:
    if tag != banto_v:
        sys.exit(f"tag {tag} != VERSION {banto_v} — bump banto.py + pyproject.toml before tagging")
    print(f"version ok: {banto_v} (matches tag)")
else:
    print(f"version ok: {banto_v}")
