"""Phase 20: a question about a day must be answered about that day.

Measured at the start of the phase. ``RulePlanner._recall`` turned "yesterday" into
a *search string*:

    if "yesterday" in text:
        query = "yesterday work"

so "what did I work on yesterday" asked the memory index for the phrase "yesterday
work" and answered whatever ranked best, from any date, with

    "Here is what I remember: User asked: Jarvis, schedule two hours for project
    work tomorrow.. Result: list_schedules: You have nothing scheduled."

Two untruths in one line. The answer was not scoped to the day that was asked
about, and the item shown was JARVIS's own activity echo, presented as a remembered
fact. ``MemoryItem.created_at`` already existed and was simply not used, not
returned, and not mentioned.

Nothing here redesigns memory. It stops claiming a date-scoped answer that was never
computed, and stops passing an activity echo off as something the user said.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from jarvis.core.models import CommandRequest, MemoryScope
from jarvis.tools.base import ToolContext
from jarvis.tools.memory_tools import search_memory

YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
TODAY = date.today().isoformat()


def _ctx(core) -> ToolContext:
    return ToolContext(settings=core.settings, apps=core.apps, devices=core.devices,
                       memory=core.memory, graph=core.graph, core=core)


@pytest.fixture()
def two_days(core):
    """One memory dated yesterday, one dated today, and their ids for cleanup."""
    from jarvis.memory.store import utcnow

    yesterday = core.memory.remember("phase20 marker YESTERDAY fact", scope=MemoryScope.LONG_TERM)
    today = core.memory.remember("phase20 marker TODAY fact", scope=MemoryScope.LONG_TERM)
    # Back-date one row directly: the point of the test is the date, and nothing
    # else in the system lets a caller invent one.
    back = (date.today() - timedelta(days=1)).isoformat() + "T09:00:00+00:00"
    core.db.execute("UPDATE memories SET created_at = ? WHERE id = ?",
                    (back, yesterday.id))
    try:
        yield {"yesterday": yesterday.id, "today": today.id}
    finally:
        for mid in (yesterday.id, today.id):
            core.db.execute("DELETE FROM memories WHERE id = ?", (mid,))


# ---------------------------------------------------------------------------
# The planner must stop turning a date into a search term.
# ---------------------------------------------------------------------------


def test_yesterday_becomes_a_window_not_a_query(core):
    plan = core.planner.parse("what did I work on yesterday?")

    assert plan.actions[0].tool == "search_memory"
    params = plan.actions[0].params
    assert params.get("query") != "yesterday work", (
        "the date is still being searched for as text instead of filtered on")
    assert params.get("since") == YESTERDAY and params.get("until") == YESTERDAY, params


def test_a_recall_without_a_date_stays_unscoped(core):
    params = core.planner.parse("what did I work on recently?").actions[0].params

    assert params.get("since", "") == "" and params.get("until", "") == ""
    assert params.get("query")


# ---------------------------------------------------------------------------
# The tool must answer about the window it was given.
# ---------------------------------------------------------------------------


async def test_windowed_recall_returns_only_that_window(core, two_days):
    result = await search_memory(_ctx(core), query="phase20 marker",
                                 since=YESTERDAY, until=YESTERDAY)
    contents = [r["content"] for r in result.output["results"]]

    assert any("YESTERDAY" in c for c in contents)
    assert not any("TODAY" in c for c in contents), "the window was not applied"
    assert result.output["window"] == {"since": YESTERDAY, "until": YESTERDAY}


async def test_windowed_recall_says_which_day_it_answered(core, two_days):
    result = await search_memory(_ctx(core), query="phase20 marker",
                                 since=YESTERDAY, until=YESTERDAY)

    assert YESTERDAY in result.message or "yesterday" in result.message.lower()


async def test_an_empty_window_is_reported_as_empty_not_as_a_misspelling(core):
    result = await search_memory(_ctx(core), query="phase20 nothing stored here",
                                 since=YESTERDAY, until=YESTERDAY)

    assert result.output["window"]["since"] == YESTERDAY
    assert "phase20 nothing stored here" not in result.message, (
        "an empty day was reported as 'nothing stored about <query>'")
    assert result.output["results"] == []


async def test_recall_reports_each_item_when_it_was_recorded(core, two_days):
    result = await search_memory(_ctx(core), query="phase20 marker")

    for item in result.output["results"]:
        assert item.get("created_at"), item


# ---------------------------------------------------------------------------
# An activity echo is not something the user said.
# ---------------------------------------------------------------------------


async def test_an_echo_is_labelled_as_an_echo(core, two_days):
    echo = core.memory.remember("User asked: phase20 test. Result: get_time: 12:00.",
                                scope=MemoryScope.SESSION, source="orchestrator")
    try:
        result = await search_memory(_ctx(core), query="phase20 test echo")
        hit = [r for r in result.output["results"] if r["id"] == echo.id]
        assert hit, "the echo was not retrieved at all"
        assert hit[0]["is_echo"] is True

        fact = await search_memory(_ctx(core), query="phase20 marker YESTERDAY")
        mine = [r for r in fact.output["results"] if r["id"] == two_days["yesterday"]]
        assert mine, "the fixture fact was not retrieved"
        assert mine[0]["is_echo"] is False
        # Not asserted about *every* result: this store is session-scoped and shared,
        # and by now it is full of other tests' activity echoes -- which is precisely
        # the pollution 15-E describes, not something this test should paper over.
    finally:
        core.db.execute("DELETE FROM memories WHERE id = ?", (echo.id,))


async def test_the_top_answer_announces_that_it_is_an_echo(core, two_days):
    echo = core.memory.remember("User asked: phase20 loud echo. Result: list_apps: 8 apps.",
                                scope=MemoryScope.SESSION, source="orchestrator")
    try:
        result = await search_memory(_ctx(core), query="phase20 loud echo")
        assert result.output["top_is_echo"] is True
        assert "activity" in result.message.lower() or "not something you told me" in result.message
    finally:
        core.db.execute("DELETE FROM memories WHERE id = ?", (echo.id,))


# ---------------------------------------------------------------------------
# Through the pipeline, on the day that was asked about.
# ---------------------------------------------------------------------------


async def test_the_spoken_question_is_answered_about_the_day_asked(core, two_days):
    response = await core.handle(
        CommandRequest(text="Jarvis, what did I work on yesterday?", source="test"))
    payload = json.loads(response.model_dump_json())
    action = payload["actions"][0]

    assert action["output"]["window"]["since"] == YESTERDAY
    for item in action["output"]["results"]:
        assert item["created_at"].startswith(YESTERDAY), item


async def test_a_read_of_the_store_is_still_a_successful_read(core, two_days):
    """Phase 18's rule survives: this is a read that genuinely happened."""
    result = await search_memory(_ctx(core), query="phase20 marker",
                                 since=YESTERDAY, until=YESTERDAY)

    assert result.verification.value == "success"
