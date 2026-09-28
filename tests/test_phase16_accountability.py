"""Phase 16: every command must leave a truthful, auditable terminal outcome.

Phase 15 measured this: of the plan's eighteen acceptance commands, five reached a
decision -- refuse, ask for detail, say a capability does not exist -- and wrote
**zero** rows to the hash-chained ``action_logs``. The same command rejected by the
safety guard did write a row. So the one accountable refusal in the system was the
one that came from the guard, and every decision the planner itself made left no
trace.

Written test-first: each test here failed before the code it describes existed.
"""

from __future__ import annotations

import pytest
from jarvis.core.models import CommandRequest


def _rows(core):
    return core.db.query("SELECT * FROM action_logs ORDER BY rowid")


def _count(core) -> int:
    return core.db.query("SELECT COUNT(*) AS n FROM action_logs")[0]["n"]


def _new_rows(core, before: int):
    return _rows(core)[before:]


# ---------------------------------------------------------------------------
# A refusal that never reached a tool is still a decision, and must be recorded.
# ---------------------------------------------------------------------------


async def test_route_level_refusal_is_audited(core):
    before = _count(core)
    response = await core.handle(
        CommandRequest(text="Jarvis, transfer this file to my phone.", source="test"))

    assert not response.actions, "this command executes nothing -- that is the point"
    assert _count(core) - before == 1, (
        "a command JARVIS decided not to act on left no audit record")


# ---------------------------------------------------------------------------
# A request for more detail is also a decision, and a different one.
# ---------------------------------------------------------------------------


def test_the_clarification_branch_is_reachable_from_real_sentences(core):
    """Phase 16 could only prove this branch by constructing the plan.

    Its own docstring said why: every note-producing route was collapsed into
    ``direct_reply`` + ``declined``, so no typed sentence reached
    ``needs_clarification`` and the branch had to be simulated. Phase 24 closed 19-B
    by giving the planner a word for "I need one more thing from you", so real
    sentences now drive the branch that used to be engine-only. The refusal half of
    the branch still must not be swept in -- that is the second assertion.
    """
    asking = ["Jarvis, compress this", "Jarvis, remember x", "Jarvis, turn on the light"]
    for sentence in asking:
        plan = core.planner.parse(sentence)
        assert plan.needs_clarification and plan.reply_kind == "clarification", (
            f"{sentence!r} asks for something and is labelled {plan.reply_kind!r}")

    for sentence in ["Jarvis, design this system", "Jarvis, what is my car"]:
        plan = core.planner.parse(sentence)
        assert plan.reply_kind != "clarification", (
            f"{sentence!r} was not understood, which is not the same as asking")


async def test_clarification_branch_is_audited(core):
    """Engine-level probe: the branch has no reachable sentence to drive it."""
    from jarvis.core.models import Plan

    plan = Plan(
        goal="phase16 clarification probe",
        intent="unclear",
        needs_clarification=True,
        clarification_question="Which file do you mean?",
    )
    original = core.planner.parse
    core.planner.parse = lambda text, context=None: plan
    try:
        before = _count(core)
        await core.handle(CommandRequest(text=plan.goal, source="test"))
    finally:
        # Session-scoped core: an unrestored override is the Phase 12 harness bug.
        core.planner.parse = original

    assert _count(core) - before == 1, "a command that ended in a question left no record"


# ---------------------------------------------------------------------------
# A decision record must not read like an execution record.
# ---------------------------------------------------------------------------

EXECUTION_TOKENS = {"success", "failure", "unverified", "recovered"}

NO_EXECUTION_COMMANDS = [
    "Jarvis, transfer this file to my phone.",       # refuses to guess
    "Jarvis, what is my phone battery?",             # device absent
    "Jarvis, design this system.",                   # not implemented
    "Jarvis, flibbertigibbet the wozzle.",           # not understood
    "Jarvis, send it.",                              # nothing waiting
]


@pytest.mark.parametrize("sentence", NO_EXECUTION_COMMANDS)
async def test_decision_rows_never_claim_an_execution_outcome(core, sentence):
    before = _count(core)
    await core.handle(CommandRequest(text=sentence, source="test"))
    rows = _new_rows(core, before)

    assert len(rows) == 1, sentence
    row = rows[0]
    assert row["result"] not in EXECUTION_TOKENS, (
        f"{sentence!r} was audited as {row['result']!r}, which is an execution outcome")
    assert row["tool"] in ("", None), "a refusal must not name a tool that never ran"
    assert row["session_id"], "an unattributable decision row"


