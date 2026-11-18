"""Mission diagnosis: read a failure off the record, never invent a cause.

The module under test is a pure reading of mission and step records. These
tests pin what it is allowed to say: the recorded error appears, a failure
with no verification leaves the outcome explicitly unknown, a completed run is
not distorted into a failure, and every suggested next step stays
non-destructive.
"""

from __future__ import annotations

import pytest

from jarvis.core.mission_diagnosis import Diagnosis, diagnose, render

_DESTRUCTIVE = ("delete", "remove", "overwrite", "kill", "terminate", "rm ")

def _failed_mission(**overrides):
    mission = {
        "mission_id": "MIS-test",
        "request": "write the weekly report",
        "outcome": "the weekly report exists and mentions revenue",
        "state": "failed",
        "reason": "step S2 failed: the disk is full",
    }
    mission.update(overrides)
    return mission

def _steps_with_error():
    return [
        {"step_id": "S1", "title": "read the notes", "tool": "read_file",
         "status": "done", "detail": "read 3 notes from disk"},
        {"step_id": "S2", "title": "write the report", "tool": "write_file",
         "status": "failed", "detail": "no space left on device"},
    ]

# -- a recorded error is reported verbatim ---------------------------------

def test_a_failed_step_with_a_recorded_error_reports_that_error():
    diag = diagnose(_failed_mission(), _steps_with_error())

    assert isinstance(diag, Diagnosis)
    assert diag.failed_step == "S2"
    assert diag.error == "no space left on device"
    assert "no space left on device" in render(diag)

def test_the_last_completed_step_is_the_one_before_the_failure():
    diag = diagnose(_failed_mission(), _steps_with_error())

    assert diag.last_completed_step == "S1"

def test_the_runner_style_detail_field_is_read_as_the_error():
    # `MissionStep.to_dict` has no `error` key; the runner stores the failure
    # text in `detail`. This pins that the real schema is what is read.
    steps = [{"step_id": "S1", "title": "ping the host", "tool": "run_command",
              "status": "failed", "detail": "the tool raised: connection refused"}]

    diag = diagnose(_failed_mission(), steps)

    assert diag.failed_step == "S1"
    assert diag.error == "the tool raised: connection refused"

def test_an_explicit_error_key_wins_over_detail_when_both_are_present():
    steps = [{"step_id": "S1", "status": "failed",
              "error": "specific failure", "detail": "generic failure"}]

    diag = diagnose(_failed_mission(), steps)

    assert diag.error == "specific failure"

def test_a_failed_step_with_no_error_text_says_so_instead_of_inventing_one():
    steps = [{"step_id": "S1", "title": "do a thing", "tool": "some_tool",
              "status": "failed"}]

    diag = diagnose(_failed_mission(), steps)

    assert diag.failed_step == "S1"
    assert diag.error == ""
    assert any("no error text" in observation for observation in diag.observations)

# -- missing verification means unknown, not success -----------------------

def test_a_failed_step_with_no_verification_leaves_the_outcome_unknown():
    diag = diagnose(_failed_mission(), _steps_with_error())

    assert diag.outcome_unknown is True
    text = render(diag)
    assert "could not be confirmed" in text
    lowered = text.lower()
    assert "succeeded" not in lowered
    assert "success" not in lowered

def test_a_step_whose_verification_status_is_unknown_leaves_the_outcome_unknown():
    steps = [{"step_id": "S1", "title": "write the file", "tool": "write_file",
              "status": "failed", "detail": "the tool raised: timed out",
              "verification": {"status": "unknown",
                               "observation": "the file could not be read back"}}]

    diag = diagnose(_failed_mission(), steps)

    assert diag.outcome_unknown is True
    assert "could not be confirmed" in render(diag)

def test_an_empty_verification_status_is_treated_as_unknown():
    steps = [{"step_id": "S1", "status": "failed", "detail": "boom",
              "verification": {}}]

    diag = diagnose(_failed_mission(), steps)

    assert diag.outcome_unknown is True

def test_a_known_verification_status_settles_the_outcome():
    steps = [{"step_id": "S1", "status": "failed", "detail": "boom",
              "verification": {"status": "failed",
                               "observation": "the file does not contain the needle"}}]

    diag = diagnose(_failed_mission(), steps)

    assert diag.outcome_unknown is False
    assert any("failed" in observation for observation in diag.observations)

