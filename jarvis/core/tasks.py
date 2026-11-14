"""Task / job manager (section 31).

Every complex request becomes a job with observable steps. v0.1 persists jobs
so the dashboard can show what is running, and supports cancel.

The lifecycle verbs (pause / resume / retry / cancel) are the part of this
manager that a caller can see, so each one answers with a
:class:`LifecycleReport` that states what actually happened. Until Phase 26
``retry()`` set the job to ``running``, reset its failed steps to ``pending``
and blanked their ``detail``, then executed nothing -- measured as 0 executions
and a job permanently ``running``, which the briefing then published as
"1 job(s) in progress." A step whose real state is ``failed`` stays ``failed``
here, because this manager records state and does not execute: the re-run that
Phase 27 added lives in :meth:`jarvis.core.orchestrator.JarvisCore.retry_job`,
which calls back into the permission gate for every step it re-issues.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..core.models import ActionStatus, Job, JobStep, new_id, utcnow
from ..db.database import get_db
from ..logging_setup import get_logger

log = get_logger("core.tasks")


@dataclass
class LifecycleReport:
    """What a job verb actually did.

    ``executed`` is the field that keeps the report honest: no lifecycle verb in
    this module runs a tool, so it is always 0, and every caller that tells a
    user something happened has to carry that number with it.
    """

    job_id: str
    action: str
    accepted: bool
    status: str = ""
    executed: int = 0
    steps_touched: int = 0
    reason: str = ""
    message: str = ""

    def __bool__(self) -> bool:
        return self.accepted

    def as_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "action": self.action,
            "accepted": self.accepted,
            "status": self.status,
            "executed": self.executed,
            "steps_touched": self.steps_touched,
            "reason": self.reason,
            "message": self.message,
        }


class TaskManager:
    """Creates and tracks long-running jobs."""

    def __init__(self) -> None:
        self.db = get_db()

    def create(self, goal: str, steps: Optional[List[str]] = None,
               planned: Optional[List[Any]] = None) -> Job:
        """Open a job and its step rows.

        ``planned`` is the matching list of :class:`PlannedAction`. It is stored
        alongside each step because a step that only remembers its tool name
        cannot be re-issued: Phase 27's re-run reads ``params`` and ``device``
        back out of the row rather than guessing them, and a row written before
        those columns existed is refused by the gate instead of invented.
        """
        job = Job(goal=goal)
        job.steps = [JobStep(name=name) for name in (steps or [])]
        self.db.execute(
            """
            INSERT INTO tasks (job_id, goal, status, requires_confirmation, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.goal,
                job.status,
                json.dumps(job.requires_confirmation),
                job.created_at.isoformat(),
                job.updated_at.isoformat(),
            ),
        )
        for index, step in enumerate(job.steps):
            action = planned[index] if planned is not None and index < len(planned) else None
            self.db.execute(
                """
                INSERT INTO task_steps (step_id, job_id, name, status, detail, seq, params, device)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (new_id("STEP"), job.job_id, step.name, step.status.value, step.detail, index,
                 json.dumps(getattr(action, "params", {}) or {}, default=str),
                 getattr(action, "device", "") or ""),
            )
        log.info("job created", extra_fields={"job_id": job.job_id, "goal": goal})
        return job

    def update_step(
        self,
        job_id: str,
        name: str,
        status: ActionStatus,
        detail: str = "",
        seq: Optional[int] = None,
    ) -> None:
        """Move one step to a new state.

        ``seq`` selects the step by position. A multi-step plan may name the same
        tool twice, and name-matching would then move both rows at once, so a
        plan that knows its own ordering should pass it.
        """
        if seq is None:
            self.db.execute(
                "UPDATE task_steps SET status = ?, detail = ? WHERE job_id = ? AND name = ?",
                (status.value, detail, job_id, name),
            )
        else:
            self.db.execute(
                "UPDATE task_steps SET status = ?, detail = ? WHERE job_id = ? AND seq = ?",
                (status.value, detail, job_id, seq),
            )
        self.db.execute(
            "UPDATE tasks SET updated_at = ? WHERE job_id = ?",
            (utcnow().isoformat(), job_id),
        )

    def attach_confirmation(self, job_id: str, seq: int, confirmation_id: str) -> None:
        """Record which approval a waiting step is blocked on."""
        self.db.execute(
            "UPDATE task_steps SET confirmation_id = ? WHERE job_id = ? AND seq = ?",
            (confirmation_id, job_id, seq),
        )

    def step_for_confirmation(self, confirmation_id: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM task_steps WHERE confirmation_id = ? AND confirmation_id <> ''",
            (confirmation_id,),
        )
        return dict(rows[0]) if rows else None

    def step_statuses(self, job_id: str) -> List[str]:
        rows = self.db.query(
            "SELECT status FROM task_steps WHERE job_id = ? ORDER BY seq", (job_id,)
        )
        return [row["status"] for row in rows]

    def refresh_status(self, job_id: str) -> str:
        """Recompute a job's status from its steps and store it.

        A job's status must never be written from the plan that created it,
        because a plan can stop half-way: the approval for step 2 arrives in a
        different call, and anything not recomputed there stayed ``running``
        forever.
        """
        status = job_status_for(self.step_statuses(job_id))
        self.set_status(job_id, status)
        return status

    def set_status(self, job_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE job_id = ?",
            (status, utcnow().isoformat(), job_id),
        )

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM tasks WHERE job_id = ?", (job_id,))
        if not rows:
            return None
        job = dict(rows[0])
        steps = self.db.query(
            "SELECT * FROM task_steps WHERE job_id = ? ORDER BY seq", (job_id,)
        )
        job["steps"] = [dict(s) for s in steps]
        return job

    def list(self, limit: int = 25) -> List[Dict[str, Any]]:
        rows = self.db.query("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def cancel(self, job_id: str) -> LifecycleReport:
        job = self.get(job_id)
        if job is None:
            return LifecycleReport(job_id, "cancel", False, reason="no such job",
                                   message="Nothing was cancelled: there is no such job.")
        self.set_status(job_id, "cancelled")
        log.info("job cancelled", extra_fields={"job_id": job_id})
        return LifecycleReport(
            job_id, "cancel", True, status="cancelled",
            message=("The job is marked cancelled. Steps that already ran are not "
                     "undone, and nothing further will be dispatched for them."),
        )

    # -- lifecycle (section 31: pause / resume / retry) ----------------------
    def pause(self, job_id: str) -> LifecycleReport:
        """Hold a job without discarding its progress."""
        job = self.get(job_id)
        if job is None:
            return LifecycleReport(job_id, "pause", False, reason="no such job",
                                   message="Nothing was paused: there is no such job.")
        if job["status"] in ("done", "cancelled"):
            return LifecycleReport(
                job_id, "pause", False, status=job["status"],
                reason=f"a {job['status']} job cannot be paused",
                message=f"Nothing changed: the job is already {job['status']}.",
            )
        self.set_status(job_id, "paused")
        log.info("job paused", extra_fields={"job_id": job_id})
        return LifecycleReport(
            job_id, "pause", True, status="paused",
            message="The job is marked paused. Its steps keep the state they reached.",
        )

    def resume(self, job_id: str) -> LifecycleReport:
        """Bring a paused job back to the state its steps are actually in.

        Not ``running``: measured, nothing re-dispatches a job row, so claiming
        ``running`` put a job that would never move into the briefing's "in
        progress" count.
        """
        job = self.get(job_id)
        if job is None:
            return LifecycleReport(job_id, "resume", False, reason="no such job",
                                   message="Nothing was resumed: there is no such job.")
        if job["status"] != "paused":
            return LifecycleReport(
                job_id, "resume", False, status=job["status"],
                reason="the job is not paused",
                message=f"Nothing changed: the job is {job['status']}, not paused.",
            )
        status = self.refresh_status(job_id)
        log.info("job resumed", extra_fields={"job_id": job_id, "status": status})
        return LifecycleReport(
            job_id, "resume", True, status=status,
            message=(
                "The pause was lifted; nothing ran. The job's real state is "
                f"{status!r} -- "
                + ("an approval is still waiting on it."
                   if status == "awaiting_confirmation"
                   else "ask me the goal again to have its steps executed.")
            ),
        )

    def retry(self, job_id: str) -> LifecycleReport:
        """Record a request to attempt the failed steps again.

        This runs nothing, and says so. The failed steps keep their status and
        their ``detail`` -- erasing the failure text was what made a retried job
        look recovered with its evidence removed -- and the job's status is
        recomputed from its steps rather than asserted.
        """
        job = self.get(job_id)
        if job is None:
            return LifecycleReport(job_id, "retry", False, reason="no such job",
                                   message="Nothing ran: there is no such job.")
        if job["status"] == "cancelled":
            return LifecycleReport(
                job_id, "retry", False, status="cancelled",
                reason="the job was cancelled",
                message="Nothing ran: a cancelled job is not re-attempted.",
            )

        failed_steps = [s for s in job["steps"]
                        if s["status"] == ActionStatus.FAILED.value]
        stamp = utcnow().isoformat(timespec="seconds")
        for step in failed_steps:
            evidence = step["detail"] or "no reason recorded"
            self.db.execute(
                "UPDATE task_steps SET detail = ? WHERE job_id = ? AND seq = ?",
                (f"{evidence} | re-attempt requested {stamp}", job_id, step["seq"]),
            )
        status = self.refresh_status(job_id)
        log.info("job retry requested", extra_fields={
            "job_id": job_id, "failed_steps": len(failed_steps), "executed": 0})
        if not failed_steps:
            return LifecycleReport(
                job_id, "retry", True, status=status,
                message="Nothing ran: no step of this job is recorded as failed, "
                        "so there is nothing to re-attempt.",
            )
        return LifecycleReport(
            job_id, "retry", True, status=status, steps_touched=len(failed_steps),
            message=(
                f"Nothing ran in this call. {len(failed_steps)} failed step(s) are "
                "marked as needing another attempt, with the reason they failed kept."
            ),
        )

    def restore_attempt_trail(self, job_id: str, seq: int, prior_detail: str) -> None:
        """Put the failure that prompted a re-run back in front of the new result.

        An attempt writes its own summary over the step, which on a re-run would
        delete the evidence Phase 26 made a point of keeping -- the reader would
        see only the second try and not know there was a first.
        """
        rows = self.db.query(
            "SELECT detail FROM task_steps WHERE job_id = ? AND seq = ?", (job_id, seq)
        )
        current = rows[0]["detail"] if rows else ""
        if not rows or current == prior_detail:
            return
        self.db.execute(
            "UPDATE task_steps SET detail = ? WHERE job_id = ? AND seq = ?",
            (f"{prior_detail} | this attempt: {current}", job_id, seq),
        )

    def reconcile_after_restart(self) -> List[str]:
        """Correct the `running` rows this process cannot possibly have started.

        Called once, at startup, before the scheduler wakes. At that instant nothing is in
        flight here, so every row still marked `running` belongs to a process that is gone --
        usually one that died between opening the job and writing its first step, which leaves
        the steps `pending` and the job `running` forever.

        That stale column is not a display problem. `get_current_task` reads it and tells a
        person "Current task: \"lifecycle test\" -- running, 0 of 2 step(s) done" about work ten
        days dead, and `stop_current_task` reads it, cancels that row and reports
        `verified: True` -- a stop spent on a phantom while anything real keeps running.

        The derivation is the existing one (`job_status_for` over the step rows), so this adds no
        policy: it applies, at the one moment the answer is knowable, the rule ``refresh_status``
        already documents -- "anything not recomputed there stayed ``running`` forever". It runs
        nowhere else on purpose: a read that reconciles is a GET that writes job state, and this
        project removed that class of thing in Phase 28.

        Rows whose steps cannot contradict `running` (a job with no step rows at all) are left
        alone rather than guessed at; a measured pass over the delivered database found none.
        """
        rows = self.db.query("SELECT job_id FROM tasks WHERE status = 'running' ORDER BY created_at ASC")
        moved: List[str] = []
        for row in rows:
            job_id = row["job_id"]
            derived = job_status_for(self.step_statuses(job_id))
            if derived == "running":
                continue
            self.set_status(job_id, derived)
            moved.append(job_id)

        if moved:
            log.info(
                "stale running jobs reconciled at startup",
                extra_fields={"jobs": len(moved), "statuses": sorted(
                    {self.get(job_id)["status"] for job_id in moved})},
            )
        return moved

    def jobs_with_status(self, status: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Jobs in one named state, newest first.

        `incomplete` is deliberately *not* included by callers that surface "needs
        attention": a plan that stopped is worth seeing on the Work page, but this
        machine holds twelve stale test jobs in that state, and a list of those
        would bury the one that matters.
        """
        rows = self.db.query(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        )
        return [dict(r) for r in rows]

    def failed_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM tasks WHERE status = 'failed' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]


def job_status_for(statuses: List[str]) -> str:
    """Aggregate per-step states into one honest job status.

    Order matters. A step left ``pending`` means the sequence stopped before it:
    calling that ``done`` was how a goal that never ran its second half read as
    completed.
    """
    if not statuses:
        return "running"
    if any(s in (ActionStatus.FAILED.value, ActionStatus.BLOCKED.value) for s in statuses):
        return "failed"
    if ActionStatus.AWAITING_CONFIRMATION.value in statuses:
        return "awaiting_confirmation"
    if ActionStatus.PENDING.value in statuses or ActionStatus.RUNNING.value in statuses:
        # Steps the plan never reached. Not a failure -- nothing went wrong --
        # and emphatically not done.
        return "incomplete"
    if ActionStatus.UNVERIFIED.value in statuses:
        # Not a failure, and not a done either: the work was answered but never
        # observed.
        return "unverified"
    return "done"
