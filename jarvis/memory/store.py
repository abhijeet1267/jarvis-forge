"""Memory store: short-term, long-term and project memory (section 30).

Retrieval is intentionally simple and dependency-free: a keyword/BM25-lite
scorer over SQLite rows, with an optional embedding column ready for a later
vector backend. This keeps the core runnable offline.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any, Dict, List, Optional

from datetime import datetime

from ..core.models import MemoryItem, MemoryScope, MemoryType, PrivacyLevel, utcnow
from ..db.database import get_db
from ..logging_setup import get_logger

log = get_logger("memory.store")

_WORD = re.compile(r"[a-z0-9]+")

# Words that carry no retrieval signal. Without this, "what did I work on
# yesterday" scores a memory containing "what" as highly as one about work.
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "i", "me", "my", "mine", "you", "your", "we", "our", "it", "its",
    "do", "does", "did", "done", "have", "has", "had", "will", "would",
    "can", "could", "should", "shall", "may", "might", "must",
    "to", "of", "in", "on", "at", "by", "for", "with", "about", "from",
    "and", "or", "but", "if", "then", "than", "as", "so", "that", "this",
    "what", "which", "who", "whom", "when", "where", "why", "how",
    "any", "all", "some", "no", "not", "yes", "just", "very", "also",
    "please", "jarvis", "ok", "okay", "well", "now", "get", "got",
}

# Crude suffix stripping. A real stemmer would be better, but this reliably
# unifies the forms that actually appear in spoken commands.
_SUFFIXES = ("ing", "ers", "er", "ies", "ied", "ed", "es", "s")


def _stem(word: str) -> str:
    """Very light suffix stripping, guarded against mangling short words."""
    if len(word) <= 4:
        return word
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            base = word[: -len(suffix)]
            # "studies" -> "studi" reads badly; prefer "study" style endings.
            if suffix in ("ies", "ied"):
                base += "y"
            return base
    return word


def _tokens(text: str, *, keep_stopwords: bool = False) -> List[str]:
    """Tokenise for retrieval: lowercase, drop stopwords, light stemming."""
    words = _WORD.findall(text.lower())
    if keep_stopwords:
        return words
    return [_stem(w) for w in words if w not in _STOPWORDS and len(w) > 1]


def normalise(text: str) -> str:
    """Collapse case and whitespace so stored copies can be compared verbatim."""
    return " ".join((text or "").casefold().split())


def like_pattern(text: str) -> str:
    """A ``LIKE`` pattern matching ``text`` literally, wildcards escaped.

    Public because ``forget`` needs the same matching outside this table. SQLite
    ``LIKE`` is case-insensitive for ASCII, which is enough for copy detection;
    it is not a substitute for :func:`normalise` when the result is a claim.
    """
    escaped = (text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class MemoryStore:
    """CRUD + relevance search over memory items."""

    def __init__(self) -> None:
        self.db = get_db()

    # -- write ---------------------------------------------------------------
    def remember(
        self,
        content: str,
        *,
        scope: MemoryScope = MemoryScope.SESSION,
        source: str = "user",
        project_id: Optional[str] = None,
        privacy: PrivacyLevel = PrivacyLevel.INTERNAL,
        confidence: float = 1.0,
        tags: Optional[List[str]] = None,
        memory_type: Optional[MemoryType] = None,
        importance: float = 0.5,
        expires_at: Optional[datetime] = None,
        relationships: Optional[Dict[str, List[str]]] = None,
        index: bool = True,
    ) -> MemoryItem:
        """Store a fact. ``private``/``secret`` items are never auto-shared.

        v0.9 adds a type, importance, optional expiry and relationships. The
        vector is computed on write so retrieval never has to backfill lazily;
        pass ``index=False`` to skip it.
        """
        item = MemoryItem(
            content=content,
            scope=scope,
            source=source,
            project_id=project_id,
            privacy_level=privacy,
            confidence=confidence,
            tags=tags or [],
            memory_type=memory_type or MemoryType.GENERAL,
            importance=max(0.0, min(1.0, float(importance))),
            expires_at=expires_at,
            relationships=relationships or {},
        )
        self.db.execute(
            """
            INSERT INTO memories
                (id, content, source, scope, privacy_level, project_id,
                 confidence, tags, created_at, updated_at, memory_type,
                 importance, expires_at, relationships)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.content,
                item.source,
                item.scope.value,
                item.privacy_level.value,
                item.project_id,
                item.confidence,
                json.dumps(item.tags),
                item.created_at.isoformat(),
                item.updated_at.isoformat(),
                item.memory_type.value,
                item.importance,
                item.expires_at.isoformat() if item.expires_at else None,
                json.dumps(item.relationships),
            ),
        )
        if index:
            # Best effort: a failure here must not lose the memory itself.
            try:
                from .vector_store import VectorIndex

                VectorIndex().index_item(item.id, item.content)
            except Exception as exc:
                log.info("could not index memory", extra_fields={"error": str(exc)})

        log.info(
            "memory stored",
            extra_fields={
                "id": item.id,
                "scope": item.scope.value,
                "type": item.memory_type.value,
                "project": project_id,
            },
        )
        return item

    def forget(self, memory_id: str) -> bool:
        """Delete a memory item (user-requested data deletion).

        Returns whether a row actually matched. This used to answer ``True``
        unconditionally, so a caller could not tell "forgotten" from "there was
        never anything with that id" -- and a user-facing "forget" could report a
        deletion it had not performed. Note that deleting *this row* is not the
        same as forgetting *the fact*; see :meth:`quoting`.
        """
        with self.db.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return bool(cur.rowcount)

    def quoting(
        self,
        text: str,
        *,
        exclude_id: str = "",
        limit: int = 200,
        min_length: int = 12,
    ) -> List[MemoryItem]:
        """Live memories that still reproduce ``text``, in whole or in part.

        One remembered fact is not necessarily one row: ``JarvisCore`` writes a
        session-scoped echo of every executed command that quotes both the user's
        wording and the tool's reply, so the same sentence lives in two rows of
        this table. Any caller that reports a fact as forgotten therefore has to
        check whether this table still serves it, which is what this is for.

        Two shapes count as a copy, because the duplication runs both ways:

        * a row that contains ``text`` -- the echo of the command that stored it;
        * a row contained *inside* ``text`` -- deleting that echo must not read as
          complete forgetting while the shorter original is still stored. The
          ``min_length`` floor keeps an unrelated short row from matching.

        Matching is deliberately narrow -- verbatim containment, ignoring case and
        runs of whitespace -- so a paraphrase is *not* counted as a copy. Treat an
        empty result as "no row reproduces this text", never as proof that nothing
        else survives: rows in other tables (the transcript, the audit trail, the
        project graph) are outside this method.
        """
        probe = normalise(text)
        if not probe:
            return []
        rows = self.db.query(
            "SELECT * FROM memories WHERE id != ? ORDER BY created_at DESC LIMIT 1000",
            (exclude_id,),
        )
        copies: List[MemoryItem] = []
        for row in rows:
            stored = normalise(row["content"])
            if not stored:
                continue
            if probe in stored or (len(stored) >= min_length and stored in probe):
                copies.append(self._row_to_item(row))
            if len(copies) >= limit:
                break
        return copies

    # -- read ----------------------------------------------------------------
    def get(self, memory_id: str) -> Optional[MemoryItem]:
        rows = self.db.query("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return self._row_to_item(rows[0]) if rows else None

    def all(
        self,
        scope: Optional[MemoryScope] = None,
        project_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[MemoryItem]:
        sql = "SELECT * FROM memories WHERE 1=1"
        params: List[Any] = []
        if scope:
            sql += " AND scope = ?"
            params.append(scope.value)
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_item(r) for r in self.db.query(sql, tuple(params))]

    def search(self, query: str, limit: int = 5) -> List[MemoryItem]:
        """Rank memories against a query using a BM25-style scorer.

        Improvements over plain token overlap:

        * stopwords are dropped, so "what did I work on" is judged on "work"
        * light stemming unifies "running"/"run" and "tests"/"test"
        * rare terms are weighted higher than common ones (inverse document
          frequency), so a distinctive word dominates a generic one
        * recency nudges newer memories ahead of older ones
        """
        query_terms = Counter(_tokens(query))
        if not query_terms:
            return []

        rows = self.db.query("SELECT * FROM memories ORDER BY created_at DESC LIMIT 1000")
        if not rows:
            return []

        documents = []
        for row in rows:
            item = self._row_to_item(row)
            counts = Counter(_tokens(item.content))
            if counts:
                documents.append((item, counts))
        if not documents:
            return []

        total_docs = len(documents)
        doc_freq = Counter()
        for _item, counts in documents:
            doc_freq.update(counts.keys())

        avg_length = sum(sum(c.values()) for _i, c in documents) / total_docs
        k1, b = 1.5, 0.75
        now = utcnow()

        scored: List[tuple[float, MemoryItem]] = []
        for item, counts in documents:
            length = sum(counts.values()) or 1
            score = 0.0
            for term, q_count in query_terms.items():
                tf = counts.get(term, 0)
                if not tf:
                    continue
                # Inverse document frequency with a smoothing floor.
                idf = math.log(1 + (total_docs - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
                denominator = tf + k1 * (1 - b + b * length / avg_length)
                score += idf * (tf * (k1 + 1) / denominator) * q_count

            if score <= 0:
                continue

            # Confidence, scope and recency all modulate the raw relevance.
            score *= item.confidence
            if item.scope in (MemoryScope.LONG_TERM, MemoryScope.PROJECT):
                score *= 1.15

            created = item.created_at
            if created is not None:
                if created.tzinfo is None:
                    created = created.replace(tzinfo=now.tzinfo)
                age_days = max(0.0, (now - created).total_seconds() / 86400.0)
                score *= 1.0 / (1.0 + age_days / 90.0)

            scored.append((score, item))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _score, item in scored[:limit]]

    def stats(self) -> Dict[str, int]:
        rows = self.db.query("SELECT scope, COUNT(*) AS n FROM memories GROUP BY scope")
        out = {r["scope"]: r["n"] for r in rows}
        out["total"] = sum(out.values())
        return out

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _row_to_item(row: Any) -> MemoryItem:
        return MemoryItem(
            id=row["id"],
            content=row["content"],
            source=row["source"] or "user",
            scope=MemoryScope(row["scope"]),
            privacy_level=PrivacyLevel(row["privacy_level"] or "internal"),
            project_id=row["project_id"],
            confidence=row["confidence"] if row["confidence"] is not None else 1.0,
            tags=json.loads(row["tags"] or "[]"),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            memory_type=_memory_type(row),
            importance=_column(row, "importance", 0.5) or 0.5,
            expires_at=_parse_dt(_column(row, "expires_at", None)) if _column(row, "expires_at", None) else None,
            relationships=json.loads(_column(row, "relationships", "{}") or "{}"),
        )


def _column(row: Any, name: str, default: Any = None) -> Any:
    """Read a column that may not exist yet (pre-migration rows)."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def _memory_type(row: Any) -> MemoryType:
    raw = _column(row, "memory_type", "general") or "general"
    try:
        return MemoryType(raw)
    except ValueError:
        return MemoryType.GENERAL


def _parse_dt(value: Optional[str]):
    if not value:
        return utcnow()
    try:
        from datetime import datetime

        return datetime.fromisoformat(value)
    except ValueError:
        return utcnow()
