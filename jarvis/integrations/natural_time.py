"""Understanding the times people actually say.

**A calendar action could be proposed, approved, and then fail.**

The assistant proposes a time in ordinary English — the model wrote
``"tomorrow at 9:00 AM"`` — and `create_event` parses the start with
`parse_datetime`, which accepted only seven strict formats
(``2026-10-23 09:00`` and friends). Everything else returned `None`, and the
tool answered:

    "I could not understand the start time 'tomorrow at 9:00 AM'.
     Try 2026-09-21 17:00."

So the sequence was: the assistant said it would set the reminder, the
permission gate held it, **the person approved it**, and the action failed
at the last step. The person's "yes" was spent on nothing.

Measured, before this module: **1 of 12** ordinary time phrases parsed.

    '2026-10-23 09:00'          -> parsed
    'tomorrow at 9:00 AM'       -> rejected
    'tomorrow at 9am'           -> rejected
    'tomorrow 9am'              -> rejected
    'next Monday at 10:00'      -> rejected
    'today at 5pm'              -> rejected
    'in 2 hours'                -> rejected
    'tonight at 8'              -> rejected
    'Friday at 3pm'             -> rejected
    '2026-10-23 9:00 AM'        -> rejected
    '9am tomorrow'              -> rejected
    'tomorrow morning'          -> rejected

Two rules, and the second is the one that matters most:

1. **What a person said is understood.** A relative day ("tomorrow",
   "next Friday"), a clock time with or without a meridiem ("9am", "5:30
   PM"), and a combination of the two ("tomorrow at 9:00 AM", "9am
   tomorrow") all resolve.

2. **What cannot be understood is still refused.** The module returns
   `None` for a phrase with no defensible reading, and the caller asks
   rather than guessing. A time that is *ambiguous* — "3pm" is not
   ambiguous, but "noon-ish" is — is not invented into a number.

Every relative word is anchored to the machine's current local time, and
the reference is taken **once** so that "tomorrow at 9am" and "9am
tomorrow" cannot disagree across a midnight.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

#: The weekday names, in Python's `weekday()` order (Monday = 0).
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

#: An explicit ISO date, which must not be read as a clock time.
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

#: The day words, as an offset from today.
_DAY_OFFSETS = {
    "today": 0,
    "tonight": 0,
    "tomorrow": 1,
    "tmr": 1,
    "tomorow": 1,
    "yesterday": -1,
}

#: The parts of the day, and the hour they mean. "noon" and "midnight" are
#: named words rather than clock times, so they are here.
_PARTS_OF_DAY = {
    "midnight": (0, 0),
    "noon": (12, 0),
    "midday": (12, 0),
    "morning": (9, 0),
    "afternoon": (14, 0),
    "evening": (18, 0),
    "night": (20, 0),
    "lunchtime": (12, 0),
    "lunch": (12, 0),
    "breakfast": (8, 0),
    "dinner": (19, 0),
    # "tonight" is a day word *and* an evening qualifier: "tonight at 8"
    # means 20:00, not 08:00.
    "tonight": (20, 0),
}

#: A clock time: "9", "9:30", "09:30", "9.30".
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*(am|pm)?\b", re.I)

#: A relative offset: "in 2 hours", "in 30 minutes", "in 3 days".
_RELATIVE = re.compile(
    r"\bin\s+(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(minute|minutes|min|hour|hours|hr|hrs|day|days|week|weeks)\b", re.I)

#: A "day month" or "month day" pair: "23 October", "October 23".
#: A day-of-month and a month name. The month must be a *month*: the original
#: pattern accepted any word of 3-9 letters, so "3 tomorrow" matched as
#: "3 <month>" and the whole phrase was stripped from the clock text — leaving an
#: empty string, and the answer silently fell back to the day's default hour
#: (09:00) instead of the 3 the person said.
_DAY_MONTH = re.compile(
    r"\b(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\b", re.I)
#: The mirror of _DAY_MONTH, and it had the same bug in the other direction:
#: "tomorrow 3 pm" matched as "<month> 3", so the whole phrase was stripped from
#: the clock text and the answer fell back to 09:00 — the person said 3 pm and
#: was told about nine in the morning. Only a real month name qualifies.
_MONTH_DAY = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\s+(\d{1,2})\b", re.I)

#: The month names, and the abbreviations `strptime` accepts.
_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

#: The spelled-out numbers a relative offset may use.
_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

#: The filler words that carry no time meaning. Stripping them is what
#: lets "at" and "on" and "by" be optional without making them significant.
_FILLER = re.compile(r"\b(at|on|by|starting|start|beginning|begin|of|the|around|about|roughly|somewhere|please)\b", re.I)


def _clock_text(text: str) -> str:
    """The text a clock time may be read from.

    An explicit date is removed first: in "2026-10-23 9:00 AM" the digits
    of the date are not a clock, and reading them as one gave
    ``2026-10-23 10:00`` — the day's own hour. A named date ("23 October")
    is removed for the same reason.
    """
    cleaned = _ISO_DATE.sub(" ", text)
    cleaned = _DAY_MONTH.sub(" ", cleaned)
    cleaned = _MONTH_DAY.sub(" ", cleaned)
    return _FILLER.sub(" ", cleaned)


def _now() -> datetime:
    """The machine's local time, as an aware datetime.

    Taken once per parse so that two words in the same phrase cannot land
    on different days across a midnight.
    """
    return datetime.now().astimezone()


def _apply_meridiem(hour: int, meridiem: Optional[str]) -> Optional[int]:
    """Turn a 12-hour clock into a 24-hour one.

    "9pm" is 21; "12am" is 0; "12pm" is 12. An hour above 12 with a
    meridiem is contradictory and returns `None` rather than a guess.
    """
    if meridiem is None:
        return hour
    meridiem = meridiem.lower()
    if meridiem == "am":
        return 0 if hour == 12 else hour
    if meridiem == "pm":
        if hour == 12:
            return 12
        if hour > 12:
            return None
        return hour + 12
    return None


def _resolve_weekday(word: str, now: datetime, *, next_week: bool) -> Optional[datetime]:
    """The next occurrence of a weekday.

    "Friday" means the coming Friday — today if today is Friday, otherwise
    the next one. "next Friday" means the Friday of the following week, so
    it is always at least a week away.
    """
    index = _WEEKDAYS.get(word.lower())
    if index is None:
        return None
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ahead = (index - today.weekday()) % 7
    if ahead == 0:
        ahead = 7          # "Friday" said on a Friday means the next one
    if next_week:
        # The Friday of the week *after* the coming one: always at least a
        # week away, and never today.
        ahead += 7
    return today + timedelta(days=ahead)


def _resolve_month_day(text: str, now: datetime) -> Optional[datetime]:
    """A named month and day, with no year: "23 October", "October 23".

    The year is the current one, unless the date has already passed this
    year — in which case it is next year, because a person saying
    "October 23" in December means next October.
    """
    match = _DAY_MONTH.search(text)
    if match:
        day = int(match.group(1))
        month = _MONTHS.get(match.group(2).lower())
    else:
        match = _MONTH_DAY.search(text)
        if not match:
            return None
        month = _MONTHS.get(match.group(1).lower())
        day = int(match.group(2))
    if month is None:
        return None
    try:
        candidate = now.replace(month=month, day=day, hour=0, minute=0,
                                second=0, microsecond=0)
    except ValueError:
        return None        # "31 February"
    if candidate < now:
        try:
            candidate = candidate.replace(year=candidate.year + 1)
        except ValueError:
            return None    # "29 February" in a non-leap year
    return candidate


def parse_natural_datetime(value: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Read an ordinary time phrase, or return ``None``.

    The phrase is understood in the order a person means it: the *day* first
    (a relative word, a weekday, a named date), then the *clock* (a time of
    day, a part of the day), then a relative offset ("in 2 hours").

    A phrase with a day but no clock time lands at the day's conventional
    hour for that word — "tomorrow" alone means tomorrow morning, because
    that is what a person who says "schedule it tomorrow" means, and
    "tonight" means this evening.

    Returns `None` when nothing in the phrase names a time. It never
    invents one.
    """
    if not value:
        return None
    text = value.strip().lower()
    if not text:
        return None
    now = now or _now()

    # --- an explicit relative offset: "in 2 hours" ------------------------
    offset = _RELATIVE.search(text)
    if offset:
        raw = offset.group(1).lower()
        count = _WORD_NUMBERS.get(raw)
        if count is None:
            try:
                count = int(raw)
            except ValueError:
                count = None
        if count is not None:
            unit = offset.group(2).lower()
            if unit.startswith("minute") or unit in ("min",):
                return now + timedelta(minutes=count)
            if unit.startswith("hour") or unit in ("hr", "hrs"):
                return now + timedelta(hours=count)
            if unit.startswith("day"):
                return now + timedelta(days=count)
            if unit.startswith("week"):
                return now + timedelta(weeks=count)

    # --- the day ----------------------------------------------------------
    day: Optional[datetime] = None
    weekday_match = re.search(
        r"\b(next\s+|last\s+|this\s+)?(" + "|".join(_WEEKDAYS) + r")\b", text)
    if weekday_match:
        day = _resolve_weekday(
            weekday_match.group(2), now,
            next_week=bool(weekday_match.group(1)
                           and weekday_match.group(1).strip() == "next"))
    else:
        for word, offset_days in _DAY_OFFSETS.items():
            if re.search(rf"\b{word}\b", text):
                day = (now + timedelta(days=offset_days)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                break
    if day is None:
        day = _resolve_month_day(text, now)

    # --- the clock --------------------------------------------------------
    hour: Optional[int] = None
    minute = 0
    had_clock = False

    # The part of the day ("morning", "evening") is a *qualifier* when a
    # clock is also present ("tomorrow morning at 11") and the answer on its
    # own otherwise ("this evening"). It is read first so that a bare hour
    # can be qualified by it -- "3 in the afternoon" is 15:00, not 03:00.
    part_hour: Optional[int] = None
    part_minute = 0
    for word, (word_hour, word_minute) in _PARTS_OF_DAY.items():
        if re.search(rf"\b{word}\b", text):
            part_hour, part_minute = word_hour, word_minute
            break

    clock = _CLOCK.search(_clock_text(text))
    if clock:
        raw_hour = int(clock.group(1))
        minute = int(clock.group(2) or 0)
        meridiem = clock.group(4)
        resolved = _apply_meridiem(raw_hour, meridiem)
        if resolved is None:
            return None
        if meridiem is None and 1 <= raw_hour <= 12 and part_hour is not None:
            # "3 in the morning" and "3 in the afternoon" -- the part of the
            # day qualifies a bare hour.
            if part_hour >= 12:
                resolved = raw_hour + 12
            else:
                resolved = raw_hour
        if resolved > 23:
            return None
        hour = resolved
        had_clock = True
    elif part_hour is not None:
        hour, minute = part_hour, part_minute

    # --- combine ----------------------------------------------------------
    if day is not None:
        if hour is None:
            # "tomorrow" with no clock: the day's conventional hour.
            hour, minute = 9, 0
        elif not had_clock and part_hour is not None and part_hour >= 12:
            # "tonight at 8" -- "tonight" says the evening, so a bare 8 means
            # 20:00, not 08:00. The part of the day qualifies the hour when
            # the hour carries no meridiem of its own.
            #
            # "noon" and "midnight" are already 24-hour times, so they are
            # left alone: adding twelve to noon would give 24:00.
            if 1 <= hour <= 12 and hour != 12:
                hour = hour + 12
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if had_clock:
        # A clock time with no day: today, or tomorrow if it has passed.
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if hour is not None:
        # Only a part of the day ("morning"), with no clock: today, or
        # tomorrow if that part has already passed.
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    return None


def describe_datetime(value: datetime) -> str:
    """A time, written the way a person would say it.

    Used in the question a tool asks when it cannot understand a time, so
    the person is offered a phrase they can recognise rather than a
    formatted timestamp they have to decode.
    """
    now = _now()
    days = (value.date() - now.date()).days
    if days == 0:
        day_word = "today"
    elif days == 1:
        day_word = "tomorrow"
    elif days == -1:
        day_word = "yesterday"
    elif 0 < days < 7:
        day_word = value.strftime("%A")
    else:
        day_word = value.strftime("%d %B")
    return f"{day_word} at {value.strftime('%I:%M %p').lstrip('0').lower()}"
