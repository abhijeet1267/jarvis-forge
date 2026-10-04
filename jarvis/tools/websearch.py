"""The ``search_web`` tool: one sentence's worth of web search, three honest outcomes.

The capability lives in :mod:`jarvis.websearch`; this module only turns its outcomes into
:class:`ActionResult` states, and refuses anything the provider cannot answer honestly (an
empty query, a pasted paragraph) *before* a request is made.
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Tuple

from .. import websearch
from ..core.models import RiskLevel, ToolSpec
from ..logging_setup import get_logger
from .base import ToolContext, fail, ok

log = get_logger("tools.websearch")

TOOL = "search_web"


async def search_web(ctx: ToolContext, query: str = "", limit: int = 5) -> Any:
    """Search the web and report what the source said, or why it could not."""
    text = str(query or "").strip()
    if not text:
        return fail(TOOL, "What should I search for?", device=ctx.device_id, query="")
    if len(text) > websearch.MAX_QUERY_CHARS:
        return fail(TOOL,
                    f"That question is {len(text)} characters, which is too long to search as "
                    f"one query (the limit is {websearch.MAX_QUERY_CHARS}). Ask it as a shorter "
                    "phrase.",
                    device=ctx.device_id, query=text[:80])

    try:
        results: List[dict] = await asyncio.to_thread(websearch.search, text, int(limit or 5))
    except websearch.SearchUnavailable as exc:
        # An outage, stated as one. Not "no results" -- that would turn a network failure
        # into an answer about the world.
        return fail(TOOL,
                    f"I could not reach the search service, so I have no answer for "
                    f"{text!r}. This is the network, not an empty result.",
                    device=ctx.device_id, query=text, detail=str(exc)[:160])
    except websearch.SearchUnreadable as exc:
        return fail(TOOL,
                    f"The search service answered, but I could not read the answer: "
                    f"{str(exc)[:160]}",
                    device=ctx.device_id, query=text)

    if not results:
        return ok(TOOL,
                  f"{websearch.describe()['name']} had no result for {text!r}. "
                  "That is an empty answer from one source, not a claim that nothing exists.",
                  device=ctx.device_id, query=text, source=websearch.describe()["name"],
                  count=0, results=[])

    lines = [f"{r['title']} — {r['url']}" for r in results]
    return ok(TOOL,
              f"Searched for {text!r}: {len(results)} result(s) from "
              f"{websearch.describe()['name']}. " + " ".join(lines),
              device=ctx.device_id, query=text,
              source=websearch.describe()["name"],
              count=len(results), results=results)


SPECS: List[Tuple[ToolSpec, Any]] = [
    (
        ToolSpec(
            name=TOOL,
            description="Search the web for a phrase and return what the source actually "
                        "answered. An outage is reported as an outage, never as no results.",
            agent="research",
            risk=RiskLevel.LOW,
            requires_confirmation=False,
            reversible=True,
            parameters={"query": "string", "limit": "integer"},
            required_parameters=["query"],
            tags=["web", "search", "read"],
        ),
        search_web,
    ),
]
