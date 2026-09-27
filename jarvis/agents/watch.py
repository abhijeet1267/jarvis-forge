"""Watch agent (v1.8).

Routes to the watch companion tools. Delivery truth is reported by the tools
themselves: with no bridge configured an item is "queued", never "sent to your
watch", and an unpaired watch gets a refusal rather than a hopeful queue.

The agent can display a confirmation and read back what the wearer decided. It
cannot answer one: no tool of this agent resolves a pending confirmation, so a
prompt sitting on the wrist can never be approved from the software side. That
is asserted by a test, the way the vision agent's "vision never acts" guarantee
is.
"""

from __future__ import annotations

from ..registry.tools import ToolRegistry
from .base import Agent


class WatchAgent(Agent):
    """Status, notifications, haptics, confirmation prompts and quick routines."""

    name = "watch"

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)

    def capabilities(self) -> set:
        """What this agent is allowed to do, for the dashboard and /health."""
        return {
            "get_watch_status",
            "get_watch_outbox",
            "send_watch_notification",
            "vibrate_watch",
            "ask_watch_confirmation",
            "run_watch_routine",
        }
