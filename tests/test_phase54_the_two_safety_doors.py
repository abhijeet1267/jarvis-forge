"""Phase 54 -- the two safety doors that had logic and no handle.

Measured before editing:

* ``emergency_stop`` is a real PHYSICAL-tier tool with honest reporting, but it is reachable
  only by typing the sentence. There is no route, so a phone -- the device §30 of the build
  prompt names first for an emergency stop -- cannot pull it.
* ``PermissionEngine.revoke`` writes an explicit denial, and **no product surface calls it at
  all**. The permission engine has one door and no handle: a user who wants to take a
  capability away has no way to say so except by editing the database.

Both phases of this file are about exposing what exists without building a second version of
it, and about the two ways that exposure could still lie: a stop that reports the cancel
request rather than the resulting state, and a "restore" that quietly grants standing authority
the user never asked for.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from jarvis.core.models import ActionStatus, PlannedAction, PolicyDecision

from tests.conftest import AUTH_HEADERS


def _api():
    """``jarvis.api.app`` is shadowed by the ``app`` attribute the package re-exports, so
    importlib is the only way to get the module itself -- the same reason the Phase 49 API
    test reaches for it."""
    import importlib

    return importlib.import_module("jarvis.api.app")


@pytest.fixture()
def client():
    with TestClient(_api().app, base_url="http://127.0.0.1:8765",
                    headers=AUTH_HEADERS) as test_client:
        yield test_client


@pytest.fixture()
def core():
    from jarvis.core.orchestrator import JarvisCore

    return JarvisCore()


def _clear_policy(core) -> None:
    """Start from the declared policy.

    A device-less denial binds every device (Phase 49a's asymmetric rule), so a revocation left
    by an earlier test in this shared session database would make a later device-scoped test
    read "blocked" for a reason it never created.
    """
    core.db.execute("DELETE FROM permissions")


def _clear_tasks(core) -> None:
    core.db.execute("UPDATE tasks SET status = 'done' WHERE job_id != ''")


def _running_job(core, goal: str = "p54 job"):
    _clear_tasks(core)
    job = core.tasks.create(goal, ["take_screenshot", "delete_file"])
    core.tasks.update_step(job.job_id, "take_screenshot", ActionStatus.SUCCEEDED)
    core.tasks.update_step(job.job_id, "delete_file", ActionStatus.RUNNING)
    core.tasks.set_status(job.job_id, "running")
    return job.job_id


def _audit_rows(core, intent: str):
    rows = core.db.query(
        "SELECT result, target, detail FROM action_logs WHERE intent = ? ORDER BY rowid DESC",
        (intent,))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# A. emergency stop, reachable from a client
# ---------------------------------------------------------------------------


def test_the_emergency_stop_route_exists(client):
    r = client.post("/emergency_stop")
    assert r.status_code == 200, r.text


def test_the_stop_route_requires_authorisation():
    with TestClient(_api().app, base_url="http://127.0.0.1:8765") as anon:
        assert anon.post("/emergency_stop").status_code == 401


def test_the_stop_is_not_confirmation_dependent(client, core):
    """A stop that has to be approved before it stops is not an emergency stop."""
    job_id = _running_job(core, goal="p54 gated stop")
    r = client.post("/emergency_stop")
    body = r.json()
    assert body.get("needs_confirmation") is not True, body
    assert body.get("confirmation_id") in (None, ""), body
    assert core.tasks.get(job_id)["status"] == "cancelled"


def test_the_stop_cancels_the_job_in_the_table_not_just_in_the_reply(client, core):
    job_id = _running_job(core, goal="p54 real stop")
    body = client.post("/emergency_stop").json()
    assert body["result"]["jobs_cancelled"] >= 1, body
    assert core.db.query(
        "SELECT status FROM tasks WHERE job_id = ?", (job_id,))[0]["status"] == "cancelled"


def test_the_stop_reports_state_it_read_back_not_state_it_requested(client, core, monkeypatch):
    """The first version trusted ``tasks.cancel()``'s report. A report says the request was
    accepted; only the row says the job will not run. This pins the difference."""
    from jarvis.core import tasks as tasks_module

    job_id = _running_job(core, goal="p54 lying report")

    original = tasks_module.TaskManager.cancel

    def accepted_but_unchanged(self, jid: str):
        report = original(self, jid)
        # Roll the row back: the cancel "succeeded" and the world did not change.
        self.db.execute("UPDATE tasks SET status = 'running' WHERE job_id = ?", (jid,))
        return report

    monkeypatch.setattr(tasks_module.TaskManager, "cancel", accepted_but_unchanged)
    body = client.post("/emergency_stop").json()
    result = body["result"]
    assert result["verified"] is False, (
        f"the stop claimed verification from a report while the row still says running: {result}")
    assert job_id in result["jobs_stuck"], result


def test_nothing_in_flight_is_reported_as_nothing_not_as_a_stopped_thing(client, core):
    _clear_tasks(core)
    result = client.post("/emergency_stop").json()["result"]
    assert result["jobs_cancelled"] == 0, result
    assert "nothing" in result["message"].lower(), result["message"]


def test_the_stop_still_says_what_it_cannot_stop(client):
    """The physical layer is on the robot and outside this process. A stop that omitted that
    would read as an all-clear."""
    result = client.post("/emergency_stop").json()["result"]
    assert "NOT stopped" in result["message"] or "not stopped" in result["message"].lower()
    assert result["physical_stop"] == "not_available", result


def test_the_stop_goes_through_the_existing_tool_not_a_second_one(client, monkeypatch):
    """§12 of the operating prompt: do not create a second emergency-stop implementation.

    Patched on the core the running app holds -- a second ``JarvisCore()`` would be a different
    registry, and a spy on it would prove nothing about which code the route reached.
    """
    import importlib

    app_module = importlib.import_module("jarvis.api.app")
    seen = {}
    live = app_module.get_core()
    original = live.tools.handler("emergency_stop")

    async def spy(ctx, **kwargs):
        seen["called"] = True
        return await original(ctx, **kwargs)

    live.tools._handlers["emergency_stop"] = spy
    try:
        client.post("/emergency_stop")
        assert seen.get("called") is True, "the route reimplemented the stop instead of calling it"
    finally:
        live.tools._handlers["emergency_stop"] = original


def test_the_stop_is_audited(client, core):
    _running_job(core, goal="p54 audited stop")
    client.post("/emergency_stop")
    rows = _audit_rows(core, "emergency.stop")
    assert rows, "an emergency stop left no ledger row"
    assert rows[0]["result"] in ("success", "unverified"), rows[0]


# ---------------------------------------------------------------------------
# B. tool-level revoke, on a governed surface
# ---------------------------------------------------------------------------


def test_the_policy_state_is_readable(client):
    body = client.get("/permissions/policy").json()
    assert isinstance(body, list), body
    if body:
        assert {"tool", "granted"} <= set(body[0]), body[0]


def test_revoking_an_unknown_tool_is_refused_and_writes_nothing(client, core):
    before = core.db.query("SELECT COUNT(*) AS n FROM permissions")[0]["n"]
    r = client.post("/permissions/not_a_real_tool/revoke")
    assert r.status_code == 404, r.text
    assert core.db.query("SELECT COUNT(*) AS n FROM permissions")[0]["n"] == before


def test_revoking_blocks_the_capability_and_the_reply_reports_the_state_after(client, core):
    _clear_policy(core)
    r = client.post("/permissions/send_email/revoke")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revoked"] is True, body
    assert body["state"]["blocked"] is True, body
    outcome = core.policy.evaluate(PlannedAction(
        step_id=1, tool="send_email", params={"to": "a@b.c", "subject": "x", "body": "y"},
        device="laptop_main"))
    assert outcome.decision is PolicyDecision.BLOCK, outcome.decision
    assert "revok" in outcome.reason.lower(), outcome.reason


def test_a_revocation_binds_the_other_spelling_of_the_same_capability(client, core):
    """A tool name is stored state (Phase 49a). Revoking the old name must still bind the
    canonical one, or the denial is cosmetic."""
    _clear_policy(core)
    client.post("/permissions/open_app/revoke")
    outcome = core.policy.evaluate(PlannedAction(
        step_id=1, tool="open_application", params={"app": "Safari"}, device="laptop_main"))
    assert outcome.decision is PolicyDecision.BLOCK, outcome.decision


def test_a_revocation_does_not_spill_onto_an_unrelated_capability(client, core):
    _clear_policy(core)
    client.post("/permissions/open_app/revoke")
    outcome = core.policy.evaluate(PlannedAction(
        step_id=1, tool="close_app", params={"app": "Safari"}, device="laptop_main"))
    assert outcome.decision is not PolicyDecision.BLOCK, (
        f"revoking open_app also blocked close_app: {outcome.reason}")


def test_a_device_scoped_revocation_binds_only_that_device(client, core):
    _clear_policy(core)
    r = client.post("/permissions/send_email/revoke", json={"device": "phone_watch"})
    assert r.status_code == 200, r.text
    elsewhere = core.policy.evaluate(PlannedAction(
        step_id=1, tool="send_email", params={"to": "a@b.c", "subject": "x", "body": "y"},
        device="laptop_main"))
    assert elsewhere.decision is not PolicyDecision.BLOCK, (
        "a device-scoped denial bound a device it never named")


def test_the_emergency_stop_itself_cannot_be_revoked(client, core):
    """A door that can be locked from the outside is not a safety door. Revoking the stop would
    be a weakening, whatever the caller's authority."""
    r = client.post("/permissions/emergency_stop/revoke")
    assert r.status_code == 409, r.text
    assert core.db.query("SELECT COUNT(*) AS n FROM permissions WHERE tool = 'emergency_stop'")[0]["n"] == 0
    rows = _audit_rows(core, "tool.revoke")
    assert rows and rows[0]["result"] == "denied", rows


def test_restoring_removes_the_denial_without_granting_standing_authority(client, core):
    """``grant`` writes granted=1, which also downgrades CONFIRM to EXECUTE for reversible
    tools. Un-revoking must return the tool to its declared policy, not hand it a permission
    the user never offered."""
    _clear_policy(core)
    client.post("/permissions/send_email/revoke")
    r = client.post("/permissions/send_email/restore")
    assert r.status_code == 200, r.text
    assert core.policy.is_revoked("send_email") is False
    assert core.policy.is_granted("send_email") is False, (
        "restore wrote a standing grant; the tool is now more trusted than before it was revoked")
    outcome = core.policy.evaluate(PlannedAction(
        step_id=1, tool="send_email", params={"to": "a@b.c", "subject": "x", "body": "y"},
        device="laptop_main"))
    assert outcome.decision is not PolicyDecision.BLOCK, outcome.reason


def test_restoring_something_never_revoked_says_so(client):
    r = client.post("/permissions/get_battery_status/restore")
    assert r.status_code == 200, r.text
    assert r.json()["restored"] is False, r.json()


def test_both_doors_are_audited(client, core):
    _clear_policy(core)
    client.post("/permissions/send_email/revoke")
    client.post("/permissions/send_email/restore")
    rows = _audit_rows(core, "tool.revoke")
    results = [r["result"] for r in rows[:2]]
    assert "success" in results, rows[:2]


def test_revoke_requires_authorisation():
    with TestClient(_api().app, base_url="http://127.0.0.1:8765") as anon:
        assert anon.post("/permissions/send_email/revoke").status_code == 401


def test_the_policy_list_shows_the_denial_after_it_is_made(client, core):
    _clear_policy(core)
    client.post("/permissions/send_email/revoke")
    rows = client.get("/permissions/policy").json()
    entry = next((r for r in rows if r["tool"] == "send_email"), None)
    assert entry is not None, rows
    assert entry["granted"] == 0, entry


# ---------------------------------------------------------------------------
# C. the surfaces that must be able to pull the stop
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("page", ["phone.html", "index.html"])
def test_both_clients_carry_an_emergency_stop_control(page):
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "dashboard" / page).read_text(
        encoding="utf-8")
    assert "/emergency_stop" in html, f"{page} has no way to reach the stop"
    assert "Emergency" in html or "emergency" in html, f"{page} names no stop"


