"""Robotics / sensor-fleet agent (v1.6).

Routes to the robotics gateway tools. All physical reachability is reported
honestly by the tools themselves: an offline bridge means "no motion command
was sent" / "no experiment was started", never a fabricated success.
"""

from __future__ import annotations

from ..registry.tools import ToolRegistry
from .base import Agent


class RoboticsAgent(Agent):
    """Fleet status, telemetry, safe motion commands and experiment routines."""

    name = "robotics"

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)

    def capabilities(self) -> set:
        """What this agent is allowed to do, for the dashboard and /health."""
        return {
            "get_robot_status",
            "list_robots",
            "get_robot_telemetry",
            "command_robot_motion",
            "run_experiment",
        }
