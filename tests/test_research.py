"""Research Agent tests (v1.0).

The plan's hard requirement is **"never fabricate citations"**, and its second is
to distinguish source facts from interpretation, hypotheses and unresolved
claims. Most of this file exists to prove those two things.
"""

from __future__ import annotations

import urllib.request

import pytest

from jarvis.core.models import PlannedAction, PolicyDecision, RiskLevel
from jarvis.research.brief import (
    build_brief,
    compare_papers,
    extract_datasets,
    extract_limitations,
    extract_methods,
    identify_gaps,
)
from jarvis.research.notes import (
    CLAIM_TYPES,
    HYPOTHESIS,
    INTERPRETATION,
    SOURCE_FACT,
    UNRESOLVED,
    ResearchNoteStore,
    looks_hedged,
)
from jarvis.research.papers import PaperStore
from jarvis.research.sources import ArxivSource, SourceHit, get_source
from jarvis.tools.base import ToolContext
from jarvis.tools.research import (
    add_paper,
    add_research_note,
    cite_paper,
    compare_papers_tool,
    find_research_gaps,
    list_papers,
    research_brief,
    search_papers,
)


@pytest.fixture()
def store() -> PaperStore:
    return PaperStore()


@pytest.fixture()
def notes() -> ResearchNoteStore:
    return ResearchNoteStore()


@pytest.fixture()
def ctx() -> ToolContext:
    return ToolContext()


def _paper(store: PaperStore, **kwargs) -> object:
    defaults = dict(
        title="Online Sketch-based Query Optimization",
        authors=["Izenov", "Datta"],
        year=2021,
        venue="arXiv preprint",
        abstract=(
            "We propose a new skimming method for query optimization. "
            "We evaluate on TPC-H and a synthetic workload. "
            "However, we assume single-node execution and cannot handle distributed plans."
        ),
        source_backend="arxiv",
    )
    defaults.update(kwargs)
    return store.add(**defaults)


# ---------------------------------------------------------------------------
# Paper store
# ---------------------------------------------------------------------------
def test_paper_from_a_real_backend_is_verified(store: PaperStore) -> None:
    paper = store.add("A real paper", source_backend="arxiv")
    assert paper.verified is True


def test_user_supplied_paper_is_not_verified(store: PaperStore) -> None:
    """An unverified paper must be labelled as such, not implied to be checked."""
    paper = store.add("A paper I typed in", source_backend="user")
    assert paper.verified is False


def test_adding_a_paper_needs_a_title(store: PaperStore) -> None:
    with pytest.raises(ValueError):
        store.add("")


def test_duplicate_papers_are_not_stored_twice(store: PaperStore) -> None:
    first = store.add("Same paper", arxiv_id="2102.02440v1", source_backend="arxiv")
    second = store.add("Same paper", arxiv_id="2102.02440v1", source_backend="arxiv")
    assert first.paper_id == second.paper_id


def test_citation_key_is_readable(store: PaperStore) -> None:
    paper = store.add("Keyed paper", authors=["Ada Lovelace"], year=1843)
    assert paper.citation_key == "lovelace1843"


# ---------------------------------------------------------------------------
# The "never fabricate a citation" rule
# ---------------------------------------------------------------------------
def test_citing_an_unknown_paper_returns_none(store: PaperStore) -> None:
    """This is the whole point: a missing reference yields nothing, not a guess."""
    assert store.cite("PAPER-does-not-exist") is None


def test_citing_a_stored_paper_works(store: PaperStore) -> None:
    paper = _paper(store)
    citation = store.cite(paper.paper_id)
    assert citation is not None
    assert paper.title in citation


def test_cite_many_skips_missing_ids(store: PaperStore) -> None:
    paper = _paper(store)
    citations = store.cite_many([paper.paper_id, "PAPER-nope"])
    assert len(citations) == 1


def test_bibtex_citation_is_well_formed(store: PaperStore) -> None:
    paper = _paper(store)
    bibtex = store.cite(paper.paper_id, style="bibtex")
    assert bibtex.startswith("@misc{")
    assert "title = {" in bibtex
    assert bibtex.rstrip().endswith("}")


@pytest.mark.asyncio
async def test_cite_tool_refuses_an_unknown_paper(ctx: ToolContext) -> None:
    """The tool must say so, not produce a plausible-looking citation."""
    result = await cite_paper(ctx, paper_id="PAPER-nope")
    assert not result.ok
    assert "will not invent" in result.summary


@pytest.mark.asyncio
async def test_cite_tool_needs_an_id(ctx: ToolContext) -> None:
    assert not (await cite_paper(ctx)).ok


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def test_only_approved_sources_are_reachable() -> None:
    assert get_source("arxiv") is not None
    assert get_source("local") is not None
    # An unapproved name resolves to nothing rather than falling back.
    assert get_source("made-up-index") is None


def test_arxiv_is_registered_as_requiring_network() -> None:
    assert ArxivSource().requires_network is True
    assert get_source("local").requires_network is False


