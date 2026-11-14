"""Approval Integration for Real JARVIS Execution (Phase 79).

Connects workflow execution to existing Vantage confirmation hub:
- HIGH-risk actions require approval before execution
- Self-approval blocked
- Approval expiration enforced
- Phone/Mac surfaces can approve/reject
- Provenance tracking for audit trail
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from ..core.models import ConfirmationState, new_id, utcnow
from ..db.database import get_db


class ApprovalGate:
    """Gate for high-risk action approval requirements."""
    
    @staticmethod
    def requires_approval(tool_name: str, arguments: Dict[str, Any]) -> bool:
        """Determine if a tool requires approval based on risk level."""
        
        HIGH_RISK_TOOLS = {
            'delete_file': True,
            'move_file': True,
            'quit_application': True,
            'execute_shell': True,
            'lock_screen': True,
        }
        
        return HIGH_RISK_TOOLS.get(tool_name, False)
    
    @staticmethod
    def classify_risk(tool_name: str, arguments: Dict[str, Any]) -> str:
        """Classify risk level for an action."""
        
        HIGH_RISK = ['delete_file', 'move_file', 'quit_application']
        MEDIUM_RISK = ['navigate_url', 'create_directory', 'open_application']
        
        if tool_name in HIGH_RISK:
            return "HIGH"
        elif tool_name in MEDIUM_RISK:
            return "MEDIUM"
        else:
            return "LOW"


class ConfirmationIntegration:
    """Integrate workflow approvals with existing confirmation hub."""
    
    @staticmethod
    def create_approval_request(
        request_id: str,
        session_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        description: str,
    ) -> str:
        """Create approval request that connects to confirmation hub."""
        
        db = get_db()
        confirmation_id = new_id("CONF")
        
        # Determine risk and effect
        risk_level = ApprovalGate.classify_risk(tool_name, arguments)
        
        # Create detailed preview
        preview = f"{description}:\n"
        for key, value in arguments.items():
            if key not in ['path', 'application', 'url']:
                continue
            preview += f"  {key}: {value}\n"
        
        db.execute("""
            INSERT INTO confirmations 
            (confirmation_id, request_id, session_id, tool, params, device,
             risk, preview, state, created_at, requested_by_session, requested_surface)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            confirmation_id,
            request_id,
            session_id,
            tool_name,
            '{"tool": "' + tool_name + '", "params": "' + str(arguments) + '"}',
            "workflow",
            ApprovalGate.classify_risk(tool_name, arguments),
            preview,
            ConfirmationState.PENDING.value,
            utcnow().isoformat(),
            session_id,
            "phone"  # Default surface
        ))
        
        return confirmation_id
    
    @staticmethod
    def is_self_approval(requester_session: str, approver_session: str) -> bool:
        """Check if this is a self-approval attempt."""
        return requester_session == approver_session
    
    @staticmethod
    def check_approval_expired(confirmation_id: str) -> bool:
        """Check if approval has expired."""
        db = get_db()
        rows = db.query(
            "SELECT created_at FROM confirmations WHERE confirmation_id = ?",
            (confirmation_id,)
        )
        
        if not rows:
            return True
        
        created_at = rows[0]["created_at"]
        
        try:
            from datetime import datetime, timezone, timedelta
            
            # Parse timestamp
            dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            
            # Check expiry (30 minutes default)
            expiry = dt + timedelta(minutes=30)
            return datetime.now(timezone.utc) > expiry
            
        except Exception:
            return True  # On parse error, treat as expired
    
    @staticmethod
    def resolve_approval(confirmation_id: str, approved: bool, session_id: str) -> Dict[str, Any]:
        """Resolve an approval request."""
        
        db = get_db()
        
        # Get confirmation details
        rows = db.query(
            "SELECT * FROM confirmations WHERE confirmation_id = ?",
            (confirmation_id,)
        )
        
        if not rows:
            return {"success": False, "error": "Confirmation not found"}
        
        row = rows[0]
        
        # Validate approval is valid
        if ApprovalIntegration.is_self_approval(row["requested_by_session"], session_id):
            return {
                "success": False,
                "error": "You cannot approve your own requests",
            }
        
        if ApprovalIntegration.check_approval_expired(confirmation_id):
            return {
                "success": False,
                "error": "Approval has expired",
            }
        
        # Update state
        new_state = ConfirmationState.APPROVED.value if approved else ConfirmationState.REJECTED.value
        
        db.execute("""
            UPDATE confirmations SET state = ?, resolved_at = ?
            WHERE confirmation_id = ?
        """, (new_state, utcnow().isoformat(), confirmation_id))
        
        return {
            "success": True,
            "approved": approved,
            "confirmation_id": confirmation_id,
        }


# Convenience alias
ApprovalIntegration.resolve_approval = ConfirmationIntegration.resolve_approval
