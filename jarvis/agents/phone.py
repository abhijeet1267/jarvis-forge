"""Phone Agent (v0.6).

Owns the device-facing tools: pairing, device status and file transfer. Like
every agent it only *executes*; the permission engine remains the sole
authoriser.
"""

from __future__ import annotations

from ..registry.tools import ToolRegistry
from .base import Agent


class PhoneAgent(Agent):
    """Device pairing, status and secure transfer."""

    name = "phone"

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)

    def capabilities(self) -> set:
        """What this agent is allowed to do, for the dashboard and /health."""
        return {
            "pair_device",
            "revoke_device",
            "device_status",
            "send_file",
            "transfer_status",
            "cancel_transfer",
        }
