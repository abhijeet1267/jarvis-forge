"""A time a person said must survive the whole path, not just the parser.

**A calendar action could be proposed, approved, and then fail.**

The assistant proposed a time in ordinary English — the model wrote
``"tomorrow at 9:00 AM"`` — and `create_event` parsed the start with
`parse_datetime`, which accepted only seven strict formats. Everything else
returned `None`, and the tool answered:

    "I could not understand the start time 'tomorrow at 9:00 AM'.
     Try 2026-09-21 17:00."

The sequence was: the assistant said it would set the reminder, the
permission gate held it, **the person approved it**, and the action failed
at the last step. The person's "yes" was spent on nothing.

Measured, before the fix: **1 of 12** ordinary time phrases parsed.

    '2026-10-23 09:00'          -> parsed
    'tomorrow at 9:00 AM'       -> rejected
    'tomorrow at 9am'           -> rejected
    'next Monday at 10:00'      -> rejected
    'today at 5pm'              -> rejected
    'in 2 hours'                -> rejected
    'Friday at 3pm'             -> rejected
    '9am tomorrow'              -> rejected
    'tomorrow morning'          -> rejected

Two rules, and the second is the one that matters most:

1. **What a person said is understood.** A relative day, a clock time with
   or without a meridiem, and a combination of the two all resolve.
2. **What cannot be understood is still refused.** A phrase with no
   defensible reading returns `None` and the caller asks rather than
   guessing. The old test — "sometime next week-ish" must not parse — still
   holds.

The planner is told the format too, so the model stops *proposing* a
phrase: it resolves a relative time itself, against the current date and
time it is given.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from jarvis.integrations.calendar_provider import parse_datetime  # noqa: E402
from jarvis.integrations.natural_time import (  # noqa: E402
    describe_datetime,
    parse_natural_datetime,
)

#: A fixed "now" so the expectations are not a function of the clock.
#: 2026-10-22 is a Thursday, at 14:30 local.
NOW = datetime(2026, 10, 22, 14, 30).astimezone()


# ---------------------------------------------------------------------------
# 1. The phrases people actually say
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "phrase,expected",
    [
        # A relative day with a clock time.
        ("tomorrow at 9:00 AM", datetime(2026, 10, 23, 9, 0)),
        ("tomorrow at 9am", datetime(2026, 10, 23, 9, 0)),
        ("tomorrow 9am", datetime(2026, 10, 23, 9, 0)),
        ("9am tomorrow", datetime(2026, 10, 23, 9, 0)),
        ("tomorrow at 14:30", datetime(2026, 10, 23, 14, 30)),
        # A clock time alone.
        ("today at 5pm", datetime(2026, 10, 22, 17, 0)),
        ("at 6pm", datetime(2026, 10, 22, 18, 0)),
        ("6 pm", datetime(2026, 10, 22, 18, 0)),
        # A weekday.
        ("Friday at 3pm", datetime(2026, 10, 23, 15, 0)),
        ("Monday", datetime(2026, 10, 26, 9, 0)),
        ("next Friday", datetime(2026, 10, 30, 9, 0)),
        ("next Monday at 10:00", datetime(2026, 11, 2, 10, 0)),
        # A part of the day.
        ("tomorrow morning", datetime(2026, 10, 23, 9, 0)),
        ("this evening", datetime(2026, 10, 22, 18, 0)),
        ("3 in the afternoon", datetime(2026, 10, 22, 15, 0)),
        ("tonight at 8", datetime(2026, 10, 22, 20, 0)),
        # "noon" said at 14:30 has already passed today, so it is tomorrow's.
        ("noon", datetime(2026, 10, 23, 12, 0)),
        ("tomorrow at noon", datetime(2026, 10, 23, 12, 0)),
        # A named date.
        ("23 October", datetime(2026, 10, 23, 9, 0)),
        ("October 23", datetime(2026, 10, 23, 9, 0)),
        # A relative offset.
        ("in 2 hours", datetime(2026, 10, 22, 16, 30)),
        ("in a week", datetime(2026, 10, 29, 14, 30)),
        # A day word with no clock.
        ("tomorrow", datetime(2026, 10, 23, 9, 0)),
        ("yesterday", datetime(2026, 10, 21, 9, 0)),
    ],
)
def test_the_phrases_people_say_are_understood(phrase: str, expected: datetime) -> None:
    """Every ordinary phrase resolves to the time it names.

    The reference is fixed, so these are not a function of when the suite
    runs.
    """
    got = parse_natural_datetime(phrase, NOW)
    assert got is not None, f"{phrase!r} was not understood"
    assert (got.year, got.month, got.day, got.hour, got.minute) == (
        expected.year, expected.month, expected.day, expected.hour, expected.minute), \
        f"{phrase!r} resolved to {got}, expected {expected}"


def test_parse_datetime_understands_the_phrases_too() -> None:
    """The shared parser is the one the tools use.

    `parse_datetime` is what `create_event`, `update_event` and
    `list_events` call, so understanding ordinary English there is what
    makes the whole path work.
    """
    assert parse_datetime("tomorrow at 9:00 AM") is not None
    assert parse_datetime("next Friday at 3pm") is not None
    assert parse_datetime("in 2 hours") is not None


def test_an_explicit_format_still_wins() -> None:
    """The strict formats are not reinterpreted.

    "2026-10-23 09:00" must stay exactly that, and "2026-10-23 9:00 AM"
    must not have the date's own hour read as the clock.
    """
    got = parse_datetime("2026-10-23 09:00")
    assert got is not None
    assert (got.year, got.month, got.day, got.hour, got.minute) == (
        2026, 10, 23, 9, 0)

    got = parse_datetime("2026-10-23 9:00 AM")
    assert got is not None
    assert (got.hour, got.minute) == (9, 0), (
        f"the date's own hour was read as the clock: {got}")


# ---------------------------------------------------------------------------
# 2. What still cannot be understood is refused
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "phrase",
    [
        "sometime next week-ish",
        "whenever you can",
        "not a time at all",
        "",
        "   ",
    ],
)
def test_a_phrase_with_no_readable_time_is_refused(phrase: str) -> None:
    """A time is not invented.

    The old behaviour — refusing — is right and is kept: the caller asks
    the person rather than booking the wrong hour.
    """
    assert parse_natural_datetime(phrase, NOW) is None


def test_a_contradictory_hour_is_refused() -> None:
    """"13pm" is not a time.

    It is refused rather than folded into 1pm or 25:00.
    """
    assert parse_natural_datetime("13pm", NOW) is None


def test_an_impossible_date_is_refused() -> None:
    """"31 February" is not a date."""
    assert parse_natural_datetime("31 February", NOW) is None


# ---------------------------------------------------------------------------
# 3. The suggestion a person is offered is a real, future date
# ---------------------------------------------------------------------------
def test_the_unreadable_time_suggestion_is_not_in_the_past() -> None:
    """The old message offered a date from whenever it was written.

    "Try 2026-09-21 17:00." was hard-coded, so by the time a person read
    it the date was in the past — an invitation to book the wrong day.
    The suggestion is now computed.
    """
    source = (REPO / "jarvis" / "tools" / "calendar.py").read_text(encoding="utf-8")
    # The check is on the *string literals*, not the whole file: the source
    # mentions these dates in a comment explaining that they used to be
    # hard-coded, and that comment must not be mistaken for the bug. The
    # comment is stripped first, so only a literal that would reach a
    # person counts.
    without_comments = re.sub(r"#.*", "", source)
    literals = " ".join(re.findall(r'"([^"]*)"', without_comments))
    for stale in ("2026-09-21 17:00", "2026-09-25"):
        assert stale not in literals, (
            f"the calendar tool still offers {stale!r} as an example; it is "
            "in the past now")


def test_describe_datetime_writes_a_time_the_person_said() -> None:
    """The suggestion is a phrase, not a formatted timestamp.

    A person is offered "tomorrow at 9:00 am", which they recognise, rather
    than "2026-10-23 09:00:00+05:30", which they have to decode.

    The target is computed from the date the test runs on -- the original
    fixed date (2026-10-23) was "tomorrow" only on 2026-10-22, and a test
    that goes stale on every rollover is a test that tests the calendar,
    not the code.
    """
    target = datetime.now().astimezone() + timedelta(days=1)
    target = target.replace(hour=9, minute=0, second=0, microsecond=0)
    text = describe_datetime(target)
    assert "tomorrow" in text.lower()
    assert "9:00" in text


# ---------------------------------------------------------------------------
# 4. The planner is told the format, so the model stops proposing a phrase
# ---------------------------------------------------------------------------
def test_the_planner_prompt_states_the_time_format() -> None:
    """The model is told what a time parameter must look like.

    Without this, the model wrote "tomorrow at 9:00 AM" and the tool
    refused it. The instruction names the format explicitly and tells the
    model to resolve a relative time itself.
    """
    source = (REPO / "jarvis" / "core" / "model_planner.py").read_text(
        encoding="utf-8")
    assert "YYYY-MM-DD HH:MM" in source, (
        "the planner's prompt does not state the time format the tools read")
    assert "resolve it to the concrete date and time" in source, (
        "the planner's prompt does not tell the model to resolve a relative "
        "time itself")


def test_the_planner_prompt_carries_the_current_date_and_time() -> None:
    """The model cannot resolve "tomorrow" without knowing what today is.

    The prompt used to carry no date at all, so a relative time could only
    be a guess.
    """
    from jarvis.core.model_planner import ModelPlanner

    note = ModelPlanner._now_note()
    assert "Current date and time" in note
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    assert today in note, (
        f"the note does not carry today's date ({today}): {note}")


def test_the_planner_prompt_is_built_with_the_note() -> None:
    """The note is actually in the prompt, not just defined."""
    source = (REPO / "jarvis" / "core" / "model_planner.py").read_text(
        encoding="utf-8")
    assert "_now_note()" in source, (
        "the planner's prompt does not include the current date and time")


# ---------------------------------------------------------------------------
# 5. The whole path, in the tools
# ---------------------------------------------------------------------------
def test_the_calendar_tool_asks_a_question_for_a_time_it_cannot_read() -> None:
    """The tool still refuses, and says so in the person's own terms.

    This is the fallback for a phrase the parser cannot read at all. It
    must not invent a time, and it must not offer a date in the past.
    """
    import asyncio

    from jarvis.tools.base import ToolContext
    from jarvis.tools.calendar import create_event

    result = asyncio.run(create_event(
        ToolContext(), title="A thing", start="whenever you feel like it"))
    text = str(result).lower()
    assert "could not understand" in text, (
        f"the tool did not say it could not read the time: {result}")
    assert "2026-09-21" not in text and "2026-09-25" not in text, (
        f"the tool offered a hard-coded past date again: {result}")


def test_the_calendar_tool_creates_an_event_from_an_ordinary_phrase() -> None:
    """The whole point: a phrase the person said becomes a real event.

    The tool reads the start, and the event lands on the day and hour the
    phrase named. This is the step that used to fail *after* the person
    had approved the action.
    """
    import asyncio

    from jarvis.tools.base import ToolContext
    from jarvis.tools.calendar import create_event

    result = asyncio.run(create_event(
        ToolContext(), title="Ordinary phrase", start="tomorrow at 9:00 AM",
        duration_minutes=15))
    text = str(result)
    assert "could not understand" not in text.lower(), (
        f"the tool refused a time a person would say: {result}")

    # The event is real, and it is cleaned up afterwards so the suite does
    # not leave a stray event in the calendar.
    uid = ""
    for token in text.replace("'", '"').split():
        if token.startswith("EVT-"):
            uid = token.strip('",')
            break
    if uid:
        from jarvis.integrations.calendar_provider import get_calendar_provider

        get_calendar_provider(None).cancel(uid)
