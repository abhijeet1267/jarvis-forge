"""Phase 10 P0-1: `verification` must be the authority, not `ok`.

Phase 9 established that the tri-state field was write-only: the audit line, the
job-step status, the job "done" flag and the recovery engine all decided from
the boolean `.ok`, which only means "the handler returned without raising". Two
agents then overwrote `verified = True` after the fact. The reproduction this
file guards against was measured on the real tree:

    reply = "Your home gateway is offline, so I could not off Living room
             ceiling light. No device was switched.
             (Recovered automatically -- control_home_device: succeeded on retry 1)"
    failures row: {recovered: 1, strategy: retry, note: 'succeeded on retry 1'}

An honest refusal, promoted to a logged and user-visible recovery.
"""

from __future__ import annotations

import asyncio

from jarvis.core.models import (
    ActionStatus,
    ActionResult,
    CommandRequest,
    PlannedAction,
    PolicyDecision,
    Verification,
)
from jarvis.tools.base import ToolContext, ok, unverified


def _ctx(core) -> ToolContext:
    return ToolContext(
        device_id="laptop_main", session_id="p0-1", settings=core.settings,
        apps=core.apps, devices=core.devices, memory=core.memory,
        graph=core.graph, core=core,
    )


def _replace_handler(core, name, factory):
    """Install a handler factory and hand back the original for restore()."""
    original = core.tools._handlers[name]
    core.tools._handlers[name] = factory()
    return original


# --------------------------------------------------------------------------
# the field itself
# --------------------------------------------------------------------------

def test_ok_and_unverified_are_distinguishable():
    assert ok("t", "observed").verification is Verification.SUCCESS
    assert ok("t", "observed").verified is True

    result = unverified("t", "not observed")
    assert result.verification is Verification.UNKNOWN
    assert result.ok is True, "the handler did answer; only the effect is unestablished"
    assert result.verified is False, "an unobserved effect must not read as verified"


# --------------------------------------------------------------------------
# step status
# --------------------------------------------------------------------------

def test_action_status_maps_verification_not_ok(core):
    mapper = core._action_status

    assert mapper(ok("t", "observed")) is ActionStatus.SUCCEEDED
    assert mapper(unverified("t", "not observed")) is ActionStatus.UNVERIFIED
    assert mapper(ActionResult(ok=False, tool="t", error="boom", verified=False)) \
        is ActionStatus.FAILED


def test_unverified_is_its_own_state_not_a_failure(core):
    status = core._action_status(unverified("t", "not observed"))
    assert status is not ActionStatus.FAILED
    assert status is not ActionStatus.SUCCEEDED
    assert status.value == "unverified"


# --------------------------------------------------------------------------
# the audit line
# --------------------------------------------------------------------------

def test_unverified_action_is_not_audited_as_success(core):
    def factory():
        async def handler(ctx, **kw):
            return unverified("get_time", "clock could not be read")
        return handler

    original = _replace_handler(core, "get_time", factory)
    try:
        asyncio.run(core.handle(CommandRequest(text="what time is it", source="t")))
    finally:
        core.tools._handlers["get_time"] = original

    entry = next(r for r in core.audit.recent(15) if r.get("tool") == "get_time")
    assert str(entry["result"]) == "unverified", entry
    assert entry["result"] != "success", "the ledger asserted a verified effect"
    assert entry["result"] != "failure", "it did not fail either"


def test_a_verified_success_is_still_audited_as_success(core):
    """The fix must not have become a blanket downcast."""
    asyncio.run(core.handle(CommandRequest(text="what time is it", source="t")))
    entry = next(r for r in core.audit.recent(15) if r.get("tool") == "get_time")
    assert entry["result"] == "success", entry


# --------------------------------------------------------------------------
# recovery: the exact Phase 9 reproduction
# --------------------------------------------------------------------------

