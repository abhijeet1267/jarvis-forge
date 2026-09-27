"""Phase 10 P0-3: emergency_stop must be truthful about what it can stop.

Phase 9 measured the previous implementation returning
``ok=True / Verification.SUCCESS / "Emergency stop engaged. Physical actions
cancelled; safety controllers remain active."`` with ``cancelled_actions=0`` --
it counted device capabilities that no registered device advertises, so it
performed no cancellation at all while reporting a completed physical safety
action. It is also the only risk-4 tool with no confirmation gate, so nothing
stood between the false claim and the user.

No physical robot, bridge or safety controller exists in this environment. These
tests therefore assert *software* behaviour and the wording only; nothing here
validates that any real actuator would stop.
"""

from __future__ import annotations

import asyncio
import re

from jarvis.core.models import RiskLevel
from jarvis.tools.base import ToolContext

PHYSICAL_CLAIM = re.compile(
    r"physical actions (were )?cancelled|stop engaged|all (physical )?actions (stopped|cancelled)",
    re.I,
)


def _ctx(core) -> ToolContext:
    return ToolContext(
        device_id="laptop_main",
        session_id="stop",
        settings=core.settings,
        apps=core.apps,
        devices=core.devices,
        memory=core.memory,
        graph=core.graph,
        core=core,
    )


def _run(core, **kw):
    return asyncio.run(core.tools.handler("emergency_stop")(_ctx(core), **kw))


def test_still_ungated_and_still_the_safety_tool(core):
    """The exemption from confirmation is deliberate and must not be lost."""
    spec = core.tools.get("emergency_stop")
    assert int(spec.risk) == int(RiskLevel.PHYSICAL)
    assert spec.requires_confirmation is False
    assert "safety" in (spec.tags or [])


def test_never_claims_physical_cancellation(core):
    result = _run(core)
    assert not PHYSICAL_CLAIM.search(result.message), result.message


def test_reports_the_physical_layer_as_unavailable(core):
    result = _run(core)
    out = result.output
    assert out["physical_stop"] == "not_available"
    assert "robot" in out["physical_stop_reason"].lower() or "bridge" in out["physical_stop_reason"].lower()
    assert "not stopped" in result.message.lower() or "NOT stopped" in result.message
    assert "emergency-stop" in result.message.lower()


def test_safety_controllers_are_not_claimed_running(core):
    """It never had a way to observe them, so it must not assert their state."""
    controllers = _run(core).output["safety_controllers"].lower()
    assert "running" != controllers
    assert "untouched" in controllers or "independent" in controllers


def test_idle_stop_says_there_was_nothing_to_cancel(core):
    core.tasks.db.execute("DELETE FROM tasks")
    result = _run(core)
    assert result.output["jobs_cancelled"] == 0
    assert "nothing here to cancel" in result.message.lower()


def test_a_running_job_is_actually_cancelled(core):
    """The claim must be backed by an observable state change."""
    core.tasks.db.execute("DELETE FROM tasks")
    job = core.tasks.create(goal="long render", steps=["encode"])
    core.tasks.set_status(job.job_id, "running")

    result = _run(core)

    assert result.output["jobs_cancelled"] == 1
    assert job.job_id in result.output["cancelled_job_ids"]
    after = core.tasks.get(job.job_id)
    assert str(after["status"]) == "cancelled", after
    assert "1 JARVIS job" in result.message


def test_a_pending_scheduled_job_is_not_misreported_as_stopped(core):
    """Cancelling JARVIS work is not the same as stopping in-flight motion."""
    core.tasks.db.execute("DELETE FROM tasks")
    result = _run(core)
    assert result.output["physical_stop"] == "not_available"
    assert "actuator" in result.message.lower()


def test_verification_tracks_observation_not_intention(core):
    """A stop that hit a stuck job must not be reported as fully verified."""
    core.tasks.db.execute("DELETE FROM tasks")
    job = core.tasks.create(goal="stuck", steps=["s"])
    core.tasks.set_status(job.job_id, "running")

    real_cancel = core.tasks.cancel

    def refusing(job_id):
        return False

    core.tasks.cancel = refusing
    try:
        result = _run(core)
    finally:
        core.tasks.cancel = real_cancel

    assert result.output["jobs_stuck"] == [job.job_id]
    assert result.verified is False, (
        "a stop that could not cancel the work it was asked to stop must not "
        "report a verified outcome"
    )
