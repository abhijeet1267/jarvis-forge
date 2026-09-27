"""Design Agent (v1.2).

Generates UI concepts, architecture and system diagrams, documentation and
presentations, and keeps the results as linkable assets.

The artifacts are real — valid SVG, self-contained HTML, real Markdown — and
outlines are labelled as outlines rather than presenting empty sections as
finished work.
"""

from __future__ import annotations

from ..registry.tools import ToolRegistry
from .base import Agent


class DesignAgent(Agent):
    """Design and documentation artifacts."""

    name = "design"

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)

    def capabilities(self) -> set:
        return {
            "create_architecture_diagram",
            "create_flowchart",
            "create_ui_concept",
            "create_documentation",
            "create_presentation",
            "list_design_assets",
            "read_design_asset",
            "analyze_design",
            "link_asset_to_project",
            "design_integrations",
            "delete_design_asset",
        }
