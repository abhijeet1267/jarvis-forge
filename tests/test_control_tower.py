"""Agent Control Tower: the state is real, and the stop actually stops.

The load-bearing claims, each tested against the real dispatch path rather than
a simulated heartbeat:

* a worker reads ``running`` only while a call is genuinely inside it;
* pausing a worker makes its next dispatch fail with a reason, and the tool is
  never called;
* an exhausted budget does the same;
* a stopped worker stays stopped.

The gate lives in ``Agent.execute``, which every tool call in the system funnels
through, so these tests go through a real ``Agent`` and a real handler.
"""

from __future__ import annotations

import pytest

from jarvis.core.control_tower import (
    BUDGET_EXHAUSTED,
    IDLE,
    PAUSED,
    RUNNING,
    STOPPED,
    ControlTower,
    WorkerRefusal,
)
from jarvis.core.models import ActionResult, PlannedAction


@pytest.fixture()
def tower():
    """A fresh tower per test; the singleton is process-wide."""
    ControlTower.reset()
    t = ControlTower.get()
    yield t
    ControlTower.reset()


@pytest.fixture()
def agent():
    """A real ``Agent`` whose one tool records whether it was actually called."""
    from jarvis.agents.base import Agent
    from jarvis.registry.tools import ToolRegistry

    calls = []

    async def handler(ctx, **params):
        calls.append(params)
        return ActionResult(ok=True, tool="write_file",
                            output={"path": params.get("path", "")},
                            message="wrote")

    registry = ToolRegistry()
    from jarvis.core.models import ToolSpec

    registry.register(ToolSpec(name="write_file", agent="laptop",
                               description="write"), handler)

    class _Agent(Agent):
        name = "laptop"

    return _Agent(registry), calls


def _action(tool="write_file", **params):
    return PlannedAction(tool=tool, params=params or {"path": "/tmp/x"})


@pytest.mark.asyncio
async def test_a_call_passes_and_the_worker_records_it(tower, agent):
    a, calls = agent
    tower.register_worker("laptop", ["write_file"])

    result = await a.execute(_action(), ctx=None)

    assert result.ok and calls, "the tool was not called"
    worker = tower.worker("laptop")
    assert worker.calls_made == 1
    assert worker.state == IDLE, "a finished call should not leave it running"
    assert worker.last_duration_ms is not None


@pytest.mark.asyncio
async def test_a_paused_worker_refuses_and_the_tool_is_never_called(tower, agent):
    """The core claim: pausing does not merely draw a badge -- the call fails."""
    a, calls = agent
    tower.register_worker("laptop", ["write_file"])
    tower.pause("laptop", reason="the person is checking something")

    result = await a.execute(_action(), ctx=None)

    assert result.ok is False
    assert "paused" in result.error.lower(), result.error
    assert calls == [], "the tool ran while its worker was paused"
    assert tower.worker("laptop").calls_refused == 1


@pytest.mark.asyncio
async def test_resuming_lets_the_next_call_through(tower, agent):
    a, calls = agent
    tower.register_worker("laptop", ["write_file"])
    tower.pause("laptop")
    first = await a.execute(_action(), ctx=None)
    assert first.ok is False

    tower.resume("laptop")
    second = await a.execute(_action(), ctx=None)
    assert second.ok is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_an_exhausted_budget_refuses_further_calls(tower, agent):
    a, calls = agent
    tower.register_worker("laptop", ["write_file"], call_budget=2)

    assert (await a.execute(_action(), ctx=None)).ok is True
    assert (await a.execute(_action(), ctx=None)).ok is True
    third = await a.execute(_action(), ctx=None)

    assert third.ok is False
    assert "budget" in third.error.lower(), third.error
    assert len(calls) == 2, "a third call ran past the budget"
    assert tower.worker("laptop").state == BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_raising_the_budget_lets_the_worker_run_again(tower, agent):
    a, calls = agent
    tower.register_worker("laptop", ["write_file"], call_budget=1)
    await a.execute(_action(), ctx=None)
    assert (await a.execute(_action(), ctx=None)).ok is False

    tower.set_budget("laptop", 3)
    assert (await a.execute(_action(), ctx=None)).ok is True


@pytest.mark.asyncio
async def test_a_stopped_worker_does_not_resume(tower, agent):
    a, calls = agent
    tower.register_worker("laptop", ["write_file"])
    tower.cancel("laptop", reason="the person stopped it")

    result = await a.execute(_action(), ctx=None)
    assert result.ok is False
    assert "stopped" in result.error.lower()
    assert calls == []

    # Resume must refuse a stopped worker rather than quietly bringing it back.
    with pytest.raises(WorkerRefusal):
        tower.resume("laptop")


@pytest.mark.asyncio
async def test_a_worker_reads_running_only_while_a_call_is_inside_it(tower):
    """The state must come from a real in-flight call, not a timer."""
    from jarvis.agents.base import Agent
    from jarvis.registry.tools import ToolRegistry
    from jarvis.core.models import ToolSpec

    seen = {}

    async def handler(ctx, **params):
        seen["during"] = tower.worker("laptop").state
        return ActionResult(ok=True, tool="write_file", message="ok")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="write_file", agent="laptop"), handler)

    class _Agent(Agent):
        name = "laptop"

    tower.register_worker("laptop", ["write_file"])
    assert tower.worker("laptop").state == IDLE

    await _Agent(registry).execute(_action(), ctx=None)

    assert seen["during"] == RUNNING, "the worker did not read running during a call"
    assert tower.worker("laptop").state == IDLE


