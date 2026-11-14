"""Structured intent system for Vantine Agent Core (Phase 75).

Converves user input into verified, executable intents with:
- Intent ID and session tracking
- Requested action type
- Parameters and context
- Risk level assessment
- Approval requirements
- Created timestamp
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from ..core.models import new_id, utcnow


class ActionType(Enum):
    """Supported action types."""
    # Application control
    OPEN_APPLICATION = "open_application"
    QUIT_APPLICATION = "quit_application"
    ACTIVATE_APPLICATION = "activate_application"
    LIST_APPLICATIONS = "list_applications"
    
    # File operations
    OPEN_FILE = "open_file"
    SEARCH_FILES = "search_files"
    CREATE_DIRECTORY = "create_directory"
    MOVE_FILE = "move_file"
    COPY_FILE = "copy_file"
    RENAME_FILE = "rename_file"
    DELETE_FILE = "delete_file"
    
    # Browser/web
    OPEN_BROWSER = "open_browser"
    NAVIGATE_URL = "navigate_url"
    SEARCH_WEB = "search_web"
    
    # Terminal/shell
    RUN_COMMAND = "run_command"
    
    # System
    LOCK_SCREEN = "lock_screen"
    INSPECT_SYSTEM = "inspect_system"
    READ_CLIPBOARD = "read_clipboard"
    WRITE_CLIPBOARD = "write_clipboard"
    
    # Multi-step sequences
    MULTI_STEP = "multi_step"


@dataclass
class Intent:
    """Structured user intent with all required fields."""
    intent_id: str
    session_id: str
    action_type: ActionType
    user_id: str = "user"
    source: str = "unknown"  # voice, text, phone, automation
    parameters: Dict[str, Any] = field(default_factory=dict)
    
    # Assessment fields
    risk_level: str = "LOW"  # LOW, MEDIUM, HIGH, CRITICAL
    approval_required: bool = False
    
    # Tracking
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    processed_at: Optional[str] = None
    status: str = "pending"  # pending, planned, approved, executing, completed, failed, cancelled
    
    # Optional multi-step sequence
    steps: List[IntentStep] = field(default_factory=list)
    
    # Result storage
    result: Optional[str] = None
    verification_status: Optional[str] = None  # verified, failed, unknown
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database/storage."""
        return {
            "intent_id": self.intent_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "source": self.source,
            "action_type": self.action_type.value,
            "parameters": self.parameters,
            "risk_level": self.risk_level,
            "approval_required": self.approval_required,
            "created_at": self.created_at,
            "processed_at": self.processed_at,
            "status": self.status,
            "steps": [step.to_dict() for step in self.steps],
            "result": self.result,
            "verification_status": self.verification_status,
        }


@dataclass
class IntentStep:
    """A single step within a multi-step intent."""
    step_id: str
    action_type: ActionType
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)  # Parent step IDs this depends on
    expected_state: Optional[str] = None
    risk: str = "LOW"
    approval_required: bool = False


class IntentParser:
    """Parse natural language into structured intents."""
    
    @staticmethod
    def parse(user_input: str, session_id: str, source: str = "text") -> Intent:
        """Parse user input into a structured Intent object."""
        
        # Determine action type from keywords
        intent = _classify_intent(user_input, source)
        
        # Extract parameters
        parameters = _extract_parameters(user_input, intent.action_type)
        
        # Assess risk level
        risk_level = _assess_risk(intent.action_type, parameters)
        approval_required = risk_level in ["HIGH", "CRITICAL"]
        
        intent_id = new_id("INTENT")
        
        return Intent(
            intent_id=intent_id,
            session_id=session_id,
            source=source,
            action_type=intent.action_type,
            parameters=parameters,
            risk_level=risk_level,
            approval_required=approval_required,
        )


