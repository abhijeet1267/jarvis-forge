"""Memory control tools (v0.9).

The plan requires the user to be able to inspect, search, export, delete,
clear a scope and disable categories. These are the tools for that.

Two rules carried over from the rest of the system:

* **Deletion is confirmed and never implicit.** `forget_memory` and
  `clear_memory_scope` are destructive.
* **A deletion reports what it left behind.** One fact can occupy several rows
  (the user's, plus the orchestrator's automatic echo) and several tables, so
  `forget_memory` says which copies it removed and which are still retrievable
  rather than reporting unqualified success.
* **Sensitive memories are handled more carefully.** ``private`` and ``secret``
  items are excluded from exports unless explicitly asked for, and are listed
  by type only in an overview.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..core.models import MemoryScope, MemoryType, PrivacyLevel, RiskLevel, ToolSpec, utcnow
from ..db.database import get_db
from ..logging_setup import get_logger
from ..memory.store import MemoryStore, like_pattern
from ..memory.vector_store import VectorIndex
from .base import ToolContext, fail, ok

log = get_logger("tools.memory_controls")

#: Privacy levels that are withheld from an export unless asked for by name.
SENSITIVE_LEVELS = {PrivacyLevel.PRIVATE.value, PrivacyLevel.SECRET.value}


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
def category_enabled(name: str) -> bool:
    """Whether a memory category is currently being stored."""
    rows = get_db().query("SELECT enabled FROM memory_categories WHERE name = ?", (name,))
    return bool(rows[0]["enabled"]) if rows else True


def disabled_categories() -> List[str]:
    rows = get_db().query("SELECT name FROM memory_categories WHERE enabled = 0")
    return [row["name"] for row in rows]


def set_category(name: str, enabled: bool) -> None:
    get_db().execute(
        """
        INSERT INTO memory_categories (name, enabled, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled,
                                        updated_at = datetime('now')
        """,
        (name, 1 if enabled else 0),
    )


# ---------------------------------------------------------------------------
# Inspect
# ---------------------------------------------------------------------------
async def memory_overview(ctx: ToolContext) -> Any:
    """A summary of what JARVIS remembers."""
    store = MemoryStore()
    db = get_db()

    by_scope = {
        row["scope"]: row["n"]
        for row in db.query("SELECT scope, COUNT(*) AS n FROM memories GROUP BY scope")
    }
    by_type = {
        row["memory_type"]: row["n"]
        for row in db.query(
            "SELECT COALESCE(memory_type, 'general') AS memory_type, COUNT(*) AS n "
            "FROM memories GROUP BY memory_type"
        )
    }
    expired = [
        item
        for item in store.all(limit=5000)
        if item.is_expired()
    ]

    try:
        vectors = VectorIndex().stats()
    except Exception:
        vectors = {"indexed": 0, "memories": 0, "coverage": 0.0}

    total = sum(by_scope.values())
    return ok(
        "memory_overview",
        (
            f"I am holding {total} memory item(s) across {len(by_scope)} scope(s); "
            f"{vectors.get('indexed', 0)} are indexed for semantic search."
        ),
        device=ctx.device_id,
        total=total,
        by_scope=by_scope,
        by_type=by_type,
        expired=len(expired),
        disabled_categories=disabled_categories(),
        vector_index=vectors,
    )


async def inspect_memory(ctx: ToolContext, memory_id: str = "") -> Any:
    """Look at one memory in full."""
    if not memory_id:
        return fail("inspect_memory", "Which memory should I look at?", device=ctx.device_id)

    item = MemoryStore().get(memory_id)
    if item is None:
        return fail("inspect_memory", f"I have no memory with id {memory_id!r}.", device=ctx.device_id)

    return ok(
        "inspect_memory",
        f"[{item.memory_type.value}] {item.content[:80]}",
        device=ctx.device_id,
        memory={
            "id": item.id,
            "content": item.content,
            "type": item.memory_type.value,
            "scope": item.scope.value,
            "importance": item.importance,
            "confidence": item.confidence,
            "privacy": item.privacy_level.value,
            "source": item.source,
            "created_at": item.created_at.isoformat(),
            "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            "expired": item.is_expired(),
            "tags": item.tags,
            "relationships": item.relationships,
        },
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
async def export_memories(
    ctx: ToolContext,
    scope: str = "",
    include_sensitive: bool = False,
) -> Any:
    """Export memories as JSON.

    Sensitive items are withheld unless explicitly requested, because an export
    is a file that tends to get copied around.
    """
    store = MemoryStore()
    try:
        chosen = MemoryScope(scope) if scope else None
    except ValueError:
        return fail(
            "export_memories",
            f"Unknown scope {scope!r}. Try one of: {', '.join(s.value for s in MemoryScope)}.",
            device=ctx.device_id,
        )

    items = store.all(scope=chosen, limit=5000)
    withheld = 0
    payload = []
    for item in items:
        if item.privacy_level.value in SENSITIVE_LEVELS and not include_sensitive:
            withheld += 1
            continue
        payload.append(
            {
                "id": item.id,
                "content": item.content,
                "type": item.memory_type.value,
                "scope": item.scope.value,
                "importance": item.importance,
                "confidence": item.confidence,
                "privacy": item.privacy_level.value,
                "created_at": item.created_at.isoformat(),
                "expires_at": item.expires_at.isoformat() if item.expires_at else None,
                "tags": item.tags,
                "relationships": item.relationships,
            }
        )

    return ok(
        "export_memories",
        f"Exported {len(payload)} memory item(s)"
        + (f", withheld {withheld} sensitive item(s)." if withheld else "."),
        device=ctx.device_id,
        count=len(payload),
        withheld=withheld,
        memories=payload,
    )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
#: Where the wording of a memory survives its deletion, and which columns of each
#: table hold it. Reported, never removed: the transcript and the audit log are
#: records of what happened rather than memories, and ``tests/test_privacy.py``
#: asserts the audit log must outlive memory deletion on purpose. The project
#: graph is listed here too because ``ProjectGraph`` exposes no delete API, so a
#: node's text is a copy this tool cannot remove -- while ``project_summary`` and
#: ``explain_failures`` keep serving it to the user.
RETAINED_SURFACES: Dict[str, List[str]] = {
    "messages": ["content"],
    "action_logs": ["intent", "target", "detail"],
    "graph_nodes": ["label", "data"],
}

#: Below this length, stored text matching inside a deleted sentence is
#: coincidence, not evidence that the row carries the fact.
_MIN_COPY_CHARS = 12


def rows_carrying(table: str, columns: List[str], text: str) -> int:
    """How many rows of ``table`` still reproduce ``text``, in either direction.

    Forward: the column contains the deleted text. Reverse: the column's text sits
    inside the deleted row -- which catches a transcript message quoted by an echo
    that is itself being deleted, where the message does not contain the echo whole.
    The reverse test is exact (``instr``), hence the length floor.
    """
    if not text.strip():
        return 0
    pattern = like_pattern(text)
    clauses = " OR ".join(
        [f"{column} LIKE ? ESCAPE '\\'" for column in columns]
        + [f"(length({column}) >= ? AND instr(?, {column}) > 0)" for column in columns]
    )
    params = tuple([pattern] * len(columns)) + tuple(
        value for _ in columns for value in (_MIN_COPY_CHARS, text)
    )
    rows = get_db().query(f"SELECT COUNT(*) AS n FROM {table} WHERE {clauses}", params)
    return rows[0]["n"] if rows else 0


def retained_surfaces(text: str) -> Dict[str, int]:
    """Count the places outside ``memories`` that still reproduce ``text``."""
    counts: Dict[str, int] = {}
    for name, columns in RETAINED_SURFACES.items():
        try:
            counts[name] = rows_carrying(name, columns, text)
        except Exception as exc:  # pragma: no cover - a missing table is not fatal
            log.info(
                "retained-surface scan failed",
                extra_fields={"surface": name, "error": str(exc)},
            )
            counts[name] = 0
    return counts


def _describe_ids(items: List[Any], limit: int = 5) -> str:
    """Name rows by id and scope. Never by content -- see :func:`_forget_report`."""
    named = ", ".join(f"{i.id} ({i.scope.value})" for i in items[:limit])
    if len(items) > limit:
        named += f", and {len(items) - limit} more"
    return named


def _forget_report(
    item: Any,
    copies: List[Any],
    retained: Dict[str, int],
    complete: bool,
) -> str:
    """Say what was removed and what was not, without repeating the deleted text.

    The reply is itself stored -- ``JarvisCore._remember_outcome`` keeps a memory
    of every executed command and ``_log_message`` keeps the transcript -- so a
    response that quoted the fact would save it again one row after reporting it
    forgotten. The old ``"Forgotten: {content[:60]}"`` did exactly that.
    """
    removed = f"memory {item.id} ({item.scope.value})"
    if not complete:
        reasons: List[str] = []
        if copies:
            noun = "memory" if len(copies) == 1 else "memories"
            verb = "quotes" if len(copies) == 1 else "quote"
            reasons.append(
                f"{len(copies)} other {noun} {verb} the same wording word for word and "
                f"will still come back in search, the listing and the export: "
                f"{_describe_ids(copies)}"
            )
        if retained.get("graph_nodes"):
            reasons.append(
                f"{retained['graph_nodes']} project-graph node(s) still carry it, "
                "and the graph exposes no delete API for me to reach them"
            )
        report = (
            f"Partly forgotten: I deleted {removed} and its search vector, but "
            + "; ".join(reasons)
            + ". I removed only the one row you named."
        )
    else:
        report = (
            f"Forgotten. I deleted {removed} and its search vector; nothing else I "
            "remember, and no project-graph node, reproduces that text."
        )

    if retained.get("messages") or retained.get("action_logs"):
        report += (
            f" The wording still stands in the history of this exchange -- "
            f"{retained.get('messages', 0)} transcript message(s) and "
            f"{retained.get('action_logs', 0)} audit record(s) -- which are kept as "
            "records by design and were not deleted."
        )
    return report


async def forget_memory(ctx: ToolContext, memory_id: str = "") -> Any:
    """Delete one memory and report exactly what that did *not* remove.

    One remembered fact is often more than one row: the orchestrator stores a
    session-scoped echo of every command, quoting the user's words and the tool's
    reply. So this deletes the row the user named, then checks whether the memory
    table still serves the same text and says so, with ``verified=False`` when it
    does. It never claims more than "this row is gone".
    """
    if not memory_id:
        return fail("forget_memory", "Which memory should I forget?", device=ctx.device_id)

    store = MemoryStore()
    item = store.get(memory_id)
    if item is None:
        return fail("forget_memory", f"I have no memory with id {memory_id!r}.", device=ctx.device_id)

    if not store.forget(memory_id):  # pragma: no cover - raced with another writer
        return fail(
            "forget_memory",
            f"{memory_id} was gone before I could delete it, so nothing changed.",
            device=ctx.device_id,
        )

    copies = store.quoting(item.content, exclude_id=memory_id)
    retained = retained_surfaces(item.content)
    # "Complete" is scoped to what a memory deletion owns: this table and the
    # graph. The transcript and audit copies are counted, disclosed below, and
    # left in place on purpose.
    complete = not copies and not retained["graph_nodes"]
    message = _forget_report(item, copies, retained, complete)

    log.info(
        "memory forgotten",
        extra_fields={
            "id": memory_id,
            "complete": complete,
            "copies": len(copies),
            "retained": retained,
        },
    )
    return ok(
        "forget_memory",
        message,
        device=ctx.device_id,
        verified=complete,
        memory_id=memory_id,
        removed_ids=[memory_id],
        # Ids and scopes only: putting ``content`` here would re-store it.
        remaining_copies=[
            {"id": c.id, "scope": c.scope.value, "source": c.source} for c in copies
        ],
        retained=retained,
        audit_trail_retained=bool(retained["messages"] or retained["action_logs"]),
        complete=complete,
    )


async def clear_memory_scope(ctx: ToolContext, scope: str = "") -> Any:
    """Delete every memory in a scope."""
    if not scope:
        return fail(
            "clear_memory_scope",
            f"Which scope? One of: {', '.join(s.value for s in MemoryScope)}.",
            device=ctx.device_id,
        )
    try:
        chosen = MemoryScope(scope)
    except ValueError:
        return fail(
            "clear_memory_scope",
            f"Unknown scope {scope!r}. Try one of: {', '.join(s.value for s in MemoryScope)}.",
            device=ctx.device_id,
        )

    db = get_db()
    rows = db.query("SELECT COUNT(*) AS n FROM memories WHERE scope = ?", (chosen.value,))
    removed = rows[0]["n"] if rows else 0
    db.execute("DELETE FROM memories WHERE scope = ?", (chosen.value,))

    log.info("memory scope cleared", extra_fields={"scope": chosen.value, "removed": removed})
    return ok(
        "clear_memory_scope",
        f"Cleared {removed} memory item(s) from the {chosen.value} scope.",
        device=ctx.device_id,
        scope=chosen.value,
        removed=removed,
    )


async def sweep_expired_memories(ctx: ToolContext) -> Any:
    """Delete memories whose expiry has passed."""
    store = MemoryStore()
    now = utcnow()
    removed = 0
    for item in store.all(limit=5000):
        if item.is_expired(now):
            store.forget(item.id)
            removed += 1

    return ok(
        "sweep_expired_memories",
        f"Removed {removed} expired memory item(s)." if removed else "Nothing has expired.",
        device=ctx.device_id,
        removed=removed,
    )


async def set_memory_category(ctx: ToolContext, category: str = "", enabled: bool = True) -> Any:
    """Turn a memory category on or off."""
    valid = {t.value for t in MemoryType} | {s.value for s in MemoryScope}
    if category not in valid:
        return fail(
            "set_memory_category",
            f"Unknown category {category!r}. Try one of: {', '.join(sorted(valid))}.",
            device=ctx.device_id,
        )

    set_category(category, enabled)
    return ok(
        "set_memory_category",
        f"{'Enabled' if enabled else 'Disabled'} the {category} category. "
        + ("New memories of that kind will not be stored." if not enabled else ""),
        device=ctx.device_id,
        category=category,
        enabled=enabled,
    )


SPECS = [
    (
        ToolSpec(
            name="memory_overview",
            description="Summarise what JARVIS remembers.",
            agent="memory",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["memory", "read"],
        ),
        memory_overview,
    ),
    (
        ToolSpec(
            name="inspect_memory",
            description="Show one memory in full.",
            agent="memory",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"memory_id": "string"},
            required_parameters=["memory_id"],
            tags=["memory", "read"],
        ),
        inspect_memory,
    ),
    (
        ToolSpec(
            name="export_memories",
            description="Export memories as JSON (sensitive items withheld by default).",
            agent="memory",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"scope": "string", "include_sensitive": "bool"},
            tags=["memory", "read", "privacy"],
        ),
        export_memories,
    ),
    (
        ToolSpec(
            name="forget_memory",
            description="Delete one memory and report any copy that stays retrievable.",
            agent="memory",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            allowed_devices=["laptop_main"],
            parameters={"memory_id": "string"},
            required_parameters=["memory_id"],
            tags=["memory", "destructive"],
        ),
        forget_memory,
    ),
    (
        ToolSpec(
            name="clear_memory_scope",
            description="Delete every memory in a scope.",
            agent="memory",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            allowed_devices=["laptop_main"],
            parameters={"scope": "string"},
            required_parameters=["scope"],
            tags=["memory", "destructive"],
        ),
        clear_memory_scope,
    ),
    (
        ToolSpec(
            name="sweep_expired_memories",
            description="Delete memories whose expiry has passed.",
            agent="memory",
            # Was LOW with no confirmation, fifteen lines below the tool that deletes
            # a whole scope and is DESTRUCTIVE and confirms. Same store.forget() call,
            # same reversible=False; the only difference was that no sentence could
            # reach this one. Phase 30 made it reachable, so the tier had to catch up.
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            allowed_devices=["laptop_main"],
            parameters={},
            tags=["memory", "destructive"],
        ),
        sweep_expired_memories,
    ),
    (
        ToolSpec(
            name="set_memory_category",
            description="Turn a memory category on or off.",
            agent="memory",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"category": "string", "enabled": "bool"},
            required_parameters=["category"],
            tags=["memory", "privacy"],
        ),
        set_memory_category,
    ),
]
