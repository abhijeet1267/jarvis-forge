"""Phase 68: a job cannot outlive the process that was running it.

Reproduced on 2026-10-06 against a **copy** of the delivered app's real database, with the tools
called directly so nothing was written to the user's home:

    GET_CURRENT_TASK says: Current task: "lifecycle test" -- running, 0 of 2 step(s) done.
    STOP_CURRENT_TASK says: Stopped "lifecycle test" -- the job is cancelled. ...
                            verified: True  cancelled: True
    running rows in the copy: 11

Twelve rows, all named `lifecycle test`, all created between 14:01 and 14:19 on 2026-09-25 by
`tests/test_api.py:385` -- which is `job = get_core().tasks.create("lifecycle test", ["step_a",
"step_b"])`, run against the default home in the week before conftest gave each session its own.
Each has two steps, both `pending`: the process that opened them never came back.

Two live defects follow from that, and neither is cosmetic:

* **the assistant narrates work it is not doing.** `get_current_task` reads the stored column and
  tells the user it is running a ten-day-old test job;
* **the stop spends itself on a phantom.** `stop_current_task` cancels the newest `running` row and
  reports `verified: True`. A real job that is not the newest would keep running while the user was
  told it stopped -- the Phase 54 rule, "a stop that does not stop", reached from the other side.

The root cause is not the fixtures. `refresh_status()` exists and its own docstring names this
hazard -- "anything not recomputed there stayed ``running`` forever" -- but it is called only from
the orchestrator's *write* paths. Nothing reconciles a `running` row at the one moment the system
can know it is stale: process start, when no job of this process exists yet. This project has now
been bitten by a stored `running` status three times (Phase 26's retry, Phase 14's orphaned steps,
this), which is why the fix is placed at the ownership boundary rather than at another call site.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

API = importlib.import_module("jarvis.api.app")
from jarvis.core.models import ActionStatus
from jarvis.core.tasks import TaskManager, job_status_for
from jarvis.db import database as db_module
from jarvis.tools.base import ToolContext
from jarvis.tools.self_state import get_current_task, stop_current_task


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A task manager over a database of its own, so nothing touches the session's."""
    fresh = db_module.Database(path=tmp_path / "jarvis.db")
    monkeypatch.setattr(db_module, "_DB", fresh)
    return TaskManager()


def _orphan(store: TaskManager, goal: str = "lifecycle test") -> str:
    """A job opened and never advanced -- exactly what a killed process leaves behind."""
    job_id = store.create(goal, ["step_a", "step_b"]).job_id
    store.set_status(job_id, "running")
    return job_id


# ---------------------------------------------------------------------------
# 1. the reconciliation, at the moment it is knowable
# ---------------------------------------------------------------------------
def test_a_running_row_outliving_its_process_is_reconciled(store):
    """Before/after in one test, so the pre-state is on the record and the sweep is not vacuous."""
    job_id = _orphan(store)
    assert store.get(job_id)["status"] == "running", "the fixture is not the state being fixed"
    assert [s["status"] for s in store.get(job_id)["steps"]] == ["pending", "pending"]

    touched = store.reconcile_after_restart()

    assert touched == [job_id], touched
    assert store.get(job_id)["status"] == job_status_for(["pending", "pending"]) == "incomplete"


def test_a_job_this_process_started_is_left_alone(store):
    """The sweep happens at startup, before this process can own a job -- so a live job survives.

    This is the property that keeps the fix from being a blunt instrument: a job in flight has
    steps that are still `pending` (the orchestrator writes a step's status at its *outcome*, not
    when it begins), so a sweep that ran mid-flight would read "nothing started" and mark real work
    `incomplete`. It is safe precisely because it runs once, at the boundary, and nowhere else --
    which is also what the read-path test below pins.

    (The first draft of this test asserted that a step moved to `awaiting_confirmation` would make
    the job report that status on its own. It does not, and correctly so: `update_step` records the
    step and `refresh_status` aggregates, and the orchestrator is the one that calls it. The
    assertion was wrong about the design, not the design wrong about the job.)
    """
    store.reconcile_after_restart()                 # the startup moment
    live = store.create("real work", ["step_a"]).job_id
    store.set_status(live, "running")               # this process is running it, right now
    store.update_step(live, "step_a", ActionStatus.AWAITING_CONFIRMATION, "waiting for a person")

    assert store.get(live)["status"] == "running", "a job in flight was swept after startup"
    assert store.list(limit=5)[0]["status"] == "running", "reading the list changed job state"


def test_paused_and_cancelled_jobs_are_not_touched(store):
    """`paused` is a state a person set and expects to survive a restart; it is not a crash mark."""
    paused = _orphan(store, "pause me")
    store.set_status(paused, "paused")
    done = _orphan(store, "finished earlier")
    store.set_status(done, "done")

    assert store.reconcile_after_restart() == []
    assert store.get(paused)["status"] == "paused"
    assert store.get(done)["status"] == "done"


