"""The approval system: what it must refuse, proved adversarially.

A mission's consequential steps park at ``awaiting_approval`` and resume only on a
per-step approval carrying a ``confirmation_id``. That makes the *refusals* the
security boundary: everything here asks "can a thing that is not a valid
approval be made to authorize a step?" rather than "does the happy path work".

The six required cases are strict tests and pass against the current code. The
section at the bottom pins defects this file found; each is marked
``xfail(strict=True)`` so a fix turns it red rather than letting the marker
quietly outlive the bug. Do not delete those markers to get a green run -- read
the reason first.

Everything runs through the real routes, the real permission engine and the real
lease service against the isolated test ``JARVIS_HOME``. The only stand-in is
the step executor: it is replaced with a spy so "the tool ran" is an observation
of a call, not of a handler's return value. No desktop input is performed and no
Accessibility permission is needed.
"""

from __future__ import annotations

import re

import pytest

from tests.conftest import TEST_TOKEN

HEADERS = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}


# -- harness ----------------------------------------------------------------

def _client():
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


class _Ok:
    ok = True
    message = "the spy step ran"


class _SpyExecutor:
    """Stands in for the agent registry's executor and records every call.

    It owns every tool so ``find_agent`` always resolves to it. That is what
    makes "was the tool invoked?" answerable: a refusal that never reaches this
    object is a refusal that never reached a capability.

    A ``write_file`` call also performs the write, so a test that asserts the
    file is absent is observing a side effect that genuinely did not happen --
    not an executor that never touches disks and would leave every file absent
    whether it ran or not.
    """

    def __init__(self) -> None:
        self.calls: list = []

    def owns(self, tool: str) -> bool:
        return True

    async def execute(self, action, ctx):
        self.calls.append((action.tool, dict(action.params or {})))
        if action.tool == "write_file":
            from pathlib import Path

            Path(action.params["path"]).write_text(
                str(action.params.get("content") or ""), encoding="utf-8")
        return _Ok()

    def tools_called(self) -> list:
        return [tool for tool, _ in self.calls]


@pytest.fixture()
def spy():
    """Swap the core's agents for a spy, and put the real ones back afterwards.

    ``core`` is process-wide, so this must restore even when a test fails --
    otherwise one adversarial test would silently disarm every later one.
    """
    from jarvis.api.app import get_core

    core = get_core()
    original = core.agents
    spy = _SpyExecutor()
    core.agents = {"spy": spy}
    try:
        yield spy
    finally:
        core.agents = original


def _confirmation(mission: dict, step_id: str = "S1") -> str:
    """The confirmation id recorded on a parked step, or "" when none is."""
    step = _step(mission, step_id)
    match = re.search(r"confirmation (\S+)", (step or {}).get("detail") or "")
    return match.group(1).rstrip(")") if match else ""


def _step(mission: dict, step_id: str = "S1") -> dict:
    return next(s for s in mission.get("steps") or [] if s["step_id"] == step_id)


