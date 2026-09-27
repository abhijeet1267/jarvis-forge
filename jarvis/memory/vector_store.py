"""Vector index over memories (v0.9).

Vectors live in the existing ``embedding`` column of the ``memories`` table, so
there is no second store to keep in sync and no vector database to run. Search
is a linear cosine scan in pure Python, which is comfortably fast at the scale a
personal assistant accumulates.

If anything here fails, the caller falls back to keyword search. Vector
retrieval is an *enhancement*; it is never the only way to find a memory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..db.database import get_db
from ..logging_setup import get_logger
from .embeddings import Embedder, cosine, get_embedder

log = get_logger("memory.vector")

#: Only re-embed when the text actually changed, tracked by this column.
_STALE_MARKER = ""


@dataclass
class VectorHit:
    memory_id: str
    score: float

    def as_dict(self) -> Dict[str, Any]:
        return {"memory_id": self.memory_id, "score": round(self.score, 4)}


class VectorIndex:
    """Cosine similarity search over stored memory embeddings."""

    def __init__(self, embedder: Optional[Embedder] = None) -> None:
        self.db = get_db()
        self.embedder = embedder or get_embedder()

    # -- indexing ------------------------------------------------------------
    def index_item(self, memory_id: str, content: str) -> bool:
        """Compute and store the embedding for one memory."""
        try:
            vector = self.embedder.embed(content)
        except Exception as exc:
            log.info("embedding failed", extra_fields={"memory_id": memory_id, "error": str(exc)})
            return False

        self.db.execute(
            "UPDATE memories SET embedding = ? WHERE id = ?",
            (json.dumps([round(v, 6) for v in vector]), memory_id),
        )
        return True

    def index_all(self, limit: int = 5000) -> int:
        """Embed every memory that does not have one yet."""
        rows = self.db.query(
            "SELECT id, content FROM memories "
            "WHERE embedding IS NULL OR embedding = ? LIMIT ?",
            (_STALE_MARKER, limit),
        )
        indexed = 0
        for row in rows:
            if self.index_item(row["id"], row["content"]):
                indexed += 1
        if indexed:
            log.info("vector index built", extra_fields={"indexed": indexed})
        return indexed

    def forget(self, memory_id: str) -> None:
        """Vectors are stored on the row, so deleting the row removes them."""
        self.db.execute("UPDATE memories SET embedding = NULL WHERE id = ?", (memory_id,))

    # -- search --------------------------------------------------------------
    def _load(self) -> List[Tuple[str, List[float]]]:
        rows = self.db.query(
            "SELECT id, embedding FROM memories "
            "WHERE embedding IS NOT NULL AND embedding != '' LIMIT 5000"
        )
        loaded: List[Tuple[str, List[float]]] = []
        for row in rows:
            try:
                vector = json.loads(row["embedding"])
            except (TypeError, ValueError):
                continue
            if isinstance(vector, list) and vector:
                loaded.append((row["id"], [float(v) for v in vector]))
        return loaded

    def search(self, query: str, limit: int = 10, min_score: float = 0.0) -> List[VectorHit]:
        """Rank memories by cosine similarity to the query."""
        if not query.strip():
            return []

        try:
            query_vector = self.embedder.embed(query)
        except Exception as exc:
            log.info("query embedding failed", extra_fields={"error": str(exc)})
            return []

        hits: List[VectorHit] = []
        for memory_id, vector in self._load():
            score = cosine(query_vector, vector)
            if score > min_score:
                hits.append(VectorHit(memory_id=memory_id, score=score))

        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]

    # -- status --------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        total = self.db.query("SELECT COUNT(*) AS n FROM memories")
        indexed = self.db.query(
            "SELECT COUNT(*) AS n FROM memories WHERE embedding IS NOT NULL AND embedding != ''"
        )
        total_n = total[0]["n"] if total else 0
        indexed_n = indexed[0]["n"] if indexed else 0
        return {
            "memories": total_n,
            "indexed": indexed_n,
            "pending": max(0, total_n - indexed_n),
            "coverage": round(indexed_n / total_n, 3) if total_n else 1.0,
            "embedder": self.embedder.describe(),
        }

    def rebuild(self) -> int:
        """Drop and recompute every vector. Used when the embedder changes."""
        self.db.execute("UPDATE memories SET embedding = NULL")
        return self.index_all()
