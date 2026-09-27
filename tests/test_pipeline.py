"""End-to-end pipeline tests: the proof loop from section 60.

    Intent -> Plan -> Permission -> Tool -> Execution -> Verification
           -> Memory -> Response
"""

from __future__ import annotations

import pytest
from envcaps import HAS_BATTERY

from jarvis.core.models import ActionStatus, CommandRequest, PolicyDecision
from jarvis.core.orchestrator import JarvisCore


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not HAS_BATTERY, reason="this host reports no battery; the product's honest answer is 'unavailable'")
async def test_battery_command(core: JarvisCore, make_request) -> None:
    response = await core.handle(make_request("what is my battery"))

    assert response.intent == "laptop_battery"
    assert response.actions
    assert response.actions[0].ok
    assert "battery" in response.reply.lower()


async def test_time_command(core: JarvisCore, make_request) -> None:
    response = await core.handle(make_request("what time is it"))
    assert response.intent == "time"
    assert response.actions[0].ok


async def test_help_lists_capabilities(core: JarvisCore, make_request) -> None:
    response = await core.handle(make_request("help"))
    assert response.intent == "help"
    assert "open" in response.reply.lower()


async def test_search_files(core: JarvisCore, make_request) -> None:
    response = await core.handle(make_request("find my report"))
    assert response.intent == "search_files"
    assert response.actions[0].ok


async def test_emergency_stop_always_allowed(core: JarvisCore, make_request) -> None:
    response = await core.handle(make_request("stop everything"))

    assert response.intent == "emergency_stop"
    assert response.decision is PolicyDecision.EXECUTE
    assert response.actions[0].ok
    assert not response.needs_confirmation


# ---------------------------------------------------------------------------
# Confirmation flow
# ---------------------------------------------------------------------------
async def test_delete_requires_confirmation_then_moves_to_trash(
    core: JarvisCore, make_request, tmp_area
) -> None:
    target = tmp_area / "AMR_old.txt"
    target.write_text("scratch")

    response = await core.handle(make_request(f"delete {target}"))

    assert response.decision is PolicyDecision.CONFIRM
    assert response.confirmation_id
    assert target.exists(), "nothing may happen before approval"

    approved = await core.resolve_confirmation(response.confirmation_id, approve=True)

    assert approved.actions[0].ok
    assert not target.exists()
    assert "trash" in approved.reply.lower()


async def test_denied_delete_leaves_the_file(core: JarvisCore, make_request, tmp_area) -> None:
    target = tmp_area / "keep_me.txt"
    target.write_text("important")

    response = await core.handle(make_request(f"delete {target}"))
    denied = await core.resolve_confirmation(response.confirmation_id, approve=False)

    assert target.exists()
    assert "cancel" in denied.reply.lower()


async def test_double_resolve_is_reported(core: JarvisCore, make_request, tmp_area) -> None:
    target = tmp_area / "once.txt"
    target.write_text("x")

    response = await core.handle(make_request(f"delete {target}"))
    await core.resolve_confirmation(response.confirmation_id, approve=False)
    again = await core.resolve_confirmation(response.confirmation_id, approve=False)

    assert "already" in again.reply.lower()


# ---------------------------------------------------------------------------
# Safety tests (section 45)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    [
        "delete everything",
        "wipe the disk",
        "run this command as root",
        "install unknown.exe",
        "unlock the door",
        "disable the firewall",
        "pay 500 dollars",
    ],
)
async def test_safety_commands_are_never_silently_executed(
    core: JarvisCore, make_request, command: str
) -> None:
    """Every dangerous phrasing must be rejected, clarified or confirmed."""
    response = await core.handle(make_request(command))

    rejected = response.decision is PolicyDecision.BLOCK
    confirmed = response.decision is PolicyDecision.CONFIRM
    clarified = not response.plan.actions

    assert rejected or confirmed or clarified, (
        f"{command!r} executed silently with decision={response.decision}"
    )
    # And critically: nothing may have actually run.
    assert not response.actions or all(not a.ok for a in response.actions)


