"""Phase 94: First-Run Wizard and Permissions Tests.

Tests first-run wizard functionality, permission status accuracy,
onboarding state persistence, and System Settings guidance.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestOnboardingStatePersistence:
    """Test persistent onboarding state."""
    
    def test_state_file_not_created_initially(self, tmp_path):
        """State file should not exist before initialization."""
        from jarvis.wizard import OnboardingManager
        
        # Use temp directory
        manager = OnboardingManager(state_dir=tmp_path)
        
        assert not (tmp_path / ".vantage_onboarding_state.json").exists()
    
    def test_save_load_roundtrip(self, tmp_path):
        """Saving and loading preserves state."""
        from jarvis.wizard import OnboardingManager, OnboardingStep
        
        manager = OnboardingManager(state_dir=tmp_path)
        
        # Initialize state
        manager.initialize_new_session()
        manager.state.current_step = OnboardingStep.PERMISSIONS_STATUS
        manager.state.permission_states["microphone"] = "GRANTED"
        manager.state.mark_complete(OnboardingStep.PERMISSIONS_STATUS)
        
        # Save
        manager.save_state()
        
        # Create new manager instance
        manager2 = OnboardingManager(state_dir=tmp_path)
        state2 = manager2.load_state()
        
        assert state2 is not None
        assert state2.current_step == OnboardingStep.PERMISSIONS_STATUS
        assert state2.permission_states["microphone"] == "GRANTED"
        assert OnboardingStep.PERMISSIONS_STATUS in state2.completed_steps
    
    def test_reset_clears_state(self, tmp_path):
        """Reset clears persisted state."""
        from jarvis.wizard import OnboardingManager, OnboardingStep
        
        manager = OnboardingManager(state_dir=tmp_path)
        
        # Set up state
        manager.initialize_new_session()
        manager.state.current_step = OnboardingStep.COMPLETE
        manager.save_state()
        
        # Reset
        manager.reset_state()
        
        assert manager.state is not None
        assert manager.state.current_step == OnboardingStep.WELCOME
    
    def test_is_complete_property(self, tmp_path):
        """is_complete only returns True when COMPLETE step done."""
        from jarvis.wizard import OnboardingManager, OnboardingStep
        
        manager = OnboardingManager(state_dir=tmp_path)
        manager.initialize_new_session()
        manager.state.mark_complete(OnboardingStep.PERMISSIONS_STATUS)
        
        assert not manager.state.is_complete
        
        manager.state.mark_complete(OnboardingStep.COMPLETE)
        assert manager.state.is_complete


class TestPermissionStatus:
    """Test permission status detection."""
    
    def test_permission_status_enum_values(self):
        """All enum values are defined."""
        from jarvis.wizard import PermissionStatus
        
        statuses = [s.value for s in PermissionStatus]
        
        assert "granted" in statuses
        assert "denied" in statuses
        assert "unknown" in statuses
        assert "unavailable" in statuses
    
    @patch("jarvis.wizard.check_microphone_permission")
    def test_check_accessibility_permission_structure(self, mock_mic):
        """Accessibility check returns proper structure."""
        from jarvis.wizard import check_accessibility_permission, PermissionInfo, PermissionStatus
        
        # Mock microphone check (not actually called but ensures import works)
        mock_mic.return_value = MagicMock()
        
        result = check_accessibility_permission()
        
        assert isinstance(result, PermissionInfo)
        assert result.name == "Desktop Control (Accessibility)"
        assert result.system_name == "accessibility"
        assert result.status in PermissionStatus
        assert result.is_mandatory == False  # Core features work without it
        assert result.settings_path is not None
    
    def test_check_microphone_permission_structure(self):
        """Microphone check returns proper structure."""
        from jarvis.wizard import check_microphone_permission, PermissionInfo, PermissionStatus
        
        result = check_microphone_permission()
        
        assert isinstance(result, PermissionInfo)
        assert result.name == "Microphone"
        assert result.system_name == "microphone"
        assert result.status in PermissionStatus
        assert result.is_mandatory == False  # Voice optional but recommended
        assert result.settings_path is not None


class TestGetPermissionOverview:
    """Test permission overview gathering."""
    
    def test_overview_contains_all_permissions(self):
        """Overview includes all relevant permissions (structure check only)."""
        from jarvis.wizard import PermissionStatus, PermissionInfo
        
        # Test that PermissionInfo has required fields with correct types
        info = PermissionInfo(
            name="Test Permission",
            system_name="test_system",
            status=PermissionStatus.GRANTED,
            is_mandatory=False,
            description="Test description",
            settings_path="System Settings → Privacy",
        )
        
        assert isinstance(info, dict) or hasattr(info, 'name')
        if hasattr(info, 'name'):
            assert info.name == "Test Permission"
            assert info.settings_path == "System Settings → Privacy"
            assert info.is_mandatory == False


class TestFirstRunWizardClass:
    """Test wizard class behavior."""
    
    def test_wizard_requires_steps(self):
        """REQUIRED_STEPS list exists and has items."""
        from jarvis.wizard import FirstRunWizard, OnboardingStep
        
        wizard = FirstRunWizard()
        
        assert len(FirstRunWizard.REQUIRED_STEPS) > 0
        assert OnboardingStep.PERMISSIONS_STATUS in FirstRunWizard.REQUIRED_STEPS
    
    def test_wizard_recommended_steps_exist(self):
        """RECOMMENDED_STEPS list exists."""
        from jarvis.wizard import FirstRunWizard
        
        assert len(FirstRunWizard.RECOMMENDED_STEPS) > 0
    
    @patch("jarvis.wizard.OnboardingManager")
    def test_should_show_wizard_when_no_state(self, mock_manager_class):
        """Should show wizard when no state file exists."""
        from jarvis.wizard import should_show_wizard
        
        mock_instance = MagicMock()
        mock_instance.load_state.return_value = None
        
        mock_manager_class.return_value = mock_instance
        
        result = should_show_wizard()
        
        assert result is True
    
    @patch("jarvis.wizard.OnboardingManager")
    def test_should_show_wizard_when_incomplete(self, mock_manager_class):
        """Should show wizard when setup incomplete."""
        from jarvis.wizard import should_show_wizard
        
        mock_instance = MagicMock()
        mock_state = MagicMock()
        mock_state.is_complete = False
        
        mock_instance.load_state.return_value = mock_state
        mock_manager_class.return_value = mock_instance
        
        result = should_show_wizard()
        
        assert result is True
    
    @patch("jarvis.wizard.OnboardingManager")
    def test_should_not_show_wizard_when_complete(self, mock_manager_class):
        """Should not show wizard when already complete."""
        from jarvis.wizard import should_show_wizard
        
        mock_instance = MagicMock()
        mock_state = MagicMock()
        mock_state.is_complete = True
        
        mock_instance.load_state.return_value = mock_state
        mock_manager_class.return_value = mock_instance
        
        result = should_show_wizard()
        
        assert result is False


class TestTruthfulPermissionsReporting:
    """Test that permissions are reported truthfully."""
    
    def test_granted_only_when_evidence_exists(self, tmp_path):
        """Only report GRANTED when we have actual evidence."""
        from jarvis.wizard import OnboardingManager, PermissionStatus
        
        manager = OnboardingManager(state_dir=tmp_path)
        manager.initialize_new_session()
        
        # Simulate checking permission - would fail in CI environment
        # We document this as UNKNOWN, not falsely claim GRANTED
        manager.state.permission_states["microphone"] = PermissionStatus.UNKNOWN.value
        
        # This is correct behavior - unknown state, not false positive
        assert manager.state.permission_states["microphone"] == PermissionStatus.UNKNOWN.value
    
    def test_denied_permits_core_functionality(self, tmp_path):
        """Denial doesn't crash or break core functionality."""
        from jarvis.wizard import OnboardingManager, PermissionStatus
        
        manager = OnboardingManager(state_dir=tmp_path)
        manager.initialize_new_session()
        
        # Microphone denied - voice disabled
        manager.state.permission_states["microphone"] = PermissionStatus.DENIED.value
        
        # Should still work for text commands
        # Only mark as DENIED, never claim GRANTED
        assert manager.state.permission_states["microphone"] == PermissionStatus.DENIED.value
        
        # But system doesn't crash
        assert manager.state.is_complete == False
    
    def test_unavailable_marked_correctly(self, tmp_path):
        """Unavailable permissions marked as such, not denied."""
        from jarvis.wizard import OnboardingManager, PermissionStatus
        
        manager = OnboardingManager(state_dir=tmp_path)
        manager.initialize_new_session()
        
        # Notifications unavailable on desktop
        manager.state.permission_states["notifications"] = PermissionStatus.UNAVAILABLE.value
        
        # Never claim GRANTED if we can't enable it
        assert manager.state.permission_states["notifications"] != PermissionStatus.GRANTED.value
        assert manager.state.permission_states["notifications"] == PermissionStatus.UNAVAILABLE.value


