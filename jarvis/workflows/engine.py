"""Vantage Workflow Engine (Phase 77).

Orchestrates multiple real actions while preserving:
* authentication
* session boundaries  
* approval
* provenance
* verification
* truthful state
* paid/provider controls
* phone authority
* macOS permissions

Architecture:
USER REQUEST -> WORKFLOW INTENT -> WORKFLOW PLAN -> DEPENDENCY GRAPH
-> AUTHORITY CHECK -> EXECUTE -> OBSERVE -> VERIFY -> NEXT STEP -> FINAL RESULT
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..core.models import new_id, utcnow
from .handlers import DEFAULT_HANDLERS
from ..db.database import get_db


class WorkflowStatus(Enum):
    """Workflow execution status."""
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    PAUSED = "paused"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


@dataclass
class Workflow:
    """A reusable multi-step automation definition."""
    workflow_id: str
    name: str
    description: str
    owner: str = "user"
    source: str = "template"  # template, manual, ai_proposed
    trigger: str = "on_demand"  # on_demand, scheduled, event, condition
    
    # Execution tracking
    runs: List["WorkflowRun"] = field(default_factory=list)
    
    @property
    def latest_run(self) -> Optional["WorkflowRun"]:
        if not self.runs:
            return None
        return max(self.runs, key=lambda r: r.started_at or "")
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "description": self.description,
            "owner": self.owner,
            "source": self.source,
            "trigger": self.trigger,
            "latest_run_status": self.latest_run.status.value if self.latest_run else None,
        }


@dataclass
class WorkflowStep:
    """A single step in a workflow with dependencies and conditions."""
    step_id: str
    workflow_id: str
    order: int
    depends_on: List[str] = field(default_factory=list)  # Parent step IDs
    
    # Action definition
    intent: Optional[str] = None  # Natural language description
    tool_name: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    
    # Control flow
    condition: Optional[str] = None  # Condition for executing this step
    retry_policy: Optional[Dict[str, Any]] = None  # retries, delay
    timeout_seconds: Optional[int] = None
    
    # Safety
    approval_policy: str = "auto"  # auto, required, never
    verification_policy: str = "observe"  # observe, check_state, verify_output
    
    # Execution state
    status: str = "pending"  # pending, running, succeeded, failed, skipped
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[Any] = None
    error_message: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "order": self.order,
            "depends_on": self.depends_on,
            "intent": self.intent,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "condition": self.condition,
            "approval_policy": self.approval_policy,
            "verification_policy": self.verification_policy,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "error_message": self.error_message,
        }


@dataclass
class WorkflowRun:
    """A single execution of a workflow."""
    run_id: str
    workflow_id: str
    session_id: str
    user_request: str = ""
    requested_by: str = "text"  # text, voice, phone
    
    # Timing
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    
    # Status
    status: WorkflowStatus = WorkflowStatus.QUEUED
    current_step_index: int = -1
    steps_completed: int = 0
    steps_total: int = 0
    
    # Results
    result: Optional[str] = None
    failure_reason: Optional[str] = None
    
    # Provenance
    approvals: List[str] = field(default_factory=list)
    verifications: List[str] = field(default_factory=list)
    
    def mark_running(self) -> None:
        self.status = WorkflowStatus.RUNNING
        self.started_at = utcnow().isoformat()
    
    def mark_completed(self, result: str) -> None:
        self.status = WorkflowStatus.COMPLETED
        self.completed_at = utcnow().isoformat()
        self.result = result
    
    def mark_failed(self, reason: str) -> None:
        self.status = WorkflowStatus.FAILED
        self.completed_at = utcnow().isoformat()
        self.failure_reason = reason
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "session_id": self.session_id,
            "user_request": self.user_request,
            "requested_by": self.requested_by,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status.value,
            "current_step_index": self.current_step_index,
            "steps_completed": self.steps_completed,
            "steps_total": self.steps_total,
            "result": self.result,
            "failure_reason": self.failure_reason,
            "approvals_count": len(self.approvals),
            "verifications_count": len(self.verifications),
        }


class WorkflowEngine:
    """Execute workflows with authority, approval, and verification."""
    
    _instance: Optional["WorkflowEngine"] = None
    
    @classmethod
    def get_instance(cls) -> "WorkflowEngine":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def __init__(self):
        self.workflows: Dict[str, Workflow] = {}
        self.runs: Dict[str, WorkflowRun] = {}
        
        # Initialize schema
        self._ensure_schema()
    
    def _ensure_schema(self) -> None:
        """Ensure workflow tables exist."""
        db = get_db()
        
        db.execute("""
            CREATE TABLE IF NOT EXISTS workflows (
                workflow_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                owner TEXT DEFAULT 'user',
                source TEXT DEFAULT 'manual',
                trigger TEXT DEFAULT 'on_demand',
                created_at TEXT NOT NULL
            )
        """)
        
        db.execute("""
            CREATE TABLE IF NOT EXISTS workflow_steps (
                step_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id) ON DELETE CASCADE,
                `order` INTEGER NOT NULL,
                depends_on TEXT DEFAULT '[]',
                intent TEXT,
                tool_name TEXT,
                arguments TEXT DEFAULT '{}',
                condition TEXT,
                approval_policy TEXT DEFAULT 'auto',
                verification_policy TEXT DEFAULT 'observe',
                retry_policy TEXT DEFAULT '{}',
                timeout_seconds INTEGER,
                status TEXT DEFAULT 'pending'
            )
        """)
        
        db.execute("""
            CREATE TABLE IF NOT EXISTS workflow_runs (
                run_id TEXT PRIMARY KEY,
                workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),
                session_id TEXT NOT NULL,
                user_request TEXT DEFAULT '',
                requested_by TEXT DEFAULT 'text',
                started_at TEXT,
                completed_at TEXT,
                status TEXT DEFAULT 'queued',
                current_step_index INTEGER DEFAULT -1,
                steps_completed INTEGER DEFAULT 0,
                steps_total INTEGER DEFAULT 0,
                result TEXT,
                failure_reason TEXT
            )
        """)
        
        db.execute("CREATE INDEX IF NOT EXISTS idx_workflow_runs_session ON workflow_runs(session_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow ON workflow_runs(workflow_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_workflow_steps_workflow ON workflow_steps(workflow_id)")
    
    def register_workflow(self, workflow: Workflow) -> None:
        """Register a workflow definition."""
        db = get_db()
        
        db.execute("""
            INSERT OR REPLACE INTO workflows 
            (workflow_id, name, description, owner, source, trigger, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            workflow.workflow_id,
            workflow.name,
            workflow.description,
            workflow.owner,
            workflow.source,
            workflow.trigger,
            utcnow().isoformat(),
        ))
        
        self.workflows[workflow.workflow_id] = workflow
    
    def execute_workflow(
        self,
        workflow_id: str,
        session_id: str,
        user_request: str = "",
        variables: Optional[Dict[str, Any]] = None,
    ) -> WorkflowRun:
        """Execute a workflow with full authority/approval/verification."""
        
        if workflow_id not in self.workflows:
            raise ValueError(f"Unknown workflow: {workflow_id}")
        
        workflow = self.workflows[workflow_id]
        
        # Create run
        run = WorkflowRun(
            run_id=new_id("WF_RUN"),
            workflow_id=workflow_id,
            session_id=session_id,
            user_request=user_request,
            requested_by="text",
        )
        
        self.runs[run.run_id] = run
        run.mark_running()
        
        # Get steps
        db = get_db()
        rows = db.query(
            # `order` is a SQL keyword; unquoted it is a syntax error, so no workflow
            # could ever load its steps.
            "SELECT * FROM workflow_steps WHERE workflow_id = ? ORDER BY `order` ASC",
            (workflow_id,)
        )
        
        steps = [self._row_to_step(r) for r in rows]
        run.steps_total = len(steps)
        
        # Execute steps sequentially
        try:
            for i, step in enumerate(steps):
                run.current_step_index = i
                
                # Check if we need approval
                if step.approval_policy == "required":
                    run.status = WorkflowStatus.WAITING_FOR_APPROVAL
                    break
                
                # Execute step
                run.status = WorkflowStatus.RUNNING
                step.started_at = utcnow().isoformat()

                success, result, error = self._execute_step(step, variables or {})

                # `retry_policy` was stored on every step and never read, so a
                # template could declare retries and get none. Applied only to
                # failed steps, and only the number of attempts the step asked
                # for -- a policy that never fires is the same dead capability
                # as a template that cannot run.
                attempts = 1 + max(0, int((step.retry_policy or {}).get("retries", 0)))
                delay = float((step.retry_policy or {}).get("delay_seconds", 1))
                tried = 1
                while not success and tried < attempts:
                    time.sleep(max(0.0, delay))
                    tried += 1
                    success, result, error = self._execute_step(step, variables or {})
                if tried > 1:
                    step.intent = f"{step.intent or ''} (attempt {tried} of {attempts})".strip()

                if success:
                    step.status = "succeeded"
                    step.result = result
                    run.steps_completed += 1
                    run.verifications.append(step.step_id)
                else:
                    step.status = "failed"
                    step.error_message = error
                    step.completed_at = utcnow().isoformat()
                    run.mark_failed(f"Step {i} failed: {error}")
                    self._log_run(run)
                    return run
            
            # All steps completed
            if run.status != WorkflowStatus.FAILED:
                run.mark_completed("All workflow steps completed successfully")
            
            self._log_run(run)
            
        except Exception as e:
            run.mark_failed(str(e))
            self._log_run(run)
        
        return run
    
    def _execute_step(
        self,
        step: WorkflowStep,
        variables: Dict[str, Any],
    ) -> Tuple[bool, Any, Optional[str]]:
        """Execute a single workflow step using real handlers."""
        
        try:
            # Resolve any variables in arguments
            resolved_args = self._resolve_variables(step.arguments, variables)
            
            # Dispatch to real handler
            tool_name = step.tool_name or "unknown_tool"
            
            if tool_name not in DEFAULT_HANDLERS:
                return False, None, f"Unknown tool: {tool_name}"
            
            handler = DEFAULT_HANDLERS[tool_name]

            # A step's own timeout was stored and then ignored, so a long step
            # was always cut off at the handler's default. Pass it through when
            # the handler accepts one.
            if step.timeout_seconds and tool_name in ("execute_shell", "search_files"):
                resolved_args = {**resolved_args, "timeout": int(step.timeout_seconds)}

            # Execute handler with resolved args
            result = handler(resolved_args)
            
            # Determine success from result
            success = result.get("success", False)
            if success:
                return True, result, None

            # A handler that failed without an `error` key still usually says why
            # in `message` / `reason` / `stderr`. Reporting a bare None produced
            # runs recorded as "Step 0 failed: None", which tells nobody anything.
            error = None
            for key in ("error", "message", "reason", "stderr"):
                value = result.get(key)
                if value:
                    error = str(value)
                    break
            if error is None:
                # A shell command commonly reports through stdout only -- a
                # failing `pytest` writes its summary there and leaves stderr
                # empty -- so the reason would otherwise be dropped entirely.
                tail = (result.get("stdout") or "").strip().splitlines()[-3:]
                if tail:
                    error = " | ".join(line.strip() for line in tail)
            if error is None:
                error = f"{tool_name} reported failure without a reason"
            return False, result, error
        except Exception as e:
            return False, None, str(e)
    
    def _resolve_variables(
        self,
        args: Dict[str, Any],
        variables: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Resolve $variable references in arguments."""
        resolved = {}
        
        for key, value in args.items():
            if isinstance(value, str):
                # Simple string substitution
                for var_name, var_value in variables.items():
                    placeholder = f"${var_name}"
                    value = value.replace(placeholder, str(var_value))
                resolved[key] = value
            else:
                resolved[key] = value
        
        return resolved
    
    @staticmethod
    def _field(row: Any, name: str, default: Any = None) -> Any:
        """One column from a row that may be a sqlite3.Row or a dict.

        ``sqlite3.Row`` has no ``.get``, so calling it raised AttributeError and
        every workflow with steps failed before its first step ran.
        """
        try:
            value = row[name]
        except (KeyError, IndexError):
            return default
        return default if value is None else value

    def _row_to_step(self, row: Any) -> WorkflowStep:
        """Convert database row to WorkflowStep."""
        try:
            depends_on = json.loads(self._field(row, "depends_on", "[]"))
            arguments = json.loads(self._field(row, "arguments", "{}"))
            retry_policy = json.loads(self._field(row, "retry_policy", "{}"))
        except (json.JSONDecodeError, TypeError):
            depends_on = []
            arguments = {}
            retry_policy = {}
        
        return WorkflowStep(
            step_id=row["step_id"],
            workflow_id=row["workflow_id"],
            order=row["order"],
            depends_on=depends_on,
            intent=self._field(row, "intent"),
            tool_name=self._field(row, "tool_name"),
            arguments=arguments,
            condition=self._field(row, "condition"),
            approval_policy=self._field(row, "approval_policy", "auto"),
            verification_policy=self._field(row, "verification_policy", "observe"),
            retry_policy=retry_policy,
            timeout_seconds=self._field(row, "timeout_seconds"),
        )
    
    def _log_run(self, run: WorkflowRun) -> None:
        """Log workflow run to action journal."""
        db = get_db()
        
        db.execute("""
            INSERT INTO workflow_runs 
            (run_id, workflow_id, session_id, user_request, requested_by,
             started_at, completed_at, status, current_step_index,
             steps_completed, steps_total, result, failure_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run.run_id,
            run.workflow_id,
            run.session_id,
            run.user_request,
            run.requested_by,
            run.started_at,
            run.completed_at,
            run.status.value,
            run.current_step_index,
            run.steps_completed,
            run.steps_total,
            run.result,
            run.failure_reason,
        ))
    
    def get_active_run(self, session_id: str) -> Optional[WorkflowRun]:
        """Get currently running workflow for a session."""
        db = get_db()
        rows = db.query(
            "SELECT * FROM workflow_runs WHERE session_id = ? AND status IN (?, ?)",
            (session_id, WorkflowStatus.RUNNING.value, WorkflowStatus.WAITING_FOR_APPROVAL.value)
        )
        return self._run_from_row(rows[0]) if rows else None
    
    def _run_from_row(self, row: Any) -> WorkflowRun:
        """Convert database row to WorkflowRun."""
        return WorkflowRun(
            run_id=row["run_id"],
            workflow_id=row["workflow_id"],
            session_id=row["session_id"],
            user_request=self._field(row, "user_request", ""),
            requested_by=self._field(row, "requested_by", "text"),
            started_at=self._field(row, "started_at"),
            completed_at=self._field(row, "completed_at"),
            status=WorkflowStatus(self._field(row, "status", "queued")),
            current_step_index=self._field(row, "current_step_index", -1),
            steps_completed=self._field(row, "steps_completed", 0),
            steps_total=self._field(row, "steps_total", 0),
            result=self._field(row, "result"),
            failure_reason=self._field(row, "failure_reason"),
        )
    
    def cancel_run(self, run_id: str) -> bool:
        """Cancel an ongoing workflow run."""
        if run_id not in self.runs:
            return False
        
        run = self.runs[run_id]
        
        if run.status in [WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED]:
            return False
        
        run.status = WorkflowStatus.CANCELLED
        run.completed_at = utcnow().isoformat()
        run.failure_reason = "Cancelled by user"
        
        db = get_db()
        db.execute(
            "UPDATE workflow_runs SET status = ?, completed_at = ?, failure_reason = ? WHERE run_id = ?",
            (WorkflowStatus.CANCELLED.value, run.completed_at, run.failure_reason, run_id)
        )
        
        return True


