"""Research tools (v1.0).

The plan requires the Research Agent to research topics, collect references,
summarise/compare papers, extract methods, datasets and limitations, identify
gaps, maintain notes and citations, and connect findings to projects.

It also requires it to **never fabricate citations** and to **distinguish
source facts from interpretation, hypotheses and unresolved claims** — both of
which are enforced structurally rather than promised.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from ..research.brief import (
    build_brief,
    compare_papers,
    extract_datasets,
    extract_limitations,
    extract_methods,
    identify_gaps,
)
from ..research.notes import (
    CLAIM_TYPES,
    HYPOTHESIS,
    INTERPRETATION,
    SOURCE_FACT,
    UNRESOLVED,
    ResearchNoteStore,
)
from ..research.papers import PaperStore
from ..research.sources import approved_sources, get_source
from .base import ToolContext, fail, ok

log = get_logger("tools.research")


def _store() -> PaperStore:
    return PaperStore()


def _resolve(paper_ref: str):
    """Find a paper by id, arXiv id, or title fragment. Names are never guessed."""
    store = _store()
    paper = store.get(paper_ref)
    if paper is not None:
        return paper
    matches = store.search(paper_ref, limit=3)
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
async def search_papers(
    ctx: ToolContext,
    query: str = "",
    source: str = "arxiv",
    limit: int = 5,
    project_id: str = "",
) -> Any:
    """Search an approved source and store what comes back."""
    if not query:
        return fail("search_papers", "What should I research?", device=ctx.device_id)

    backend = get_source(source)
    if backend is None:
        return fail(
            "search_papers",
            f"{source!r} is not an approved source. Available: "
            + ", ".join(s.name for s in approved_sources()),
            device=ctx.device_id,
        )

    source_errors: List[str] = []
    # A refused source is a fact about that source, not about the request. Try the
    # others before failing: "research this" dying because one upstream dislikes
    # Python's TLS fingerprint is a worse outcome than an answer with a caveat.
    # Compared by name: get_source() builds a fresh backend each call, so an
    # identity check would re-add the requested source and query it twice.
    for candidate in [backend] + [s for s in approved_sources() if s.name != backend.name]:
        try:
            hits = candidate.search(query, limit=max(1, min(int(limit), 25)))
            backend = candidate
            break
        except Exception as exc:
            source_errors.append(f"{candidate.name}: {exc}")
            hits = []
    else:
        return fail(
            "search_papers",
            "No approved source could answer: " + " | ".join(source_errors),
            device=ctx.device_id,
            source_errors=source_errors,
        )

    if not hits:
        return ok(
            "search_papers",
            f"I found no results for {query!r} on {backend.name}."
            + (f" ({'; '.join(source_errors)})" if source_errors else ""),
            device=ctx.device_id,
            results=[],
            source=backend.name,
            source_errors=source_errors,
        )

    papers = _store().add_many(hits, project_id=project_id or None)
    listing = [f"{p.title[:66]} ({p.year or 'n.d.'})" for p in papers[:5]]
    return ok(
        "search_papers",
        f"I found {len(papers)} reference(s) on {backend.name}:\n" + "\n".join(listing)
        + (f"\n  ({'; '.join(source_errors)})" if source_errors else ""),
        device=ctx.device_id,
        source=backend.name,
        source_errors=source_errors,
        papers=[p.as_dict() for p in papers],
    )


async def list_papers(ctx: ToolContext, project_id: str = "") -> Any:
    """List stored references."""
    papers = _store().all(project_id=project_id or None)
    if not papers:
        return ok(
            "list_papers",
            "No references stored yet.",
            device=ctx.device_id,
            papers=[],
        )
    return ok(
        "list_papers",
        f"{len(papers)} reference(s) stored.",
        device=ctx.device_id,
        papers=[p.as_dict() for p in papers],
    )


async def add_paper(
    ctx: ToolContext,
    title: str = "",
    authors: str = "",
    year: int = 0,
    url: str = "",
    project_id: str = "",
) -> Any:
    """Add a reference the user supplies explicitly.

    These are stored as **unverified** — they did not come from an authoritative
    backend, and the store says so rather than implying they were checked.
    """
    if not title:
        return fail("add_paper", "Which paper should I add? Give me its title.", device=ctx.device_id)

    author_list = [a.strip() for a in authors.split(",") if a.strip()]
    paper = _store().add(
        title,
        authors=author_list,
        year=int(year) if year else None,
        url=url,
        source_backend="user",
        project_id=project_id or None,
    )
    return ok(
        "add_paper",
        f"Stored {paper.title[:60]} (unverified — you supplied it).",
        device=ctx.device_id,
        paper=paper.as_dict(),
    )


async def cite_paper(ctx: ToolContext, paper_id: str = "", style: str = "text") -> Any:
    """Produce a citation for a stored paper.

    If the paper is not in the store, this fails. It does not invent one.
    """
    if not paper_id:
        return fail("cite_paper", "Which paper should I cite?", device=ctx.device_id)

    store = _store()
    if store.get(paper_id) is None:
        return fail(
            "cite_paper",
            (
                f"I have no stored paper with id {paper_id!r}, so I will not invent a "
                "citation for it. Search for it first, or add it."
            ),
            device=ctx.device_id,
        )

    citation = store.cite(paper_id, style=style)
    return ok("cite_paper", citation, device=ctx.device_id, citation=citation, style=style)


# ---------------------------------------------------------------------------
# Extraction and comparison
# ---------------------------------------------------------------------------
async def extract_paper_methods(ctx: ToolContext, paper_id: str = "") -> Any:
    """Extract stated methods from a stored paper's abstract."""
    paper = _resolve(paper_id) if paper_id else None
    if paper is None:
        return fail(
            "extract_paper_methods",
            f"I have no stored paper matching {paper_id!r}.",
            device=ctx.device_id,
        )

    items = extract_methods(paper)
    if not items:
        return ok(
            "extract_paper_methods",
            f"{paper.title[:50]} does not state a method in the stored abstract.",
            device=ctx.device_id,
            paper_id=paper.paper_id,
            methods=[],
        )
    return ok(
        "extract_paper_methods",
        f"Methods stated in {paper.title[:44]}: {items[0][:120]}",
        device=ctx.device_id,
        paper_id=paper.paper_id,
        methods=items,
    )


