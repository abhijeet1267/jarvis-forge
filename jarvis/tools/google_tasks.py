"""Google Tasks as tools: list, create, complete.

Why this module exists: the Calendar already reaches the planner through the
``CalendarProvider`` contract, but Tasks did not reach the assistant at all.
"Add milk to my Google Tasks" had no tool behind it, so the model could only
mislay it in the local planner -- the duplicate-reminder problem this
integration is meant to prevent. These three tools close that gap by speaking
to :class:`GoogleTasksClient`, which is the same adapter the HTTP routes use.

Two rules are inherited rather than re-stated:

* A failed read is a failure. ``list_google_tasks`` returns ``fail`` with the
  provider's own status and reason; it never returns "no tasks" for a read
  that could not happen.
* These are Google records, not local ones. Creating or completing one does
  not touch the planner database, so the same task cannot become two
  reminders. Each result says so, so a caller cannot mistake it for a local
  task.

Tasks is a *remote write*, so both write tools are ``EXTERNAL`` risk and
require confirmation: a phone asking for one gets a confirmation card, and the
policy engine's self-approval guard means that phone cannot then approve its
own request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from .base import ActionResult, ToolContext, fail, ok

log = get_logger("tools.google_tasks")

def _client(settings: Any):
    from ..integrations.google_tasks import GoogleTasksClient

    return GoogleTasksClient(settings)

def _parse_due(raw: str) -> tuple[Optional[datetime], str]:
    """A due *date* (Google Tasks has no time of day), or a reason it is not one."""
    text = (raw or "").strip()
    if not text:
        return None, ""
    try:
        return datetime.fromisoformat(text[:10]), ""
    except ValueError:
        return None, f"I could not read “{text}” as a date. Use YYYY-MM-DD."

async def list_google_tasks(ctx: ToolContext, show_completed: bool = False,
                            list_id: str = "", limit: int = 100,
                            overdue_only: bool = False) -> Any:
    """Read the connected account's Google Tasks.

    ``tasks()`` returns a ``ProviderResults``: a list that also carries the
    provider's status. ``read_succeeded`` is what separates "no open tasks"
    from "Google could not be asked", and only the former may be reported as
    an empty list.

    ``overdue_only`` filters by the *calendar day*, not by instant: Google
    Tasks stores a date with no time, so a task due today is due today at
    09:00 as much as at 23:00, and comparing it against a timestamp would
    call it overdue five minutes after midnight.
    """
    result = _client(ctx.settings).tasks(
        list_id=list_id, show_completed=bool(show_completed), limit=int(limit or 100))
    if not result.read_succeeded:
        return fail(
            "list_google_tasks",
            result.detail or "Google Tasks could not be read.",
            device=ctx.device_id,
            provider_status=result.status.value,
            tasks=[], read_succeeded=False,
        )
    tasks: List[dict] = [t.as_dict() for t in result]
    if overdue_only:
        today = datetime.now().astimezone().date()
        overdue = []
        for task in tasks:
            if task.get("done"):
                continue
            due = str(task.get("due") or "")
            try:
                due_day = datetime.fromisoformat(due[:10]).date()
            except ValueError:
                continue
            if due_day < today:
                task = dict(task)
                task["days_overdue"] = (today - due_day).days
                overdue.append(task)
        if not overdue:
            return ok(
                "list_google_tasks",
                ("No Google task is overdue." if tasks
                 else "No open Google tasks, so nothing is overdue."),
                device=ctx.device_id, provider="google_tasks",
                tasks=[], overdue=[], read_succeeded=True,
                provider_status=result.status.value, source="google", verified=True,
            )
        overdue.sort(key=lambda t: str(t.get("due") or ""))
        names = ", ".join(
            f"“{t.get('title')}” (due {t.get('due')})" for t in overdue[:8])
        return ok(
            "list_google_tasks",
            f"{len(overdue)} overdue Google task(s): {names}.",
            device=ctx.device_id, provider="google_tasks",
            tasks=overdue, overdue=overdue, read_succeeded=True,
            provider_status=result.status.value, source="google", verified=True,
        )
    titles = ", ".join(str(t.get("title") or "") for t in tasks[:8])
    if not tasks:
        message = ("No open Google tasks." if not show_completed
                   else "No tasks in that Google list.")
    else:
        message = f"{len(tasks)} Google task(s): {titles}"
    return ok("list_google_tasks", message, device=ctx.device_id,
              provider="google_tasks", tasks=tasks, read_succeeded=True,
              source="google", verified=True)

async def add_google_task(ctx: ToolContext, title: str = "", notes: str = "",
                          due: str = "", list_id: str = "") -> Any:
    """Create a task in Google Tasks (the user's real task list)."""
    clean = (title or "").strip()
    if not clean:
        return fail("add_google_task", "A task needs a title.", device=ctx.device_id)
    due_at, due_problem = _parse_due(due)
    if due_problem:
        return fail("add_google_task", due_problem, device=ctx.device_id)
    result = _client(ctx.settings).create(
        clean, notes=notes or "", due=due_at, list_id=list_id, source="Vantage")
    if not result.get("ok"):
        return fail(
            "add_google_task",
            result.get("error") or "Google did not confirm the task was created.",
            device=ctx.device_id,
            provider_status=result.get("status") or "error",
        )
    task = result.get("task") or {}
    return ok(
        "add_google_task",
        f"Added “{clean}” to Google Tasks.",
        device=ctx.device_id,
        provider="google_tasks", task=task, source="google",
        # Verified by Google's own response, not by our request being sent.
        verified=bool(task.get("id")),
        note="This is a Google task. Nothing was copied into the local planner.",
    )

async def complete_google_task(ctx: ToolContext, task_ref: str = "", list_id: str = "") -> Any:
    """Mark a Google task completed, verified against what Google reports.

    ``task_ref`` may be an id or the task's title. A person says "mark buy milk
    done", not Google's opaque id, so a title is resolved against the live list
    first. Ambiguity is refused rather than guessed: completing the wrong task
    is a real change in the user's list, and "close enough" is not a reason to
    pick one.
    """
    ref = (task_ref or "").strip()
    if not ref:
        return fail("complete_google_task", "Which task should I complete?",
                    device=ctx.device_id)

    client = _client(ctx.settings)
    task_id, problem = _resolve_task_id(client, ref, list_id)
    if problem:
        return fail("complete_google_task", problem, device=ctx.device_id)
    result = client.complete(task_id, list_id=list_id)
    if not result.get("ok"):
        return fail(
            "complete_google_task",
            result.get("error") or "Google did not confirm the task was completed.",
            device=ctx.device_id,
            provider_status=result.get("status") or "error",
        )
    task = result.get("task") or {}
    return ok("complete_google_task",
              f"Completed “{task.get('title') or ref}” in Google Tasks.",
              device=ctx.device_id, provider="google_tasks", task=task,
              source="google", verified=True)


def _resolve_task_id(client: Any, ref: str, list_id: str = "") -> tuple[str, str]:
    """An id for ``ref``, or ``("", reason)`` when it cannot be resolved safely.

    An id-shaped reference is used as-is (Google ids do not contain spaces);
    anything else is matched case-insensitively against open tasks. Exactly one
    match is required: zero means "no such task", several means the sentence
    did not identify which one, and neither may be guessed.
    """
    if " " not in ref and len(ref) >= 8:
        return ref, ""
    wanted = ref.casefold()
    listing = client.tasks(list_id=list_id, show_completed=False, limit=100)
    if not listing.read_succeeded:
        # Never resolve against a list that could not be read: "no such task"
        # for an unreadable list is a false answer of a different kind.
        return "", (listing.detail or "Google Tasks could not be read, so I did not "
                                      "change anything.")
    matches = [t for t in listing if (t.title or "").casefold() == wanted]
    if not matches:
        partial = [t for t in listing if wanted in (t.title or "").casefold()]
        if len(partial) == 1:
            return partial[0].id, ""
        if len(partial) > 1:
            names = ", ".join(f"“{t.title}”" for t in partial[:5])
            return "", f"Several Google tasks match “{ref}”: {names}. Say which one."
        return "", (f"No open Google task is called “{ref}”. I did not change anything.")
    if len(matches) > 1:
        return "", (f"{len(matches)} open Google tasks are called “{ref}”. "
                    "Complete one by its exact title, or rename them first.")
    return matches[0].id, ""

SPECS = [
    (
        ToolSpec(
            name="list_google_tasks",
            description="List tasks from the connected Google Tasks account.",
            agent="calendar",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"show_completed": "bool", "list_id": "string", "limit": "int",
                        "overdue_only": "bool"},
            tags=["google", "tasks", "read"],
        ),
        list_google_tasks,
    ),
    (
        ToolSpec(
            name="add_google_task",
            description="Add a task to Google Tasks (not the local planner list).",
            agent="calendar",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=True,
            # Tasks are the user's own list and creating one is the point of
            # the integration; still EXTERNAL because it leaves the machine.
            allowed_devices=["laptop_main"],
            parameters={"title": "string", "notes": "string", "due": "string",
                        "list_id": "string"},
            required_parameters=["title"],
            tags=["google", "tasks", "write"],
        ),
        add_google_task,
    ),
    (
        ToolSpec(
            name="complete_google_task",
            description="Mark a task completed in Google Tasks.",
            agent="calendar",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"task_ref": "string", "list_id": "string"},
            required_parameters=["task_ref"],
            tags=["google", "tasks", "write"],
        ),
        complete_google_task,
    ),
]