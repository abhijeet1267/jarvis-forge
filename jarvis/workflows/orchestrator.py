"""Workflow Orchestrator (Phase 77).

Coordinates workflows with the existing Agent Core:

REQUEST -> WORKFLOW -> INTENT -> PLAN -> APPROVAL -> EXECUTION -> VERIFICATION -> RESULT
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..agent.executor import ExecutionStatus
from ..agent.intent import Intent, IntentParser, ActionType
from ..agent.plan import PlanGenerator, validate_plan
from ..db.database import get_db


class WorkflowOrchestrator:
    """Execute workflows through the full agent pipeline."""
    
    def __init__(self):
        self._workflow_runs: Dict[str, Dict] = {}
    
    def execute_workflow_request(
        self,
        user_input: str,
        session_id: str,
        workflow_template: str = "",
    ) -> Dict:
        """Execute a workflow request through the full agent pipeline."""
        
        # Parse intent
        intent = IntentParser.parse(user_input, session_id, source="voice")

        # `POST /workflows/{name}/execute` already knows which template was
        # asked for. Re-deriving it from the prose threw that away: the caller
        # passes "Run End workday", the prose has no mapping entry for it, and
        # the id it does hold ("END_WORKDAY") is never consulted -- so a template
        # named in the URL answered "Please specify which workflow to run."
        known = self._known_workflow_id(workflow_template)
        if known:
            return self._execute_workflow_template(known, session_id, user_input)

        # Check if it's a workflow execution request
        if "run" in user_input.lower() or "execute" in user_input.lower():
            template_name = self._extract_workflow_template(user_input)
            
            if not template_name:
                return {
                    "status": "error",
                    "error": "Please specify which workflow to run.",
                    "template": None,
                }
            
            return self._execute_workflow_template(template_name, session_id, user_input)
        
        # For other requests, fall back to standard agent processing
        return {
            "status": "unknown_intent",
            "error": "This request is not a workflow command.",
            "template": None,
        }
    
    def _execute_workflow_template(
        self,
        template_name: str,
        session_id: str,
        user_input: str,
    ) -> Dict:
        """Execute a specific workflow template."""
        
        # Get workflow engine instance (lazy)
        from ..workflows.engine import get_default_workflow_engine
        
        engine = get_default_workflow_engine()
        
        # Templates are seeded under their bare id (RUN_TESTS, SYSTEM_STATUS, ...).
        # Prefixing "TEMPLATE_" here meant no seeded workflow could ever be found.
        workflow_id = template_name.upper()
        if workflow_id.startswith("TEMPLATE_"):
            workflow_id = workflow_id[len("TEMPLATE_"):]
        
        # Execute
        try:
            run = engine.execute_workflow(
                workflow_id=workflow_id,
                session_id=session_id,
                user_request=user_input,
            )
            
            return {
                "status": "running" if run.status.value == "running" else "completed",
                "run_id": run.run_id,
                "workflow_id": workflow_id,
                "current_status": run.status.value,
                "step": run.current_step_index + 1,
                "total_steps": run.steps_total,
                "result": run.result or run.failure_reason,
            }
            
        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "run_id": None,
                "workflow_id": workflow_id,
            }
    
    @staticmethod
    def _known_workflow_id(name: str) -> Optional[str]:
        """The workflow id a caller named directly, normalized, or None.

        Accepts ``run_tests``, ``RUN_TESTS`` or ``TEMPLATE_RUN_TESTS`` so the
        URL segment, the prose and the seeded id all agree.
        """
        if not name:
            return None
        from ..workflows.engine import TEMPLATE_STEPS

        candidate = str(name).strip().upper()
        if candidate.startswith("TEMPLATE_"):
            candidate = candidate[len("TEMPLATE_"):]
        return candidate if candidate in TEMPLATE_STEPS else None

    def _extract_workflow_template(self, text: str) -> Optional[str]:
        """Extract workflow template name from user input."""
        text_lower = text.lower()
        
        mappings = {
            "workspace": "OPEN_WORKSPACE",
            "development environment": "DEVELOPMENT_START",
            "dev mode": "DEVELOPMENT_START",
            "start dev": "DEVELOPMENT_START",
            "tests": "RUN_TESTS",
            "run tests": "RUN_TESTS",
            "build": "CHECK_BUILD",
            "check build": "CHECK_BUILD",
            "clean downloads": "CLEAN_DOWNLOADS",
            "downloads": "CLEAN_DOWNLOADS",
            "meeting": "PREPARE_MEETING",
            "end day": "END_WORKDAY",
            "end workday": "END_WORKDAY",
            "status": "SYSTEM_STATUS",
            "system status": "SYSTEM_STATUS",
        }
        
        for key, value in mappings.items():
            if key in text_lower:
                return value
        
        return None
    
    def get_running_workflows(self, session_id: str) -> List[Dict]:
        """Get all running workflows for a session."""
        
        db = get_db()
        rows = db.query(
            "SELECT * FROM workflow_runs WHERE session_id = ? AND status IN (?, ?, ?)",
            (session_id, "running", "verifying", "waiting_for_approval")
        )
        
        return [self._row_to_run(row) for row in rows]
    
    def _row_to_run(self, row: dict) -> Dict:
        """Convert database row to workflow run dict."""
        from ..workflows.engine import WorkflowStatus
        
        return {
            "run_id": row["run_id"],
            "workflow_id": row["workflow_id"],
            "status": WorkflowStatus(row["status"]).value,
            "current_step": row.get("current_step_index", -1) + 1,
            "total_steps": row.get("steps_total", 0),
            "steps_completed": row.get("steps_completed", 0),
        }
    
    def cancel_workflow(self, session_id: str, run_id: Optional[str] = None) -> bool:
        """Cancel a running workflow."""
        
        db = get_db()
        
        if run_id:
            # Cancel specific run
            db.execute(
                "UPDATE workflow_runs SET status = ?, completed_at = ?, failure_reason = ? WHERE run_id = ?",
                ("cancelled", utcnow().isoformat(), "Cancelled by user", run_id)
            )
            return True
        
        else:
            # Cancel all active runs for session
            db.execute(
                """UPDATE workflow_runs 
                   SET status = ?, completed_at = ?, failure_reason = ? 
                   WHERE session_id = ? AND status IN ('running', 'verifying')""",
                ("cancelled", utcnow().isoformat(), "Cancelled by user", session_id)
            )
            return True
    
    def get_workflow_status(self, run_id: str) -> Optional[Dict]:
        """Get current status of a workflow run."""
        
        db = get_db()
        row = db.query(
            "SELECT * FROM workflow_runs WHERE run_id = ?",
            (run_id,)
        )
        
        if not row:
            return None
        
        r = row[0]
        
        return {
            "run_id": r["run_id"],
            "workflow_id": r["workflow_id"],
            "status": r["status"],
            "current_step": r.get("current_step_index", -1) + 1,
            "total_steps": r.get("steps_total", 0),
            "steps_completed": r.get("steps_completed", 0),
            "result": r.get("result"),
            "failure_reason": r.get("failure_reason"),
        }
