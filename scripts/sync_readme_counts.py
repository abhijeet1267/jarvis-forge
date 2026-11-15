#!/usr/bin/env python3
"""Update the README's published test counts from the actual tree.

The README publishes how many tests exist, and tests enforce that the numbers
are true. That is a good property, but it meant every commit that added a test
also had to remember to edit the README, and forgetting produced a red suite for
a reason unrelated to the change.

This script makes the numbers derived rather than typed. Run it as the last step
before a commit that touched tests:

    .venv/bin/python scripts/sync_readme_counts.py

It reads the tree (collect-only, so nothing executes) and rewrites the two
numbers in place. It prints what it changed and exits non-zero only when it
cannot find the lines it expects.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"


def collected() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True)
    match = re.search(r"(\d+) tests collected", result.stdout + result.stderr)
    if not match:
        raise SystemExit("could not read the collected count from pytest")
    return int(match.group(1))


def module_count() -> int:
    return len(list((REPO / "tests").glob("*.py")))


def jarvis_module_count() -> int:
    return len(list((REPO / "jarvis").rglob("*.py")))


def main() -> int:
    body = README.read_text(encoding="utf-8")
    original = body
    tests = collected()
    modules = module_count()

    body = re.sub(r"\d+ collected", f"{tests} collected", body)
    body = re.sub(r"\d+ test modules", f"{modules} test modules", body)
    body = re.sub(r"tests/\s+\d+ modules", f"tests/         {modules} modules", body)

    # The ownership doc publishes the Python module count, and a test enforces
    # it -- the same recurring-red-suite problem the README counts had.
    jarvis = jarvis_module_count()
    ownership = REPO / "docs" / "FILE_OWNERSHIP.md"
    if ownership.is_file():
        doc = ownership.read_text(encoding="utf-8")
        updated = re.sub(r"\d+ Python modules", f"{jarvis} Python modules", doc)
        if updated != doc:
            ownership.write_text(updated, encoding="utf-8")
            print(f"FILE_OWNERSHIP updated: {jarvis} Python modules")

    if body == original:
        print(f"README already current: {tests} collected, {modules} modules")
        return 0

    README.write_text(body, encoding="utf-8")
    print(f"README updated: {tests} collected, {modules} modules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())