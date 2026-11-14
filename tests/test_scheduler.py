"""Mission step scheduling: dependencies are barriers, shared resources serialise.

The scheduler decides only *which* steps may start together. These tests pin the
two guarantees that make concurrency safe, and the cases where refusing to
parallelise is the correct answer.
"""

from __future__ import annotations

import pytest

from jarvis.core.missions import MissionStep
from jarvis.core.scheduler import blocked_reason, ready_steps, schedule_summary


def _step(step_id, *, status="pending", path="", depends_on=()):
    params = {"path": path} if path else {}
    return MissionStep(step_id=step_id, title=step_id, tool="read_file",
                       params=params, status=status,
                       depends_on=list(depends_on))


# -- dependencies -----------------------------------------------------------

def test_independent_steps_are_all_ready():
    steps = [_step("S1"), _step("S2"), _step("S3")]
    assert [s.step_id for s in ready_steps(steps)] == ["S1", "S2", "S3"]


def test_a_dependent_step_waits_for_its_prerequisite():
    steps = [_step("S1"), _step("S2", depends_on=["S1"])]
    ready = [s.step_id for s in ready_steps(steps)]
    assert ready == ["S1"], "S2 cannot start before S1 finishes"


def test_a_dependent_step_becomes_ready_once_the_prerequisite_is_done():
    steps = [_step("S1", status="done"), _step("S2", depends_on=["S1"])]
    assert [s.step_id for s in ready_steps(steps)] == ["S2"]


def test_a_dependency_that_failed_still_unblocks_the_dependent():
    """'Finished' is what ordering needs; the runner decides what to do about it."""
    steps = [_step("S1", status="failed"), _step("S2", depends_on=["S1"])]
    assert [s.step_id for s in ready_steps(steps)] == ["S2"]


# -- resource conflicts -----------------------------------------------------

def test_two_steps_writing_the_same_file_are_not_both_ready():
    steps = [_step("S1", path="/tmp/p/a.txt"), _step("S2", path="/tmp/p/a.txt")]
    ready = [s.step_id for s in ready_steps(steps)]
    assert ready == ["S1"], "two writers to one file must serialise"


def test_two_steps_writing_different_files_may_run_together():
    steps = [_step("S1", path="/tmp/p/a.txt"), _step("S2", path="/tmp/p/b.txt")]
    assert len(ready_steps(steps)) == 2


def test_sibling_files_in_one_directory_may_run_together():
    """Different files in the same folder are independent writes.

    An earlier rule claimed the containing directory, which serialised every
    write in a shared folder and defeated the scheduler. The claim is the exact
    path: two steps naming different files do not conflict.
    """
    steps = [_step("S1", path="/tmp/p/sub/a.txt"),
             _step("S2", path="/tmp/p/sub/b.txt")]
    assert len(ready_steps(steps)) == 2


def test_one_step_naming_a_directory_conflicts_with_a_file_inside_it():
    """A step that claims a directory really does own what is under it."""
    steps = [_step("S1", path="/tmp/p/sub"),
             _step("S2", path="/tmp/p/sub/a.txt")]
    assert len(ready_steps(steps)) == 1


def test_a_step_whose_resource_is_being_written_now_waits():
    steps = [_step("S1", path="/tmp/p/a.txt", status="running"),
             _step("S2", path="/tmp/p/a.txt")]
    assert ready_steps(steps) == []


def test_steps_without_a_path_never_conflict():
    steps = [_step("S1"), _step("S2"), _step("S3")]
    assert len(ready_steps(steps)) == 3


def test_a_parent_segment_in_a_path_is_normalised_before_comparing():
    """`/tmp/p/../p/a.txt` is the same file as `/tmp/p/a.txt`."""
    steps = [_step("S1", path="/tmp/p/a.txt"),
             _step("S2", path="/tmp/p/../p/a.txt")]
    assert len(ready_steps(steps)) == 1


# -- the human-readable view ------------------------------------------------

def test_the_summary_names_what_is_ready_running_and_waiting():
    steps = [_step("S1", path="/tmp/p/a.txt", status="running"),
             _step("S2", path="/tmp/p/a.txt"),
             _step("S3", depends_on=["S1"]),
             _step("S4", status="done")]
    summary = schedule_summary(steps)

    assert summary["running"] == ["S1"]
    assert summary["finished"] == ["S4"]
    assert summary["ready"] == []
    waiting = {item["step_id"]: item["reason"] for item in summary["waiting"]}
    assert "S1" in waiting["S2"] and "writing" in waiting["S2"]
    assert "S1" in waiting["S3"] and "finish" in waiting["S3"]


def test_blocked_reason_is_empty_for_a_ready_step():
    assert blocked_reason(_step("S1"), [_step("S1")]) == ""


# -- safety ----------------------------------------------------------------

def test_a_cycle_does_not_hand_out_a_step_and_never_loops_forever():
    """Two steps depending on each other are simply never ready."""
    steps = [_step("S1", depends_on=["S2"]), _step("S2", depends_on=["S1"])]
    assert ready_steps(steps) == []


def test_a_missing_dependency_keeps_the_step_waiting():
    steps = [_step("S2", depends_on=["S-does-not-exist"])]
    assert ready_steps(steps) == []