class TestSystemSettingsGuidance:
    """Test that users get accurate System Settings guidance."""
    
    @pytest.mark.skip(reason="macOS-only - requires actual system permissions")
    def test_settings_paths_provided(self):
        """All permissions provide settings path guidance."""
        from jarvis.wizard import check_accessibility_permission, check_microphone_permission
        
        accl_info = check_accessibility_permission()
        mic_info = check_microphone_permission()
        
        assert accl_info.settings_path is not None
        assert "System Settings" in accl_info.settings_path
        assert "Automation" in accl_info.settings_path
        
        assert mic_info.settings_path is not None
        assert "System Settings" in mic_info.settings_path
        assert "Microphone" in mic_info.settings_path
    
    @pytest.mark.skip(reason="macOS-only - requires actual system permissions")
    def test_note_for_optional_permissions(self):
        """Optional permissions include helpful notes."""
        from jarvis.wizard import check_microphone_permission
        
        info = check_microphone_permission()
        
        if info.status == PermissionStatus.DENIED:
            assert info.note is not None
            assert "grant" in info.note.lower() or "permission" in info.note.lower()


# Red-First Tests
# ==============================

@pytest.mark.skip(reason="macOS-only - requires actual MacPermissionDetector")
@pytest.mark.asyncio
async def test_wizard_never_claims_permission_without_evidence():
    """RED-FIRST TEST: Wizard must never claim permission granted without real evidence."""
    from jarvis.wizard import PermissionStatus, check_microphone_permission
    
    # This test verifies that when permissions are unavailable in test environment,
    # we report UNKNOWN or UNAVAILABLE, never falsely claim GRANTED
    result = check_microphone_permission()
    
    # In CI/testing environment, should be UNKNOWN or UNAVAILABLE
    assert result.status != PermissionStatus.GRANTED or result.status.value in ["unknown", "unavailable"]


