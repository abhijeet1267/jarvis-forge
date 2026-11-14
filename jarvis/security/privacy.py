"""Privacy controls (section 49).

Three jobs:

1. **Redaction** -- stop secrets reaching the audit log, the log files, or a
   model prompt. Section 26 says "never place API keys directly into prompts";
   this is the code that makes that true rather than aspirational.
2. **Privacy mode** -- a single switch that additionally masks personal
   identifiers (paths under the home directory, email addresses).
3. **Data control** -- export everything JARVIS holds, or delete it by scope.
   Section 49 requires "data deletion" and "data export" to actually exist.

Redaction is deliberately conservative: it must never mangle ordinary text, so
it targets credential *shapes* and exact known secrets rather than guessing.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..config import Settings, get_settings
from ..logging_setup import get_logger

log = get_logger("security.privacy")

MASK = "[redacted]"

# Credential shapes. Kept narrow so normal prose is untouched.
#
# The first pattern is handled separately: it preserves the key name so the log
# still says *which* credential was involved, and only masks the value.
# `authorization` is followed by an *optional* scheme word ("Bearer", "Basic")
# and the credential after it. Without this, "Authorization: Bearer SECRET123"
# matched "Bearer" as the value and masked the scheme, leaving the token in the
# clear -- measured. An HTTP header is exactly how this project's relay and
# gateway carry credentials, so this shape matters more than it looks.
_KV_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|authorization|"
    r"bearer|client[_-]?secret|access[_-]?key|private[_-]?key|"
    r"refresh[_-]?token|id[_-]?token|session[_-]?token)\b"
    r"(\s*[:=]\s*|\s+)"
    # A credential word may be followed by a scheme word ("Bearer", "Basic").
    r"(?:(?:bearer|basic|token|digest)\s+)?"
    # The value: quoted (any 6+ chars), or an unquoted run of 8+ token
    # characters that CONTAINS A DIGIT. The digit requirement is what stops
    # ordinary prose being mangled -- "your session token expired" and
    # "token budget remaining" both used to lose their next word. Real
    # credentials carry a digit; English words do not.
    r"(?:"
    r"([\"'])([^\"']{6,})\3"
    r"|"
    r"([A-Za-z0-9._~+/\-][A-Za-z0-9._~+/=\-]{7,})"
    r")"
)
_SECRET_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b(sk|pk|rk|ghp|gho|ghu|ghs|xox[baprs])[-_][A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                      # AWS access key id
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]

# Personal identifiers, only masked when privacy mode is on.
_PRIVATE_PATTERNS: List[re.Pattern] = [
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),            # email
    re.compile(r"\b(?:\+?\d[\d\s().-]{8,}\d)\b"),             # phone-ish
]

_SENSITIVE_KEYS = {
    "api_key", "apikey", "token", "password", "passwd", "secret",
    "authorization", "auth", "credential", "credentials", "private_key",
    "access_key", "client_secret", "session_token", "api_token",
}


def _known_secrets(settings: Optional[Settings] = None) -> List[str]:
    """Secrets we know by value and can therefore mask exactly."""
    settings = settings or get_settings()
    candidates = [
        settings.llm_api_key,
        settings.api_token,
        # v0.7 integrations. Mail and calendar passwords must never reach a log.
        settings.email_password,
        settings.calendar_password,
    ]
    try:
        from .auth import get_token_store

        candidates.append(get_token_store().token())
    except Exception:  # pragma: no cover - token store is optional here
        pass
    return [c for c in candidates if c and len(c) >= 8]


def redact(text: str, settings: Optional[Settings] = None) -> str:
    """Mask credentials in a string.

    Safe to call on arbitrary text: it only rewrites things that look like
    secrets, and exact values of known secrets.
    """
    if not text:
        return text

    settings = settings or get_settings()
    result = str(text)

    # 1. Exact known secrets first -- these are certain, not heuristic.
    for secret in _known_secrets(settings):
        if secret in result:
            result = result.replace(secret, MASK)

    # 2. key=value credentials, preserving the key name for context.
    def _mask(m: re.Match) -> str:
        quote, quoted, bare = m.group(3), m.group(4), m.group(5)
        # An unquoted value only counts as a credential when it carries a
        # digit; otherwise it is an English word and masking it destroys the
        # sentence ("token expired" -> "token [redacted]").
        if bare is not None and not any(ch.isdigit() for ch in bare):
            return m.group(0)
        value = quoted if quoted is not None else bare
        q = quote or ""
        return f"{m.group(1)}{m.group(2)}{q}{MASK}{q}"

    result = _KV_SECRET.sub(_mask, result)

    # 3. Credential shapes that stand alone.
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(MASK, result)

    # 4. Personal identifiers, when the user has asked for privacy mode.
    if settings.privacy_mode:
        for pattern in _PRIVATE_PATTERNS:
            result = pattern.sub(MASK, result)
        home = str(Path.home())
        if home and home in result:
            result = result.replace(home, "~")

    return result


def redact_value(value: Any, settings: Optional[Settings] = None) -> Any:
    """Recursively redact a JSON-like structure."""
    if isinstance(value, str):
        return redact(value, settings)
    if isinstance(value, dict):
        out: Dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
                out[key] = MASK
            else:
                out[key] = redact_value(item, settings)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_value(item, settings) for item in value]
    return value


def contains_secret(text: str, settings: Optional[Settings] = None) -> bool:
    """True when the text appears to carry a credential."""
    if not text:
        return False
    settings = settings or get_settings()
    if any(secret in text for secret in _known_secrets(settings)):
        return True
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


# ---------------------------------------------------------------------------
# Data control (section 49: export, deletion)
# ---------------------------------------------------------------------------
#: Tables that hold user data, and how to describe them.
USER_DATA_TABLES = {
    "memories": "Stored memories",
    "messages": "Conversation messages",
    "conversations": "Conversations",
    "action_logs": "Audit records",
    "failures": "Recorded failures",
    "tasks": "Jobs",
    "task_steps": "Job steps",
    "confirmations": "Confirmations",
    "files": "Indexed files",
    "graph_nodes": "Project graph nodes",
    "graph_edges": "Project graph edges",
    "projects": "Projects",
    "sensor_readings": "Sensor readings",
    "experiments": "Experiments",
}

#: Deletion scope -> the tables it clears.
DELETE_SCOPES: Dict[str, List[str]] = {
    "memories": ["memories"],
    "conversations": ["messages", "conversations"],
    "audit": ["action_logs", "failures"],
    "projects": ["graph_nodes", "graph_edges", "projects"],
    "tasks": ["task_steps", "tasks"],
    "everything": list(USER_DATA_TABLES.keys()),
}


class PrivacyManager:
    """Export and delete the data JARVIS holds."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        from ..db.database import get_db

        self.settings = settings or get_settings()
        self.db = get_db()

    # -- export --------------------------------------------------------------
    def export(self, redact_output: bool = True) -> Dict[str, Any]:
        """Return everything JARVIS stores about the user as plain JSON.

        Redacted by default: an export is a file that gets copied around, and a
        dump containing live credentials would be a new leak.
        """
        payload: Dict[str, Any] = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "version": 1,
            "redacted": redact_output,
            "data": {},
        }

        for table, description in USER_DATA_TABLES.items():
            try:
                rows = self.db.query(f"SELECT * FROM {table}")
            except Exception:
                continue
            records = [dict(row) for row in rows]
            if redact_output:
                records = [redact_value(record, self.settings) for record in records]
            payload["data"][table] = {
                "description": description,
                "count": len(records),
                "records": records,
            }

        payload["totals"] = {
            table: entry["count"] for table, entry in payload["data"].items()
        }
        return payload

    def write_export(self, destination: Optional[Path] = None, redact_output: bool = True) -> Path:
        """Write the export to disk with owner-only permissions."""
        import os

        target = destination or (
            self.settings.home / f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.export(redact_output), handle, indent=2, default=str)
        os.chmod(target, 0o600)
        log.info("data exported", extra_fields={"path": str(target)})
        return target

    # -- deletion ------------------------------------------------------------
    def preview_delete(self, scope: str) -> Dict[str, int]:
        """Report how many records a deletion would remove, without deleting."""
        tables = DELETE_SCOPES.get(scope)
        if tables is None:
            raise ValueError(f"unknown scope {scope!r}; expected one of {sorted(DELETE_SCOPES)}")

        counts: Dict[str, int] = {}
        for table in tables:
            try:
                rows = self.db.query(f"SELECT COUNT(*) AS n FROM {table}")
                counts[table] = rows[0]["n"] if rows else 0
            except Exception:
                counts[table] = 0
        return counts

    def delete(self, scope: str, confirm: bool = False) -> Dict[str, Any]:
        """Delete user data for a scope.

        Requires ``confirm=True``. Deletion is irreversible, so it is never
        performed implicitly.
        """
        tables = DELETE_SCOPES.get(scope)
        if tables is None:
            raise ValueError(f"unknown scope {scope!r}; expected one of {sorted(DELETE_SCOPES)}")
        if not confirm:
            raise ValueError(
                f"refusing to delete {scope!r} without confirm=True "
                "(this is irreversible)"
            )

        removed: Dict[str, int] = {}
        for table in tables:
            try:
                rows = self.db.query(f"SELECT COUNT(*) AS n FROM {table}")
                removed[table] = rows[0]["n"] if rows else 0
                self.db.execute(f"DELETE FROM {table}")
            except Exception as exc:
                log.info("delete failed", extra_fields={"table": table, "error": str(exc)})
                removed[table] = 0

        log.info("user data deleted", extra_fields={"scope": scope, "removed": removed})

        # Audit the deletion itself -- but only if we did not just clear the log.
        if "action_logs" not in tables:
            try:
                from .audit import AuditLog

                AuditLog().record(
                    intent=f"privacy.delete:{scope}",
                    tool="privacy_delete",
                    result="success",
                    risk="high",
                    detail={"scope": scope, "removed": removed},
                )
            except Exception:  # pragma: no cover
                pass

        return {"scope": scope, "removed": removed, "total_removed": sum(removed.values())}

    def purge_expired_memories(self, older_than_days: int = 90) -> int:
        """Drop session-scoped memories older than a cutoff."""
        cutoff = (
            datetime.now(timezone.utc).timestamp() - older_than_days * 86400
        )
        rows = self.db.query(
            "SELECT id, created_at, scope FROM memories WHERE scope IN ('temporary', 'session')"
        )
        removed = 0
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if created.timestamp() < cutoff:
                self.db.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
                removed += 1
        return removed


def describe_privacy(settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Privacy posture, surfaced by /health."""
    settings = settings or get_settings()
    return {
        "privacy_mode": settings.privacy_mode,
        "redaction_enabled": True,
        "delete_scopes": sorted(DELETE_SCOPES),
        "tracked_tables": sorted(USER_DATA_TABLES),
        "export_redacted_by_default": True,
    }
