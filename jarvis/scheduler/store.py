"""Scheduled job storage (v0.8).

Each job records its own action, parameters and **permission ceiling**. The
ceiling is what stops a schedule from becoming a way to gain authority the user
never granted: execution is re-evaluated against the permission engine every
time, and a job may never act above the risk level it was created with.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, new_id, utcnow
from ..db.database import get_db
from ..logging_setup import get_logger
from .schedule import Recurrence, parse_schedule

log = get_logger("scheduler.store")

#: Jobs may not be created for actions above this risk without the action being
#: explicitly acknowledged. Destructive and physical work stays manual.
MAX_SCHEDULABLE_RISK = RiskLevel.EXTERNAL

ACTIVE = "active"
PAUSED = "paused"
CANCELLED = "cancelled"


@dataclass
class ScheduledJob:
    schedule_id: str
    name: str
    recurrence: Recurrence
    action: str
    params: Dict[str, Any] = field(default_factory=dict)
    owner: str = "user"
    status: str = ACTIVE
    max_risk: int = int(RiskLevel.LOW)
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    run_count: int = 0
    failure_count: int = 0
    last_error: str = ""
    last_result: str = ""
    requires_confirmation: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "name": self.name,
            "schedule": self.recurrence.describe(),
            "rrule": self.recurrence.to_rrule(),
            "action": self.action,
            "params": self.params,
            "owner": self.owner,
            "status": self.status,
            "max_risk": self.max_risk,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "run_count": self.run_count,
            "failure_count": self.failure_count,
            "last_error": self.last_error,
            "last_result": self.last_result,
            "requires_confirmation": self.requires_confirmation,
        }


class ScheduleStore:
    """CRUD for scheduled jobs."""

    def __init__(self) -> None:
        self.db = get_db()

    # -- create --------------------------------------------------------------
    def create(
        self,
        name: str,
        schedule_text: str,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        max_risk: int = int(RiskLevel.LOW),
        owner: str = "user",
    ) -> ScheduledJob:
        """Create a job. Raises ValueError when the schedule is unparseable."""
        recurrence = parse_schedule(schedule_text)
        if recurrence is None:
            raise ValueError(
                f"I could not understand the schedule {schedule_text!r}. Try "
                '"every morning", "every Monday at 9 AM", or "at 8 PM".'
            )

        now = datetime.now().astimezone()
        schedule_id = new_id("SCHED")
        job = ScheduledJob(
            schedule_id=schedule_id,
            name=name.strip() or action,
            recurrence=recurrence,
            action=action,
            params=params or {},
            owner=owner,
            max_risk=int(max_risk),
            next_run=recurrence.next_after(now),
        )

        self.db.execute(
            """
            INSERT INTO scheduled_jobs
                (schedule_id, name, rrule, prompt, status, next_run, created_at,
                 owner, action, params, max_risk, requires_confirmation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.schedule_id,
                job.name,
                recurrence.to_rrule(),
                action,
                job.status,
                job.next_run.isoformat() if job.next_run else None,
                utcnow().isoformat(),
                job.owner,
                job.action,
                json.dumps(job.params, default=str),
                job.max_risk,
                1 if job.requires_confirmation else 0,
            ),
        )
        log.info(
            "job scheduled",
            extra_fields={"schedule_id": schedule_id, "action": action, "when": recurrence.describe()},
        )
        return job

    def update(
        self,
        schedule_id: str,
        *,
        name: Optional[str] = None,
        schedule_text: Optional[str] = None,
        action: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        max_risk: Optional[int] = None,
    ) -> Optional[ScheduledJob]:
        """Edit a job in place, keeping its identity and its run history.

        A changed schedule re-parses and re-computes ``next_run`` from *now* —
        never from the old time — so an edit cannot leave a stale next run that
        fires immediately. ``schedule_runs`` rows are untouched: an edit changes
        what happens next, not what already happened.
        """
        job = self.get(schedule_id)
        if job is None:
            return None

        sets: list = []
        values: list = []
        if name is not None and name.strip():
            sets.append("name = ?"); values.append(name.strip())
        if action is not None and action.strip():
            sets.append("action = ?"); values.append(action.strip())
        if params is not None:
            sets.append("params = ?"); values.append(json.dumps(params, default=str))
        if max_risk is not None:
            sets.append("max_risk = ?"); values.append(int(max_risk))
        if schedule_text is not None:
            recurrence = parse_schedule(schedule_text)
            if recurrence is None:
                raise ValueError(
                    f"I could not understand the schedule {schedule_text!r}. Try "
                    '"every morning", "every Monday at 9 AM", or "at 8 PM".'
                )
            next_run = recurrence.next_after(datetime.now().astimezone())
            sets.append("rrule = ?"); values.append(recurrence.to_rrule())
            sets.append("next_run = ?")
            values.append(next_run.isoformat() if next_run else None)

        if not sets:
            return job
        values.append(schedule_id)
        self.db.execute(
            f"UPDATE scheduled_jobs SET {', '.join(sets)} WHERE schedule_id = ?",
            tuple(values),
        )
        log.info("job edited", extra_fields={"schedule_id": schedule_id})
        return self.get(schedule_id)

    # -- read ----------------------------------------------------------------
    @staticmethod
    def _row_to_job(row: Any) -> Optional[ScheduledJob]:
        recurrence = Recurrence.from_rrule(row["rrule"] or "")
        if recurrence is None:
            return None

        def _ts(value: Any) -> Optional[datetime]:
            if not value:
                return None
            try:
                return datetime.fromisoformat(str(value))
            except ValueError:
                return None

        def _col(name: str, default: Any = None) -> Any:
            try:
                return row[name]
            except (IndexError, KeyError):
                return default

        return ScheduledJob(
            schedule_id=row["schedule_id"],
            name=row["name"],
            recurrence=recurrence,
            action=_col("action", "") or row["prompt"] or "",
            params=json.loads(_col("params", "{}") or "{}"),
            owner=_col("owner", "user") or "user",
            status=row["status"] or ACTIVE,
            max_risk=int(_col("max_risk", int(RiskLevel.LOW)) or int(RiskLevel.LOW)),
            last_run=_ts(row["last_run"]),
            next_run=_ts(row["next_run"]),
            run_count=int(_col("run_count", 0) or 0),
            failure_count=int(_col("failure_count", 0) or 0),
            last_error=_col("last_error", "") or "",
            last_result=_col("last_result", "") or "",
            requires_confirmation=bool(_col("requires_confirmation", 0)),
        )

    def get(self, schedule_id: str) -> Optional[ScheduledJob]:
        rows = self.db.query("SELECT * FROM scheduled_jobs WHERE schedule_id = ?", (schedule_id,))
        return self._row_to_job(rows[0]) if rows else None

    def list(self, include_cancelled: bool = False, limit: int = 50) -> List[ScheduledJob]:
        if include_cancelled:
            rows = self.db.query(
                "SELECT * FROM scheduled_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        else:
            rows = self.db.query(
                "SELECT * FROM scheduled_jobs WHERE status != ? ORDER BY created_at DESC LIMIT ?",
                (CANCELLED, limit),
            )
        jobs = [self._row_to_job(r) for r in rows]
        return [j for j in jobs if j is not None]

    def due(self, moment: Optional[datetime] = None) -> List[ScheduledJob]:
        """Active jobs whose next run has arrived."""
        moment = moment or datetime.now().astimezone()
        return [
            job
            for job in self.list()
            if job.status == ACTIVE and job.next_run and job.next_run <= moment
        ]

    # -- lifecycle -----------------------------------------------------------
    def set_status(self, schedule_id: str, status: str) -> bool:
        job = self.get(schedule_id)
        if job is None:
            return False
        self.db.execute(
            "UPDATE scheduled_jobs SET status = ? WHERE schedule_id = ?", (status, schedule_id)
        )
        return True

    def pause(self, schedule_id: str) -> bool:
        return self.set_status(schedule_id, PAUSED)

    def resume(self, schedule_id: str) -> bool:
        job = self.get(schedule_id)
        if job is None:
            return False
        # Recompute the next run so a long pause does not fire immediately.
        job.next_run = job.recurrence.next_after(datetime.now().astimezone())
        self.db.execute(
            "UPDATE scheduled_jobs SET status = ?, next_run = ? WHERE schedule_id = ?",
            (ACTIVE, job.next_run.isoformat() if job.next_run else None, schedule_id),
        )
        return True

    def cancel(self, schedule_id: str) -> bool:
        return self.set_status(schedule_id, CANCELLED)

    def delete(self, schedule_id: str) -> bool:
        job = self.get(schedule_id)
        if job is None:
            return False
        self.db.execute("DELETE FROM scheduled_jobs WHERE schedule_id = ?", (schedule_id,))
        return True

    # -- run bookkeeping -----------------------------------------------------
    def record_run(
        self,
        job: ScheduledJob,
        outcome: str,
        detail: str = "",
        advance: bool = True,
    ) -> None:
        """Record one execution and advance the schedule."""
        now = datetime.now().astimezone()
        next_run = job.recurrence.next_after(now) if advance else job.next_run

        failed = outcome == "failed"
        self.db.execute(
            """
            UPDATE scheduled_jobs
            SET last_run = ?, next_run = ?, run_count = run_count + 1,
                failure_count = failure_count + ?, last_error = ?, last_result = ?
            WHERE schedule_id = ?
            """,
            (
                now.isoformat(),
                next_run.isoformat() if next_run else None,
                1 if failed else 0,
                detail if failed else "",
                detail,
                job.schedule_id,
            ),
        )
        self.db.execute(
            """
            INSERT INTO schedule_runs (run_id, schedule_id, finished_at, outcome, detail)
            VALUES (?, ?, ?, ?, ?)
            """,
            (new_id("RUN"), job.schedule_id, now.isoformat(), outcome, detail[:500]),
        )

        # A one-shot job retires after it fires.
        if job.recurrence.kind == "once":
            self.set_status(job.schedule_id, CANCELLED)

    def history(self, schedule_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM schedule_runs WHERE schedule_id = ? ORDER BY started_at DESC LIMIT ?",
            (schedule_id, limit),
        )
        return [dict(r) for r in rows]

    def stats(self) -> Dict[str, Any]:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM scheduled_jobs GROUP BY status")
        by_status = {r["status"]: r["n"] for r in rows}
        runs = self.db.query("SELECT outcome, COUNT(*) AS n FROM schedule_runs GROUP BY outcome")
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_outcome": {r["outcome"]: r["n"] for r in runs},
        }
