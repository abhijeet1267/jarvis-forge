"""NSStatusItem Menu Bar Integration for Vantage (Phase 87).

Integrates RuntimeSupervisor state with native macOS menu bar item.
Uses Python's Objective-C bridge via PyObjC for NSStatusItem.

Menu displays:
- Runtime status indicator (dot color)
- Status text
- Quick actions (open dashboard, phone devices, quit)

Never guesses state independently—always queries RuntimeSupervisor."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional


def get_menu_bar_state_text(state: str) -> str:
    """Map supervisor state to menu text."""
    states = {
        "not_started": "Starting",
        "starting": "Starting...",
        "ready": "Ready",
        "degraded": "Degraded",
        "stopping": "Stopping...",
        "stopped": "Stopped",
        "failed": "Error",
        "recovering": "Recovering",
    }
    return states.get(state, "Unknown")


def get_menu_indicator_color(state: str) -> str:
    """Map supervisor state to indicator dot color.

    Keyed by the supervisor's state *values* (the same strings
    ``get_menu_bar_state_text`` uses), not by the display text -- the controller
    passes ``supervisor_state`` directly, so a key like ``"error"`` (the label for
    the ``failed`` state) or ``"stopping..."`` would never match and the dot would
    fall through to gray for a state that should be red.
    """
    colors = {
        "not_started": "blue",
        "starting": "blue",
        "ready": "green",
        "degraded": "orange",
        "stopping": "orange",
        "stopped": "gray",
        "failed": "red",
        "recovering": "purple",
    }
    return colors.get(state, "gray")


class MenuBarController:
    """Manages Vantine menu bar presence using NSStatusItem."""
    
    def __init__(self):
        self._status_item = None
        self._supervisor = None
        self._update_timer = None
        self._is_visible = True
        
        # Import PyObjC on demand
        try:
            from Foundation import NSLog
            from AppKit import NSStatusBar, NSStatusItem, NSImage
            self._NS = {"NSStatusBar": NSStatusBar, "NSStatusItem": NSStatusItem, "NSImage": NSImage}
        except ImportError:
            self._NS = None
    
    def initialize(self, supervisor) -> bool:
        """Initialize menu bar integration."""
        if not self._NS:
            print("PyObjC not available - skipping menu bar")
            return False
        
        self._supervisor = supervisor
        self._create_status_item()
        
        # Start periodic updates
        self._start_update_timer()
        
        return True
    
    def _create_status_item(self):
        """Create NSStatusItem instance."""
        ns = self._NS
        
        status_bar = ns["NSStatusBar"].systemStatusBar()
        self._status_item = status_bar.statusItemWithLength_(ns["NSStatusBar"].nsStatusBarButtonLength())
        
        # Set up target/action for menu events
        # Would connect to application delegate in production
        
        return self._status_item
    
    def _start_update_timer(self):
        """Start periodic state refresh."""
        # In production: use NSTimer to poll supervisor every 2 seconds
        pass
    
    def update_display(self):
        """Update menu bar display based on supervisor state."""
        if not self._status_item or not self._supervisor:
            return
        
        # Query supervisor for truthful state
        status = self._supervisor.get_status()
        supervisor_state = status.get("supervisor_state", "unknown")
        
        # Map state to display
        state_text = get_menu_bar_state_text(supervisor_state)
        indicator_color = get_menu_indicator_color(supervisor_state)
        
        # Update status item appearance
        # In production: would modify image/title properties of status_item
    
    def hide(self):
        """Hide menu bar item temporarily."""
        if self._status_item:
            self._is_visible = False
            # In production: set visibleToAll_ = False
    
    def show(self):
        """Show menu bar item."""
        if self._status_item:
            self._is_visible = True
            # In production: set visibleToAll_ = True
    
    def destroy(self):
        """Destroy menu bar item."""
        if self._status_item:
            # In production: remove status item from status bar
            self._status_item = None
            self._supervisor = None


# Singleton instance
MENUBAR_CONTROLLER = None


def get_menu_bar_controller():
    """Get singleton menu bar controller instance."""
    global MENUBAR_CONTROLLER
    
    if MENUBAR_CONTROLLER is None:
        MENUBAR_CONTROLLER = MenuBarController()
    
    return MENUBAR_CONTROLLER

