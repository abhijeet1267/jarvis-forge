"""Research notes (v1.0).

The plan requires the Research Agent to **distinguish source facts, model
interpretation, hypotheses and unresolved claims**. This module gives every note
a mandatory claim type, so a summary never quietly promotes a guess into a fact.

The four kinds, and what they mean when rendered:

* ``source_fact``   — attributable to a stored paper. Must carry a ``paper_id``.
* ``interpretation`` — JARVIS's reading of a source. Opinion, not evidence.
* ``hypothesis``    — an idea to test. May be wrong.
* ``unresolved``    — an open or contradicted question.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.models import new_id, utcnow
from ..db.database import get_db
from ..logging_setup import get_logger

log = get_logger("research.notes")

#: The four claim kinds the plan requires.
SOURCE_FACT = "source_fact"
INTERPRETATION = "interpretation"
HYPOTHESIS = "hypothesis"
UNRESOLVED = "unresolved"

CLAIM_TYPES = [SOURCE_FACT, INTERPRETATION, HYPOTHESIS, UNRESOLVED]

#: How each kind is labelled when rendered for a human.
CLAIM_LABELS = {
    SOURCE_FACT: "Fact (from source)",
    INTERPRETATION: "Interpretation",
    HYPOTHESIS: "Hypothesis",
    UNRESOLVED: "Unresolved",
}

#: Kinds that assert something about the world and therefore need a source.
REQUIRES_SOURCE = {SOURCE_FACT}

#: Rough words that indicate a claim is hedged rather than established.
HEDGE_PATTERN = r"\b(may|might|could|suggests?|appears?|seems?|likely|possibly|perhaps|we believe|i think)\b"


@dataclass
class ResearchNote:
    """One note in a research thread."""

    note_id: str
    claim: str
    claim_type: str
    topic: str = ""
    project_id: Optional[str] = None
    paper_id: Optional[str] = None
    evidence: str = ""
    added_at: Optional[datetime] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "note_id": self.note_id,
            "claim": self.claim,
            "claim_type": self.claim_type,
            "claim_label": CLAIM_LABELS.get(self.claim_type, self.claim_type),
            "topic": self.topic,
            "project_id": self.project_id,
            "paper_id": self.paper_id,
            "evidence": self.evidence,
            "added_at": self.added_at.isoformat() if self.added_at else None,
        }


class ResearchNoteStore:
    """Persist and query research notes."""

    def __init__(self) -> None:
        self.db = get_db()

    def add(
        self,
        claim: str,
        claim_type: str = INTERPRETATION,
        *,
        topic: str = "",
        project_id: Optional[str] = None,
        paper_id: Optional[str] = None,
        evidence: str = "",
    ) -> ResearchNote:
        """Record a note. The claim type decides how it may be presented."""
        claim = (claim or "").strip()
        if not claim:
            raise ValueError("a note needs a claim")

        if claim_type not in CLAIM_TYPES:
            raise ValueError(
                f"unknown claim type {claim_type!r}; expected one of {CLAIM_TYPES}"
            )

        # A fact without a source is not a fact. Downgrade rather than let a
        # bare assertion be labelled as sourced.
        if claim_type == SOURCE_FACT and not paper_id:
            claim_type = INTERPRETATION
            if not evidence:
                evidence = "No source was attached, so this was recorded as an interpretation."

        note = ResearchNote(
            note_id=new_id("NOTE"),
            claim=claim,
            claim_type=claim_type,
            topic=topic,
            project_id=project_id,
            paper_id=paper_id,
            evidence=evidence,
            added_at=utcnow(),
        )

        self.db.execute(
            """
            INSERT INTO research_notes
                (note_id, project_id, paper_id, topic, claim, claim_type, evidence, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                note.note_id,
                note.project_id,
                note.paper_id,
                note.topic,
                note.claim,
                note.claim_type,
                note.evidence,
                note.added_at.isoformat() if note.added_at else None,
            ),
        )
        return note

    @staticmethod
    def _row_to_note(row: Any) -> ResearchNote:
        added = row["added_at"]
        return ResearchNote(
            note_id=row["note_id"],
            claim=row["claim"],
            claim_type=row["claim_type"],
            topic=row["topic"] or "",
            project_id=row["project_id"],
            paper_id=row["paper_id"],
            evidence=row["evidence"] or "",
            added_at=datetime.fromisoformat(added) if added else None,
        )

    def get(self, note_id: str) -> Optional[ResearchNote]:
        rows = self.db.query("SELECT * FROM research_notes WHERE note_id = ?", (note_id,))
        return self._row_to_note(rows[0]) if rows else None

    def list(
        self,
        project_id: Optional[str] = None,
        topic: Optional[str] = None,
        claim_type: Optional[str] = None,
        paper_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[ResearchNote]:
        clauses, params = [], []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if topic:
            clauses.append("topic = ?")
            params.append(topic)
        if claim_type:
            clauses.append("claim_type = ?")
            params.append(claim_type)
        if paper_id:
            clauses.append("paper_id = ?")
            params.append(paper_id)

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.query(
            f"SELECT * FROM research_notes{where} ORDER BY added_at DESC LIMIT ?",
            tuple(params),
        )
        return [self._row_to_note(r) for r in rows]

    def forget(self, note_id: str) -> bool:
        note = self.get(note_id)
        if note is None:
            return False
        self.db.execute("DELETE FROM research_notes WHERE note_id = ?", (note_id,))
        return True

    def stats(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        notes = self.list(project_id=project_id, limit=5000)
        by_type: Dict[str, int] = {}
        for note in notes:
            by_type[note.claim_type] = by_type.get(note.claim_type, 0) + 1
        return {"notes": len(notes), "by_type": by_type}


def looks_hedged(text: str) -> bool:
    """True when a claim reads as hedged rather than established."""
    import re

    return bool(re.search(HEDGE_PATTERN, (text or "").lower()))
