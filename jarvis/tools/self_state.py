"""Vantage answering questions about itself (build prompt §14, §16, §30).

Every handler here reads state the running process already owns through ``ctx.core`` -- the
same objects ``/confirmations``, ``/health``, ``/audit`` and ``/tasks`` are built from. There
is no second store, because a second store is a second answer waiting to disagree.

The deliberate boundaries:

* **Empty is an answer, not a failure.** "Nothing is waiting for you" is complete; reporting a
  read failure as an empty queue is how an outage gets mistaken for an idle machine.
* **A stale confirmation is not pending.** The policy engine expires rows on read, and this
  module goes through it rather than querying the table, so the TTL is one rule in one place.
* **Stopping is verified by reading the state back.** ``tasks.cancel`` returns a report; the
  claim here is made from the row's status after the call, not from the call returning.
* **A schedule is not a task.** ``cancel_schedule`` stops something that would have run later;
  this stops the execution happening now. They are different capabilities and neither routes
  to the other.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from .base import ToolContext, fail, ok, unverified

log = get_logger("tools.self_state")

#: Job states in which work is still in front of Vantage. Mirrors the vocabulary
#: ``emergency_stop`` uses in ``jarvis/tools/system.py`` so the two stop paths agree.
ACTIVE_STATES = ("running", "pending", "queued", "paused", "awaiting_confirmation")


def _no_core(tool: str, ctx: ToolContext) -> Any:
    """Refuse rather than reconstruct.

    Building a fresh registry/engine to answer "what is your status" would report the state of
    the object this handler just created, not the state of the running system.
    """
    return fail(tool, "I could not reach my own running state, so I will not report it.",
                device=ctx.device_id)


def _age_seconds(iso: str) -> Optional[int]:
    from datetime import datetime, timezone

    try:
        then = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - then).total_seconds()))


# ---------------------------------------------------------------------------
# pending approvals
# ---------------------------------------------------------------------------

async def get_pending_confirmations(ctx: ToolContext, limit: int = 10) -> Any:
    """What is waiting for a decision, from the engine that owns the waiting."""
    core = getattr(ctx, "core", None)
    if core is None:
        return _no_core("get_pending_confirmations", ctx)

    rows: List[Dict[str, Any]] = core.policy.pending()
    shown = rows[:max(1, int(limit or 10))]
    items = []
    for row in shown:
        age = _age_seconds(row.get("created_at", ""))
        items.append({
            "confirmation_id": row.get("confirmation_id", ""),
            "tool": row.get("tool", ""),
            "device": row.get("device", ""),
            "risk": int(row.get("risk", 0)),
            "age_seconds": age,
            "expires_in_seconds": (
                max(0, int(core.settings.confirm_timeout) - age)
                if age is not None else None),
        })

    if not rows:
        return ok("get_pending_confirmations",
                  "Nothing is waiting for you right now.",
                  device=ctx.device_id, count=0, confirmations=[])

    words = ", ".join(f"{i['tool']} ({i['confirmation_id']})" for i in items)
    ttl = int(core.settings.confirm_timeout)
    message = (f"{len(rows)} action(s) waiting for a decision: {words}. "
               f"Each expires {ttl} seconds after it was created.")
    if len(rows) > len(items):
        message += f" Showing the first {len(items)}."
    return ok("get_pending_confirmations", message, device=ctx.device_id,
              count=len(rows), confirmations=items, ttl_seconds=ttl)


# ---------------------------------------------------------------------------
# health and mode
# ---------------------------------------------------------------------------

async def get_vantage_status(ctx: ToolContext) -> Any:
    """Are you running / what mode are you in -- answered from the live process."""
    core = getattr(ctx, "core", None)
    if core is None:
        return _no_core("get_vantage_status", ctx)

    tools = len(core.tools.names())
    agents = len({s.agent for s in core.tools.all()})
    scheduler = core.scheduler.describe()
    pending = len(core.policy.pending())
    llm_enabled = bool(core.settings.llm_enabled)
    devices = [d for d in core.devices.all()]
    online = [d for d in devices if getattr(d, "online", False)]

    mode = "a configured model" if llm_enabled else "Basic Mode: deterministic rules, no model"
    lines = [
        f"Vantage is running in {mode}.",
        f"{tools} capabilities registered across {agents} agents, "
        f"{pending} waiting for approval.",
        f"Scheduler {'is running' if scheduler.get('running') else 'is NOT running'}"
        f" ({scheduler.get('total', 0)} job(s) defined).",
    ]
    if online:
        lines.append(f"{len(online)} device(s) connected: "
                     + ", ".join(sorted(d.device_id for d in online)) + ".")
    elif devices:
        lines.append("No paired device is connected right now.")
    else:
        lines.append("No device is paired yet.")

    return ok("get_vantage_status", " ".join(lines), device=ctx.device_id,
              running=True, tools=tools, agents=agents, llm_enabled=llm_enabled,
              scheduler_running=bool(scheduler.get("running")),
              scheduler_jobs=scheduler.get("total", 0), pending_confirmations=pending,
              devices=len(devices), devices_online=len(online))


# ---------------------------------------------------------------------------
# recent actions
# ---------------------------------------------------------------------------

async def get_recent_activity(ctx: ToolContext, limit: int = 8) -> Any:
    """What happened recently, in the words the ledger recorded."""
    core = getattr(ctx, "core", None)
    if core is None:
        return _no_core("get_recent_activity", ctx)

    wanted = max(1, min(int(limit or 8), 25))
    rows = core.audit.recent(wanted)
    if not rows:
        return ok("get_recent_activity", "I have no recorded actions yet.",
                  device=ctx.device_id, count=0, actions=[])

    actions = [{
        "tool": row.get("tool", ""),
        "result": row.get("result", ""),
        "intent": row.get("intent", ""),
        "at": row.get("created_at", ""),
        "target": row.get("target", ""),
    } for row in rows]

    counts: Dict[str, int] = {}
    for row in actions:
        counts[row["result"] or "unknown"] = counts.get(row["result"] or "unknown", 0) + 1
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    newest = actions[0]
    return ok("get_recent_activity",
              f"{len(actions)} most recent action(s): {summary}. "
              f"Last was {newest['tool'] or 'unknown'} -> {newest['result'] or 'unknown'}.",
              device=ctx.device_id, count=len(actions), actions=actions)


# ---------------------------------------------------------------------------
# the current task, and stopping it
# ---------------------------------------------------------------------------

#: The vocabulary is ``jarvis/core/models.ActionStatus``. "success" and "done" are not step
#: statuses in this codebase -- the first version of this module counted finished steps with
#: them, so every task report said "0 of N step(s) done" no matter what had finished.
SUCCEEDED_STATES = ("succeeded",)
UNFINISHED_STATES = ("pending", "running", "awaiting_confirmation")


def _active_job(core: Any) -> Optional[Dict[str, Any]]:
    """The newest job whose steps are still in front of Vantage."""
    for job in core.tasks.list(limit=25):
        if str(job.get("status") or "") in ACTIVE_STATES:
            return job
    return None


def _step_summary(core: Any, job_id: str) -> List[Dict[str, Any]]:
    rows = core.db.query(
        "SELECT name, status, detail FROM task_steps WHERE job_id = ? ORDER BY seq",
        (job_id,))
    return [{"name": r["name"], "status": r["status"], "detail": r["detail"] or ""}
            for r in rows]


async def get_current_task(ctx: ToolContext) -> Any:
    """What are you doing -- answered from the job table, not from the request being read."""
    core = getattr(ctx, "core", None)
    if core is None:
        return _no_core("get_current_task", ctx)

    job = _active_job(core)
    if job is None:
        waiting = len(core.policy.pending())
        if waiting:
            return ok("get_current_task",
                "Nothing is executing right now, but "
                f"{waiting} action(s) {'is' if waiting == 1 else 'are'} waiting for your "
                "approval -- ask me for pending approvals to see what they are.",
                device=ctx.device_id, running=False, job_id="", steps=[],
                pending_confirmations=waiting)
        return ok("get_current_task", "Nothing is running right now.",
                  device=ctx.device_id, running=False, job_id="", steps=[])

    job_id = str(job.get("job_id") or "")
    steps = _step_summary(core, job_id)
    done = sum(1 for s in steps if s["status"] in SUCCEEDED_STATES)
    waiting = [s for s in steps if s["status"] == "awaiting_confirmation"]
    line = (f"Current task: \"{job.get('goal') or job_id}\" -- {job.get('status')}, "
            f"{done} of {len(steps)} step(s) done.")
    if waiting:
        line += (f" {len(waiting)} step(s) are waiting for an approval: "
                 + ", ".join(s["name"] for s in waiting) + ".")
    return ok("get_current_task", line, device=ctx.device_id, running=True,
              job_id=job_id, status=str(job.get("status") or ""), goal=job.get("goal", ""),
              steps=steps)


async def stop_current_task(ctx: ToolContext) -> Any:
    """Cancel the task in flight, then read its state back before saying it stopped."""
    core = getattr(ctx, "core", None)
    if core is None:
        return _no_core("stop_current_task", ctx)

    job = _active_job(core)
    if job is None:
        return ok("stop_current_task",
                  "Nothing was stopped: no task is running.",
                  device=ctx.device_id, cancelled=False, job_id="")

    job_id = str(job.get("job_id") or "")
    goal = str(job.get("goal") or job_id)
    report = core.tasks.cancel(job_id)

    # Verified against the row, not the call: the report says the request was accepted, and
    # only the state says the job is no longer going to run.
    after = core.tasks.get(job_id)
    status = str((after or {}).get("status") or "")
    steps = _step_summary(core, job_id)
    ran = [s["name"] for s in steps if s["status"] in SUCCEEDED_STATES]
    not_run = [s["name"] for s in steps if s["status"] in UNFINISHED_STATES]

    if status == "cancelled":
        detail = f"Steps that already ran are not undone ({len(ran)} done)."
        if not_run:
            detail += " Not started or interrupted: " + ", ".join(not_run) + "."
        return ok("stop_current_task",
                  f"Stopped \"{goal}\" -- the job is cancelled. {detail}",
                  device=ctx.device_id, cancelled=True, job_id=job_id, status=status,
                  verified=True, ran=ran, not_run=not_run)

    reason = getattr(report, "reason", "") or "the job did not move to cancelled"
    return unverified("stop_current_task",
        f"I asked to stop \"{goal}\", but its state now reads {status!r} -- {reason}. "
        "It may still be running.",
        device=ctx.device_id, cancelled=False, job_id=job_id, status=status, verified=False)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

SPECS: List[Tuple[ToolSpec, Any]] = [
    (
        ToolSpec(
            name="get_pending_confirmations",
            description="List the actions waiting for the user's approval, with their age "
                        "against the confirmation TTL.",
            agent="laptop",
            risk=RiskLevel.READ,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"limit": "integer"},
            required_parameters=[],
            tags=["vantage", "read", "self", "confirmation"],
        ),
        get_pending_confirmations,
    ),
    (
        ToolSpec(
            name="get_vantage_status",
            description="Report Vantage's own operating state: mode, capability count, "
                        "scheduler, pending approvals and connected devices.",
            agent="laptop",
            risk=RiskLevel.READ,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={},
            required_parameters=[],
            tags=["vantage", "read", "self", "health"],
        ),
        get_vantage_status,
    ),
    (
        ToolSpec(
            name="get_recent_activity",
            description="Read back the most recent audit rows exactly as recorded, including "
                        "denied, blocked and unverified results.",
            agent="laptop",
            risk=RiskLevel.READ,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"limit": "integer"},
            required_parameters=[],
            tags=["vantage", "read", "self", "audit"],
        ),
        get_recent_activity,
    ),
    (
        ToolSpec(
            name="get_current_task",
            description="Report the task in flight now, with per-step state, or say that "
                        "nothing is running.",
            agent="laptop",
            risk=RiskLevel.READ,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={},
            required_parameters=[],
            tags=["vantage", "read", "self", "task"],
        ),
        get_current_task,
    ),
    (
        ToolSpec(
            name="stop_current_task",
            description="Cancel the task in flight and confirm the job's state afterwards.",
            agent="laptop",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=False,
            allowed_devices=["laptop_main"],
            parameters={},
            required_parameters=[],
            tags=["vantage", "self", "task", "stop"],
        ),
        stop_current_task,
    ),
]