async def extract_paper_datasets(ctx: ToolContext, paper_id: str = "") -> Any:
    """Extract datasets or benchmarks named in a stored paper."""
    paper = _resolve(paper_id) if paper_id else None
    if paper is None:
        return fail(
            "extract_paper_datasets",
            f"I have no stored paper matching {paper_id!r}.",
            device=ctx.device_id,
        )

    items = extract_datasets(paper)
    return ok(
        "extract_paper_datasets",
        f"{len(items)} dataset/benchmark statement(s) found."
        if items
        else f"{paper.title[:48]} names no dataset in the stored abstract.",
        device=ctx.device_id,
        paper_id=paper.paper_id,
        datasets=items,
    )


async def extract_paper_limitations(ctx: ToolContext, paper_id: str = "") -> Any:
    """Extract limitations or assumptions stated in a stored paper."""
    paper = _resolve(paper_id) if paper_id else None
    if paper is None:
        return fail(
            "extract_paper_limitations",
            f"I have no stored paper matching {paper_id!r}.",
            device=ctx.device_id,
        )

    items = extract_limitations(paper)
    return ok(
        "extract_paper_limitations",
        f"{len(items)} limitation statement(s) found."
        if items
        else f"{paper.title[:48]} states no limitation in the stored abstract.",
        device=ctx.device_id,
        paper_id=paper.paper_id,
        limitations=items,
    )


async def compare_papers_tool(ctx: ToolContext, paper_ids: str = "") -> Any:
    """Compare several stored papers side by side."""
    ids = [p.strip() for p in (paper_ids or "").split(",") if p.strip()]
    if len(ids) < 2:
        return fail(
            "compare_papers",
            "Which papers should I compare? Give me at least two ids.",
            device=ctx.device_id,
        )

    store = _store()
    papers, missing = [], []
    for paper_id in ids:
        paper = store.get(paper_id) or _resolve(paper_id)
        if paper is None:
            missing.append(paper_id)
        else:
            papers.append(paper)

    if not papers:
        return fail("compare_papers", "None of those papers are stored.", device=ctx.device_id)

    comparison = compare_papers(papers)
    comparison["missing"] = missing
    return ok(
        "compare_papers",
        f"Compared {comparison['count']} paper(s)"
        + (f"; {len(missing)} not found." if missing else "."),
        device=ctx.device_id,
        comparison=comparison,
    )


