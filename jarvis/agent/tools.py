"""Mac Control Tool Registry (Phase 75).

Centralized tool definitions with:
- Input/output schemas
- Risk levels
- Required permissions
- Approval requirements
- Verification methods
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class RiskLevel(Enum):
    """Action risk classification."""
    LOW = "LOW"          # Non-destructive, read-only
    MEDIUM = "MEDIUM"    # State changes, reversible
    HIGH = "HIGH"        # Destructive or sensitive
    CRITICAL = "CRITICAL"   # Irreversible or security-sensitive


@dataclass
class ToolDefinition:
    """Definition for a registered tool."""
    tool_name: str
    description: str
    
    # Schemas
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    
    # Safety
    risk_level: RiskLevel
    required_permissions: List[str] = field(default_factory=list)
    approval_required: bool = False
    approval_policy: str = "explicit"  # explicit, implicit, never
    
    # Execution
    verification_method: str = "observe"  # How to verify action succeeded
    timeout_seconds: int = 30
    supports_dry_run: bool = False
    
    # Implementation hook
    handler: Optional[Callable] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "tool_name": self.tool_name,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "risk_level": self.risk_level.value,
            "required_permissions": self.required_permissions,
            "approval_required": self.approval_required,
            "approval_policy": self.approval_policy,
            "verification_method": self.verification_method,
            "timeout_seconds": self.timeout_seconds,
            "supports_dry_run": self.supports_dry_run,
        }


class ToolRegistry:
    """Centralized tool registration and lookup."""
    
    _instance: Optional["ToolRegistry"] = None
    
    @classmethod
    def get_instance(cls) -> "ToolRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        self._initialize_builtins()
    
    def _initialize_builtins(self) -> None:
        """Initialize built-in tools."""
        
        # Application control tools
        self.register_tool(ToolDefinition(
            tool_name="open_application",
            description="Open a macOS application by name or bundle ID",
            input_schema={
                "type": "object",
                "properties": {
                    "application": {"type": "string", "description": "Application name or bundle identifier"}
                },
                "required": ["application"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "application": {"type": "string"},
                    "pid": {"type": "integer"},
                    "message": {"type": "string"}
                }
            },
            risk_level=RiskLevel.LOW,
            approval_required=False,
            verification_method="check_process_exists",
            required_permissions=["accessibility"],
        ))
        
        self.register_tool(ToolDefinition(
            tool_name="quit_application",
            description="Close/quit a running macOS application",
            input_schema={
                "type": "object",
                "properties": {
                    "application": {"type": "string", "description": "Application name or bundle ID"}
                },
                "required": ["application"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "application": {"type": "string"},
                    "was_running": {"type": "boolean"},
                    "message": {"type": "string"}
                }
            },
            risk_level=RiskLevel.MEDIUM,
            approval_required=True,
            verification_method="check_process_not_exists",
            required_permissions=["accessibility"],
        ))
        
        self.register_tool(ToolDefinition(
            tool_name="activate_application",
            description="Bring an application to focus/foreground",
            input_schema={
                "type": "object",
                "properties": {
                    "application": {"type": "string", "description": "Application name or bundle ID"}
                },
                "required": ["application"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "application": {"type": "string"},
                    "previously_active": {"type": "string"},
                    "message": {"type": "string"}
                }
            },
            risk_level=RiskLevel.LOW,
            approval_required=False,
            verification_method="check_active_application",
            required_permissions=["accessibility"],
        ))
        
        self.register_tool(ToolDefinition(
            tool_name="list_applications",
            description="List all running applications on the system",
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "properties": {
                    "applications": {"type": "array", "items": {"type": "object"}}
                }
            },
            risk_level=RiskLevel.LOW,
            approval_required=False,
            verification_method="observe",
        ))
        
        # Browser tools
        self.register_tool(ToolDefinition(
            tool_name="navigate_url",
            description="Navigate to a URL in the active browser",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL including http:// or https://"}
                },
                "required": ["url"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "url": {"type": "string"},
                    "page_title": {"type": "string"},
                    "message": {"type": "string"}
                }
            },
            risk_level=RiskLevel.MEDIUM,
            approval_required=False,
            verification_method="check_page_loaded",
            required_permissions=["accessibility"],
        ))
        
        self.register_tool(ToolDefinition(
            tool_name="detect_browser",
            description="Detect currently active browser application",
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "properties": {
                    "browser": {"type": "string", "nullable": True},
                    "bundle_id": {"type": "string", "nullable": True}
                }
            },
            risk_level=RiskLevel.LOW,
            approval_required=False,
            verification_method="observe",
        ))
        
        # Shell command tool
        self.register_tool(ToolDefinition(
            tool_name="execute_shell",
            description="Execute a shell command with safety restrictions",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30},
                    "safe_mode": {"type": "boolean", "default": True},
                    "working_dir": {"type": "string", "nullable": True}
                },
                "required": ["command"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "exit_code": {"type": "integer"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "timed_out": {"type": "boolean"}
                }
            },
            risk_level=RiskLevel.HIGH,
            approval_required=True,
            verification_method="check_exit_code",
            required_permissions=[],
        ))
        
        # File operations
        self.register_tool(ToolDefinition(
            tool_name="search_files",
            description="Search for files matching a query",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "File name pattern or content"},
                    "path": {"type": "string", "nullable": True, "description": "Root path to search from"}
                },
                "required": ["query"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "results": {"type": "array", "items": {"type": "string"}},
                    "count": {"type": "integer"}
                }
            },
            risk_level=RiskLevel.LOW,
            approval_required=False,
            verification_method="check_results_count",
        ))
        
        self.register_tool(ToolDefinition(
            tool_name="create_directory",
            description="Create a new directory",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string", "description": "Parent directory"}
                },
                "required": ["name"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "created_path": {"type": "string"}
                }
            },
            risk_level=RiskLevel.MEDIUM,
            approval_required=False,
            verification_method="check_directory_exists",
        ))
        
        self.register_tool(ToolDefinition(
            tool_name="delete_file",
            description="Delete a file permanently",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Full file path"}
                },
                "required": ["path"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "deleted_path": {"type": "string"}
                }
            },
            risk_level=RiskLevel.HIGH,
            approval_required=True,
            verification_method="check_file_not_exists",
        ))
        
        self.register_tool(ToolDefinition(
            tool_name="move_file",
            description="Move/rename a file",
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "destination": {"type": "string"}
                },
                "required": ["source", "destination"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "moved_from": {"type": "string"},
                    "moved_to": {"type": "string"}
                }
            },
            risk_level=RiskLevel.HIGH,
            approval_required=True,
            verification_method="check_file_exists",
        ))
        
        # System inspection tools
        self.register_tool(ToolDefinition(
            tool_name="inspect_system",
            description="Get system status information",
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "properties": {
                    "cpu_usage": {"type": "number"},
                    "memory_usage": {"type": "number"},
                    "disk_usage": {"type": "number"},
                    "uptime": {"type": "integer"},
                    "active_apps": {"type": "integer"}
                }
            },
            risk_level=RiskLevel.LOW,
            approval_required=False,
            verification_method="observe",
        ))
        
        self.register_tool(ToolDefinition(
            tool_name="lock_screen",
            description="Lock the user's Mac screen immediately",
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "locked_at": {"type": "string"}
                }
            },
            risk_level=RiskLevel.CRITICAL,
            approval_required=True,
            verification_method="check_screen_locked",
        ))
        
        # Clipboard
        self.register_tool(ToolDefinition(
            tool_name="read_clipboard",
            description="Read contents of system clipboard",
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "has_text": {"type": "boolean"}
                }
            },
            risk_level=RiskLevel.LOW,
            approval_required=False,
            verification_method="observe",
        ))
        
        self.register_tool(ToolDefinition(
            tool_name="write_clipboard",
            description="Write text to system clipboard",
            input_schema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Text to write"}
                },
                "required": ["content"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"}
                }
            },
            risk_level=RiskLevel.LOW,
            approval_required=False,
            verification_method="verify_clipboard",
        ))
        
        # Verification helpers
        self.register_tool(ToolDefinition(
            tool_name="check_process_exists",
            description="Check if a process is running",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "bundle_id": {"type": "string"}
                }
            },
            output_schema={
                "type": "object",
                "properties": {
                    "exists": {"type": "boolean"},
                    "pid": {"type": "integer", "nullable": True}
                }
            },
            risk_level=RiskLevel.LOW,
            approval_required=False,
            verification_method="observe",
        ))
        
        self.register_tool(ToolDefinition(
            tool_name="check_file_exists",
            description="Check if a file exists",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"]
            },
            output_schema={
                "type": "object",
                "properties": {
                    "exists": {"type": "boolean"},
                    "is_file": {"type": "boolean"},
                    "is_directory": {"type": "boolean"}
                }
            },
            risk_level=RiskLevel.LOW,
            approval_required=False,
            verification_method="observe",
        ))
    
    def register_tool(self, tool: ToolDefinition) -> None:
        """Register a tool definition."""
        self._tools[tool.tool_name] = tool
    
    def get_tool(self, tool_name: str) -> Optional[ToolDefinition]:
        """Get a tool definition by name."""
        return self._tools.get(tool_name)
    
    def list_tools(self) -> List[ToolDefinition]:
        """List all registered tools."""
        return list(self._tools.values())
    
    def check_approval_required(self, tool_name: str) -> bool:
        """Check if a tool requires approval."""
        tool = self.get_tool(tool_name)
        if not tool:
            return False
        return tool.approval_required or tool.risk_level in [RiskLevel.HIGH, RiskLevel.CRITICAL]
    
    def check_permission_required(self, tool_name: str) -> List[str]:
        """Get permissions required for a tool."""
        tool = self.get_tool(tool_name)
        if not tool:
            return []
        return tool.required_permissions


# Singleton instance
DEFAULT_REGISTRY = ToolRegistry.get_instance()