def _classify_intent(text: str, source: str) -> IntentStep:
    """Classify input into an action type."""
    text_lower = text.lower().strip()
    
    # Application control
    if any(word in text_lower for word in ["open ", "launch ", "start "]):
        app = _extract_application_name(text)
        return IntentStep(new_id("STEP"), ActionType.OPEN_APPLICATION, f"Open {app}", {"application": app})
    
    if "quit " in text_lower or "close " in text_lower or "exit " in text_lower:
        app = _extract_application_name(text)
        return IntentStep(new_id("STEP"), ActionType.QUIT_APPLICATION, f"Quit {app}", {"application": app})
    
    if "find " in text_lower or "locate " in text_lower or "search " in text_lower:
        query = _extract_query(text)
        return IntentStep(new_id("STEP"), ActionType.SEARCH_FILES, "Search for file", {"query": query})
    
    # Web/browser
    if "web" in text_lower or "internet" in text_lower or "url" in text_lower or "visit " in text_lower:
        url = _extract_url_or_search(text)
        return IntentStep(new_id("STEP"), ActionType.NAVIGATE_URL, "Navigate to URL", {"url": url})
    
    # Terminal commands
    if any(cmd in text_lower for cmd in ["run ", "execute ", "terminal "]) or ("python" in text_lower or "bash" in text_lower or "./" in text_lower):
        command = _extract_command(text)
        return IntentStep(new_id("STEP"), ActionType.RUN_COMMAND, "Execute command", {"command": command})
    
    # File operations
    if "create folder " in text_lower or "make directory " in text_lower:
        dir_name = _extract_folder_name(text)
        return IntentStep(new_id("STEP"), ActionType.CREATE_DIRECTORY, "Create directory", {"name": dir_name})
    
    if "move " in text_lower and ("to " in text_lower or "into " in text_lower):
        src = _extract_source_file(text)
        dest = _extract_destination(text)
        return IntentStep(new_id("STEP"), ActionType.MOVE_FILE, "Move file", {"source": src, "destination": dest})
    
    # System
    if "lock mac" in text_lower or "lock computer" in text_lower or "secure " in text_lower:
        return IntentStep(new_id("STEP"), ActionType.LOCK_SCREEN, "Lock screen", {})
    
    if "system status" in text_lower or "how is my mac" in text_lower or "what's running" in text_lower:
        return IntentStep(new_id("STEP"), ActionType.INSPECT_SYSTEM, "Inspect system", {})
    
    if "clipboard" in text_lower:
        if "read" in text_lower:
            return IntentStep(new_id("STEP"), ActionType.READ_CLIPBOARD, "Read clipboard", {})
        else:
            return IntentStep(new_id("STEP"), ActionType.WRITE_CLIPBOARD, "Write clipboard", {"content": ""})
    
    # Default: treat as text query
    return IntentStep(new_id("STEP"), ActionType.SEARCH_WEB, "General query", {"query": text})


def _extract_parameters(text: str, action_type: ActionType) -> Dict[str, Any]:
    """Extract parameters from user input based on action type."""
    params = {}
    
    if action_type == ActionType.OPEN_APPLICATION:
        app = _extract_application_name(text)
        params["application"] = app
    
    elif action_type == ActionType.QUIT_APPLICATION:
        app = _extract_application_name(text)
        params["application"] = app
    
    elif action_type in [ActionType.SEARCH_FILES, ActionType.SEARCH_WEB]:
        params["query"] = _extract_query(text)
    
    elif action_type == ActionType.NAVIGATE_URL:
        params["url"] = _extract_url_or_search(text)
    
    elif action_type == ActionType.RUN_COMMAND:
        params["command"] = _extract_command(text)
    
    elif action_type == ActionType.CREATE_DIRECTORY:
        params["name"] = _extract_folder_name(text)
    
    elif action_type == ActionType.MOVE_FILE:
        params["source"] = _extract_source_file(text)
        params["destination"] = _extract_destination(text)
    
    return params


def _extract_application_name(text: str) -> str:
    """Extract application name from user input."""
    # Common patterns
    patterns = [
        r"(?:open|launch|start|quit)\s+([A-Za-z]+)",
        r"open\s+(Safari|Chrome|Firefox|Vim|VS Code|Visual Studio Code|Terminal|iTerm2|Finder)",
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).title()
    
    return "Application"


def _extract_query(text: str) -> str:
    """Extract search query from text."""
    if "find " in text.lower():
        return text.split("find ")[1].split()[0:]
    elif "search " in text.lower():
        return text.split("search ")[1].split()[0:]
    return text


