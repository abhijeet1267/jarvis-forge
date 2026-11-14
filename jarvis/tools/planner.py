"""The planner tools: a person's tasks and their day, from a sentence.

These are thin adapters over :class:`jarvis.core.planner_service.PlannerService`
-- the same service the ``/planner/*`` routes and the planner page use, so the
chat surface and the screen agree about what is on the list.

Two rules this module keeps:

* **A calendar that could not be read is not an empty day.** ``plan_day`` reads
  the calendar and reports an unreadable one as such, never as "you're free".
* **A task write is a task write.** ``add_task``/``complete_task`` are LOW tier
  and touch only the user's own task table; they never create calendar events.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from ..core.models import RiskLevel, ToolSpec
from .base import ToolContext, fail, ok


def _service():
    from ..core.orchestrator import JarvisCore  # noqa: F401  (type only)
    from ..core.planner_service import PlannerService

    return PlannerService()


def _parse_dt(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        from ..integrations.natural_time import parse_natural_time

        return parse_natural_time(value)
    except Exception:
        return None


async def start_day(ctx: ToolContext) -> Any:
    """Assemble the day: calendar, tasks, attention — and say what to do first.

    Composes real state only. Creates nothing; scheduling stays a separate,
    authorised step.
    """
    from ..integrations.calendar_provider import get_calendar_provider, read_status

    service = _service()
    now = datetime.now().astimezone()
    from datetime import timedelta

    start = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
    events: List[Any] = []
    failed = False
    try:
        result = get_calendar_provider(ctx.settings).list_events(start, start + timedelta(days=1), limit=100)
        if read_status(result).read_succeeded:
            events = list(result)
        else:
            failed = True
    except Exception:
        failed = True

    agenda = service.todays_agenda(now=now, calendar_events=events, calendar_read_failed=failed)
    nxt = agenda.get("next_action")
    lines = ["Starting your day:"]
    if failed:
        lines.append("  • Calendar: could not be read — this is NOT an empty day.")
    elif agenda["events"]:
        lines.append(f"  • {len(agenda['events'])} event(s) today:")
        for e in agenda["events"][:5]:
            lines.append(f"      {e.get('start', '')[11:16]}  {e.get('title') or '(untitled)'}")
    else:
        lines.append("  • Calendar: nothing scheduled.")
    for c in agenda["conflicts"]:
        lines.append(f"  • CONFLICT: ‘{c['first']}’ overlaps ‘{c['second']}’")
    if agenda["overdue"]:
        lines.append(f"  • {len(agenda['overdue'])} overdue task(s)")
    if agenda["blocked"]:
        lines.append(f"  • {len(agenda['blocked'])} blocked task(s)")
    if nxt:
        lines.append(f"  • Start with: {nxt['task']['title']}  ({nxt['rule']})")
    else:
        lines.append("  • Nothing pending.")
    lines.append("  (Assembled from real data; nothing was changed.)")
    return ok("start_day", "\n".join(lines), device=ctx.device_id, verified=True, agenda=agenda)


async def plan_day(ctx: ToolContext, date: str = "today") -> Any:
    """Show today's plan: calendar, tasks, conflicts, and what is next."""
    from ..integrations.calendar_provider import get_calendar_provider, read_status

    service = _service()
    now = datetime.now().astimezone()
    from datetime import timedelta

    if (date or "").strip().lower() in ("", "today"):
        day = now.date()
    else:
        parsed = _parse_dt(date)
        if parsed is None:
            return fail("plan_day", f"I could not understand the date {date!r}.", device=ctx.device_id)
        day = parsed.date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo)
    end = start + timedelta(days=1)

    events: List[Any] = []
    failed = False
    try:
        result = get_calendar_provider(ctx.settings).list_events(start, end, limit=100)
        if read_status(result).read_succeeded:
            events = list(result)
        else:
            failed = True
    except Exception:
        failed = True

    agenda = service.todays_agenda(day=day, now=now, calendar_events=events,
                                   calendar_read_failed=failed)

    lines = [f"Your plan for {day.strftime('%A %-d %B')}:"]
    if failed:
        lines.append("  • Calendar: could not be read — this is NOT an empty day.")
    elif agenda["events"]:
        lines.append("  • Calendar:")
        for e in agenda["events"]:
            when = e.get("start", "")[11:16]
            lines.append(f"      {when}  {e.get('title') or '(untitled)'}")
    else:
        lines.append("  • Calendar: nothing scheduled.")

    for conflict in agenda["conflicts"]:
        lines.append(f"  • CONFLICT: ‘{conflict['first']}’ overlaps ‘{conflict['second']}’")

    if agenda["overdue"]:
        lines.append("  • Overdue:")
        for t in agenda["overdue"]:
            lines.append(f"      {t['title']}")
    if agenda["blocked"]:
        lines.append("  • Blocked:")
        for t in agenda["blocked"]:
            lines.append(f"      {t['title']} (waiting on {', '.join(t['depends_on'])})")

    open_tasks = [t for t in agenda["tasks"] if t["status"] == "open"]
    if open_tasks:
        lines.append(f"  • {len(open_tasks)} open task(s)")
    nxt = agenda["next_action"]
    if nxt:
        lines.append(f"  • Next: {nxt['task']['title']}  ({nxt['rule']})")

    return ok("plan_day", "\n".join(lines), device=ctx.device_id, verified=True,
              agenda=agenda)


