"""The approval lifecycle, end to end through the real routes.

The campaign claim is narrow: **approve leads to the permitted action, deny
prevents execution.** This file proves exactly that, and nothing wider, for a
real mission record created through ``POST /missions`` and driven through
``POST /missions/{id}/run`` and ``POST /missions/{id}/steps/{step_id}/decide``.

The only stand-in is a test-only tool registered in the real registry with a
counting handler. That is deliberate: the mission's executor resolves a step
through ``find_agent(core.agents, tool)`` and the agent dispatches the
registry's handler, so the counter is reached only after the gate, the agent
and the Control Tower have all allowed the call -- it is an observation of a
dispatch, not of a status field. The handler performs no desktop input, no
file I/O, and needs no Accessibility permission.

What this file can and cannot establish, stated so a later reader does not read
more into a green run:

* It **does** establish that a CONFIRM parks the mission before the handler is
  reached, that the confirmation id recorded on the parked step is what
  resumes it (once), that a denial never reaches the handler, and that a
  fabricated id authorizes nothing.
* It **does not** establish that a *person* approved anything. Authentication
  on this backend is one shared token (or a session standing in for it), and
  possession of the confirmation id is the whole check once a caller is through
  the door -- `tests/test_mission_approval_refusals.py` pins that as a fact.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import TEST_TOKEN

HEADERS = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}

#: A test-only capability. Not a desktop, file or system tool: its handler
#: counts the dispatch and returns OK, so "did the action run?" is a number.
PROBE_TOOL = "v26_lifecycle_probe"


# -- harness ----------------------------------------------------------------

def _client():
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


@pytest.fixture()
def probe():
    """Register the counting tool in the real registry, then restore it.

    Registered exactly as `tests/test_phase30_capabilities_are_reachable.py`
    and `tests/test_control_tower.py` do it: a `ToolSpec` on the process-wide
    registry with a handler beside it. `ToolRegistry` has no removal method, so
    the two private maps are popped afterwards -- the registry is process-wide
    and a leaked spec would change every later test's permission surface.
    """
    from jarvis.api.app import get_core
    from jarvis.core.models import RiskLevel, ToolSpec
    from jarvis.tools.base import ActionResult

    core = get_core()
    dispatches: list = []

    spec = ToolSpec(
        name=PROBE_TOOL,
        description="test-only probe; performs nothing",
        agent="laptop",
        risk=RiskLevel.EXTERNAL,
        requires_confirmation=True,
        reversible=True,
        parameters={"path": "string"},
        required_parameters=("path",),
    )

    async def handler(ctx, **params):
        dispatches.append(dict(params))
        return ActionResult(ok=True, tool=PROBE_TOOL, message="the probe ran")

    core.tools.register(spec, handler)
    try:
        yield dispatches
    finally:
        core.tools._specs.pop(PROBE_TOOL, None)
        core.tools._handlers.pop(PROBE_TOOL, None)


@pytest.fixture()
def client(probe):  # noqa: ARG001 - the registration must precede the request
    return _client()


# -- helpers ----------------------------------------------------------------

def _create(client, target) -> str:
    """Open a real mission whose single step is a consequential tool call."""
    created = client.post("/missions", headers=HEADERS, json={
        "request": "run the probe that needs approval",
        "steps": [{
            "step_id": "S1",
            "title": "run the probe",
            "tool": PROBE_TOOL,
            "params": {"path": str(target)},
        }],
    })
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["state"] == "draft", body
    return body["mission_id"]


def _run(client, mission_id: str, approval=None):
    body = {} if approval is None else {"approval": approval}
    return client.post(f"/missions/{mission_id}/run", headers=HEADERS, json=body)


def _state(mission_id: str) -> dict:
    """The mission as it is stored, not as an earlier response remembered it."""
    response = _client().get(f"/missions/{mission_id}", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _step(mission: dict, step_id: str = "S1") -> dict:
    return next(s for s in mission.get("steps") or [] if s["step_id"] == step_id)


def _confirmation(mission: dict, step_id: str = "S1") -> str:
    step = _step(mission, step_id)
    match = re.search(r"confirmation (\S+)", (step or {}).get("detail") or "")
    return match.group(1).rstrip(")") if match else ""


def _decide(client, mission_id: str, decision: str, **extra):
    return client.post(f"/missions/{mission_id}/steps/S1/decide",
                       headers=HEADERS, json={"decision": decision, **extra})


# == 1. a consequential step parks before it executes =======================

def test_a_consequential_step_parks_before_it_executes(client, probe, tmp_path):
    """CONFIRM is a question, not a permission: the handler must not be reached.

    The probe is the assertion. A state field can be rewritten by a later
    request; a handler dispatch cannot be un-made.
    """
    from jarvis.api.app import get_core

    mission_id = _create(client, tmp_path / "parked.txt")
    run = _run(client, mission_id)

    assert run.status_code == 200, run.text
    body = run.json()
    assert body["state"] == "awaiting_approval", body.get("reason")
    assert probe == [], f"the step ran before anyone answered: {probe}"
    assert _step(body)["status"] == "pending"

    issued = _confirmation(body)
    assert issued.startswith("CONF-"), issued

    # The id is a real row minted by the real permission engine, on the mission
    # surface -- not a string the route made up.
    row = get_core().policy.get_confirmation(issued)
    assert row is not None and row["state"] == "pending", row
    assert row["requested_surface"] == "mission"
    assert row["tool"] == PROBE_TOOL

    # A second /run without an answer is still not consent.
    _run(client, mission_id)
    assert probe == [], "a repeated run acted without an approval"
    assert _state(mission_id)["state"] == "awaiting_approval"


# == 2. approving then running executes it exactly once =====================

def test_approving_then_running_executes_exactly_once(client, probe, tmp_path):
    """The positive half without which every refusal test proves nothing."""
    target = tmp_path / "approved.txt"
    mission_id = _create(client, target)
    parked = _run(client, mission_id).json()
    assert parked["state"] == "awaiting_approval"
    issued = _confirmation(parked)
    assert issued

    approved = _decide(client, mission_id, "approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "awaiting_approval", (
        "recording the decision is not executing it")
    assert "approved by the operator" in (_step(approved.json())["detail"] or "")
    assert issued in _step(approved.json())["detail"], (
        "the confirmation the person answered was dropped from the record")
    assert probe == [], "the approval alone executed the step"

    ran = _run(client, mission_id, {"confirmation_id": issued})
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert body["state"] in ("partially_completed", "completed"), (
        f"an approved step did not run: {body.get('reason')}")
    assert len(probe) == 1, f"the step executed {len(probe)} times: {probe}"
    assert probe[0]["path"] == str(target)
    assert _step(body)["status"] == "done"

    # One approval buys one execution: a repeated /run must not re-dispatch.
    _run(client, mission_id, {"confirmation_id": issued})
    assert len(probe) == 1, f"a resumed run repeated the side effect: {probe}"


# == 3. denying means the action never executes =============================

def test_denying_prevents_execution_and_the_mission_reflects_it(client, probe,
                                                               tmp_path):
    """The campaign's second half, from the decision to the stored record."""
    mission_id = _create(client, tmp_path / "denied.txt")
    parked = _run(client, mission_id).json()
    assert parked["state"] == "awaiting_approval"

    denied = _decide(client, mission_id, "deny")
    assert denied.status_code == 200, denied.text
    body = denied.json()

    assert body["state"] == "blocked", (
        f"a denied mission was left {body['state']}, not blocked")
    step = _step(body)
    assert step["status"] == "failed", step
    assert step["detail"] == "denied by the operator", step["detail"]
    assert probe == [], f"a denied step executed: {probe}"

    # And the refusal survives a run attempt through the real route.
    refused = _run(client, mission_id)
    assert probe == [], "the denied step executed on a later /run"
    assert refused.status_code in (404, 409), (
        f"a run of a denied mission was accepted ({refused.status_code}); "
        f"body: {refused.text}")

    stored = _state(mission_id)
    assert stored["state"] == "blocked"
    assert _step(stored)["status"] == "failed"
    assert "denied by the operator" in (_step(stored)["detail"] or ""), (
        f"the denial record was rewritten: {_step(stored)['detail']!r}")


