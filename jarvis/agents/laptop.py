"""Laptop Agent (section 4.2 / Phase 3).

Owns application launch, file operations and system queries for the primary
computer. Prefers official APIs and CLI over GUI automation, per section 10.
"""

from __future__ import annotations

from ..core.models import PlannedAction, ActionResult
from ..logging_setup import get_logger
from ..tools.base import ToolContext
from .base import Agent

log = get_logger("agents.laptop")


class LaptopAgent(Agent):
    """Executes computer-control tools on the local machine."""

    name = "laptop"

    async def execute(self, action: PlannedAction, ctx: ToolContext) -> ActionResult:
        # Refuse anything that is not actually a computer tool.
        if not self.owns(action.tool):
            from ..tools.base import fail

            return fail(
                action.tool,
                f"{action.tool!r} is not a laptop capability",
                device=action.device,
            )

        # Ensure the context points at the right device for this action.
        ctx.device_id = action.device
        result = await super().execute(action, ctx)

        # Record which platform answered, but never assert that the action was
        # verified: `verified` is the handler's claim about the world, and
        # stamping it True here overwrote real observations in tools/files.py
        # and tools/system.py -- reintroducing the exact defect
        # tools/base.py documents as fixed. ActionResult reconciles
        # `verified` with `verification` at construction; this does not.
        if result.ok and not result.verification_note:
            result.verification_note = f"executed on {ctx.platform} ({action.device})"

        log.info(
            "laptop action complete",
            extra_fields={"tool": action.tool, "ok": result.ok, "ms": result.duration_ms},
        )
        return result
