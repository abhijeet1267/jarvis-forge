"""Session-scoped memory of entities a mission actually verified.

WHY THIS EXISTS
---------------
The rule planner refuses to infer a target from a vague pronoun -- see the
comment "Never infer a broad target from a vague pronoun" in
``jarvis/core/planner.py`` and the guard that rejects
``this``/``that``/``it``/``these``/``those``/``them`` as a target. That refusal
stays exactly as it is: guessing a file or a calendar entry from "delete it" is
how the wrong thing gets destroyed.

What is missing is not more guessing but memory. A mission that has just
verified "the file is /tmp/AMR_Report.pdf" or "the front window is Safari" may
remember that fact for the rest of the session, so a later resolver can turn
"it" into that concrete name *when and only when* exactly one recent verified
entity exists. With no recent verified entity -- or with several and no kind in
the utterance -- the resolver must still ask; this class never picks between
candidates on its own.

REMEMBERING IS NOT AUTHORIZATION
--------------------------------
Recording an entity grants nothing. Resolving "it" to a concrete name is not
approval, not permission and not consent: every consequential action still goes
through the permission engine, which judges the resolved concrete target on its
own merits. Memory narrows a reference; it never widens authority, and an
unverified entry is never surfaced by the lookup methods.

OPERATIONAL NOTES
-----------------
- Session-scoped: entries are isolated per session id; one session never sees
  another's entities.
- Expiry uses ``time.monotonic`` (injectable for deterministic tests). An entry
  is expired once its age is strictly greater than ``max_age_seconds``, so it
  stays usable up to and including that age.
- Each session holds at most ``max_entries`` entries; recording past the bound
  evicts the oldest entry of that session. Expired entries are pruned whenever
  something new is recorded.
- Entries carry app names, window titles and file paths, which may be
  sensitive. This module keeps them quiet: it never logs, prints, or puts entry
  content into exception messages.
- Thread-safe: one lock guards all session state.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

__all__ = ["EntityMemory", "Entry"]


@dataclass(frozen=True)
class Entry:
    """One remembered entity.

    ``recorded_at`` is a ``time.monotonic()`` reading taken when the entry was
    recorded. It is only meaningful for age arithmetic -- never display it as a
    wall-clock timestamp.
    """

    kind: str
    name: str
    verified: bool
    evidence: str
    recorded_at: float


class EntityMemory:
    """Per-session, bounded, expiring memory of verified entities.

    ``record`` accepts verified and unverified entries so callers can keep an
    honest history, but ``last_verified`` and ``last_verified_of_kind`` only
    ever return a verified, unexpired entry.

    ``clock`` exists so tests can inject a deterministic monotonic source; it
    defaults to ``time.monotonic``.
    """

    def __init__(
        self,
        max_age_seconds: float = 1800.0,
        max_entries: int = 32,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_age_seconds = float(max_age_seconds)
        self._max_entries = int(max_entries)
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: Dict[str, List[Entry]] = {}

    def record(
        self,
        session_id: str,
        kind: str,
        name: str,
        *,
        verified: bool,
        evidence: str = "",
    ) -> None:
        """Remember one entity for ``session_id``.

        Only pass ``verified=True`` for something a mission actually observed;
        ``evidence`` can say how. Recording is not authorization: the entry
        only helps resolve a later reference, it never permits acting on it.

        ``kind`` is normalised to a stripped, lower-case token (for example
        ``"app"``, ``"file"``, ``"window"``). ``name`` is stored exactly as
        given, because it may be a real file path where whitespace matters.
        """
        sid = _require_non_empty("session_id", session_id)
        kind_key = _require_non_empty("kind", kind).strip().lower()
        target = _require_non_empty("name", name)
        now = self._clock()
        entry = Entry(
            kind=kind_key,
            name=target,
            verified=bool(verified),
            evidence=str(evidence or ""),
            recorded_at=now,
        )
        with self._lock:
            entries = [
                existing
                for existing in self._sessions.get(sid, ())
                if now - existing.recorded_at <= self._max_age_seconds
            ]
            entries.append(entry)
            overflow = len(entries) - self._max_entries
            if overflow > 0:
                del entries[:overflow]
            self._sessions[sid] = entries

    def last_verified(self, session_id: str) -> Optional[Entry]:
        """Most recent verified, unexpired entry for the session, or None.

        The caller decides whether it is safe to use it: when ambiguity is
        possible, ask instead of guessing.
        """
        return self._latest(session_id, kind=None)

    def last_verified_of_kind(self, session_id: str, kind: str) -> Optional[Entry]:
        """Most recent verified, unexpired entry of ``kind``, or None."""
        return self._latest(session_id, kind=kind.strip().lower())

    def clear(self, session_id: str) -> None:
        """Forget everything remembered for the session."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def snapshot(self, session_id: str) -> Dict[str, Any]:
        """JSON-safe summary of the session's live entries, oldest first.

        Expired entries are omitted, matching what the lookup methods can
        return. The dict contains only strings, booleans, integers and floats.
        """
        now = self._clock()
        with self._lock:
            stored = list(self._sessions.get(session_id, ()))
        entries: List[Dict[str, Any]] = []
        for entry in stored:
            age = now - entry.recorded_at
            if age > self._max_age_seconds:
                continue
            entries.append(
                {
                    "kind": entry.kind,
                    "name": entry.name,
                    "verified": entry.verified,
                    "evidence": entry.evidence,
                    "age_seconds": round(max(0.0, age), 3),
                }
            )
        return {
            "session_id": session_id,
            "max_age_seconds": self._max_age_seconds,
            "max_entries": self._max_entries,
            "entries": entries,
        }

    def _latest(self, session_id: str, *, kind: Optional[str]) -> Optional[Entry]:
        now = self._clock()
        with self._lock:
            stored = self._sessions.get(session_id)
            if not stored:
                return None
            for entry in reversed(stored):
                if not entry.verified:
                    continue
                if kind is not None and entry.kind != kind:
                    continue
                if now - entry.recorded_at > self._max_age_seconds:
                    continue
                return entry
        return None


def _require_non_empty(label: str, value: str) -> str:
    """Reject an empty display token without echoing its content."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value