def test_recovery_does_not_call_a_no_op_a_recovered_success(core):
    """Fail once, then answer honestly that nothing was observed.

    That sequence is not a recovery, and must not be reported as one.

    Phase 26 (R-2) changed *how many times* the handler runs here, not what the
    report is allowed to say. ``control_home_device`` is LOW-risk and not
    declared idempotent, so a device command whose first attempt timed out is no
    longer re-sent on the strength of its error text -- one invocation, and the
    escalation says why. The same claim about an *executed* unverified retry is
    kept under a tool the new policy does re-run, in
    ``test_phase26_recovery_trust.py``.
    """
    state = {"n": 0}

    def factory():
        async def handler(ctx, **kw):
            state["n"] += 1
            if state["n"] == 1:
                return ActionResult(
                    ok=False, tool="control_home_device",
                    error="gateway connection reset", verified=False,
                )
            return unverified("control_home_device",
                              "gateway offline, no device was switched")
        return handler

    original = _replace_handler(core, "control_home_device", factory)
    try:
        response = asyncio.run(core.handle(
            CommandRequest(text="turn on the kitchen light", source="t")))
    finally:
        core.tools._handlers["control_home_device"] = original

    assert state["n"] == 1, (
        f"{state['n']} invocations -- a home command that may already have "
        "landed must not be re-sent automatically (Phase 26 R-2)")
    reply = response.reply.lower()
    assert "recovered automatically" not in reply, response.reply
    assert "succeeded on retry" not in reply, response.reply
    assert "did not work" in reply or "could not recover" in reply, response.reply

    rows = core.db.query(
        "SELECT recovered, attempts, strategy, note FROM failures "
        "WHERE tool = 'control_home_device' ORDER BY rowid DESC LIMIT 1"
    )
    assert rows, "the failed action must still be recorded"
    row = dict(rows[0])
    assert int(row["recovered"]) == 0, row
    assert row["strategy"] != "retry", f"an unverified retry was logged as a recovery: {row}"

    # Phase 9's R-12 note-lie, closed by Phase 26: the note and the retry
    # decision now come from the same function, so "not retried" here is the
    # truth -- the action genuinely was never re-executed.
    assert row["attempts"] == 0 and "not retried" in str(row["note"]), row


# --------------------------------------------------------------------------
# the agent override that reintroduced the original bug
# --------------------------------------------------------------------------

async def test_confirmed_action_audits_verification_not_ok(core, tmp_area):
    """``resolve_confirmation()`` has its own ``audit.record`` call site.

    Phase 10 P0-1 first fixed only ``_audit_action``, so every *confirmed*
    action still logged ``success`` straight from ``.ok`` -- measured as an
    audit row of ``result='success'`` beside an ActionResult carrying
    ``verified=False`` and ``audit_result='unknown'``.
    """
    def factory():
        async def handler(ctx, **kw):
            return unverified("delete_file", "answered, but nothing was observed")
        return handler

    original = _replace_handler(core, "delete_file", factory)
    victim = tmp_area / "second-site.txt"
    victim.write_text("x")
    try:
        verdict = core.policy.evaluate(PlannedAction(
            step_id=1, tool="delete_file", params={"path": str(victim)},
            device="laptop_main"))
        assert verdict.decision is PolicyDecision.CONFIRM, verdict.reason
        response = await core.resolve_confirmation(
            verdict.confirmation_id, True, session_id="second-site")
    finally:
        core.tools._handlers["delete_file"] = original

    assert response.actions, response.reply
    assert response.actions[0].verification is Verification.UNKNOWN

    entry = next(
        r for r in core.audit.recent(15)
        if r.get("tool") == "delete_file" and r.get("session_id") == "second-site"
    )
    assert str(entry["result"]) == "unverified", (
        f"the confirmed-action audit site logged {entry['result']!r} "
        "for a result the handler marked unverified"
    )
    assert (entry.get("detail") or {}).get("verification") == "unknown", entry


def test_laptop_agent_does_not_stamp_verified(core):
    agent = next(a for a in core.agents.values() if a.name == "laptop")
    spec = core.tools.get("get_time")
    assert spec is not None and agent.owns("get_time")

    def factory():
        async def handler(ctx, **kw):
            return unverified("get_time", "clock could not be read")
        return handler

    original = _replace_handler(core, "get_time", factory)
    try:
        from jarvis.core.models import PlannedAction

        result = asyncio.run(agent.execute(
            PlannedAction(step_id=1, tool="get_time", params={}, device="laptop_main"),
            _ctx(core),
        ))
    finally:
        core.tools._handlers["get_time"] = original

    assert result.verification is Verification.UNKNOWN
    assert result.verified is False, (
        "the agent overwrote the handler's own observation with verified=True"
    )
    assert result.verification_note, "the platform note itself should still be recorded"