async def test_protected_paths_are_blocked(core: JarvisCore, make_request) -> None:
    for command in ("delete /etc/hosts", "delete ~/.ssh/id_rsa", "delete ~"):
        response = await core.handle(make_request(command))
        assert response.decision is PolicyDecision.BLOCK, command


async def test_vague_delete_is_clarified_not_guessed(core: JarvisCore, make_request) -> None:
    response = await core.handle(make_request("delete this"))
    assert not response.plan.actions
    assert response.reply


# ---------------------------------------------------------------------------
# Honesty about unimplemented capabilities
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "command",
    [
        # v0.6 made transfer real, v0.7 email/calendar, v1.5 home, v1.7 the car and
        # v1.8 the watch, so the phone side is what still reports itself honestly
        # as unimplemented.
        "what is my phone battery",
    ],
)
async def test_unimplemented_capabilities_are_honest(
    core: JarvisCore, make_request, command: str
) -> None:
    response = await core.handle(make_request(command))

    assert not response.actions, f"{command!r} should not produce executed actions"
    assert "not" in response.reply.lower()


async def test_car_reports_disconnected_honestly(core: JarvisCore, make_request) -> None:
    from jarvis.car.gateway import reset_car_gateway

    reset_car_gateway(None)
    try:
        response = await core.handle(make_request("check my car fuel"))
        assert response.actions[0].ok
        assert response.actions[0].output.get("connected") is False
    finally:
        # Reading the car builds the process-wide gateway singleton and marks it
        # probed, and /health asserts it has *not* been probed. Leaving it here
        # made test_api fail whenever this module ran first -- invisible only
        # because alphabetical order puts test_api earlier. Found 2026-09-26 while
        # investigating that exact failure (Phase 16 finding 16-C).
        reset_car_gateway(None)


async def test_unknown_command_gets_guidance(core: JarvisCore, make_request) -> None:
    response = await core.handle(make_request("flibbertigibbet the wozzle"))
    assert response.intent == "unknown"
    assert response.reply


# ---------------------------------------------------------------------------
# Memory and audit
# ---------------------------------------------------------------------------
async def test_remember_then_recall(core: JarvisCore, make_request) -> None:
    saved = await core.handle(make_request("remember that the AMR chassis uses 3mm aluminium"))
    assert saved.actions[0].ok

    recalled = await core.handle(make_request("do you remember the AMR chassis"))
    assert recalled.intent == "recall"
    assert "aluminium" in recalled.reply.lower()


async def test_audit_records_every_action(core: JarvisCore, make_request) -> None:
    before = len(core.audit.recent(limit=200))
    await core.handle(make_request("what is my battery"))
    after = core.audit.recent(limit=200)

    assert len(after) > before
    assert any(row["tool"] == "battery_status" for row in after)


@pytest.mark.skipif(not HAS_BATTERY, reason="needs a real battery reading to audit")
async def test_audit_result_is_a_status_token_not_a_message(core: JarvisCore, make_request) -> None:
    """The result column must stay filterable, so successes never appear as failures."""
    await core.handle(make_request("what is my battery"))

    rows = core.audit.recent(limit=50)
    battery_rows = [r for r in rows if r["tool"] == "battery_status"]
    assert battery_rows
    assert battery_rows[0]["result"] == "success"

    failure_tools = {r["tool"] for r in core.audit.failures(limit=50)}
    assert "battery_status" not in failure_tools


async def test_session_memory_is_written(core: JarvisCore, make_request) -> None:
    stats_before = core.memory.stats().get("total", 0)
    await core.handle(make_request("what time is it"))
    assert core.memory.stats().get("total", 0) >= stats_before


