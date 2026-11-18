"""The Command Center journey, end to end, over the real HTTP routes.

This file owns the *user journey* and nothing else: an instruction becomes a
mission, the consequential step parks for approval, and the person's answer --
approve, deny, or nothing at all -- decides what happens to a real file on disk.

The adversarial refusals live in ``tests/test_mission_approval_refusals.py``
(forged ids, cross-step and cross-mission ids, revision binding, lease
revocation, the generic ``/confirm`` hub). This file deliberately does not
repeat them. What it adds is the sequence a person actually drives:

1. approve -> the mission completes, the step is done, the file holds exactly
   the content the person was shown;
2. deny -> the mission blocks, the step is failed with the denial on it, and
   the file was never written;
3. an approval id presented with no credential at all is refused, and the same
   id with the credential is accepted -- the id is never the capability;
4. the denial survives a fresh read of the mission (a new client, the list
   route), rather than living only in the response that recorded it;
5. desktop instructions are routed correctly: a cursor move opens a draft
   mission with a pointer step, a file move is refused as not-desktop, and an
   ambiguous "type this up" comes back as a question with no mission created.

Every assertion runs against ``jarvis.api.app:app`` in-process through
``TestClient``, the real permission engine, the real runner and the real
``write_file`` tool. Files are created under the test's own ``tmp_path`` and
nowhere else.
"""

from __future__ import annotations

import re

from tests.conftest import TEST_TOKEN

HEADERS = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}


# -- harness ----------------------------------------------------------------

def _client():
    from fastapi.testclient import TestClient
    from jarvis.api.app import app

    return TestClient(app, base_url="http://127.0.0.1:8765")


def _step(mission: dict, step_id: str = "S1") -> dict:
    return next(s for s in mission.get("steps") or [] if s["step_id"] == step_id)


def _confirmation_on(mission: dict, step_id: str = "S1") -> str:
    """The confirmation id recorded on a parked step, or "" when it has none."""
    match = re.search(r"confirmation (CONF-\S+)", _step(mission, step_id).get("detail") or "")
    return match.group(1).rstrip(")") if match else ""


def _write_mission(client, target, content: str) -> str:
    """A mission whose one step writes ``content`` to ``target``.

    A ``file_contains`` condition is recorded with it, so the runner verifies
    the effect by re-reading the file rather than by believing the write's own
    message. Without a condition the honest verdict is "unknown" and the
    mission ends partially completed even when the write worked; with one, a
    completed mission is a claim the file system can back up.
    """
    created = client.post("/missions", headers=HEADERS, json={
        "request": f"write {target.name} and prove it landed",
        "conditions": [{
            "condition_id": "C1",
            "statement": f"{target} holds exactly the approved content",
            "verifier": {"kind": "file_contains", "path": str(target),
                         "needle": content},
        }],
        "steps": [{
            "step_id": "S1",
            "title": f"write {target.name}",
            "tool": "write_file",
            "params": {"path": str(target), "content": content},
        }],
    })
    assert created.status_code == 200, created.text
    return created.json()["mission_id"]


def _run(client, mission_id: str, confirmation: str = "") -> dict:
    body = {} if not confirmation else {"approval": {"confirmation_id": confirmation}}
    return client.post(f"/missions/{mission_id}/run", headers=HEADERS, json=body).json()


def _parked(client, target, content: str) -> tuple:
    """Create and run a mission until its step awaits approval.

    Returns ``(mission_id, confirmed_id, shown_content)`` where
    ``shown_content`` is what the parked step says it will write -- the thing
    a person approves. Reading it from the parked mission (rather than only
    from the request this test sent) is what lets the approve test assert
    "the file holds what was approved" literally.
    """
    mission_id = _write_mission(client, target, content)
    parked = _run(client, mission_id)
    assert parked["state"] == "awaiting_approval", (
        f"a consequential write did not park for approval: {parked['state']}")
    confirmation = _confirmation_on(parked)
    assert confirmation, (
        f"the parked step recorded no confirmation to answer: "
        f"{_step(parked)['detail']!r}")
    return mission_id, confirmation, _step(parked)["params"]["content"]


# -- 1. approve: the journey a person expects to work -----------------------

