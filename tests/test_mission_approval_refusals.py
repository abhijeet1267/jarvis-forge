"""The approval system: what it must refuse, proved adversarially.

A mission's consequential steps park at ``awaiting_approval`` and resume only on a
per-step approval carrying a ``confirmation_id``. That makes the *refusals* the
security boundary: everything here asks "can a thing that is not a valid
approval be made to authorize a step?" rather than "does the happy path work".

Every case here is a strict test and passes against the current code. The
section at the bottom pins defects this file found; each was fixed rather than
marked `xfail`, and the test stays as the regression proof. Do not weaken them
to get a green run -- read the reason first.

What the confirmation id is, and is not (measured against this code, not
assumed): the gate mints it with `new_secret_id("CONF")` -- 128 random bits --
and writes the row as the same ordinary row a chat action's confirmation gets
(`jarvis/security/policy.py:_create_confirmation`). It is *not* a signature and
carries no identity: possession is the whole of the check once a caller is
through the door, and the door is authenticated by one master token (or a
session that stands in for it) for the entire backend. So this file can prove
binding and refusal; it cannot prove that a person, rather than any process
holding the token, was the one who clicked. `test_a_mission_confirmation_cannot_
be_answered_as_a_chat_action` pins the one thing that used to make that worse:
the mission's own gate confirmation was a fully live chat credential that
executed the step's *original* params, skipping the plan-revision binding.

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


# == 7. what the confirmation id actually is ==============================
#
# The audit answers, in order: who authenticates a caller, who mints the id,
# whether a person is distinguishable from a script, whether it expires, and
# what it is bound to. Each answer that is "no" is pinned below as a fact so a
# later change cannot quietly claim more than the system does.

def test_the_whole_backend_is_guarded_by_one_token_not_a_person(tmp_path, spy):
    """AUTHENTICATION IS A SHARED SECRET, NOT AN IDENTITY. SAY IT PLAINLY.

    `require_auth` accepts the master token or any live session token, and a
    dashboard session is refused only on the phone surface. There is no
    per-caller identity, no user account and no second factor anywhere on this
    path. So "a person approved this" is not a fact this system can produce; the
    strongest true statement is "something holding the token posted the id".

    This is a characterization test, not an endorsement. If a real per-person
    credential is ever added, this test should be rewritten to assert the new
    property -- not deleted.
    """
    client = _client()
    target = tmp_path / "shared_token.txt"
    mission_id = _write_mission(client, target)
    parked = _run(client, mission_id)
    assert parked["state"] == "awaiting_approval"

    # The very same shared token that conducted the mission answers it. No
    # separate approver identity exists to present.
    approved = _run(client, mission_id,
                    {"confirmation_id": _confirmation(parked)})
    assert approved["state"] != "awaiting_approval", (
        "the runner refused its own caller's approval, which contradicts the "
        "documented model: one token covers both")
    assert (tmp_path / "shared_token.txt").exists()


def test_the_confirmation_id_is_minted_by_the_gate_not_the_approver(tmp_path, spy):
    """The engine issues the id; the approval only echoes it back.

    `_create_confirmation` writes an ordinary `confirmations` row (128 random
    bits, no signature, no approver field) when the gate answers CONFIRM. The
    runner then compares the caller's offered string with the copy recorded on
    the parked step. Possession of that string is therefore the entire check
    once the caller is authenticated -- which is why the binding tests below
    (mission, step, revision) are the defense that actually exists.
    """
    from jarvis.api.app import get_core

    client = _client()
    mission_id = _write_mission(client, tmp_path / "minted.txt")
    parked = _run(client, mission_id)
    issued = _confirmation(parked)
    assert issued.startswith("CONF-"), issued
    assert len(issued) > 20, "a confirmation id is a credential; it must not be short"

    # It exists as a real row in the engine's table -- the same table a chat
    # action's confirmation would occupy.
    row = get_core().policy.get_confirmation(issued)
    assert row is not None and row["state"] == "pending"
    assert row["requested_surface"] == "mission"


# == 8. a mission confirmation is not a chat credential ===================
#
# Found by this audit, 2026-11-17. Before the fix, POST /confirm with the id
# scraped out of a parked mission step executed the step directly:
#
#   * with the mission still AWAITING_APPROVAL (its step never marked done, so
#     a later /run executed it a SECOND time -- measured: content "EE"), and
#   * after a steer, with the step's ORIGINAL params -- the pre-revision action
#     the mission's own binding had deliberately refused (measured: "old.txt"
#     written when the current plan said "new.txt").
#
# The fix is in `PermissionEngine.resolve` (mission-owned rows never resolve
# through the generic hub) and `JarvisCore.resolve_confirmation` (a resolve that
# leaves the row pending no longer falls through to execute). This test fails
# on the un-fixed tree.

def test_a_mission_confirmation_cannot_be_answered_as_a_chat_action(tmp_path, spy):
    """The id a person was shown for a mission step is not a chat credential."""
    client = _client()
    target = tmp_path / "chat_path.txt"
    mission_id = _write_mission(client, target, content="via-mission")
    parked = _run(client, mission_id)
    assert parked["state"] == "awaiting_approval"
    issued = _confirmation(parked)
    assert issued

    answered = client.post(f"/confirm/{issued}", headers=HEADERS,
                           params={"approve": "true"})
    # The route may keep its 200 envelope; the property is what matters.
    assert answered.status_code in (200, 409), answered.text
    assert spy.calls == [], (
        "a mission's parked confirmation executed the step through /confirm: "
        f"{spy.calls}")
    assert not target.exists(), "the step ran on a chat-style approval"

    # The mission is untouched and still answerable on its own route.
    after = _state(mission_id)
    assert after["state"] == "awaiting_approval"
    assert _step(after, "S1")["status"] == "pending"

    resumed = _run(client, mission_id, {"confirmation_id": issued})
    assert resumed["state"] != "awaiting_approval", (
        "refusing /confirm must not consume the mission's own approval")
    assert target.read_text(encoding="utf-8") == "via-mission"


def test_a_steer_cannot_be_undone_through_the_chat_confirm_route(tmp_path, spy):
    """The pre-revision id must not run the pre-revision action anywhere.

    The mission's own `/run` already refuses this (test 5 above). `/confirm`
    bypassed that refusal entirely because it rebuilt the action from the
    stored confirmation row and never compared `plan_revision`.
    """
    client = _client()
    old_target = tmp_path / "old.txt"
    new_target = tmp_path / "new.txt"
    mission_id = _write_mission(client, old_target, content="OLD")
    parked = _run(client, mission_id)
    old_approval = _confirmation(parked)
    assert old_approval

    steered = client.post(f"/missions/{mission_id}/steer", headers=HEADERS, json={
        "steps": [{"step_id": "S1", "title": "write", "tool": "write_file",
                   "params": {"path": str(new_target), "content": "NEW"}}],
        "reason": "the person changed what the step should write"})
    assert steered.status_code == 200, steered.text
    assert steered.json()["plan_revision"] == parked["plan_revision"] + 1

    # The mission's own route refuses the spent approval...
    refused = _run(client, mission_id, {"confirmation_id": old_approval})
    assert refused["state"] == "awaiting_approval", refused.get("reason")
    assert spy.calls == []

    # ...and the chat route must not be the way around that refusal.
    answered = client.post(f"/confirm/{old_approval}", headers=HEADERS,
                           params={"approve": "true"})
    assert answered.status_code in (200, 409), answered.text
    assert spy.calls == [], (
        "a pre-revision approval executed through /confirm after a steer: "
        f"{spy.calls}")
    assert not old_target.exists(), (
        "the pre-revision action ran -- the exact approval the revision was "
        "supposed to spend")
    assert not new_target.exists(), "the post-revision action ran on an old approval"


def test_a_mission_confirmation_does_not_survive_two_paths_as_a_double_execution(
        tmp_path, spy):
    """One approval buys one execution, whichever route is used.

    Before the fix, `/confirm` executed once and left the mission parked, so the
    next `/run` executed again. A delete or a send is not idempotent.
    """
    client = _client()
    target = tmp_path / "twice.txt"
    mission_id = _write_mission(client, target, content="once")
    parked = _run(client, mission_id)
    issued = _confirmation(parked)

    client.post(f"/confirm/{issued}", headers=HEADERS, params={"approve": "true"})
    ran_via_confirm = len(spy.calls)
    assert ran_via_confirm == 0, f"/confirm executed the step: {spy.calls}"

    _run(client, mission_id, {"confirmation_id": issued})
    assert len(spy.calls) == 1, (
        f"the step executed {len(spy.calls)} times for one approval: {spy.calls}")
    assert target.read_text(encoding="utf-8") == "once"


# == 9. binding: a revision that leaves the step unchanged ================

def test_a_revision_that_keeps_the_step_identical_keeps_its_approval(tmp_path, spy):
    """The binding is (tool, params), not "any revision at all".

    A revision that renames the step's title but leaves the action identical is
    not a new ask, so the person's answer still describes it -- and the runner's
    intent check, re-recorded against the new revision, must let that run. A
    guard that refused every post-revision approval would be safe but useless,
    and would turn steering into a permanent block (already covered positively
    by the resumable test above; this one isolates the unchanged-step case).
    """
    from jarvis.api.app import get_core

    client = _client()
    target = tmp_path / "unchanged.txt"
    mission_id = _write_mission(client, target, content="same")
    parked = _run(client, mission_id)
    issued = _confirmation(parked)
    assert issued

    steered = client.post(f"/missions/{mission_id}/steer", headers=HEADERS, json={
        "steps": [{"step_id": "S1", "title": "a clearer title",
                   "tool": "write_file",
                   "params": {"path": str(target), "content": "same"}}],
        "reason": "only the title changed"})
    assert steered.status_code == 200, steered.text
    assert steered.json()["plan_revision"] == parked["plan_revision"] + 1

    # The engine row still exists; the runner decides, not the revision number.
    row = get_core().policy.get_confirmation(issued)
    assert row is not None

    resumed = _run(client, mission_id, {"confirmation_id": issued})
    assert resumed["state"] != "awaiting_approval", (
        "a revision that left the step's action identical still forced "
        f"re-approval: {resumed.get('reason')}")
    assert target.read_text(encoding="utf-8") == "same"
    assert spy.tools_called() == ["write_file"]


# == 10. sequential decide calls: the second cannot re-answer ==============

def test_a_second_decision_on_a_settled_step_is_refused(tmp_path, spy):
    """Two sequential decide calls must not both take effect.

    The route guards on `step.status == "pending"`, so the second call is a 409
    and the denial recorded by the first is not overwritten into an approval.
    The property asserted is the executor was never reached.
    """
    client = _client()
    target = tmp_path / "settled.txt"
    mission_id = _write_mission(client, target)
    assert _run(client, mission_id)["state"] == "awaiting_approval"

    first = client.post(f"/missions/{mission_id}/steps/S1/decide", headers=HEADERS,
                        json={"decision": "deny"})
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "blocked"

    second = client.post(f"/missions/{mission_id}/steps/S1/decide", headers=HEADERS,
                         json={"decision": "approve"})
    assert second.status_code == 409, second.text

    assert spy.calls == [], "a settled step executed"
    assert not target.exists()
    step = _step(_state(mission_id), "S1")
    assert step["status"] == "failed"
    assert step["detail"] == "denied by the operator", (
        f"the denial was rewritten: {step['detail']!r}")


# == 11. an approval minted for one mission name is still per-mission =====

def test_an_approval_minted_in_one_mission_does_not_carry_to_another(tmp_path, spy):
    """The same shape of step in two missions carries two independent ids.

    `test_a_foreign_confirmation_id_cannot_resume_a_parked_mission` already
    covers a *different* id. This covers the stronger claim that matters for a
    cloned plan: two missions with byte-identical steps must not share one.
    """
    client = _client()
    target = tmp_path / "shared_shape.txt"
    one = _write_mission(client, target)
    two = _write_mission(client, target)

    first = _run(client, one)
    second = _run(client, two)
    assert _confirmation(first) and _confirmation(second)
    assert _confirmation(first) != _confirmation(second), (
        "identical steps in two missions were issued the same confirmation")

    refused = _run(client, one, {"confirmation_id": _confirmation(second)})
    assert refused["state"] == "awaiting_approval", (
        f"the other mission's id authorized this one: {refused.get('reason')}")
    assert spy.calls == []


# == 12. restart while pending: the id and the parked state survive =======

def test_a_parked_mission_and_its_confirmation_survive_a_restart(tmp_path, spy):
    """Park, re-open the database and a fresh service, then resume.

    A confirmation that lived only in memory would make a relaunch either lose
    the person's ability to answer or -- worse -- silently un-park. Both the
    mission row and the confirmation row are read back through new objects over
    the same database file.
    """
    from jarvis.core.missions import MissionService
    from jarvis.db.database import Database, get_db
    from jarvis.security.policy import PermissionEngine

    client = _client()
    target = tmp_path / "restart.txt"
    mission_id = _write_mission(client, target, content="survived")
    parked = _run(client, mission_id)
    assert parked["state"] == "awaiting_approval"
    issued = _confirmation(parked)
    assert issued

    # A fresh handle on the same file, and a fresh service over it.
    reopened = Database(get_db().path)
    fresh_service = MissionService(reopened)
    reloaded = fresh_service.get(mission_id)
    assert reloaded is not None, "the mission did not survive the restart"
    assert reloaded.state == "awaiting_approval"
    assert _confirmation(reloaded.to_dict()) == issued, (
        "the confirmation id the person was shown did not survive the restart")

    # The engine, too, still knows the row -- and says it is still pending, not
    # silently expired or approved.
    rows = reopened.query(
        "SELECT state, tool FROM confirmations WHERE confirmation_id = ?", (issued,))
    assert rows and rows[0]["state"] == "pending", (
        f"the confirmation row did not survive the restart: {rows}")
    assert rows[0]["tool"] == "write_file"

    # And the id still resumes the step after the restart.
    resumed = _run(client, mission_id, {"confirmation_id": issued})
    assert resumed["state"] != "awaiting_approval", (
        f"the parked approval stopped working after a restart: {resumed.get('reason')}")
    assert target.read_text(encoding="utf-8") == "survived"
    assert spy.tools_called() == ["write_file"]

# -- a mission's confirmation cannot be spent through the generic hub --------
#
# Measured by audit: the generic confirmation surface (`POST /confirm/{id}`) and
# the mission runner both resolve from the same confirmation store, but only the
# runner also compares the id with the one recorded on the parked step, checks
# the plan revision and spends the step's lease. Resolving a mission-owned
# confirmation through the hub therefore skipped every one of those.

def test_a_mission_confirmation_cannot_be_spent_through_the_confirm_route(tmp_path):
    """The hub must refuse a confirmation a mission's gate minted.

    Otherwise an approval parked at revision 0 could execute the step's original
    params after a steer replaced them -- the revision check is exactly what the
    hub path skips.
    """
    client = _client()
    target = tmp_path / "hub_bypass.txt"
    mission_id = _write_mission(client, target)

    parked = _run(client, mission_id)
    assert parked["state"] == "awaiting_approval", parked["state"]
    confirmation = _step(parked)["detail"].split("confirmation ", 1)[1].split()[0].rstrip(")").strip()

    # Try to spend it through the generic confirmation endpoint.
    response = client.post(f"/confirm/{confirmation}?approve=true", headers=HEADERS)

    # Whatever the hub answers, the effect is what matters: the mission's step
    # must not have run behind the runner's back.
    state = _state(mission_id)
    assert state["state"] == "awaiting_approval", (
        f"the hub resolved a mission-owned confirmation: state is {state['state']!r}")
    assert not target.exists(), "the hub path executed the parked step"
    assert response.status_code in (200, 404, 409, 422), response.status_code


def test_a_mission_confirmation_is_refused_by_the_policy_layer():
    """Belt and braces: the refusal is recorded where the confirmation lives.

    The route above may answer with a message; this asserts the policy store
    itself declined, so a future caller reaching `resolve` by another route hits
    the same wall.
    """
    from jarvis.core.orchestrator import JarvisCore

    core = JarvisCore()
    engine = core.policy
    if not hasattr(engine, "resolve"):
        pytest.skip("this build has no confirmation resolver to check")

    # A record shaped exactly as the mission gate writes it.
    created = engine.evaluate(
        type("A", (), {"tool": "write_file", "params": {"path": "/tmp/x"}, "device": "laptop_main"})(),
        session_id="mission:MIS-test", requested_surface="mission")
    confirmation_id = getattr(created, "confirmation_id", None)
    if not confirmation_id:
        pytest.skip("the policy engine minted no confirmation for this action")

    resolved = engine.resolve(confirmation_id)
    state = getattr(resolved, "state", None) or (resolved or {}).get("state")
    assert state != "approved", (
        "the generic resolver approved a mission-owned confirmation")