# ---------------------------------------------------------------------------
# Notes and gaps
# ---------------------------------------------------------------------------
async def add_research_note(
    ctx: ToolContext,
    claim: str = "",
    claim_type: str = INTERPRETATION,
    topic: str = "",
    paper_id: str = "",
    evidence: str = "",
    project_id: str = "",
) -> Any:
    """Record a research note with an explicit claim type.

    ``source_fact`` requires a paper: a fact with no source is downgraded to an
    interpretation rather than presented as sourced.
    """
    if not claim:
        return fail("add_research_note", "What should I note?", device=ctx.device_id)

    if claim_type not in CLAIM_TYPES:
        return fail(
            "add_research_note",
            f"Unknown claim type {claim_type!r}. Use one of: {', '.join(CLAIM_TYPES)}.",
            device=ctx.device_id,
        )

    if claim_type == SOURCE_FACT and not paper_id:
        return fail(
            "add_research_note",
            "A source fact needs a paper id. I will not label an unsourced claim as fact.",
            device=ctx.device_id,
        )

    notes = ResearchNoteStore()
    try:
        note = notes.add(
            claim,
            claim_type=claim_type,
            topic=topic,
            paper_id=paper_id or None,
            evidence=evidence,
            project_id=project_id or None,
        )
    except ValueError as exc:
        return fail("add_research_note", str(exc), device=ctx.device_id)

    return ok(
        "add_research_note",
        f"Noted as {claim_type.replace('_', ' ')}: {claim[:70]}",
        device=ctx.device_id,
        note=note.as_dict(),
    )


async def find_research_gaps(ctx: ToolContext, project_id: str = "", topic: str = "") -> Any:
    """Report gaps recorded in notes and limitations stated by stored papers.

    JARVIS does not invent a gap: if nothing is recorded, it says so.
    """
    papers = _store().all(project_id=project_id or None)
    notes = ResearchNoteStore().list(project_id=project_id or None)
    if topic:
        notes = [n for n in notes if topic.lower() in (n.topic or "").lower()]

    gaps = identify_gaps(papers, notes)
    if gaps["gap_count"] == 0:
        return ok(
            "find_research_gaps",
            "No gaps are recorded. I will not invent one.",
            device=ctx.device_id,
            gaps=gaps,
        )

    return ok(
        "find_research_gaps",
        f"{gaps['gap_count']} gap signal(s): "
        f"{len(gaps['declared_limitations'])} stated limitation(s), "
        f"{len(gaps['open_questions'])} open question(s).",
        device=ctx.device_id,
        gaps=gaps,
    )


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------
async def research_brief(
    ctx: ToolContext,
    topic: str = "",
    project_id: str = "",
) -> Any:
    """Compose a research brief from stored papers and notes."""
    if not topic:
        return fail("research_brief", "What topic should the brief cover?", device=ctx.device_id)

    store = _store()
    papers = store.search(topic)
    if project_id:
        papers = [p for p in papers if p.project_id == project_id] or papers

    notes = ResearchNoteStore().list(project_id=project_id or None)
    if topic:
        notes = [n for n in notes if topic.lower() in (n.claim + " " + (n.topic or "")).lower()]

    brief = await build_brief(topic, papers=papers, notes=notes, store=store)
    return ok(
        "research_brief",
        brief["text"],
        device=ctx.device_id,
        citations=brief["citations"],
        missing=brief["missing"],
        paper_count=brief["paper_count"],
        note_count=brief["note_count"],
    )


async def link_paper_to_project(
    ctx: ToolContext,
    paper_id: str = "",
    project_id: str = "",
) -> Any:
    """Connect a stored paper to a project in the knowledge graph."""
    if not paper_id or not project_id:
        return fail(
            "link_paper_to_project",
            "I need both a paper id and a project id.",
            device=ctx.device_id,
        )

    paper = _store().get(paper_id)
    if paper is None:
        return fail(
            "link_paper_to_project",
            f"I have no stored paper {paper_id!r}.",
            device=ctx.device_id,
        )

    _store().link_project(paper_id, project_id)
    node = ctx.graph.add_node(
        project_id=project_id,
        kind="paper",
        label=paper.title,
        data={
            "paper_id": paper.paper_id,
            "year": paper.year,
            "venue": paper.venue,
            "arxiv_id": paper.arxiv_id,
            "verified": paper.verified,
        },
    )
    return ok(
        "link_paper_to_project",
        f"Linked {paper.title[:50]} to {project_id}.",
        device=ctx.device_id,
        paper_id=paper_id,
        project_id=project_id,
        node=node,
    )


