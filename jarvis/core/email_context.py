"""The message a conversation has just been about.

A follow-up -- "read that one", "summarize the second one", "what did they
say in that message?" -- names no message. The words it uses point at the
mailbox read the conversation just did, so the pipeline keeps a pointer to
that read, per session, and a demonstrative request resolves against it
instead of being answered with a fresh search of nothing or a guess.

What is kept, and why nothing else:

* the **uid** the email tools actually use -- the identifier the read path
  takes, never a subject, an array index or a display label;
* the **subject and sender** for the reply, so the person sees which message
  is being read;
* the **candidates** of the last search, so "the second one" can be answered
  from the list the person just saw rather than re-searching and getting a
  differently ordered answer;
* the **provider name**, so a pointer written against one mailbox is not
  spent against another.

The pointer is **not** a copy of the message: the body is fetched through the
existing authorized read path every time. It is **not** a module global: the
row lives in ``email_selections`` keyed by ``session_id`` -- the same key the
messages, confirmations and audit rows use -- so one conversation's selection
cannot be read by another, and a fresh session starts with nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger

log = get_logger("core.email_context")


@dataclass
class EmailSelection:
    """The message a session's conversation is currently pointing at."""

    uid: str = ""
    subject: str = ""
    sender: str = ""
    provider: str = ""
    #: The rows of the search that produced the selection, in the order the
    #: reply showed them. Empty when nothing was searched or it matched none.
    candidates: List[Dict[str, Any]] = field(default_factory=list)


class EmailContext:
    """Reads and writes the per-session email selection.

    Every method takes the ``session_id`` it applies to; nothing here is
    keyed on a process-wide notion of "the" conversation. A session with no
    row gets the empty selection, which the callers translate into an honest
    "there is no message selected" rather than a guess.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    def get(self, session_id: str) -> Optional[EmailSelection]:
        """The session's selection, or ``None`` when it has made none."""
        if not session_id:
            return None
        try:
            rows = self.db.query(
                "SELECT uid, subject, sender, provider, candidates, updated_at "
                "FROM email_selections WHERE session_id = ?",
                (session_id,),
            )
        except Exception as exc:
            log.info("could not read the email selection",
                     extra_fields={"error": str(exc)[:120]})
            return None
        if not rows:
            return None
        row = rows[0]
        try:
            candidates = json.loads(row["candidates"] or "[]")
        except (TypeError, ValueError):
            candidates = []
        if not isinstance(candidates, list):
            candidates = []
        return EmailSelection(
            uid=str(row["uid"] or ""),
            subject=str(row["subject"] or ""),
            sender=str(row["sender"] or ""),
            provider=str(row["provider"] or ""),
            candidates=[c for c in candidates if isinstance(c, dict)],
        )

    def set(self, session_id: str, selection: EmailSelection) -> None:
        """Record the session's current selection.

        Writing an empty uid with no candidates clears it: a search that
        matched nothing does not manufacture a selection, and an earlier one
        must not survive it.
        """
        if not session_id:
            return
        try:
            self.db.execute(
                """
                INSERT INTO email_selections
                    (session_id, uid, subject, sender, provider, candidates, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(session_id) DO UPDATE SET
                    uid = excluded.uid,
                    subject = excluded.subject,
                    sender = excluded.sender,
                    provider = excluded.provider,
                    candidates = excluded.candidates,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    selection.uid,
                    selection.subject,
                    selection.sender,
                    selection.provider,
                    json.dumps(selection.candidates),
                ),
            )
        except Exception as exc:
            # Losing the pointer is degrading, not fatal: the follow-up then
            # says "there is no message selected" instead of failing the turn.
            log.info("could not store the email selection",
                     extra_fields={"error": str(exc)[:120]})

    def clear(self, session_id: str) -> None:
        self.set(session_id, EmailSelection())


__all__ = ["EmailContext", "EmailSelection"]
