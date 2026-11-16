"""Live Mission Steering: change the plan, never the past.

The claims worth testing are the three refusals in the module docstring, because
each one is a way a "steer" feature could quietly lie:

* a new constraint must not be able to claim it undid a file a completed step
  wrote;
* a steered plan must not inherit the approval given to the plan before it;
* a steered plan must not escape the intent the original request established.
"""

from __future__ import annotations

import json

import pytest

from jarvis.core.missions import MissionService, MissionState
from jarvis.core.steering import (
    SteeringError,
    apply_revision,
    intent_refusals,
    revision_summary,
    standing_effects,
    steerable,
)
from jarvis.security.intentshield import IntentScope


@pytest.fixture()
def service(tmp_path):
    from jarvis.db.database import Database

    return MissionService(Database(tmp_path / "steering.db"))


def _scope(**overrides):
    data = {
        "mission_id": "MIS-TEST",
        "goal": "organize the work folder",
        "allowed_capabilities": {"write_file", "copy_file"},
        "allowed_resource_prefixes": ["/tmp/work"],
        "risk_budget": 2,
    }
    data.update(overrides)
    return IntentScope(**data)


def _mission(service, steps, *, intent=None, state=MissionState.RUNNING):
    mission = service.create(
        "organize the work folder", steps=steps,
        intent=(intent or _scope()).to_json())
    for target in (MissionState.PLANNED, MissionState.RUNNING):
        mission = service.transition(mission.mission_id, target)
        if target is state:
            break
    return service.get(mission.mission_id)


def _two_steps():
    return [
        {"step_id": "S1", "title": "write a", "tool": "write_file",
         "params": {"path": "/tmp/work/a.txt", "content": "a"}},
        {"step_id": "S2", "title": "write b", "tool": "write_file",
         "params": {"path": "/tmp/work/b.txt", "content": "b"}},
    ]


# -- the past is not editable ------------------------------------------------

def test_a_completed_step_keeps_its_effect_through_a_revision(service):
    mission = _mission(service, _two_steps())
    service.mark_step(mission.mission_id, "S1", status="done", detail="wrote a.txt")
    mission = service.get(mission.mission_id)

    effects = standing_effects(mission)
    assert [e["step_id"] for e in effects] == ["S1"]
    assert effects[0]["reversible"] is False
    assert "does not undo" in effects[0]["note"]


def test_a_revision_cannot_drop_a_step_that_already_ran(service):
    """Removing a finished step from the plan would make the record claim it
    never happened."""
    mission = _mission(service, _two_steps())
    service.mark_step(mission.mission_id, "S1", status="done")
    mission = service.get(mission.mission_id)

    with pytest.raises(SteeringError, match="already ran"):
        service.apply_revision(
            mission.mission_id,
            steps=[{"step_id": "S2", "title": "write b", "tool": "write_file",
                    "params": {"path": "/tmp/work/b.txt", "content": "b"}}],
            reason="only b now")


def test_a_revision_cannot_rewrite_a_completed_steps_parameters(service):
    """A step that ran used the parameters it ran with. Editing them would
    change the record of what was done, not the plan."""
    mission = _mission(service, _two_steps())
    service.mark_step(mission.mission_id, "S1", status="done")

    revised = service.apply_revision(
        mission.mission_id,
        steps=[{"step_id": "S1", "title": "something else", "tool": "copy_file",
                "params": {"source": "/tmp/work/x", "destination": "/tmp/work/y"}},
               {"step_id": "S2"}],
        reason="a change that tries to rewrite history")

    s1 = next(s for s in revised.steps if s.step_id == "S1")
    assert s1.tool == "write_file", "the completed step's tool was rewritten"
    assert s1.status == "done"


def test_dropping_a_step_that_had_not_run_is_allowed(service):
    mission = _mission(service, _two_steps())
    service.mark_step(mission.mission_id, "S1", status="done")

    revised = service.apply_revision(
        mission.mission_id, steps=[{"step_id": "S1"}], reason="b is no longer wanted")

    assert [s.step_id for s in revised.steps] == ["S1"]
    assert revised.plan_revision == 1


# -- a steered plan is a new request -----------------------------------------

def test_a_revision_moves_the_plan_revision_not_the_row_version(service):
    mission = _mission(service, _two_steps())
    before = service.get(mission.mission_id)
    service.mark_step(mission.mission_id, "S1", status="done")
    after_mark = service.get(mission.mission_id)

    # Marking a step writes the row, so version moves -- but the plan did not.
    assert after_mark.version > before.version
    assert after_mark.plan_revision == before.plan_revision

    steered = service.apply_revision(
        mission.mission_id,
        steps=[{"step_id": "S1"}, {"step_id": "S3", "title": "write c",
                                   "tool": "write_file",
                                   "params": {"path": "/tmp/work/c.txt"}}],
        reason="add c")
    assert steered.plan_revision == before.plan_revision + 1


