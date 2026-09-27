"""Scheduler tools (v0.8).

Creating a schedule is a grant of future authority, so it is confirmed like any
other external action. The job stores the risk ceiling of the action it was
created for; the runner refuses to exceed it later.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from ..scheduler.runner import BUILTIN_ACTIONS, SchedulerRunner
from ..scheduler.store import ACTIVE, CANCELLED, MAX_SCHEDULABLE_RISK, ScheduleStore
from .base import ToolContext, fail, ok

log = get_logger("tools.scheduler")


def _store() -> ScheduleStore:
    return ScheduleStore()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
async def schedule_task(
    ctx: ToolContext,
    name: str = "",
    schedule: str = "",
    action: str = "reminder",
    params: str = "",
    message: str = "",
) -> Any:
    """Create a recurring or one-off scheduled job."""
    if not schedule:
        return fail(
            "schedule_task",
            "When should I run that? For example: 'every morning' or 'at 8 PM'.",
            device=ctx.device_id,
        )

    # `params` arrives as a JSON string from the planner; tolerate a dict too.
    if isinstance(params, dict):
        parsed_params: Dict[str, Any] = dict(params)
    elif params:
        try:
            parsed_params = json.loads(params)
        except (TypeError, ValueError):
            return fail("schedule_task", "I could not read those parameters.", device=ctx.device_id)
    else:
        parsed_params = {}

    if message and "message" not in parsed_params:
        parsed_params["message"] = message

    # --- resolve the action ------------------------------------------------
    if action in BUILTIN_ACTIONS:
        max_risk = int(RiskLevel.READ)
        label = action
    else:
        spec = ctx.tools.get(action) if hasattr(ctx, "tools") else None
        if spec is None:
            # Fall back to a fresh registry lookup.
            from ..tools import build_default_registry

            spec = build_default_registry().get(action)

        if spec is None:
            return fail(
                "schedule_task",
                f"I do not know an action called {action!r}.",
                device=ctx.device_id,
            )
        if int(spec.risk) > int(MAX_SCHEDULABLE_RISK):
            return fail(
                "schedule_task",
                (
                    f"{action} is a {spec.risk_label()} action, which is too risky to "
                    "run automatically. I can schedule it only if you run it yourself."
                ),
                device=ctx.device_id,
            )
        max_risk = int(spec.risk)
        label = action

    try:
        job = _store().create(
            name=name or label,
            schedule_text=schedule,
            action=action,
            params=parsed_params,
            max_risk=max_risk,
        )
    except ValueError as exc:
        return fail("schedule_task", str(exc), device=ctx.device_id)

    return ok(
        "schedule_task",
        f"Scheduled {job.name} — {job.recurrence.describe()}. "
        f"Next run {job.next_run.strftime('%a %d %b %H:%M') if job.next_run else 'unknown'}.",
        device=ctx.device_id,
        schedule_id=job.schedule_id,
        schedule=job.as_dict(),
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
async def list_schedules(ctx: ToolContext, include_cancelled: bool = False) -> Any:
    """List scheduled jobs."""
    jobs = _store().list(include_cancelled=include_cancelled)
    if not jobs:
        return ok("list_schedules", "You have nothing scheduled.", device=ctx.device_id, schedules=[])

    lines = [f"{j.name} — {j.recurrence.describe()}" for j in jobs[:5]]
    return ok(
        "list_schedules",
        f"{len(jobs)} scheduled job(s):\n" + "\n".join(lines),
        device=ctx.device_id,
        schedules=[j.as_dict() for j in jobs],
    )


async def get_schedule(ctx: ToolContext, schedule_id: str = "") -> Any:
    """One job, with its run history."""
    if not schedule_id:
        return fail("get_schedule", "Which schedule?", device=ctx.device_id)

    store = _store()
    job = store.get(schedule_id)
    if job is None:
        return fail("get_schedule", f"No schedule {schedule_id!r}.", device=ctx.device_id)

    return ok(
        "get_schedule",
        f"{job.name} — {job.recurrence.describe()}, {job.run_count} run(s), "
        f"{job.failure_count} failure(s).",
        device=ctx.device_id,
        schedule=job.as_dict(),
        history=store.history(schedule_id, limit=10),
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
async def pause_schedule(ctx: ToolContext, schedule_id: str = "") -> Any:
    if not schedule_id:
        return fail("pause_schedule", "Which schedule should I pause?", device=ctx.device_id)
    if not _store().pause(schedule_id):
        return fail("pause_schedule", f"No schedule {schedule_id!r}.", device=ctx.device_id)
    return ok("pause_schedule", "Paused.", device=ctx.device_id, schedule_id=schedule_id)


async def resume_schedule(ctx: ToolContext, schedule_id: str = "") -> Any:
    if not schedule_id:
        return fail("resume_schedule", "Which schedule should I resume?", device=ctx.device_id)
    if not _store().resume(schedule_id):
        return fail("resume_schedule", f"No schedule {schedule_id!r}.", device=ctx.device_id)
    return ok("resume_schedule", "Resumed.", device=ctx.device_id, schedule_id=schedule_id)


async def cancel_schedule(ctx: ToolContext, schedule_id: str = "") -> Any:
    if not schedule_id:
        return fail("cancel_schedule", "Which schedule should I cancel?", device=ctx.device_id)

    store = _store()
    job = store.get(schedule_id)
    if job is None:
        return fail("cancel_schedule", f"No schedule {schedule_id!r}.", device=ctx.device_id)

    store.cancel(schedule_id)
    return ok(
        "cancel_schedule",
        f"Cancelled {job.name}.",
        device=ctx.device_id,
        schedule_id=schedule_id,
    )


async def run_schedule_now(ctx: ToolContext, schedule_id: str = "") -> Any:
    """Run a job immediately, through the normal permission pipeline."""
    if not schedule_id:
        return fail("run_schedule_now", "Which schedule?", device=ctx.device_id)

    store = _store()
    job = store.get(schedule_id)
    if job is None:
        return fail("run_schedule_now", f"No schedule {schedule_id!r}.", device=ctx.device_id)

    # Needs the live core to evaluate and execute.
    core = getattr(ctx, "core", None)
    if core is None:
        return fail(
            "run_schedule_now",
            "I cannot run a schedule without the running core.",
            device=ctx.device_id,
        )

    outcome = await SchedulerRunner(store).run_job(core, job)
    return ok(
        "run_schedule_now",
        f"{job.name}: {outcome['outcome']} — {outcome.get('detail', '')[:120]}",
        device=ctx.device_id,
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# Briefing
# ---------------------------------------------------------------------------
async def daily_briefing(ctx: ToolContext, sections: str = "") -> Any:
    """Compose the daily briefing."""
    from ..core.briefing import DEFAULT_SECTIONS, build_briefing

    core = getattr(ctx, "core", None)
    if core is None:
        return fail("daily_briefing", "I cannot build a briefing right now.", device=ctx.device_id)

    requested: Optional[List[str]] = None
    if sections:
        requested = [s.strip() for s in sections.split(",") if s.strip()]
    elif ctx.settings.briefing_sections:
        requested = [s.strip() for s in ctx.settings.briefing_sections.split(",") if s.strip()]

    briefing = await build_briefing(core, sections=requested or list(DEFAULT_SECTIONS))
    return ok(
        "daily_briefing",
        briefing["text"],
        device=ctx.device_id,
        sections=briefing["sections"],
        unknown=briefing["unknown"],
    )


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------
SPECS = [
    (
        ToolSpec(
            name="schedule_task",
            description="Create a recurring or one-off scheduled job.",
            agent="scheduler",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={
                "name": "string",
                "schedule": "string",
                "action": "string",
                "params": "string",
                "message": "string",
            },
            required_parameters=["schedule"],
            tags=["scheduler", "write"],
        ),
        schedule_task,
    ),
    (
        ToolSpec(
            name="list_schedules",
            description="List scheduled jobs.",
            agent="scheduler",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"include_cancelled": "bool"},
            tags=["scheduler", "read"],
        ),
        list_schedules,
    ),
    (
        ToolSpec(
            name="get_schedule",
            description="Show one scheduled job and its run history.",
            agent="scheduler",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"schedule_id": "string"},
            required_parameters=["schedule_id"],
            tags=["scheduler", "read"],
        ),
        get_schedule,
    ),
    (
        ToolSpec(
            name="pause_schedule",
            description="Pause a scheduled job.",
            agent="scheduler",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"schedule_id": "string"},
            required_parameters=["schedule_id"],
            tags=["scheduler"],
        ),
        pause_schedule,
    ),
    (
        ToolSpec(
            name="resume_schedule",
            description="Resume a paused scheduled job.",
            agent="scheduler",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"schedule_id": "string"},
            required_parameters=["schedule_id"],
            tags=["scheduler"],
        ),
        resume_schedule,
    ),
    (
        ToolSpec(
            name="cancel_schedule",
            description="Cancel a scheduled job.",
            agent="scheduler",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            allowed_devices=["laptop_main"],
            parameters={"schedule_id": "string"},
            required_parameters=["schedule_id"],
            tags=["scheduler", "destructive"],
        ),
        cancel_schedule,
    ),
    (
        ToolSpec(
            name="run_schedule_now",
            description="Run a scheduled job immediately, with permission checks.",
            agent="scheduler",
            risk=RiskLevel.EXTERNAL,
            requires_confirmation=True,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"schedule_id": "string"},
            required_parameters=["schedule_id"],
            tags=["scheduler"],
        ),
        run_schedule_now,
    ),
    (
        ToolSpec(
            name="daily_briefing",
            description="Compose the daily briefing.",
            agent="scheduler",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"sections": "string"},
            tags=["scheduler", "briefing", "read"],
        ),
        daily_briefing,
    ),
]