def _extract_url_or_search(text: str) -> str:
    """Extract URL or search terms from text."""
    # Try to match URL pattern
    url_pattern = r'https?://\S+|www\.\S+'
    match = re.search(url_pattern, text)
    if match:
        return match.group()
    
    # Otherwise extract search terms
    if "search " in text.lower():
        return text.split("search ")[1]
    elif "google " in text.lower():
        return text.split("google ")[1]
    
    return text


def _extract_command(text: str) -> str:
    """Extract shell command from text."""
    # Find command patterns
    if "run " in text.lower():
        parts = text.split("run ", 1)
        if len(parts) > 1:
            return parts[1].strip()
    
    # Look for typical command patterns
    cmd_patterns = [
        r'(?:run|execute)\s+(python.*?)(?:\.py|$)',
        r'./(\w+)',
        r'python\s+\S+\.py',
        r'bash\s+\S+',
    ]
    
    for pattern in cmd_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    
    return text


def _extract_folder_name(text: str) -> str:
    """Extract folder name from create command."""
    if "create folder " in text.lower():
        return text.split("create folder ", 1)[1].strip()
    elif "make directory " in text.lower():
        return text.split("make directory ", 1)[1].strip()
    return "New Folder"


def _extract_source_file(text: str) -> str:
    """Extract source file path from move command."""
    if "move " in text.lower():
        parts = text.split("move ", 1)
        if len(parts) > 1:
            rest = parts[1].strip()
            # Everything before "to " or "into "
            if " to " in rest.lower():
                return rest.split(" to ")[0]
            elif " into " in rest.lower():
                return rest.split(" into ")[0]
            return rest
    return ""


def _extract_destination(text: str) -> str:
    """Extract destination from move command."""
    if " move " in text.lower():
        parts = text.split(" move ", 1)
        if len(parts) > 1:
            rest = parts[1].strip()
            if " to " in rest.lower():
                return rest.split(" to ")[1]
            elif " into " in rest.lower():
                return rest.split(" into ")[1]
    return ""


def _assess_risk(action_type: ActionType, parameters: Dict[str, Any]) -> str:
    """Assess risk level for an action."""
    high_risk_actions = [
        ActionType.DELETE_FILE,
        ActionType.QUIT_APPLICATION,
        ActionType.MOVE_FILE,
        ActionType.COPY_FILE,
        ActionType.RENAME_FILE,
        ActionType.RUN_COMMAND,
    ]
    
    critical_risk_actions = [
        ActionType.LOCK_SCREEN,
    ]
    
    if action_type in critical_risk_actions:
        return "CRITICAL"
    
    if action_type in high_risk_actions:
        return "HIGH"
    
    if action_type in [ActionType.OPEN_APPLICATION, ActionType.NAVIGATE_URL]:
        return "MEDIUM"
    
    # Default to LOW
    return "LOW"


def save_intent_to_db(intent: Intent) -> None:
    """Save intent to database."""
    db = get_db()
    
    db.execute("""
        INSERT OR REPLACE INTO intents (
            intent_id, session_id, user_id, source, action_type, 
            parameters, risk_level, approval_required, 
            created_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        intent.intent_id,
        intent.session_id,
        intent.user_id,
        intent.source,
        intent.action_type.value,
        json.dumps(intent.parameters),
        intent.risk_level,
        intent.approval_required,
        intent.created_at,
        intent.status,
    ))


# Database schema initialization hook
SCHEMA_INTENT = """
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT DEFAULT 'user',
    source TEXT DEFAULT 'unknown',
    action_type TEXT NOT NULL,
    parameters TEXT DEFAULT '{}',
    risk_level TEXT DEFAULT 'LOW',
    approval_required INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    processed_at TEXT,
    result TEXT,
    verification_status TEXT
);

CREATE TABLE IF NOT EXISTS intent_steps (
    step_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES intents(intent_id) ON DELETE CASCADE,
    action_type TEXT NOT NULL,
    description TEXT,
    parameters TEXT DEFAULT '{}',
    dependencies TEXT DEFAULT '[]',
    expected_state TEXT,
    risk TEXT DEFAULT 'LOW',
    approval_required INTEGER DEFAULT 0,
    seq INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_intents_session ON intents(session_id);
CREATE INDEX IF NOT EXISTS idx_intents_created ON intents(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_intent_steps_intent ON intent_steps(intent_id);
"""


# Note: This will be called during database initialization via get_db() hook
# See jarvis/db/database.py for integration
