"""Consistency audit: do a mission's state, steps, conditions and verdict agree?

This file does not test features; it pins *invariants* -- statements that must
hold across every observable combination of ``state``, per-step ``status``,
per-condition ``status`` and the computed :meth:`Mission.verdict`.

Two real violations were found by this audit and have since been FIXED, so the
assertions below are live guarantees rather than reproductions:

* ``run()`` on a terminal mission executed its steps (fixed: a terminal-state
  guard returns the mission untouched);
* ``run_parallel()`` settled ``completed``/``verified`` with a step still
  ``pending`` when a dependency could never be satisfied (fixed: an empty
  ready-set with unfinished steps settles FAILED, naming the stranded step).

The semantics below were read from the source and confirmed by running the real
``MissionService`` + ``MissionRunner``, not asserted from memory.

The campaign invariant under audit:

    A mission must NEVER report ``completed`` together with ``verified`` while a
    required step is still ``pending`` or a required condition ``failed``.

Real semantics, read from the source and confirmed by running the real
``MissionService`` + ``MissionRunner`` (not asserted from memory):

* ``Mission.verdict()`` (``jarvis/core/missions.py:226-242``) is computed from
  *conditions only*; it never looks at steps.
* ``MissionState.COMPLETED`` is gated on conditions being verified
  (``jarvis/core/missions.py:381-392``); it does **not** check step statuses.
* A failed *step* settles the mission ``FAILED`` (``mission_runner.py:493-496``).
  A run where all steps ran but some condition did not verify settles
  ``PARTIALLY_COMPLETED`` (``mission_runner.py:510-513``).
* The sequential ``run()`` loop skips only ``done``/``skipped`` steps
  (``mission_runner.py:335-336``), so it executes every pending step before
  verification. The parallel ``run_parallel()`` loop, when ``ready_steps``
  returns nothing, now checks for unfinished steps first: if any remain the
  mission settles FAILED (a dependency was missing or the graph has a cycle);
  only when every step is finished does it proceed to verify.
"""

from __future__ import annotations

import pytest

from jarvis.core.mission_runner import MissionRunner, Verification, Verifiers
from jarvis.core.missions import ConditionStatus, MissionState


@pytest.fixture()
def service():
    from jarvis.core.missions import MissionService
    from jarvis.db.database import get_db

    return MissionService(get_db())


def _allow_all(step, mission):
    return {"allowed": True, "reason": "test gate"}


class _Ok:
    ok = True
    message = "did the work"


class _Bad:
    ok = False
    message = "the tool refused"


def _mission(service, *, conditions=None, steps):
    return service.create("consistency audit", conditions=conditions, steps=steps)


def _shape(mission):
    """The four views that must agree, in one comparable dict."""
    return {
        "state": mission.state,
        "verdict": mission.verdict(),
        "steps": {s.step_id: s.status for s in mission.steps},
        "conditions": {c.condition_id: c.status for c in mission.conditions},
    }


def _assert_no_false_success(mission):
    """The campaign invariant, as one reusable assertion.

    ``completed`` + ``verified`` may not coexist with a ``pending`` step or a
    ``failed`` condition. This is asserted in every case; the tests that
    exercise a path where the API can *violate* it are marked ``xfail`` so the
    assertion is the failure, not a weakened expectation.
    """
    shape = _shape(mission)
    if shape["state"] == MissionState.COMPLETED.value and shape["verdict"] == "verified":
        assert "pending" not in shape["steps"].values(), (
            f"completed+verified with a pending step: {shape}")
        assert ConditionStatus.FAILED.value not in shape["conditions"].values(), (
            f"completed+verified with a failed condition: {shape}")


# -- 1. the honest happy path ----------------------------------------------

@pytest.mark.asyncio
async def test_all_steps_succeed_and_all_conditions_verify(service, tmp_path):
    """Every step done, every condition verified -> completed / verified."""
    target = tmp_path / "out.txt"

    def write(step, mission):
        target.write_text("--done\n", encoding="utf-8")
        return _Ok()

    mission = _mission(
        service,
        conditions=[{"statement": "the file says --done", "verifier": "file content"},
                    {"statement": "the file exists", "verifier": "file content"}],
        steps=[{"title": "write", "tool": "write_file"},
               {"title": "write again", "tool": "write_file"}],
    )
    runner = MissionRunner(service, execute_step=write, permission=_allow_all)
    done = await runner.run(mission.mission_id, verifiers={
        "C1": Verifiers.file_contains(str(target), "--done", "C1"),
        "C2": Verifiers.file_absent(str(tmp_path / "nope"), "C2"),
    })

    assert _shape(done) == {
        "state": "completed",
        "verdict": "verified",
        "steps": {"S1": "done", "S2": "done"},
        "conditions": {"C1": "verified", "C2": "verified"},
    }
    _assert_no_false_success(done)