@pytest.mark.asyncio
async def test_a_failing_tool_is_counted_as_a_failure(tower):
    from jarvis.agents.base import Agent
    from jarvis.registry.tools import ToolRegistry
    from jarvis.core.models import ToolSpec

    async def handler(ctx, **params):
        return ActionResult(ok=False, tool="write_file", error="disk full")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="write_file", agent="laptop"), handler)

    class _Agent(Agent):
        name = "laptop"

    tower.register_worker("laptop", ["write_file"])
    await _Agent(registry).execute(_action(), ctx=None)

    worker = tower.worker("laptop")
    assert worker.failures == 1
    assert worker.last_error == "disk full"
    assert worker.in_flight == 0, "a failed call must not leave in-flight elevated"


def test_the_snapshot_counts_states_honestly(tower):
    tower.register_worker("a", ["a_tool"], call_budget=0)
    tower.register_worker("b", ["b_tool"])
    tower.pause("b")

    snap = tower.snapshot()
    assert snap["summary"]["total"] == 2
    assert snap["summary"]["paused"] == 1
    assert snap["summary"]["budget_exhausted"] == 1
    # The boundary note is part of the contract, not decoration.
    assert "dispatch boundary" in snap["note"]


def test_a_tool_no_worker_owns_is_not_gated(tower):
    """A bare agent with no registered worker still runs; the tower is an
    addition, not a prerequisite that would break every existing test agent."""
    assert tower.admit("an_unknown_tool") is None


def test_pausing_an_unknown_worker_is_a_lookup_error(tower):
    with pytest.raises(KeyError):
        tower.pause("nobody")


def test_a_negative_budget_is_refused(tower):
    tower.register_worker("laptop", ["write_file"])
    with pytest.raises(ValueError):
        tower.set_budget("laptop", -1)

# -- the gate over the wire ---------------------------------------------------

def test_the_tower_is_readable_and_controllable_over_http():
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app
    from jarvis.core.control_tower import ControlTower

    ControlTower.reset()
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}

    listing = client.get("/control-tower", headers=headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["workers"], "the tower registered no workers from the live core"
    assert "summary" in body

    name = body["workers"][0]["name"]

    paused = client.post(f"/agents/{name}/pause", headers=headers,
                         json={"reason": "checking"})
    assert paused.status_code == 200, paused.text
    assert paused.json()["state"] == PAUSED

    resumed = client.post(f"/agents/{name}/resume", headers=headers)
    assert resumed.json()["state"] != PAUSED

    budget = client.post(f"/agents/{name}/budget", headers=headers,
                        json={"budget": 0})
    assert budget.json()["state"] == BUDGET_EXHAUSTED

    freed = client.post(f"/agents/{name}/budget", headers=headers,
                       json={"budget": None})
    assert freed.json()["state"] != BUDGET_EXHAUSTED

    ControlTower.reset()


def test_a_paused_worker_is_refused_through_a_real_mission_run():
    """End to end: pause a worker, then run a mission whose step uses its tool,
    and the step fails with the pause as its reason. This is the claim the
    screen makes -- that pausing stops work -- proven through the real path."""
    import pytest as _pytest
    from tests.conftest import TEST_TOKEN
    from fastapi.testclient import TestClient
    from jarvis.api.app import app
    from jarvis.core.control_tower import ControlTower

    ControlTower.reset()
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    headers = {"X-JARVIS-Token": TEST_TOKEN, "Host": "127.0.0.1"}

    tower = ControlTower.get()
    from jarvis.api.app import get_core
    tower.register_from_registry(get_core().tools)

    # Pause the worker that owns write_file.
    owner = tower.worker_for_tool("write_file").name
    client.post(f"/agents/{owner}/pause", headers=headers, json={"reason": "test"})

    created = client.post("/missions", headers=headers, json={
        "request": "write a file",
        "steps": [{"step_id": "S1", "title": "write", "tool": "write_file",
                   "params": {"path": "/tmp/should_not_exist_control_tower.txt",
                              "content": "x"}}],
    })
    assert created.status_code == 200, created.text
    mission_id = created.json()["mission_id"]

    # Run; if the step needs approval it will park, so approve and resume until
    # it either blocks or finishes. The pause must stop the actual execution.
    import re
    for _ in range(4):
        run = client.post(f"/missions/{mission_id}/run", headers=headers, json={})
        data = run.json()
        if data["state"] != "awaiting_approval":
            break
        step = next((s for s in data["steps"] if s["status"] == "pending"), None)
        if step is None:
            break
        m = re.search(r"confirmation (\S+)", step.get("detail") or "")
        conf = m.group(1).rstrip(")") if m else ""
        lease = client.post(f"/missions/{mission_id}/leases", headers=headers,
                           json={"capability": "write_file",
                                 "scope": ["/tmp"], "max_uses": 1,
                                 "ttl_seconds": 300}).json()["lease_id"]
        client.post(f"/missions/{mission_id}/steps/{step['step_id']}/decide",
                    headers=headers,
                    json={"decision": "approve", "lease_id": lease})
        run = client.post(f"/missions/{mission_id}/run", headers=headers,
                         json={"approval": {"confirmation_id": conf,
                                            "lease_id": lease}})
        data = run.json()
        break

    import os
    assert not os.path.exists("/tmp/should_not_exist_control_tower.txt"), (
        "the file was written even though its worker was paused")
    # And the failure names the pause, so it is not a coincidental refusal.
    details = " ".join((s.get("detail") or "") for s in data.get("steps", []))
    assert "paused" in details.lower(), (
        f"the run failed but not because the worker was paused: {details!r}")
    ControlTower.reset()
