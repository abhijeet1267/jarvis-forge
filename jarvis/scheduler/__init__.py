"""Scheduler (v0.8).

Recurring, user-approved jobs plus the daily briefing. A scheduled job carries
its own permission ceiling and is re-evaluated through the permission engine on
every run, so a schedule can never gain authority the user did not grant.
"""

from .runner import BUILTIN_ACTIONS, TICK_SECONDS, SchedulerRunner
from .schedule import Recurrence, parse_schedule
from .store import (
    ACTIVE,
    CANCELLED,
    MAX_SCHEDULABLE_RISK,
    PAUSED,
    ScheduleStore,
    ScheduledJob,
)

__all__ = [
    "SchedulerRunner",
    "ScheduleStore",
    "ScheduledJob",
    "Recurrence",
    "parse_schedule",
    "ACTIVE",
    "PAUSED",
    "CANCELLED",
    "MAX_SCHEDULABLE_RISK",
    "BUILTIN_ACTIONS",
    "TICK_SECONDS",
]
