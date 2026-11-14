"""Calendar tools (v0.7).

Reading is free; creating, changing and cancelling events are permission
controlled, as the plan requires.

Every handler distinguishes *what the provider encountered* from *what the
calendar contains*: "Nothing scheduled for today" is only said when a server
actually answered with nothing. A read that failed is a ``fail()``, and
``find_free_time`` refuses to offer a slot it could not have checked -- a user
must never be able to book against a read that never happened.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple

from ..core.models import ActionResult, RiskLevel, ToolSpec, Verification
from ..integrations.calendar_provider import (
    CalendarEvent,
    ProviderStatus,
    get_calendar_provider,
    parse_datetime,
    read_status,
    status_token,
)
from ..logging_setup import get_logger
from .base import ToolContext, fail, ok

log = get_logger("tools.calendar")

#: Working hours used when suggesting free slots.
DAY_START_HOUR = 8
DAY_END_HOUR = 22

#: One sentence per way a calendar read can fail. Each names a different action
#: for the user, because each failure needs a different one -- and none of them
#: is allowed to sound like an empty day.
FAILURE_LINES = {
    ProviderStatus.UNAVAILABLE: (
        "I could not reach your calendar server at {where} ({detail}). Nothing was read, "
        "so this is not an empty day — check that the server is running and reachable."
    ),
    ProviderStatus.AUTH_ERROR: (
        "Your calendar was not read: it rejected the saved credentials ({detail}). "
        "Nothing was read — reconnect the calendar account (for Google, in Settings → "
        "Google; otherwise check the calendar username and password)."
    ),
    ProviderStatus.NETWORK_ERROR: (
        "The connection to your calendar server at {where} broke before it answered "
        "({detail}). Nothing was read, so this is not an empty day."
    ),
    ProviderStatus.PROVIDER_ERROR: (
        "Your calendar server answered but could not return your schedule ({detail}). "
        "Nothing was read, so this is not an empty day."
    ),
}

#: Used when a provider reports a state this layer has no sentence for. Still a
#: refusal: an unknown failure must never be reported as an empty day either.
_FALLBACK_LINE = (
    "I could not read your calendar ({detail}). Nothing was read, so this is not an empty day."
)


def _outcome_of(outcome: Any) -> tuple[ProviderStatus, str, str]:
    """(status, detail, server) for a read result or for a provider dict.

    Reads come back as a labelled list; writes report a dict. Both carry the
    same :class:`ProviderStatus`, so one sentence table covers both.
    """
    if isinstance(outcome, dict):
        status = status_token(outcome.get("status")) or ProviderStatus.PROVIDER_ERROR
        detail = str(outcome.get("detail") or outcome.get("error") or "")
        endpoint = str(outcome.get("endpoint") or "")
    else:
        status = read_status(outcome)
        detail = str(getattr(outcome, "detail", "") or "")
        endpoint = str(getattr(outcome, "endpoint", "") or "")
    return status, detail, endpoint


def _read_problem(
    tool: str,
    ctx: ToolContext,
    outcome: Any,
    *,
    because: str = "",
    **output: Any,
) -> ActionResult:
    """Refuse, in the right words, when the calendar could not be read.

    ``outcome`` is whatever the provider returned or raised with a status: a
    labelled result from a read, or a provider dict from a write.
    """
    status, detail, endpoint = _outcome_of(outcome)
    line = FAILURE_LINES.get(status, _FALLBACK_LINE)
    message = line.format(where=endpoint or "the configured address", detail=detail or status.value)
    if because:
        message = f"{message} {because}"
    return fail(
        tool,
        message,
        device=ctx.device_id,
        provider_status=status.value,
        verified=True,
        provider_detail=detail,
        provider_endpoint=endpoint,
        **output,
    )


def _now() -> datetime:
    return datetime.now().astimezone()


def _resolve_range(date: str, days: int = 1) -> tuple[datetime, datetime, Optional[str]]:
    """Turn a human date word into a concrete window.

    The week phrases are *ranges* rather than instants, so they are read
    here; everything else goes through `parse_datetime`, which understands
    both the strict formats and ordinary English ("tomorrow at 9am",
    "next Friday", "23 October").
    """
    today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    text = (date or "today").strip().lower()

    if text in ("today", ""):
        start = today
    elif text == "tomorrow":
        start = today + timedelta(days=1)
    elif text == "yesterday":
        start = today - timedelta(days=1)
    elif text in ("this week", "week"):
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7), None
    elif text in ("next week",):
        start = today - timedelta(days=today.weekday()) + timedelta(days=7)
        return start, start + timedelta(days=7), None
    else:
        parsed = parse_datetime(text)
        if parsed is None:
            # The suggestion is a *live* date, not a fixed one: the old
            # message offered "2026-09-25" from whenever it was written,
            # which is in the past by the time a person reads it.
            example = (today + timedelta(days=1)).strftime("%Y-%m-%d")
            return today, today + timedelta(days=1), (
                f"I could not understand the date {date!r}. Try 'today', 'tomorrow', "
                f"'next Friday', or an explicit date like {example}."
            )
        start = parsed.replace(hour=0, minute=0, second=0, microsecond=0)

    return start, start + timedelta(days=max(1, days)), None


async def list_events(ctx: ToolContext, date: str = "today", days: int = 1,
                      query: str = "") -> Any:
    """List events in a window.

    A ``query`` searches the whole calendar for the text and ignores the
    window, so a clarification that needs an event's id can find one that
    is not scheduled for today. It is the same search as
    :func:`search_events`, carried here so that this tool can be the
    identifier source for an event id.
    """
    if query:
        start, end, problem = _resolve_range("today", 1)
        if problem:
            return fail("list_events", problem, device=ctx.device_id)
        provider = get_calendar_provider(ctx.settings)
        try:
            events = provider.search(query, limit=20)
        except Exception as exc:
            return fail("list_events", f"Could not read the calendar: {exc}",
                        device=ctx.device_id)
        status = read_status(events)
        if not status.read_succeeded:
            return _read_problem("list_events", ctx, events)
        return ok(
            "list_events",
            f"{len(events)} event(s) matching {query!r}.",
            device=ctx.device_id,
            provider=provider.name,
            provider_status=status.value,
        verified=True,
            events=[e.as_dict() for e in events],
        )

    start, end, problem = _resolve_range(date, days)
    if problem:
        return fail("list_events", problem, device=ctx.device_id)

    provider = get_calendar_provider(ctx.settings)
    try:
        events = provider.list_events(start, end, limit=100)
    except Exception as exc:
        return fail("list_events", f"Could not read the calendar: {exc}", device=ctx.device_id)

    status = read_status(events)
    if not status.read_succeeded:
        return _read_problem("list_events", ctx, events)

    if status is ProviderStatus.REAL_EMPTY:
        return ok(
            "list_events",
            f"Nothing scheduled for {date or 'today'}.",
            device=ctx.device_id,
            provider=provider.name,
            provider_status=status.value,
            # A successful empty read is a *verified fact*: the provider
            # answered and said there is nothing. Leaving this UNKNOWN made
            # the assistant append "I could not verify it happened" to its
            # own correct answer, which reads as a failure and is not one.
            verified=True,
            events=[],
        )

    first = events[0]
    return ok(
        "list_events",
        f"{len(events)} event(s). First: {first.title} at {first.start.strftime('%H:%M')}.",
        device=ctx.device_id,
        provider=provider.name,
        provider_status=status.value,
        verified=True,
        events=[e.as_dict() for e in events],
    )


async def search_events(ctx: ToolContext, query: str = "") -> Any:
    """Find events by title, location or description."""
    if not query:
        return fail("search_events", "What should I look for?", device=ctx.device_id)

    provider = get_calendar_provider(ctx.settings)
    try:
        events = provider.search(query, limit=20)
    except Exception as exc:
        return fail("search_events", f"Could not search the calendar: {exc}", device=ctx.device_id)

    status = read_status(events)
    if not status.read_succeeded:
        return _read_problem(
            "search_events", ctx, events, because=f"I cannot say whether anything matches {query!r}."
        )

    if status is ProviderStatus.REAL_EMPTY:
        return ok(
            "search_events",
            f"I found no events matching {query!r}.",
            device=ctx.device_id,
            provider_status=status.value,
        verified=True,
            events=[],
        )

    return ok(
        "search_events",
        f"{len(events)} event(s) matching {query!r}. Next: {events[0].title}.",
        device=ctx.device_id,
        provider_status=status.value,
        verified=True,
        events=[e.as_dict() for e in events],
    )


async def next_event(ctx: ToolContext) -> Any:
    """The next upcoming event."""
    provider = get_calendar_provider(ctx.settings)
    now = _now()
    try:
        events = provider.list_events(now, now + timedelta(days=30), limit=50)
    except Exception as exc:
        return fail("next_event", f"Could not read the calendar: {exc}", device=ctx.device_id)

    status = read_status(events)
    if not status.read_succeeded:
        return _read_problem("next_event", ctx, events, because="I cannot say what is coming up.")

    upcoming = [e for e in events if e.end > now]
    if not upcoming:
        return ok(
            "next_event",
            "You have nothing coming up.",
            device=ctx.device_id,
            provider_status=status.value,
        verified=True,
            event=None,
        )

    event = upcoming[0]
    delta = event.start - now
    when = "now" if delta.total_seconds() <= 0 else f"in {int(delta.total_seconds() // 60)} minutes"
    return ok(
        "next_event",
        f"Next up: {event.title} at {event.start.strftime('%H:%M')} ({when}).",
        device=ctx.device_id,
        provider_status=status.value,
        verified=True,
        event=event.as_dict(),
    )


async def brief_meeting(ctx: ToolContext, meeting: str = "") -> Any:
    """Prepare a briefing for the next (or named) meeting.

    The words "prepare me for the next meeting" used to fall to `next_event`, which
    reports the time and nothing else — the same request on the planner page gave a
    full brief. This routes the sentence to the briefing itself, assembled from the
    real calendar, planner tasks and mailbox.
    """
    import sys

    module = sys.modules.get("jarvis.api.app")
    if module is None:  # pragma: no cover - the app is always imported when tools run
        return fail("brief_meeting", "The briefing service is not available.",
                    device=ctx.device_id)
    brief = module.build_meeting_briefing(meeting)
    if not brief.get("ok"):
        reason = brief.get("reason") or "I could not prepare that meeting."
        return fail("brief_meeting", reason, device=ctx.device_id,
                    candidates=brief.get("candidates") or [])

    event = brief.get("meeting") or {}
    points = brief.get("talking_points") or []
    when = event.get("start") or ""
    headline = f"Briefing for {event.get('title') or 'your next meeting'}"
    return ok(
        "brief_meeting",
        headline + (f" ({when[:16]})" if when else "") + ". "
        + " ".join(points) + " I prepared this; I do not attend the meeting.",
        device=ctx.device_id,
        meeting=event,
        talking_points=points,
        related_tasks=brief.get("related_tasks") or [],
        related_email=brief.get("related_email") or [],
        conflict=brief.get("conflict"),
        verified=True,
    )

async def check_availability(ctx: ToolContext, when: str = "", duration_minutes: int = 60) -> Any:
    """Whether a specific time is free, and what else is on then.

    ``find_free_time`` answers "when is the next gap"; this answers "am I free at
    3 PM", which is a different question and the one people actually ask before
    agreeing to something. An unreadable calendar is a refusal with
    ``available=None`` -- never "you're free", which would be a guess about
    someone's diary.
    """
    from ..integrations.calendar_provider import parse_datetime

    start = parse_datetime(when) if str(when).strip() else None
    if start is None:
        return fail("check_availability",
                    f"I could not understand the time {when!r}.", device=ctx.device_id)
    if start.tzinfo is None:
        start = start.astimezone()
    duration = max(15, int(duration_minutes))
    end = start + timedelta(minutes=duration)

    provider = get_calendar_provider(ctx.settings)
    try:
        events = provider.list_events(start - timedelta(minutes=1), end + timedelta(minutes=1),
                                      limit=50)
    except Exception as exc:
        return fail("check_availability", f"Could not read the calendar: {exc}",
                    device=ctx.device_id)

    status = read_status(events)
    if not status.read_succeeded:
        return _read_problem(
            "check_availability", ctx, events,
            because="I will not tell you a time is free from a calendar I could not read.",
            slot=None, available=None)

    clashes = [
        {"title": e.title, "start": e.start.isoformat(), "end": e.end.isoformat()}
        for e in events if e.start < end and e.end > start
    ]
    if clashes:
        names = "; ".join(c["title"] or "(untitled)" for c in clashes[:3])
        return ok("check_availability",
                  f"You already have {len(clashes)} event(s) then: {names}.",
                  device=ctx.device_id, verified=True, available=False,
                  slot={"start": start.isoformat(), "end": end.isoformat()},
                  conflicts=clashes)
    return ok("check_availability",
              f"You are free at {start.strftime('%H:%M')} for {duration} minutes.",
              device=ctx.device_id, verified=True, available=True,
              slot={"start": start.isoformat(), "end": end.isoformat()}, conflicts=[])

async def find_free_time(ctx: ToolContext, date: str = "today", duration_minutes: int = 60) -> Any:
    """Find the first free slot of a given length.

    A slot is only ever offered from a schedule that was actually read: this is
    the one output a user might book against, so an unreadable calendar here is
    a refusal with ``slot=None``, never a guess.
    """
    start, end, problem = _resolve_range(date, 1)
    if problem:
        return fail("find_free_time", problem, device=ctx.device_id)

    duration = max(15, int(duration_minutes))
    provider = get_calendar_provider(ctx.settings)
    try:
        events = provider.list_events(start, end, limit=100)
    except Exception as exc:
        return fail("find_free_time", f"Could not read the calendar: {exc}", device=ctx.device_id)

    status = read_status(events)
    if not status.read_succeeded:
        return _read_problem(
            "find_free_time",
            ctx,
            events,
            because="I will not suggest a slot from a calendar I could not read.",
            slot=None,
        )

    day_start = start.replace(hour=DAY_START_HOUR)
    day_end = start.replace(hour=DAY_END_HOUR)

    busy = sorted(
        [(max(e.start, day_start), min(e.end, day_end)) for e in events if e.end > day_start and e.start < day_end]
    )

    # Planner tasks with a start time already occupy the day, and the calendar has
    # never heard of them. Without this, every separate request for a block saw a
    # clear day and was handed the same first slot -- three of them stacked on
    # 08:00 across three calls.
    try:
        from ..core.orchestrator import JarvisCore

        for task in JarvisCore().planner_service.list_tasks():
            if not task.is_open or task.start_at is None:
                continue
            task_start = task.start_at
            if task_start.tzinfo is None:
                task_start = task_start.astimezone()
            task_end = task_start + timedelta(minutes=max(15, int(task.estimate_minutes or 30)))
            if task_end > day_start and task_start < day_end:
                busy.append((max(task_start, day_start), min(task_end, day_end)))
        busy.sort()
    except Exception:  # a planner read must not break finding free time
        pass

    cursor = max(day_start, _now()) if start.date() == _now().date() else day_start
    for busy_start, busy_end in busy:
        if (busy_start - cursor) >= timedelta(minutes=duration):
            break
        cursor = max(cursor, busy_end)

    if (day_end - cursor) < timedelta(minutes=duration):
        return ok(
            "find_free_time",
            f"I could not find a free {duration}-minute slot on {date or 'today'}.",
            device=ctx.device_id,
            provider_status=status.value,
        verified=True,
            slot=None,
        )

    return ok(
        "find_free_time",
        f"The first free {duration}-minute slot is {cursor.strftime('%H:%M')}–"
        f"{(cursor + timedelta(minutes=duration)).strftime('%H:%M')}.",
        device=ctx.device_id,
        provider_status=status.value,
        slot={
            "start": cursor.isoformat(),
            "end": (cursor + timedelta(minutes=duration)).isoformat(),
        },
    )


async def create_event(
    ctx: ToolContext,
    title: str = "",
    start: str = "",
    end: str = "",
    duration_minutes: int = 60,
    location: str = "",
) -> Any:
    """Create a calendar event."""
    if not title:
        return fail("create_event", "What should I call the event?", device=ctx.device_id)

    start_at = parse_datetime(start)
    if start_at is None:
        # The example is a *live* date. The old message offered
        # "2026-09-21 17:00" from whenever it was written, so by the time a
        # person read it the date was in the past -- an invitation to book
        # the wrong day.
        from ..integrations.natural_time import describe_datetime

        example = describe_datetime(
            _now().replace(hour=9, minute=0, second=0, microsecond=0)
            + timedelta(days=1))
        return fail(
            "create_event",
            f"I could not understand the start time {start!r}. Try something "
            f"like {example!r}.",
            device=ctx.device_id,
        )

    end_at = parse_datetime(end) if end else start_at + timedelta(minutes=max(5, int(duration_minutes)))
    if end_at <= start_at:
        return fail("create_event", "The end time is before the start time.", device=ctx.device_id)

    provider = get_calendar_provider(ctx.settings)
    event = CalendarEvent(
        uid="",
        title=title,
        start=start_at,
        end=end_at,
        location=location,
    )
    try:
        result = provider.create(event)
    except Exception as exc:
        return fail("create_event", f"Could not create the event: {exc}", device=ctx.device_id)

    if not result.get("ok"):
        # A *refusal* is not a read failure. A read-only provider (Google, whose
        # connection deliberately asks only for ``calendar.readonly``) answers
        # `ok: False` with a status of `auth_error`; routing that through
        # `_read_problem` reported "Google rejected the access token" and sent the
        # person to reconnect an account that was working perfectly. Say what
        # actually happened instead, and name the alternative.
        if result.get("read_only"):
            return fail(
                "create_event",
                "That calendar is connected read-only, so I cannot add an event to it. "
                "Nothing was created. A local event can be added instead if you want "
                "it in Vantage's own calendar.",
                device=ctx.device_id,
                provider=provider.name, read_only=True,
                local_alternative=True,
            )
        if status_token(result.get("status")) is not None:
            return _read_problem(
                "create_event", ctx, result, because=f"The event {title!r} was not created."
            )
        return fail("create_event", result.get("error", "could not create the event"), device=ctx.device_id)

    # The provider said it created the event. Whether the event is *there* is
    # a different question, and the only honest way to answer it is to look.
    # So the calendar is read back and the new event looked for by its id.
    #
    # Before this, the result was `ok(...)`, whose verification default is
    # UNKNOWN -- truthful, but it left "created" resting on the provider's own
    # word. A read-back turns that into a fact: the event is in the calendar
    # or it is not.
    uid = str(result.get("uid") or "")
    confirmed, note = _confirm_event(provider, uid, start_at, end_at)
    if confirmed:
        built = ok(
            "create_event",
            f"Created {title} on {start_at.strftime('%a %d %b at %H:%M')}.",
            device=ctx.device_id,
            verified=True,
            verification=Verification.SUCCESS,
            provider=provider.name,
            event=result["event"],
        )
    else:
        # The provider claimed success but the event is not there. That is not a
        # success, and it is not a plain failure either -- the call may well
        # have been made. It is reported as unverified, with the reason.
        built = ok(
            "create_event",
            f"Created {title} on {start_at.strftime('%a %d %b at %H:%M')}.",
            device=ctx.device_id,
            verified=False,
            verification=Verification.UNKNOWN,
            provider=provider.name,
            event=result["event"],
        )
    # `ok()` puts unknown keyword arguments into the output dict, so the note
    # is set on the result itself -- it is a field callers read, not a piece
    # of payload.
    built.verification_note = note
    return built


def _confirm_event(
    provider: Any,
    uid: str,
    start_at: datetime,
    end_at: datetime,
) -> Tuple[bool, str]:
    """Look the new event up in the calendar. ``(confirmed, note)``.

    The read covers the event's own window with a margin, because a provider
    may store the times slightly differently (a timezone, an all-day flag), and
    the id is what identifies the event, not the time.

    A read that itself failed is not a failed confirmation: the honest answer
    is that the outcome could not be established, which is a different fact
    from "the event is not there".
    """
    if not uid:
        return False, "The calendar did not return an id for the new event, so I could not confirm it."

    try:
        window = provider.list_events(
            start_at - timedelta(days=1), end_at + timedelta(days=1), limit=200
        )
    except Exception as exc:  # a read that breaks is not a failed confirmation
        return False, f"The calendar was written to, but reading it back failed ({exc}), so I could not confirm the event."

    status = status_token(getattr(window, "status", None))
    if status is not None and status not in (ProviderStatus.HAS_RESULTS, ProviderStatus.REAL_EMPTY):
        # The read failed. That is not a failed confirmation -- the honest
        # answer is that the outcome could not be established, which is a
        # different fact from "the event is not there".
        return False, (
            f"The calendar was written to, but reading it back did not work ({status.value}), "
            "so I could not confirm the event."
        )

    for existing in window:
        if getattr(existing, "uid", "") == uid:
            return True, f"Confirmed by reading the calendar back (uid {uid})."

    return False, (
        f"The calendar accepted the event (uid {uid}), but reading it back did not show it, "
        "so I cannot confirm it was created."
    )


async def update_event(
    ctx: ToolContext,
    uid: str = "",
    title: str = "",
    start: str = "",
    end: str = "",
    location: str = "",
) -> Any:
    """Change an existing event."""
    if not uid:
        return fail("update_event", "Which event should I change?", device=ctx.device_id)

    changes: dict = {}
    if title:
        changes["title"] = title
    if location:
        changes["location"] = location
    if start:
        parsed = parse_datetime(start)
        if parsed is None:
            return fail("update_event", f"I could not understand {start!r}.", device=ctx.device_id)
        changes["start"] = parsed
    if end:
        parsed = parse_datetime(end)
        if parsed is None:
            return fail("update_event", f"I could not understand {end!r}.", device=ctx.device_id)
        changes["end"] = parsed

    if not changes:
        return fail("update_event", "Nothing to change.", device=ctx.device_id)

    provider = get_calendar_provider(ctx.settings)
    try:
        result = provider.update(uid, changes)
    except Exception as exc:
        return fail("update_event", f"Could not update the event: {exc}", device=ctx.device_id)

    if not result.get("ok"):
        if status_token(result.get("status")) is not None:
            return _read_problem(
                "update_event", ctx, result, because="The event was not changed."
            )
        return fail("update_event", result.get("error", "could not update"), device=ctx.device_id)

    return ok(
        "update_event",
        f"Updated the event.",
        device=ctx.device_id,
        event=result.get("event"),
    )


async def cancel_event(ctx: ToolContext, uid: str = "") -> Any:
    """Cancel an event."""
    if not uid:
        return fail("cancel_event", "Which event should I cancel?", device=ctx.device_id)

    provider = get_calendar_provider(ctx.settings)
    try:
        result = provider.cancel(uid)
    except Exception as exc:
        return fail("cancel_event", f"Could not cancel the event: {exc}", device=ctx.device_id)

    if not result.get("ok"):
        if status_token(result.get("status")) is not None:
            return _read_problem(
                "cancel_event", ctx, result, because="The event was not cancelled."
            )
        return fail("cancel_event", result.get("error", "could not cancel"), device=ctx.device_id)

    name = result.get("cancelled")
    return ok(
        "cancel_event",
        f"Cancelled {name}." if name else "Cancelled the event.",
        device=ctx.device_id,
        uid=uid,
    )


async def daily_schedule(ctx: ToolContext, date: str = "today") -> Any:
    """A readable schedule for one day."""
    start, end, problem = _resolve_range(date, 1)
    if problem:
        return fail("daily_schedule", problem, device=ctx.device_id)

    provider = get_calendar_provider(ctx.settings)
    try:
        events = provider.list_events(start, end, limit=100)
    except Exception as exc:
        return fail("daily_schedule", f"Could not read the calendar: {exc}", device=ctx.device_id)

    status = read_status(events)
    if not status.read_succeeded:
        return _read_problem("daily_schedule", ctx, events, because=f"I cannot describe {date or 'today'}.")

    if status is ProviderStatus.REAL_EMPTY:
        return ok(
            "daily_schedule",
            f"Your {date or 'today'} is clear.",
            device=ctx.device_id,
            provider_status=status.value,
        verified=True,
            schedule=[],
        )

    lines = [f"{e.start.strftime('%H:%M')}–{e.end.strftime('%H:%M')}  {e.title}" for e in events]
    return ok(
        "daily_schedule",
        f"{len(events)} event(s) on {date or 'today'}:\n" + "\n".join(lines),
        device=ctx.device_id,
        provider_status=status.value,
        verified=True,
        schedule=[e.as_dict() for e in events],
    )


SPECS = [
    (
        ToolSpec(
            name="list_events",
            description="List calendar events for a day or week.",
            agent="calendar",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"date": "string", "days": "int", "query": "string"},
            tags=["calendar", "read"],
            # The `lookup` key says which parameter carries the thing the
            # person called the event. Without it the source is run with no
            # arguments -- and "no arguments" for a windowed enumerator
            # means *today*, so a request about next week's event was
            # answered "I checked, and there is nothing of that kind right
            # now" while the event sat six days away. The lookup turns the
            # enumerator into a search over the whole calendar.
            provides_identifiers={"uid": {"items": "events", "id": "uid",
                                          "label": "title",
                                          "lookup": "query"}},

        ),
        list_events,
    ),
    (
        ToolSpec(
            name="search_events",
            description="Find calendar events by text.",
            agent="calendar",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"query": "string"},
            required_parameters=["query"],
            tags=["calendar", "read"],
        ),
        search_events,
    ),
    (
        ToolSpec(
            name="next_event",
            description="Report the next upcoming event.",
            agent="calendar",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["calendar", "read"],
        ),
        next_event,
    ),
    (
        ToolSpec(
            name="check_availability",
            description=("Whether a specific time is free, and what else is on then. "
                         "An unreadable calendar is a refusal, never 'you are free'."),
            agent="calendar",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"when": "string", "duration_minutes": "int"},
            required_parameters=["when"],
            tags=["calendar", "read"],
        ),
        check_availability,
    ),
    (
        ToolSpec(
            name="brief_meeting",
            description=("Prepare a briefing for the next or a named meeting: related mail, "
                         "open tasks and any clash. It prepares; it does not attend."),
            agent="calendar",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"meeting": "string"},
            tags=["calendar", "meeting", "read"],
        ),
        brief_meeting,
    ),
    (
        ToolSpec(
            name="find_free_time",
            description="Find the first free slot of a given length.",
            agent="calendar",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"date": "string", "duration_minutes": "int"},
            tags=["calendar", "read"],
        ),
        find_free_time,
    ),
    (
        ToolSpec(
            name="daily_schedule",
            description="A readable schedule for one day.",
            agent="calendar",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"date": "string"},
            tags=["calendar", "read"],
        ),
        daily_schedule,
    ),
    (
        ToolSpec(
            name="create_event",
            description="Create a calendar event.",
            agent="calendar",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={
                "title": "string",
                "start": "string",
                "end": "string",
                "duration_minutes": "int",
                "location": "string",
            },
            required_parameters=["title", "start"],
            tags=["calendar", "write"],
        ),
        create_event,
    ),
    (
        ToolSpec(
            name="update_event",
            description="Change an existing calendar event.",
            agent="calendar",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"uid": "string", "title": "string", "start": "string", "end": "string", "location": "string"},
            required_parameters=["uid"],
            tags=["calendar", "write"],
        ),
        update_event,
    ),
    (
        ToolSpec(
            name="cancel_event",
            description="Cancel a calendar event.",
            agent="calendar",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            allowed_devices=["laptop_main"],
            parameters={"uid": "string"},
            required_parameters=["uid"],
            tags=["calendar", "destructive"],
        ),
        cancel_event,
    ),
]
