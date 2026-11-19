"""Process observation: the identity it reports must be the process asked about.

Every test here reads the real process table via ``/bin/ps`` -- none mocks it --
except the one that deliberately makes the table unreadable, which must still
report UNKNOWN rather than "not running".

The defect being fixed: the observer used to ask ``needle.lower() in
table.lower()`` over the whole ``ps -axo comm=`` output. Asking whether
"Calculator" runs said yes when only "CalculatorHelper" was running, which could
let a mission claim VERIFIED for the wrong thing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

import pytest

from jarvis.core.outcome_assurance import (
    _names_match,
    observe_process,
    observer_for_spec,
)


# -- a uniquely named process, so "exists" and "absent" are deterministic -----

class UniqueProcess:
    """A process whose name cannot collide with anything else on the machine.

    A symlink to ``/bin/sleep`` gives the child a unique executable basename;
    macOS ``ps -axo comm=`` reports the symlink path, which is the name in the
    table an observer reads.
    """

    def __init__(self, tmp_path, suffix: str = ""):
        self.suffix = suffix
        self.uniq = f"vantage-probe-{uuid.uuid4().hex}{suffix}"
        link = tmp_path / self.uniq
        os.symlink("/bin/sleep", link)
        self.path = str(link)
        self.proc = None

    @property
    def basename(self) -> str:
        return self.uniq

    @property
    def stem(self) -> str:
        """The name without ``suffix`` -- the needle that must *not* match."""
        return self.uniq[: -len(self.suffix)] if self.suffix else self.uniq

    def start(self, seconds: str = "30") -> "UniqueProcess":
        self.proc = subprocess.Popen([self.path, seconds])
        # Wait until ps can see it, bounded -- never assume a sleep duration.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if observe_process(self.uniq, timeout=5.0).status == "verified":
                return self
            time.sleep(0.05)
        raise AssertionError(f"{self.uniq} never appeared in the process table")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


@pytest.fixture
def unique_process(tmp_path):
    with UniqueProcess(tmp_path) as helper:
        yield helper


# -- the precise identity function, directly ---------------------------------

def test_exact_name_matches_the_executable_basename():
    assert _names_match("/Applications/Foo.app/Contents/MacOS/Calculator", "Calculator")


def test_a_substring_neighbour_does_not_match_the_exact_name():
    """"CalculatorHelper" is a different process; asking for "Calculator" must
    not be answered by it."""
    assert not _names_match("/usr/libexec/CalculatorHelper", "Calculator")


def test_the_match_is_case_insensitive():
    assert _names_match("/bin/CALCULATOR", "calculator")
    assert _names_match("/bin/calculator", "CALCULATOR")


def test_a_needle_that_is_a_path_is_compared_by_its_basename():
    assert _names_match("/bin/Calculator", "/Applications/Calculator.app/Contents/MacOS/Calculator")
    assert not _names_match("/usr/libexec/CalculatorHelper",
                            "/Applications/Calculator.app/Contents/MacOS/Calculator")


def test_empty_name_never_matches():
    assert not _names_match("/bin/sleep", "")
    assert not _names_match("", "sleep")


def test_substring_matching_is_still_available_when_asked():
    assert _names_match("/usr/libexec/CalculatorHelper", "Calculator", exact=False)
    assert not _names_match("/usr/libexec/CalculatorHelper", "Calculator", exact=True)


def test_a_trailing_slash_does_not_hide_the_basename():
    assert _names_match("/usr/bin/sleep/", "sleep")


# -- the observer against the real table -------------------------------------

def test_a_real_running_process_is_observed_present(unique_process):
    obs = observe_process(unique_process.basename)
    assert obs.status == "verified", obs.detail
    assert unique_process.basename in obs.detail


def test_the_current_python_interpreter_is_observed_present():
    """A second, independent real process: the interpreter running this test."""
    name = os.path.basename(sys.executable)
    obs = observe_process(name)
    assert obs.status == "verified", obs.detail


def test_a_name_that_does_not_exist_is_observed_absent():
    obs = observe_process(f"vantage-not-{uuid.uuid4().hex}")
    assert obs.status == "failed", obs.detail
    assert "not in the process table" in obs.detail


def test_absent_on_a_name_that_does_not_exist_is_verified():
    obs = observe_process(f"vantage-not-{uuid.uuid4().hex}", absent=True)
    assert obs.status == "verified", obs.detail


def test_absent_on_a_running_process_fails_after_the_grace(unique_process):
    started = time.monotonic()
    obs = observe_process(unique_process.basename, absent=True, grace=0.5)
    elapsed = time.monotonic() - started
    assert obs.status == "failed", obs.detail
    assert "still running" in obs.detail
    assert elapsed >= 0.4, f"grace was not honoured ({elapsed:.3f}s)"


def test_absent_with_zero_grace_returns_quickly_on_a_running_process(unique_process):
    """grace=0 must not wait: the budget is a single read plus subprocess time,
    far under the timeout+grace a broken loop would spend."""
    started = time.monotonic()
    obs = observe_process(unique_process.basename, absent=True, grace=0.0, timeout=5.0)
    elapsed = time.monotonic() - started
    assert obs.status == "failed", obs.detail
    assert elapsed < 2.0, f"grace=0 took {elapsed:.3f}s"


def test_the_grace_window_ends_early_when_the_process_is_genuinely_gone(tmp_path):
    """The documented reason grace exists: a process mid-exit. Start it, ask,
    then kill it during the wait -- the loop must notice and verify, not burn
    the whole grace."""
    helper = UniqueProcess(tmp_path).start()
    try:
        def _kill_soon():
            time.sleep(0.15)
            helper.stop()

        import threading

        threading.Thread(target=_kill_soon, daemon=True).start()
        started = time.monotonic()
        obs = observe_process(helper.basename, absent=True, grace=5.0, timeout=5.0)
        elapsed = time.monotonic() - started
        assert obs.status == "verified", obs.detail
        assert elapsed < 4.5, f"loop did not end early ({elapsed:.3f}s)"
    finally:
        helper.stop()


# -- the precision defect, against a real process table ----------------------

def test_a_process_whose_name_merely_contains_the_needle_does_not_match(tmp_path):
    """The real-world false positive: a sibling binary named ``<needle>Helper``
    is running while ``<needle>`` is not. The exact observer must say absent;
    the historical substring test would have said present."""
    helper = UniqueProcess(tmp_path, suffix="helper").start()
    try:
        exact = observe_process(helper.stem)
        assert exact.status == "failed", exact.detail
        assert exact.status != "verified"

        # Proof the old rule really would have matched: the substring is there.
        table = subprocess.run(["/bin/ps", "-axo", "comm="], capture_output=True,
                               text=True, timeout=5).stdout
        assert helper.stem.lower() in table.lower()
        assert _names_match(helper.path, helper.stem, exact=False)

        # And the explicit opt-out still sees it.
        loose = observe_process(helper.stem, exact=False)
        assert loose.status == "verified", loose.detail
    finally:
        helper.stop()


def test_the_spec_parses_exact_and_defaults_to_precise(tmp_path):
    helper = UniqueProcess(tmp_path, suffix="helper").start()
    try:
        default_obs = observer_for_spec(json.dumps(
            {"kind": "process_running", "process": helper.stem}))
        assert default_obs is not None
        assert default_obs().status == "failed"

        loose_obs = observer_for_spec(json.dumps(
            {"kind": "process_running", "process": helper.stem, "exact": False}))
        assert loose_obs is not None
        assert loose_obs().status == "verified"
    finally:
        helper.stop()


# -- an unreadable table is UNKNOWN, never "not running" ---------------------

def test_an_unreadable_table_is_unknown_not_absent(monkeypatch):
    def unreadable(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(subprocess, "run", unreadable)
    obs = observe_process("launchd", absent=True)
    assert obs.status == "unknown", obs.detail
    assert "could not be read" in obs.detail

    obs = observe_process("launchd")
    assert obs.status == "unknown", obs.detail


def test_a_ps_that_exits_nonzero_is_unknown(monkeypatch):
    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Result())
    obs = observe_process("launchd")
    assert obs.status == "unknown", obs.detail


def test_an_empty_process_name_is_unknown():
    obs = observe_process("   ")
    assert obs.status == "unknown", obs.detail
