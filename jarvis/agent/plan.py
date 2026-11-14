"""Plan engine for Vantage Agent Core (Phase 75).

Creates structured, validated executable plans from intents:
- Step-by-step action sequences
- Tool requirements identification
- Approval requirements per step
- Expected state verification
- Dependencies between steps
- Risk assessment per step
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from ..core.models import new_id, utcnow


@dataclass
class Plan:
    """A structured, executable plan."""
    plan_id: str
    intent_id: str
    session_id: str
    
    # Description of what this plan accomplishes
    description: str
    
    # Steps in order
    steps: List["PlanStep"] = field(default_factory=list)
    
    # Metadata
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    status: str = "draft"  # draft, approved, executing, completed, failed, cancelled
    
    # Execution tracking
    current_step_index: int = -1
    steps_completed: int = 0
    steps_total: int = 0
    
    # Result
    result: Optional[str] = None
    failure_reason: Optional[str] = None
    
    def add_step(self, step: "PlanStep") -> None:
        """Add a step to the plan."""
        step.step_index = len(self.steps)
        self.steps.append(step)
        self.steps_total += 1
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database/storage."""
        return {
            "plan_id": self.plan_id,
            "intent_id": self.intent_id,
            "session_id": self.session_id,
            "description": self.description,
            "steps": [step.to_dict() for step in self.steps],
            "created_at": self.created_at,
            "status": self.status,
            "current_step_index": self.current_step_index,
            "steps_completed": self.steps_completed,
            "steps_total": self.steps_total,
            "result": self.result,
            "failure_reason": self.failure_reason,
        }