# -- 2. one step's executor fails ------------------------------------------

@pytest.mark.asyncio
async def test_a_failed_step_never_completes_or_verifies(service):
    """Observed: state ``failed``, verdict ``unknown``.

    The first step is ``done``, the second ``failed``, and the acceptance
    condition is never evaluated (it stays ``pending``) because a failed step
    short-circuits the run before verification (``mission_runner.py:493-496``).
    The verdict is ``unknown`` -- not ``failed`` -- because no condition was
    ever judged; and crucially it is not ``verified``.
    """
    def second_fails(step, mission):
        return _Ok() if step.step_id == "S1" else _Bad()

    mission = _mission(
        service,
        conditions=[{"statement": "the work happened", "verifier": "file content"}],
        steps=[{"title": "first", "tool": "read_file"},
               {"title": "second", "tool": "write_file"}],
    )
    runner = MissionRunner(service, execute_step=second_fails, permission=_allow_all)
    result = await runner.run(mission.mission_id)

    assert result.state != MissionState.COMPLETED.value
    assert result.verdict() != "verified"
    assert _shape(result) == {
        "state": "failed",
        "verdict": "unknown",
        "steps": {"S1": "done", "S2": "failed"},
        "conditions": {"C1": "pending"},
    }
    _assert_no_false_success(result)


# -- 3. a condition verifier says FAILED -----------------------------------

@pytest.mark.asyncio
async def test_a_contradicted_condition_yields_verdict_failed(service):
    """Observed: step ``done``, condition ``failed`` -> verdict ``failed``,
    state ``partially_completed``.

    This is coherent and is the intended mapping: the *step* ran (so the mission
    is not ``failed``), but the independent observer contradicted the claimed
    outcome, so the mission closes as ``partially_completed`` carrying the
    verdict ``failed``. ``verdict()`` ranks a single failed condition above any
    amount of verified ones (``missions.py:238-239``).
    """
    mission = _mission(
        service,
        conditions=[{"statement": "the flag is present", "verifier": "file content"}],
        steps=[{"title": "write", "tool": "write_file"}],
    )
    runner = MissionRunner(service, execute_step=lambda s, m: _Ok(),
                           permission=_allow_all)
    result = await runner.run(
        mission.mission_id,
        verifiers={"C1": lambda m: Verification(
            "C1", ConditionStatus.FAILED, "the flag is absent")})

    assert _shape(result) == {
        "state": "partially_completed",
        "verdict": "failed",
        "steps": {"S1": "done"},
        "conditions": {"C1": "failed"},
    }
    _assert_no_false_success(result)


# -- 4. a condition verifier cannot observe --------------------------------

@pytest.mark.asyncio
async def test_an_unknown_condition_is_never_verified(service):
    """Observed: condition ``unknown`` -> verdict ``unknown``, not verified."""
    mission = _mission(
        service,
        conditions=[{"statement": "something we cannot see", "verifier": "?"}],
        steps=[{"title": "do it", "tool": "read_file"}],
    )
    runner = MissionRunner(service, execute_step=lambda s, m: _Ok(),
                           permission=_allow_all)
    result = await runner.run(
        mission.mission_id,
        verifiers={"C1": Verifiers.always_unknown("nobody can observe it", "C1")})

    assert result.verdict() != "verified"
    assert _shape(result) == {
        "state": "partially_completed",
        "verdict": "unknown",
        "steps": {"S1": "done"},
        "conditions": {"C1": "unknown"},
    }
    _assert_no_false_success(result)


# -- 5. a step with no condition at all (unobserved) -----------------------

@pytest.mark.asyncio
async def test_a_mission_with_no_condition_is_never_verified(service):
    """Observed: no conditions -> verdict ``unknown``, state
    ``partially_completed``.

    A step that ran with nothing to check it cannot be called verified: with no
    conditions, ``verdict()`` returns ``unknown`` (``missions.py:235-236``) and
    ``transition`` refuses ``COMPLETED`` outright (``missions.py:382-385``), so
    the run settles ``partially_completed``.
    """
    mission = _mission(service, conditions=[], steps=[{"title": "do", "tool": "read_file"}])
    runner = MissionRunner(service, execute_step=lambda s, m: _Ok(),
                           permission=_allow_all)
    result = await runner.run(mission.mission_id)

    assert result.verdict() != "verified"
    assert _shape(result) == {
        "state": "partially_completed",
        "verdict": "unknown",
        "steps": {"S1": "done"},
        "conditions": {},
    }
    _assert_no_false_success(result)