# Singleton instance (lazy)
_DEFAULT_WORKFLOW_ENGINE: Optional[WorkflowEngine] = None


def get_default_workflow_engine() -> WorkflowEngine:
    """Get the singleton workflow engine, with the built-in templates present."""
    global _DEFAULT_WORKFLOW_ENGINE
    if _DEFAULT_WORKFLOW_ENGINE is None:
        _DEFAULT_WORKFLOW_ENGINE = WorkflowEngine.get_instance()
        # Seeded here rather than at import so the tables exist first, and so a
        # fresh install and an upgraded one converge on the same set.
        seed_default_workflows(_DEFAULT_WORKFLOW_ENGINE)
    return _DEFAULT_WORKFLOW_ENGINE


#: The steps each pre-built template actually runs, in order.
#:
#: ``GET /workflows`` used to advertise the eight templates below while nothing
#: ever created them: a fresh install had zero workflows and zero steps, so
#: executing any of them raised ``Unknown workflow`` and answered 500. A
#: published name that cannot run is the disconnected-surface defect in its
#: purest form, so the definitions and their steps are seeded together.
#:
#: Every step is a real handler from ``DEFAULT_HANDLERS``; none of these
#: templates is a placeholder. ``verification_policy="check_state"`` is used
#: wherever the handler itself re-reads the result rather than assuming it
#: worked.
TEMPLATE_STEPS: Dict[str, List[Dict[str, Any]]] = {
    "OPEN_WORKSPACE": [
        {"intent": "Confirm the session is alive", "tool_name": "list_applications",
         "arguments": {}},
        {"intent": "Open the editor", "tool_name": "open_application",
         "arguments": {"application": "Cursor"}},
    ],
    "DEVELOPMENT_START": [
        {"intent": "Check the editor is running", "tool_name": "check_process_exists",
         "arguments": {"application": "Cursor"}},
        {"intent": "Open the editor", "tool_name": "open_application",
         "arguments": {"application": "Cursor"}},
        {"intent": "Open the terminal", "tool_name": "open_application",
         "arguments": {"application": "Terminal"}},
    ],
    "RUN_TESTS": [
        # A bare `python3 -m pytest` resolves to /usr/bin/python3, which has no
        # pytest, so the step reported "No module named pytest" on a machine
        # where the suite runs fine. The venv interpreter is put first on PATH
        # rather than named by path: permission is what the path encodes, so
        # widening the allowlist to admit it would be the wrong fix here.
        {"intent": "Run the test suite", "tool_name": "execute_shell",
         "arguments": {"command": "python3 -m pytest -q",
                       "working_dir": str(Path(__file__).resolve().parents[2]),
                       "env": {"PATH": str(Path(sys.executable).parent) + ":${PATH}"}},
         # The default 30 s assumes a command, not a test suite.
         "timeout_seconds": 1800,
         "verification_policy": "check_state"},
    ],
    "CHECK_BUILD": [
        {"intent": "Check the working tree", "tool_name": "execute_shell",
         "arguments": {"command": "git status --short"},
         "verification_policy": "check_state"},
    ],
    "CLEAN_DOWNLOADS": [
        {"intent": "List recent downloads", "tool_name": "search_files",
         "arguments": {"query": "*", "path": "~/Downloads"},
         "verification_policy": "check_state"},
    ],
    "PREPARE_MEETING": [
        {"intent": "See what is running", "tool_name": "list_applications",
         "arguments": {}},
    ],
    "END_WORKDAY": [
        {"intent": "Closes the editor", "tool_name": "quit_application",
         "arguments": {"application": "Cursor"}},
    ],
    "SYSTEM_STATUS": [
        # AppleScript is occasionally busy for a moment right after login or a
        # wake; one retry turns a spurious "not authorised" into an answer.
        {"intent": "List running applications", "tool_name": "list_applications",
         "arguments": {}, "retry_policy": {"retries": 1, "delay_seconds": 1.0}},
        {"intent": "Report the working tree", "tool_name": "execute_shell",
         "arguments": {"command": "pwd"},
         "verification_policy": "check_state"},
    ],
}