async def list_tasks(ctx: ToolContext, include_done: bool = False) -> Any:
    """List the person's tasks."""
    service = _service()
    tasks = service.list_tasks(include_done=include_done)
    if not tasks:
        return ok("list_tasks", "You have no open tasks.", device=ctx.device_id,
                  verified=True, tasks=[])
    open_refs = {t.task_ref for t in service.list_tasks()}
    now = datetime.now().astimezone()
    rows = [t.as_dict(now=now, open_refs=open_refs) for t in tasks]
    lines = [f"{len(rows)} task(s):"]
    for t in rows:
        bits = [t["title"]]
        if t["priority"] != "normal":
            bits.append(f"[{t['priority']}]")
        if t["overdue"]:
            bits.append("(overdue)")
        if t["blocked"]:
            bits.append("(blocked)")
        if t["due_at"]:
            bits.append(f"due {t['due_at'][:16].replace('T', ' ')}")
        lines.append("  • " + " ".join(bits))
    return ok("list_tasks", "\n".join(lines), device=ctx.device_id, verified=True,
              tasks=rows)


async def add_task(ctx: ToolContext, title: str = "", due: str = "",
                   priority: str = "normal", estimate_minutes: int = 0) -> Any:
    """Add a task to the person's list."""
    title = (title or "").strip()
    if not title:
        return fail("add_task", "What should I add? Give me the task's title.",
                    device=ctx.device_id)
    due_at = _parse_dt(due) if due else None
    if due and due_at is None:
        return fail("add_task", f"I could not understand the date {due!r}.", device=ctx.device_id)
    service = _service()
    task = service.create_task(title, priority=priority, due_at=due_at,
                               estimate_minutes=int(estimate_minutes or 0))
    when = f", due {due_at.strftime('%-d %b %H:%M')}" if due_at else ""
    return ok("add_task", f"Added “{task.title}”{when}.", device=ctx.device_id,
              verified=True, task=task.as_dict(now=datetime.now().astimezone()))


async def complete_task(ctx: ToolContext, task_ref: str = "", evidence: str = "") -> Any:
    """Mark a task done."""
    service = _service()
    ref = (task_ref or "").strip()
    if not ref:
        # Fall back to the planner's own "next" task, so "done" after a plan works.
        agenda = service.todays_agenda()
        nxt = agenda.get("next_action")
        if not nxt:
            return fail("complete_task", "There is no task to complete.", device=ctx.device_id)
        ref = nxt["task"]["task_ref"]
    task = service.complete_task(ref, evidence=evidence)
    if task is None:
        return fail("complete_task", f"I could not find task {ref!r}.", device=ctx.device_id)
    return ok("complete_task", f"Done: “{task.title}”.", device=ctx.device_id,
              verified=True, task=task.as_dict(now=datetime.now().astimezone()))


