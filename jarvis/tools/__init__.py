"""Tool package.

``build_default_registry`` assembles every v0.1 capability into a single
:class:`ToolRegistry` and mirrors the specs into the ``tools`` table so the
dashboard can show the live catalogue.
"""

from __future__ import annotations

from ..core.models import ToolSpec
from ..db.database import get_db
from ..logging_setup import get_logger
from ..registry.tools import ToolRegistry
from . import (
    agents,
    browser,
    coding_agent,
    apps,
    calendar,
    car,
    coding,
    design,
    email,
    files,
    home,
    knowledge,
    laptop_center,
    memory_controls,
    memory_tools,
    phone,
    research,
    robotics,
    scheduler,
    self_state,
    storage,
    system,
    vision,
    watch,
)

log = get_logger("tools")


def _all_specs():
    return [
        *agents.SPECS,
        *browser.SPECS,
        *coding_agent.SPECS,
        *apps.SPECS,
        *files.SPECS,
        *system.SPECS,
        *storage.SPECS,
        *knowledge.SPECS,
        *memory_tools.SPECS,
        *phone.SPECS,
        *email.SPECS,
        *calendar.SPECS,
        *scheduler.SPECS,
        *memory_controls.SPECS,
        *research.SPECS,
        *coding.SPECS,
        *laptop_center.SPECS,
        *design.SPECS,
        *vision.SPECS,
        *home.SPECS,
        *robotics.SPECS,
        *car.SPECS,
        *watch.SPECS,
        *self_state.SPECS,
    ]


def build_default_registry() -> ToolRegistry:
    """Create a registry populated with the built-in tools."""
    registry = ToolRegistry()
    for spec, handler in _all_specs():
        registry.register(spec, handler)
    _persist_specs(registry.all())
    log.info("tool registry built", extra_fields={"count": len(registry.names())})
    return registry


def _persist_specs(specs: list[ToolSpec]) -> None:
    db = get_db()
    for spec in specs:
        db.execute(
            """
            INSERT INTO tools (name, agent, risk, requires_confirmation, reversible, description)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                agent=excluded.agent,
                risk=excluded.risk,
                requires_confirmation=excluded.requires_confirmation,
                reversible=excluded.reversible,
                description=excluded.description
            """,
            (
                spec.name,
                spec.agent,
                int(spec.risk),
                1 if spec.requires_confirmation else 0,
                1 if spec.reversible else 0,
                spec.description,
            ),
        )


__all__ = ["build_default_registry", "ToolRegistry"]
