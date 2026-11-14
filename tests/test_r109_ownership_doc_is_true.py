"""The ownership doc must agree with the tree it describes.

`docs/FILE_OWNERSHIP.md` is the map a contributor uses to decide what is safe to
change together, so a stale entry misroutes real work. The dashboard row omitted
three shipped pages, and the module count had drifted by ten.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_the_dashboard_row_names_every_shipped_page():
    """Only *served* pages: `ai_settings.html` is abandoned owner WIP with no route.

    The doc records what ships, not everything on disk — that page was
    deliberately dropped from it (iteration 72), so globbing the directory would
    make this guard demand documenting a page nobody can reach.
    """
    doc = (REPO / "docs" / "FILE_OWNERSHIP.md").read_text()
    app = (REPO / "jarvis" / "api" / "app.py").read_text()
    row = next((ln for ln in doc.splitlines() if "**Dashboard (web UI)**" in ln), "")
    assert row, "the dashboard ownership row is gone"
    served = [p.name for p in (REPO / "dashboard").glob("*.html")
              if f'"{p.name}"' in app]
    assert served, "no dashboard page is referenced by the API"
    missing = [name for name in served if name not in row]
    assert not missing, f"the ownership doc does not name the served page(s) {missing}"


def test_the_module_count_matches_the_tree():
    doc = (REPO / "docs" / "FILE_OWNERSHIP.md").read_text()
    match = re.search(r"(\d+) Python modules", doc)
    assert match, "the doc no longer states a module count"
    actual = len([p for p in (REPO / "jarvis").rglob("*.py")
                  if "__pycache__" not in p.parts])
    assert int(match.group(1)) == actual, (
        f"the doc says {match.group(1)} modules; the tree has {actual}")
