"""One conversation model, shared by every chat surface (R2.2).

Vantage has two ways for a person to talk to it, and until this module they
did not share anything:

* ``/command`` -- the full pipeline. The rules route the sentence, the planner
  decides what to do, the permission gate decides whether it may happen, and
  the tool layer does it. Its conversation is kept in the ``messages`` table,
  keyed by ``session_id``.
* ``/ai/chat/send`` -- a direct call to the configured provider. Its
  conversation is kept in ``ai_messages``, keyed by ``conversation_id``, and it
  never touches the planner, the gate or a tool.

Two stores, two schemas, two history loaders, two different answers to "what
did we just talk about". A follow-up that resolves in one surface does not
resolve in the other, and the second surface cannot act on anything at all.

This module is the one place a conversation is read and written. It does not
replace either store -- the ``messages`` table is the pipeline's ledger and
carries the intent, and ``ai_messages`` carries the provider metadata the
chat UI shows -- and it does not delete either surface. It is the *access*
layer: one interface, both histories, one definition of what a turn is.

The two histories are joined by ``session_id``, which is how a conversation
started on one surface is continued on the other. When a caller supplies a
``conversation_id`` that names a chat, and a ``session_id``, both are read and
the turns are merged newest-last, de-duplicated by content, so a person who
used the dashboard and then the app sees one conversation rather than two
half-conversations.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("core.conversation")

#: How many prior turns a follow-up may see. Bounded so a long conversation
#: never inflates the prompt toward the context window.
DEFAULT_HISTORY_LIMIT = 12

#: The roles a turn can have. ``error`` is the chat store's own marker for a
#: turn that failed, and it is deliberately not sent to a model: a failed
#: call is not something to say back to the model as if the person had said it.
_ROLES_SENDABLE = ("user", "assistant")


class ConversationService:
    """Reads and writes the conversation turns behind every chat surface."""

    def __init__(self, db: Any, history_limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        self.db = db
        self.history_limit = history_limit

    # -- reading ------------------------------------------------------------

    def history(
        self,
        session_id: str = "",
        conversation_id: str = "",
        current_text: str = "",
        limit: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        """The conversation's prior turns, oldest first.

        ``current_text`` is dropped from the result: it is the turn being sent
        now, and a client that persists before sending would otherwise feed the
        same sentence to the model twice.

        Both identifiers are honoured, and the turns are merged. A surface that
        has only one of them still gets a history; a surface that has both gets
        the whole conversation.
        """
        limit = limit or self.history_limit
        turns: List[Dict[str, str]] = []

        if session_id:
            turns.extend(self._session_turns(session_id, limit))
        if conversation_id:
            turns.extend(self._chat_turns(conversation_id, limit))

        if not turns:
            return []

        # Newest first for the cut, then back to oldest first for the model.
        turns.sort(key=lambda t: t.get("_seq", 0.0), reverse=True)
        kept = turns[:limit]
        kept.reverse()

        # A turn that appears in both stores is the same turn. Keeping the
        # first occurrence (the session ledger is authoritative) stops the
        # model seeing the same sentence twice.
        seen = set()
        out: List[Dict[str, str]] = []
        for turn in kept:
            role = str(turn.get("role") or "")
            content = str(turn.get("content") or "")
            if role not in _ROLES_SENDABLE or not content.strip():
                continue
            key = (role, content.strip())
            if key in seen:
                continue
            seen.add(key)
            out.append({"role": role, "content": content})

        if current_text and out:
            last = out[-1]
            if last["role"] == "user" and last["content"].strip() == current_text.strip():
                out.pop()
        return out

    def _session_turns(self, session_id: str, limit: int) -> List[Dict[str, str]]:
        """The pipeline's own turns, from the ``messages`` table."""
        try:
            # `rowid` is selected explicitly: a sqlite3.Row is read by column
            # name, and the implicit rowid has no name to read.
            rows = self.db.query(
                """SELECT rowid AS seq, role, content FROM messages
                   WHERE session_id = ? AND role IN ('user', 'assistant')
                   ORDER BY rowid DESC LIMIT ?""",
                (session_id, limit),
            )
        except Exception as exc:
            log.info("could not load session history",
                     extra_fields={"error": str(exc)[:120]})
            return []
        return [
            {"role": str(r["role"]), "content": str(r["content"] or ""),
             "_seq": float(r["seq"] or 0.0)}
            for r in rows
        ]

    def _chat_turns(self, conversation_id: str, limit: int) -> List[Dict[str, str]]:
        """The chat surface's turns, from the ``ai_messages`` table.

        The two stores order their turns differently -- the pipeline's by
        ``rowid`` (an integer), the chat store's by an ISO-8601 timestamp (a
        string) -- so each is reduced to the same sortable form before they are
        merged. Mixing the two forms directly is what a float cast on the
        timestamp did: it raised, and the whole history read failed.
        """
        try:
            rows = self.db.query(
                """SELECT role, content, timestamp FROM ai_messages
                   WHERE conversation_id = ? AND role IN ('user', 'assistant')
                   ORDER BY timestamp DESC LIMIT ?""",
                (conversation_id, limit),
            )
        except Exception as exc:
            log.info("could not load chat history",
                     extra_fields={"error": str(exc)[:120]})
            return []
        return [
            {"role": str(r["role"]), "content": str(r["content"] or ""),
             "_seq": _iso_to_epoch(str(r["timestamp"] or ""))}
            for r in rows
        ]

    # -- writing ------------------------------------------------------------

    def append(
        self,
        role: str,
        content: str,
        session_id: str = "",
        conversation_id: str = "",
        intent: str = "",
    ) -> None:
        """Record one turn in whichever store the caller's surface uses.

        A turn written with both identifiers goes to both, so a conversation
        that crosses surfaces is one conversation rather than two fragments.
        A failure to write is logged, never raised: losing a turn is bad, but
        losing the answer to the person who asked is worse.
        """
        if role not in ("user", "assistant", "error"):
            return
        content = str(content or "")
        if not content.strip():
            return

        if session_id:
            try:
                self.db.execute(
                    "INSERT INTO messages (message_id, session_id, role, content, intent) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (_new_message_id(), session_id, role, content, intent),
                )
            except Exception as exc:
                log.info("could not record session turn",
                         extra_fields={"error": str(exc)[:120]})

        if conversation_id:
            try:
                # `ai_messages.conversation_id` is a foreign key onto `ai_chats`,
                # so the chat row has to exist before a turn can be attached to
                # it. A caller that supplies only a conversation id (the chat
                # surface) is asking for a conversation, and the missing row is
                # what makes it one.
                self._ensure_chat(conversation_id)
                self.db.execute(
                    "INSERT INTO ai_messages (id, conversation_id, role, content, "
                    "provider_meta, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                    (_new_message_id(), conversation_id, role, content, "{}",
                     _now_iso()),
                )
            except Exception as exc:
                log.info("could not record chat turn",
                         extra_fields={"error": str(exc)[:120]})

    def _ensure_chat(self, conversation_id: str) -> None:
        """Make sure the chat row a turn will hang off exists."""
        try:
            self.db.execute(
                "INSERT OR IGNORE INTO ai_chats (id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (conversation_id, "Conversation", _now_iso(), _now_iso()),
            )
        except Exception as exc:
            log.info("could not create chat row",
                     extra_fields={"error": str(exc)[:120]})

    def append_exchange(
        self,
        user_text: str,
        assistant_text: str,
        session_id: str = "",
        conversation_id: str = "",
        intent: str = "",
    ) -> None:
        """Record a completed turn: what the person said, and what was said back."""
        self.append("user", user_text, session_id=session_id,
                    conversation_id=conversation_id, intent=intent)
        self.append("assistant", assistant_text, session_id=session_id,
                    conversation_id=conversation_id, intent=intent)


def _new_message_id() -> str:
    from .models import new_id
    return new_id("MSG")


def _iso_to_epoch(value: str) -> float:
    """An ISO-8601 timestamp as a sortable number.

    The two stores record their turns in different shapes, and the merged
    history has to order them against each other. An unparseable timestamp
    sorts first rather than raising -- a turn with a bad timestamp is still a
    turn, and losing the whole history over one of them is the worse outcome.
    """
    if not value:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return 0.0


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


__all__ = ["ConversationService", "DEFAULT_HISTORY_LIMIT"]
