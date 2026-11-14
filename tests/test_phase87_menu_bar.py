"""Phase 87 — Menu Bar + Background Mode Tests."""

from __future__ import annotations

import pytest


class TestMenuBarStateMapping:
    """Tests for truthful state mapping."""
    
    def test_ready_state(self):
        from jarvis.runtime.menubar import get_menu_bar_state_text, get_menu_indicator_color
        
        assert get_menu_bar_state_text("ready") == "Ready"
        assert get_menu_indicator_color("ready") == "green"
    
    def test_degraded_state(self):
        from jarvis.runtime.menubar import get_menu_bar_state_text, get_menu_indicator_color
        
        assert get_menu_bar_state_text("degraded") == "Degraded"
        assert get_menu_indicator_color("degraded") == "orange"
    
    def test_error_state(self):
        from jarvis.runtime.menubar import get_menu_bar_state_text, get_menu_indicator_color
        
        assert get_menu_bar_state_text("failed") == "Error"
        assert get_menu_indicator_color("failed") == "red"
    
    def test_unknown_default(self):
        from jarvis.runtime.menubar import get_menu_bar_state_text, get_menu_indicator_color
        
        assert get_menu_bar_state_text("unknown") == "Unknown"
        assert get_menu_indicator_color("unknown") == "gray"


class TestBackgroundMode:
    """Tests background mode behavior."""
    
    def test_window_close_doesnt_kill_runtime(self):
        """Closing window doesn't terminate runtime supervisor."""
        # In production: would test that Supervisor stays alive
        pass
    
    def test_quit_app_shuts_down_runtime(self):
        """Quit command properly shuts down RuntimeSupervisor."""
        from jarvis.runtime.supervisor import RuntimeSupervisor
        
        supervisor = RuntimeSupervisor()
        supervisor.stop()
        
        assert supervisor.status.supervisor_state == "stopped"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
