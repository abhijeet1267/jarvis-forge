"""Resolve a vague reference ("it", "that") against verified task state.

Vantage's rule planner refuses vague pronouns on purpose: ``planner.py`` carries
"Never infer a broad target from a vague pronoun" and returns a note instead of
an action when the object of "delete"/"move" is ``it``/``that``/``this``. That
refusal is right for a stateless command line and it stays in force.

This module adds the one thing that refusal cannot do: after a mission has
**verified** that an application or file exists, a following sentence may refer
to it by pronoun. The rule is deliberately conservative:

* a session has at most one *most recent verified* entity that a pronoun can
  bind to; if the reference would be ambiguous, this returns ``None`` and the
  caller falls through to the planner's existing refusal — never to a guess;
* an entity that was only *planned* or *attempted* is never a binding target,
  because "open Calculator" that failed must not make "close it" close
  something else;
* resolution happens before planning and only rewrites the *text* into a
  concrete name. The rewritten sentence then travels the ordinary path —
  planner -> ToolSpec validation -> PermissionEngine -> MissionRunner — so this
  module cannot authorize anything, only disambiguate what was said.

Resolving a reference is **not** permission to act on the resolved entity. The
permission engine sees the resolved name exactly as if the user had typed it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

#: Words that can stand in for a previously established entity. Kept identical
#: to the set ``planner.py`` refuses, so anything this module resolves would
#: otherwise have been refused; the two lists must not drift apart.
VAGUE_REFERENCES = frozenset(
    {"it", "this", "that", "these", "those", "them", "the app", "the file",
     "the window", "the document", "the previous result", "the last mission"}
)

#: Minimum confidence a resolver needs before it will rewrite a sentence. The
#: memory is either unambiguous or it is not; there is no middle value today,
#: but the constant documents the intent and gives a single place to tighten.
_REQUIRED_CONFIDENCE = 1.0


def _is_vague(subject: str) -> bool:
    """True when *subject* names nothing specific."""
    return subject.strip().lower() in VAGUE_REFERENCES


def resolve_reference(
    text: str,
    memory: Any,
    session_id: str,
    *,
    kind: str = "app",
) -> Optional[str]:
    """Rewrite a pronoun reference in *text* to the session's verified entity.

    Returns the rewritten sentence, or ``None`` when the sentence contains no
    vague reference, when the session has no verified entity of *kind*, or when
    the memory cannot supply exactly one candidate. ``None`` is the signal to
    proceed with *text* unchanged — which for a vague subject means the planner
    refuses it, as it always has.

    ``memory`` is duck-typed: it must expose ``last_verified_of_kind(session_id,
    kind)`` returning an object with a ``.name`` attribute, or ``None``.
    """
    if not text or not text.strip() or memory is None or not session_id:
        return None

    lowered = text.strip().lower()
    # Find a reference that stands as the object of the sentence. Matching only a
    # whole word keeps "it" from firing inside "item" or "edit".
    ref = None
    for candidate in ("the previous result", "the last mission", "the document",
                      "the window", "the file", "the app", "them", "those",
                      "these", "that", "this", "it"):
        if re.search(rf"(?<![\w]){re.escape(candidate)}(?![\w])", lowered):
            ref = candidate
            break
    if ref is None:
        return None

    entity = memory.last_verified_of_kind(session_id, kind)
    if entity is None:
        return None

    name = str(getattr(entity, "name", "") or "").strip()
    if not name:
        return None

    # Replace only the first occurrence, and only as a whole word, so a sentence
    # that mentions the pronoun twice is not rewritten into nonsense.
    resolved = re.sub(
        rf"(?<![\w]){re.escape(ref)}(?![\w])",
        name,
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    if resolved.strip() == text.strip():
        return None
    return resolved


def describe_resolution(
    text: str,
    memory: Any,
    session_id: str,
    *,
    kind: str = "app",
) -> Dict[str, Optional[str]]:
    """The resolution decision, for the UI and for honest spoken replies.

    Carries ``original``, ``resolved`` (or ``None``) and ``entity`` (or
    ``None``) so a caller can say *"I read 'it' as Calculator"* rather than
    silently acting on a guess. Silence about a substitution is how a person
    ends up with the wrong window closed.
    """
    entity = memory.last_verified_of_kind(session_id, kind) if memory and session_id else None
    resolved = resolve_reference(text, memory, session_id, kind=kind)
    return {
        "original": text,
        "resolved": resolved,
        "entity": str(getattr(entity, "name", "") or "") if entity is not None else None,
    }