async def weekly_report(ctx: ToolContext, days: int = 7) -> Any:
    """Report work recorded in the last week, or say plainly that there was none."""
    import sys

    module = sys.modules.get("jarvis.api.app")
    if module is None:
        return fail("weekly_report", "The report service is not available.",
                    device=ctx.device_id)
    from ..core.orchestrator import JarvisCore

    core = JarvisCore()
    if not 1 <= int(days) <= 90:
        return fail("weekly_report", "I can report on 1 to 90 days.", device=ctx.device_id)

    from datetime import datetime as _dt, timedelta as _td

    now = _dt.now().astimezone()
    since = now - _td(days=int(days))
    done, overdue = [], []
    for task in core.planner_service.list_tasks(status="done"):
        when = task.done_at
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.astimezone()
        if when >= since:
            done.append(task.title)
    for task in core.planner_service.list_tasks():
        if task.is_overdue(now):
            overdue.append(task.title)

    if not done:
        return ok("weekly_report",
                  f"Nothing was recorded as completed in the last {days} day(s), so there "
                  "is no progress to report. Nothing was invented.",
                  device=ctx.device_id, completed_count=0, completed=[], verified=True)

    headline = f"In the last {days} day(s) you completed {len(done)} task(s): " + "; ".join(done[:5])
    if overdue:
        headline += f". Still overdue: " + "; ".join(overdue[:3])
    return ok("weekly_report", headline, device=ctx.device_id,
              completed_count=len(done), completed=done, overdue=overdue, verified=True)

async def schedule_coding(ctx: ToolContext, goal: str = "", duration_minutes: int = 90) -> Any:
    """Book a coding block into a real free slot, as a planner task."""
    if not str(goal).strip():
        return fail("schedule_coding", "What should the coding block be for?",
                    device=ctx.device_id)
    from ..tools.calendar import find_free_time

    found = await find_free_time(ctx, date="today", duration_minutes=int(duration_minutes))
    slot = (found.output or {}).get("slot") if found.ok else None
    if not slot:
        return fail("schedule_coding",
                    getattr(found, "error", "")
                    or "I could not find a free slot, so I did not book one.",
                    device=ctx.device_id, slot=None)

    from datetime import datetime as _dt
    from ..core.orchestrator import JarvisCore

    start = _dt.fromisoformat(slot["start"])
    task = JarvisCore().planner_service.create_task(
        str(goal), detail=f"Focused coding block ({duration_minutes} min)",
        priority="high", start_at=start, estimate_minutes=int(duration_minutes),
        source="coding")
    return ok("schedule_coding",
              f"Booked {duration_minutes} minutes for {goal} at "
              f"{start.strftime('%H:%M')}. It is a planner task; nothing was added "
              "to your calendar.",
              device=ctx.device_id, task=task.as_dict(), slot=slot, verified=True)

async def reschedule_unfinished(ctx: ToolContext, days: int = 5) -> Any:
    """Move timed, unfinished work into real free time."""
    import sys

    module = sys.modules.get("jarvis.api.app")
    if module is None:
        return fail("reschedule_unfinished", "The planner service is not available.",
                    device=ctx.device_id)
    result = await module.reschedule_unfinished({"days": int(days)}, None)
    if not result.get("moved_count"):
        return ok("reschedule_unfinished",
                  result.get("note") or "Nothing needed moving.",
                  device=ctx.device_id, moved=[], verified=True)
    titles = ", ".join(m["title"] for m in result["moved"][:4])
    return ok("reschedule_unfinished",
              f"Moved {result['moved_count']} unfinished task(s) into free time: {titles}.",
              device=ctx.device_id, moved=result["moved"], verified=True)

async def begin_work(ctx: ToolContext) -> Any:
    """Plan the day, open the next task's project, and offer the agent prompt."""
    import sys

    module = sys.modules.get("jarvis.api.app")
    if module is None:
        return fail("begin_work", "The planner service is not available.",
                    device=ctx.device_id)
    result = await module.begin_work({}, None)

    opened = result.get("opened_project") or {}
    parts = [result.get("headline") or "Here is your plan."]
    if opened.get("attempted"):
        parts.append(("Opened: " if opened.get("ok") else "Could not open: ")
                     + str(opened.get("message") or ""))
    agent = result.get("agent") or {}
    if agent.get("prompt"):
        parts.append(f"Next prompt for {agent.get('agent') or 'the agent'}: {agent['prompt']}")
    return ok("begin_work", "  ".join(parts), device=ctx.device_id,
              opened_project=opened, agent=agent, verified=bool(opened.get("ok")))

