"""Phase 12: the typed-sentence route to real filesystem work.

Phase 11 proved `write_file` executes for real, but the planner had no route, so
the capability was unreachable from anything a user types. These tests drive the
same objects `/command` drives -- real planner, real permission engine, real
confirmation, real disk -- and assert the safety properties the new route must
not quietly give back.

Every filesystem assertion reads the file from outside JARVIS.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from jarvis.core.models import PlannedAction, PolicyDecision, Verification
from jarvis.core.orchestrator import JarvisCore
from jarvis.tools.base import unverified


@pytest.fixture()
def workspace(tmp_path, monkeypatch) -> Path:
    """A real sandbox, with the workspace setting pointed at it."""
    box = tmp_path / "JARVIS_REAL_TEST"
    (box / "nested").mkdir(parents=True)
    from jarvis.config import get_settings

    monkeypatch.setattr(get_settings(), "file_workspace", str(box))
    return box


def _plan(core: JarvisCore, sentence: str):
    return core.planner.parse(sentence)


# ------------------------------------------------------------------ routing


def test_typed_create_sentence_routes_to_write_file(core, workspace):
    plan = _plan(core, "Create a file called hello.txt containing: JARVIS REAL TEST")

    assert plan.intent == "write_file", plan.intent
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.tool == "write_file"
    assert action.params["content"] == "JARVIS REAL TEST\n"
    # anchored inside the workspace, not the process working directory
    assert action.params["path"] == str(workspace / "hello.txt")


def test_qualifier_variants_route(core, workspace):
    for sentence in (
        "create a new file called notes.txt containing buy milk",
        "Create the file called log.txt with hello world",
        "save a text file named out.txt containing: data",
    ):
        plan = _plan(core, sentence)
        assert plan.intent == "write_file", sentence
        assert plan.actions[0].params["mode"] == "overwrite"


def test_append_sentence_routes_with_append_mode(core, workspace):
    plan = _plan(core, "Append this line to hello.txt: REAL END-TO-END VERIFIED")

    assert plan.intent == "append_file"
    params = plan.actions[0].params
    assert params["mode"] == "append"
    assert params["content"] == "REAL END-TO-END VERIFIED\n"
    assert params["path"] == str(workspace / "hello.txt")


def test_unsupported_phrasing_reaches_no_tool(core, workspace):
    for sentence in ("Do something completely unsupported.", "Xqzw prlm npto."):
        plan = _plan(core, sentence)
        assert plan.actions == [], sentence
        assert plan.intent == "unknown", sentence


# ------------------------------------------------------------- path safety


def test_relative_traversal_is_refused_by_the_route(core, workspace):
    plan = _plan(core, "Create a file called ../escape.txt containing: X")

    assert plan.actions == [], "a relative traversal produced an executable action"
    assert "outside my file workspace" in plan.direct_reply
    assert not (workspace.parent / "escape.txt").exists()


def test_absolute_protected_path_is_refused(core, workspace):
    plan = _plan(core, "Create a file called /etc/hosts containing: X")

    assert plan.actions == []
    assert Path("/etc/hosts").read_text() != "X"


def test_absolute_home_dotfile_is_refused(core, workspace):
    plan = _plan(core, f"Create a file called {Path.home()}/.ssh/id_rsa containing: X")
    assert plan.actions == []


def test_symlink_escape_is_refused(core, workspace):
    link = workspace / "sshlink"
    try:
        link.symlink_to(Path.home() / ".ssh")
    except OSError:
        pytest.skip("cannot create a symlink in this environment")

    plan = _plan(core, "Create a file called sshlink/id_rsa containing: X")

    assert plan.actions == [], "a symlink out of the workspace produced an action"
    written = Path.home() / ".ssh" / "id_rsa"
    assert not written.exists() or not written.read_text(errors="replace").endswith("X")


def test_nested_path_inside_workspace_is_allowed(core, workspace):
    plan = _plan(core, "Create a file called nested/example.txt containing: deep")

    assert plan.actions[0].params["path"] == str(workspace / "nested" / "example.txt")


def test_route_still_goes_through_the_permission_engine(core, workspace):
    """The route must not become a bypass: the action is still confirm-gated."""
    action = _plan(core, "Create a file called hello.txt containing: X").actions[0]

    verdict = core.policy.evaluate(
        PlannedAction(step_id=1, tool=action.tool, params=action.params,
                      device="laptop_main"))

    assert verdict.decision is PolicyDecision.CONFIRM, verdict.reason
    assert verdict.confirmation_id, "no confirmation was created"


# ------------------------------------------------------------- real execution


def test_full_pipeline_creates_a_real_file_and_verifies_it(core, workspace):
    """request -> permission -> confirmation -> tool -> disk -> verification."""
    sentence = "Create a file called hello.txt containing: JARVIS REAL TEST"
    from jarvis.core.models import CommandRequest

    first = asyncio.run(core.handle(CommandRequest(text=sentence, source="test")))
    assert first.needs_confirmation
    victim = workspace / "hello.txt"
    assert not victim.exists(), "the file was created before the user confirmed"

    second = asyncio.run(
        core.resolve_confirmation(first.confirmation_id, True, session_id="laptop"))

    assert second.actions, second.reply
    result = second.actions[0]
    assert result.verification is Verification.SUCCESS
    assert result.verified is True

    # independent read, outside JARVIS
    assert victim.read_text() == "JARVIS REAL TEST\n"
    assert len(victim.read_bytes()) == 17


def test_append_then_read_back_through_the_pipeline(core, workspace):
    from jarvis.core.models import CommandRequest

    async def say(text):
        response = await core.handle(CommandRequest(text=text, source="test"))
        if response.confirmation_id:
            response = await core.resolve_confirmation(
                response.confirmation_id, True, session_id="laptop")
        return response

    asyncio.run(say("Create a file called hello.txt containing: JARVIS REAL TEST"))
    asyncio.run(say("Append this line to hello.txt: REAL END-TO-END VERIFIED"))

    assert (workspace / "hello.txt").read_text() == (
        "JARVIS REAL TEST\nREAL END-TO-END VERIFIED\n")


def test_audit_records_the_real_action_as_verified(core, workspace):
    from jarvis.core.models import CommandRequest

    response = asyncio.run(core.handle(
        CommandRequest(text="Create a file called auditme.txt containing: X", source="test")))
    asyncio.run(core.resolve_confirmation(
        response.confirmation_id, True, session_id="laptop"))

    rows = core.audit.recent(20)
    written = [r for r in rows if r.get("tool") == "write_file"]
    results = {str(r["result"]) for r in written}
    assert "awaiting_confirmation" in results, results
    assert "success" in results, results


# ---------------------------------------- the third .ok site found in Phase 12


def test_scheduler_does_not_record_unverified_work_as_executed(core):
    """`runner.py` was a third site deciding success from `.ok`."""
    from jarvis.scheduler.runner import SchedulerRunner
    from jarvis.scheduler.store import ScheduleStore

    original = core.tools._handlers["get_battery_status"]
    core.tools._handlers["get_battery_status"] = (
        lambda ctx, **kw: asyncio.sleep(0, result=unverified(
            "get_battery_status", "could not read"))
    )
    try:
        store = ScheduleStore()
        job = store.create(name="p12", schedule_text="every day", action="get_battery_status",
                           params={}, max_risk=3)
        outcome = asyncio.run(SchedulerRunner(store).run_job(core, job))
    finally:
        # `core` is session-scoped: leaving this handler in place poisoned every
        # later battery_status test in the run, which is the shared-state defect
        # Phase 9's PERF-4 flagged and Phase 10 pinned in the revocation suite.
        core.tools._handlers["get_battery_status"] = original

    assert outcome["outcome"] == "unverified", (
        f"an unobserved action was recorded as {outcome['outcome']!r}")
    assert outcome["outcome"] != "executed"


# ---------------------------------------------------------------------------
# input validation (Phase 9 #11): a command is a sentence or a bounded run of
# sentences, never an unbounded paste
# ---------------------------------------------------------------------------

def test_an_empty_command_is_refused():
    import pytest
    from jarvis.core.models import CommandRequest

    with pytest.raises(ValueError, match="must not be empty"):
        CommandRequest(text="   ", source="api")


def test_an_overlong_command_is_refused_with_a_clear_message():
    import pytest
    from jarvis.core.models import CommandRequest, MAX_COMMAND_CHARS

    with pytest.raises(ValueError, match="too long") as excinfo:
        CommandRequest(text="x" * (MAX_COMMAND_CHARS + 1), source="api")
    # The error names the limit so a caller knows how to split the request.
    assert str(MAX_COMMAND_CHARS) in str(excinfo.value)


def test_a_command_at_the_limit_is_accepted():
    from jarvis.core.models import CommandRequest, MAX_COMMAND_CHARS

    # Exactly the limit is a valid (if maximal) command; the bound is inclusive.
    req = CommandRequest(text="x" * MAX_COMMAND_CHARS, source="api")
    assert len(req.text) == MAX_COMMAND_CHARS
