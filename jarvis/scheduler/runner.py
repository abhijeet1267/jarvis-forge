"""Scheduler runner (v0.8).

The rule the plan states in capitals: **never allow a scheduled task to silently
escalate its permissions.** That is enforced here in two independent ways:

1. **A risk ceiling per job.** The job records the risk level it was created
   with. If the action's risk is now higher, the run is refused rather than
   quietly executed. A schedule cannot become a way to gain authority later.
2. **Re-evaluation every single run.** The action goes through the same
   ``PermissionEngine`` as an interactive command. If the verdict is CONFIRM,
   the runner **creates a pending confirmation** and stops — it never approves
   on the user's behalf. If the verdict is BLOCK, the run fails and is recorded.

A scheduled job therefore has exactly the authority the user granted when they
created it, and no more.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.models import (
    ActionResult,
    PlannedAction,
    PolicyDecision,
    RiskLevel,
    Verification,
)
from ..core.recovery import RecoveryOutcome
from ..logging_setup import get_logger
from .store import ACTIVE, ScheduleStore, ScheduledJob

log = get_logger("scheduler.runner")

#: Actions the scheduler can run without a tool. They are read-only by nature.
BUILTIN_ACTIONS = {"briefing", "reminder"}


def _record_unattended_failure(core, action: PlannedAction, result: ActionResult,
                               detail: str) -> bool:
    """Write an unattended failure into the same ledger an interactive one uses.

    ``strategy="none"`` and ``attempts=0`` are the claim: the scheduler does not
    run the recovery chain, and giving it retries is a different decision from
    making sure its failures are counted.
    """
    return core.recovery.record_failure(
        action,
        result,
        RecoveryOutcome(
            strategy="none",
            attempts=0,
            note=(f"{detail} — not retried: an unattended schedule runs "
                  "without the recovery chain"),
            answered_for=action.tool,
        ),
    )

#: How often the background loop checks for due jobs.
TICK_SECONDS = 30


class SchedulerRunner:
    """Executes due jobs through the normal permission pipeline."""

    def __init__(self, store: Optional[ScheduleStore] = None) -> None:
        self.store = store or ScheduleStore()
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # -- one job -------------------------------------------------------------
    async def run_job(self, core, job: ScheduledJob) -> Dict[str, Any]:
        """Run one job. Returns an outcome record; never raises."""
        # --- builtin actions ------------------------------------------------
        if job.action in BUILTIN_ACTIONS:
            try:
                text = await self._builtin(core, job)
            except Exception as exc:
                self.store.record_run(job, "failed", f"{job.action} raised: {exc}")
                return {"outcome": "failed", "detail": str(exc)}
            self.store.record_run(job, "executed", text[:200])
            return {"outcome": "executed", "detail": text}

        # --- tool actions ---------------------------------------------------
        spec = core.tools.get(job.action)
        if spec is None:
            detail = f"unknown action {job.action!r}"
            self.store.record_run(job, "failed", detail)
            return {"outcome": "failed", "detail": detail}

        # Guard 1: the job may not exceed the risk ceiling it was created with.
        if int(spec.risk) > int(job.max_risk):
            detail = (
                f"refused: {job.action} is now risk {spec.risk_label()} but this job "
                f"was created with a ceiling of {RiskLevel(job.max_risk).name.lower()}. "
                "A schedule cannot raise its own authority."
            )
            log.info("scheduled run refused", extra_fields={"schedule_id": job.schedule_id})
            self.store.record_run(job, "blocked", detail)
            return {"outcome": "blocked", "detail": detail}

        action = PlannedAction(
            tool=job.action,
            params=dict(job.params),
            device="laptop_main",
            reason=f"scheduled: {job.name}",
        )

        # Guard 2: re-evaluate through the permission engine, every run.
        verdict = core.policy.evaluate(action, session_id=f"sched:{job.schedule_id}")

        if verdict.decision is PolicyDecision.BLOCK:
            self.store.record_run(job, "blocked", verdict.reason)
            return {"outcome": "blocked", "detail": verdict.reason}

        if verdict.decision is PolicyDecision.CONFIRM:
            # Do NOT approve on the user's behalf. Park it as a pending
            # confirmation and let the user decide.
            self.store.record_run(
                job,
                "awaiting_approval",
                f"needs your approval: {verdict.reason}",
            )
            log.info(
                "scheduled run awaiting approval",
                extra_fields={
                    "schedule_id": job.schedule_id,
                    "confirmation_id": verdict.confirmation_id,
                },
            )
            return {
                "outcome": "awaiting_approval",
                "detail": verdict.reason,
                "confirmation_id": verdict.confirmation_id,
            }

        # --- execute --------------------------------------------------------
        from ..agents.base import find_agent
        from ..tools.base import ToolContext

        agent = find_agent(core.agents, job.action)
        if agent is None:
            detail = f"no agent owns {job.action!r}"
            self.store.record_run(job, "failed", detail)
            _record_unattended_failure(
                core, action,
                ActionResult(ok=False, tool=job.action, error=detail, verified=False),
                detail)
            return {"outcome": "failed", "detail": detail}

        ctx = ToolContext(session_id=f"sched:{job.schedule_id}")
        result = await agent.execute(action, ctx)

        # Third `.ok`-derived success site, found by the Phase 12 audit: a job
        # whose handler answered without observing anything was recorded as
        # "executed" next to a detail line of "could not read". Same defect class
        # as Phase 9 P0-1, which was fixed at the two orchestrator sites.
        if result.verification is Verification.SUCCESS:
            self.store.record_run(job, "executed", result.message or "done")
            return {"outcome": "executed", "detail": result.message}

        if result.ok:
            self.store.record_run(job, "unverified", result.summary)
            return {"outcome": "unverified", "detail": result.summary}

        self.store.record_run(job, "failed", result.summary)
        # R-12's other half, measured: a scheduled action that failed wrote
        # nothing to `failures` (0 -> 0), so /metrics and `explain_failures`
        # described only the interactive half of the system. `schedule_runs`
        # already told the truth about the run; the recovery ledger had to hear
        # about it too.
        _record_unattended_failure(core, action, result, result.summary)
        return {"outcome": "failed", "detail": result.summary}

    async def _builtin(self, core, job: ScheduledJob) -> str:
        """Run a built-in scheduled action."""
        if job.action == "briefing":
            from ..core.briefing import build_briefing

            briefing = await build_briefing(core, sections=job.params.get("sections"))
            return briefing["text"]

        if job.action == "reminder":
            message = job.params.get("message") or job.name
            return f"Reminder: {message}"

        raise ValueError(f"unknown builtin {job.action!r}")

    # -- all due jobs --------------------------------------------------------
    async def tick(self, core, moment: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Run every job that is due. Returns one record per job."""
        results = []
        for job in self.store.due(moment):
            outcome = await self.run_job(core, job)
            results.append({"schedule_id": job.schedule_id, "name": job.name, **outcome})
        return results

    # -- background loop -----------------------------------------------------
    async def start(self, core) -> None:
        """Start the background loop. Safe to call twice."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(core))
        log.info("scheduler started", extra_fields={"tick_seconds": TICK_SECONDS})

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self, core) -> None:  # pragma: no cover - long-running
        while self._running:
            try:
                await self.tick(core)
            except Exception as exc:
                # A scheduler must never die because one job misbehaved.
                log.info("scheduler tick failed", extra_fields={"error": str(exc)})
            await asyncio.sleep(TICK_SECONDS)

    def describe(self) -> Dict[str, Any]:
        stats = self.store.stats()
        return {
            "running": self._running,
            "tick_seconds": TICK_SECONDS,
            "builtin_actions": sorted(BUILTIN_ACTIONS),
            **stats,
        }