async def cancel_task(ctx: ToolContext, task_ref: str = "", title: str = "") -> Any:
    """Cancel a planner task by reference or by its title.

    The task is *cancelled*, not erased: its row stays so the record of what was
    planned is not silently rewritten. Deleting a title by pattern-matching was
    also the wrong shape — "delete the task to call Sam" was becoming `delete_file`,
    a proposal to delete a file called "the task to call Sam".
    """
    service = _service()
    ref = str(task_ref).strip()
    if not ref:
        wanted = str(title).strip().lower()
        if not wanted:
            return fail("cancel_task", "Which task should I cancel?", device=ctx.device_id)
        matches = [t for t in service.list_tasks()
                   if wanted in t.title.lower() or t.title.lower() in wanted]
        if not matches:
            return fail("cancel_task", f"No open task matches {title!r}.",
                        device=ctx.device_id)
        if len(matches) > 1:
            names = "; ".join(t.title for t in matches[:3])
            return fail("cancel_task",
                        f"{len(matches)} tasks match: {names}. Which one?",
                        device=ctx.device_id)
        ref = matches[0].task_ref
    task = service.cancel_task(ref)
    if task is None:
        return fail("cancel_task", f"No task {ref!r}.", device=ctx.device_id)
    return ok("cancel_task", f"Cancelled “{task.title}”.", device=ctx.device_id,
              task=task.as_dict(), verified=True)

SPECS = [
    (
        ToolSpec(
            name="start_day",
            description="Assemble your day: calendar, tasks and what to do first.",
            agent="planner",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["planner", "read"],
        ),
        start_day,
    ),
    (
        ToolSpec(
            name="cancel_task",
            description="Cancel a planner task, by reference or by its title. It is "
                        "cancelled, not erased.",
            agent="planner",
            risk=RiskLevel.LOW,
            requires_confirmation=True,
            reversible=False,
            allowed_devices=["laptop_main"],
            parameters={"task_ref": "string", "title": "string"},
            tags=["planner"],
        ),
        cancel_task,
    ),
    (
        ToolSpec(
            name="begin_work",
            description=("Plan the day, open the next task's project in the editor, and "
                         "offer the coding-agent prompt. It does not start the agent."),
            agent="planner",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["planner"],
        ),
        begin_work,
    ),
    (
        ToolSpec(
            name="reschedule_unfinished",
            description="Move timed, unfinished tasks into real free slots.",
            agent="planner",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"days": "int"},
            tags=["planner"],
        ),
        reschedule_unfinished,
    ),
    (
        ToolSpec(
            name="reschedule_unfinished",
            description="Move timed, unfinished tasks into real free slots.",
            agent="planner",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"days": "int"},
            tags=["planner"],
        ),
        reschedule_unfinished,
    ),
    (
        ToolSpec(
            name="schedule_coding",
            description="Book a coding block into a real free slot as a planner task.",
            agent="planner",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"goal": "string", "duration_minutes": "int"},
            required_parameters=["goal"],
            tags=["planner", "coding"],
        ),
        schedule_coding,
    ),
    (
        ToolSpec(
            name="weekly_report",
            description="Report work completed in the last week, from recorded tasks.",
            agent="planner",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"days": "int"},
            tags=["planner", "report", "read"],
        ),
        weekly_report,
    ),
    (
        ToolSpec(
            name="plan_day",
            description="Show today's plan: calendar, tasks, conflicts and what is next.",
            agent="planner",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"date": "string"},
            tags=["planner", "read"],
        ),
        plan_day,
    ),
    (
        ToolSpec(
            name="list_tasks",
            description="List your tasks.",
            agent="planner",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"include_done": "bool"},
            tags=["planner", "read"],
        ),
        list_tasks,
    ),
    (
        ToolSpec(
            name="add_task",
            description="Add a task to your list.",
            agent="planner",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"title": "string", "due": "string", "priority": "string",
                        "estimate_minutes": "int"},
            required_parameters=["title"],
            tags=["planner", "write"],
        ),
        add_task,
    ),
    (
        ToolSpec(
            name="complete_task",
            description="Mark a task done.",
            agent="planner",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"task_ref": "string", "evidence": "string"},
            tags=["planner", "write"],
        ),
        complete_task,
    ),
]