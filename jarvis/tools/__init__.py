"""Tool package.

``build_default_registry`` assembles every v0.1 capability into a single
:class:`ToolRegistry` and mirrors the specs into the ``tools`` table so the
dashboard can show the live catalogue.
"""

from __future__ import annotations

from ..core.models import ToolSpec
from ..config import get_settings
from ..db.database import get_db
from ..integrations import mcp_client
from ..logging_setup import get_logger
from ..registry.tools import ToolRegistry
from . import (
    agents,
    browser,
    browser_tools,
    coding_agent,
    apps,
    calendar,
    car,
    clipboard,
    coding,
    design,
    desktop,
    desktop_native,
    email,
    files,
    google_tasks,
    home,
    knowledge,
    laptop_center,
    mcp_tools,
    n8n_tools,
    memory_controls,
    memory_tools,
    phone,
    persistence,
    planner,
    research,
    robotics,
    scheduler,
    self_state,
    storage,
    system,
    websearch,
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
        *clipboard.SPECS,
        *files.SPECS,
        *system.SPECS,
        *storage.SPECS,
        *knowledge.SPECS,
        *memory_tools.SPECS,
        *phone.SPECS,
        *email.SPECS,
        *calendar.SPECS,
        *google_tasks.SPECS,
        *mcp_tools.specs_for(mcp_client.active_connector() or
                             mcp_client.connector_from_settings(get_settings())),
        *n8n_tools.specs_for(),
        *scheduler.SPECS,
        *memory_controls.SPECS,
        *planner.SPECS,
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
        *websearch.SPECS,
        *persistence.SPECS,
        *desktop.SPECS,
        *desktop_native.SPECS,
        *browser_tools.SPECS,
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
