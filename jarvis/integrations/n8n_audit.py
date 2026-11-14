"""The n8n audit trail: what Vantage asked n8n to run, and what came back.

The brief asks for an audit trail containing the initiating session, the
approved workflow, a correlation id, the time, the outcome, and a safe error
summary — without tokens, secrets, or unnecessary personal data.

This is deliberately *narrow*:

* it stores the workflow **key**, never the webhook path or the base URL;
* it stores the outcome word and a bounded, already-redacted message;
* it does **not** store the workflow's input or its output payload. A daily
  agenda's output is a person's schedule; the reason they asked for it is not
  the audit trail's business, and a local file is easy to forget about.

Records live in memory, bounded to the most recent ``MAX_ENTRIES``. A durable
log is the execution ledger's job; this is the small, bounded answer the
Connections screen and ``GET /n8n/executions`` show.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

MAX_ENTRIES = 100
MAX_MESSAGE = 300

_lock = threading.Lock()
_entries: List[Dict[str, Any]] = []

def record(result: Any, *, session: str = "", initiated_by: str = "backend") -> Dict[str, Any]:
    """Append one execution outcome. Returns the row that was stored.

    ``result`` is an ``ExecutionResult``; anything else is refused rather than
    guessed at, because a malformed row in an audit trail is worse than none.
    """
    status = getattr(getattr(result, "status", None), "value", None)
    if status is None:
        return {}

    message = str(getattr(result, "message", "") or "")
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE].rstrip() + "…"
    row = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "workflow": str(getattr(result, "workflow_key", "") or ""),
        "correlation_id": str(getattr(result, "correlation_id", "") or ""),
        "status": status,
        "message": message,
        "http_status": int(getattr(result, "http_status", 0) or 0),
        "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
        # The session is the one that asked; a phone session is a different
        # human from the laptop's operator, and the trail should say which.
        "session": (session or "")[:64],
        "initiated_by": initiated_by[:32],
    }
    with _lock:
        _entries.append(row)
        if len(_entries) > MAX_ENTRIES:
            del _entries[: len(_entries) - MAX_ENTRIES]
    return dict(row)

def recent(limit: int = 20) -> List[Dict[str, Any]]:
    """The newest ``limit`` rows, newest first."""
    with _lock:
        rows = list(_entries)
    return [dict(r) for r in reversed(rows[-max(0, int(limit)):])]

def clear() -> None:
    """Drop the in-memory trail. Tests use this; nothing else should."""
    with _lock:
        _entries.clear()