def test_the_sweep_reports_the_rows_it_moved_and_logs_nothing_it_did_not_do(store):
    """A silent correction is indistinguishable from a state that was always right."""
    first = _orphan(store, "one")
    second = _orphan(store, "two")
    moved = set(store.reconcile_after_restart())
    assert moved == {first, second}
    assert store.reconcile_after_restart() == [], "a second pass must find nothing left to fix"


# ---------------------------------------------------------------------------
# 2. the two user-visible lies, fixed at the source
# ---------------------------------------------------------------------------
@pytest.fixture()
def core_with_orphan(store, monkeypatch):
    """A JarvisCore whose task table holds one stale `running` row, as the delivered app does."""
    from jarvis.core.orchestrator import JarvisCore

    job_id = _orphan(store)
    core = JarvisCore()
    core.tasks = store
    monkeypatch.setattr(core, "tasks", store)
    return core, job_id


def test_the_current_task_answer_does_not_narrate_a_phantom(core_with_orphan):
    core, _job_id = core_with_orphan
    core.tasks.reconcile_after_restart()
    result = asyncio.run(get_current_task(ToolContext(core=core, device_id="laptop_main")))
    text = (result.message or "").lower()
    assert "lifecycle test" not in text, result.message
    assert result.output.get("running") is False, result.output


def test_the_stop_refuses_instead_of_cancelling_a_job_that_was_never_running(core_with_orphan):
    """The measured harm: `verified: True` on a stop that cancelled a ten-day-old test row."""
    core, job_id = core_with_orphan
    core.tasks.reconcile_after_restart()
    before = core.tasks.get(job_id)["status"]

    result = asyncio.run(stop_current_task(ToolContext(core=core, device_id="laptop_main")))

    assert result.output.get("cancelled") is False, result.output
    assert "nothing was stopped" in (result.message or "").lower(), result.message
    assert core.tasks.get(job_id)["status"] == before, "the refusal still wrote the row"


def test_without_the_sweep_the_stop_would_still_spend_itself_on_the_phantom(core_with_orphan):
    """Negative control on the pre-fix behaviour, so the fix is shown to be load-bearing.

    This asserts the bug, not the cure: if the reconciliation ever stops mattering, this test
    fails and says why the other two are not vacuous.
    """
    core, job_id = core_with_orphan
    result = asyncio.run(stop_current_task(ToolContext(core=core, device_id="laptop_main")))
    assert result.output.get("cancelled") is True, "the phantom no longer fools the stop path"
    assert core.tasks.get(job_id)["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 3. it has to happen on its own, at startup -- not when someone remembers
# ---------------------------------------------------------------------------
def test_a_starting_server_reconciles_the_rows_it_inherits():
    """Driven through the real lifespan, not grepped: a source check would pass on a call that
    never runs, which is the Phase 58 lesson (dead code behind a passing textual test).

    Seeded through the core the server actually uses rather than a patched database. The first
    version of this test swapped `db_module._DB` and passed alone, then failed in a full run:
    `get_core()` is a module global built against the session database long before this test, so
    the lifespan reconciled a database it was not looking at. Same lesson as Phase 33 and 63 --
    when a new test disagrees between `pytest file.py` and `pytest -q`, the shared object is the
    suspect, not the assertion.
    """
    from fastapi.testclient import TestClient

    tasks = API.get_core().tasks
    seeded = tasks.create("phase68 stale row", ["step_a", "step_b"]).job_id
    tasks.set_status(seeded, "running")
    try:
        assert tasks.get(seeded)["status"] == "running"

        with TestClient(API.app, base_url="http://127.0.0.1:8765"):
            pass                      # entering the context runs the lifespan

        after = tasks.get(seeded)["status"]
        assert after != "running", f"the server started and still claims {seeded} is running"
        assert after == "incomplete", after      # two pending steps: never started, not failed
    finally:
        # The row is this test's own; the session database is not the place to leave it.
        tasks.db.execute("DELETE FROM task_steps WHERE job_id = ?", (seeded,))
        tasks.db.execute("DELETE FROM tasks WHERE job_id = ?", (seeded,))


def test_the_reconcile_is_not_wired_into_a_read_path(store, monkeypatch):
    """Reads must not write. `/tasks` is polled every few seconds by two clients; a sweep on read
    would mean a GET mutating job state, which is the class of thing Phase 28 removed."""
    calls = []
    monkeypatch.setattr(TaskManager, "reconcile_after_restart",
                        lambda self: calls.append("read") or [])
    store.list(limit=5)
    store.get("JOB-nope")
    assert calls == [], "the list/detail read path now reconciles -- reads must not write state"
