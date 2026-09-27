"""Scheduler Agent (v0.8).

Owns recurring jobs and the daily briefing. It can *create* schedules, but it
cannot widen their authority: the runner re-evaluates every job through the
permission engine and refuses anything above the ceiling the job was created
with.
"""

from __future__ import annotations

from ..registry.tools import ToolRegistry
from .base import Agent


class SchedulerAgent(Agent):
    """Recurring jobs and the daily briefing."""

    name = "scheduler"

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)

    def capabilities(self) -> set:
        return {
            "schedule_task",
            "list_schedules",
            "pause_schedule",
            "resume_schedule",
            "cancel_schedule",
            "run_schedule_now",
            "daily_briefing",
        }