async def forget_paper(ctx: ToolContext, paper_id: str = "") -> Any:
    """Delete a stored reference."""
    if not paper_id:
        return fail("forget_paper", "Which paper should I forget?", device=ctx.device_id)

    store = _store()
    paper = store.get(paper_id)
    if paper is None:
        return fail("forget_paper", f"I have no stored paper {paper_id!r}.", device=ctx.device_id)

    store.forget(paper_id)
    return ok(
        "forget_paper",
        f"Forgot {paper.title[:56]}.",
        device=ctx.device_id,
        paper_id=paper_id,
    )


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------
SPECS = [
    (
        ToolSpec(
            name="search_papers",
            description="Search an approved source (arXiv) and store references.",
            agent="research",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"query": "string", "source": "string", "limit": "int", "project_id": "string"},
            required_parameters=["query"],
            tags=["research", "sources"],
        ),
        search_papers,
    ),
    (
        ToolSpec(
            name="list_papers",
            description="List stored references.",
            agent="research",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"project_id": "string"},
            tags=["research", "read"],
            provides_identifiers={"paper_id": {"items": "papers", "id": "paper_id",
                                              "label": "title"}},

        ),
        list_papers,
    ),
    (
        ToolSpec(
            name="add_paper",
            description="Add a reference explicitly (stored as unverified).",
            agent="research",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"title": "string", "authors": "string", "year": "int", "url": "string", "project_id": "string"},
            required_parameters=["title"],
            tags=["research", "write"],
        ),
        add_paper,
    ),
    (
        ToolSpec(
            name="cite_paper",
            description="Cite a stored paper. Fails rather than inventing one.",
            agent="research",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"paper_id": "string", "style": "string"},
            required_parameters=["paper_id"],
            tags=["research", "citations"],
        ),
        cite_paper,
    ),
    (
        ToolSpec(
            name="extract_paper_methods",
            description="Extract stated methods from a stored paper.",
            agent="research",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"paper_id": "string"},
            required_parameters=["paper_id"],
            tags=["research", "read"],
        ),
        extract_paper_methods,
    ),
    (
        ToolSpec(
            name="extract_paper_datasets",
            description="Extract datasets and benchmarks named in a stored paper.",
            agent="research",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"paper_id": "string"},
            required_parameters=["paper_id"],
            tags=["research", "read"],
        ),
        extract_paper_datasets,
    ),
    (
        ToolSpec(
            name="extract_paper_limitations",
            description="Extract limitations stated in a stored paper.",
            agent="research",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"paper_id": "string"},
            required_parameters=["paper_id"],
            tags=["research", "read"],
        ),
        extract_paper_limitations,
    ),
    (
        ToolSpec(
            name="compare_papers",
            description="Compare stored papers side by side.",
            agent="research",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"paper_ids": "string"},
            required_parameters=["paper_ids"],
            tags=["research", "read"],
        ),
        compare_papers_tool,
    ),
    (
        ToolSpec(
            name="add_research_note",
            description="Record a note with an explicit claim type.",
            agent="research",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={
                "claim": "string",
                "claim_type": "string",
                "topic": "string",
                "paper_id": "string",
                "evidence": "string",
                "project_id": "string",
            },
            required_parameters=["claim"],
            tags=["research", "write"],
        ),
        add_research_note,
    ),
    (
        ToolSpec(
            name="find_research_gaps",
            description="Report recorded gaps and stated limitations.",
            agent="research",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"project_id": "string", "topic": "string"},
            tags=["research", "read"],
        ),
        find_research_gaps,
    ),
    (
        ToolSpec(
            name="research_brief",
            description="Compose a research brief from stored papers and notes.",
            agent="research",
            risk=RiskLevel.READ,
            allowed_devices=["laptop_main"],
            parameters={"topic": "string", "project_id": "string"},
            required_parameters=["topic"],
            tags=["research", "read"],
        ),
        research_brief,
    ),
    (
        ToolSpec(
            name="link_paper_to_project",
            description="Connect a stored paper to a project in the graph.",
            agent="research",
            risk=RiskLevel.LOW,
            reversible=True,
            allowed_devices=["laptop_main"],
            parameters={"paper_id": "string", "project_id": "string"},
            required_parameters=["paper_id", "project_id"],
            tags=["research", "graph"],
        ),
        link_paper_to_project,
    ),
    (
        ToolSpec(
            name="forget_paper",
            description="Delete a stored reference.",
            agent="research",
            risk=RiskLevel.DESTRUCTIVE,
            requires_confirmation=True,
            reversible=False,
            allowed_devices=["laptop_main"],
            parameters={"paper_id": "string"},
            required_parameters=["paper_id"],
            tags=["research", "destructive"],
        ),
        forget_paper,
    ),
]
