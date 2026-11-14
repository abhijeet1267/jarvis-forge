"""Published counts must equal what the repository actually holds.

The README's `tests/` line said **99 modules · 2510 collected** while the tree
held 179 modules and collected 3393 tests. A published count is the number a
reader quotes to somebody else, so a stale one is a false claim — the same rule
the tool/route counts already follow.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _readme() -> str:
    return (REPO / "README.md").read_text()


def test_the_readme_test_module_count_is_real():
    match = re.search(r"tests/\s+(\d+) modules", _readme())
    assert match, "the README no longer publishes a module count"
    actual = len(list((REPO / "tests").glob("*.py")))
    assert int(match.group(1)) == actual, (
        f"the README says {match.group(1)} test modules; there are {actual}")


def test_the_readme_collected_count_matches_a_real_collection():
    """Collection only — no execution, so this guard is cheap."""
    import subprocess
    import sys

    match = re.search(r"(\d+) collected", _readme())
    assert match, "the README no longer publishes a collected count"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=str(REPO), capture_output=True, text=True, timeout=180)
    found = re.search(r"(\d+) tests collected", result.stdout)
    assert found, result.stdout[-400:]
    assert int(match.group(1)) == int(found.group(1)), (
        f"the README says {match.group(1)} collected; collection finds {found.group(1)}")


def test_the_readme_names_the_product_pages():
    """A page the README does not name is a page nobody knows exists."""
    text = _readme()
    for page in ("planner.html", "operator.html", "work.html", "phone.html"):
        assert page in text, f"the README's layout does not name {page}"
