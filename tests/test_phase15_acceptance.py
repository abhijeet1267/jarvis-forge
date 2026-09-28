"""Phase 15: the plan's own acceptance list must not report success for nothing.

Section 59 of ``JARVIS_Forge_Full_Working_Plan.md`` says to use those eighteen
commands as the acceptance test. Run live against the HTTP pipeline, five of them
answered "I could not read it" / "nothing was changed" while the machine-readable
verification said ``success`` -- which the dashboard renders as a VERIFIED chip
directly above the words refusing it. One more routed a request to *create* a
schedule into a request to *list* schedules, succeeded, and told the user nothing
had been created.

These tests pin the repaired state. ``.ok`` deliberately stays true on the
affected reads: the handler did complete and report honestly. What changed is that
the verification no longer claims the world was read or altered.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from jarvis.core.models import CommandRequest, Verification
from jarvis.home import HomeGateway, reset_home_gateway
from jarvis.tools.car import get_car_telemetry
from jarvis.tools.system import get_location_summary

PAGE = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(not node, reason="node not available to run the page's code")


def _ctx(core):
    from jarvis.tools.base import ToolContext

    return ToolContext(settings=core.settings, apps=core.apps, devices=core.devices,
                       memory=core.memory, graph=core.graph, core=core)


@pytest.fixture(scope="module")
def state_of(tmp_path_factory):
    """Evaluate the shipped dashboard's own pipelineState() against a payload."""
    body = re.search(r"<script>(.*?)</script>", PAGE.read_text(), re.S).group(1)
    extracted = body[body.index("const STATE_TONE"):body.index("function renderState")]
    extracted = extracted.replace("const STATE_TONE", "var STATE_TONE", 1)
    assert "function pipelineState" in extracted

    root = tmp_path_factory.mktemp("p15")
    lib = root / "page.js"

    def _run(payload) -> str:
        lib.write_text(
            "const s = " + json.dumps(extracted) + ";\n"
            "eval(s);\n"
            "console.log(pipelineState(JSON.parse(process.argv[2])));\n"
        )
        proc = subprocess.run(
            [node, str(lib), json.dumps(payload, default=str)],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr[:300]
        return proc.stdout.strip()

    return _run


def _payload(response) -> dict:
    return json.loads(response.model_dump_json())


@pytest.fixture()
def car_offline():
    """A guaranteed-unconfigured car gateway, dropped again afterwards.

    Reading it builds the process-wide singleton and marks it ``checked``, which
    leaks into /health for whatever module runs next.
    """
    from jarvis.car.gateway import reset_car_gateway

    reset_car_gateway(None)
    yield
    reset_car_gateway(None)


# ---------------------------------------------------------------------------
# 15-A -- "could not read" is not a success
# ---------------------------------------------------------------------------


async def test_car_telemetry_offline_is_not_verified(core, car_offline):
    """Commands 14 and 15 of the acceptance list.

    Live sweep before the fix: reply "I could not read those car values (no car
    adapter or vehicle API is configured)" with verification=success, audited as
    ``success``, rendered VERIFIED.
    """
    result = await get_car_telemetry(_ctx(core), metric="fuel")

    assert result.ok is True, "the handler did complete -- that part was accurate"
    assert result.verification is Verification.FAILED
    assert result.output["provider_status"] == "unavailable"
    assert "could not read" in result.message


async def test_location_summary_without_a_source_is_not_verified(core):
    result = await get_location_summary(_ctx(core), date="today")

    assert result.ok is True
    assert result.verification is Verification.FAILED
    assert result.output["provider_status"] == "unavailable"


async def test_home_routine_offline_changed_nothing_and_says_so(core):
    """Command 17: the reply literally ends "Nothing was changed"."""
    reset_home_gateway(HomeGateway())
    try:
        result = await _run_routine(core)
    finally:
        reset_home_gateway(None)

    assert result.ok is True
    assert result.verification is Verification.FAILED
    assert result.output["offline"] is True
    assert result.output["applied"] == 0
    assert "nothing was changed" in result.message.lower()


async def _run_routine(core):
    from jarvis.tools.home import run_home_routine

    return await run_home_routine(_ctx(core), routine="study")


@pytest.mark.parametrize("sentence, refusal_word", [
    ("Jarvis, check my car fuel.", "could not read"),
    ("Jarvis, give me today's location summary.", "not available"),
    ("Jarvis, turn on study mode.", "nothing was changed"),
])
async def test_refusing_reply_never_carries_a_verified_state(core, car_offline, sentence,
                                                            refusal_word, state_of):
    """The whole defect in one property, through the real pipeline.

    Not a per-tool assertion: whatever the tool reports, a reply that refuses may
    not arrive at the renderer as verified work.
    """
    response = await core.handle(CommandRequest(text=sentence, source="test"))
    payload = _payload(response)

    assert payload["actions"], f"{sentence!r} produced no action to judge"
    assert refusal_word in payload["reply"].lower(), (
        f"the reply no longer refuses, so this test is stale: {payload['reply']!r}")
    assert all(a["verification"] != "success" for a in payload["actions"])
    assert state_of(payload) != "VERIFIED"
    assert state_of(payload) == "UNAVAILABLE"


async def test_audit_records_these_as_failures_not_success(core):
    """The ledger is the durable truth; the UI is only its copy."""
    before = core.audit.db.query(
        "SELECT COUNT(*) AS n FROM action_logs WHERE tool = 'get_location_summary' "
        "AND result = 'success'"
    )[0]["n"]

    await core.handle(CommandRequest(text="Jarvis, give me today's location summary.",
                                     source="test"))

    after = core.audit.db.query(
        "SELECT COUNT(*) AS n FROM action_logs WHERE tool = 'get_location_summary' "
        "AND result = 'success'"
    )[0]["n"]
    assert after == before, "a read that produced nothing was audited as success again"


# ---------------------------------------------------------------------------
# 15-B -- "schedule X" is a request to create, not a request to list
# ---------------------------------------------------------------------------


def test_schedule_two_hours_tomorrow_is_not_answered_by_listing(core):
    """Command 7, verbatim from the acceptance list.

    Before: routed to ``list_schedules``, replied "You have nothing scheduled",
    verification success -- a VERIFIED chip for a schedule that was never made.
    """
    plan = core.planner.parse("Jarvis, schedule two hours for project work tomorrow.")

    assert not [a for a in plan.actions if a.tool == "list_schedules"], (
        "a creation request was answered by listing existing schedules")
    assert plan.needs_clarification or plan.direct_reply, (
        "the sentence should either schedule something or ask when, not silently succeed")


async def test_schedule_two_hours_tomorrow_reports_no_invented_success(core, state_of):
    def count() -> int:
        return core.db.query("SELECT COUNT(*) AS n FROM scheduled_jobs")[0]["n"]

    before = count()
    response = await core.handle(
        CommandRequest(text="Jarvis, schedule two hours for project work tomorrow.", source="test"))
    payload = _payload(response)

    assert not payload["actions"], "something was reported as executed"
    # Phase 24: it asks what the reminder should say, so the honest state is a
    # question rather than a refusal. "No invented success" is what this test is for.
    assert state_of(payload) == "NEEDS DETAIL"
    assert state_of(payload) != "VERIFIED"
    # And nothing was filed. Counted as a delta, not an absolute: ``core`` is
    # session-scoped and other modules schedule real jobs.
    assert count() == before, "a schedule was created by a request that only asked when"


@pytest.mark.parametrize("sentence, tool", [
    ("what do I have scheduled?", "list_schedules"),
    ("cancel my schedule SCH-abcd1234", "cancel_schedule"),
    ("every morning at 7 give me my briefing", "schedule_task"),
    ("remind me to file the AMR review at 5 PM", "schedule_task"),
])
def test_the_other_schedule_readings_still_route(core, sentence, tool):
    """Guard against over-correcting: listing and cancelling are still listing."""
    actions = core.planner.parse(sentence).actions
    assert actions and actions[0].tool == tool, f"{sentence!r} -> {[a.tool for a in actions]}"


def test_a_bare_time_is_not_filed_as_a_nameless_reminder(core):
    """"schedule <something> tomorrow at 3pm" must not invent a job's content."""
    plan = core.planner.parse("Jarvis, schedule two hours tomorrow at 3pm please.")
    tools = [a.tool for a in plan.actions]

    assert "schedule_task" not in tools or plan.needs_clarification or plan.direct_reply, (
        f"a job with no stated content was filed: {tools}")


# ---------------------------------------------------------------------------
# 15-C -- an honest refusal must not render as UNKNOWN either
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sentence, expected", [
    # A refusal the system chose, stated plainly: a known outcome, not an
    # unknowable one. Five of the eighteen acceptance commands landed here.
    #
    # "Jarvis, send it." is deliberately absent: whether it refuses, asks, or runs
    # depends on whether a draft is waiting, which is world state this session-scoped
    # core shares with every other module. Phase 24 pins it deterministically by
    # removing the draft instead of hoping the ordering agrees.
    #
    # The schedule row moved to NEEDS DETAIL in Phase 24: the sentence asks what the
    # reminder should say, and 19-B is closed, so it is no longer labelled as a refusal.
    ("Jarvis, transfer this file to my phone.", "NOTHING EXECUTED"),
    ("Jarvis, what is my phone battery?", "NOTHING EXECUTED"),
    ("Jarvis, schedule two hours for project work tomorrow.", "NEEDS DETAIL"),
])
async def test_refusals_and_clarifications_have_their_own_state(core, sentence, expected, state_of):
    response = await core.handle(CommandRequest(text=sentence, source="test"))
    payload = _payload(response)
    assert state_of(payload) == expected, payload["reply"][:120]


