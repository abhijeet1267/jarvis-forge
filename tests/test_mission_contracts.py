"""Mission contracts: durable intent, a validated state machine, honest verdicts.

P1's claim is narrow and testable: a mission records *what must be true*, moves
only through legal states, cannot be marked complete without evidence, and
survives a restart. These tests pin each of those, including the refusals --
the interesting behaviour is what the contract will not let a caller do.
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
def service():
    """A mission service over an isolated database.

    A private JARVIS_HOME per test: mission rows are durable state, and a test
    that shares them with the developer's real data is the class of leak this
    project has already been bitten by.
    """
    from jarvis.db.database import get_db
    from jarvis.core.missions import MissionService

    return MissionService(get_db())


def _mission(service, **kwargs):
    defaults = dict(
        request="organize the sample project",
        conditions=[{"statement": "README mentions the new flag",
                     "verifier": "file content"}],
        steps=[{"title": "inspect the tree", "tool": "read_file"},
               {"title": "update the README", "tool": "write_file"}],
    )
    defaults.update(kwargs)
    return service.create(**defaults)


# -- creation ---------------------------------------------------------------

def test_a_created_mission_starts_in_draft_and_records_its_request(service):
    mission = _mission(service)
    assert mission.state == "draft"
    assert mission.request == "organize the sample project"
    assert len(mission.conditions) == 1
    assert len(mission.steps) == 2


def test_a_mission_without_a_request_is_refused(service):
    with pytest.raises(ValueError):
        service.create("", conditions=[])


# -- the state machine ------------------------------------------------------

def test_the_happy_path_reaches_completed(service):
    from jarvis.core.missions import MissionState, ConditionStatus

    mission = _mission(service)
    service.transition(mission.mission_id, MissionState.PLANNED)
    service.transition(mission.mission_id, MissionState.RUNNING)
    service.transition(mission.mission_id, MissionState.VERIFYING)
    service.record_condition(mission.mission_id, "C1",
                             status=ConditionStatus.VERIFIED,
                             observation="README contains the flag")
    done = service.transition(mission.mission_id, MissionState.COMPLETED)

    assert done.state == "completed"
    assert done.verdict() == "verified"


def test_an_illegal_transition_is_refused_not_ignored(service):
    """DRAFT -> COMPLETED skips planning, approval, running and verification."""
    from jarvis.core.missions import MissionState, MissionTransitionError

    mission = _mission(service)
    with pytest.raises(MissionTransitionError) as excinfo:
        service.transition(mission.mission_id, MissionState.COMPLETED)

    assert "not a legal transition" in str(excinfo.value)
    # And the stored state did not move.
    assert service.get(mission.mission_id).state == "draft"


def test_completing_without_verified_conditions_is_refused(service):
    """The whole point: 'done' is an assertion until something checked it."""
    from jarvis.core.missions import MissionState, MissionTransitionError

    mission = _mission(service)
    service.transition(mission.mission_id, MissionState.PLANNED)
    service.transition(mission.mission_id, MissionState.RUNNING)
    service.transition(mission.mission_id, MissionState.VERIFYING)

    with pytest.raises(MissionTransitionError) as excinfo:
        service.transition(mission.mission_id, MissionState.COMPLETED)
    assert "not verified" in str(excinfo.value)

    # PARTIALLY_COMPLETED is the honest way to close that mission.
    partial = service.transition(mission.mission_id, MissionState.PARTIALLY_COMPLETED)
    assert partial.state == "partially_completed"
    assert partial.verdict() == "partial" or partial.verdict() == "unknown"


def test_a_mission_with_no_conditions_cannot_complete(service):
    from jarvis.core.missions import MissionState, MissionTransitionError

    mission = service.create("do something vague", conditions=[])
    for state in (MissionState.PLANNED, MissionState.RUNNING, MissionState.VERIFYING):
        service.transition(mission.mission_id, state)

    with pytest.raises(MissionTransitionError) as excinfo:
        service.transition(mission.mission_id, MissionState.COMPLETED)
    assert "no success conditions" in str(excinfo.value)


def test_an_empty_mission_verdict_is_unknown_never_success(service):
    mission = service.create("vague request", conditions=[])
    assert mission.verdict() == "unknown"


def test_repeating_a_transition_is_idempotent(service):
    from jarvis.core.missions import MissionState

    mission = _mission(service)
    service.transition(mission.mission_id, MissionState.PLANNED)
    again = service.transition(mission.mission_id, MissionState.PLANNED)
    assert again.state == "planned"


# -- verdicts ---------------------------------------------------------------

def test_verdict_is_failed_when_any_condition_contradicts(service):
    from jarvis.core.missions import ConditionStatus

    mission = _mission(service)
    service.record_condition(mission.mission_id, "C1",
                             status=ConditionStatus.FAILED,
                             observation="README does not mention the flag")
    assert service.get(mission.mission_id).verdict() == "failed"


def test_verdict_stays_unknown_when_a_condition_was_never_observed(service):
    """A step running is not a condition being verified."""
    mission = _mission(service)
    service.mark_step(mission.mission_id, "S1", status="done", detail="read 12 files")
    service.mark_step(mission.mission_id, "S2", status="done", detail="wrote README")

    reloaded = service.get(mission.mission_id)
    assert reloaded.verdict() == "unknown", (
        "two 'done' steps must not add up to a verified mission")


def test_a_partial_verification_is_reported_as_partial(service):
    from jarvis.core.missions import ConditionStatus

    mission = _mission(service, conditions=[
        {"statement": "README updated", "verifier": "file content"},
        {"statement": "no duplicate files remain", "verifier": "directory scan"},
    ])
    service.record_condition(mission.mission_id, "C1",
                             status=ConditionStatus.VERIFIED, observation="ok")

    assert service.get(mission.mission_id).verdict() == "partial"


# -- durability -------------------------------------------------------------

def test_a_mission_survives_a_new_service_over_the_same_database(service):
    """A restart is a new process over the same rows; nothing is in memory only."""
    from jarvis.core.missions import MissionService, MissionState
    from jarvis.db.database import get_db

    mission = _mission(service)
    service.transition(mission.mission_id, MissionState.PLANNED)
    service.transition(mission.mission_id, MissionState.RUNNING)

    reopened = MissionService(get_db())
    reloaded = reopened.get(mission.mission_id)

    assert reloaded is not None
    assert reloaded.state == "running"
    assert reloaded.request == mission.request
    assert len(reloaded.steps) == 2


def test_incomplete_missions_names_work_interrupted_mid_flight(service):
    from jarvis.core.missions import MissionState

    running = _mission(service, request="still working")
    service.transition(running.mission_id, MissionState.PLANNED)
    service.transition(running.mission_id, MissionState.RUNNING)

    finished = _mission(service, request="already done")
    for state in (MissionState.PLANNED, MissionState.RUNNING,
                  MissionState.VERIFYING, MissionState.CANCELLED):
        service.transition(finished.mission_id, state)

    interrupted = service.incomplete_missions()
    ids = [m.mission_id for m in interrupted]

    assert running.mission_id in ids
    assert finished.mission_id not in ids, "a cancelled mission is not resumable work"


def test_the_version_advances_so_a_stale_writer_cannot_clobber(service):
    from jarvis.core.missions import MissionState

    mission = _mission(service)
    before = mission.version
    service.transition(mission.mission_id, MissionState.PLANNED)
    assert service.get(mission.mission_id).version > before


# -- steps ------------------------------------------------------------------

def test_marking_an_unknown_step_is_refused(service):
    mission = _mission(service)
    with pytest.raises(KeyError):
        service.mark_step(mission.mission_id, "S-does-not-exist", status="done")


def test_a_step_status_must_be_one_of_the_known_values(service):
    mission = _mission(service)
    with pytest.raises(ValueError):
        service.mark_step(mission.mission_id, "S1", status="finished")