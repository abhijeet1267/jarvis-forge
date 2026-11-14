"""A long workflow must not freeze the server that is running it.

`POST /workflows/{name}/execute` was an `async def` route that called the
orchestrator directly. Workflow steps are blocking processes — a test suite, a
build — so the call held the event loop for the step's whole duration: a 14-minute
RUN_TESTS left `/planner` timing out (measured: 6 consecutive 5 s timeouts while
the step ran), and the suite's own live-server tests failed because they could not
reach the server that was busy running them.

`TestClient` hides this: it drives the app through a portal with its own thread,
so a blocking `async def` still leaves that client answering. The property is
therefore checked on the route itself, against its own coroutine.
"""

from __future__ import annotations

import asyncio
import inspect
import threading

import pytest


def test_the_execute_route_yields_to_the_event_loop(monkeypatch):
    """The route must not do its blocking work on the loop it was called on.

    Run the route's own coroutine in a loop that also holds a ticker task. If the
    handler blocks, the ticker stops advancing. (This is the measurement the
    browser got for free: `/planner` timing out for the length of the step.)
    """
    import jarvis.workflows.orchestrator as orch_mod
    from jarvis.api.app import execute_workflow

    started = threading.Event()
    release = threading.Event()

    class _SlowOrchestrator:
        def execute_workflow_request(self, **_kwargs):
            started.set()
            release.wait(timeout=10)
            return {"status": "completed", "run_id": "WF_RUN-test",
                    "workflow_id": "SLOW", "current_status": "completed",
                    "step": 1, "total_steps": 1, "result": "ok"}

    monkeypatch.setattr(orch_mod, "WorkflowOrchestrator", _SlowOrchestrator)

    async def _scenario():
        ticks = 0

        async def _ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        ticker = asyncio.create_task(_ticker())
        route = asyncio.create_task(
            execute_workflow("SLOW", {"session_id": "t"}, None))

        # Wait until the blocking step has actually begun.
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set(), "the workflow never began"

        before = ticks
        await asyncio.sleep(0.3)
        advanced = ticks - before
        release.set()
        result = await route
        ticker.cancel()
        return advanced, result

    advanced, result = asyncio.run(_scenario())

    assert result["run_id"] == "WF_RUN-test"
    # A ticker that keeps running proves the loop was not held by the step.
    assert advanced >= 5, (
        f"the event loop advanced only {advanced} times while a blocking step "
        "ran — the route is holding the loop")


def test_the_route_awaits_rather_than_calling_directly():
    """A structural guard, cheap and exact: the orchestrator call is awaited."""
    from jarvis.api.app import execute_workflow

    source = inspect.getsource(execute_workflow)
    assert "await" in source, source
    assert "to_thread" in source or "run_in_executor" in source, (
        "the blocking orchestrator call must be moved off the event loop")