def test_source_hits_carry_their_backend() -> None:
    hit = SourceHit(title="t", backend="arxiv")
    assert hit.as_dict()["backend"] == "arxiv"


# ---------------------------------------------------------------------------
# Extraction (extract, never invent)
# ---------------------------------------------------------------------------
def test_methods_are_extracted_from_the_abstract(store: PaperStore) -> None:
    paper = _paper(store)
    methods = extract_methods(paper)
    assert methods, "the abstract states a method"
    assert any("propose" in m.lower() for m in methods)


def test_datasets_are_extracted_from_the_abstract(store: PaperStore) -> None:
    paper = _paper(store)
    assert extract_datasets(paper)


def test_limitations_are_extracted_from_the_abstract(store: PaperStore) -> None:
    paper = _paper(store)
    assert extract_limitations(paper)


def test_extraction_returns_empty_when_nothing_is_stated(store: PaperStore) -> None:
    """If the abstract says nothing, the result is empty -- not invented."""
    paper = store.add("Silent paper", abstract="A short abstract with no detail.")
    assert extract_methods(paper) == []
    assert extract_datasets(paper) == []
    assert extract_limitations(paper) == []


def test_compare_builds_a_structured_table(store: PaperStore) -> None:
    a = _paper(store)
    b = store.add("Second paper", year=2019, abstract="We present a framework. We evaluate on IMDB.")
    comparison = compare_papers([a, b])
    assert comparison["count"] == 2
    assert comparison["year_range"] == (2019, 2021)


def test_compare_reports_unverified_sources(store: PaperStore) -> None:
    a = _paper(store)
    b = store.add("Typed-in paper", source_backend="user")
    assert compare_papers([a, b])["all_verified"] is False


# ---------------------------------------------------------------------------
# Notes: claim provenance
# ---------------------------------------------------------------------------
def test_the_four_claim_types_exist() -> None:
    assert {SOURCE_FACT, INTERPRETATION, HYPOTHESIS, UNRESOLVED} == set(CLAIM_TYPES)


def test_a_source_fact_without_a_paper_is_downgraded(notes: ResearchNoteStore) -> None:
    """An unsourced claim must never be presented as a sourced fact."""
    note = notes.add("This is definitely true", claim_type=SOURCE_FACT)
    assert note.claim_type == INTERPRETATION


def test_a_source_fact_with_a_paper_stays_a_fact(notes: ResearchNoteStore) -> None:
    note = notes.add("The method uses sketches", claim_type=SOURCE_FACT, paper_id="PAPER-abc")
    assert note.claim_type == SOURCE_FACT


def test_an_unknown_claim_type_is_rejected(notes: ResearchNoteStore) -> None:
    with pytest.raises(ValueError):
        notes.add("Something", claim_type="definitely_true")


def test_notes_can_be_filtered_by_type(notes: ResearchNoteStore) -> None:
    notes.add("A guess", claim_type=HYPOTHESIS)
    notes.add("A reading", claim_type=INTERPRETATION)
    assert len(notes.list(claim_type=HYPOTHESIS)) >= 1


def test_hedged_language_is_detected() -> None:
    assert looks_hedged("This may improve throughput")
    assert not looks_hedged("Throughput improved by 40 percent")


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------
def test_no_gaps_invented_when_none_recorded(store: PaperStore, notes: ResearchNoteStore) -> None:
    """The agent says 'no gaps recorded' rather than making one up."""
    gaps = identify_gaps([], [])
    assert gaps["gap_count"] == 0
    assert gaps["open_questions"] == []


def test_gaps_come_from_recorded_sources(store: PaperStore, notes: ResearchNoteStore) -> None:
    paper = _paper(store)
    notes.add("Does this scale to distributed plans?", claim_type=UNRESOLVED)
    gaps = identify_gaps([paper], notes.list())
    assert gaps["gap_count"] >= 1
    assert gaps["declared_limitations"], "the paper states a limitation"


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_brief_cites_only_stored_papers(store: PaperStore) -> None:
    paper = _paper(store)
    brief = await build_brief("query optimization", papers=[paper], store=store)
    assert len(brief["citations"]) == 1
    assert brief["missing"] == []


@pytest.mark.asyncio
async def test_brief_reports_a_reference_that_is_not_stored(store: PaperStore) -> None:
    """A paper object not in the store is flagged, not silently cited."""
    ghost = store.add("Temporary")
    store.forget(ghost.paper_id)

    brief = await build_brief("topic", papers=[ghost], store=store)
    assert brief["missing"] == [ghost.paper_id]
    assert "[UNVERIFIED]" in brief["text"]


@pytest.mark.asyncio
async def test_brief_separates_claim_kinds(store: PaperStore, notes: ResearchNoteStore) -> None:
    paper = _paper(store)
    notes.add("Sketching reduced cost", claim_type=SOURCE_FACT, paper_id=paper.paper_id)
    notes.add("This likely scales", claim_type=HYPOTHESIS)

    brief = await build_brief("topic", papers=[paper], notes=notes.list(), store=store)
    text = brief["text"]
    assert "Fact (from source)" in text
    assert "Hypothesis" in text


