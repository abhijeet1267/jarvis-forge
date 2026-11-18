"""VO-06: "why didn't it work" is answered from the mission ledger.

These drive the real ``MissionService`` and the real diagnosis tool. The claim
under test is not "a string comes back" -- it is that the answer is grounded in
the recorded mission and refuses to invent a cause when the record is silent.
"""

from __future__ import annotations

import pytest

from jarvis.core.missions import MissionState
from jarvis.core.orchestrator import JarvisCore
from jarvis.tools.diagnosis import explain_last_failure


class _Ctx:
    """The minimal ToolContext the tool reads: ``.core`` and ``.device_id``."""

    device_id = "laptop_main"

    def __init__(self, core):
        self.core = core


@pytest.fixture
def core():
    return JarvisCore()


def _failed_mission(core, detail="permission denied: cannot write to /protected"):
    m = core.missions.create(
        "save the note",
        scope={},
        conditions=[{"condition_id": "S1", "statement": "write_file on laptop",
                     "verifier": "tool:write_file"}],
        steps=[{"step_id": "S1", "title": "write the note", "tool": "write_file",
                "params": {"path": "/protected/note.txt"}}],
    )
    core.missions.transition(m.mission_id, MissionState.PLANNED, actor="test")
    core.missions.transition(m.mission_id, MissionState.RUNNING, actor="test")
    core.missions.mark_step(m.mission_id, "S1", status="failed", detail=detail)
    core.missions.transition(m.mission_id, MissionState.FAILED, actor="test",
                             reason="one or more actions failed")
    return core.missions.get(m.mission_id)


def _run(core, mission_id=""):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(
        explain_last_failure(_Ctx(core), mission_id=mission_id))


def test_explains_a_real_failure(core):
    m = _failed_mission(core)
    result = _run(core, m.mission_id)
    assert result.ok
    assert result.output["found"] is True
    assert "permission denied: cannot write" in result.message, (
        "the recorded error is not in the explanation")
    assert result.output["failed_step"]


def test_no_failure_in_ledger_is_reported_not_invented(core):
    """With nothing explainable in the ledger, the tool says so rather than guessing.

    The test database is shared across the session, so a sibling test's failure
    may already be present. This asserts the *behaviour* -- an empty ledger
    yields an honest "nothing to explain" -- by checking a fresh ledger look-up
    agrees with the tool, instead of assuming the ledger starts empty.
    """
    explainable = core.missions.list(
        limit=50,
        states=[MissionState.FAILED, MissionState.CANCELLED,
                MissionState.PARTIALLY_COMPLETED, MissionState.BLOCKED],
    )
    result = _run(core)
    assert result.ok
    if explainable:
        # A failure exists, so the tool must have explained *one of them*, and
        # must not have claimed the ledger was empty.
        assert result.output["found"] is True
        assert result.output["mission_id"]
    else:
        assert result.output["found"] is False
        low = result.message.lower()
        assert "invent" in low or "nothing to explain" in low


def test_unknown_mission_id_is_reported(core):
    result = _run(core, "MISSION-DOES-NOT-EXIST")
    assert result.ok
    assert result.output["found"] is False


def test_explanation_never_suggests_a_destructive_rerun(core):
    m = _failed_mission(core)
    result = _run(core, m.mission_id)
    joined = " ".join(result.output.get("safe_next_steps") or []).lower()
    for verb in ("delete", "remove", "overwrite", "kill", "terminate", "rm "):
        assert verb not in joined, f"safe_next_steps suggests {verb!r}"


def test_explanation_marks_unknown_outcome_when_unverified(core):
    """A failed step with no verification must not read as a confirmed failure."""
    m = _failed_mission(core)
    result = _run(core, m.mission_id)
    # Written to the output so the UI can show uncertainty rather than a verdict.
    assert "outcome_unknown" in result.output


def test_tool_is_low_risk_and_needs_no_confirmation(core):
    """Diagnosis reads; it must never require approval to answer a question."""
    spec = core.tools.get("explain_last_failure")
    assert spec is not None
    assert spec.requires_confirmation is False