async def test_a_question_is_not_guessed_at_from_reply_wording(core, state_of):
    """The renderer reads a field; it never reads the punctuation.

    This test was written in Phase 19 to record a limitation -- "separating them needs
    a field the backend does not have yet". Phase 24 added that field
    (``plan.reply_kind = "clarification"`` plus ``needs_clarification``), so the
    property being pinned is now the stronger one: the distinction comes from the
    backend's own word, and a question mark in a *statement* still cannot promote it.
    """
    response = await core.handle(
        CommandRequest(text="Jarvis, schedule two hours for project work tomorrow.", source="test"))
    payload = _payload(response)
    plan = payload["plan"]

    assert "?" in plan["direct_reply"], payload["reply"][:120]
    assert plan["needs_clarification"] is True
    assert plan["reply_kind"] == "clarification"
    assert state_of(payload) == "NEEDS DETAIL"

    # ...and the other direction: an unavailable capability states, it does not ask.
    statement = _payload(await core.handle(
        CommandRequest(text="Jarvis, what is my phone battery?", source="test")))
    assert statement["plan"]["needs_clarification"] is False, (
        "a statement of what is missing was promoted to a question the user can answer")
    assert state_of(statement) == "NOTHING EXECUTED"


async def test_unknown_route_still_reads_as_unknown_not_something_executed(core, state_of):
    response = await core.handle(CommandRequest(text="Jarvis, design this system.", source="test"))
    payload = _payload(response)

    state = state_of(payload)
    # Phase 19 gave the planner a word for this case, so the page no longer has to
    # fall back to "unknown". The invariant this test exists for is unchanged: a
    # command nothing recognised can never render as verified work.
    assert state in ("UNKNOWN", "NOTHING EXECUTED", "NOT RECOGNISED"), state
    assert state != "VERIFIED"


def test_phase13_invariants_still_hold(state_of):
    """Nothing here may turn an absent answer into a claimed one."""
    assert state_of({"decision": "EXECUTE", "actions": []}) == "UNKNOWN"
    assert state_of({"verified": True, "status": "VERIFIED", "actions": []}) == "UNKNOWN"
    assert state_of({"ok": True, "reply": "success", "actions": []}) == "UNKNOWN"


def test_page_ships_the_two_new_states():
    source = PAGE.read_text()
    assert "NOTHING EXECUTED" in source and "NEEDS DETAIL" in source
    assert "plan.needs_clarification" in source, "the clarification is never read"
    assert "plan.direct_reply" in source, "the refusal is never read"