def test_an_approved_write_runs_and_the_file_holds_what_was_approved(tmp_path):
    """Ask, park, approve, done -- with the file as the proof, not the status.

    The content asserted against is the content on the *parked step the person
    was shown*, so this fails if execution ever runs different params than the
    approval described.
    """
    target = tmp_path / "approved.txt"
    approved = "the exact payload the operator approved\nline two\n"

    client = _client()
    mission_id, confirmation, shown = _parked(client, target, approved)
    assert not target.exists(), "the parked mission wrote to disk before approval"

    resumed = _run(client, mission_id, confirmation)

    assert resumed["state"] in ("completed", "partially_completed"), (
        f"an approved mission ended {resumed['state']!r}: {resumed.get('reason')}")
    assert resumed["is_terminal"], "an approved, finished mission is not terminal"
    assert _step(resumed)["status"] == "done", (
        f"the approved step is {_step(resumed)['status']!r}: "
        f"{_step(resumed)['detail']!r}")
    assert target.exists(), "the approved write never reached the disk"
    assert target.read_text(encoding="utf-8") == shown, (
        "the file does not hold what the approval described")
    assert target.read_text(encoding="utf-8") == approved

    # With its condition independently verified, the honest verdict -- and the
    # state -- is the completed one, not merely "the steps ran".
    assert resumed["condition_summary"]["verified"] == 1, resumed["condition_summary"]
    assert resumed["verdict"] == "verified", resumed["verdict"]
    assert resumed["state"] == "completed", resumed["state"]


# -- 2. deny: the file must never appear ------------------------------------

def test_a_denied_write_blocks_the_mission_and_no_file_appears(tmp_path):
    """Same journey, the other answer. The absence of the file is the point."""
    target = tmp_path / "denied.txt"
    client = _client()
    mission_id, confirmation, _ = _parked(client, target, "must never land")

    denied = client.post(f"/missions/{mission_id}/steps/S1/decide",
                         headers=HEADERS, json={"decision": "deny"})
    assert denied.status_code == 200, denied.text
    body = denied.json()

    assert body["state"] == "blocked", (
        f"a denied mission ended {body['state']!r}, not blocked")
    step = _step(body)
    assert step["status"] == "failed", f"the denied step is {step['status']!r}"
    assert "denied" in (step["detail"] or "").lower(), (
        f"the step carries no denial reason: {step['detail']!r}")
    assert not target.exists(), "the denied write reached the disk"

    # Even holding the id the mission was parked with, nothing was written:
    # the denial settled the step before any approval could be spent on it.
    _run(client, mission_id, confirmation)
    assert not target.exists(), "a denied step executed on the parked id"


# -- 3. an id alone is not a capability -------------------------------------

def test_a_confirmation_id_without_a_credential_is_refused_and_writes_nothing(tmp_path):
    """The discriminating pair: same id, same body, only the header differs.

    A confirmation id is what a person is shown; the credential is what proves
    the caller may act. Posting the id with no ``X-JARVIS-Token`` must be a
    401 before any mission code runs, and the identical request *with* the
    credential must then succeed -- which pins that the refusal came from the
    missing credential and not from anything about the id.
    """
    target = tmp_path / "unauthorized.txt"
    client = _client()
    mission_id, confirmation, shown = _parked(client, target, "requires a credential")
    assert not target.exists()

    body = {"approval": {"confirmation_id": confirmation}}
    refused = client.post(f"/missions/{mission_id}/run", json=body)
    assert refused.status_code == 401, (
        f"an approval id with no credential was not refused: {refused.status_code} "
        f"{refused.text}")
    assert not target.exists(), "an unauthenticated approval wrote to the disk"

    # The hub surface is guarded the same way, by the same dependency.
    hub = client.post(f"/confirm/{confirmation}", params={"approve": "true"})
    assert hub.status_code == 401, hub.text
    assert not target.exists(), "the generic hub acted for an unauthenticated caller"

    # The mission is untouched by both refusals...
    after = client.get(f"/missions/{mission_id}", headers=HEADERS).json()
    assert after["state"] == "awaiting_approval"
    assert _step(after)["status"] == "pending"

    # ...and the very same id, now with the credential, is accepted: what was
    # missing was authorization, never the id.
    resumed = _run(client, mission_id, confirmation)
    assert resumed["state"] == "completed", (
        f"the id stopped working after an unauthenticated attempt: "
        f"{resumed['state']!r} {resumed.get('reason')}")
    assert target.read_text(encoding="utf-8") == shown


