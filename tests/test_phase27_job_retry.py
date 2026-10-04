"""Phase 27: a job re-run that actually re-runs (plan §31's `retry`).

Phase 26 made ``retry`` honest instead of fictional -- it stopped claiming
``running``, stopped erasing the failure text, and started reporting
``executed: 0``. Its message named the reason plainly: *"This build has no job
re-dispatcher."* That sentence was true when it was written, and measured again
at the start of this phase:

    step columns: ['step_id', 'job_id', 'name', 'status', 'detail', 'seq', 'confirmation_id']
    params recoverable from the row? False | device? False
    invocations after tasks.retry(): {'get_time': 0, 'get_battery_status': 0}

So a step could not be re-issued from its own record at all: the row kept the
tool's *name* and nothing of what it had been asked to do. This phase adds the
missing half, and every guard that comes with it -- a re-run goes through the
same permission gate, mints a fresh confirmation for anything gated, and refuses
rather than inventing parameters it never stored.

Nothing here weakens Phase 26's R-2 rule: *automatic* recovery still retries
only what is known to be safe to run twice. A user asking for another attempt is
a different authority, and the gate is applied again on top of it.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from jarvis.api.app import app
from jarvis.core.models import (
    ActionStatus,
    ActionResult,
    CommandRequest,
    PlannedAction,
    PolicyDecision,
)
from jarvis.core.planner import Plan

AUTH_HEADERS = {"X-JARVIS-Token": __import__("os").environ["JARVIS_API_TOKEN"]}

READ_ONLY = "read_file"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1:8765", headers=AUTH_HEADERS)


@pytest.fixture(autouse=True)
def _handlers_restored(core):
    """The counting handlers stay installed until the test is over.

    A retry happens in a *later* call than the run that failed, so handlers
    installed by the first call must still be there for the second -- and they
    must not leak into the next test, because ``core`` is session-scoped. The
    endpoints run against ``get_core()``, which is a different instance, so both
    are restored.
    """
    from jarvis.api.app import get_core

    app_core = get_core()
    saved = {c: dict(c.tools._handlers) for c in {id(core): core, id(app_core): app_core}.values()}
    yield
    for target, handlers in saved.items():
        target.tools._handlers.clear()
        target.tools._handlers.update(handlers)


def _handlers(core, tools, failing):
    """Install counting handlers. The autouse fixture puts the real ones back."""
    calls = {t: 0 for t in tools}

    def build(tool):
        async def handler(ctx, **kw):
            calls[tool] += 1
            if tool in failing:
                return ActionResult(ok=False, tool=tool,
                                    error="power reporting unavailable", verified=False)
            return ActionResult(ok=True, tool=tool, message=f"{tool} done", verified=True)
        return handler

    for t in tools:
        core.tools._handlers[t] = build(t)
    return calls


def _run_two_step(core, second_params=None):
    """A real 2-action plan whose second step fails; returns its job id."""
    calls = _handlers(core, ["get_time", "get_battery_status"], failing={"get_battery_status"})
    original_parse = core.planner.parse
    core.planner.parse = lambda *a, **k: Plan(
        goal="time then battery", intent="workflow", planner="rules", actions=[
            PlannedAction(step_id=1, tool="get_time", params={}, device="laptop_main"),
            PlannedAction(step_id=2, tool="get_battery_status",
                          params=second_params if second_params is not None else {"verbose": "true"},
                          device="laptop_main"),
        ])
    try:
        response = asyncio.run(core.handle(CommandRequest(text="time then battery", source="p27")))
    finally:
        core.planner.parse = original_parse
    assert response.job_id, "no job was created, so there is nothing to retry"
    return response.job_id, calls


def _steps(core, job_id):
    return {s["seq"]: s for s in core.tasks.get(job_id)["steps"]}


# ---------------------------------------------------------------------------
# 27-A -- a step has to remember what it was asked to do
# ---------------------------------------------------------------------------


def test_a_stored_step_carries_what_it_needs_to_run_again(core):
    """The measurement that opened this phase: the row held a name and nothing else."""
    job_id, _ = _run_two_step(core)
    step = _steps(core, job_id)[1]
    assert step["name"] == "get_battery_status"
    assert json.loads(step["params"]) == {"verbose": "true"}, step
    assert step["device"] == "laptop_main", step


def test_a_step_that_never_ran_keeps_its_plan_too(core):
    """Parameters are recorded when the job is created, not only when it fails --
    otherwise a halted sequence could only ever be resumed for its last step."""
    job_id, _ = _run_two_step(core)
    assert json.loads(_steps(core, job_id)[0]["params"]) == {}
    assert _steps(core, job_id)[0]["device"] == "laptop_main"


# ---------------------------------------------------------------------------
# 27-B -- retrying re-runs the failed step, and only that one
# ---------------------------------------------------------------------------


def test_retry_reruns_the_failed_step_and_not_the_one_that_succeeded(core):
    job_id, calls = _run_two_step(core)
    calls["get_time"] = calls["get_battery_status"] = 0

    outcome = asyncio.run(core.retry_job(job_id))

    assert outcome.executed == 1, outcome.as_dict()
    assert calls["get_time"] == 0, (
        f"a re-run replayed the step that already landed: {calls}")
    assert calls["get_battery_status"] >= 1, calls


def test_a_successful_retry_leaves_the_job_done(core):
    job_id, calls = _run_two_step(core)
    step = _steps(core, job_id)[1]
    assert step["status"] == ActionStatus.FAILED.value

    # The cause is gone: the second attempt answers honestly.
    _handlers(core, ["get_battery_status"], failing=set())
    outcome = asyncio.run(core.retry_job(job_id))

    assert outcome.report.status == "done", outcome.as_dict()
    steps = _steps(core, job_id)
    assert steps[0]["status"] == ActionStatus.SUCCEEDED.value
    assert steps[1]["status"] == ActionStatus.SUCCEEDED.value
    assert "re-attempt requested" in steps[1]["detail"], (
        "the first failure stays on the record after the second attempt")


def test_a_failing_retry_leaves_the_job_failed_and_says_so(core):
    job_id, _ = _run_two_step(core)
    outcome = asyncio.run(core.retry_job(job_id))  # same handler, same failure
    assert outcome.executed == 1
    assert outcome.report.status == "failed", outcome.as_dict()
    steps = _steps(core, job_id)
    assert steps[1]["status"] == ActionStatus.FAILED.value


def test_retrying_a_job_with_nothing_failed_runs_nothing(core):
    job = core.tasks.create("all done", ["get_time"])
    seq = _steps(core, job.job_id)[0]["seq"]
    core.tasks.update_step(job.job_id, "get_time", ActionStatus.SUCCEEDED, "ok", seq=seq)
    core.tasks.refresh_status(job.job_id)

    outcome = asyncio.run(core.retry_job(job.job_id))
    assert outcome.executed == 0
    assert "nothing" in outcome.report.message.lower()


def test_an_unknown_job_is_not_redispatched(core):
    outcome = asyncio.run(core.retry_job("JOB-not-here"))
    assert outcome.executed == 0
    assert outcome.report.accepted is False
    assert outcome.report.reason == "no such job"


def test_a_cancelled_job_is_not_redispatched(core):
    job_id, _ = _run_two_step(core)
    core.tasks.cancel(job_id)
    outcome = asyncio.run(core.retry_job(job_id))
    assert outcome.executed == 0
    assert outcome.report.accepted is False


# ---------------------------------------------------------------------------
# 27-C -- a re-run is a fresh trip through the gate, not a replay of consent
# ---------------------------------------------------------------------------


def test_a_retry_of_a_gated_step_asks_again_and_runs_nothing_first(core, tmp_area):
    victim = tmp_area / "p27-gated.txt"
    victim.write_text("x")
    gate_calls = _handlers(core, ["delete_file"], failing={"delete_file"})
    time_calls = _handlers(core, ["get_time"], failing=set())
    original_parse = core.planner.parse
    core.planner.parse = lambda *a, **k: Plan(
        goal="delete the file", intent="workflow", planner="rules", actions=[
            PlannedAction(step_id=1, tool="get_time", params={}, device="laptop_main"),
            PlannedAction(step_id=2, tool="delete_file", params={"path": str(victim)},
                          device="laptop_main"),
        ])
    try:
        response = asyncio.run(core.handle(CommandRequest(text="delete the file", source="p27")))
    finally:
        core.planner.parse = original_parse

    assert response.job_id and response.needs_confirmation, response.reply
    assert gate_calls["delete_file"] == 0, "the gated step ran before it was approved"

    # The user approves, and it fails -- which is what makes the step retryable.
    approved = asyncio.run(core.resolve_confirmation(response.confirmation_id, True))
    assert approved.actions and approved.actions[0].ok is False, approved.reply
    assert gate_calls["delete_file"] == 1, gate_calls

    gate_calls["delete_file"] = 0
    time_calls["get_time"] = 0
    outcome = asyncio.run(core.retry_job(response.job_id))

    assert outcome.executed == 0, (
        "a destructive step must not execute on a retry before anyone approves it again")
    assert outcome.response is not None and outcome.response.needs_confirmation is True, (
        outcome.response.reply if outcome.response else outcome.as_dict())
    assert outcome.response.confirmation_id, "no fresh approval was offered"
    assert gate_calls["delete_file"] == 0
    assert time_calls["get_time"] == 0, "the step that already succeeded was replayed"


def test_the_fresh_confirmation_is_a_new_one_not_the_old_replay(core, tmp_area):
    """Reusing the earlier token would let an expired or revoked approval authorise
    a second execution."""
    victim = tmp_area / "p27-fresh.txt"
    victim.write_text("x")
    first = core.policy.evaluate(PlannedAction(
        step_id=1, tool="delete_file", params={"path": str(victim)}, device="laptop_main"))
    assert first.decision is PolicyDecision.CONFIRM
    core.policy.resolve(first.confirmation_id, True)

    job = core.tasks.create(
        "delete the file", ["delete_file"],
        planned=[PlannedAction(step_id=1, tool="delete_file",
                               params={"path": str(victim)}, device="laptop_main")])
    seq = _steps(core, job.job_id)[0]["seq"]
    core.tasks.update_step(job.job_id, "delete_file", ActionStatus.FAILED, "device did not answer",
                           seq=seq)
    _handlers(core, ["delete_file"], failing=set())
    outcome = asyncio.run(core.retry_job(job.job_id))
    assert outcome.response.confirmation_id != first.confirmation_id, (
        "the retry reused an approval that had already been spent")
    record = core.policy.get_confirmation(outcome.response.confirmation_id)
    assert record["state"] == "pending", record


# ---------------------------------------------------------------------------
# 27-D -- what a re-run refuses to do
# ---------------------------------------------------------------------------


def test_a_step_whose_parameters_were_never_stored_is_not_reinvented(core):
    """Jobs written before this phase have no params column content. The gate has
    to refuse them; filling in a plausible parameter would be the old bug with a
    nicer face."""
    job = core.tasks.create("delete the file", ["delete_file"])
    row = _steps(core, job.job_id)[0]
    core.db.execute("UPDATE task_steps SET params = '{}' WHERE job_id = ? AND seq = ?",
                    (job.job_id, row["seq"]))
    core.tasks.update_step(job.job_id, "delete_file", ActionStatus.FAILED,
                           "device did not answer", seq=row["seq"])
    core.tasks.refresh_status(job.job_id)
    calls = _handlers(core, ["delete_file"], failing=set())
    outcome = asyncio.run(core.retry_job(job.job_id))

    assert calls["delete_file"] == 0, "a destructive action ran with invented parameters"
    assert outcome.response is not None, outcome.as_dict()
    assert outcome.response.decision is PolicyDecision.BLOCK, outcome.response.reply
    assert "path" in outcome.response.reply, outcome.response.reply


def test_a_step_whose_tool_no_longer_exists_is_refused_not_crashed(core):
    job = core.tasks.create("frobnicate", ["frobnicate_the_warp_core"])
    seq = _steps(core, job.job_id)[0]["seq"]
    core.tasks.update_step(job.job_id, "frobnicate_the_warp_core", ActionStatus.FAILED,
                           "no agent owns this tool", seq=seq)
    core.tasks.refresh_status(job.job_id)

    outcome = asyncio.run(core.retry_job(job.job_id))
    assert outcome.executed == 0
    assert outcome.report.accepted is False
    assert "frobnicate_the_warp_core" in outcome.report.reason, outcome.report.reason


# ---------------------------------------------------------------------------
# 27-E -- what the caller is told
# ---------------------------------------------------------------------------


def test_the_retry_endpoint_reports_the_actions_it_ran(client):
    """Driven through the app's own core: ``get_core()`` is the instance the
    endpoints use, and it is not the session fixture's."""
    from jarvis.api.app import get_core

    app_core = get_core()
    job_id, calls = _run_two_step(app_core)
    calls["get_time"] = calls["get_battery_status"] = 0
    # The cause of the failure is gone, so this retry should genuinely finish.
    second = _handlers(app_core, ["get_battery_status"], failing=set())
    body = client.post(f"/tasks/{job_id}/retry").json()
    assert body["executed"] == 1, body
    assert body["job"]["status"] == "done", body["job"]
    assert "ran" in body["note"].lower(), body
    assert second["get_battery_status"] >= 1, second
    assert calls["get_time"] == 0, calls


def test_the_endpoint_still_says_zero_when_nothing_was_rerun(client, core):
    job = core.tasks.create("nothing failed", ["get_time"])
    seq = core.tasks.get(job.job_id)["steps"][0]["seq"]
    core.tasks.update_step(job.job_id, "get_time", ActionStatus.SUCCEEDED, "ok", seq=seq)
    core.tasks.refresh_status(job.job_id)
    body = client.post(f"/tasks/{job.job_id}/retry").json()
    assert body["executed"] == 0, body


def test_an_unknown_job_is_still_a_404(client):
    assert client.post("/tasks/JOB-does-not-exist/retry").status_code == 404


def test_a_re_run_is_distinguishable_in_the_ledger(core):
    """The chain has to show that this happened twice, and why the second time."""
    job_id, calls = _run_two_step(core)
    before = core.audit.recent(200)
    _handlers(core, ["get_battery_status"], failing=set())
    asyncio.run(core.retry_job(job_id))
    after = core.audit.recent(200)
    new = [r for r in after if r not in before]
    sessions = {r.get("session_id") for r in new}
    assert any(s == f"retry:{job_id}" for s in sessions), sessions
    assert core.audit.verify_chain()["ok"] is True