@dataclass
class PlanStep:
    """A single executable step in a plan."""
    step_id: str
    tool_name: str
    step_index: int = -1
    arguments: Dict[str, Any] = field(default_factory=dict)
    description: str = ""
    
    # Dependencies
    dependencies: List[str] = field(default_factory=list)  # parent step IDs this waits for
    
    # Safety & policy
    risk_level: str = "LOW"  # LOW, MEDIUM, HIGH, CRITICAL
    approval_required: bool = False
    approval_id: Optional[str] = None
    
    # Verification
    expected_state: Optional[str] = None
    verification_method: str = "observe"  # observe, check_file, check_process, etc.
    
    # State tracking
    status: str = "pending"  # pending, running, succeeded, failed, skipped
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    actual_result: Optional[str] = None
    error_message: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database/storage."""
        return {
            "step_id": self.step_id,
            "step_index": self.step_index,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "description": self.description,
            "dependencies": self.dependencies,
            "risk_level": self.risk_level,
            "approval_required": self.approval_required,
            "approval_id": self.approval_id,
            "expected_state": self.expected_state,
            "verification_method": self.verification_method,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "actual_result": self.actual_result,
            "error_message": self.error_message,
        }


class PlanGenerator:
    """Generate executable plans from intents."""
    
    @staticmethod
    def generate(intent) -> Plan:
        """Generate a plan from an Intent object."""
        plan_id = new_id("PLAN")
        
        # Generate steps based on action type
        steps = _generate_steps_for_action(intent.action_type, intent.parameters)
        
        plan = Plan(
            plan_id=plan_id,
            intent_id=intent.intent_id,
            session_id=intent.session_id,
            description=f"Execute {intent.action_type.value}",
        )
        
        for step in steps:
            plan.add_step(step)
        
        return plan


def _generate_steps_for_action(action_type: str, parameters: Dict[str, Any]) -> List[PlanStep]:
    """Generate plan steps for a given action type."""
    
    if action_type == "open_application":
        return _generate_open_app_steps(parameters.get("application", ""))
    
    elif action_type == "quit_application":
        return _generate_quit_app_steps(parameters.get("application", ""))
    
    elif action_type == "navigate_url":
        return _generate_navigate_steps(parameters.get("url", ""))
    
    elif action_type == "run_command":
        return _generate_shell_steps(parameters.get("command", ""))
    
    elif action_type == "search_files":
        return _generate_search_steps(parameters.get("query", ""))
    
    elif action_type == "create_directory":
        return _generate_create_dir_steps(parameters.get("name", ""))
    
    elif action_type == "delete_file":
        return _generate_delete_steps(parameters.get("path", ""))
    
    else:
        # Default: single step
        return [PlanStep(
            step_id=new_id("STEP"),
            tool_name="execute_action",
            arguments={"action_type": action_type, "parameters": parameters},
            description=f"Execute {action_type}",
            risk_level="MEDIUM",
        )]


def _generate_open_app_steps(app_name: str) -> List[PlanStep]:
    """Generate steps for opening an application."""
    steps = []
    
    # Step 1: Open application
    steps.append(PlanStep(
        step_id=new_id("STEP"),
        tool_name="open_application",
        arguments={"application": app_name},
        description=f"Open {app_name}",
        risk_level="LOW",
        expected_state=f"{app_name} process should be running",
        verification_method="check_process",
    ))
    
    # Step 2: Verify it opened
    steps.append(PlanStep(
        step_id=new_id("STEP"),
        tool_name="verify_application_running",
        arguments={"application": app_name},
        description=f"Verify {app_name} is running",
        risk_level="LOW",
        expected_state=f"{app_name} verified as active",
        verification_method="check_process",
    ))
    
    return steps


def _generate_quit_app_steps(app_name: str) -> List[PlanStep]:
    """Generate steps for quitting an application."""
    return [PlanStep(
        step_id=new_id("STEP"),
        tool_name="quit_application",
        arguments={"application": app_name},
        description=f"Quit {app_name}",
        risk_level="HIGH",
        approval_required=True,
        expected_state=f"{app_name} process should not be running",
        verification_method="check_process",
    )]


def _generate_navigate_steps(url: str) -> List[PlanStep]:
    """Generate steps for navigating to a URL."""
    steps = []
    
    # Check if browser is open
    steps.append(PlanStep(
        step_id=new_id("STEP"),
        tool_name="detect_browser",
        arguments={},
        description="Detect active browser",
        risk_level="LOW",
        expected_state="Browser detected or ready to launch",
        verification_method="observe",
    ))
    
    # Navigate
    steps.append(PlanStep(
        step_id=new_id("STEP"),
        tool_name="navigate_url",
        arguments={"url": url},
        description=f"Navigate to {url}",
        risk_level="MEDIUM",
        expected_state="Page should load successfully",
        verification_method="check_page_loaded",
    ))
    
    return steps


def _generate_shell_steps(command: str) -> List[PlanStep]:
    """Generate steps for shell command execution."""
    return [PlanStep(
        step_id=new_id("STEP"),
        tool_name="execute_shell",
        arguments={
            "command": command,
            "timeout": 30,
            "safe_mode": True,
        },
        description=f"Execute command: {command[:50]}...",
        risk_level="HIGH",
        approval_required=True,
        expected_state="Command completes with exit code 0 or documented error",
        verification_method="check_exit_code",
    )]


def _generate_search_steps(query: str) -> List[PlanStep]:
    """Generate steps for file search."""
    return [PlanStep(
        step_id=new_id("STEP"),
        tool_name="search_files",
        arguments={"query": query},
        description=f"Search for '{query}'",
        risk_level="LOW",
        expected_state="Search results returned",
        verification_method="check_results_count",
    )]


def _generate_create_dir_steps(name: str) -> List[PlanStep]:
    """Generate steps for creating directory."""
    return [PlanStep(
        step_id=new_id("STEP"),
        tool_name="create_directory",
        arguments={"name": name},
        description=f"Create directory '{name}'",
        risk_level="MEDIUM",
        expected_state="Directory exists after creation",
        verification_method="check_directory_exists",
    )]


def _generate_delete_steps(path: str) -> List[PlanStep]:
    """Generate steps for file deletion."""
    return [PlanStep(
        step_id=new_id("STEP"),
        tool_name="delete_file",
        arguments={"path": path},
        description=f"Delete file: {path[:50]}...",
        risk_level="HIGH",
        approval_required=True,
        expected_state="File no longer exists",
        verification_method="check_file_not_exists",
    )]


def validate_plan(plan: Plan) -> tuple[bool, List[str]]:
    """Validate a plan before execution.
    
    Returns:
        (is_valid, list_of_errors)
    """
    errors = []
    
    # Check plan has steps
    if not plan.steps:
        errors.append("Plan has no steps")
    
    # Validate each step
    for step in plan.steps:
        # Required fields
        if not step.tool_name:
            errors.append(f"Step {step.step_id} missing tool_name")
        
        if not step.description:
            errors.append(f"Step {step.step_id} missing description")
        
        # Verify step index ordering
        if step.step_index != plan.steps.index(step):
            errors.append(f"Step {step.step_id} has incorrect step_index")
        
        # Validate tool exists (via registry)
        if not _tool_exists(step.tool_name):
            errors.append(f"Tool '{step.tool_name}' not registered")
        
        # Validate arguments match tool schema
        if not _validate_arguments(step.tool_name, step.arguments):
            errors.append(f"Invalid arguments for tool '{step.tool_name}'")
    
    return len(errors) == 0, errors


def _tool_exists(tool_name: str) -> bool:
    """Check if a tool is registered."""
    # This will integrate with tool registry
    # For now, return True for known tools
    known_tools = [
        "open_application",
        "quit_application", 
        "navigate_url",
        "execute_shell",
        "search_files",
        "create_directory",
        "delete_file",
        "verify_application_running",
        "detect_browser",
        "check_page_loaded",
        "check_process",
        "check_exit_code",
        "check_directory_exists",
        "check_file_not_exists",
        "check_results_count",
    ]
    return tool_name in known_tools


def _validate_arguments(tool_name: str, arguments: Dict[str, Any]) -> bool:
    """Validate arguments against tool schema."""
    # Schema validation per tool
    required_args = {
        "open_application": ["application"],
        "quit_application": ["application"],
        "navigate_url": ["url"],
        "execute_shell": ["command"],
        "search_files": ["query"],
        "create_directory": ["name"],
        "delete_file": ["path"],
    }
    
    if tool_name not in required_args:
        return True  # Unknown tool, allow any args
    
    required = required_args[tool_name]
    for arg in required:
        if arg not in arguments:
            return False
    
    return True


def save_plan_to_db(plan: Plan) -> None:
    """Save plan to database."""
    db = get_db()
    
    db.execute("""
        INSERT OR REPLACE INTO plans (
            plan_id, intent_id, session_id, description,
            created_at, status, steps_total
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        plan.plan_id,
        plan.intent_id,
        plan.session_id,
        plan.description,
        plan.created_at,
        plan.status,
        plan.steps_total,
    ))
    
    for step in plan.steps:
        db.execute("""
            INSERT OR REPLACE INTO plan_steps (
                step_id, plan_id, step_index, tool_name, arguments,
                description, dependencies, risk_level, approval_required,
                expected_state, verification_method, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            step.step_id,
            plan.plan_id,
            step.step_index,
            step.tool_name,
            json.dumps(step.arguments),
            step.description,
            json.dumps(step.dependencies),
            step.risk_level,
            step.approval_required,
            step.expected_state,
            step.verification_method,
            step.status,
        ))


# Database schema integration
SCHEMA_PLAN = """
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    session_id TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT DEFAULT 'draft',
    current_step_index INTEGER DEFAULT -1,
    steps_completed INTEGER DEFAULT 0,
    steps_total INTEGER DEFAULT 0,
    result TEXT,
    failure_reason TEXT
);

CREATE TABLE IF NOT EXISTS plan_steps (
    step_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    arguments TEXT DEFAULT '{}',
    description TEXT,
    dependencies TEXT DEFAULT '[]',
    risk_level TEXT DEFAULT 'LOW',
    approval_required INTEGER DEFAULT 0,
    approval_id TEXT,
    expected_state TEXT,
    verification_method TEXT DEFAULT 'observe',
    status TEXT DEFAULT 'pending',
    started_at TEXT,
    completed_at TEXT,
    actual_result TEXT,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_plans_intent ON plans(intent_id);
CREATE INDEX IF NOT EXISTS idx_plan_steps_plan ON plan_steps(plan_id);
CREATE INDEX IF NOT EXISTS idx_plans_status ON plans(status);
"""
