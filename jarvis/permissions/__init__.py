"""macOS permission status for Vantage (build prompt §4)."""

from __future__ import annotations

from .service import CAPABILITIES, PermissionStatusService, describe_permissions

__all__ = ["CAPABILITIES", "PermissionStatusService", "describe_permissions"]
