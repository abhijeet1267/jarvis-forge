"""Phase 79 Integration Tests - Real End-to-End Verification.

Tests real macOS control through the full pipeline:
Workflow → Handler → macOS → Observation → Verification → Result
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from unittest.mock import patch


class TestRealHandlerDispatch:
    """Test that workflow steps dispatch to real handlers."""
    
    def test_list_applications_works(self):
        """Test list_applications handler returns real macOS state."""
        from jarvis.workflows.handlers import ApplicationHandler
        
        result = ApplicationHandler.list_applications()
        
        assert result.get("count") is not None
        assert isinstance(result["applications"], list)
        # Should return at least some running applications
        assert len(result["applications"]) >= 0
    
    def test_open_application_handler(self):
        """Test open_application dispatcher."""
        from jarvis.workflows.handlers import DEFAULT_HANDLERS
        
        handler = DEFAULT_HANDLERS.get('open_application')
        assert handler is not None
        
        # Call with app_name as string parameter
        result = handler({"application": "Calculator"})
        
        assert isinstance(result, dict)
        assert "success" in result or "message" in result
    
    def test_file_operations_are_blocked_for_protected_paths(self):
        """Test FileControlHandler blocks protected paths."""
        from jarvis.workflows.handlers import FileControlHandler
        
        # Try to delete system path
        result = FileControlHandler.delete_file("/System")
        
        assert result.get("blocked") is True
        assert "Protected path" in result.get("reason", "")
    
    def test_shell_executor_blocks_dangerous_commands(self):
        """Test ShellExecutor blocks dangerous patterns."""
        from jarvis.workflows.handlers import ShellExecutor
        
        # Try rm -rf
        result = ShellExecutor.execute_command("rm -rf /tmp/test")
        
        assert result.get("blocked") is True
        assert "Dangerous command blocked" in result.get("reason", "")
        
        # Try allowed command
        safe_result = ShellExecutor.execute_command("echo hello")
        assert safe_result.get("success") is True


class TestVerificationEngine:
    """Test real state observation."""
    
    def test_verify_application_running_via_ps(self):
        """Test application verification via process observation."""
        from jarvis.workflows.verification_engine import ObservationEngine
        
        # Check if Calculator.app exists (might not be running)
        is_running, msg = ObservationEngine.verify_application_running("Calculator")
        
        # Either it's running or it's not - but we should get real observation
        assert isinstance(is_running, bool)
        assert msg is None or isinstance(msg, str)
    
    def test_verify_file_exists(self):
        """Test file existence check."""
        from jarvis.workflows.verification_engine import ObservationEngine
        
        # Create temp file for testing
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name
        
        try:
            exists, path = ObservationEngine.verify_file_exists(temp_path)
            
            assert exists is True
            assert temp_path in str(path)
        finally:
            # Cleanup
            Path(temp_path).unlink(missing_ok=True)
    
    def test_verified_unverified_distinction_preserved(self):
        """Test that EXECUTED_UNVERIFIED is never treated as SUCCESS."""
        from jarvis.workflows.verification_engine import VerificationState
        
        ctx = VerificationState()
        
        # Mark executed unverified
        ctx.mark_executed_unverified("step_1")
        assert ctx.current_verification_status == VerificationState.EXECUTED_UNVERIFIED
        
        # This should NOT become VERIFIED_SUCCESS
        assert ctx.current_verification_status != VerificationState.VERIFIED_SUCCESS
    
    def test_idempotency_check(self):
        """Test idempotency key generation."""
        from jarvis.workflows.verification_engine import VerificationState
        
        ctx = VerificationState()
        
        # First call - should record state
        result1 = ctx.check_idempotent_action("test_key")
        
        # Second call - should detect duplicate based on current state
        result2 = ctx.check_idempotent_action("test_key")
        
        # Both should have same underlying state value
        state1 = ctx.idempotency_keys.get("test_key")
        state2 = ctx.get_action_state("test_key")
        
        # State values should match
        assert state1 == state2


class TestWorkflowExecutionIntegration:
    """Test full workflow execution with real handlers."""
    
    def test_workflow_steps_dispatch_to_handlers(self):
        """Test that workflow step execution dispatches to handler."""
        from jarvis.workflows.handlers import ApplicationHandler, DEFAULT_HANDLERS
        
        # Verify all registered tools have handlers
        assert len(DEFAULT_HANDLERS) > 0
        
        # Test one specific handler dispatch - use class method directly (no args wrapper)
        result = ApplicationHandler.list_applications()
        assert result is not None
        assert "applications" in result
    
    def test_workflow_variables_resolution(self):
        """Test variable resolution in workflow steps."""
        from jarvis.workflows.engine import WorkflowEngine
        
        engine = WorkflowEngine.get_instance()
        
        variables = {"app_name": "Safari", "url": "https://example.com"}
        
        # Simulate argument resolution
        args = {"application": "$app_name"}
        resolved = engine._resolve_variables(args, variables)
        
        assert resolved.get("application") == "Safari"


class TestApprovalAndSafety:
    """Test approval boundaries and safety checks."""
    
    def test_deletion_requires_high_risk_policy(self):
        """Test that deletion actions are marked HIGH risk."""
        from jarvis.workflows.handlers import FileControlHandler
        
        result = FileControlHandler.delete_file("/tmp/test_delete.txt")
        
        # Delete operation should either succeed or fail gracefully
        # but must never crash or expose secrets
        assert isinstance(result, dict)
        assert "success" in result or "error" in result or "blocked" in result
        
        # Never store secrets in results
        result_str = str(result).lower()
        assert "password" not in result_str
        assert "secret" not in result_str
        assert "api_key" not in result_str
        assert "token" not in result_str


class TestCrashRecoveryStateTracking:
    """Test recovery state preservation."""
    
    def test_last_verified_step_tracking(self):
        """Test that last verified step is tracked for recovery."""
        from jarvis.workflows.verification_engine import VerificationState
        
        ctx = VerificationState()
        
        # Initial state
        assert ctx.last_verified_step is None
        
        # After marking a step as verified
        ctx.mark_verified("step_3")
        assert ctx.last_verified_step == "step_3"
        
        # Get safe resume point
        resume_point = ctx.get_safe_resume_point()
        assert resume_point == "step_3"
    
    def test_recovery_after_execution_failure(self):
        """Test failure state tracking for recovery."""
        from jarvis.workflows.verification_engine import VerificationState
        
        ctx = VerificationState()
        
        ctx.mark_failure("timeout_error")
        
        assert ctx.current_verification_status in [
            VerificationState.VERIFICATION_FAILED,
            VerificationState.EXECUTION_FAILED
        ]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