# -- 4. the denial survives a fresh read ------------------------------------

def test_a_denial_is_still_on_the_mission_when_it_is_read_again(tmp_path):
    """The record of "no" is durable state, not a response-only artefact.

    Read through a brand-new client and through the list route, so nothing the
    original response object remembers can be what makes this pass.
    """
    target = tmp_path / "persisted_denial.txt"
    client = _client()
    mission_id, _, _ = _parked(client, target, "no")

    denied = client.post(f"/missions/{mission_id}/steps/S1/decide",
                         headers=HEADERS, json={"decision": "deny"})
    assert denied.status_code == 200, denied.text

    fresh = _client()
    reloaded = fresh.get(f"/missions/{mission_id}", headers=HEADERS)
    assert reloaded.status_code == 200, reloaded.text
    mission = reloaded.json()

    assert mission["state"] == "blocked", (
        f"the denial was not persisted: state is {mission['state']!r}")
    step = _step(mission)
    assert step["status"] == "failed", (
        f"the denied step came back as {step['status']!r}")
    assert "denied" in (step["detail"] or "").lower(), (
        f"the denial's reason is gone: {step['detail']!r}")
    assert "awaiting approval" not in (step["detail"] or "").lower(), (
        "the denial was resurrected into a pending approval")
    assert not target.exists()

    blocked = fresh.get("/missions", headers=HEADERS, params={"state": "blocked"})
    assert blocked.status_code == 200, blocked.text
    listed = blocked.json()["missions"]
    assert any(m["mission_id"] == mission_id for m in listed), (
        "the blocked mission is missing from the blocked list")


# -- 5. desktop instruction routing -----------------------------------------

def test_a_cursor_move_instruction_opens_a_draft_mission_with_a_pointer_step():
    """The Command Center's desktop sentence must become a pointer step.

    Measured history: this sentence used to reach the filesystem planner as
    ``move_file`` with source="the cursor". The route must hand it to the
    desktop planner and open a mission -- in draft, so review and approval
    still come first.
    """
    client = _client()
    response = client.post("/missions/desktop", headers=HEADERS,
                           json={"request": "move the cursor to 400 300"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "planned", body
    assert body["clarification"] is None
    assert [s["tool"] for s in body["steps"]] == ["desktop_move_pointer"]
    assert body["steps"][0]["params"] == {"x": 400.0, "y": 300.0}
    mission = body["mission"]
    assert mission is not None, "a planned desktop instruction created no mission"
    assert mission["state"] == "draft", (
        f"a desktop instruction opened a {mission['state']!r} mission; plan "
        "review must come before anything acts")

    # It is a real mission, readable by id through the ordinary route.
    fetched = client.get(f"/missions/{mission['mission_id']}", headers=HEADERS)
    assert fetched.status_code == 200, fetched.text
    steps = fetched.json()["steps"]
    assert [s["tool"] for s in steps] == ["desktop_move_pointer"]


def test_a_file_move_instruction_is_refused_by_the_desktop_route():
    """"move report.pdf to Documents" is a file operation, not a pointer move.

    The route refuses what it does not own rather than guessing, so the
    instruction can never be planned as a cursor action on a file name.
    """
    client = _client()
    response = client.post("/missions/desktop", headers=HEADERS,
                           json={"request": "move report.pdf to Documents"})
    assert response.status_code == 422, response.text
    assert "not a desktop instruction" in response.json()["detail"]


def test_an_ambiguous_type_request_comes_back_as_a_question_with_no_mission():
    """"type this up for me" asks for composition, not literal keystrokes.

    The answer is a clarification, and no mission row is created for it: a
    question is a result, not a draft waiting to be run.
    """
    client = _client()
    before = {m["mission_id"]
              for m in client.get("/missions", headers=HEADERS).json()["missions"]}

    response = client.post("/missions/desktop", headers=HEADERS,
                           json={"request": "type this up for me"})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "clarification", body
    assert body["mission"] is None, "a clarification opened a mission"
    assert body["steps"] == [], "a clarification planned steps"
    assert body["clarification"], "a clarification with no question is not one"

    after = {m["mission_id"]
             for m in client.get("/missions", headers=HEADERS).json()["missions"]}
    assert after - before == set(), (
        f"a clarification created missions: {sorted(after - before)}")