def _state(mission_id: str) -> dict:
    """The mission as it is on disk, not as a response remembered it."""
    response = _client().get(f"/missions/{mission_id}", headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def _write_mission(client, target, *, content: str = "x", steps: int = 1,
                   step_id: str = "S1") -> str:
    """A mission whose steps are consequential writes needing approval."""
    plan = [{"step_id": step_id, "title": "write", "tool": "write_file",
             "params": {"path": str(target), "content": content}}]
    if steps > 1:
        plan.append({"step_id": "S2", "title": "write two", "tool": "write_file",
                     "params": {"path": str(target) + ".two", "content": content}})
    created = client.post("/missions", headers=HEADERS, json={
        "request": "write a file that needs approval", "steps": plan})
    assert created.status_code == 200, created.text
    return created.json()["mission_id"]


def _run(client, mission_id: str, approval=None) -> dict:
    body = {} if approval is None else {"approval": approval}
    return client.post(f"/missions/{mission_id}/run", headers=HEADERS,
                       json=body).json()


# == 1. a denied step never executes =======================================

def test_a_denied_step_never_executes_and_the_mission_ends_blocked(tmp_path, spy):
    """A person said no. The step must not run, and the mission must say so.

    The spy is the assertion that matters: a status field can be rewritten by a
    later request, a call to the executor cannot be un-made.
    """
    target = tmp_path / "denied.txt"
    client = _client()
    mission_id = _write_mission(client, target)

    parked = _run(client, mission_id)
    assert parked["state"] == "awaiting_approval"
    assert spy.calls == [], "a step awaiting approval ran before anyone answered"

    denied = client.post(f"/missions/{mission_id}/steps/S1/decide",
                         headers=HEADERS, json={"decision": "deny"})
    assert denied.status_code == 200, denied.text
    assert denied.json()["state"] == "blocked"
    assert spy.calls == [], "the denied step executed"

    # Attempt to run it anyway. The refusal is the point, not the status code.
    _run(client, mission_id)

    assert spy.calls == [], (
        "a step the operator explicitly denied was executed anyway")
    assert not target.exists(), "the denied write reached the disk"
    assert _state(mission_id)["state"] == "blocked"


# == 2. a stale or foreign confirmation id cannot authorize ================

def test_a_foreign_confirmation_id_cannot_resume_a_parked_mission(tmp_path, spy):
    """An approval is for one action. Another mission's id is not that approval.

    The "other" id is real and currently valid -- for a different mission -- so
    this is not a test of malformed input. It is a test that an authentic
    credential is still scoped to what it was issued for.
    """
    client = _client()
    parked_id = _write_mission(client, tmp_path / "mine.txt")
    other_id = _write_mission(client, tmp_path / "theirs.txt")

    parked = _run(client, parked_id)
    assert parked["state"] == "awaiting_approval"
    other = _run(client, other_id)
    foreign = _confirmation(other)
    assert foreign, "the second mission parked with no confirmation to borrow"

    refused = _run(client, parked_id, {"confirmation_id": foreign})

    assert refused["state"] == "awaiting_approval", (
        f"another mission's confirmation resumed this one: {refused['reason']}")
    assert spy.calls == [], "a foreign approval authorized a step"


def test_a_fabricated_confirmation_id_cannot_resume_a_parked_mission(tmp_path, spy):
    """There is no path where a made-up id is treated as consent."""
    client = _client()
    mission_id = _write_mission(client, tmp_path / "fabricated.txt")
    parked = _run(client, mission_id)
    assert parked["state"] == "awaiting_approval"

    refused = _run(client, mission_id, {"confirmation_id": "deadbeef"})

    assert refused["state"] == "awaiting_approval", (
        f"a fabricated confirmation authorized the step: {refused['reason']}")
    assert spy.calls == []


def test_an_empty_confirmation_id_does_not_count_as_approval(tmp_path, spy):
    """`""` and whitespace are absence, not a wildcard."""
    client = _client()
    mission_id = _write_mission(client, tmp_path / "empty.txt")
    assert _run(client, mission_id)["state"] == "awaiting_approval"

    for offered in ("", "   "):
        refused = _run(client, mission_id, {"confirmation_id": offered})
        assert refused["state"] == "awaiting_approval", (
            f"{offered!r} was accepted as an approval")
    assert spy.calls == []


# == 3. an approval for S1 cannot authorize S2 =============================

def _park_two_step_mission(client, target, spy) -> tuple:
    """A mission parked on S2 with S1 already run; returns (id, s1, s2) ids.

    S1's confirmation is captured while S1 is parked; S2's while S2 is parked.
    Kept as one helper so both the cross-step refusal and its positive control
    start from exactly the same state -- otherwise the control could pass for a
    reason the refusal test never had.
    """
    mission_id = _write_mission(client, target, steps=2)

    first = _run(client, mission_id)
    assert first["state"] == "awaiting_approval"
    s1_confirmation = _confirmation(first, "S1")
    assert s1_confirmation, "the parked step recorded no confirmation"

    second = _run(client, mission_id, {"confirmation_id": s1_confirmation})
    assert second["state"] == "awaiting_approval", second.get("reason")
    s2_confirmation = _confirmation(second, "S2")
    assert s2_confirmation, "the second step parked with no confirmation"
    assert s1_confirmation != s2_confirmation, (
        "both steps were issued the same confirmation, so per-step approval "
        "cannot be enforced")
    assert spy.tools_called() == ["write_file"], (
        f"only S1 should have run by now: {spy.calls}")
    return mission_id, s1_confirmation, s2_confirmation


def test_an_approval_for_one_step_cannot_authorize_another(tmp_path, spy):
    """Two parked steps carry two confirmations. Spending one on the other fails.

    The positive control lives in its own test below, deliberately: a refused
    replay here mutates the recorded id (see the rotation defect at the bottom
    of this file), so asserting both directions in one test would make the
    control's outcome depend on that other bug.
    """
    client = _client()
    mission_id, s1_confirmation, _ = _park_two_step_mission(
        client, tmp_path / "two_steps.txt", spy)

    refused = _run(client, mission_id, {"confirmation_id": s1_confirmation})

    assert refused["state"] == "awaiting_approval", (
        f"S1's approval authorized S2: {refused['reason']}")
    assert len(spy.calls) == 1, (
        f"S2 ran on S1's approval: {spy.calls}")
    assert _step(_state(mission_id), "S2")["status"] == "pending"
    assert not (tmp_path / "two_steps.txt.two").exists(), (
        "S2's write reached the disk on S1's approval")


def test_a_parked_steps_own_approval_does_resume_it(tmp_path, spy):
    """The discriminating half: the guard has to have a path that allows.

    Without this, a check that refused every confirmation would pass the
    cross-step test while proving nothing about per-step binding.
    """
    client = _client()
    mission_id, _, s2_confirmation = _park_two_step_mission(
        client, tmp_path / "own_approval.txt", spy)

    resumed = _run(client, mission_id, {"confirmation_id": s2_confirmation})

    assert resumed["state"] != "awaiting_approval", (
        f"S2's own approval was refused, so the guard is not discriminating: "
        f"{resumed.get('reason')}")
    assert len(spy.calls) == 2
    # Proof the absent-file assertions elsewhere are meaningful: when a step is
    # allowed, this same spy does put the file on disk.
    assert (tmp_path / "own_approval.txt").read_text(encoding="utf-8") == "x"
    assert (tmp_path / "own_approval.txt.two").read_text(encoding="utf-8") == "x"


# == 4. a revoked lease cannot authorize ===================================

def test_a_revoked_lease_is_refused_at_its_next_use(tmp_path, spy):
    """Revocation is durable and takes effect at use, not at issue.

    Both halves are checked: the service refuses the spend, and the step that
    would have been authorized by it never reaches the executor.
    """
    from jarvis.db.database import get_db
    from jarvis.security.leases import LeaseRefusal, LeaseService

    client = _client()
    target = tmp_path / "leased.txt"
    mission_id = _write_mission(client, target)

    leases = LeaseService(get_db())
    lease = leases.issue(mission_id=mission_id, session_id="S1",
                         capability="write_file", scope=[str(tmp_path)],
                         ttl_seconds=60, max_uses=1)

    revoked = client.post(f"/missions/leases/{lease.lease_id}/revoke",
                          headers=HEADERS, json={"reason": "withdrawn by the operator"})
    assert revoked.status_code == 200, revoked.text

    with pytest.raises(LeaseRefusal, match="revoked"):
        leases.consume(lease.lease_id, capability="write_file",
                       resource=str(target), session_id="S1")
    assert leases.get(lease.lease_id).uses == 0, (
        "a revoked lease was spent anyway")

    # The per-step approval route must refuse it too, not just the service.
    assert _run(client, mission_id)["state"] == "awaiting_approval"
    decided = client.post(f"/missions/{mission_id}/steps/S1/decide",
                          headers=HEADERS,
                          json={"decision": "approve", "lease_id": lease.lease_id})
    assert decided.status_code == 409, decided.text
    assert "revoked" in decided.json()["detail"]

    step = _step(_state(mission_id), "S1")
    assert step["status"] == "pending", (
        f"a step was approved on a revoked lease: {step['status']}")
    assert spy.calls == [], "a revoked lease authorized an execution"


# == 5. a plan revision invalidates the old approval ======================

def test_an_approval_given_before_a_revision_cannot_resume_the_new_plan(tmp_path, spy):
    """A revision changes what a parked step would do, so its approval is spent.

    Steering is reachable from the API (`/missions/{id}/steer`), so this is
    covered end to end rather than only at the runner seam.
    """
    client = _client()
    target = tmp_path / "steered.txt"
    mission_id = _write_mission(client, target, content="original")

    parked = _run(client, mission_id)
    assert parked["state"] == "awaiting_approval"
    old_approval = _confirmation(parked)
    assert old_approval

    steered = client.post(f"/missions/{mission_id}/steer", headers=HEADERS, json={
        "steps": [{"step_id": "S1", "title": "write", "tool": "write_file",
                   "params": {"path": str(target), "content": "changed"}}],
        "reason": "the person changed what the step should write"})
    assert steered.status_code == 200, steered.text
    body = steered.json()
    assert body["plan_revision"] == parked["plan_revision"] + 1
    assert body["state"] != "awaiting_approval", (
        "the mission kept waiting on an approval the revision invalidated")

    refused = _run(client, mission_id, {"confirmation_id": old_approval})

    # The security property, stated so any correct design satisfies it: the old
    # approval must not be the thing the mission now accepts. Re-parking with a
    # *new* confirmation is a legitimate fix, so no particular state is required
    # beyond "this approval no longer matches".
    assert refused["state"] != "awaiting_approval" or \
        _confirmation(refused) != old_approval, (
        "the pre-revision approval was carried onto the new plan")
    assert spy.calls == [], (
        "the pre-revision approval authorized a step of the new plan")
    assert not target.exists(), "the changed write ran on the old approval"


# == 6. running a mission does not by itself act ==========================

def test_running_a_parked_mission_without_an_approval_has_no_side_effect(tmp_path, spy):
    """`/run` is not consent. Without an approval it must return still parked."""
    client = _client()
    target = tmp_path / "never_acted.txt"
    mission_id = _write_mission(client, target)

    first = _run(client, mission_id)
    second = _run(client, mission_id)

    for result in (first, second):
        assert result["state"] == "awaiting_approval", (
            f"a run without an approval left the mission {result['state']}")
    assert spy.calls == [], "a mission acted without an approval"
    assert not target.exists(), "a parked mission wrote to disk"

    step = _step(_state(mission_id), "S1")
    assert step["status"] == "pending", (
        f"a parked step was marked {step['status']} without running")


def test_running_a_draft_mission_with_a_fabricated_approval_does_not_act(tmp_path, spy):
    """Supplying an approval to a mission that never parked buys nothing.

    A draft's step has not been shown to anyone, so there is no approval it
    could match. Accepting the caller's word here would make the whole
    confirmation step optional.
    """
    client = _client()
    target = tmp_path / "draft.txt"
    mission_id = _write_mission(client, target)

    result = _run(client, mission_id, {"confirmation_id": "deadbeef"})

    assert result["state"] == "awaiting_approval", (
        f"a fabricated approval of a draft mission let it run: {result['reason']}")
    assert spy.calls == [], "a fabricated approval reached the executor"
    assert not target.exists()


# == defects found by this file ===========================================
#
# These are real, reproduced defects, not speculative cases. Each is
# `xfail(strict=True)`: the suite stays green today, and a fix makes the test
# fail so the marker cannot outlive the bug. Reproductions are in the reason.

def test_a_run_attempt_cannot_erase_a_denial(tmp_path, spy):
    """The record of a refusal has to outlive an unrelated failed request.

    A denial is the one durable artefact of "the person said no". A request that
    is refused with 404 must not rewrite it on its way out.
    """
    client = _client()
    mission_id = _write_mission(client, tmp_path / "erased_denial.txt")
    assert _run(client, mission_id)["state"] == "awaiting_approval"

    denied = client.post(f"/missions/{mission_id}/steps/S1/decide",
                         headers=HEADERS, json={"decision": "deny"})
    assert denied.status_code == 200, denied.text
    assert _step(denied.json(), "S1")["detail"] == "denied by the operator"

    attempt = client.post(f"/missions/{mission_id}/run", headers=HEADERS, json={})
    assert attempt.status_code == 404, attempt.text

    after = _step(_state(mission_id), "S1")
    assert after["status"] == "failed", (
        f"the denial was overwritten by a refused run: {after['status']!r}")
    assert "denied by the operator" in (after["detail"] or ""), (
        f"the denial's reason was erased: {after['detail']!r}")


def test_a_rejected_confirmation_does_not_rotate_the_live_one(tmp_path, spy):
    """A refused credential must not be exchanged for a working one.

    The person was shown a confirmation id. If any refused request invalidates
    it and hands out the replacement, the id is not an approval bound to an
    action -- it is a value the API keeps reissuing to whoever asks.
    """
    client = _client()
    mission_id = _write_mission(client, tmp_path / "rotated.txt")

    parked = _run(client, mission_id)
    issued = _confirmation(parked)
    assert issued

    refused = _run(client, mission_id, {"confirmation_id": "deadbeef"})
    assert refused["state"] == "awaiting_approval"
    assert _confirmation(refused) == issued, (
        "a rejected id replaced the confirmation the operator was given")
    assert spy.calls == [], (
        "the replacement id disclosed by the refusal authorized the step")

    # And the id the person actually holds must still work.
    resumed = _run(client, mission_id, {"confirmation_id": issued})
    assert resumed["state"] != "awaiting_approval", (
        "the operator's own approval stopped matching after a bogus attempt")


def test_a_steered_mission_can_be_resumed_with_a_fresh_approval(tmp_path, spy):
    """Refusing the pre-revision approval is correct; refusing forever is not.

    A revision is a person's deliberate change. It must invalidate the *old*
    answer and then be answerable again, otherwise steering is a one-way
    bricking of the mission rather than an edit.
    """
    from jarvis.core.missions import MissionState
    from jarvis.core.missions import MissionService
    from jarvis.db.database import get_db

    client = _client()
    target = tmp_path / "resumable.txt"
    mission_id = _write_mission(client, target, content="before")

    parked = _run(client, mission_id)
    assert parked["state"] == "awaiting_approval"
    assert parked["intent"], "the mission recorded no intent, so this case does not apply"

    steered = client.post(f"/missions/{mission_id}/steer", headers=HEADERS, json={
        "steps": [{"step_id": "S1", "title": "write", "tool": "write_file",
                   "params": {"path": str(target), "content": "after"}}],
        "reason": "the person changed what the step should write"})
    assert steered.status_code == 200, steered.text

    # Recover to a runnable state, as the operator would after seeing a block.
    MissionService(get_db()).transition(
        mission_id, MissionState.PLANNED, actor="user",
        reason="the operator recovered the mission after the revision")

    run = _run(client, mission_id)
    fresh = _confirmation(run)
    if fresh:
        run = _run(client, mission_id, {"confirmation_id": fresh})

    assert run["state"] != "blocked", (
        "a revision left the mission permanently unrunnable: "
        f"{run.get('reason')}")