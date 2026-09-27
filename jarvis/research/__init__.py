"""Research (v1.0).

A Research Agent that keeps sources, reasoning artifacts and project context
organised — and never fabricates a citation.

The enforcement is structural rather than a matter of good manners: a paper can
only enter the store from an approved source backend or by explicit user
addition, and :meth:`papers.PaperStore.cite` returns ``None`` for anything not
stored. There is no code path that produces a citation for a paper that does
not exist.
"""

from .brief import (
    build_brief,
    compare_papers,
    extract_datasets,
    extract_limitations,
    extract_methods,
    identify_gaps,
)
from .notes import (
    CLAIM_LABELS,
    CLAIM_TYPES,
    HYPOTHESIS,
    INTERPRETATION,
    SOURCE_FACT,
    UNRESOLVED,
    ResearchNote,
    ResearchNoteStore,
    looks_hedged,
)
from .papers import Paper, PaperStore
from .sources import (
    ArxivSource,
    LocalCorpusSource,
    SourceHit,
    approved_sources,
    describe_sources,
    get_source,
)

__all__ = [
    "Paper",
    "PaperStore",
    "SourceHit",
    "ArxivSource",
    "LocalCorpusSource",
    "approved_sources",
    "get_source",
    "describe_sources",
    "ResearchNote",
    "ResearchNoteStore",
    "CLAIM_TYPES",
    "CLAIM_LABELS",
    "SOURCE_FACT",
    "INTERPRETATION",
    "HYPOTHESIS",
    "UNRESOLVED",
    "looks_hedged",
    "build_brief",
    "compare_papers",
    "extract_methods",
    "extract_datasets",
    "extract_limitations",
    "identify_gaps",
]