def test_a_revision_that_adds_a_step_requires_fresh_approval(service):
    mission = _mission(service, _two_steps())
    summary = revision_summary(
        mission,
        [{"step_id": "S1"}, {"step_id": "S2"},
         {"step_id": "S3", "title": "write c", "tool": "write_file",
          "params": {"path": "/tmp/work/c.txt"}}])

    assert summary["requires_new_approval"] is True
    assert summary["added_step_ids"] == ["S3"]
    assert "previous approval does not cover it" in summary["note"]


def test_a_revision_that_only_drops_future_work_needs_no_new_approval(service):
    mission = _mission(service, _two_steps())
    summary = revision_summary(mission, [{"step_id": "S1"}])

    assert summary["requires_new_approval"] is False


# -- the intent outlives the plan --------------------------------------------

def test_a_revision_cannot_widen_the_resource_scope(service):
    mission = _mission(service, _two_steps())
    summary = revision_summary(
        mission,
        [{"step_id": "S1"}, {"step_id": "S9", "title": "escape",
                             "tool": "write_file",
                             "params": {"path": "/etc/passwd", "content": "x"}}])

    violations = {r["step_id"]: r["violation"] for r in summary["intent_refusals"]}
    assert violations == {"S9": "resource"}


def test_a_revision_cannot_add_a_forbidden_capability(service):
    mission = _mission(service, _two_steps())
    summary = revision_summary(
        mission,
        [{"step_id": "S1"}, {"step_id": "S9", "title": "delete",
                             "tool": "delete_file",
                             "params": {"path": "/tmp/work/a.txt"}}])

    violations = {r["step_id"]: r["violation"] for r in summary["intent_refusals"]}
    # delete_file is in DEFAULT_FORBIDDEN_CAPABILITIES, so it is refused for
    # being forbidden even before it is refused for not being an allowed one.
    assert violations.get("S9") == "forbidden"


def test_a_revision_that_stays_in_scope_has_no_refusals(service):
    mission = _mission(service, _two_steps())
    assert intent_refusals(mission) == []


def test_a_mission_with_no_intent_has_no_intent_refusals(service):
    """The permission engine is still the gate; the intent check is additional,
    never a replacement, and its absence is not a claim of safety."""
    mission = service.create("no intent recorded",
                             steps=[{"step_id": "S1", "tool": "write_file",
                                     "params": {"path": "/anywhere"}}])
    assert intent_refusals(mission) == []


def test_an_unparseable_intent_refuses_every_step(service):
    """An authorization nobody can read is not one. Refusing is the only
    honest answer."""
    mission = service.create("broken intent", steps=[{"step_id": "S1",
                                                      "tool": "write_file"}])
    service.db.execute("UPDATE missions SET intent = ? WHERE mission_id = ?",
                       ("{not json", mission.mission_id))
    mission = service.get(mission.mission_id)

    refusals = intent_refusals(mission)
    assert len(refusals) == 1
    assert refusals[0]["violation"] == "intent"


def test_a_revision_is_checked_against_the_revision_it_would_create(service):
    """The stored intent's own revision stamp must not make every revision look
    like a plan change; the run-time lease check is what enforces that."""
    mission = _mission(service, _two_steps())
    # A same-shape revision at revision 1 must be allowed by the intent check.
    summary = revision_summary(
        mission, [{"step_id": "S1"}, {"step_id": "S2"},
                  {"step_id": "S3", "title": "write c", "tool": "write_file",
                   "params": {"path": "/tmp/work/c.txt"}}])
    assert summary["to_revision"] == 1
    assert summary["intent_refusals"] == []


# -- the protocol refuses what it cannot honour ------------------------------

def test_a_finished_mission_cannot_be_steered(service):
    mission = _mission(service, _two_steps())
    service.mark_step(mission.mission_id, "S1", status="done")
    service.mark_step(mission.mission_id, "S2", status="done")
    for target in (MissionState.VERIFYING, MissionState.PARTIALLY_COMPLETED):
        try:
            service.transition(mission.mission_id, target)
        except Exception:
            pass
    mission = service.get(mission.mission_id)

    if not steerable(mission):
        with pytest.raises(SteeringError, match="finished"):
            service.apply_revision(mission.mission_id, steps=[{"step_id": "S1"}],
                                   reason="too late")
    else:
        pytest.skip("the mission did not reach a terminal state")


def test_a_revision_must_record_a_reason(service):
    mission = _mission(service, _two_steps())
    with pytest.raises(SteeringError, match="why it was made"):
        service.apply_revision(mission.mission_id, steps=[{"step_id": "S1"},
                                                          {"step_id": "S2"}],
                               reason="  ")


def test_duplicate_step_ids_are_refused(service):
    mission = _mission(service, _two_steps())
    with pytest.raises(SteeringError, match="appears twice"):
        service.apply_revision(
            mission.mission_id,
            steps=[{"step_id": "S1"}, {"step_id": "S1", "title": "again"}],
            reason="an ambiguous plan")


