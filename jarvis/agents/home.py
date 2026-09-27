"""Home / IoT agent (v1.5).

Routes to the home gateway tools. All physical reachability is reported
honestly by the tools themselves: an offline gateway means "no device was
switched", never a fabricated success.
"""

from __future__ import annotations

from ..registry.tools import ToolRegistry
from .base import Agent


class HomeAgent(Agent):
    """Gateway state, device control and home routines."""

    name = "home"

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)

    def capabilities(self) -> set:
        """What this agent is allowed to do, for the dashboard and /health."""
        return {
            "get_home_status",
            "list_home_devices",
            "control_home_device",
            "get_home_reading",
            "run_home_routine",
        }
