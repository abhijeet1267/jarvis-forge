"""Task / job manager (section 31).

Every complex request becomes a job with observable steps. v0.1 persists jobs
so the dashboard can show what is running, and supports cancel.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..core.models import ActionStatus, Job, JobStep, new_id, utcnow
from ..db.database import get_db
from ..logging_setup import get_logger

log = get_logger("core.tasks")


class TaskManager:
    """Creates and tracks long-running jobs."""

    def __init__(self) -> None:
        self.db = get_db()

    def create(self, goal: str, steps: Optional[List[str]] = None) -> Job:
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
            self.db.execute(
                """
                INSERT INTO task_steps (step_id, job_id, name, status, detail, seq)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (new_id("STEP"), job.job_id, step.name, step.status.value, step.detail, index),
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

    def cancel(self, job_id: str) -> bool:
        self.set_status(job_id, "cancelled")
        log.info("job cancelled", extra_fields={"job_id": job_id})
        return True

    # -- lifecycle (section 31: pause / resume / retry) ----------------------
    def pause(self, job_id: str) -> bool:
        """Hold a job without discarding its progress."""
        job = self.get(job_id)
        if job is None or job["status"] in ("done", "cancelled"):
            return False
        self.set_status(job_id, "paused")
        log.info("job paused", extra_fields={"job_id": job_id})
        return True

    def resume(self, job_id: str) -> bool:
        """Continue a paused job."""
        job = self.get(job_id)
        if job is None or job["status"] != "paused":
            return False
        self.set_status(job_id, "running")
        log.info("job resumed", extra_fields={"job_id": job_id})
        return True

    def retry(self, job_id: str) -> bool:
        """Reset failed steps so a job can run again."""
        job = self.get(job_id)
        if job is None or job["status"] == "cancelled":
            return False

        self.db.execute(
            """
            UPDATE task_steps
            SET status = ?, detail = ''
            WHERE job_id = ? AND status = ?
            """,
            (ActionStatus.PENDING.value, job_id, ActionStatus.FAILED.value),
        )
        self.set_status(job_id, "running")
        log.info("job retried", extra_fields={"job_id": job_id})
        return True

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