# == 4. a forged confirmation id authorizes nothing =========================

def test_a_forged_confirmation_id_is_refused(client, probe, tmp_path):
    """A made-up id must not be consent, and must not rotate the real one."""
    mission_id = _create(client, tmp_path / "forged.txt")
    parked = _run(client, mission_id).json()
    issued = _confirmation(parked)
    assert issued

    for forged in ("deadbeef", "CONF-" + "0" * 32, "", "   "):
        refused = _run(client, mission_id, {"confirmation_id": forged})
        assert refused.status_code == 200, refused.text
        body = refused.json()
        assert body["state"] == "awaiting_approval", (
            f"forged id {forged!r} moved the mission to {body['state']}")
        assert _confirmation(body) == issued, (
            f"forged id {forged!r} replaced the confirmation the operator "
            f"was given: {_confirmation(body)!r}")
        assert probe == [], f"forged id {forged!r} authorized an execution"

    # The real id still works after the forged attempts -- proving the refusal
    # is discriminating rather than a route that refuses everything.
    resumed = _run(client, mission_id, {"confirmation_id": issued}).json()
    assert resumed["state"] in ("partially_completed", "completed"), (
        f"the operator's own approval stopped matching: {resumed.get('reason')}")
    assert len(probe) == 1


# == 5. a denied mission cannot be resumed into execution ===================

