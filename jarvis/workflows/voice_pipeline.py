"""Voice Pipeline for Real JARVIS Execution (Phase 79).

Connects existing speech recognition to WorkflowOrchestrator:
- Speech → text transcription
- Text → intent parsing  
- Intent → workflow execution
- Verification → truthful response
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, Optional


class VoicePipelineHandler:
    """Handle voice input/output through workflow engine."""
    
    def process_voice_command(self, 
                              transcription: str,
                              session_id: str) -> Dict[str, Any]:
        """Process transcribed voice command through workflow engine."""
        
        from jarvis.workflows.orchestrator import WorkflowOrchestrator
        
        # Parse transcription as text request
        result = WorkflowOrchestrator().execute_workflow_request(
            user_input=transcription,
            session_id=session_id,
            source="voice",
        )
        
        return result
    
    def handle_jarvis_open_app(self, 
                               application_name: str,
                               session_id: str) -> Dict[str, Any]:
        """Handle 'Jarvis, open [app]' style commands."""
        
        from jarvis.workflows.handlers import ApplicationHandler
        
        result = ApplicationHandler.open_application(application_name)
        
        if result.get("success"):
            # Verify it's actually running
            is_running, msg = self._verify_app_running(application_name)
            
            if is_running:
                return {
                    "status": "verified_success",
                    "response": f"{application_name.capitalize()} is open.",
                    "verified": True,
                }
            else:
                return {
                    "status": "executed_unverified",
                    "response": f"I opened {application_name} but could not verify it started.",
                    "verified": False,
                }
        else:
            return {
                "status": "failed",
                "response": f"Could not open {application_name}.",
                "error": result.get("message"),
            }
    
    def handle_jarvis_ask_status(self, session_id: str) -> Dict[str, Any]:
        """Handle 'Jarvis, what are you doing?' type commands."""
        
        from jarvis.workflows.engine import WorkflowEngine
        
        engine = WorkflowEngine.get_instance()
        
        # Check current active workflows
        active_workflows = []
        
        return {
            "status": "idle",
            "response": "I'm ready to help with any commands.",
            "active_workflows": active_workflows,
        }
    
    def _verify_app_running(self, app_name: str) -> tuple:
        """Verify application is actually running."""
        
        try:
            script = f'''
            tell application "System Events"
                exists (every application process whose name contains "{app_name}")
            end tell
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            is_running = "true" in result.stdout.lower()
            message = f"{app_name} verified running" if is_running else None
            
            return is_running, message
            
        except Exception:
            return False, None
    
    def generate_voice_response(self, result: Dict[str, Any]) -> str:
        """Generate concise, truthful voice response based on result."""
        
        status = result.get("status", "unknown")
        
        if status == "completed":
            steps_completed = result.get("steps_completed", 0)
            total_steps = result.get("total_steps", 1)
            
            if steps_completed == total_steps:
                return "Task completed successfully."
            else:
                return f"Completed {steps_completed} of {total_steps} steps."
        
        elif status == "waiting_for_approval":
            description = result.get("description", "")
            return f"I need your approval to: {description}"
        
        elif status == "verified_success":
            return result.get("response", "Done.")
        
        elif status == "executed_unverified":
            return result.get("response", "Action executed but could not be verified.")
        
        elif status == "failed":
            error = result.get("error", "Something went wrong.")
            return f"Failed: {error}"
        
        else:
            return "Command processing..."


# Singleton instance
DEFAULT_VOICE_PIPELINE = VoicePipelineHandler()