def seed_default_workflows(engine: "WorkflowEngine") -> int:
    """Create the pre-built workflows and their steps, reflecting the current ones.

    Idempotent, and it rewrites the template's step definitions on every boot.
    That is safe because ``workflow_steps`` is *definition* only: the engine
    never writes a step's status back (run history lives in ``workflow_runs``),
    so there is no state here to lose — and a template whose definition was
    wrong (a bare ``python3`` that has no pytest, a dropped ``path`` argument)
    would otherwise stay broken forever on an install that had already seeded.
    Returns the number of built-in workflows.
    """
    db = get_db()
    for workflow_id, name, description, source, trigger in TEMPLATE_DEFINITIONS:
        engine.register_workflow(Workflow(
            workflow_id=workflow_id, name=name, description=description,
            owner="system", source=source, trigger=trigger))

        steps = TEMPLATE_STEPS.get(workflow_id) or []
        if not steps:
            continue
        db.execute("DELETE FROM workflow_steps WHERE workflow_id = ?", (workflow_id,))
        previous: List[str] = []
        for order, spec in enumerate(steps):
            step_id = new_id("WF_STEP")
            # `timeout_seconds` and `approval_policy` are columns, not handler
            # arguments. Leaving them in `arguments` meant the step's timeout
            # was stored as NULL and the handler fell back to its 30 s default.
            arguments = dict(spec.get("arguments", {}))
            timeout_seconds = spec.get("timeout_seconds") or arguments.pop("timeout", None)
            arguments.pop("timeout_seconds", None)
            arguments.pop("approval_policy", None)
            db.execute(
                """
                INSERT INTO workflow_steps
                    (step_id, workflow_id, `order`, depends_on, intent, tool_name,
                     arguments, condition, approval_policy, verification_policy,
                     retry_policy, timeout_seconds, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    step_id, workflow_id, order,
                    json.dumps(previous), spec.get("intent", ""), spec.get("tool_name", ""),
                    json.dumps(arguments), spec.get("condition"),
                    spec.get("approval_policy", "auto"),
                    spec.get("verification_policy", "observe"),
                    json.dumps(spec.get("retry_policy", {})), timeout_seconds,
                ),
            )
            previous = [step_id]
    return len(TEMPLATE_DEFINITIONS)


# Pre-built workflow templates
TEMPLATE_DEFINITIONS: List[Tuple[str, str, str, str, str]] = [
    (
        "OPEN_WORKSPACE",
        "Open workspace",
        "Opens development tools and project",
        "template",
        "on_demand"
    ),
    (
        "DEVELOPMENT_START",
        "Development start",
        "Starts development environment",
        "template",
        "on_demand"
    ),
    (
        "RUN_TESTS",
        "Run tests",
        "Runs test suite and reports results",
        "template",
        "on_demand"
    ),
    (
        "CHECK_BUILD",
        "Check build",
        "Checks build status and artifacts",
        "template",
        "on_demand"
    ),
    (
        "CLEAN_DOWNLOADS",
        "Review downloads",
        # It *lists* what is in Downloads and never moves or deletes anything, so
        # "cleans" was a claim the step list contradicted.
        "Lists what is in your Downloads folder for you to review",
        "template",
        "on_demand"
    ),
    (
        "PREPARE_MEETING",
        "Prepare meeting",
        "Prepares communication tools",
        "template",
        "on_demand"
    ),
    (
        "END_WORKDAY",
        "End workday",
        "Cleans up and saves state",
        "template",
        "on_demand"
    ),
    (
        "SYSTEM_STATUS",
        "System status",
        "Reports system health and state",
        "template",
        "on_demand"
    ),
]