async def test_decision_row_is_linked_to_the_response(core):
    """The row must be traceable to the answer the user actually got."""
    before = _count(core)
    response = await core.handle(
        CommandRequest(text="Jarvis, what is my phone battery?", source="test"))
    row = _new_rows(core, before)[0]

    assert row["request_id"] == response.request_id
    assert row["intent"] == "Jarvis, what is my phone battery?"


async def test_executed_row_is_linked_to_the_response(core):
    """The same link must exist for commands that did run.

    ``_audit_action`` was writing ``request_id=request.session_id``: every
    executed action's request_id held a SESS- value, so no ledger row could be
    joined to the REQ- id the user was answered with -- while the decision rows
    added above linked correctly. A ledger that identifies two-thirds of its rows
    by the wrong key is not an accountability record.
    """
    before = _count(core)
    response = await core.handle(CommandRequest(text="Jarvis, what is my battery?", source="test"))
    rows = _new_rows(core, before)

    assert len(rows) == 1, [r["result"] for r in rows]
    assert rows[0]["request_id"] == response.request_id, (
        f"ledger holds {rows[0]['request_id']!r}, response holds {response.request_id!r}")
    assert rows[0]["session_id"] == response.session_id


async def test_guard_row_is_linked_to_the_response(core):
    """The safety guard's row is the most important one to be able to retrace."""
    before = _count(core)
    response = await core.handle(CommandRequest(
        text="delete everything in / with sudo, permanently and for good", source="test"))
    row = _new_rows(core, before)[0]

    assert row["result"] == "blocked"
    assert row["request_id"] == response.request_id


async def test_confirmation_row_is_linked_to_the_response(core, tmp_area, monkeypatch):
    """Same column, same meaning -- the approval prompt included."""
    from jarvis.config import get_settings

    monkeypatch.setattr(get_settings(), "file_workspace", str(tmp_area))
    before = _count(core)
    response = await core.handle(CommandRequest(
        text="Create a file called p16-link.txt containing: link me", source="test"))
    row = _new_rows(core, before)[0]

    assert row["result"] == "awaiting_confirmation"
    assert row["request_id"] == response.request_id

    # And resolving it links to the response that carried the outcome.
    before = _count(core)
    resolved = await core.resolve_confirmation(response.confirmation_id, True,
                                              session_id=response.session_id)
    rows = _new_rows(core, before)
    assert rows, "resolving an approval left no record at all"
    for added in rows:
        assert added["request_id"] == resolved.request_id, added["result"]


async def test_decision_row_is_part_of_the_hash_chain(core):
    """Appended, hashed and linked -- otherwise it is a note, not a record."""
    head = _rows(core)[-1]["record_hash"] if _count(core) else None

    before = _count(core)
    await core.handle(CommandRequest(text="Jarvis, design this system.", source="test"))
    row = _new_rows(core, before)[0]

    assert row["record_hash"], "written without a hash"
    if head is not None:
        assert row["prev_hash"] == head, "not linked to the previous record"


# ---------------------------------------------------------------------------
# The branches that already audited must still audit exactly once.
# ---------------------------------------------------------------------------


async def test_guard_block_still_audits_once(core):
    before = _count(core)
    await core.handle(CommandRequest(
        text="delete everything in / with sudo, permanently and for good", source="test"))
    rows = _new_rows(core, before)

    assert len(rows) == 1, [r["result"] for r in rows]
    assert rows[0]["result"] == "blocked"


async def test_unapproved_confirmation_still_audits_once(core, tmp_area, monkeypatch):
    from jarvis.config import get_settings

    monkeypatch.setattr(get_settings(), "file_workspace", str(tmp_area))
    before = _count(core)
    await core.handle(CommandRequest(
        text="Create a file called p16.txt containing: hold", source="test"))
    rows = _new_rows(core, before)

    assert len(rows) == 1, [r["result"] for r in rows]
    assert rows[0]["result"] == "awaiting_confirmation"