def test_a_denied_mission_cannot_be_resumed_into_execution(client, probe,
                                                           tmp_path):
    """No run turns a denial into an execution while it stands.

    The id that was shown before the denial is spent by it: a later /run
    carrying that id must not reach the handler, including after the operator
    moves the mission back to a state the transition table allows. If the
    runner re-parks, it mints a fresh id -- a new question for a new answer,
    never the denied one.

    NOTE what this does *not* claim. The denial is not durable against a legal
    recovery: BLOCKED -> RUNNING rewrites the denied step to "pending" and a
    later /run re-parks it with a NEW confirmation id, which can then be
    approved. That is a real defect, pinned separately in
    `test_a_recovery_does_not_erase_a_denial` at the bottom of this file.
    This test is the narrower property that holds today: the *denied id*
    cannot be the one that resumes.
    """
    mission_id = _create(client, tmp_path / "no_resume.txt")
    parked = _run(client, mission_id).json()
    issued = _confirmation(parked)
    assert issued

    denied = _decide(client, mission_id, "deny")
    assert denied.status_code == 200, denied.text
    assert denied.json()["state"] == "blocked"
    assert probe == []

    # 1. Run again while blocked.
    blocked_run = _run(client, mission_id)
    assert probe == [], "a blocked mission executed on /run"
    assert blocked_run.status_code in (404, 409), blocked_run.text

    # 2. Re-present the id that was answered "deny" -- the exact credential a
    #    stale client would still be holding.
    _run(client, mission_id, {"confirmation_id": issued})
    assert probe == [], "the denied confirmation id authorized an execution"

    # 3. Recover the mission through the legal BLOCKED edges and try again.
    #    BLOCKED -> PLANNED and BLOCKED -> RUNNING are deliberately legal
    #    (a precondition may become true later); the denial must still hold.
    for target in ("running", "planned"):
        moved = client.post(f"/missions/{mission_id}/transition", headers=HEADERS,
                            json={"state": target,
                                  "reason": "operator recovery attempt in test"})
        if moved.status_code != 200:
            continue  # the edge is not offered from the current state
        _run(client, mission_id, {"confirmation_id": issued})
        assert probe == [], (
            f"the denied confirmation authorized execution after recovery "
            f"to {target!r}")

    stored = _state(mission_id)
    assert stored["state"] not in ("completed", "partially_completed"), (
        f"a denied mission was reported as finished: {stored['state']}")
    assert _step(stored)["status"] != "done", (
        "a denied step was marked done")


# == 6. defect found by this file: a recovery erases a denial ==============
#
# Measured on this tree, 2026-11-19, through the routes only:
#   park -> decide deny (blocked, step "denied by the operator") ->
#   transition BLOCKED -> RUNNING (legal by design) -> /run ->
#   step becomes "pending" with a fresh confirmation id -> decide approve ->
#   /run with the fresh id -> handler dispatched, mission partially_completed.
#
# BLOCKED -> RUNNING is deliberately legal (a precondition may become true
# later), so the defect is not the transition. It is that the run which
# re-parks a previously-denied step rewrites "denied by the operator" into a
# fresh "awaiting approval" and hands out a working id -- the same
# rewrite-on-a-refused-request shape `tests/test_mission_approval_refusals.py`
# pinned for the AWAITING_APPROVAL case, not closed for the BLOCKED one.
# The denial survives the transition and is destroyed only by the run.

def test_a_recovery_does_not_erase_a_denial(client, probe, tmp_path):
    """Recovering a blocked mission must not turn "no" into a fresh question.

    The person answered; the durable record of that answer is the one artefact
    a denial leaves behind. A later, unrelated request must not replace it --
    and must certainly not hand out a working id for the action that was
    refused.
    """
    mission_id = _create(client, tmp_path / "recovered.txt")
    parked = _run(client, mission_id).json()
    assert parked["state"] == "awaiting_approval"
    denied_id = _confirmation(parked)
    assert denied_id

    denied = _decide(client, mission_id, "deny")
    assert denied.json()["state"] == "blocked"
    assert _step(denied.json())["detail"] == "denied by the operator"

    recovered = client.post(f"/missions/{mission_id}/transition", headers=HEADERS,
                            json={"state": "running", "reason": "precondition fixed"})
    assert recovered.status_code == 200, recovered.text
    assert _step(recovered.json())["detail"] == "denied by the operator", (
        "the transition alone rewrote the denial")

    after_run = _run(client, mission_id).json()

    assert _step(after_run)["status"] == "failed", (
        f"a run after recovery turned the denied step back into "
        f"{_step(after_run)['status']!r}")
    assert "denied by the operator" in (_step(after_run)["detail"] or ""), (
        f"the denial was erased by a recovery: {_step(after_run)['detail']!r}")
    fresh = _confirmation(after_run)
    assert not fresh or fresh == denied_id, (
        f"a recovery issued a new, working confirmation ({fresh!r}) for an "
        "action the operator denied")
    assert probe == [], "the denied action was re-armed and executed"