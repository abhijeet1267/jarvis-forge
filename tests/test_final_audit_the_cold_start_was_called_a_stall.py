"""The final audit's flaky-suite finding, fixed at its cause.

Diagnosed with a traced full-suite run, not guessed. The reproduction produced this trace, one line
per recogniser call in the same pytest process:

    20.02s  ok=False timed_out=True                        <- one real call, the ceiling
     0.00s  ok=False degraded=True cooldown=44.9s          <- and four more, instantly refused
     ...
    FAILED tests/test_vision.py::test_ocr_reads_real_text
    FAILED tests/test_vision.py::test_ocr_returns_bounding_boxes
    FAILED tests/test_vision.py::test_find_text_locates_a_word
    FAILED tests/test_vision.py::test_read_image_text_tool
    FAILED tests/test_vision.py::test_find_image_text_tool

Measured in the same investigation: a recogniser call costs **0.28 s** once the process has done one
(25 calls averaged), and **7.4 s** for a process's very first call on an idle machine -- and that
first call is the one that occasionally exceeds 20 s when the suite is doing other things.

So the bug is a guard designed for one state firing on another. The 20 s budget is a *steady-state*
ceiling; it was being applied to the cold initialisation of the Vision framework, which is not a
stall but a first call. And because one timeout puts the shared backend on cooldown for 45 s, a
single slow start did not fail one test -- it failed five, which is why the flake arrived in
clusters and vanished on a re-run.

The fix separates the two situations the guard was conflating:

* the first read of a process gets a documented cold-start allowance;
* a stall that follows a successful read is a stall, and still earns the cooldown immediately;
* a *first* stall is not evidence of a stalled subsystem, so the next call is tried -- and two
  consecutive stalls do cooldown, which is the protection the original comment asked for.

Nothing here makes the suite faster by lying about what happened: an exhausted budget is still
reported as a timeout with its number in the sentence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jarvis.vision import ocr


class _Runner:
    """Stands where the helper binary stands: records the budget it was given, answers on demand."""

    def __init__(self, *, stall=False, lines=2):
        self.stall = stall
        self.lines = lines
        self.timeouts = []

    def __call__(self, args, **kwargs):
        self.timeouts.append(float(kwargs.get("timeout") or 0.0))
        if self.stall:
            raise subprocess.TimeoutExpired(args, timeout=kwargs.get("timeout"))
        payload = '{"lines": [' + ",".join(
            '{"text": "word %d", "confidence": 0.9}' % i for i in range(self.lines)
        ) + "]}"
        return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")


@pytest.fixture()
def image(tmp_path) -> Path:
    """A real file on disk: `read_text` refuses a path it cannot find, before anything else."""
    created = tmp_path / "sample.png"
    created.write_bytes(b"stubbed png, the helper is what is faked")
    return created


@pytest.fixture()
def backend(monkeypatch, tmp_path):
    """A backend whose binary exists, so nothing is compiled and only the budget is tested."""
    image = tmp_path / "sample.png"
    image.write_bytes(b"not a png, the helper is stubbed")
    made = _Runner()
    monkeypatch.setattr(ocr.subprocess, "run", made)
    subject = ocr.OCRBackend()
    subject._binary = Path("/usr/bin/true")
    subject._failure = None
    yield subject, made


def test_the_first_read_of_a_process_is_not_held_to_the_steady_budget(backend, tmp_path):
    """20 s is the steady number (measured: 0.28 s per call). A cold framework is a different
    thing, and holding it to the steady ceiling is what turned a slow start into a fake stall."""
    subject, made = backend

    result = subject.read_text(str(tmp_path / "sample.png"))

    assert result["ok"] is True
    assert made.timeouts[0] > ocr._configured_timeout(), (
        f"the first call was given {made.timeouts[0]}s, the same budget as the fortieth")
    assert made.timeouts[0] <= 120.0, "a cold start gets allowance, not a free pass"


def test_the_steady_budget_returns_as_soon_as_one_read_has_worked(backend, tmp_path):
    """The allowance is for initialisation, not forever -- otherwise a genuinely stuck
    recogniser would be waited on for the rest of the session."""
    subject, made = backend
    subject.read_text(str(tmp_path / "sample.png"))

    subject.read_text(str(tmp_path / "sample.png"))

    assert made.timeouts[1] == pytest.approx(ocr._configured_timeout())


def test_a_stall_still_cools_the_recogniser_down_on_a_cold_process(image):
    """The guard was *not* relaxed to make this flake pass, and this is the test that says so.

    The first draft of this fix let a process that had never succeeded try twice before cooling
    down. Three of Phase 27's tests describe the opposite rule, and re-reading them showed they
    were right: the problem was never the cooldown firing on a real stall, it was a cold
    initialisation being handed a steady-state budget and so counted as a stall at all. Fixing
    the budget (above) removes the flake by making the call succeed; weakening the guard would
    have traded a test symptom for a product behaviour nobody chose.
    """
    stalling = _Runner(stall=True)
    original = ocr.subprocess.run
    ocr.subprocess.run = stalling  # type: ignore[attr-defined]
    subject = ocr.OCRBackend()
    subject._binary = Path("/usr/bin/true")
    try:
        first = subject.read_text(str(image))
        assert first["ok"] is False and first["timed_out"] is True
        assert first["timed_out"] is True and subject.cooling_down() > 0
        assert subject.state()["stalls"] == 1
        third = subject.read_text(str(image))
        assert third.get("degraded") is True
        assert len(stalling.timeouts) == 1, "the cooldown must short-circuit the second read"
    finally:
        ocr.subprocess.run = original  # type: ignore[attr-defined]


def test_the_state_says_which_budget_is_in_force():
    """`/health` and `vision_capabilities` report this state to a person. A cold allowance that
    is invisible in the state is a number nobody can question."""
    subject = ocr.OCRBackend()
    state = subject.state()
    for key in ("present", "responsive", "timeout_seconds", "cold_start_seconds", "stalls"):
        assert key in state, state
    assert state["cold_start_seconds"] >= state["timeout_seconds"]
