"""Daily briefing (v0.8).

Composes the morning summary from configurable sections. Two rules:

* **A section that cannot be produced says so.** If the calendar is unreachable
  or an integration is unconfigured, the briefing reports that instead of
  silently omitting the section — otherwise an empty briefing looks like good
  news.
* **Nothing is invented.** Every line comes from a real store or tool result.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("core.briefing")

#: Sections in the order they appear. The user can enable a subset.
DEFAULT_SECTIONS = [
    "datetime",
    "calendar",
    "reminders",
    "tasks",
    "approvals",
    "failures",
    "email",
    "projects",
    "system",
]

SECTION_TITLES = {
    "datetime": "Date and time",
    "calendar": "Schedule",
    "reminders": "Reminders",
    "tasks": "Jobs",
    "approvals": "Waiting on you",
    "failures": "Failures and recoveries",
    "email": "Email",
    "projects": "Projects",
    "system": "System",
}


async def _section_datetime(core) -> List[str]:
    now = datetime.now().astimezone()
    return [now.strftime("%A %d %B %Y, %H:%M")]


async def _section_calendar(core) -> List[str]:
    from ..integrations.calendar_provider import get_calendar_provider

    provider = get_calendar_provider()
    now = datetime.now().astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        events = provider.list_events(start, start + timedelta(days=1), limit=50)
    except Exception as exc:
        return [f"calendar unavailable: {exc}"]

    if not events:
        return ["Nothing scheduled today."]
    return [
        f"{e.start.strftime('%H:%M')}–{e.end.strftime('%H:%M')}  {e.title}"
        + (f"  ({e.location})" if e.location else "")
        for e in events
    ]


async def _section_reminders(core) -> List[str]:
    from ..scheduler.store import ScheduleStore

    store = ScheduleStore()
    reminders = [j for j in store.list() if j.action == "reminder" and j.status == "active"]
    if not reminders:
        return []
    return [f"{j.name} — {j.recurrence.describe()}" for j in reminders]


async def _section_tasks(core) -> List[str]:
    jobs = core.tasks.list(limit=10)
    if not jobs:
        return []
    running = [j for j in jobs if j["status"] == "running"]
    lines = []
    if running:
        lines.append(f"{len(running)} job(s) in progress.")
    failed = core.tasks.failed_jobs(limit=5)
    if failed:
        lines.append(f"{len(failed)} job(s) failed.")
    if not lines:
        lines.append(f"{len(jobs)} recent job(s), none running.")
    return lines


async def _section_approvals(core) -> List[str]:
    pending = core.policy.pending()
    if not pending:
        return ["Nothing waiting for approval."]
    return [
        f"{p.get('tool', 'action')} — {p.get('reason', 'needs confirmation')}"
        for p in pending[:5]
    ]


async def _section_failures(core) -> List[str]:
    stats = core.recovery.failure_stats()
    if stats.get("ledger_error"):
        # "No failures recorded." is the worst possible sentence for a ledger
        # that could not be read: it is indistinguishable from a healthy system.
        lines = [f"failure ledger unavailable: {stats['ledger_error']}"]
    else:
        lines = []
    if stats.get("unwritten"):
        lines.append(
            f"{stats['unwritten']} failure record(s) could not be written"
            + (f" (last: {stats['last_error']})" if stats.get("last_error") else "")
        )
    if lines:
        return lines
    if not stats["total_failures"]:
        return ["No failures recorded."]

    lines = [
        f"{stats['total_failures']} failure(s), {stats['total_recovered']} recovered "
        f"({int(stats['recovery_rate'] * 100)}%)."
    ]
    for tool, info in list(stats["by_tool"].items())[:3]:
        lines.append(f"  {tool}: {info['failures']} failure(s)")
    return lines


async def _section_email(core) -> List[str]:
    from ..integrations.email_provider import get_email_provider

    provider = get_email_provider()
    try:
        unread = provider.search(limit=5, unread_only=True)
    except Exception as exc:
        return [f"mailbox unavailable: {exc}"]

    if not unread:
        return ["No unread mail."]
    lines = [f"{len(unread)} unread message(s)."]
    for message in unread[:3]:
        subject = message.subject or "(no subject)"
        lines.append(f"  {message.sender}: {subject}")
    return lines


async def _section_projects(core) -> List[str]:
    try:
        summary = core.graph.summary()
    except Exception:
        return []
    if not summary:
        return []
    if isinstance(summary, dict):
        names = list(summary.get("projects", []))[:3]
        if names:
            return [", ".join(str(n) for n in names)]
    return []


async def _section_system(core) -> List[str]:
    from ..tools.system import battery_status, system_status
    from ..tools.base import ToolContext

    ctx = ToolContext()
    lines: List[str] = []
    try:
        status = await system_status(ctx)
        if status.ok:
            lines.append(status.message)
    except Exception:
        pass
    try:
        battery = await battery_status(ctx)
        if battery.ok:
            lines.append(battery.message)
    except Exception:
        pass
    return lines or ["System status unavailable."]


BUILDERS = {
    "datetime": _section_datetime,
    "calendar": _section_calendar,
    "reminders": _section_reminders,
    "tasks": _section_tasks,
    "approvals": _section_approvals,
    "failures": _section_failures,
    "email": _section_email,
    "projects": _section_projects,
    "system": _section_system,
}


async def build_briefing(
    core,
    sections: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Compose the briefing.

    Returns ``{"text": ..., "sections": {name: [lines]}, "enabled": [...]}``.
    An unknown section name is reported rather than silently dropped.
    """
    enabled = list(sections) if sections else list(DEFAULT_SECTIONS)
    unknown = [s for s in enabled if s not in BUILDERS]

    rendered: Dict[str, List[str]] = {}
    for name in enabled:
        builder = BUILDERS.get(name)
        if builder is None:
            continue
        try:
            lines = await builder(core)
        except Exception as exc:
            # A broken section must not take the whole briefing down.
            lines = [f"{name} unavailable: {exc}"]
        if lines:
            rendered[name] = lines

    now = datetime.now().astimezone()
    parts = [f"JARVIS DAILY BRIEFING — {now.strftime('%A %d %B %Y')}", ""]
    for name, lines in rendered.items():
        parts.append(SECTION_TITLES.get(name, name.title()))
        parts.extend(f"  {line}" for line in lines)
        parts.append("")

    if unknown:
        parts.append(f"(Sections I do not have: {', '.join(unknown)})")

    return {
        "text": "\n".join(parts).rstrip(),
        "sections": rendered,
        "enabled": [s for s in enabled if s in BUILDERS],
        "unknown": unknown,
        "generated_at": now.isoformat(),
    }


def describe_briefing() -> Dict[str, Any]:
    return {
        "available_sections": list(DEFAULT_SECTIONS),
        "default_sections": list(DEFAULT_SECTIONS),
    }