def test_the_panel_can_show_a_revoked_capability():
    """The laptop panel carries it and the phone deliberately does not: a handset that could
    take capabilities away from the machine is a worse remote control, not a better one."""
    from pathlib import Path

    from pathlib import Path as _P

    html = (_P(__file__).resolve().parents[1] / "dashboard" / "index.html").read_text(
        encoding="utf-8")
    assert "/permissions/policy" in html, "the panel cannot read the policy state it shows"
    assert "data-revoke" in html and "data-restore" in html, (
        "the panel offers no revoke or restore control")
    assert "changePolicy(" in html, "the panel's controls are not wired to the routes"


# ---------------------------------------------------------------------------
# D. nothing above the doors changed
# ---------------------------------------------------------------------------


def test_no_gate_was_loosened(core):
    """This phase adds refusals. It must not remove one."""
    specs = core.tools.all()
    assert sum(1 for s in specs if s.requires_confirmation) == 28
    assert int(core.tools.get("emergency_stop").risk) == 4
    assert core.tools.get("emergency_stop").requires_confirmation is False


def test_the_registry_still_publishes_one_row_per_capability(core):
    specs = core.tools.all()
    assert len(specs) == len({s.name for s in specs})
    for spec in specs:
        for alias in spec.aliases:
            assert alias not in {s.name for s in specs}
