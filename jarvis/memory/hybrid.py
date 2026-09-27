"""Hybrid retrieval (v0.9).

The architecture the plan specifies:

    Query
      |
      +--> Keyword retrieval (BM25)  --+
      |                                |
      +--> Vector retrieval (cosine) --+
                                       |
                              Hybrid ranking (RRF)
                                       |
                              Relevant memories

Fusion uses **Reciprocal Rank Fusion** rather than a weighted sum of scores.
That matters because BM25 scores and cosine similarities are on completely
different scales — adding them directly would let whichever happens to produce
larger numbers dominate. RRF only looks at *rank*, so the two retrievers
contribute comparably without any score calibration.

Keyword retrieval is never removed: if the vector side is unavailable or empty,
this degrades to BM25 alone and says so in the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..core.models import MemoryItem
from ..logging_setup import get_logger
from .store import MemoryStore
from .vector_store import VectorIndex

log = get_logger("memory.hybrid")

#: RRF damping constant. 60 is the value from the original paper; it keeps the
#: first few ranks clearly separated without letting rank 1 dominate outright.
RRF_K = 60


@dataclass
class HybridResult:
    item: MemoryItem
    score: float
    keyword_rank: Optional[int] = None
    vector_rank: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.item.id,
            "content": self.item.content,
            "scope": self.item.scope.value,
            "score": round(self.score, 6),
            "keyword_rank": self.keyword_rank,
            "vector_rank": self.vector_rank,
            "found_by": [
                name
                for name, rank in (("keyword", self.keyword_rank), ("vector", self.vector_rank))
                if rank is not None
            ],
        }


def fuse(
    keyword_ids: Sequence[str],
    vector_ids: Sequence[str],
    k: int = RRF_K,
) -> Dict[str, Dict[str, Any]]:
    """Reciprocal Rank Fusion of two ranked id lists.

    Returns ``{id: {"score": ..., "keyword_rank": ..., "vector_rank": ...}}``.
    """
    fused: Dict[str, Dict[str, Any]] = {}

    for rank, memory_id in enumerate(keyword_ids, start=1):
        entry = fused.setdefault(memory_id, {"score": 0.0, "keyword_rank": None, "vector_rank": None})
        entry["score"] += 1.0 / (k + rank)
        entry["keyword_rank"] = rank

    for rank, memory_id in enumerate(vector_ids, start=1):
        entry = fused.setdefault(memory_id, {"score": 0.0, "keyword_rank": None, "vector_rank": None})
        entry["score"] += 1.0 / (k + rank)
        entry["vector_rank"] = rank

    return fused


def hybrid_search(
    query: str,
    limit: int = 5,
    store: Optional[MemoryStore] = None,
    index: Optional[VectorIndex] = None,
    candidate_pool: int = 25,
) -> Dict[str, Any]:
    """Search memories using keyword + vector retrieval.

    Returns a dict with the ranked ``results`` and a ``retrieval`` block
    describing which retrievers actually contributed -- so a caller can tell
    whether it got hybrid results or a keyword-only fallback.
    """
    store = store or MemoryStore()
    if not query.strip():
        return {"results": [], "retrieval": {"mode": "none", "reason": "empty query"}}

    # --- keyword side (always available) -----------------------------------
    keyword_items = store.search(query, limit=candidate_pool)
    keyword_ids = [item.id for item in keyword_items]
    by_id: Dict[str, MemoryItem] = {item.id: item for item in keyword_items}

    # --- vector side (best effort) -----------------------------------------
    vector_ids: List[str] = []
    vector_error = ""
    try:
        index = index or VectorIndex()
        vector_hits = index.search(query, limit=candidate_pool)
        vector_ids = [hit.memory_id for hit in vector_hits]
    except Exception as exc:
        # Never let the vector layer break search.
        vector_error = str(exc)
        log.info("vector retrieval unavailable", extra_fields={"error": vector_error})

    # --- fuse ---------------------------------------------------------------
    if not vector_ids:
        mode = "keyword_only"
        ranked = [
            HybridResult(item=item, score=1.0 / (RRF_K + rank), keyword_rank=rank)
            for rank, item in enumerate(keyword_items, start=1)
        ]
    else:
        mode = "hybrid"
        fused = fuse(keyword_ids, vector_ids)

        # The vector side can surface memories keyword search never returned.
        missing = [mid for mid in vector_ids if mid not in by_id]
        for memory_id in missing:
            item = store.get(memory_id)
            if item is not None:
                by_id[memory_id] = item

        ranked = [
            HybridResult(
                item=by_id[memory_id],
                score=entry["score"],
                keyword_rank=entry["keyword_rank"],
                vector_rank=entry["vector_rank"],
            )
            for memory_id, entry in fused.items()
            if memory_id in by_id
        ]
        ranked.sort(key=lambda r: r.score, reverse=True)

    results = ranked[:limit]
    return {
        "results": results,
        "retrieval": {
            "mode": mode,
            "keyword_candidates": len(keyword_ids),
            "vector_candidates": len(vector_ids),
            "error": vector_error,
            "rrf_k": RRF_K,
        },
    }


def describe_retrieval(store: Optional[MemoryStore] = None, index: Optional[VectorIndex] = None) -> Dict[str, Any]:
    """Retrieval posture for /health."""
    store = store or MemoryStore()
    try:
        index = index or VectorIndex()
        stats = index.stats()
    except Exception as exc:
        return {"mode": "keyword_only", "reason": str(exc)}

    return {
        "mode": "hybrid" if stats["indexed"] else "keyword_only",
        "index": stats,
        "fusion": "reciprocal rank fusion",
    }
