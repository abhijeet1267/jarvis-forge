"""A sentence that asks to erase data must not be answered with a disk report.

Found by the Phase 69 intent corpus, and it is the one guard case in 101 that the pipeline let
run: "empty the trash and wipe free space" reached `EXECUTE`.

The route is `RulePlanner._storage`, which matches the phrase "free space" and returns
`Intent("check_storage", tool="check_storage")`. What it does not look at is the verb of the
sentence -- the person asked for two destructive operations and got a read, because part of the
sentence happened to be a word the read recognises. Measured before the fix:

    decision EXECUTE | intent check_storage | tool check_storage

Nothing dangerous executed -- `check_storage` is a read, and the tier system did its job. The
defect is the answer: a person who reads a disk report immediately after asking for a wipe has no
way to tell whether the wipe was refused or performed. That is the same class of problem Phase 29
fixed from the other direction (a route must fill the action it claims, or ask), and the same one
Phase 61 closed for the computer-use hands.

The fix uses the mechanism that already exists for "this half is not available": an `Intent` with
a note and no tool, which `_to_plan` renders as a decline (`reply_kind="declined"`) with no
action. No gate moved: the destructive phrasing patterns in `security/risk.py` and every risk tier
are untouched, and the read is still reachable by the sentences that actually want it -- which the
last test below is there to prove, because a refusal rule that eats "check storage" would be a
worse bug than the one it fixes.
"""

from __future__ import annotations

import asyncio

import pytest

from jarvis.core.models import CommandRequest, PolicyDecision

WIPE = "empty the trash and wipe free space"


@pytest.fixture()
def registry(core):
    return {spec.name: spec for spec in core.tools.all()}


def test_the_erase_sentence_is_not_routed_to_a_disk_read(core, registry):
    """The router's own answer: no action, and a note that says what was declined."""
    plan = core.planner.parse(WIPE)

    assert plan.actions == [], f"the route still produced {plan.actions[0].tool!r}"
    assert plan.direct_reply, "a declined route has to say so, not fall through to silence"
    for half in ("trash", "space"):
        assert half in plan.direct_reply.lower(), (
            f"the answer does not name the {half} half it refused: {plan.direct_reply!r}")


def test_the_answer_says_nothing_was_done(core):
    """A refusal that a person could read as a completed wipe is the failure being fixed; the
    sentence has to carry its own "nothing happened"."""
    plan = core.planner.parse(WIPE)

    reply = plan.direct_reply.lower()
    assert "did nothing" in reply or "nothing was" in reply or "not run" in reply, reply


def test_the_pipeline_does_not_execute_the_erase_sentence(core):
    """The measured failure was `decision EXECUTE`. The route is not enough -- the whole request
    must stop before a handler is reached."""
    response = asyncio.run(core.handle(CommandRequest(
        text=WIPE, source="cli", session_id="phase69-erase")))

    assert response.decision is not PolicyDecision.EXECUTE, response.decision
    assert response.plan is not None and response.plan.actions == []
    assert response.reply, "an empty reply would leave the person guessing"


def test_the_ledger_records_the_refusal_as_a_non_action(core):
    """`reply_kind` is what keeps a refusal distinguishable from a question in the chain (19-B), so
    the decline has to reach the audit row, not only the screen."""
    before = core.db.query("SELECT COALESCE(MAX(rowid),0) AS m FROM action_logs")[0]["m"]
    asyncio.run(core.handle(CommandRequest(text=WIPE, source="cli",
                                           session_id="phase69-erase-audit")))
    rows = core.db.query("SELECT result,detail FROM action_logs WHERE rowid > ? ORDER BY rowid",
                         (before,))

    assert rows, "the refusal left no trace"
    assert rows[-1]["result"] == "no_action", rows[-1]["result"]
    assert "declined" in (rows[-1]["detail"] or ""), rows[-1]["detail"]


@pytest.mark.parametrize("say", [
    "check storage",
    "is my disk full",
    "disk usage",
    "what is my storage",
])
def test_the_sentences_that_really_do_want_a_disk_report_still_get_one(core, say):
    """The over-correction check. A refusal rule that also eats the harmless wording is not a fix,
    and the corpus only caught the original bug because these sentences were in it too."""
    plan = core.planner.parse(say)

    assert plan.actions, f"{say!r} now reaches no tool at all"
    assert plan.actions[0].tool == "check_storage", (say, plan.actions[0].tool)


def test_the_how_much_space_wording_now_routes_and_the_gap_it_recorded_is_closed(core):
    """This sentence used to reach nothing: the storage trigger wanted `space free` or `space left`
    and the wording is `space is free`. Phase 69 recorded it as 69-D with a test asserting the gap
    was still there; Phase 70 closed it by widening that trigger, so the assertion is inverted here
    rather than deleted -- a test that used to prove a defect is the cheapest record of when it was
    fixed."""
    plan = core.planner.parse("how much space is free")

    assert plan.actions and plan.actions[0].tool == "check_storage", plan.direct_reply


def test_a_deletion_question_about_space_is_still_a_read(core):
    """"How much disk space is free before I delete some photos" wants the number in order to
    decide. The erase verb targets files the person names, not the storage, so the decline rule
    must not swallow it."""
    plan = core.planner.parse("how much disk space is free before i delete some photos")

    assert plan.actions and plan.actions[0].tool == "check_storage", plan.direct_reply
