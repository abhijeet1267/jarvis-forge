"""Email Agent (v0.7).

Reads, searches and drafts mail. Sending exists but is a Level-3 destructive
action, so it is always confirmed and never inferred.
"""

from __future__ import annotations

from ..registry.tools import ToolRegistry
from .base import Agent


class EmailAgent(Agent):
    """Mailbox reading, drafting and confirmed sending."""

    name = "email"

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)

    def capabilities(self) -> set:
        return {"search_email", "read_email", "summarize_email", "draft_email", "send_email"}
