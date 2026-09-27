"""Memory Agent (section 4.10).

Owns persistence of facts and project-graph nodes.
"""

from __future__ import annotations

from ..core.models import PlannedAction, ActionResult
from ..logging_setup import get_logger
from ..tools.base import ToolContext
from .base import Agent

log = get_logger("agents.memory")


class MemoryAgent(Agent):
    name = "memory"

    async def execute(self, action: PlannedAction, ctx: ToolContext) -> ActionResult:
        result = await super().execute(action, ctx)
        log.info("memory action complete", extra_fields={"tool": action.tool, "ok": result.ok})
        return result