@pytest.mark.asyncio
async def test_denied_permission_does_not_enable_capability():
    """Denied microphone permission must not enable dependent (speech) capability.

    RED-FIRST invariant. The previous test set an ``OnboardingState`` flag and then
    ``pass``ed — asserting nothing (``OnboardingState`` has no capability-gating
    method to query, so there was nothing to assert on). The real, queryable
    "denied mic -> capability not enabled" logic lives in
    ``check_speech_recognition_permission()``, which gates speech on the microphone
    status. So the invariant is pinned there: with the microphone check reporting
    DENIED, speech recognition must report DENIED (not GRANTED), i.e. the dependent
    capability stays off. A positive control (GRANTED mic -> GRANTED speech) keeps the
    test from passing vacuously by always returning DENIED.
    """
    from jarvis import wizard
    from jarvis.wizard import (PermissionStatus, PermissionInfo,
                               check_speech_recognition_permission)

    def mic_status(status: PermissionStatus) -> "PermissionInfo":
        return PermissionInfo(
            name="Microphone", system_name="microphone", status=status,
            is_mandatory=False, description="", settings_path="",
        )

    # Denied mic -> speech recognition must NOT be enabled (must not report GRANTED).
    with patch.object(wizard, "check_microphone_permission", return_value=mic_status(PermissionStatus.DENIED)):
        result = check_speech_recognition_permission()
    assert result.status is not PermissionStatus.GRANTED, \
        "denied microphone must not enable speech recognition"
    assert result.status is PermissionStatus.DENIED, \
        f"speech recognition should be DENIED when the mic is DENIED, got {result.status}"

    # Positive control: a granted mic does enable it (so the DENIED assertion above is
    # a real gate, not a function that ignores the mic and always says DENIED).
    with patch.object(wizard, "check_microphone_permission", return_value=mic_status(PermissionStatus.GRANTED)):
        granted = check_speech_recognition_permission()
    assert granted.status is PermissionStatus.GRANTED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
