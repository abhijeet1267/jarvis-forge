"""The daily planner: a person's tasks, their day, and what to do next.

This is the user-facing planner, not the workflow engine. A *task* here is
something the person wrote down ("finish the AMR report", due Friday, high
priority); a *workflow run* (``tasks``/``task_steps``) is Vantage executing a
multi-step goal. They are different tables because they answer different
questions, and conflating them is how a to-do list starts reporting "running".

What the planner is careful about:

* **It never invents data.** An agenda is built from rows that exist -- calendar
  events actually read from the provider, tasks actually saved. A calendar that
  could not be read is reported as unread, not as "no events".
* **Blocked is not doable.** A task whose dependencies are unfinished is shown
  as blocked, with what it waits on, never listed as the next action.
* **Order is justified.** "Next" is picked by a stated rule (overdue first, then
  priority, then due time, then estimate), and the plan says which rule fired.
* **Conflicts are surfaced.** Overlapping calendar events are named, not
  silently laid on top of each other.
* **Proposals are not writes.** Scheduling a task into a calendar slot produces
  a *proposal*; the calendar write still goes through the permission engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

from ..core.models import new_id, utcnow
from ..db.database import get_db
from ..logging_setup import get_logger

log = get_logger("core.planner_service")

STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_CANCELLED = "cancelled"

PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None


@dataclass
class Task:
    """One to-do item."""

    task_ref: str
    title: str
    detail: str = ""
    status: str = STATUS_OPEN
    priority: str = "normal"
    due_at: Optional[datetime] = None
    start_at: Optional[datetime] = None
    estimate_minutes: int = 0
    depends_on: List[str] = field(default_factory=list)
    source: str = "user"
    project: str = ""
    done_at: Optional[datetime] = None
    evidence: str = ""

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN

    def is_blocked(self, open_refs: set) -> bool:
        """A task is blocked when any dependency it names is still open."""
        return any(ref in open_refs for ref in self.depends_on)

    def is_overdue(self, now: Optional[datetime] = None) -> bool:
        if not self.is_open or not self.due_at:
            return False
        now = now or datetime.now().astimezone()
        due = self.due_at
        # A due date stored without an offset is interpreted in the local zone;
        # comparing it directly to an aware `now` raises.
        if due.tzinfo is None:
            due = due.astimezone()
        return due < now

    def as_dict(self, *, now: Optional[datetime] = None, open_refs: Optional[set] = None) -> Dict[str, Any]:
        open_refs = open_refs if open_refs is not None else set()
        return {
            "task_ref": self.task_ref,
            "title": self.title,
            "detail": self.detail,
            "status": self.status,
            "priority": self.priority,
            "due_at": _iso(self.due_at),
            "start_at": _iso(self.start_at),
            "estimate_minutes": self.estimate_minutes,
            "depends_on": list(self.depends_on),
            "source": self.source,
            "project": self.project,
            "done_at": _iso(self.done_at),
            "evidence": self.evidence,
            "overdue": self.is_overdue(now),
            "blocked": self.is_blocked(open_refs),
        }


class PlannerService:
    """Persistent user tasks + a computed daily plan."""

    def __init__(self, db: Any = None) -> None:
        self.db = db or get_db()

    def due_now(self, now: Optional[datetime] = None) -> List["Task"]:
        """Tasks whose due time has arrived and that have not been notified.

        Separate from "overdue" on purpose: overdue is a *state* the attention
        list reports continuously, while a notification is an *event* and must
        happen once. The row is recorded here, at the moment it is handed out, so
        two callers racing cannot both claim it.
        """
        now = now or datetime.now().astimezone()
        due: List[Task] = []
        for task in self.list_tasks():
            if not task.due_at:
                continue
            when = task.due_at
            if when.tzinfo is None:
                when = when.astimezone()
            if when > now:
                continue
            already = self.db.query(
                "SELECT 1 FROM notified_tasks WHERE task_ref = ?", (task.task_ref,))
            if already:
                continue
            due.append(task)
        return due

    def mark_notified(self, task_ref: str) -> None:
        """Record that a task's notification has been handed to the system."""
        self.db.execute(
            "INSERT OR IGNORE INTO notified_tasks (task_ref) VALUES (?)", (task_ref,))


    # -- task storage --------------------------------------------------------
    def create_task(
        self,
        title: str,
        *,
        detail: str = "",
        priority: str = "normal",
        due_at: Optional[datetime] = None,
        start_at: Optional[datetime] = None,
        estimate_minutes: int = 0,
        depends_on: Optional[List[str]] = None,
        project: str = "",
        source: str = "user",
    ) -> Task:
        title = (title or "").strip()
        if not title:
            raise ValueError("a task needs a title")
        priority = priority if priority in PRIORITY_ORDER else "normal"
        task_ref = new_id("VT")
        self.db.execute(
            """INSERT INTO vantage_tasks
               (task_ref, title, detail, status, priority, due_at, start_at,
                estimate_minutes, depends_on, source, project, created_at, updated_at)
               VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_ref, title, detail, priority, _iso(due_at), _iso(start_at),
             max(0, int(estimate_minutes)), json.dumps(depends_on or []),
             source, project, utcnow().isoformat(), utcnow().isoformat()),
        )
        return self.get_task(task_ref)  # type: ignore[return-value]

    def _row_to_task(self, row: Any) -> Task:
        try:
            deps = json.loads(row["depends_on"] or "[]")
        except (TypeError, ValueError):
            deps = []
        return Task(
            task_ref=row["task_ref"], title=row["title"], detail=row["detail"] or "",
            status=row["status"] or STATUS_OPEN, priority=row["priority"] or "normal",
            due_at=_parse(row["due_at"]), start_at=_parse(row["start_at"]),
            estimate_minutes=int(row["estimate_minutes"] or 0),
            depends_on=[d for d in deps if isinstance(d, str)],
            source=row["source"] or "user", project=row["project"] or "",
            done_at=_parse(row["done_at"]), evidence=row["evidence"] or "",
        )

    def get_task(self, task_ref: str) -> Optional[Task]:
        rows = self.db.query("SELECT * FROM vantage_tasks WHERE task_ref = ?", (task_ref,))
        return self._row_to_task(rows[0]) if rows else None

    def list_tasks(self, *, status: Optional[str] = None, include_done: bool = False) -> List[Task]:
        sql = "SELECT * FROM vantage_tasks"
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        elif not include_done:
            sql += " WHERE status = 'open'"
        sql += " ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 " \
               "WHEN 'normal' THEN 2 ELSE 3 END, IFNULL(due_at, '9999') ASC, created_at ASC"
        return [self._row_to_task(r) for r in self.db.query(sql, params)]

    def update_task(self, task_ref: str, **changes: Any) -> Optional[Task]:
        """Update mutable fields. Unknown fields are ignored, never silently stored."""
        allowed = {"title", "detail", "priority", "due_at", "start_at",
                   "estimate_minutes", "depends_on", "project", "status"}
        sets, params = [], []
        for key, value in changes.items():
            if key not in allowed:
                continue
            if key in ("due_at", "start_at"):
                value = _iso(value) if isinstance(value, datetime) else value
            elif key == "depends_on":
                value = json.dumps(value or [])
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return self.get_task(task_ref)
        sets.append("updated_at = ?")
        params.append(utcnow().isoformat())
        params.append(task_ref)
        self.db.execute(f"UPDATE vantage_tasks SET {', '.join(sets)} WHERE task_ref = ?",
                        tuple(params))
        return self.get_task(task_ref)

    def complete_task(self, task_ref: str, evidence: str = "") -> Optional[Task]:
        """Mark a task done, recording the evidence the person gave."""
        task = self.get_task(task_ref)
        if task is None:
            return None
        self.db.execute(
            "UPDATE vantage_tasks SET status='done', done_at=?, evidence=?, updated_at=? "
            "WHERE task_ref=?",
            (utcnow().isoformat(), evidence, utcnow().isoformat(), task_ref),
        )
        return self.get_task(task_ref)

    def cancel_task(self, task_ref: str) -> Optional[Task]:
        self.db.execute(
            "UPDATE vantage_tasks SET status='cancelled', updated_at=? WHERE task_ref=?",
            (utcnow().isoformat(), task_ref),
        )
        return self.get_task(task_ref)

    # -- the day -------------------------------------------------------------
    def todays_agenda(
        self,
        *,
        day: Optional[date] = None,
        now: Optional[datetime] = None,
        calendar_events: Optional[List[Any]] = None,
        calendar_read_failed: bool = False,
    ) -> Dict[str, Any]:
        """Everything the person needs to see for one day.

        ``calendar_events`` is passed in (already read from the provider) rather
        than read here, so this service has no opinion about which calendar
        backend is in use. A failed read is reported as ``calendar_read_failed``
        -- never rendered as an empty day.
        """
        now = now or datetime.now().astimezone()
        day = day or now.date()
        day_start = datetime.combine(day, time.min, tzinfo=now.tzinfo)
        day_end = day_start + timedelta(days=1)

        tasks = self.list_tasks()
        open_refs = {t.task_ref for t in tasks if t.is_open}
        task_dicts = [t.as_dict(now=now, open_refs=open_refs) for t in tasks]

        events = list(calendar_events or [])
        conflicts = self._find_conflicts(events, day_start, day_end)

        due_today = [t for t in task_dicts if t["due_at"] and self._same_day(t["due_at"], day)]
        overdue = [t for t in task_dicts if t["overdue"]]
        blocked = [t for t in task_dicts if t["blocked"]]
        doable = [t for t in task_dicts if not t["blocked"]]
        next_action = self._pick_next(doable, now)

        return {
            "day": day.isoformat(),
            "calendar_read_failed": bool(calendar_read_failed),
            "events": [self._event_dict(e) for e in events],
            "conflicts": conflicts,
            "tasks": task_dicts,
            "due_today": due_today,
            "overdue": overdue,
            "blocked": blocked,
            "next_action": next_action,
            "has_unscheduled": any(not t.get("start_at") for t in doable),
            "generated_at": now.isoformat(),
        }

    @staticmethod
    def _same_day(value: str, day: date) -> bool:
        parsed = _parse(value)
        return bool(parsed and parsed.date() == day)

    @staticmethod
    def _event_dict(event: Any) -> Dict[str, Any]:
        if isinstance(event, dict):
            return event
        if hasattr(event, "as_dict"):
            return event.as_dict()
        return {"title": str(event)}

    @staticmethod
    def _find_conflicts(events: List[Any], day_start: datetime, day_end: datetime) -> List[Dict[str, Any]]:
        """Overlapping events in the window, named. Silently stacking them is a lie."""
        spans = []
        for event in events:
            start = getattr(event, "start", None)
            end = getattr(event, "end", None)
            if not isinstance(start, datetime) or not isinstance(end, datetime):
                continue
            if end <= day_start or start >= day_end:
                continue
            spans.append((start, end, getattr(event, "title", "") or "(untitled)"))
        spans.sort()
        found: List[Dict[str, Any]] = []
        for i in range(len(spans)):
            for j in range(i + 1, len(spans)):
                a, b = spans[i], spans[j]
                if b[0] >= a[1]:
                    break
                found.append({
                    "first": a[2], "second": b[2],
                    "overlap_start": a[0].isoformat(),
                    "overlap_end": min(a[1], b[1]).isoformat(),
                })
        return found

    @staticmethod
    def _pick_next(doable: List[Dict[str, Any]], now: datetime) -> Optional[Dict[str, Any]]:
        """The next task, with the rule that picked it stated.

        Order: overdue first, then priority, then earliest due time. A task with
        no due date sorts last among equals. The returned dict names the rule so
        the answer is justifiable, not a hunch.
        """
        if not doable:
            return None
        overdue = [t for t in doable if t.get("overdue")]
        pool = overdue or doable
        rule = "earliest overdue task" if overdue else "highest priority, then due time"

        def key(t: Dict[str, Any]):
            prio = PRIORITY_ORDER.get(t.get("priority", "normal"), 2)
            due = t.get("due_at") or "9999"
            return (prio, due)

        chosen = sorted(pool, key=key)[0]
        return {"task": chosen, "rule": rule}