"""Jarvis Agent Orchestrator (Phase 76).

The central Vantage agent that coordinates:

VOICE/TEXT -> INTENT -> PLAN -> AUTHORITY/APPROVAL -> TOOL EXECUTION -> WORLD OBSERVATION -> VERIFICATION -> RESULT

Never executes arbitrary model text directly through shell/API calls.

Uses approved mechanism for each action type with permission checks, approval flows,
and state verification before reporting success.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..core.models import new_id, utcnow
from ..db.database import get_db
from .intent import Intent, IntentParser, ActionType, save_intent_to_db
from .plan import Plan, PlanGenerator, validate_plan, save_plan_to_db
from .tools import ToolRegistry, RiskLevel
from .executor import ToolExecutor, ExecutionResult, ExecutionStatus


@dataclass
class AgentContext:
    """Context for agent operations."""
    session_id: str
    user_id: str = "user"  # Put defaults after required
    
    # State tracking (all with defaults)
    current_intent: Optional[Intent] = None
    current_plan: Optional[Plan] = None
    current_step: Optional[int] = None
    
    # Approval state
    pending_approval_step: Optional[int] = None
    approval_granted: bool = False
    
    # Verification state
    verification_completed: bool = False
    
    # Result storage
    result: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ActionRecord:
    """Record of an executed action for auditing."""
    request_id: str
    intent_id: str
    plan_id: str
    step_id: str
    execution_id: str
    session_id: str
    started_at: str
    status: str
    tool: str
    
    # All remaining fields have defaults
    completed_at: Optional[str] = None
    verification_status: Optional[str] = None
    risk_level: str = "LOW"
    approval_required: bool = False
    approval_id: Optional[str] = None
    surface: str = "text"
    user_id: str = "user"
    
    # Results
    safe_result: Optional[str] = None
    failure_reason: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "intent_id": self.intent_id,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "execution_id": self.execution_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "status": self.status,
            "completed_at": self.completed_at,
            "verification_status": self.verification_status,
            "tool": self.tool,
            "risk_level": self.risk_level,
            "approval_required": self.approval_required,
            "approval_id": self.approval_id,
            "surface": self.surface,
            "user_id": self.user_id,
            "safe_result": self.safe_result,
            "failure_reason": self.failure_reason,
        }
    failure_reason: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "intent_id": self.intent_id,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "execution_id": self.execution_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "verification_status": self.verification_status,
            "tool": self.tool,
            "risk_level": self.risk_level,
            "approval_required": self.approval_required,
            "approval_id": self.approval_id,
            "safe_result": self.safe_result,
            "failure_reason": self.failure_reason,
        }


class JarvisAgentOrchestrator:
    """Central Vantage agent orchestrator."""
    
    def __init__(self):
        self.registry = ToolRegistry.get_instance()
        self.executor: Optional[ToolExecutor] = None
        self._session_state: Dict[str, AgentContext] = {}
        
        # Database setup
        self._create_action_journal_table()
    
    def _create_action_journal_table(self) -> None:
        """Create action journal table if not exists."""
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS action_journals (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                intent_id TEXT NOT NULL REFERENCES intents(intent_id),
                plan_id TEXT NOT NULL REFERENCES plans(plan_id),
                step_id TEXT NOT NULL,
                execution_id TEXT,
                session_id TEXT NOT NULL,
                user_id TEXT DEFAULT 'user',
                surface TEXT DEFAULT 'text',
                tool TEXT NOT NULL,
                risk_level TEXT DEFAULT 'LOW',
                approval_id TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                execution_status TEXT DEFAULT 'pending',
                verification_status TEXT,
                safe_result TEXT,
                failure_reason TEXT
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_action_session ON action_journals(session_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_action_intent ON action_journals(intent_id)")
    
    def process_request(self, user_input: str, session_id: str, source: str = "text") -> Dict[str, Any]:
        """Process a user request through the full agent pipeline."""
        
        try:
            # Create context
            ctx = self._get_or_create_context(session_id)
            
            # Step 1: Parse intent
            ctx.current_intent = IntentParser.parse(user_input, session_id, source)
            save_intent_to_db(ctx.current_intent)
            
            # Step 2: Generate plan
            plan = PlanGenerator.generate(ctx.current_intent)
            is_valid, errors = validate_plan(plan)
            
            if not is_valid:
                ctx.error = f"Plan validation failed: {errors}"
                return self._build_response(ctx, plan=None)
            
            ctx.current_plan = plan
            save_plan_to_db(plan)
            
            # Step 3: Check approval requirements
            requires_approval, steps_needing_approval = self._check_approval_requirements(plan)
            
            if requires_approval:
                ctx.pending_approval_step = steps_needing_approval[0]
                ctx.approval_granted = False
                
                return {
                    "status": "AWAITING_APPROVAL",
                    "intent_id": ctx.current_intent.intent_id,
                    "plan_id": plan.plan_id,
                    "approval_required_for_step": ctx.pending_approval_step,
                    "approved": False,
                    "result": None,
                    "error": None,
                }
            
            # Step 4: Execute plan (no approval needed)
            return self._execute_plan(ctx)
            
        except Exception as e:
            ctx.error = str(e)
            return self._build_response(ctx, plan=ctx.current_plan)
    
    def approve_step(self, session_id: str, step_index: int) -> Dict[str, Any]:
        """Grant approval for a specific plan step."""
        ctx = self._get_or_create_context(session_id)
        
        if ctx.current_plan is None:
            return {"status": "ERROR", "message": "No plan in progress"}
        
        if ctx.pending_approval_step != step_index:
            return {"status": "ERROR", "message": f"Step {step_index} not pending approval"}
        
        # Record approval
        ctx.approval_granted = True
        ctx.pending_approval_step = None
        
        # Continue execution
        return self._execute_plan(ctx)
    
    def cancel_task(self, session_id: str) -> Dict[str, Any]:
        """Cancel the current task."""
        ctx = self._get_or_create_context(session_id)
        
        if ctx.current_plan is None:
            return {"status": "ERROR", "message": "No active task to cancel"}
        
        ctx.current_plan.status = "cancelled"
        
        return {
            "status": "CANCELLED",
            "plan_id": ctx.current_plan.plan_id,
            "result": None,
            "error": "Task cancelled by user"
        }
    
    def check_task_status(self, session_id: str) -> Dict[str, Any]:
        """Check the status of the current task."""
        ctx = self._get_or_create_context(session_id)
        
        return {
            "status": ctx.current_plan.status if ctx.current_plan else "NONE",
            "intent_id": ctx.current_intent.intent_id if ctx.current_intent else None,
            "plan_id": ctx.current_plan.plan_id if ctx.current_plan else None,
            "current_step": ctx.current_step,
            "steps_completed": ctx.current_plan.steps_completed if ctx.current_plan else 0,
            "total_steps": ctx.current_plan.steps_total if ctx.current_plan else 0,
            "result": ctx.result,
            "error": ctx.error,
        }
    
    def _get_or_create_context(self, session_id: str) -> AgentContext:
        """Get or create session context."""
        if session_id not in self._session_state:
            self._session_state[session_id] = AgentContext(session_id=session_id)
        return self._session_state[session_id]
    
    def _check_approval_requirements(self, plan: Plan) -> Tuple[bool, List[int]]:
        """Check if any plan steps require approval."""
        needs_approval = []
        
        for i, step in enumerate(plan.steps):
            if step.approval_required or step.risk_level in ["HIGH", "CRITICAL"]:
                needs_approval.append(i)
        
        return len(needs_approval) > 0, needs_approval
    
    def _execute_plan(self, ctx: AgentContext) -> Dict[str, Any]:
        """Execute a plan without requiring approval."""
        
        plan = ctx.current_plan
        executor = ToolExecutor(None)  # Initialize executor
        
        # Execute each step sequentially
        for step_idx, step in enumerate(plan.steps):
            try:
                ctx.current_step = step_idx
                
                # Execute step
                result = executor.execute(
                    tool_name=step.tool_name,
                    arguments=step.arguments,
                    session_id=ctx.session_id,
                    step_id=step.step_id,
                )
                
                # Log execution
                self._log_action(
                    request_id=new_id("REQ"),
                    intent_id=plan.intent_id,
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    execution_id=result.execution_id,
                    session_id=ctx.session_id,
                    tool=step.tool_name,
                    risk_level=step.risk_level,
                    approval_required=step.approval_required,
                    status="executed",
                    verification_status=result.verification_status.value if result.verification_status else None,
                    safe_result=result.safe_result_summary,
                    failure_reason=result.failure_reason,
                )
                
                # Update plan state
                plan.steps_completed += 1
                
                # Check if verification succeeded
                if result.verification_status == ExecutionStatus.VERIFIED_SUCCESS:
                    continue  # Continue to next step
                elif result.verification_status == ExecutionStatus.VERIFICATION_FAILED:
                    plan.status = "failed"
                    plan.failure_reason = result.verification_details or "Verification failed"
                    break
                elif result.status == ExecutionStatus.EXECUTION_FAILED:
                    plan.status = "failed"
                    plan.failure_reason = result.failure_reason
                    break
                
            except Exception as e:
                ctx.error = str(e)
                plan.status = "failed"
                break
        
        # Mark complete if all steps succeeded
        if plan.status != "failed":
            plan.status = "completed"
            ctx.result = "All actions completed successfully"
        
        return self._build_response(ctx, plan)
    
    def _build_response(self, ctx: AgentContext, plan: Optional[Plan]) -> Dict[str, Any]:
        """Build response based on context state."""
        
        if ctx.error:
            return {
                "status": "ERROR",
                "intent_id": ctx.current_intent.intent_id if ctx.current_intent else None,
                "plan_id": plan.plan_id if plan else None,
                "result": None,
                "error": ctx.error,
            }
        
        if plan:
            if plan.status == "completed":
                return {
                    "status": "COMPLETED",
                    "intent_id": plan.intent_id,
                    "plan_id": plan.plan_id,
                    "steps_executed": plan.steps_completed,
                    "total_steps": plan.steps_total,
                    "result": ctx.result,
                    "error": None,
                }
            elif plan.status == "failed":
                return {
                    "status": "FAILED",
                    "intent_id": plan.intent_id,
                    "plan_id": plan.plan_id,
                    "error": plan.failure_reason or ctx.error,
                }
        
        return {
            "status": "UNKNOWN",
            "error": "Unknown state",
        }
    
    def _log_action(self, **kwargs) -> None:
        """Log an action to the audit journal."""
        db = get_db()
        
        db.execute("""
            INSERT INTO action_journals 
            (id, request_id, intent_id, plan_id, step_id, execution_id,
             session_id, user_id, surface, tool, risk_level, approval_id,
             started_at, completed_at, execution_status, verification_status,
             safe_result, failure_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            new_id("ACTION"),
            kwargs.get("request_id"),
            kwargs.get("intent_id"),
            kwargs.get("plan_id"),
            kwargs.get("step_id"),
            kwargs.get("execution_id"),
            kwargs.get("session_id"),
            kwargs.get("user_id", "user"),
            kwargs.get("surface", "text"),
            kwargs.get("tool"),
            kwargs.get("risk_level", "LOW"),
            kwargs.get("approval_id"),
            kwargs.get("started_at", utcnow().isoformat()),
            kwargs.get("completed_at"),
            kwargs.get("status", "pending"),
            kwargs.get("verification_status"),
            kwargs.get("safe_result"),
            kwargs.get("failure_reason"),
        ))