# -- 6a. the core invariant holds on the sequential runner -----------------

@pytest.mark.asyncio
async def test_core_invariant_holds_on_the_sequential_runner(service, tmp_path):
    """Impossibility check: the sequential ``run()`` never leaves a pending step
    behind while reporting completed+verified.

    What prevents it: the loop at ``mission_runner.py:335-336`` skips only
    ``done``/``skipped`` steps, so every pending step is executed (reaching
    ``done`` or ``failed``) before VERIFYING is entered; and a failed step
    returns ``FAILED`` at ``mission_runner.py:493-496`` instead of falling
    through. ``COMPLETED`` additionally requires every condition verified
    (``missions.py:381-392``). Across the reachable outcomes the invariant
    therefore holds -- asserted over a small matrix of configurations.
    """
    target = tmp_path / "x.txt"

    async def one(verifiers, *, fail_step=None, n_conditions=1):
        def execute(step, mission):
            if step.step_id == fail_step:
                return _Bad()
            target.write_text("ok\n", encoding="utf-8")
            return _Ok()

        conditions = [{"statement": f"cond {i}", "verifier": "file content"}
                      for i in range(1, n_conditions + 1)]
        m = _mission(service, conditions=conditions,
                     steps=[{"title": "a", "tool": "read_file"},
                            {"title": "b", "tool": "write_file"}])
        r = await MissionRunner(service, execute_step=execute,
                                permission=_allow_all).run(
            m.mission_id, verifiers=verifiers)
        _assert_no_false_success(r)
        return r

    # all verified -> completed/verified, no pending step
    ok = await one({"C1": Verifiers.file_absent(str(tmp_path / "gone"), "C1")})
    assert ok.state == MissionState.COMPLETED.value
    assert "pending" not in _shape(ok)["steps"].values()

    # first step fails -> failed, still no completed+verified
    await one({}, fail_step="S1")
    # second step fails -> failed
    await one({}, fail_step="S2")
    # a verifier cannot observe -> partial, not completed
    unknown = await one({"C1": Verifiers.always_unknown("blind", "C1")})
    assert unknown.state != MissionState.COMPLETED.value


# -- 6b. the core invariant is VIOLATED by run_parallel ---------------------
#
# This is the finding. ``run_parallel`` selects steps with
# ``scheduler.ready_steps`` and stops the wave loop when nothing is eligible
# (``mission_runner.py:580-582``), then proceeds to verify and settle. It never
# checks that every step finished. A step whose ``depends_on`` names a step that
# does not exist is never eligible (``scheduler.py:ready_steps`` treats a missing
# dependency as unfinished forever), so it stays ``pending`` -- yet the mission
# is reported ``completed``/``verified`` because the only gate on COMPLETED is
# the *condition* check (``missions.py:381-392``). A dependency cycle does the
# same. Reachable through the real public runner method; note the HTTP /run
# route uses ``run()`` (sequential), so this is not currently wired to a route.

@pytest.mark.asyncio
async def test_core_invariant_is_violated_by_parallel_runner(service):
    """REPRODUCTION OF A DEFECT -- expected to fail until run_parallel is fixed.

    A single step depends on a step id that does not exist, so it is never
    eligible and stays ``pending``. The condition verifies (nothing to observe),
    and the mission is reported ``completed`` with verdict ``verified`` while
    its only step never ran.
    """
    mission = _mission(
        service,
        conditions=[{"statement": "the outcome holds", "verifier": "file absent"}],
        steps=[{"step_id": "S1", "title": "never eligible", "tool": "read_file",
                "depends_on": ["S-does-not-exist"]}],
    )
    runner = MissionRunner(service, execute_step=lambda s, m: _Ok(),
                           permission=_allow_all)
    result = await runner.run_parallel(
        mission.mission_id,
        verifiers={"C1": Verifiers.file_absent("/nonexistent-consistency", "C1")})

    # What the invariant demands -- this is what fails:
    _assert_no_false_success(result)
    assert "pending" in _shape(result)["steps"].values(), (
        "probe was meant to leave a pending step")


# -- 7. cancellation must not let work be recorded afterwards --------------
#
# A finding, not a pass: cancelling a mission (DRAFT -> CANCELLED is legal) does
# not stop a later ``run()`` from executing its steps. ``run()`` has no guard for
# a terminal starting state (``mission_runner.py:305-334`` handles only DRAFT,
# AWAITING_APPROVAL and PLANNED/AWAITING_APPROVAL), so it enters the step loop,
# executes S1, marks it ``done``, and only then raises when it tries
# ``CANCELLED -> VERIFYING`` (illegal per ``TRANSITIONS``). The mission stays
# ``cancelled`` (the illegal transition does not apply) but the side effect has
# happened and the step was rewritten to ``done`` after cancellation. The HTTP
# /run route maps the raised MissionTransitionError (a ValueError) to a 404, so
# the caller is told "not found" after the write already ran.

