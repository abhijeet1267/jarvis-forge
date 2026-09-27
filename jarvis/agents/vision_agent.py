"""Vision Agent (v1.3).

Observes images and screens: measurement, OCR, change detection. It then
**proposes** — and stops there.

The plan is explicit that vision must never automatically perform an action
because of what it sees, so this agent deliberately owns no action tools. It
cannot click, type, move or delete. Observations come back as data for the user
or another agent to act on after permission.
"""

from __future__ import annotations

from ..registry.tools import ToolRegistry
from .base import Agent


class VisionAgent(Agent):
    """Observe, read and compare — never act."""

    name = "vision"

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)

    def capabilities(self) -> set:
        return {
            "inspect_image",
            "read_image_text",
            "find_image_text",
            "capture_screen",
            "compare_images",
            "vision_capabilities",
        }
