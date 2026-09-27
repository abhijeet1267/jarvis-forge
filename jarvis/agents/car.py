"""Car agent (v1.7).

Routes to the car gateway's read-only tools. Reachability is reported by the
tools themselves: with no OBD-II adapter or vehicle API connected they name the
gap rather than inventing a number, which is the rule plan §18 sets for vehicle
telemetry.

The agent has no actuation tools by design (Phase 10 asks for a read-only
prototype), so nothing reachable here can make the vehicle do anything. That is
asserted structurally by a test, the way the Vision Agent's "vision never acts"
guarantee is.
"""

from __future__ import annotations

from ..registry.tools import ToolRegistry
from .base import Agent


class CarAgent(Agent):
    """Vehicle telemetry: status, metrics, capability discovery, diagnostics."""

    name = "car"

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)

    def capabilities(self) -> set:
        """What this agent is allowed to do, for the dashboard and /health."""
        return {
            "get_car_status",
            "get_car_telemetry",
            "list_car_metrics",
            "get_car_diagnostics",
            "get_car_service_status",
        }
