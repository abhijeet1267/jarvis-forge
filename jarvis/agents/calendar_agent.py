"""Calendar Agent (v0.7).

Reading is unrestricted; creating, changing and cancelling events are permission
controlled.
"""

from __future__ import annotations

from ..registry.tools import ToolRegistry
from .base import Agent


class CalendarAgent(Agent):
    """Schedule reading, availability and confirmed event changes."""

    name = "calendar"

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)

    def capabilities(self) -> set:
        return {
            "list_events",
            "search_events",
            "next_event",
            "find_free_time",
            "daily_schedule",
            "create_event",
            "update_event",
            "cancel_event",
        }
