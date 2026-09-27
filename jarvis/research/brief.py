"""Research briefs (v1.0).

Composes a brief from **stored papers and stored notes only**. Every citation in
the output resolves to a paper that is actually in the store; if a referenced id
is missing, the brief says so rather than substituting a plausible-looking one.

The brief also keeps the four claim kinds visually separate, so a reader can see
which lines are sourced, which are JARVIS's reading, and which are guesses.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..logging_setup import get_logger
from .notes import (
    CLAIM_LABELS,
    HYPOTHESIS,
    INTERPRETATION,
    SOURCE_FACT,
    UNRESOLVED,
    ResearchNoteStore,
)
from .papers import PaperStore

log = get_logger("research.brief")

#: Order in which claim kinds are presented.
SECTION_ORDER = [SOURCE_FACT, INTERPRETATION, HYPOTHESIS, UNRESOLVED]

#: Phrases worth scanning for when extracting structure from an abstract.
METHOD_CUES = [
    "we propose", "we present", "we introduce", "our approach", "method",
    "algorithm", "framework", "technique", "architecture", "we design",
]
DATASET_CUES = [
    "dataset", "corpora", "corpus", "benchmark", "tpch", "tpcds", "imdb",
    "synthetic workload", "real-world workload", "we evaluate on",
]
LIMITATION_CUES = [
    "limitation", "however", "we assume", "does not", "cannot", "future work",
    "open problem", "restricted to", "only consider", "beyond the scope",
]


def _sentences(text: str) -> List[str]:
    """Split into sentences without pretending to understand the text."""
    if not text:
        return []
    parts = []
    for chunk in text.replace("\n", " ").split(". "):
        chunk = chunk.strip()
        if len(chunk) > 20:
            parts.append(chunk.rstrip(".") + ".")
    return parts


def _cues(text: str, cues: List[str]) -> List[str]:
    """Sentences containing any of the cue phrases.

    This is extraction, not invention: if nothing matches, the result is empty
    and the caller reports that honestly.
    """
    lowered = (text or "").lower()
    return [s for s in _sentences(text) if any(cue in s.lower() for cue in cues)]


def extract_methods(paper) -> List[str]:
    """Sentences in the abstract that describe the approach."""
    return _cues(paper.abstract, METHOD_CUES)


def extract_datasets(paper) -> List[str]:
    """Sentences in the abstract that name data or benchmarks."""
    return _cues(paper.abstract, DATASET_CUES)


def extract_limitations(paper) -> List[str]:
    """Sentences in the abstract that describe limits or assumptions."""
    return _cues(paper.abstract, LIMITATION_CUES)


def compare_papers(papers: List) -> Dict[str, Any]:
    """A structured comparison built only from stored metadata."""
    rows = []
    for paper in papers:
        rows.append(
            {
                "paper_id": paper.paper_id,
                "title": paper.title,
                "year": paper.year,
                "venue": paper.venue,
                "verified": paper.verified,
                "methods": extract_methods(paper),
                "datasets": extract_datasets(paper),
                "limitations": extract_limitations(paper),
            }
        )

    years = [p.year for p in papers if p.year]
    return {
        "count": len(papers),
        "papers": rows,
        "year_range": (min(years), max(years)) if years else None,
        "all_verified": all(p.verified for p in papers),
    }


def identify_gaps(papers: List, notes: Optional[List] = None) -> Dict[str, Any]:
    """Open questions, derived from stored notes rather than invented.

    Gaps come from notes marked ``unresolved``, plus any *stated* limitations
    the papers themselves declare. JARVIS does not invent a gap that no source
    or note suggests.
    """
    declared = []
    for paper in papers:
        for limitation in extract_limitations(paper):
            declared.append({"paper_id": paper.paper_id, "limitation": limitation})

    open_questions = [
        note.claim for note in (notes or []) if note.claim_type == UNRESOLVED
    ]

    return {
        "declared_limitations": declared,
        "open_questions": open_questions,
        "gap_count": len(declared) + len(open_questions),
    }


async def build_brief(
    topic: str,
    papers: Optional[List] = None,
    notes: Optional[List] = None,
    store: Optional[PaperStore] = None,
) -> Dict[str, Any]:
    """Compose a research brief.

    Returns ``{"text": ..., "citations": [...], "missing": [...]}` where
    ``missing`` lists any referenced id that was not in the store -- so a broken
    reference is visible instead of silently dropped.
    """
    store = store or PaperStore()
    papers = list(papers or [])
    notes = list(notes or [])

    lines: List[str] = [f"JARVIS RESEARCH BRIEF — {topic}", ""]

    # --- sources ------------------------------------------------------------
    citations: List[str] = []
    missing: List[str] = []
    lines.append("Sources")
    if not papers:
        lines.append("  No papers are stored for this topic yet.")
    for paper in papers:
        citation = store.cite(paper.paper_id)
        if citation is None:
            # The paper object exists but is not in the store -- report it.
            missing.append(paper.paper_id)
            lines.append(f"  [UNVERIFIED] {paper.title}")
            continue
        citations.append(citation)
        flag = "" if paper.verified else " (unverified source)"
        lines.append(f"  [{paper.citation_key}] {citation}{flag}")
    lines.append("")

    # --- findings, grouped by claim kind ------------------------------------
    if notes:
        lines.append("Findings")
        for kind in SECTION_ORDER:
            matching = [n for n in notes if n.claim_type == kind]
            if not matching:
                continue
            lines.append(f"  {CLAIM_LABELS[kind]}")
            for note in matching:
                source = f" [{note.paper_id}]" if note.paper_id else ""
                lines.append(f"    - {note.claim}{source}")
        lines.append("")
    else:
        lines.append("Findings")
        lines.append("  No notes recorded yet.")
        lines.append("")

    # --- structure ----------------------------------------------------------
    if papers:
        comparison = compare_papers(papers)
        lines.append("Methods, data and limitations")
        for row in comparison["papers"]:
            lines.append(f"  {row['title'][:60]}")
            for label, key in (
                ("methods", "methods"),
                ("datasets", "datasets"),
                ("limitations", "limitations"),
            ):
                items = row[key]
                if items:
                    lines.append(f"    {label}: {items[0][:100]}")
                else:
                    lines.append(f"    {label}: not stated in the abstract")
        lines.append("")

        gaps = identify_gaps(papers, notes)
        lines.append("Open questions")
        if gaps["gap_count"] == 0:
            lines.append("  No gaps recorded. I will not invent one.")
        else:
            for item in gaps["declared_limitations"][:3]:
                lines.append(f"  - {item['limitation'][:90]}")
            for question in gaps["open_questions"][:3]:
                lines.append(f"  - {question[:90]}")
        lines.append("")

    if missing:
        lines.append(f"(References not found in the store: {', '.join(missing)})")

    return {
        "text": "\n".join(lines).rstrip(),
        "topic": topic,
        "citations": citations,
        "missing": missing,
        "paper_count": len(papers),
        "note_count": len(notes),
    }
