"""Risk classification.

Two jobs:

1. ``classify_command`` -- a pre-parse guard that catches dangerous *phrasing*
   before any planning happens (section 45 safety tests: "delete everything",
   "run this command as root", ...).
2. ``decide`` -- turn a :class:`ToolSpec` plus its parameters into a
   :class:`PolicyDecision` (EXECUTE / CONFIRM / BLOCK).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..core.models import PolicyDecision, RiskLevel, ToolSpec
from ..logging_setup import get_logger

log = get_logger("security.risk")


@dataclass
class GuardHit:
    """A dangerous phrasing pattern that was matched."""

    pattern: str
    reason: str
    decision: PolicyDecision = PolicyDecision.BLOCK


# Phrases that must never be auto-executed, with the reason shown to the user.
DANGEROUS_PATTERNS: List[GuardHit] = [
    GuardHit(r"\bdelete\s+(every|all|everything)\b", "blanket delete request"),
    GuardHit(r"\bwipe\s+(the\s+)?(disk|drive|system)\b", "disk wipe request"),
    GuardHit(r"\brm\s+-rf\s+/(\s|$)", "recursive root deletion"),
    GuardHit(r"\bformat\s+(the\s+)?(disk|drive)\b", "drive format request"),
    GuardHit(r"\bas\s+root\b|\bsudo\b|\badmin(istrator)?\s+privile", "privilege escalation"),
    GuardHit(r"\bunlock\s+(the\s+)?(door|house|home)\b", "physical security action"),
    GuardHit(r"\bdisable\s+(the\s+)?(security|firewall|antivirus)\b", "security disable request"),
    GuardHit(r"\b(install|run|execute)\b.*\.(exe|dmg|pkg|msi|bat|scr)\b", "untrusted executable"),
    GuardHit(r"\b(buy|purchase|pay|transfer)\b.*\b(money|usd|inr|rupees|\$|€|¥)\b", "financial action"),
    GuardHit(r"\b(credit\s+card|bank\s+account|password|passwd|otp|seed\s+phrase)\b", "sensitive secret"),
]

# Phrases that are risky but may proceed with explicit confirmation.
CONFIRM_PATTERNS: List[GuardHit] = [
    GuardHit(r"\bdelete\b|\bremove\b|\btrash\b", "destructive file operation", PolicyDecision.CONFIRM),
    GuardHit(r"\bsend\b.*\b(email|mail|message)\b", "outbound communication", PolicyDecision.CONFIRM),
    GuardHit(r"\buninstall\b|\binstall\b", "software change", PolicyDecision.CONFIRM),
    GuardHit(r"\bshare\b|\bupload\b|\bpublish\b", "data leaving the device", PolicyDecision.CONFIRM),
]


class RiskClassifier:
    """Stateless classifier; safe to share across requests."""

    def __init__(self, paranoid: bool = False) -> None:
        self.paranoid = paranoid

    # -- text-level guard ----------------------------------------------------
    def classify_command(self, text: str) -> Optional[GuardHit]:
        """Return a blocking/confirm hit for the raw command text, if any."""
        lowered = (text or "").lower()
        for hit in DANGEROUS_PATTERNS:
            if re.search(hit.pattern, lowered):
                log.info("command guard hit", extra_fields={"pattern": hit.pattern})
                return hit
        for hit in CONFIRM_PATTERNS:
            if re.search(hit.pattern, lowered):
                return hit
        return None

    # -- tool-level decision -------------------------------------------------
    def decide(self, spec: ToolSpec, params: Dict[str, Any]) -> tuple[PolicyDecision, str]:
        """Map a tool call to a policy decision plus a human-readable reason."""
        risk = spec.risk

        # Safety tools (emergency stop) must never be gated behind a prompt.
        if "safety" in spec.tags:
            return PolicyDecision.EXECUTE, "priority safety action"

        # Paranoid mode promotes anything above pure reads to a confirmation.
        if self.paranoid and risk >= RiskLevel.LOW:
            return PolicyDecision.CONFIRM, "paranoid mode is enabled"

        # Level 0-1 are safe by default.
        if risk <= RiskLevel.LOW and not spec.requires_confirmation:
            return PolicyDecision.EXECUTE, "low-risk action"

        # Level 2+ always confirms unless the tool explicitly opts out.
        if risk >= RiskLevel.EXTERNAL:
            return (
                PolicyDecision.CONFIRM,
                f"level {int(risk)} ({spec.risk_label()}) action requires confirmation",
            )

        if spec.requires_confirmation:
            return PolicyDecision.CONFIRM, "tool requires confirmation"

        return PolicyDecision.EXECUTE, "allowed"

    def risk_label(self, risk: RiskLevel) -> str:
        return {
            RiskLevel.READ: "read",
            RiskLevel.LOW: "low",
            RiskLevel.EXTERNAL: "external",
            RiskLevel.DESTRUCTIVE: "destructive",
            RiskLevel.PHYSICAL: "physical",
        }[risk]