@pytest.mark.asyncio
async def test_brief_with_nothing_stored_says_so(store: PaperStore) -> None:
    """An empty brief must read as empty -- not as a quiet success."""
    brief = await build_brief("new topic", papers=[], notes=[], store=store)
    assert "No papers are stored" in brief["text"]
    assert "No notes recorded yet" in brief["text"]
    # The gaps section only appears once there is something to analyse.
    assert brief["citations"] == []
    assert brief["paper_count"] == 0


@pytest.mark.asyncio
async def test_brief_states_that_no_gaps_were_invented(store: PaperStore) -> None:
    """A paper with no stated limits and no notes must produce 'no gaps'."""
    bare = store.add("A bare paper", abstract="A short abstract with no detail.")
    brief = await build_brief("topic", papers=[bare], notes=[], store=store)
    assert "No gaps recorded" in brief["text"]
    assert "will not invent" in brief["text"]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_search_papers_rejects_an_unapproved_source(ctx: ToolContext) -> None:
    result = await search_papers(ctx, query="x", source="made-up-index")
    assert not result.ok
    assert "not an approved source" in result.summary


@pytest.mark.asyncio
async def test_search_papers_needs_a_query(ctx: ToolContext) -> None:
    assert not (await search_papers(ctx)).ok


@pytest.mark.asyncio
async def test_add_paper_tool_marks_it_unverified(ctx: ToolContext) -> None:
    result = await add_paper(ctx, title="A paper I know", authors="Ada Lovelace", year=1843)
    assert result.ok
    assert result.output["paper"]["verified"] is False


@pytest.mark.asyncio
async def test_add_research_note_rejects_an_unsourced_fact(ctx: ToolContext) -> None:
    result = await add_research_note(ctx, claim="Definitely true", claim_type=SOURCE_FACT)
    assert not result.ok
    assert "needs a paper" in result.summary


@pytest.mark.asyncio
async def test_add_research_note_rejects_an_unknown_type(ctx: ToolContext) -> None:
    assert not (await add_research_note(ctx, claim="x", claim_type="truth")).ok


@pytest.mark.asyncio
async def test_add_research_note_accepts_a_hypothesis(ctx: ToolContext) -> None:
    result = await add_research_note(ctx, claim="This might scale", claim_type=HYPOTHESIS)
    assert result.ok
    assert result.output["note"]["claim_type"] == HYPOTHESIS


@pytest.mark.asyncio
async def test_compare_tool_needs_two_ids(ctx: ToolContext) -> None:
    assert not (await compare_papers_tool(ctx, paper_ids="PAPER-only-one")).ok


@pytest.mark.asyncio
async def test_gaps_tool_admits_when_there_are_none(ctx: ToolContext) -> None:
    result = await find_research_gaps(ctx, project_id="no-such-project")
    assert result.ok
    assert result.output["gaps"]["gap_count"] == 0


@pytest.mark.asyncio
async def test_list_papers_tool(ctx: ToolContext) -> None:
    assert (await list_papers(ctx)).ok


@pytest.mark.asyncio
async def test_brief_tool_needs_a_topic(ctx: ToolContext) -> None:
    assert not (await research_brief(ctx)).ok


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
def test_forgetting_a_paper_is_destructive_and_confirmed(core) -> None:
    spec = core.tools.get("forget_paper")
    assert spec.risk is RiskLevel.DESTRUCTIVE
    assert spec.requires_confirmation is True

    action = PlannedAction(
        tool="forget_paper", params={"paper_id": "PAPER-1"}, device="laptop_main"
    )
    assert core.policy.evaluate(action).decision is PolicyDecision.CONFIRM


@pytest.mark.parametrize(
    "tool,params",
    [
        ("cite_paper", {"paper_id": "PAPER-1"}),
        ("list_papers", {}),
        ("find_research_gaps", {}),
        ("compare_papers", {"paper_ids": "PAPER-1,PAPER-2"}),
    ],
)
def test_reading_research_is_not_gated(core, tool: str, params: dict) -> None:
    action = PlannedAction(tool=tool, params=params, device="laptop_main")
    assert core.policy.evaluate(action).decision is PolicyDecision.EXECUTE


def test_research_agent_is_registered(core) -> None:
    assert "research" in core.agents
    assert core.agents["research"].owns("search_papers")


# ---------------------------------------------------------------------------
# Live source (skipped when offline)
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_arxiv_returns_real_papers() -> None:
    try:
        hits = ArxivSource().search("adaptive query optimization", limit=2)
    except Exception as exc:
        pytest.skip(f"arXiv unreachable: {exc}")

    assert hits, "arXiv returned nothing"
    for hit in hits:
        assert hit.title
        assert hit.external_id, "a real arXiv result carries an identifier"
        assert hit.backend == "arxiv"
