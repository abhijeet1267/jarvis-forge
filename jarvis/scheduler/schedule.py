"""Schedule parsing (v0.8).

No cron library is installed (``croniter`` and ``apscheduler`` are absent), so
this is a deliberately small recurrence model rather than a cron parser. It
covers what people actually say:

    "at 8 PM"                  -> once, today (or tomorrow if 8 PM has passed)
    "every morning"            -> daily at 08:00
    "every evening at 7"       -> daily at 19:00
    "every Monday"             -> weekly on Monday at 09:00
    "every Monday at 9 AM"     -> weekly on Monday at 09:00
    "every 30 minutes"         -> interval
    "every day at 6:30"        -> daily at 06:30

Anything it cannot understand returns ``None`` so the caller asks, rather than
inventing a time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

#: Named times of day.
NAMED_HOURS = {
    "morning": 8,
    "noon": 12,
    "midday": 12,
    "afternoon": 14,
    "evening": 19,
    "night": 21,
    "midnight": 0,
}


@dataclass
class Recurrence:
    """A small, explicit recurrence description."""

    kind: str                  # once | daily | weekly | interval
    hour: int = 9
    minute: int = 0
    weekday: Optional[int] = None      # 0 = Monday, for kind == "weekly"
    interval_minutes: int = 0          # for kind == "interval"
    at: Optional[datetime] = None      # for kind == "once"

    def describe(self) -> str:
        if self.kind == "once":
            return f"once at {self.at.strftime('%a %d %b %H:%M')}" if self.at else "once"
        if self.kind == "interval":
            if self.interval_minutes % 60 == 0:
                return f"every {self.interval_minutes // 60} hour(s)"
            return f"every {self.interval_minutes} minutes"
        clock = f"{self.hour:02d}:{self.minute:02d}"
        if self.kind == "weekly":
            names = [n for n, i in WEEKDAYS.items() if i == self.weekday and len(n) > 3]
            return f"every {names[0].title() if names else 'week'} at {clock}"
        return f"every day at {clock}"

    def to_rrule(self) -> str:
        """A compact, self-describing string for storage."""
        if self.kind == "interval":
            return f"FREQ=MINUTES;INTERVAL={self.interval_minutes}"
        if self.kind == "once":
            return f"FREQ=ONCE;AT={self.at.isoformat()}" if self.at else "FREQ=ONCE"
        if self.kind == "weekly":
            return f"FREQ=WEEKLY;BYDAY={self.weekday};BYHOUR={self.hour};BYMINUTE={self.minute}"
        return f"FREQ=DAILY;BYHOUR={self.hour};BYMINUTE={self.minute}"

    @staticmethod
    def from_rrule(value: str) -> Optional["Recurrence"]:
        """Rebuild a recurrence from its stored form."""
        if not value:
            return None
        parts = {}
        for chunk in value.split(";"):
            if "=" in chunk:
                key, _, val = chunk.partition("=")
                parts[key.upper()] = val

        freq = parts.get("FREQ", "").upper()
        try:
            if freq == "MINUTES":
                return Recurrence(kind="interval", interval_minutes=int(parts.get("INTERVAL", 60)))
            if freq == "ONCE":
                at = datetime.fromisoformat(parts["AT"]) if "AT" in parts else None
                return Recurrence(kind="once", at=at)
            if freq == "WEEKLY":
                return Recurrence(
                    kind="weekly",
                    weekday=int(parts.get("BYDAY", 0)),
                    hour=int(parts.get("BYHOUR", 9)),
                    minute=int(parts.get("BYMINUTE", 0)),
                )
            if freq == "DAILY":
                return Recurrence(
                    kind="daily",
                    hour=int(parts.get("BYHOUR", 9)),
                    minute=int(parts.get("BYMINUTE", 0)),
                )
        except (KeyError, ValueError):
            return None
        return None

    # -- next occurrence -----------------------------------------------------
    def next_after(self, moment: datetime) -> Optional[datetime]:
        """The first occurrence strictly after ``moment``."""
        if self.kind == "once":
            return self.at if self.at and self.at > moment else None

        if self.kind == "interval":
            return moment + timedelta(minutes=max(1, self.interval_minutes))

        if self.kind == "weekly":
            target = self.weekday if self.weekday is not None else moment.weekday()
            candidate = moment.replace(
                hour=self.hour, minute=self.minute, second=0, microsecond=0
            )
            days_ahead = (target - candidate.weekday()) % 7
            candidate += timedelta(days=days_ahead)
            if candidate <= moment:
                candidate += timedelta(days=7)
            return candidate

        # daily
        candidate = moment.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if candidate <= moment:
            candidate += timedelta(days=1)
        return candidate


def _parse_clock(text: str) -> Optional[tuple[int, int]]:
    """Extract an hour/minute from a phrase, or None."""
    # A named time of day.
    for word, hour in NAMED_HOURS.items():
        if re.search(rf"\b{word}\b", text):
            return hour, 0

    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", text)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "").lower()

    if hour > 23 or minute > 59:
        return None
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem and hour <= 7:
        # "at 5" in the evening reads as 17:00.
        hour += 12
    return hour, minute


def parse_schedule(text: str, now: Optional[datetime] = None) -> Optional[Recurrence]:
    """Parse a schedule phrase. Returns None when it cannot be understood."""
    if not text:
        return None
    now = now or datetime.now().astimezone()
    lowered = text.lower()

    # --- interval ---------------------------------------------------------
    interval = re.search(r"\bevery\s+(\d+)\s*(minute|minutes|min|mins|hour|hours|hr|hrs)\b", lowered)
    if interval:
        amount = int(interval.group(1))
        unit = interval.group(2)
        minutes = amount * 60 if unit.startswith(("hour", "hr")) else amount
        if minutes < 1:
            return None
        return Recurrence(kind="interval", interval_minutes=minutes)

    # --- once (an explicit clock with no "every") --------------------------
    recurring = re.search(r"\b(every|each|daily|weekly|weekday)\b", lowered)

    if not recurring:
        clock = _parse_clock(lowered)
        if clock is None:
            return None
        hour, minute = clock
        at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        return Recurrence(kind="once", at=at)

    # --- weekly -----------------------------------------------------------
    for name, index in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", lowered):
            clock = _parse_clock(lowered)
            hour, minute = clock if clock else (9, 0)
            return Recurrence(kind="weekly", weekday=index, hour=hour, minute=minute)

    # --- daily ------------------------------------------------------------
    if re.search(r"\b(every\s*day|every\s+morning|every\s+evening|every\s+night|"
                 r"every\s+afternoon|daily|each\s+day|every\s+weekday)\b", lowered):
        clock = _parse_clock(lowered)
        hour, minute = clock if clock else (8, 0)
        return Recurrence(kind="daily", hour=hour, minute=minute)

    # "every morning" style without the word "every day".
    clock = _parse_clock(lowered)
    if clock is not None:
        hour, minute = clock
        return Recurrence(kind="daily", hour=hour, minute=minute)

    return None