def test_the_step_satisfies_join_is_read_when_no_verification_is_on_the_step():
    mission = _failed_mission(conditions=[
        {"condition_id": "C1", "statement": "the report exists",
         "status": "verified", "observation": "the file was re-read and hashes match"},
    ])
    steps = [{"step_id": "S1", "status": "failed", "detail": "boom",
              "satisfies": "C1"}]

    diag = diagnose(mission, steps)

    assert diag.outcome_unknown is False

# -- a fully completed run is not reported as a failure --------------------

def test_all_steps_completed_yields_no_failed_step():
    mission = {
        "mission_id": "MIS-ok",
        "request": "read the notes",
        "outcome": "the notes were read",
        "state": "completed",
        "reason": "every acceptance condition was independently verified",
        "conditions": [{"condition_id": "C1", "statement": "notes were read",
                        "status": "verified", "observation": "file re-read"}],
    }
    steps = [
        {"step_id": "S1", "title": "read one", "tool": "read_file",
         "status": "done", "detail": "ok"},
        {"step_id": "S2", "title": "read two", "tool": "read_file",
         "status": "done", "detail": "ok"},
    ]

    diag = diagnose(mission, steps)

    assert diag.failed_step is None
    assert diag.error == ""
    assert diag.outcome_unknown is False
    assert diag.last_completed_step == "S2"
    assert "marked done" in render(diag)

# -- safe next steps are never destructive ---------------------------------

@pytest.mark.parametrize("mission,steps", [
    (_failed_mission(), _steps_with_error()),
    (_failed_mission(), [{"step_id": "S1", "status": "failed",
                          "detail": "not executed: denied by policy"}]),
    (_failed_mission(reason=""), [{"step_id": "S1", "status": "failed"}]),
    ({"request": "x", "outcome": "y", "state": "running"},
     [{"step_id": "S1", "status": "done"}]),
    ({}, []),
])
def test_safe_next_steps_never_contains_a_destructive_verb(mission, steps):
    diag = diagnose(mission, steps)

    assert diag.safe_next_steps, "a diagnosis with no safe next step is not useful"
    blob = " ".join(diag.safe_next_steps).lower()
    for verb in _DESTRUCTIVE:
        assert verb not in blob, f"{verb!r} appeared in safe_next_steps: {blob!r}"

def test_a_refusal_in_the_failure_text_offers_requesting_approval():
    steps = [{"step_id": "S1", "status": "failed",
              "detail": "not executed: denied by policy: needs approval"}]

    diag = diagnose(_failed_mission(), steps)

    assert any("permission" in step or "approval" in step
               for step in diag.safe_next_steps)

# -- missing or empty inputs are answered, not raised ---------------------

def test_empty_inputs_do_not_raise():
    diag = diagnose({}, [])

    assert diag.intended == ""
    assert diag.failed_step is None
    assert diag.last_completed_step is None
    assert diag.outcome_unknown is True
    assert diag.safe_next_steps
    assert render(diag)

def test_none_inputs_do_not_raise():
    diag = diagnose(None, None)  # type: ignore[arg-type]

    assert diag.failed_step is None
    assert diag.observations

def test_unreadable_step_entries_are_ignored_and_noted():
    diag = diagnose(_failed_mission(), [None, 3, "S1", {"step_id": "S1",
                                                       "status": "failed"}])

    assert diag.failed_step == "S1"
    assert any("not readable" in observation for observation in diag.observations)

def test_the_mission_rows_own_steps_are_used_when_the_argument_is_empty():
    steps = [{"step_id": "S1", "status": "failed", "detail": "boom"}]
    mission = _failed_mission(steps=steps)

    diag = diagnose(mission, [])

    assert diag.failed_step == "S1"

def test_absent_mission_fields_are_reported_as_not_recorded():
    diag = diagnose({"steps": [{"step_id": "S1", "status": "failed"}]},
                    [{"step_id": "S1", "status": "failed"}])

    assert diag.intended == ""
    assert any("intended outcome was not recorded" in observation
               for observation in diag.observations)
    assert any("state was not recorded" in observation
               for observation in diag.observations)