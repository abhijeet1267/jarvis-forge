"""Professional posts: draft, schedule, remind — **never auto-publish**.

Vantage has no approved LinkedIn (or similar) publishing integration, and this
module does not pretend otherwise. What it *can* do is the part of the workflow
that is safe and real: keep drafted posts, let a person edit them, record an
intended publication date, and surface a reminder when that date arrives.

A draft's ``status`` stays ``draft``/``scheduled``. There is no code path here
that sets ``published``, because publishing is an external, authorised action
that this product does not implement — and a row that said "published" without a
verified post would be exactly the false success this project keeps refusing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.models import new_id, utcnow
from ..db.database import get_db


@dataclass
class PostDraft:
    draft_id: str
    channel: str
    body: str
    status: str
    scheduled_for: Optional[str] = None
    contact: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "channel": self.channel,
            "body": self.body,
            "status": self.status,
            "scheduled_for": self.scheduled_for,
            "contact": self.contact,
        }


class PostService:
    """CRUD for drafted posts, plus the honest publishing boundary."""

    def __init__(self, db: Any = None) -> None:
        self.db = db or get_db()

    def _row(self, row: Any) -> PostDraft:
        return PostDraft(
            draft_id=row["draft_id"], channel=row["channel"] or "linkedin",
            body=row["body"] or "", status=row["status"] or "draft",
            scheduled_for=row["scheduled_for"], contact=row["contact"] or "",
        )

    def create(self, body: str, *, channel: str = "linkedin",
               scheduled_for: Optional[datetime] = None, contact: str = "") -> PostDraft:
        body = (body or "").strip()
        if not body:
            raise ValueError("a post needs text")
        draft_id = new_id("POST")
        status = "scheduled" if scheduled_for else "draft"
        self.db.execute(
            """INSERT INTO post_drafts
               (draft_id, channel, body, status, scheduled_for, contact, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (draft_id, channel, body, status,
             scheduled_for.isoformat() if scheduled_for else None,
             contact, utcnow().isoformat(), utcnow().isoformat()),
        )
        return self.get(draft_id)  # type: ignore[return-value]

    def get(self, draft_id: str) -> Optional[PostDraft]:
        rows = self.db.query("SELECT * FROM post_drafts WHERE draft_id = ?", (draft_id,))
        return self._row(rows[0]) if rows else None

    def list(self, *, channel: str = "") -> List[PostDraft]:
        if channel:
            rows = self.db.query(
                "SELECT * FROM post_drafts WHERE channel = ? ORDER BY created_at DESC", (channel,))
        else:
            rows = self.db.query("SELECT * FROM post_drafts ORDER BY created_at DESC")
        return [self._row(r) for r in rows]

    def update(self, draft_id: str, *, body: Optional[str] = None,
               scheduled_for: Optional[datetime] = None, clear_schedule: bool = False,
               contact: Optional[str] = None) -> Optional[PostDraft]:
        draft = self.get(draft_id)
        if draft is None:
            return None
        sets, params = [], []
        if body is not None and body.strip():
            sets.append("body = ?")
            params.append(body.strip())
        if clear_schedule:
            sets.append("scheduled_for = NULL")
            sets.append("status = 'draft'")
        elif scheduled_for is not None:
            sets.append("scheduled_for = ?")
            sets.append("status = 'scheduled'")
            params.append(scheduled_for.isoformat())
        if contact is not None:
            sets.append("contact = ?")
            params.append(contact)
        if sets:
            sets.append("updated_at = ?")
            params.append(utcnow().isoformat())
            params.append(draft_id)
            self.db.execute(f"UPDATE post_drafts SET {', '.join(sets)} WHERE draft_id = ?",
                            tuple(params))
        return self.get(draft_id)

    def delete(self, draft_id: str) -> bool:
        self.db.execute("DELETE FROM post_drafts WHERE draft_id = ?", (draft_id,))
        return self.get(draft_id) is None

    def due_reminders(self, now: Optional[datetime] = None) -> List[PostDraft]:
        """Scheduled drafts whose date has arrived. A reminder, not a publication."""
        now = now or datetime.now().astimezone()
        out: List[PostDraft] = []
        for draft in self.list():
            if draft.status != "scheduled" or not draft.scheduled_for:
                continue
            try:
                when = datetime.fromisoformat(draft.scheduled_for)
            except ValueError:
                continue
            # A stored timestamp may be naive (an ISO string with no offset) while
            # `now` is aware; comparing them raises. Take the local zone for a
            # naive value so the reminder fires on time either way.
            if when.tzinfo is None:
                when = when.astimezone()
            if when <= now:
                out.append(draft)
        return out

    def draft_from_completed_work(self, *, days: int = 7,
                                  now: Optional[datetime] = None,
                                  planner: Any = None) -> Dict[str, Any]:
        """Draft a recap from tasks you actually finished — nothing invented.

        The draft names only work whose completion is recorded in the planner,
        grouped by project. When nothing was completed in the window it says so
        and creates nothing, rather than producing a fluent post about progress
        that did not happen. It is a *draft*: publishing stays manual.
        """
        from datetime import timedelta

        now = now or datetime.now().astimezone()
        since = now - timedelta(days=days)

        if planner is None:
            from .planner_service import PlannerService

            planner = PlannerService(self.db)

        finished = []
        for task in planner.list_tasks(status="done"):
            done_at = task.done_at
            if done_at is None:
                continue
            if done_at.tzinfo is None:
                done_at = done_at.astimezone()
            if done_at >= since:
                finished.append(task)

        if not finished:
            return {
                "created": None,
                "completed_count": 0,
                "reason": f"nothing was completed in the last {days} day(s), so there is "
                          "nothing true to recap yet",
            }

        by_project: Dict[str, List[str]] = {}
        for task in finished:
            by_project.setdefault(task.project or "General", []).append(task.title)

        lines = [f"A quick recap of the last {days} days:"]
        for project in sorted(by_project):
            lines.append("")
            lines.append(f"{project}:")
            for title in by_project[project]:
                lines.append(f"- {title}")
        lines.append("")
        lines.append("(Drafted by Vantage from completed work. Review before you post — "
                     "it does not publish for you.)")

        draft = self.create("\n".join(lines), channel="linkedin")
        return {
            "created": draft.as_dict(),
            "completed_count": len(finished),
            "projects": sorted(by_project),
            "publishing": self.publishing_supported(),
        }

    @staticmethod
    def publishing_supported() -> Dict[str, Any]:
        """The honest answer about publishing, stated once."""
        return {
            "supported": False,
            "reason": (
                "Vantage has no approved publishing integration for LinkedIn or any other "
                "network. It can draft, edit, schedule and remind; posting is a manual step "
                "you do yourself."
            ),
        }


__all__ = ["PostService", "PostDraft"]