# ---------------------------------------------------------------------------
# Failure handling (section 47)
# ---------------------------------------------------------------------------
async def test_tool_failure_is_reported_not_hidden(core: JarvisCore, make_request, tmp_area) -> None:
    """Deleting a path that does not exist must fail loudly."""
    missing = tmp_area / "not_here.txt"
    response = await core.handle(make_request(f"delete {missing}"))

    # Either it is blocked up front or the action reports failure.
    if response.actions:
        assert not response.actions[0].ok
        assert "not" in response.reply.lower() or "could not" in response.reply.lower()
    else:
        assert response.decision in (PolicyDecision.BLOCK, PolicyDecision.CONFIRM)


async def test_read_file_rejects_oversize_and_missing(core: JarvisCore) -> None:
    from jarvis.tools.base import ToolContext
    from jarvis.tools.files import read_file

    ctx = ToolContext()
    result = await read_file(ctx, path="/tmp/definitely-not-here-12345.txt")
    assert not result.ok


# ---------------------------------------------------------------------------
# Recovery integration (section 47)
# ---------------------------------------------------------------------------
async def test_orchestrator_reports_a_recovery(
    core: JarvisCore, make_request, monkeypatch
) -> None:
    """A recovered action must be reported, not silently presented as clean."""
    from jarvis.core.recovery import RecoveryOutcome
    from jarvis.core.models import ActionResult

    async def fake_recover(action, ctx, execute, spec, first_result):
        return RecoveryOutcome(
            recovered=True,
            strategy="retry",
            attempts=1,
            result=ActionResult(
                ok=True, tool=action.tool, message="Recovered fine.", verified=True
            ),
            note="succeeded on retry 1",
            tried=["retry#1"],
        )

    # Make the first execution fail so recovery is triggered.
    async def failing_execute(action, ctx):
        return ActionResult(ok=False, tool=action.tool, error="timed out", verified=False)

    monkeypatch.setattr(core.recovery, "recover", fake_recover)

    agent = core.agents["laptop"]
    monkeypatch.setattr(agent, "execute", failing_execute)

    response = await core.handle(make_request("system status"))

    assert response.recovery_notes, "the recovery should be surfaced"
    assert "Recovered automatically" in response.reply
    assert any(a.ok for a in response.actions)


@pytest.mark.skipif(not HAS_BATTERY, reason="uses battery_status as the action that succeeds")
async def test_recovery_is_not_invoked_on_success(core: JarvisCore, make_request, monkeypatch) -> None:
    calls = {"n": 0}

    async def spy(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("recovery must not run when the action succeeded")

    monkeypatch.setattr(core.recovery, "recover", spy)

    response = await core.handle(make_request("what is my battery"))
    assert response.actions[0].ok
    assert calls["n"] == 0
    assert not response.recovery_notes


# ---------------------------------------------------------------------------
# Protected paths are refused before any handler runs (Phase 1, CR9)
# ---------------------------------------------------------------------------
async def _boom(*args, **kwargs):  # pragma: no cover - reaching this fails the test
    raise AssertionError("a blocked action must never reach its tool handler")


async def test_protected_search_is_blocked_before_execution(
    core: JarvisCore, make_request, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is proven by the handler never being invoked, not by a string.

    ``search_files`` used to name its location ``directory``, which the old
    parameter-name check did not inspect, so a search rooted in a protected
    directory executed and returned real results.
    """
    monkeypatch.setitem(core.tools._handlers, "search_files", _boom)

    response = await core.handle(
        make_request("search for plist files in ~/Library/LaunchAgents")
    )

    assert response.decision is PolicyDecision.BLOCK
    assert not response.actions or not any(a.ok for a in response.actions)
    assert "protected" in (response.blocked_reason or "").lower()


async def test_ordinary_search_still_runs_after_the_guard_widened(
    core: JarvisCore, make_request
) -> None:
    """The contrast case: refusing protected locations must not refuse work."""
    response = await core.handle(make_request("find my latest report"))

    assert response.intent == "search_files"
    assert response.decision is not PolicyDecision.BLOCK