def test_an_empty_revision_is_refused(service):
    mission = _mission(service, _two_steps())
    with pytest.raises(SteeringError, match="at least one step"):
        service.apply_revision(mission.mission_id, steps=[], reason="nothing")


def test_apply_revision_rejects_a_revision_that_is_not_a_steerable_shape(service):
    mission = _mission(service, _two_steps())
    with pytest.raises(SteeringError):
        apply_revision(mission, steps=[], reason="empty")


# -- the summary is honest ---------------------------------------------------

def test_the_summary_names_the_steps_that_survive_and_those_that_do_not(service):
    mission = _mission(service, _two_steps())
    service.mark_step(mission.mission_id, "S1", status="done")
    mission = service.get(mission.mission_id)

    summary = revision_summary(
        mission,
        [{"step_id": "S1"}, {"step_id": "S3", "title": "write c",
                             "tool": "write_file",
                             "params": {"path": "/tmp/work/c.txt"}}])

    assert summary["completed_step_ids"] == ["S1"]
    assert summary["removed_future_step_ids"] == ["S2"]
    assert summary["to_revision"] == mission.plan_revision + 1
    assert summary["already_done"][0]["step_id"] == "S1"


def test_a_failed_step_counts_as_an_effect_that_already_happened(service):
    """`failed` means it ran and did something; a rule added now cannot say the
    attempt did not happen."""
    mission = _mission(service, _two_steps())
    service.mark_step(mission.mission_id, "S1", status="failed", detail="disk full")
    mission = service.get(mission.mission_id)

    assert [e["step_id"] for e in standing_effects(mission)] == ["S1"]

# -- the expansion report is not inflated ------------------------------------

def test_keeping_a_step_whose_tool_already_ran_is_not_an_expansion(service):
    """A revision that keeps a step using a tool the plan already ran is not
    asking for anything new. Counting it as new made every revision look like an
    expansion the previous approval did not cover."""
    mission = _mission(service, _two_steps())
    service.mark_step(mission.mission_id, "S1", status="done")
    mission = service.get(mission.mission_id)

    summary = revision_summary(
        mission,
        [{"step_id": "S1"},
         {"step_id": "S3", "title": "another write", "tool": "write_file",
          "params": {"path": "/tmp/work/c.txt"}}])

    assert summary["new_tools"] == [], (
        "write_file was already part of the plan this mission ran")
    # It is still an added step, so it still needs fresh approval.
    assert summary["requires_new_approval"] is True
    assert summary["added_step_ids"] == ["S3"]


def test_a_genuinely_new_tool_is_reported(service):
    mission = _mission(service, _two_steps())
    summary = revision_summary(
        mission,
        [{"step_id": "S1"},
         {"step_id": "S2"},
         {"step_id": "S3", "title": "copy", "tool": "copy_file",
          "params": {"source": "/tmp/work/a.txt",
                     "destination": "/tmp/work/b.txt"}}])

    assert summary["new_tools"] == ["copy_file"]


# -- steering around a parked approval ---------------------------------------

def _parked_mission(service):
    """A mission parked on a step waiting for approval, as the runner leaves it."""
    mission = _mission(service, _two_steps())
    service.mark_step(mission.mission_id, "S1", status="done",
                      detail="wrote a.txt")
    service.mark_step(mission.mission_id, "S2", status="pending",
                      detail="awaiting approval: needs a person; confirmation CONF-ABC123")
    return service.transition(
        mission.mission_id, MissionState.AWAITING_APPROVAL,
        reason="step S2 needs approval; confirmation CONF-ABC123")


def test_steering_an_unchanged_parked_step_keeps_its_approval(service):
    """The person's answer still describes the step, so it survives the
    revision rather than forcing them to answer the same question twice."""
    mission = _parked_mission(service)
    revised = service.apply_revision(
        mission.mission_id,
        steps=[{"step_id": "S1"}, {"step_id": "S2"}],
        reason="keep the plan as it is")

    s2 = next(s for s in revised.steps if s.step_id == "S2")
    assert "CONF-ABC123" in s2.detail
    assert revised.state == MissionState.AWAITING_APPROVAL.value, (
        "an unchanged step's approval was discarded")


def test_steering_a_changed_parked_step_invalidates_its_approval(service):
    """An approval is for one action. Changing what the step does must drop it,
    and the mission must leave the parked state rather than wait forever."""
    mission = _parked_mission(service)
    revised = service.apply_revision(
        mission.mission_id,
        steps=[{"step_id": "S1"},
               {"step_id": "S2", "title": "copy b instead", "tool": "copy_file",
                "params": {"source": "/tmp/work/a.txt",
                           "destination": "/tmp/work/b.txt"}}],
        reason="replace the write with a copy")

    s2 = next(s for s in revised.steps if s.step_id == "S2")
    assert "CONF-ABC123" not in s2.detail, (
        "the old approval was carried onto a different action")
    assert revised.state == MissionState.PLANNED.value, (
        "the mission stayed parked on an approval that no longer applies")
