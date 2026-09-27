"""Agent base classes.

An *agent* owns a set of tools for one device or domain. The orchestrator
never calls a tool handler directly -- it goes through the agent, which keeps
device-specific concerns (platform quirks, capability checks) in one place.
"""

from __future__ import annotations

import time
from abc import ABC
from typing import Dict, List, Optional, Set

from ..core.models import ActionResult, PlannedAction
from ..logging_setup import get_logger
from ..registry.tools import ToolRegistry
from ..tools.base import ToolContext, fail

log = get_logger("agents.base")


class Agent(ABC):
    """Base class for all specialised agents."""

    name: str = "agent"

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    # -- capability ----------------------------------------------------------
    def tool_names(self) -> Set[str]:
        """Tools owned by this agent, derived from their spec's ``agent`` field."""
        return {spec.name for spec in self.registry.all() if spec.agent == self.name}

    def owns(self, tool: str) -> bool:
        return tool in self.tool_names()

    # -- execution -----------------------------------------------------------
    async def execute(self, action: PlannedAction, ctx: ToolContext) -> ActionResult:
        """Run one action and return a verified result."""
        spec = self.registry.get(action.tool)
        handler = self.registry.handler(action.tool)
        if spec is None or handler is None:
            return fail(action.tool, f"no handler for {action.tool!r}", device=action.device)
        if not self.owns(action.tool):
            return fail(
                action.tool,
                f"{self.name} agent does not own {action.tool!r}",
                device=action.device,
            )

        started = time.perf_counter()
        try:
            result = await handler(ctx, **action.params)
        except TypeError as exc:
            # Bad parameters should surface as a clean failure, not a crash.
            return fail(action.tool, f"invalid parameters: {exc}", device=action.device)
        except Exception as exc:  # pragma: no cover - defensive
            log.info(
                "tool raised",
                extra_fields={"tool": action.tool, "error": str(exc)},
            )
            return fail(action.tool, f"{action.tool} raised: {exc}", device=action.device)

        result.duration_ms = int((time.perf_counter() - started) * 1000)
        result.finished_at = result.finished_at
        return result


class ToolAgent(Agent):
    """Generic agent that simply dispatches to the registry."""

    def __init__(self, registry: ToolRegistry, name: str) -> None:
        super().__init__(registry)
        self.name = name


def build_agents(registry: ToolRegistry) -> Dict[str, Agent]:
    """Instantiate one agent per distinct ``spec.agent`` value."""
    from .calendar_agent import CalendarAgent
    from .car import CarAgent
    from .email_agent import EmailAgent
    from .laptop import LaptopAgent
    from .laptop_center import LaptopCenterAgent
    from .memory_agent import MemoryAgent
    from .phone import PhoneAgent
    from .coding_agent import CodingAgent
    from .design_agent import DesignAgent
    from .home import HomeAgent
    from .robotics import RoboticsAgent
    from .vision_agent import VisionAgent
    from .watch import WatchAgent
    from .research_agent import ResearchAgent
    from .scheduler_agent import SchedulerAgent

    agents: Dict[str, Agent] = {
        "laptop": LaptopAgent(registry),
        "laptop_center": LaptopCenterAgent(registry),
        "memory": MemoryAgent(registry),
        "phone": PhoneAgent(registry),
        "email": EmailAgent(registry),
        "calendar": CalendarAgent(registry),
        "scheduler": SchedulerAgent(registry),
        "research": ResearchAgent(registry),
        "coding": CodingAgent(registry),
        "design": DesignAgent(registry),
        "vision": VisionAgent(registry),
        "home": HomeAgent(registry),
        "robotics": RoboticsAgent(registry),
        "car": CarAgent(registry),
        "watch": WatchAgent(registry),
    }
    # Any spec pointing at an agent we do not have a class for gets a generic one.
    for spec in registry.all():
        if spec.agent not in agents:
            agents[spec.agent] = ToolAgent(registry, spec.agent)
    return agents


def find_agent(agents: Dict[str, Agent], tool: str) -> Optional[Agent]:
    for agent in agents.values():
        if agent.owns(tool):
            return agent
    return None


__all__ = ["Agent", "ToolAgent", "build_agents", "find_agent"]
