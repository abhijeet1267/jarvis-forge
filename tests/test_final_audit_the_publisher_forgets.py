"""The final acceptance audit's first finding is in the machinery that publishes its numbers.

`benchmarks/acceptance_run.py` is the command the README points at for every score it prints, and
its docstring says the suite history is "transcribed from audit/PHASE*_REPORT.md". It is not
transcribed: it is a hard-coded list in the script that stops at **Phase 45a, 1856 tests**. Running
the documented command rewrote `benchmarks/suite_history.json` from 53 recorded phases down to 37,
deleting Phases 46 through 61 -- including this audit's own subject -- and the chart is drawn from
that file.

Measured, not inferred:

    .venv/bin/python benchmarks/acceptance_run.py     # rows 53 -> 37
    git checkout benchmarks/suite_history.json        # restored to 53

Nothing was pushed and no published number was improved by this. The rule it violated is the one
this project already states -- historical rows are not deleted to move a metric -- and the harness
did it as a side effect of being run to *check* a metric.

The fix is additive: the literal list stays as the transcription of what each phase recorded, and
any row the file already holds that the list does not know about is kept.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _harness():
    spec = importlib.util.spec_from_file_location("acceptance_run",
                                                 ROOT / "benchmarks" / "acceptance_run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_newest_closed_phase_is_still_in_the_published_history():
    """The last row is the one a reader looks at, and it is the first one the harness dropped.

    Read as a guard, not a snapshot: the floor is the newest phase whose report records a
    *measured* full-suite run ("N passed, M failed"), and that phase must appear in the file
    the charts are drawn from. The chart is a transcription of measured runs, so its floor is
    a measured phase -- not the highest-numbered `PHASE*.md`, which also matches in-progress
    WIP reports (74-90 right now) that have not run a full suite and so have no number to
    transcribe. Counting those would set the floor to a phase the chart was never supposed to
    hold.
    """
    import re

    measured = set()
    for path in (ROOT / "audit").glob("PHASE*.md"):
        match = re.match(r"PHASE(\d+)", path.name)
        if not match:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # A measured full-suite run, as the committed reports record it: "N passed, M failed".
        # Focused-only runs ("Focused tests: 5 passed") do not count -- the chart tracks
        # full-suite numbers, so a focused-only phase is not a chart row.
        if re.search(r"\b\d+ passed, \d+ failed\b", text):
            measured.add(int(match.group(1)))
    assert measured, "no phase report records a measured full-suite run: the guard itself is broken"
    newest = max(measured)

    stored = json.loads((ROOT / "benchmarks" / "suite_history.json").read_text())["phases"]
    names = " ".join(row["phase"] for row in stored)
    assert re.search(rf"\b{newest}\b", names), (
        f"audit/ has a Phase {newest} report with a measured full-suite run but "
        "suite_history.json has no row for it -- the chart's data source lost a phase")


def test_running_the_harness_keeps_the_rows_it_did_not_write(tmp_path, monkeypatch):
    """The regression itself: a newer row survives the documented command."""
    harness = _harness()

    newer = {"phases": [
        {"phase": "9 audit", "collected": 1002, "passed": 875, "failed": 126, "note": "baseline"},
        {"phase": "61 hands behind a card", "collected": 2380, "passed": 2380, "failed": 0,
         "note": "written by the phase it describes"},
    ]}
    out = tmp_path / "suite_history.json"
    out.write_text(json.dumps(newer))
    monkeypatch.setattr(harness, "OUT", tmp_path, raising=False)

    result = harness.suite_history()

    names = [row["phase"] for row in result["phases"]]
    assert "61 hands behind a card" in names, names
    assert "45a a key outside the bundle" in names, "the transcribed rows are still there"
    assert len(names) == len(set(names)), "a phase must not be recorded twice"


def test_the_harness_does_not_renumber_a_row_it_is_given(tmp_path, monkeypatch):
    """Preserving is not rewriting: the note and the counts come back unchanged."""
    harness = _harness()
    row = {"phase": "99 something future", "collected": 9999, "passed": 9999, "failed": 0,
           "note": "the exact words the phase wrote"}
    (tmp_path / "suite_history.json").write_text(json.dumps({"phases": [row]}))
    monkeypatch.setattr(harness, "OUT", tmp_path, raising=False)

    kept = [r for r in harness.suite_history()["phases"] if r["phase"] == row["phase"]]

    assert kept == [row], kept
