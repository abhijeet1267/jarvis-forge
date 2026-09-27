"""Research Agent (v1.0).

Keeps sources, reasoning artifacts and project context organised. It can search
approved sources, collect references, compare papers, extract structure, record
typed claims and compose briefs.

What it will not do: produce a citation for a paper that is not in the store, or
present a hypothesis as a sourced fact. Both are enforced in the store and the
note type system, not merely intended.
"""

from __future__ import annotations

from ..registry.tools import ToolRegistry
from .base import Agent


class ResearchAgent(Agent):
    """Technical research with traceable sources."""

    name = "research"

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__(registry)

    def capabilities(self) -> set:
        return {
            "search_papers",
            "list_papers",
            "add_paper",
            "cite_paper",
            "extract_paper_methods",
            "extract_paper_datasets",
            "extract_paper_limitations",
            "compare_papers",
            "add_research_note",
            "find_research_gaps",
            "research_brief",
            "link_paper_to_project",
            "forget_paper",
        }