@pytest.mark.asyncio
async def test_cancelled_mission_does_not_run_its_steps(service):
    """A terminal mission does not run again.

    Measured before the guard: cancelling then running dispatched the step's
    handler (a real side effect) and recorded it ``done``, and only *then*
    raised "cancelled -> verifying is not a legal transition" -- the error came
    after the damage. `run()` now returns a terminal mission untouched, which is
    also why this asserts no exception: refusing by returning leaves the caller
    able to read the record, where a raise became a 404 with no body.

    This was an ``xfail(strict=True)`` documenting the defect; it is a real
    assertion now that the guard exists.
    """
    executed = []

    def should_not_run(step, mission):
        executed.append(step.step_id)
        return _Ok()

    mission = _mission(
        service,
        conditions=[{"statement": "x", "verifier": "y"}],
        steps=[{"title": "write", "tool": "write_file"}],
    )
    service.transition(mission.mission_id, MissionState.CANCELLED,
                       actor="user", reason="cancelled by the operator")

    returned = await MissionRunner(service, execute_step=should_not_run,
                                   permission=_allow_all).run(mission.mission_id)
    assert returned.state == MissionState.CANCELLED.value

    after = service.get(mission.mission_id)
    assert after.state == MissionState.CANCELLED.value
    assert executed == [], "a cancelled mission executed a step"
    assert _shape(after)["steps"]["S1"] == "pending", (
        "a step was rewritten to done after the mission was cancelled")


# -- 8. one step done, one step failed -> what state? ----------------------

@pytest.mark.asyncio
async def test_partial_step_outcome_maps_to_failed_not_partially_completed(service):
    """Observed mapping: a failed *step* settles the mission ``FAILED``, verdict
    ``unknown`` -- NOT ``partially_completed``.

    ``partially_completed`` is reserved for the case where every step ran but
    not every condition verified (``mission_runner.py:510-513``); a step that
    failed short-circuits to ``FAILED`` first (``mission_runner.py:493-496``).
    The verdict is ``unknown`` because the two conditions were never evaluated
    (the run stopped at the failed step). Verified only in part, never
    ``verified`` overall.
    """
    def one_of_two_fails(step, mission):
        return _Ok() if step.step_id == "S1" else _Bad()

    mission = _mission(
        service,
        conditions=[{"statement": "first effect", "verifier": "v"},
                    {"statement": "second effect", "verifier": "v"}],
        steps=[{"title": "first", "tool": "read_file"},
               {"title": "second", "tool": "write_file"}],
    )
    runner = MissionRunner(service, execute_step=one_of_two_fails,
                           permission=_allow_all)
    result = await runner.run(mission.mission_id)

    assert _shape(result) == {
        "state": "failed",
        "verdict": "unknown",
        "steps": {"S1": "done", "S2": "failed"},
        "conditions": {"C1": "pending", "C2": "pending"},
    }
    assert result.verdict() != "verified"
    _assert_no_false_success(result)


# -- 9. a dependency cycle is also a stranded plan -------------------------

@pytest.mark.asyncio
async def test_a_dependency_cycle_cannot_settle_as_success(service):
    """Two steps that each depend on the other are both permanently ineligible.

    The same guard as the missing-dependency case: an empty ready-set with
    unfinished steps is a planning failure, and it must not fall through to
    verification and settle on a condition that happens to hold.
    """
    mission = _mission(
        service,
        conditions=[{"statement": "the outcome holds", "verifier": "file absent"}],
        steps=[{"step_id": "S1", "title": "a", "tool": "read_file", "depends_on": ["S2"]},
               {"step_id": "S2", "title": "b", "tool": "read_file", "depends_on": ["S1"]}],
    )
    ran = []

    async def execute(step, m):
        ran.append(step.step_id)
        return _Ok()

    result = await MissionRunner(service, execute_step=execute,
                                 permission=_allow_all).run_parallel(
        mission.mission_id,
        verifiers={"C1": Verifiers.file_absent("/nonexistent-cycle", "C1")})

    assert ran == [], "a step in a cycle was executed"
    _assert_no_false_success(result)
    assert result.state == MissionState.FAILED.value
    assert _shape(result)["steps"] == {"S1": "pending", "S2": "pending"}
