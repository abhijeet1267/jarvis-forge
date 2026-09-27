"""Laptop Control Center agent.

A dedicated cockpit for the developer's own machine. Owns the
``laptop_center`` tools: condition snapshot, process list, open-project
status, and the agent-prompt builder used to hand a task off to an external
coding agent. Structurally identical to :class:`LaptopAgent` (local machine,
read-mostly) but keeps the cockpit's tools grouped and auditable as one agent.
"""

from __future__ import annotations

from ..core.models import PlannedAction, ActionResult
from ..logging_setup import get_logger
from ..tools.base import ToolContext
from .base import Agent

log = get_logger("agents.laptop_center")


class LaptopCenterAgent(Agent):
    """Executes laptop-control tools on the local machine."""

    name = "laptop_center"

    async def execute(self, action: PlannedAction, ctx: ToolContext) -> ActionResult:
        # Refuse anything that is not actually a laptop-center tool.
        if not self.owns(action.tool):
            from ..tools.base import fail

            return fail(
                action.tool,
                f"{action.tool!r} is not a laptop-center capability",
                device=action.device,
            )

        # Ensure the context points at the right device for this action.
        ctx.device_id = action.device
        result = await super().execute(action, ctx)

        # Record the platform that answered. Deliberately does not set
        # `verified`: see jarvis/agents/laptop.py for why an agent must not
        # stamp verification onto a result it never observed.
        if result.ok and not result.verification_note:
            result.verification_note = f"laptop center action on {ctx.platform} ({action.device})"

        log.info(
            "laptop center action complete",
            extra_fields={"tool": action.tool, "ok": result.ok, "ms": result.duration_ms},
        )
        return result
